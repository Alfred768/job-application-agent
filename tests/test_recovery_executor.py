from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.career.recovery import JobApplicationRecoveryPlanner
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.runtime import AgentCore
from hello_agents.tools.registry import ToolRegistry
from job_agent.recovery_executor import (
    JobApplicationRecoveryExecutor,
    execute_audit_recovery,
    write_recovery_retry_batch,
)


def _core(executor: JobApplicationRecoveryExecutor) -> AgentCore:
    core = AgentCore(
        ControlledExecution(
            ToolRegistry(),
            policy_gate=JobApplicationPolicyGate(),
        )
    )
    planner = JobApplicationRecoveryPlanner({})
    core.register_recovery_planner(planner.name, planner)
    core.register_recovery_executor(executor.name, executor)
    return core


def test_anti_spam_recovery_executes_evidence_and_cooldown_through_core(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "applications" / "one" / "rejection.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("possible spam")
    executor = JobApplicationRecoveryExecutor(
        run_dir=tmp_path,
        now=lambda: datetime(2026, 7, 28, 13, 0, tzinfo=timezone.utc),
    )
    core = _core(executor)
    plan = core.plan_recovery(
        "submission_blocked_by_anti_spam",
        {"evidence": str(evidence)},
        planner="job_application",
    )
    assert plan is not None

    result = core.execute_recovery(
        "job_application",
        plan,
        {
            "terminal_status": plan.status,
            "retry_scope": "single_application",
            "evidence": str(evidence),
        },
    )

    assert result.status == "pending"
    assert "anti_spam_rejection" in result.evidence
    assert result.retry_ready is False
    assert result.agent_loops
    assert all(loop.rounds for loop in result.agent_loops)
    assert (
        result.agent_loops[0].rounds[0].thought.selected_action.tool_name
        == "job_application_recovery"
    )
    tool_results = core.execution.short_term_memory.tool_results
    assert tool_results
    assert all(item.tool_name == "job_application_recovery" for item in tool_results)
    assert all(item.policy_decision.allowed for item in tool_results)


def test_candidate_fact_recovery_waits_without_executing_a_tool(
    tmp_path: Path,
) -> None:
    executor = JobApplicationRecoveryExecutor(run_dir=tmp_path)
    core = _core(executor)
    plan = core.plan_recovery(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": "Work authorization",
                    "reason": "no approved answer",
                    "sensitive": True,
                    "blocking": True,
                }
            ]
        },
        planner="job_application",
    )
    assert plan is not None

    result = core.execute_recovery(
        "job_application",
        plan,
        {
            "terminal_status": plan.status,
            "retry_scope": "single_application",
        },
    )

    assert result.status == "waiting_for_user"
    assert result.retry_ready is False
    assert core.execution.short_term_memory.tool_results
    assert all(
        item.call_id
        for item in core.execution.short_term_memory.tool_results
    )


def test_injected_email_adapters_can_verify_a_scoped_recovery(
    tmp_path: Path,
) -> None:
    def completed(action, _context, _private):
        evidence = {
            "poll_verification_message": ["verification_request"],
            "apply_verification": ["verification_accepted"],
            "resume_same_application": [],
        }[action.action]
        return {
            "status": "completed",
            "evidence": evidence,
            "message": "adapter completed",
        }

    executor = JobApplicationRecoveryExecutor(
        run_dir=tmp_path,
        environ={"JOB_AGENT_GMAIL_TOKEN_FILE": str(tmp_path / "token.json")},
        handlers={
            "poll_verification_message": completed,
            "apply_verification": completed,
            "resume_same_application": completed,
        },
    )
    (tmp_path / "token.json").write_text("{}")
    core = _core(executor)
    planner = JobApplicationRecoveryPlanner(
        {"JOB_AGENT_GMAIL_TOKEN_FILE": str(tmp_path / "token.json")}
    )
    plan = planner("email_verification_required", {})
    assert plan is not None

    result = core.execute_recovery(
        "job_application",
        plan,
        {
            "terminal_status": plan.status,
            "retry_scope": "single_application",
        },
    )

    assert result.status == "verified"
    assert result.retry_ready is True
    assert set(result.evidence) == {
        "verification_request",
        "verification_accepted",
    }


def test_unconfirmed_click_does_not_persist_without_confirmation(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "applications" / "one" / "click.txt"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("Submit clicked but confirmation not detected")
    audit = {
        "applications": [
            {
                "company": "Block",
                "title": "Engineer",
                "status": "submit_clicked_unconfirmed",
                "application_id": "10",
                "evidence": str(evidence),
            }
        ]
    }

    batch = execute_audit_recovery(
        audit,
        run_dir=tmp_path,
        database=None,
        environ={},
    )

    execution = audit["applications"][0]["recovery_execution"]
    assert execution["status"] == "pending"
    assert execution["retry_ready"] is False
    persist = next(
        item
        for item in execution["actions"]
        if item["action"] == "persist_confirmed_outcome"
    )
    assert persist["status"] == "pending"
    assert execution["agent_loops"]
    assert all(item["rounds"] for item in execution["agent_loops"])
    assert batch.verified_targets == ()


def test_verified_recovery_batch_is_scoped_and_policy_annotated(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "applications" / "one"
    batch_path = tmp_path / "batch-summary.json"
    batch_path.write_text(
        json.dumps(
            [
                {
                    "company": "Example",
                    "title": "Engineer",
                    "application_id": "42",
                    "package_dir": str(package_dir),
                },
                {
                    "company": "Other",
                    "title": "Engineer",
                    "application_id": "43",
                    "package_dir": str(tmp_path / "applications" / "two"),
                },
            ]
        )
    )
    output_path = tmp_path / "recovery" / "retry.json"

    written = write_recovery_retry_batch(
        batch_path,
        verified_targets=(
            {
                "company": "Example",
                "title": "Engineer",
                "application_id": "42",
                "package_dir": str(package_dir),
                "terminal_status": "email_verification_required",
                "recovery_verified": True,
                "retry_scope": "single_application",
            },
        ),
        output_path=output_path,
    )

    assert written == output_path
    items = json.loads(output_path.read_text())
    assert len(items) == 1
    assert items[0]["application_id"] == "42"
    assert items[0]["retry"] is True
    assert items[0]["recovery_verified"] is True
    assert items[0]["retry_scope"] == "single_application"
    assert items[0]["recovery_attempt"] == 1
