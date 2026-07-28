from __future__ import annotations

import json

from hello_agents.career.evaluation import (
    EvaluationPolicy,
    JobApplicationRoundEvaluator,
    evaluation_result_to_dict,
)
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.runtime import AgentCore
from hello_agents.tools.registry import ToolRegistry


def _evaluation_core(policy: EvaluationPolicy) -> AgentCore:
    core = AgentCore(ControlledExecution(ToolRegistry()))
    evaluator = JobApplicationRoundEvaluator(policy)
    core.register_evaluator(evaluator.name, evaluator)
    return core


def test_job_application_round_evaluator_uses_final_eligible_denominator():
    policy = EvaluationPolicy(
        imported_cohort_target=2,
        min_confirmed_submission_rate=0.8,
        min_terminal_audit_coverage=1.0,
    )
    core = _evaluation_core(policy)

    result = core.evaluate_round(
        "job_application_round",
        {
            "state": {"run_id": "round-1", "phase": "complete"},
            "manifest": {
                "counts": {
                    "imported": 2,
                    "shortlisted": 2,
                    "prepared": 2,
                }
            },
            "audit": {
                "counts": {
                    "total": 2,
                    "submitted": 1,
                    "skipped": 1,
                    "submit_clicked_unconfirmed": 0,
                },
                "progress": {"complete": True},
                "applications": [
                    {"status": "submitted"},
                    {
                        "status": "skipped_policy_denied",
                        "email": "private@example.com",
                    },
                ],
            },
        },
        round_id="round-1",
    )
    payload = evaluation_result_to_dict(result)

    assert result.status == "passed"
    assert payload["counts"]["final_eligible"] == 1
    assert (
        payload["rates"][
            "confirmed_submission_rate_final_eligible"
        ]
        == 1.0
    )
    assert payload["agent_core"]["evaluator"] == "job_application_round"
    assert payload["agent_core"]["round_id"] == "round-1"
    assert "private@example.com" not in json.dumps(payload)


def test_job_application_round_evaluator_reports_pending_without_audit():
    core = _evaluation_core(EvaluationPolicy(imported_cohort_target=1))

    result = core.evaluate_round(
        "job_application_round",
        {
            "state": {"run_id": "round-2", "phase": "prepared"},
            "manifest": {
                "counts": {
                    "imported": 1,
                    "shortlisted": 1,
                    "prepared": 1,
                }
            },
            "audit": {},
        },
        round_id="round-2",
    )

    assert result.status == "pending"
    assert (
        result.metrics["assessment"]["terminal_audit_coverage"][
            "status"
        ]
        == "pending"
    )


def test_job_application_round_evaluator_recommends_reconciliation():
    core = _evaluation_core(
        EvaluationPolicy(
            imported_cohort_target=1,
            min_confirmed_submission_rate=0.8,
        )
    )

    result = core.evaluate_round(
        "job_application_round",
        {
            "state": {"run_id": "round-3", "phase": "complete"},
            "manifest": {
                "counts": {
                    "imported": 1,
                    "shortlisted": 1,
                    "prepared": 1,
                }
            },
            "audit": {
                "counts": {
                    "total": 1,
                    "submitted": 0,
                    "skipped": 0,
                    "submit_clicked_unconfirmed": 1,
                },
                "progress": {"complete": True},
                "applications": [
                    {"status": "submit_clicked_unconfirmed"}
                ],
            },
        },
        round_id="round-3",
    )

    assert result.status == "needs_attention"
    assert any(
        "Reconcile every unconfirmed" in item
        for item in result.recommendations
    )
