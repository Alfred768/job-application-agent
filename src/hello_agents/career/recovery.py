"""Career recovery planning for non-success application outcomes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from hello_agents.core.contracts import (
    RecoveryAction,
    RecoveryExecutionResult,
    RecoveryPlan,
)
from hello_agents.core.trace import agent_loop_result_to_dict


_EXPLICIT_MISSING_FACT_MARKERS = (
    "candidate fact needs explicit approved answer",
    "no approved answer",
    "profile has no approved",
    "missing candidate fact",
    "truthfulness gate",
    "user-authored",
)
_USER_AUTHORED_FIELD_MARKERS = (
    "exceptional ability",
    "provide us with 3-4 examples",
    "provide us with 3 4 examples",
    "this is your moment to wow us",
    "most interesting paper",
    "paper, blog post",
    "blog post, or documentation",
    "blog post or documentation",
    "you've read",
    "you have read",
    "read in the past month",
    "recently read",
)
_CANDIDATE_FACT_FIELD_MARKERS = (
    "accommodation",
    "able to start",
    "authorization",
    "citizen",
    "citizenship",
    "company employment",
    "contractor",
    "country",
    "currently located",
    "currently live",
    "education",
    "employee",
    "employment",
    "english level",
    "english proficiency",
    "expected graduation",
    "graduation",
    "how familiar",
    "familiarity",
    "have you used",
    "engineering blog",
    "intern season",
    "what season",
    "work on-site",
    "work onsite",
    "work on site",
    "commute",
    "sms",
    "whatsapp",
    "security clearance",
    "export control",
    "immigration",
    "nationality",
    "native name",
    "legal name",
    "offer deadline",
    "other offer",
    "prior work",
    "previously work",
    "related to",
    "relationship",
    "relocat",
    "school",
    "sponsorship",
    "start full time",
    "current residence",
    "years of experience",
    "not including internships",
    "excluding internships",
    "high school name",
    "high school graduation",
    "secondary school name",
    "secondary school graduation",
    "preferred ",
    "preference",
    "would you prefer",
    "which of these roles resonates",
    "resonates the most",
    "where are you spending summer",
)
_CAPTCHA_MARKERS = (
    "captcha blocked automatic submission",
    "captcha recovery failed",
    "error_task_not_supported",
    "recaptcha",
    "hcaptcha",
    "funcaptcha",
)
_RATE_LIMIT_MARKERS = (
    "http 429",
    "status 429",
    "too many requests",
    "rate limit",
    "server limit",
)


def _configured_bool(raw: Any, *, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "no", "off"}


def _configured_file(environ: Mapping[str, str], name: str) -> bool:
    value = str(environ.get(name) or "").strip()
    return bool(value) and Path(value).expanduser().is_file()


def runtime_recovery_capabilities(
    environ: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Return non-secret capability flags for recovery planning."""
    environment = os.environ if environ is None else environ
    gmail_token = _configured_file(environment, "JOB_AGENT_GMAIL_TOKEN_FILE")
    if (
        environ is None
        and not str(
            environment.get("JOB_AGENT_GMAIL_TOKEN_FILE") or ""
        ).strip()
    ):
        gmail_token = (
            Path(".job-agent-secrets") / "gmail-token.json"
        ).is_file()
    account_store = _configured_file(
        environment,
        "JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE",
    )
    if (
        environ is None
        and not str(
            environment.get(
                "JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE"
            )
            or ""
        ).strip()
    ):
        account_store = Path(
            ".job-agent-candidate-passwords.json"
        ).is_file()
    account_credentials = bool(
        str(environment.get("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD") or "").strip()
        or _configured_file(
            environment,
            "JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE",
        )
        or account_store
    )
    return {
        "captcha_solver_configured": bool(
            _configured_bool(environment.get("CAPMONSTER_SOLVE_CAPTCHA"))
            and str(environment.get("CAPMONSTER_API_KEY") or "").strip()
        ),
        "gmail_verification_configured": gmail_token,
        "candidate_account_credentials_configured": account_credentials,
    }


def classify_processing_failure(stdout: str) -> str:
    """Reduce page output to a privacy-safe recovery fingerprint."""
    normalized = str(stdout or "").casefold()
    if any(marker in normalized for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(marker in normalized for marker in _CAPTCHA_MARKERS):
        if "error_task_not_supported" in normalized or "unsupported" in normalized:
            return "captcha_unsupported"
        return "captcha_failed"
    return "site_processing_error"


def requires_approved_candidate_fact(item: Mapping[str, Any]) -> bool:
    """Identify fields whose answer must come from the candidate fact sources."""
    label = str(item.get("label") or "").casefold()
    reason = str(item.get("reason") or "").casefold()
    return bool(item.get("sensitive")) or any(
        marker in reason for marker in _EXPLICIT_MISSING_FACT_MARKERS
    ) or any(
        marker in label
        for marker in (
            *_USER_AUTHORED_FIELD_MARKERS,
            *_CANDIDATE_FACT_FIELD_MARKERS,
        )
    )


def recovery_plan_to_dict(plan: RecoveryPlan) -> dict[str, Any]:
    """Serialize a recovery plan without dataclass or tuple leakage."""
    return {
        "status": plan.status,
        "strategy": plan.strategy,
        "actions": [
            {
                "action": action.action,
                "description": action.description,
                "automatic": action.automatic,
                "requires_user": action.requires_user,
                "parameters": dict(action.parameters),
            }
            for action in plan.actions
        ],
        "retry_allowed": plan.retry_allowed,
        "retry_after_seconds": plan.retry_after_seconds,
        "retry_scope": plan.retry_scope,
        "retry_condition": plan.retry_condition,
        "evidence_required": list(plan.evidence_required),
        "reason": plan.reason,
    }


def recovery_execution_result_to_dict(
    result: RecoveryExecutionResult,
) -> dict[str, Any]:
    return {
        "execution_id": result.execution_id,
        "executed_at": result.executed_at,
        "strategy": result.strategy,
        "status": result.status,
        "actions": [
            {
                "action": action.action,
                "status": action.status,
                "automatic": action.automatic,
                "evidence": list(action.evidence),
                "message": action.message,
                "details": dict(action.details),
            }
            for action in result.actions
        ],
        "evidence": list(result.evidence),
        "retry_ready": result.retry_ready,
        "retry_scope": result.retry_scope,
        "reason": result.reason,
        "agent_loops": [
            agent_loop_result_to_dict(loop)
            for loop in result.agent_loops
        ],
    }


def attach_recovery_plan(
    record: dict[str, Any],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach one plan to a terminal record without replacing existing data."""
    if isinstance(record.get("recovery_plan"), Mapping):
        return record
    plan = JobApplicationRecoveryPlanner()(
        str(record.get("status") or ""),
        {
            **record,
            **dict(context or {}),
        },
    )
    if plan is not None:
        record["recovery_plan"] = recovery_plan_to_dict(plan)
    return record


class JobApplicationRecoveryPlanner:
    """Plan legitimate resolution paths outside the coding-repair lane."""

    name = "job_application"

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ = environ

    def __call__(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan | None:
        normalized = str(status or "").strip().lower()
        merged = runtime_recovery_capabilities(self._environ)
        environment = os.environ if self._environ is None else self._environ
        merged["anti_spam_cooldown_hours"] = (
            environment.get("JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS") or 24
        )
        merged.update(dict(context))
        if normalized == "submission_blocked_by_anti_spam":
            return self._anti_spam(normalized, merged)
        if normalized == "submission_processing_error":
            return self._processing_error(normalized, merged)
        if normalized == "email_verification_required":
            return self._email_verification(normalized, merged)
        if normalized == "candidate_account_required":
            return self._candidate_account(normalized, merged)
        if normalized in {"autofill_failed", "autofill_timed_out"} and (
            bool(merged.get("network_failure"))
            or str(merged.get("error") or "") in {
                "browser_navigation_network_error",
                "browser_launch_error",
                "browser_session_closed",
                "network_circuit_breaker_active",
            }
        ):
            return self._network_health(normalized, merged)
        if (
            normalized == "autofill_failed"
            and str(merged.get("error") or "")
            == "application_form_unavailable"
        ):
            return self._application_form(normalized, merged)
        if normalized == "submit_clicked_unconfirmed":
            return self._clicked_unconfirmed(normalized)
        if (
            normalized == "autofill_failed"
            and str(merged.get("error") or "")
            == "execution_interrupted_unconfirmed"
        ):
            return self._clicked_unconfirmed(normalized)
        if normalized == "autofill_completed_blocked":
            return self._blocked_fields(normalized, merged)
        return None

    def _network_health(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan:
        raw_seconds = context.get("network_cooldown_seconds") or 300
        try:
            cooldown_seconds = max(30, int(raw_seconds))
        except (TypeError, ValueError):
            cooldown_seconds = 300
        return RecoveryPlan(
            status=status,
            strategy="batch_network_health_recovery",
            actions=(
                RecoveryAction(
                    "preserve_network_failure_evidence",
                    "Preserve the redacted browser/network failure code and batch health snapshot.",
                    automatic=True,
                ),
                RecoveryAction(
                    "wait_for_network_health",
                    "Keep the global browser/network circuit open during a bounded cooldown.",
                    automatic=True,
                    parameters={"cooldown_seconds": cooldown_seconds},
                ),
                RecoveryAction(
                    "recheck_network_health",
                    "Run a read-only health check before considering this application again.",
                    automatic=True,
                ),
            ),
            retry_allowed=True,
            retry_after_seconds=cooldown_seconds,
            retry_scope="single_application",
            retry_condition=(
                "The global network circuit is closed, the read-only health check passes, "
                "and no application was created by the failed attempt."
            ),
            evidence_required=("network_failure", "network_health_rechecked"),
            reason="A cross-company browser/network failure is an environment condition, not a coding repair candidate.",
        )

    def _application_form(
        self,
        status: str,
        _context: Mapping[str, Any],
    ) -> RecoveryPlan:
        return RecoveryPlan(
            status=status,
            strategy="application_form_reconciliation",
            actions=(
                RecoveryAction(
                    "preserve_application_form_navigation_evidence",
                    "Preserve the redacted redirect and form-entry evidence.",
                    automatic=True,
                ),
                RecoveryAction(
                    "recheck_application_form_entry",
                    "Recheck the application entry in read-only mode before any retry.",
                    automatic=True,
                ),
                RecoveryAction(
                    "request_application_form_review",
                    "Request review when the application form remains unavailable.",
                    automatic=False,
                    requires_user=True,
                ),
            ),
            retry_allowed=True,
            retry_scope="single_application",
            retry_condition=(
                "The application form is reachable in a read-only recheck and no application was created."
            ),
            evidence_required=(
                "application_form_navigation",
                "application_form_rechecked",
            ),
            reason=(
                "The form entry is unavailable or unsupported; preserve evidence and recheck the single job, "
                "without treating it as a coding repair candidate."
            ),
        )

    def _anti_spam(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan:
        raw_hours = (
            context.get("anti_spam_cooldown_hours")
            or 24
        )
        try:
            cooldown_seconds = max(1, int(raw_hours)) * 3600
        except (TypeError, ValueError):
            cooldown_seconds = 24 * 3600
        return RecoveryPlan(
            status=status,
            strategy="tenant_cooldown_then_scoped_resume",
            actions=(
                RecoveryAction(
                    "preserve_rejection_evidence",
                    "Keep the redacted rejection evidence and tenant identifier.",
                    automatic=True,
                ),
                RecoveryAction(
                    "apply_tenant_cooldown",
                    "Pause only the affected ATS tenant while other companies continue.",
                    automatic=True,
                    parameters={"cooldown_seconds": cooldown_seconds},
                ),
                RecoveryAction(
                    "recheck_application_eligibility",
                    "After cooldown, confirm the application is still open and "
                    "not already submitted.",
                    automatic=True,
                ),
            ),
            retry_allowed=True,
            retry_after_seconds=cooldown_seconds,
            retry_condition=(
                "Cooldown elapsed, no prior submission exists, and a fresh eligibility "
                "check no longer shows an anti-spam block."
            ),
            evidence_required=(
                "anti_spam_rejection",
                "cooldown_elapsed",
                "duplicate_check",
            ),
            reason="Anti-spam is an environment state, not a coding defect.",
        )

    def _processing_error(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan:
        kind = str(
            context.get("processing_error_kind") or "site_processing_error"
        )
        if kind == "rate_limited":
            return RecoveryPlan(
                status=status,
                strategy="rate_limit_cooldown",
                actions=(
                    RecoveryAction(
                        "preserve_processing_evidence",
                        "Keep the redacted HTTP or page processing evidence.",
                        automatic=True,
                    ),
                    RecoveryAction(
                        "cooldown_affected_tenant",
                        "Pause the affected tenant and continue unrelated companies.",
                        automatic=True,
                        parameters={"cooldown_seconds": 3600},
                    ),
                ),
                retry_allowed=True,
                retry_after_seconds=3600,
                retry_condition=(
                    "Cooldown elapsed and a read-only availability check no longer "
                    "reports rate limiting."
                ),
                evidence_required=("rate_limit_evidence", "availability_check"),
                reason="Rate limiting must be resolved by time or the site operator.",
            )
        if kind in {"captcha_failed", "captcha_unsupported"}:
            solver_configured = bool(
                context.get("captcha_solver_configured")
            )
            actions = [
                RecoveryAction(
                    "validate_captcha_challenge",
                    "Confirm the saved challenge type and whether token solving is supported.",
                    automatic=True,
                )
            ]
            if solver_configured and kind == "captcha_failed":
                actions.append(
                    RecoveryAction(
                        "solve_supported_captcha_once",
                        "Use the configured solver for one fresh supported challenge.",
                        automatic=True,
                        parameters={"max_attempts": 1},
                    )
                )
            else:
                actions.append(
                    RecoveryAction(
                        "complete_captcha_interactively",
                        "Open the saved application for candidate-assisted CAPTCHA completion.",
                        automatic=False,
                        requires_user=True,
                    )
                )
            actions.append(
                RecoveryAction(
                    "resume_after_challenge",
                    "Resume only this application after the challenge is proven complete.",
                    automatic=solver_configured and kind == "captcha_failed",
                    requires_user=not (
                        solver_configured and kind == "captcha_failed"
                    ),
                )
            )
            return RecoveryPlan(
                status=status,
                strategy="captcha_resolution",
                actions=tuple(actions),
                retry_allowed=True,
                retry_condition=(
                    "A fresh supported challenge is solved exactly once and the page "
                    "accepts the resulting token."
                ),
                evidence_required=("captcha_challenge", "captcha_resolution"),
                reason=(
                    "CAPTCHA resolution may use the configured provider or candidate "
                    "interaction, but it cannot be bypassed."
                ),
            )
        return RecoveryPlan(
            status=status,
            strategy="processing_evidence_reconciliation",
            actions=(
                RecoveryAction(
                    "inspect_processing_evidence",
                    "Classify the saved site error without submitting again.",
                    automatic=True,
                ),
                RecoveryAction(
                    "check_portal_and_email_status",
                    "Check the portal and confirmation inbox for an existing application.",
                    automatic=True,
                ),
                RecoveryAction(
                    "persist_confirmed_outcome",
                    "Persist an exact existing-submission confirmation without clicking Submit again.",
                    automatic=True,
                ),
            ),
            retry_allowed=True,
            retry_condition=(
                "Evidence proves no application was created and the site processing "
                "error is no longer present."
            ),
            evidence_required=("processing_error", "outcome_reconciled"),
            reason="A site processing error requires outcome reconciliation first.",
        )

    def _email_verification(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan:
        configured = bool(context.get("gmail_verification_configured"))
        authorization = RecoveryAction(
            (
                "poll_verification_message"
                if configured
                else "authorize_verification_inbox"
            ),
            (
                "Poll Gmail for a request-specific code or link newer than the request."
                if configured
                else "Authorize read-only Gmail access or provide the current verification step."
            ),
            automatic=configured,
            requires_user=not configured,
        )
        return RecoveryPlan(
            status=status,
            strategy="email_verification_resume",
            actions=(
                authorization,
                RecoveryAction(
                    "apply_verification",
                    "Enter the matched code or follow the matched verification link.",
                    automatic=configured,
                    requires_user=not configured,
                ),
                RecoveryAction(
                    "resume_same_application",
                    "Resume only the matching application after verification succeeds.",
                    automatic=configured,
                    requires_user=not configured,
                ),
            ),
            retry_allowed=True,
            retry_condition=(
                "A request-specific verification code or link is accepted for this "
                "application."
            ),
            evidence_required=("verification_request", "verification_accepted"),
            reason="Email verification is an account workflow, not a coding repair.",
        )

    def _candidate_account(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan:
        configured = bool(
            context.get("candidate_account_credentials_configured")
        )
        gmail_configured = bool(
            context.get("gmail_verification_configured")
        )
        return RecoveryPlan(
            status=status,
            strategy="candidate_account_resume",
            actions=(
                RecoveryAction(
                    (
                        "sign_in_or_create_candidate_account"
                        if configured
                        else "request_candidate_account_authorization"
                    ),
                    (
                        "Use the configured credential store to sign in, create, "
                        "or verify the account."
                        if configured
                        else "Request candidate approval and store credentials "
                        "outside the audit log."
                    ),
                    automatic=configured,
                    requires_user=not configured,
                ),
                RecoveryAction(
                    "verify_candidate_account",
                    "Complete request-specific email verification when the ATS requires it.",
                    automatic=configured and gmail_configured,
                    requires_user=not (configured and gmail_configured),
                ),
                RecoveryAction(
                    "resume_same_application",
                    "Return to the matching application after account access is confirmed.",
                    automatic=configured,
                    requires_user=not configured,
                ),
            ),
            retry_allowed=True,
            retry_condition=(
                "The candidate account is accessible and any required account "
                "verification is complete."
            ),
            evidence_required=("account_access_confirmed",),
            reason="Account access needs credentials or candidate authorization.",
        )

    def _clicked_unconfirmed(self, status: str) -> RecoveryPlan:
        return RecoveryPlan(
            status=status,
            strategy="confirmation_reconciliation",
            actions=(
                RecoveryAction(
                    "inspect_saved_confirmation_evidence",
                    "Inspect the saved page evidence without clicking Submit again.",
                    automatic=True,
                ),
                RecoveryAction(
                    "check_portal_email_and_tracking",
                    "Check portal status, confirmation email, and the tracked application ID.",
                    automatic=True,
                ),
                RecoveryAction(
                    "persist_confirmed_outcome",
                    "If confirmation exists, mark the existing application "
                    "submitted without a new click.",
                    automatic=True,
                ),
            ),
            retry_allowed=False,
            retry_condition=(
                "A new retry plan may be created only after evidence proves the first "
                "click did not create an application."
            ),
            evidence_required=(
                "submission_click_evidence",
                "portal_or_email_reconciliation",
            ),
            reason="An unknown click outcome carries the highest duplicate risk.",
        )

    def _blocked_fields(
        self,
        status: str,
        context: Mapping[str, Any],
    ) -> RecoveryPlan:
        review_items = context.get("review_items")
        items = (
            [item for item in review_items if isinstance(item, Mapping)]
            if isinstance(review_items, list)
            else []
        )
        missing_facts = [
            str(item.get("label") or "unlabeled field")
            for item in items
            if requires_approved_candidate_fact(item)
        ]
        if missing_facts:
            return RecoveryPlan(
                status=status,
                strategy="candidate_fact_resolution",
                actions=(
                    RecoveryAction(
                        "request_candidate_facts",
                        "Request approved answers for the unresolved candidate fields.",
                        automatic=False,
                        requires_user=True,
                        parameters={"field_labels": missing_facts},
                    ),
                    RecoveryAction(
                        "update_approved_fact_source",
                        "Store approved answers in the profile or sensitive answer source.",
                        automatic=False,
                        requires_user=True,
                    ),
                    RecoveryAction(
                        "rebuild_scoped_application",
                        "Rebuild and revalidate only this application package.",
                        automatic=True,
                    ),
                ),
                retry_allowed=True,
                retry_condition=(
                    "Every required field resolves from an approved candidate fact "
                    "and the rebuilt package passes the submission gate."
                ),
                evidence_required=("approved_candidate_facts", "field_gate_passed"),
                reason="Candidate facts must come from the candidate, not code or an LLM.",
            )
        return RecoveryPlan(
            status=status,
            strategy="bounded_field_recovery",
            actions=(
                RecoveryAction(
                    "retry_recoverable_fields",
                    "Apply bounded field self-healing to the recorded blocking fields.",
                    automatic=True,
                    parameters={"max_attempts_per_field": 1},
                ),
                RecoveryAction(
                    "classify_remaining_blockers",
                    "Route only reproducible field defects to isolated coding repair.",
                    automatic=True,
                ),
            ),
            retry_allowed=True,
            retry_condition=(
                "All blocking fields are resolved and the package passes the field "
                "and submission gates."
            ),
            evidence_required=("field_gate_passed",),
            reason="Recoverable field failures can be retried once before repair.",
        )
