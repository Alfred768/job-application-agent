"""Career-specific policy and safety gate."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Callable
from urllib.parse import urlparse

from hello_agents.core.contracts import PolicyDecision, ToolCall, ToolEffect
from hello_agents.core.memory import LongTermMemory, ShortTermMemory


_PROTECTED_TERMINAL_MARKERS = (
    "anti_spam",
    "captcha",
    "candidate_account",
    "email_verification",
    "submit_clicked",
    "submission_processing",
)
_REPAIRABLE_STATUSES = {
    "autofill_completed_blocked",
    "autofill_failed",
    "autofill_timed_out",
}
_REMOTE_SOURCE_TOOLS = {
    "ashby_job_source",
    "browser_execute",
    "greenhouse_job_source",
    "lever_job_source",
    "remotive_job_source",
    "rss_job_source",
}
_RECOVERY_STATUSES = {
    "autofill_completed_blocked",
    "candidate_account_required",
    "email_verification_required",
    "submission_blocked_by_anti_spam",
    "submission_processing_error",
    "submit_clicked_unconfirmed",
}


class JobApplicationPolicyGate:
    """Central policy for career reads, writes, submissions, and repairs."""

    def __init__(
        self,
        *,
        semantic_answer_validator: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.semantic_answer_validator = semantic_answer_validator

    def evaluate(
        self,
        call: ToolCall,
        *,
        short_term_memory: ShortTermMemory,
        long_term_memory: LongTermMemory,
    ) -> PolicyDecision:
        linkedin_url = self._linkedin_url(call)
        if linkedin_url and (
            call.effect is ToolEffect.SUBMIT
            or call.tool_name in _REMOTE_SOURCE_TOOLS
        ):
            return self._deny(
                "linkedin_automation_forbidden",
                f"LinkedIn automation is not allowed: {linkedin_url}",
            )

        context = dict(call.context)
        if bool(context.get("duplicate")) and call.effect in {
            ToolEffect.WRITE,
            ToolEffect.SUBMIT,
        }:
            return self._deny(
                "duplicate_application",
                "The application already has a tracked matching record.",
            )

        if bool(context.get("anti_spam_cooldown_active")) and call.effect in {
            ToolEffect.WRITE,
            ToolEffect.SUBMIT,
        }:
            return self._deny(
                "anti_spam_cooldown_active",
                "The affected company or ATS tenant is in an active anti-spam cooldown.",
            )

        if bool(context.get("failure_circuit_breaker_active")) and call.effect in {
            ToolEffect.WRITE,
            ToolEffect.SUBMIT,
        }:
            return self._deny(
                "failure_circuit_breaker_active",
                "The affected company or ATS adapter has an active ordinary-failure circuit breaker.",
            )

        answer_decision = self._evaluate_candidate_answer_validations(context)
        if answer_decision is not None:
            return answer_decision

        terminal_status = str(context.get("terminal_status") or "").lower()
        if bool(context.get("retry")) and terminal_status:
            if any(marker in terminal_status for marker in _PROTECTED_TERMINAL_MARKERS):
                if not bool(context.get("recovery_verified")):
                    return self._deny(
                        "protected_terminal_retry",
                        f"Terminal status '{terminal_status}' is not an immediate retry signal.",
                    )
                if context.get("retry_scope") != "single_application":
                    return self._deny(
                        "unscoped_recovery_retry",
                        "Verified recovery may retry only the affected application.",
                    )

        if call.effect is ToolEffect.SUBMIT:
            return self._evaluate_submission(context)
        if call.effect is ToolEffect.REPAIR:
            return self._evaluate_repair(context)
        if call.tool_name == "job_application_recovery":
            return self._evaluate_recovery(context)
        return self._allow()

    def _evaluate_candidate_answer_validations(
        self,
        context: Mapping[str, Any],
    ) -> PolicyDecision | None:
        for item in self._validation_items(
            context.get("candidate_answer_validations")
        ):
            kind = str(item.get("kind") or "").strip().lower()
            label = str(item.get("label") or item.get("field") or "field")
            value = str(item.get("value") or "")
            expected = item.get("approved_value")
            if kind in {"exact", "boolean", "category", "profile_fact"}:
                if expected is None or str(value) != str(expected):
                    return self._deny(
                        "candidate_fact_mismatch",
                        f"{label} must exactly match the approved candidate fact.",
                    )
            if kind == "email" and not self._valid_email(value):
                return self._deny(
                    "candidate_email_invalid",
                    f"{label} must be a valid approved email address.",
                )
            if kind == "phone" and not self._valid_phone(value):
                return self._deny(
                    "candidate_phone_invalid",
                    f"{label} must be a valid approved phone number.",
                )
            if kind == "url" and not self._valid_url(value):
                return self._deny(
                    "candidate_url_invalid",
                    f"{label} must be a valid approved URL.",
                )

        for item in self._validation_items(
            context.get("semantic_answer_validations")
        ):
            generator_id = str(item.get("generator_id") or "").strip()
            validator_id = str(item.get("validator_id") or "").strip()
            if generator_id and validator_id and generator_id == validator_id:
                return self._deny(
                    "self_validation_forbidden",
                    "Generated open-text answers must be checked by an independent validator.",
                )
            evidence = item.get("evidence")
            if not evidence:
                return self._deny(
                    "semantic_validation_evidence_missing",
                    "Open-text answers require candidate fact evidence.",
                )
            if self.semantic_answer_validator is None:
                if bool(item.get("validator_required", False)):
                    return self._deny(
                        "semantic_validator_unavailable",
                        "An open-text answer requires independent semantic validation.",
                    )
                continue
            verdict = self.semantic_answer_validator(item)
            if self._validator_denied(verdict):
                reason = (
                    verdict.get("reason")
                    if isinstance(verdict, Mapping)
                    else "The generated answer is not entailed by candidate facts."
                )
                return self._deny(
                    "semantic_validation_failed",
                    str(reason),
                )
        return None

    def _evaluate_submission(self, context: Mapping[str, Any]) -> PolicyDecision:
        if not bool(context.get("submit_complete")):
            return self._deny(
                "submission_disabled",
                "Final submission is disabled by explicit configuration.",
            )
        if not bool(context.get("facts_verified")):
            return self._deny(
                "unverified_candidate_facts",
                "Candidate facts must be verified before final submission.",
            )
        blocking = self._items(context.get("blocking_review_items"))
        if blocking:
            return self._deny(
                "blocking_review_items",
                "Required or low-confidence fields remain unresolved.",
            )
        sensitive = self._items(context.get("unapproved_sensitive_fields"))
        if sensitive:
            return self._deny(
                "unapproved_sensitive_fields",
                "Sensitive fields must come from the approved sensitive KB.",
            )
        if not bool(context.get("resume_verified")):
            return self._deny(
                "resume_provenance_unverified",
                "The selected resume must be an unchanged approved PDF.",
            )
        if not bool(context.get("confirmation_required", True)):
            return self._deny(
                "submission_confirmation_not_required",
                "A submitted result must require page confirmation evidence.",
            )
        return self._allow("Submission prerequisites are satisfied.")

    def _evaluate_repair(self, context: Mapping[str, Any]) -> PolicyDecision:
        status = str(context.get("failure_status") or "").strip().lower()
        if status not in _REPAIRABLE_STATUSES:
            return self._deny(
                "non_repairable_status",
                f"Status '{status or 'unknown'}' is not a coding-repair candidate.",
            )
        if any(marker in status for marker in _PROTECTED_TERMINAL_MARKERS):
            return self._deny(
                "protected_terminal_repair",
                f"Status '{status}' must not enter coding repair.",
            )
        if not bool(context.get("isolated_workspace")):
            return self._deny(
                "repair_not_isolated",
                "Coding repair must run in an isolated workspace.",
            )
        if not bool(context.get("offline_verification")):
            return self._deny(
                "repair_verification_missing",
                "Repair promotion requires tests and offline verification.",
            )
        if bool(context.get("real_browser_verification")) or bool(
            context.get("real_submission")
        ):
            return self._deny(
                "real_environment_repair_verification",
                "Coding repair must be verified offline without a real submission.",
            )
        return self._allow("Repair isolation and verification are configured.")

    def _evaluate_recovery(self, context: Mapping[str, Any]) -> PolicyDecision:
        status = str(context.get("terminal_status") or "").strip().lower()
        if status not in _RECOVERY_STATUSES:
            return self._deny(
                "unsupported_recovery_status",
                f"Status '{status or 'unknown'}' has no controlled recovery lane.",
            )
        if context.get("retry_scope") != "single_application":
            return self._deny(
                "unscoped_recovery_action",
                "Recovery actions must target one tracked application.",
            )
        if bool(context.get("real_submission")):
            return self._deny(
                "recovery_cannot_submit",
                "A recovery action cannot claim or perform a final submission.",
            )
        action = str(context.get("recovery_action") or "")
        if (
            status == "submit_clicked_unconfirmed"
            and action == "persist_confirmed_outcome"
            and not bool(context.get("confirmation_evidence_verified"))
        ):
            return self._deny(
                "unconfirmed_outcome_not_reconciled",
                "A clicked-but-unconfirmed application needs verified evidence "
                "before its tracked outcome can change.",
            )
        return self._allow("Scoped recovery action is authorized.")

    def _linkedin_url(self, call: ToolCall) -> str | None:
        for value in self._walk_values((call.parameters, call.context)):
            if not isinstance(value, str):
                continue
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            if host == "linkedin.com" or host.endswith(".linkedin.com"):
                return value
        return None

    def _walk_values(self, value: Any):
        if isinstance(value, Mapping):
            for nested in value.values():
                yield from self._walk_values(nested)
        elif isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            for nested in value:
                yield from self._walk_values(nested)
        else:
            yield value

    @staticmethod
    def _items(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return list(value)
        return [value] if value else []

    @classmethod
    def _validation_items(cls, value: Any) -> list[Mapping[str, Any]]:
        return [
            item
            for item in cls._items(value)
            if isinstance(item, Mapping)
        ]

    @staticmethod
    def _valid_email(value: str) -> bool:
        return bool(
            re.fullmatch(
                r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                value.strip(),
                flags=re.I,
            )
        )

    @staticmethod
    def _valid_phone(value: str) -> bool:
        digits = re.sub(r"\D", "", value or "")
        return 8 <= len(digits) <= 15

    @staticmethod
    def _valid_url(value: str) -> bool:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _validator_denied(verdict: Any) -> bool:
        if isinstance(verdict, bool):
            return not verdict
        if isinstance(verdict, Mapping):
            raw = str(
                verdict.get("verdict")
                or verdict.get("status")
                or verdict.get("decision")
                or ""
            ).strip().lower()
            if raw in {"pass", "passed", "allow", "allowed", "yes", "true"}:
                return False
            if raw in {"deny", "denied", "fail", "failed", "no", "false"}:
                return True
        return True

    def _allow(self, reason: str = "Career policy allowed the tool call.") -> PolicyDecision:
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason=reason,
            policy=type(self).__name__,
        )

    def _deny(self, code: str, reason: str) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            code=code,
            reason=reason,
            policy=type(self).__name__,
        )
