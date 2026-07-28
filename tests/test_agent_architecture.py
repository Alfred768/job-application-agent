from __future__ import annotations

import json
from pathlib import Path

import pytest

from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.core.contracts import (
    AgentEvaluationResult,
    Plan,
    ToolCall,
    ToolEffect,
)
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.memory import (
    InMemoryLongTermMemory,
    NullLongTermMemory,
    ShortTermMemory,
)
from hello_agents.core.perception import StructuredPerception
from hello_agents.core.runtime import AgentCore
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.registry import ToolRegistry
from job_agent.execution import evaluate_browser_execution_policy
from job_agent.memory import SQLiteApplicationMemory
from job_agent.repair_orchestrator import evaluate_repair_policy
from job_agent.runtime_filler import render_runtime_autofill_script


class RecordingTool(Tool):
    def __init__(
        self,
        name: str = "recording",
        *,
        effect: ToolEffect = ToolEffect.READ,
    ) -> None:
        super().__init__(name, "record one value", effect=effect)
        self.calls: list[dict] = []

    def run(self, parameters):
        self.calls.append(dict(parameters))
        return {"recorded": parameters["value"]}

    def get_parameters(self):
        return [
            ToolParameter(
                name="value",
                type="string",
                description="Value to record.",
            )
        ]


class FakeLLM:
    provider = "deterministic"

    def invoke(self, messages, **kwargs):
        return ""


def _policy_decision(call: ToolCall):
    return JobApplicationPolicyGate().evaluate(
        call,
        short_term_memory=ShortTermMemory(),
        long_term_memory=NullLongTermMemory(),
    )


def test_perception_normalizes_environment_and_tool_feedback():
    perception = StructuredPerception()
    form = perception.observe_form('[{"label": "Email", "required": true}]')
    invalid = perception.observe_form("{not-json")

    assert form.kind == "form"
    assert form.source == "ats_page"
    assert form.payload["valid"] is True
    assert form.payload["fields"][0]["label"] == "Email"
    assert invalid.payload["valid"] is False
    assert invalid.payload["error"].startswith("invalid_form_snapshot:")


def test_agent_core_returns_structured_results_and_feedback():
    registry = ToolRegistry()
    tool = RecordingTool()
    registry.register_tool(tool)
    memory = ShortTermMemory()
    execution = ControlledExecution(
        registry,
        policy_gate=JobApplicationPolicyGate(),
        short_term_memory=memory,
        long_term_memory=InMemoryLongTermMemory(),
    )
    core = AgentCore(execution)

    run = core.run_plan(
        Plan(
            objective="Record a safe value.",
            steps=(
                ToolCall(
                    tool_name="recording",
                    parameters={"value": "ok"},
                    effect=ToolEffect.READ,
                ),
            ),
        )
    )

    assert run.status == "completed"
    assert run.outputs == {"recording": {"recorded": "ok"}}
    assert run.results[0].policy_decision is not None
    assert run.results[0].policy_decision.allowed is True
    assert memory.tool_results == run.results
    assert memory.observations[-1].kind == "tool_result"


def test_agent_core_closed_loop_preserves_every_round_transition():
    registry = ToolRegistry()
    first_tool = RecordingTool("first")
    second_tool = RecordingTool("second")
    registry.register_tool(first_tool)
    registry.register_tool(second_tool)
    memory = ShortTermMemory()
    long_term_memory = InMemoryLongTermMemory()
    core = AgentCore(
        ControlledExecution(
            registry,
            policy_gate=JobApplicationPolicyGate(),
            short_term_memory=memory,
            long_term_memory=long_term_memory,
        )
    )
    perception = StructuredPerception()
    initial = perception.observe(
        "task",
        "user",
        {"objective": "record two values"},
    )
    plan = core.create_plan(
        "Record two safe values.",
        [
            ToolCall(
                tool_name="first",
                parameters={"value": "one"},
                purpose="Record the first value.",
            ),
            ToolCall(
                tool_name="second",
                parameters={"value": "two"},
                purpose="Record the second value.",
            ),
        ],
    )

    result = core.run_loop(
        plan,
        initial_observation=initial,
        remember_rounds=True,
    )

    assert result.status == "completed"
    assert len(result.rounds) == 2
    assert result.observations[0] == initial
    assert result.rounds[1].input_observation == result.rounds[0].new_observation
    for round_ in result.rounds:
        assert round_.thought.observation_id == (
            round_.input_observation.observation_id
        )
        assert round_.thought.selected_action.call_id == round_.action.call_id
        assert round_.tool_result.call_id == round_.action.call_id
        assert round_.new_observation.payload["call_id"] == (
            round_.action.call_id
        )
        assert round_.memory_update.observation_id == (
            round_.new_observation.observation_id
        )
        assert round_.memory_update.short_term_updated is True
        assert round_.memory_update.long_term_updated is True
        assert round_.policy_decision == round_.tool_result.policy_decision
    assert memory.thoughts == result.thoughts
    assert memory.rounds == result.rounds
    assert memory.memory_updates == result.memory_updates


def test_agent_core_runs_one_selected_action_from_a_bounded_plan():
    registry = ToolRegistry()
    first_tool = RecordingTool("first")
    second_tool = RecordingTool("second")
    registry.register_tool(first_tool)
    registry.register_tool(second_tool)
    core = AgentCore(ControlledExecution(registry))
    initial = StructuredPerception().observe("task", "user", {})
    plan = core.create_plan(
        "Select one bounded action.",
        [
            ToolCall(
                tool_name="first",
                parameters={"value": "one"},
            ),
            ToolCall(
                tool_name="second",
                parameters={"value": "two"},
            ),
        ],
    )

    result = core.run_loop(
        plan,
        initial_observation=initial,
        max_rounds=1,
    )

    assert result.status == "in_progress"
    assert [round_.action.tool_name for round_ in result.rounds] == ["first"]
    assert first_tool.calls == [{"value": "one"}]
    assert second_tool.calls == []


def test_agent_core_parallel_reads_join_before_next_sequential_action():
    registry = ToolRegistry()
    first_tool = RecordingTool("first")
    second_tool = RecordingTool("second")
    joined_tool = RecordingTool("joined")
    registry.register_tool(first_tool)
    registry.register_tool(second_tool)
    registry.register_tool(joined_tool)
    core = AgentCore(ControlledExecution(registry))
    initial = StructuredPerception().observe("task", "user", {})

    parallel = core.run_concurrent_read_loop(
        core.create_plan(
            "Inspect independent runtime inputs.",
            [
                ToolCall(
                    tool_name="first",
                    parameters={"value": "one"},
                    effect=ToolEffect.READ,
                ),
                ToolCall(
                    tool_name="second",
                    parameters={"value": "two"},
                    effect=ToolEffect.READ,
                ),
            ],
        ),
        initial_observation=initial,
    )

    assert parallel.status == "completed"
    assert all(
        round_.input_observation == initial
        for round_ in parallel.rounds
    )
    parallel_group_ids = {
        round_.parallel_group_id for round_ in parallel.rounds
    }
    assert len(parallel_group_ids) == 1
    assert None not in parallel_group_ids
    joined = parallel.observations[-1]
    assert joined.kind == "concurrent_read_join"
    assert joined.payload["parallel_group_id"] in parallel_group_ids

    sequential = core.run_loop(
        core.create_plan(
            "Continue after the parallel join.",
            [
                ToolCall(
                    tool_name="joined",
                    parameters={"value": "continued"},
                )
            ],
        ),
        initial_observation=joined,
    )

    assert sequential.rounds[0].input_observation == joined
    assert joined_tool.calls == [{"value": "continued"}]


def test_agent_core_parallel_loop_rejects_environment_writes():
    registry = ToolRegistry()
    registry.register_tool(RecordingTool("write", effect=ToolEffect.WRITE))
    core = AgentCore(ControlledExecution(registry))
    initial = StructuredPerception().observe("task", "user", {})

    with pytest.raises(
        ValueError,
        match="allow only OBSERVE or READ",
    ):
        core.run_concurrent_read_loop(
            core.create_plan(
                "Reject unsafe parallel effects.",
                [
                    ToolCall(
                        tool_name="write",
                        parameters={"value": "unsafe"},
                        effect=ToolEffect.WRITE,
                    )
                ],
            ),
            initial_observation=initial,
        )


def test_agent_core_closed_loop_observes_policy_denial_before_stopping():
    registry = ToolRegistry()
    write_tool = RecordingTool("write", effect=ToolEffect.WRITE)
    registry.register_tool(write_tool)
    memory = ShortTermMemory()
    core = AgentCore(
        ControlledExecution(
            registry,
            policy_gate=JobApplicationPolicyGate(),
            short_term_memory=memory,
        )
    )
    initial = StructuredPerception().observe("task", "user", {})
    plan = core.create_plan(
        "Attempt a duplicate write.",
        [
            ToolCall(
                tool_name="write",
                parameters={"value": "blocked"},
                effect=ToolEffect.WRITE,
                context={"duplicate": True},
            )
        ],
    )

    result = core.run_loop(plan, initial_observation=initial)

    assert result.status == "policy_blocked"
    assert write_tool.calls == []
    assert result.rounds[0].new_observation.kind == "tool_result"
    assert result.rounds[0].new_observation.payload["policy_code"] == (
        "duplicate_application"
    )
    assert result.rounds[0].memory_update.short_term_updated is True


def test_agent_core_registers_evaluators_and_bounds_round_history():
    core = AgentCore(
        ControlledExecution(ToolRegistry()),
        evaluation_history_limit=2,
    )

    def evaluator(request):
        return AgentEvaluationResult(
            evaluator="quality",
            round_id=request.round_id,
            status="passed",
            metrics={"score": request.inputs["score"]},
            evaluation_id=request.evaluation_id,
        )

    core.register_evaluator("quality", evaluator)
    first = core.evaluate_round(
        "quality",
        {"score": 0.8},
        round_id="round-1",
    )
    core.evaluate_round(
        "quality",
        {"score": 0.9},
        round_id="round-2",
    )
    last = core.evaluate_round(
        "quality",
        {"score": 1.0},
        round_id="round-3",
    )

    assert first.metrics == {"score": 0.8}
    assert last.status == "passed"
    assert [item.round_id for item in core.evaluation_history] == [
        "round-2",
        "round-3",
    ]
    assert core.capabilities().evaluators == ("quality",)
    assert core.capabilities().evaluation_history_enabled is True
    assert (
        core.execution.short_term_memory.observations[-1].kind
        == "agent_evaluation"
    )
    assert (
        core.execution.short_term_memory.observations[-1].payload[
            "round_id"
        ]
        == "round-3"
    )
    core.clear_evaluation_history()
    assert core.evaluation_history == ()


def test_execution_uses_tool_effect_when_plan_understates_risk():
    registry = ToolRegistry()
    submit_tool = RecordingTool("submit_application", effect=ToolEffect.SUBMIT)
    registry.register_tool(submit_tool)
    execution = ControlledExecution(
        registry,
        policy_gate=JobApplicationPolicyGate(),
    )

    result = execution.execute(
        ToolCall(
            tool_name="submit_application",
            parameters={"value": "attempt"},
            effect=ToolEffect.READ,
        )
    )

    assert result.ok is False
    assert result.effect is ToolEffect.SUBMIT
    assert result.policy_decision is not None
    assert result.policy_decision.code == "submission_disabled"
    assert submit_tool.calls == []


def test_career_policy_blocks_forbidden_and_unsafe_actions():
    linkedin = _policy_decision(
        ToolCall(
            tool_name="rss_job_source",
            parameters={"rss_url": "https://www.linkedin.com/jobs/feed"},
            effect=ToolEffect.READ,
        )
    )
    duplicate = _policy_decision(
        ToolCall(
            tool_name="application_tracker",
            parameters={},
            effect=ToolEffect.WRITE,
            context={"duplicate": True},
        )
    )
    terminal_retry = _policy_decision(
        ToolCall(
            tool_name="browser_execute",
            parameters={},
            effect=ToolEffect.WRITE,
            context={
                "retry": True,
                "terminal_status": "email_verification_required",
            },
        )
    )
    recovered_retry = _policy_decision(
        ToolCall(
            tool_name="browser_execute",
            parameters={},
            effect=ToolEffect.WRITE,
            context={
                "retry": True,
                "terminal_status": "email_verification_required",
                "recovery_verified": True,
                "retry_scope": "single_application",
            },
        )
    )
    unscoped_recovered_retry = _policy_decision(
        ToolCall(
            tool_name="browser_execute",
            parameters={},
            effect=ToolEffect.WRITE,
            context={
                "retry": True,
                "terminal_status": "email_verification_required",
                "recovery_verified": True,
                "retry_scope": "whole_batch",
            },
        )
    )
    unresolved_submit = _policy_decision(
        ToolCall(
            tool_name="browser_execute",
            parameters={},
            effect=ToolEffect.SUBMIT,
            context={
                "submit_complete": True,
                "facts_verified": True,
                "blocking_review_items": ["Work authorization"],
                "unapproved_sensitive_fields": ["Work authorization"],
                "resume_verified": True,
                "confirmation_required": True,
            },
        )
    )

    assert linkedin.code == "linkedin_automation_forbidden"
    assert duplicate.code == "duplicate_application"
    assert terminal_retry.code == "protected_terminal_retry"
    assert recovered_retry.allowed is True
    assert unscoped_recovered_retry.code == "unscoped_recovery_retry"
    assert unresolved_submit.code == "blocking_review_items"


def test_repair_policy_requires_repairable_offline_isolated_path():
    denied = evaluate_repair_policy(
        {
            "findings": [
                {
                    "status": "submission_blocked_by_anti_spam",
                }
            ],
            "constraints": {
                "real_browser_verification": False,
                "real_submission": False,
            },
        }
    )
    real_submission = evaluate_repair_policy(
        {
            "findings": [{"status": "autofill_failed"}],
            "constraints": {
                "real_browser_verification": False,
                "real_submission": True,
            },
        }
    )
    allowed = evaluate_repair_policy(
        {
            "findings": [{"status": "autofill_timed_out"}],
            "constraints": {
                "real_browser_verification": False,
                "real_submission": False,
            },
        }
    )

    assert denied.allowed is False
    assert denied.code == "non_repairable_status"
    assert real_submission.allowed is False
    assert real_submission.code == "real_environment_repair_verification"
    assert allowed.allowed is True


def test_sqlite_long_term_memory_sanitizes_records(tmp_path: Path):
    memory = SQLiteApplicationMemory(tmp_path / "agent.db")
    memory.remember(
        "agent_run",
        {
            "company": "Acme",
            "title": "Engineer",
            "email": "private@example.com",
            "profile": {"phone": "555-0100"},
        },
    )

    records = memory.search("Acme")

    assert records[0]["namespace"] == "agent_run"
    assert records[0]["company"] == "Acme"
    serialized = json.dumps(records)
    assert "private@example.com" not in serialized
    assert "555-0100" not in serialized


def test_job_application_agent_composes_all_architecture_modules():
    agent = JobApplicationAgent(
        name="career-agent",
        llm=FakeLLM(),
        form_snapshot_json='[{"label": "Email", "required": true}]',
        profile_json='{"email": "candidate@example.com"}',
    )

    agent.run("Company: Acme\nTitle: Engineer\n\nBuild Python services.")
    state = agent.get_last_state()

    assert state is not None
    assert state.architecture_status == "completed"
    assert len(state.rounds) == len(state.tool_results)
    assert len(state.thoughts) == len(state.rounds)
    assert len(state.memory_updates) == len(state.rounds)
    assert state.tool_results
    assert all(
        result.policy_decision is not None
        for result in state.tool_results
    )
    assert {observation.kind for observation in state.observations} >= {
        "job",
        "form",
        "tool_result",
    }
    assert state.policy_decisions
    assert state.policy_decisions[-1].code == "submission_disabled"
    for previous, current in zip(state.rounds, state.rounds[1:]):
        assert current.input_observation == previous.new_observation
    assert all(
        round_.thought.strategy == "plan_and_solve_react_reflection"
        for round_ in state.rounds
    )


def test_job_application_agent_derives_resume_state_from_tool_result(
    tmp_path: Path,
):
    resume_path = tmp_path / "GAOYI_WU_Agent_Engineer.pdf"
    resume_path.write_text("pdf")
    agent = JobApplicationAgent(
        name="career-agent",
        llm=FakeLLM(),
        resume_source_dir=tmp_path,
    )

    agent.run("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    state = agent.get_last_state()

    assert state is not None
    assert state.selected_resume is not None
    resume_round = next(
        round_
        for round_ in state.rounds
        if round_.action.tool_name == "resume_selector"
    )
    assert state.selected_resume.pdf_path == Path(
        str(resume_round.tool_result.output).split("selected_pdf=", 1)[1]
    )


def test_browser_policy_blocks_linkedin_for_generated_runtime(tmp_path: Path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={"name": "Candidate"},
            application_url="https://www.linkedin.com/jobs/view/123",
        )
    )

    decision = evaluate_browser_execution_policy(
        {},
        script,
        real_runtime=True,
        environ={"JOB_AGENT_SUBMIT_COMPLETE": "1"},
    )

    assert decision.allowed is False
    assert decision.code == "linkedin_automation_forbidden"
