"""Read-only per-round evaluation for the job application Agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from hello_agents.core.contracts import (
    AgentEvaluationRequest,
    AgentEvaluationResult,
)


@dataclass(frozen=True)
class EvaluationPolicy:
    """Targets used to assess one job-application Agent round."""

    imported_cohort_target: int = 500
    min_confirmed_submission_rate: float = 0.80
    min_terminal_audit_coverage: float = 1.0

    def targets(self) -> dict[str, int | float]:
        return {
            "imported_cohort_target": self.imported_cohort_target,
            "min_confirmed_submission_rate": (
                self.min_confirmed_submission_rate
            ),
            "min_terminal_audit_coverage": (
                self.min_terminal_audit_coverage
            ),
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(values: Mapping[str, Any], name: str) -> int:
    try:
        return max(0, int(values.get(name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _rate(numerator: int, denominator: int | None) -> float | None:
    if denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _threshold_status(
    actual: float | None,
    target: float,
    *,
    pending: bool = False,
) -> str:
    if pending:
        return "pending"
    if actual is None:
        return "not_applicable"
    return "met" if actual >= target else "not_met"


def build_job_application_evaluation_metrics(
    *,
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
    policy: EvaluationPolicy,
) -> dict[str, Any]:
    """Calculate one round from aggregate artifacts without changing gates."""
    pipeline = _mapping(manifest.get("counts"))
    execution = _mapping(audit.get("counts"))
    raw_applications = audit.get("applications")
    applications = (
        raw_applications if isinstance(raw_applications, list) else []
    )
    progress = _mapping(audit.get("progress"))
    audit_present = bool(audit)

    imported = _count(pipeline, "imported")
    shortlisted = _count(pipeline, "shortlisted")
    prepared = _count(pipeline, "prepared")
    executed = _count(execution, "total")
    skipped = min(executed, _count(execution, "skipped"))
    final_eligible = executed - skipped if audit_present else None
    submitted = _count(execution, "submitted")
    terminal_records = sum(
        1
        for item in applications
        if isinstance(item, Mapping)
        and str(item.get("status") or "").strip()
    )
    terminal_coverage = (
        _rate(terminal_records, prepared) if audit_present else None
    )
    confirmed_rate = _rate(submitted, final_eligible)
    imported_confirmation_rate = (
        _rate(submitted, imported) if audit_present else None
    )
    audit_complete = (
        bool(progress.get("complete")) if audit_present else False
    )
    uncertain_submissions = _count(
        execution,
        "submit_clicked_unconfirmed",
    )

    terminal_status = _threshold_status(
        terminal_coverage,
        policy.min_terminal_audit_coverage,
        pending=not audit_present,
    )
    if audit_present and not audit_complete:
        terminal_status = "not_met"

    return {
        "schema_version": 1,
        "run_id": str(state.get("run_id") or ""),
        "phase": str(state.get("phase") or "unknown"),
        "definitions": {
            "raw_imported": (
                "Listings accepted by the source import for this run."
            ),
            "prepared": (
                "Unique, screened listings with a policy-approved "
                "application package."
            ),
            "final_eligible": (
                "Executed applications excluding only terminal safety skips; "
                "this is the denominator for the confirmed-submission "
                "quality target."
            ),
            "confirmed_submission": (
                "Only page-confirmed submissions recorded as submitted."
            ),
            "raw_import_to_confirmed_rate": (
                "A source-quality funnel metric, monitored without a "
                "submission quota."
            ),
        },
        "counts": {
            "imported": imported,
            "shortlisted": shortlisted,
            "prepared": prepared,
            "executed": executed if audit_present else None,
            "safe_skipped": skipped if audit_present else None,
            "final_eligible": final_eligible,
            "submitted": submitted if audit_present else None,
            "terminal_records": (
                terminal_records if audit_present else None
            ),
            "submit_clicked_unconfirmed": (
                uncertain_submissions if audit_present else None
            ),
        },
        "rates": {
            "shortlist_rate": _rate(shortlisted, imported),
            "package_readiness_rate": _rate(prepared, imported),
            "execution_coverage": (
                _rate(executed, prepared) if audit_present else None
            ),
            "terminal_audit_coverage": terminal_coverage,
            "confirmed_submission_rate_final_eligible": confirmed_rate,
            "raw_import_to_confirmed_rate": imported_confirmation_rate,
        },
        "targets": policy.targets(),
        "assessment": {
            "imported_cohort": {
                "status": (
                    "met"
                    if imported >= policy.imported_cohort_target
                    else "insufficient_cohort"
                ),
            },
            "terminal_audit_coverage": {
                "status": terminal_status,
                "audit_complete": (
                    audit_complete if audit_present else None
                ),
            },
            "confirmed_submission_rate_final_eligible": {
                "status": _threshold_status(
                    confirmed_rate,
                    policy.min_confirmed_submission_rate,
                    pending=not audit_present,
                ),
            },
            "submit_clicked_unconfirmed": {
                "target": 0,
                "status": (
                    "pending"
                    if not audit_present
                    else "met"
                    if uncertain_submissions == 0
                    else "not_met"
                ),
            },
        },
    }


def _overall_status(metrics: Mapping[str, Any]) -> str:
    assessment = _mapping(metrics.get("assessment"))
    statuses = {
        str(_mapping(item).get("status") or "")
        for item in assessment.values()
    }
    if "pending" in statuses:
        return "pending"
    if "not_met" in statuses:
        return "needs_attention"
    if "insufficient_cohort" in statuses:
        return "insufficient_cohort"
    if "not_applicable" in statuses:
        return "insufficient_evidence"
    return "passed"


def _recommendations(metrics: Mapping[str, Any]) -> tuple[str, ...]:
    assessment = _mapping(metrics.get("assessment"))
    recommendations: list[str] = []
    if (
        _mapping(assessment.get("terminal_audit_coverage")).get("status")
        == "not_met"
    ):
        recommendations.append(
            "Complete terminal audit coverage before treating the round as "
            "finished."
        )
    if (
        _mapping(
            assessment.get(
                "confirmed_submission_rate_final_eligible"
            )
        ).get("status")
        == "not_met"
    ):
        recommendations.append(
            "Review structured blocker and recovery distributions without "
            "lowering eligibility or submission gates."
        )
    if (
        _mapping(
            assessment.get("submit_clicked_unconfirmed")
        ).get("status")
        == "not_met"
    ):
        recommendations.append(
            "Reconcile every unconfirmed submission click before another "
            "attempt."
        )
    if (
        _mapping(assessment.get("imported_cohort")).get("status")
        == "insufficient_cohort"
    ):
        recommendations.append(
            "Collect more policy-eligible observations before drawing a "
            "stable cohort conclusion."
        )
    return tuple(recommendations)


class JobApplicationRoundEvaluator:
    """Evaluate aggregate round evidence without invoking an LLM or Tool."""

    name = "job_application_round"

    def __init__(
        self,
        policy: EvaluationPolicy | None = None,
    ) -> None:
        self.policy = policy or EvaluationPolicy()

    def __call__(
        self,
        request: AgentEvaluationRequest,
    ) -> AgentEvaluationResult:
        metrics = build_job_application_evaluation_metrics(
            state=_mapping(request.inputs.get("state")),
            manifest=_mapping(request.inputs.get("manifest")),
            audit=_mapping(request.inputs.get("audit")),
            policy=self.policy,
        )
        status = _overall_status(metrics)
        return AgentEvaluationResult(
            evaluator=self.name,
            round_id=request.round_id,
            status=status,
            metrics=metrics,
            summary=(
                "Job application round evaluation completed with status "
                f"'{status}'."
            ),
            recommendations=_recommendations(metrics),
            evaluation_id=request.evaluation_id,
        )


def evaluation_result_to_dict(
    result: AgentEvaluationResult,
) -> dict[str, Any]:
    """Preserve the existing metrics schema and append Core provenance."""
    payload = dict(result.metrics)
    payload["agent_core"] = {
        "schema_version": 1,
        "evaluation_id": result.evaluation_id,
        "evaluator": result.evaluator,
        "round_id": result.round_id,
        "status": result.status,
        "summary": result.summary,
        "recommendations": list(result.recommendations),
        "evaluated_at": result.evaluated_at,
    }
    return payload
