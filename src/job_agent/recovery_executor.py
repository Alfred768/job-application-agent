from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.career.recovery import (
    JobApplicationRecoveryPlanner,
    recovery_execution_result_to_dict,
    recovery_plan_to_dict,
)
from hello_agents.core.contracts import (
    AgentLoopResult,
    Observation,
    RecoveryAction,
    RecoveryActionResult,
    RecoveryExecutionResult,
    RecoveryPlan,
    ToolCall,
    ToolEffect,
)
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.memory import NullLongTermMemory
from hello_agents.core.runtime import AgentCore
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.registry import ToolRegistry
from job_agent.db import connect, init_db, update_application_execution_status
from job_agent.gmail_verification import (
    GmailVerificationError,
    find_application_confirmation,
    find_verification_code,
    find_verification_link,
)
from job_agent.memory import SQLiteApplicationMemory
from job_agent.agent_session import (
    DeterministicSessionLLM,
    latest_trajectory_observation,
)


RecoveryHandler = Callable[
    [RecoveryAction, Mapping[str, Any], dict[str, Any]],
    Mapping[str, Any],
]


@dataclass(frozen=True)
class RecoveryBatchResult:
    applications: tuple[dict[str, Any], ...]
    verified_targets: tuple[dict[str, Any], ...]
    status_counts: Mapping[str, int]


class _RecoveryActionTool(Tool):
    def __init__(self, executor: "JobApplicationRecoveryExecutor") -> None:
        super().__init__(
            "job_application_recovery",
            "Execute one scoped, auditable application recovery action.",
            effect=ToolEffect.WRITE,
        )
        self._executor = executor

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        action_payload = parameters["action"]
        context = parameters["context"]
        if not isinstance(action_payload, Mapping) or not isinstance(context, Mapping):
            raise ValueError("Recovery action and context must be objects.")
        action = RecoveryAction(
            action=str(action_payload.get("action") or ""),
            description=str(action_payload.get("description") or ""),
            automatic=bool(action_payload.get("automatic")),
            requires_user=bool(action_payload.get("requires_user")),
            parameters=(
                dict(action_payload.get("parameters") or {})
                if isinstance(action_payload.get("parameters"), Mapping)
                else {}
            ),
        )
        return dict(self._executor.run_action(action, context))

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="object",
                description="Structured recovery action.",
            ),
            ToolParameter(
                name="context",
                type="object",
                description="Sanitized single-application recovery context.",
            ),
        ]


class JobApplicationRecoveryExecutor:
    """Execute bounded recovery actions without claiming unproven success."""

    name = "job_application"

    def __init__(
        self,
        *,
        run_dir: Path,
        database: Path | None = None,
        environ: Mapping[str, str] | None = None,
        handlers: Mapping[str, RecoveryHandler] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.database = database.resolve() if database is not None else None
        self.environ = dict(environ or {})
        self.handlers = dict(handlers or {})
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._private_state: dict[str, Any] = {}
        self._agent_core: AgentCore | None = None
        self._initial_observation: Observation | None = None

    def bind_agent_runtime(
        self,
        core: AgentCore,
        initial_observation: Observation,
    ) -> None:
        """Continue recovery in the owning application's Agent Core."""
        self._agent_core = core
        self._initial_observation = initial_observation

    def __call__(
        self,
        plan: RecoveryPlan,
        context: Mapping[str, Any],
        execution: ControlledExecution,
    ) -> RecoveryExecutionResult:
        execution.registry.register_tool(_RecoveryActionTool(self))
        core = self._agent_core or AgentCore(execution)
        action_results: list[RecoveryActionResult] = []
        agent_loops: list[AgentLoopResult] = []
        evidence: list[str] = []
        state: dict[str, Any] = {
            "evidence": evidence,
            "confirmation_evidence_verified": False,
        }
        current_observation = self._initial_observation or Observation(
            kind="recovery_plan",
            source="job_application_recovery",
            payload={
                "phase": "recovery",
                "status": plan.status,
                "strategy": plan.strategy,
            },
        )
        for action in plan.actions:
            handler_available = action.action in self.handlers
            if (action.requires_user or not action.automatic) and not handler_available:
                action_results.append(
                    RecoveryActionResult(
                        action=action.action,
                        status="waiting_for_user",
                        automatic=action.automatic,
                        message=action.description,
                    )
                )
                continue
            scoped_context = {
                **dict(context),
                "recovery_strategy": plan.strategy,
                "recovery_action": action.action,
                "retry_scope": plan.retry_scope,
                "real_submission": False,
                "confirmation_evidence_verified": bool(
                    state["confirmation_evidence_verified"]
                ),
                "accumulated_evidence": list(evidence),
            }
            call = ToolCall(
                    tool_name="job_application_recovery",
                    parameters={
                        "action": {
                            "action": action.action,
                            "description": action.description,
                            "automatic": action.automatic,
                            "requires_user": action.requires_user,
                            "parameters": dict(action.parameters),
                        },
                        "context": scoped_context,
                    },
                    effect=ToolEffect.WRITE,
                    purpose=action.description,
                    context={
                        "terminal_status": plan.status,
                        "recovery_strategy": plan.strategy,
                        "recovery_action": action.action,
                        "retry_scope": plan.retry_scope,
                        "real_submission": False,
                        "confirmation_evidence_verified": bool(
                            state["confirmation_evidence_verified"]
                        ),
                    },
            )
            loop_result = core.run_loop(
                core.create_plan(
                    f"Execute bounded recovery action: {action.action}",
                    [call],
                ),
                initial_observation=current_observation,
                memory_query=(
                    f"{context.get('company') or ''} "
                    f"{context.get('title') or ''} {plan.strategy}"
                ).strip(),
                remember_rounds=self.database is not None,
                memory_namespace="agent_run",
            )
            agent_loops.append(loop_result)
            current_observation = loop_result.observations[-1]
            tool_result = (
                loop_result.results[0]
                if loop_result.results
                else None
            )
            if tool_result is None:
                action_results.append(
                    RecoveryActionResult(
                        action=action.action,
                        status="failed",
                        automatic=True,
                        message="Recovery Agent Core returned no ToolResult.",
                    )
                )
                continue
            if not tool_result.ok:
                decision_code = (
                    tool_result.policy_decision.code
                    if tool_result.policy_decision is not None
                    else ""
                )
                dependency_pending = (
                    action.action == "persist_confirmed_outcome"
                    and decision_code
                    == "unconfirmed_outcome_not_reconciled"
                )
                action_results.append(
                    RecoveryActionResult(
                        action=action.action,
                        status=(
                            "pending"
                            if dependency_pending
                            else "policy_denied"
                            if tool_result.policy_decision is not None
                            and not tool_result.policy_decision.allowed
                            else "failed"
                        ),
                        automatic=True,
                        message=(
                            "Confirmation reconciliation must succeed before "
                            "the tracked outcome can change."
                            if dependency_pending
                            else str(
                                tool_result.error
                                or "Recovery action failed."
                            )
                        ),
                    )
                )
                continue
            output = (
                dict(tool_result.output)
                if isinstance(tool_result.output, Mapping)
                else {}
            )
            action_evidence = tuple(
                dict.fromkeys(
                    str(item)
                    for item in output.get("evidence", [])
                    if str(item).strip()
                )
            )
            evidence.extend(
                item for item in action_evidence if item not in evidence
            )
            if bool(output.get("confirmation_evidence_verified")):
                state["confirmation_evidence_verified"] = True
            action_results.append(
                RecoveryActionResult(
                    action=action.action,
                    status=str(output.get("status") or "pending"),
                    automatic=action.automatic,
                    evidence=action_evidence,
                    message=str(output.get("message") or ""),
                    details=(
                        dict(output.get("details") or {})
                        if isinstance(output.get("details"), Mapping)
                        else {}
                    ),
                )
            )

        required = set(plan.evidence_required)
        verified = required.issubset(evidence) and not any(
            result.status
            in {"failed", "policy_denied", "pending", "waiting_for_user"}
            for result in action_results
        )
        retry_ready = verified and plan.retry_allowed
        if plan.strategy == "processing_evidence_reconciliation":
            retry_ready = retry_ready and "no_existing_submission" in evidence
        if any(
            result.status in {"failed", "policy_denied"}
            for result in action_results
        ):
            status = "failed"
        elif any(
            result.status == "waiting_for_user"
            for result in action_results
        ):
            status = "waiting_for_user"
        elif verified:
            status = "verified"
        else:
            status = "pending"
        return RecoveryExecutionResult(
            strategy=plan.strategy,
            status=status,
            actions=tuple(action_results),
            evidence=tuple(evidence),
            retry_ready=retry_ready,
            retry_scope=plan.retry_scope,
            reason=(
                "All required recovery evidence is verified."
                if verified
                else "Recovery is waiting for required actions or evidence."
            ),
            agent_loops=tuple(agent_loops),
        )

    def run_action(
        self,
        action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        handler = self.handlers.get(action.action)
        if handler is not None:
            return handler(action, context, self._private_state)
        method = getattr(self, f"_action_{action.action}", None)
        if callable(method):
            return method(action, context)
        return self._pending(
            "No automatic adapter is available for this scoped action."
        )

    def _action_preserve_rejection_evidence(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._inspect_evidence(
            context,
            evidence_name="anti_spam_rejection",
        )

    def _action_preserve_processing_evidence(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        name = (
            "rate_limit_evidence"
            if context.get("recovery_strategy") == "rate_limit_cooldown"
            else "processing_error"
        )
        return self._inspect_evidence(context, evidence_name=name)

    def _action_preserve_network_failure_evidence(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        structured_network_failure = str(context.get("error") or "") in {
            "browser_navigation_network_error",
            "browser_startup_network_error",
            "page_navigation_network_error",
        }
        if not context.get("network_failure") and not structured_network_failure:
            return self._pending("Network failure evidence is unavailable.")
        return {
            "status": "completed",
            "evidence": ["network_failure"],
            "message": (
                "The structured terminal network failure code was preserved."
                if structured_network_failure
                else "The redacted network failure code and batch health snapshot were preserved."
            ),
        }

    def _action_wait_for_network_health(
        self,
        action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._cooldown(action, context)

    def _action_recheck_network_health(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if bool(context.get("network_health_rechecked")):
            return {
                "status": "completed",
                "evidence": ["network_health_rechecked"],
                "message": "The batch network health check passed.",
            }
        apply_url = str(context.get("apply_url") or "").strip()
        parsed = urlsplit(apply_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return self._pending(
                "A valid application URL is required for the read-only health check."
            )
        request = Request(
            apply_url,
            method="HEAD",
            headers={"User-Agent": "job-agent-network-health/1.0"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                status = int(response.getcode() or 0)
        except HTTPError as exc:
            status = int(exc.code or 0)
        except (URLError, TimeoutError, OSError) as exc:
            return {
                "status": "pending",
                "evidence": [],
                "message": "The read-only network health check did not complete.",
                "details": {
                    "host": parsed.netloc,
                    "failure_code": type(exc).__name__,
                },
            }
        if status >= 500 or status <= 0:
            return {
                "status": "pending",
                "evidence": [],
                "message": "The application host is reachable but remains unhealthy.",
                "details": {"host": parsed.netloc, "http_status": status},
            }
        return {
            "status": "completed",
            "evidence": ["network_health_rechecked"],
            "message": "The application host passed a read-only network health check.",
            "details": {"host": parsed.netloc, "http_status": status},
        }

    def _action_preserve_application_form_navigation_evidence(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._inspect_evidence(
            context,
            evidence_name="application_form_navigation",
            required_markers=("application", "form"),
        )

    def _action_recheck_application_form_entry(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if bool(context.get("application_form_rechecked")):
            return {
                "status": "completed",
                "evidence": ["application_form_rechecked"],
                "message": "The application form entry was rechecked in read-only mode.",
            }
        return self._pending(
            "A read-only application-form adapter must recheck the entry before retry."
        )

    def _action_request_application_form_review(
        self,
        _action: RecoveryAction,
        _context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "status": "waiting_for_user",
            "message": "Application form review is required before a scoped retry.",
        }

    def _action_inspect_processing_evidence(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        inspected = self._inspect_evidence(
            context,
            evidence_name="processing_error",
        )
        if inspected.get("status") == "completed":
            return inspected
        if (
            str(context.get("terminal_status") or context.get("status") or "")
            == "submission_processing_error"
            and str(context.get("error") or "") == "submission_processing_error"
        ):
            return {
                "status": "completed",
                "evidence": ["processing_error"],
                "message": "The structured terminal processing-error audit was preserved.",
            }
        return inspected

    def _action_validate_captcha_challenge(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        inspected = self._inspect_evidence(
            context,
            evidence_name="captcha_challenge",
            required_markers=("captcha", "recaptcha", "hcaptcha", "funcaptcha"),
        )
        return inspected

    def _action_inspect_saved_confirmation_evidence(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        path = self._evidence_path(context.get("evidence"))
        if path is None:
            if (
                str(context.get("terminal_status") or context.get("status") or "")
                == "submit_clicked_unconfirmed"
                and str(context.get("error") or "")
                == "submission_confirmation_not_detected"
            ):
                return {
                    "status": "completed",
                    "evidence": ["submission_click_evidence"],
                    "message": "The structured click-without-confirmation audit was preserved.",
                }
            return self._pending("Saved click evidence is unavailable.")
        text = self._read_text(path)
        confirmed = "submission confirmed:" in text.casefold()
        evidence = ["submission_click_evidence"]
        if confirmed:
            evidence.append("portal_or_email_reconciliation")
        return {
            "status": "completed",
            "evidence": evidence,
            "confirmation_evidence_verified": confirmed,
            "message": "Saved submission evidence was inspected.",
        }

    def _action_check_portal_email_and_tracking(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        tracked = self._tracked_submission(context.get("application_id"))
        if tracked:
            return {
                "status": "completed",
                "evidence": ["portal_or_email_reconciliation"],
                "confirmation_evidence_verified": True,
                "message": "The tracked application already has confirmed submission.",
            }
        confirmation = self._gmail_application_confirmation(context)
        if confirmation is not None:
            return confirmation
        return self._pending(
            "No confirmed portal, email, or tracking evidence was found."
        )

    def _action_check_portal_and_email_status(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._tracked_submission(context.get("application_id")):
            return {
                "status": "completed",
                "evidence": ["outcome_reconciled", "portal_or_email_reconciliation"],
                "confirmation_evidence_verified": True,
                "message": "The tracked application already has confirmed submission.",
            }
        confirmation = self._gmail_application_confirmation(context)
        if confirmation is not None:
            return confirmation
        return self._pending(
            "No exact confirmation was found; absence of email is not proof that no application exists."
        )

    def _action_persist_confirmed_outcome(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if (
            "no_existing_submission"
            in set(context.get("accumulated_evidence") or [])
        ):
            return {
                "status": "completed",
                "evidence": [],
                "message": "Verified absence was preserved; no tracked outcome was changed.",
            }
        if not bool(context.get("confirmation_evidence_verified")):
            return self._pending(
                "Confirmed outcome evidence is required before tracking changes."
            )
        application_id = self._application_id(context.get("application_id"))
        if self.database is None or application_id is None:
            return self._pending("Tracked application identity is unavailable.")
        connection = connect(self.database)
        try:
            init_db(connection)
            updated = update_application_execution_status(
                connection,
                application_id,
                "submitted",
            )
        finally:
            connection.close()
        confirmed = bool(updated or self._tracked_submission(application_id))
        return {
            "status": "completed" if confirmed else "failed",
            "evidence": (
                ["portal_or_email_reconciliation", "confirmed_outcome_persisted"]
                if confirmed
                else []
            ),
            "message": (
                "Confirmed outcome was persisted without another submit click."
                if confirmed
                else "Confirmed outcome could not be persisted."
            ),
            "details": {"outcome": "submitted"} if confirmed else {},
        }

    def _gmail_application_confirmation(
        self,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        token = self._gmail_token()
        company = str(context.get("company") or "").strip()
        title = str(context.get("title") or "").strip()
        if token is None or not company or not title:
            return None
        safe_title = title.replace('"', " ").strip()
        query = str(
            context.get("gmail_application_confirmation_query")
            or f'in:anywhere newer_than:365d "{safe_title}"'
        )
        try:
            match = find_application_confirmation(
                str(token),
                query=query,
                company=company,
                title=title,
            )
        except GmailVerificationError:
            return None
        if not match:
            return None
        raw_id = str(match.get("message_id") or "")
        received_at_ms = int(match.get("received_at_ms") or 0)
        received_at = (
            datetime.fromtimestamp(received_at_ms / 1000, tz=timezone.utc).isoformat()
            if received_at_ms > 0
            else None
        )
        return {
            "status": "completed",
            "evidence": ["outcome_reconciled", "portal_or_email_reconciliation"],
            "confirmation_evidence_verified": True,
            "message": "An exact company-and-title application confirmation email was verified.",
            "details": {
                "source": "gmail_readonly",
                "received_at": received_at,
                "message_fingerprint": (
                    hashlib.sha256(raw_id.encode()).hexdigest()[:16]
                    if raw_id
                    else None
                ),
            },
        }

    def _action_poll_verification_message(
        self,
        _action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        token = self._gmail_token()
        requested_after_ms = self._requested_after_ms(context)
        if token is None or requested_after_ms is None:
            return self._pending(
                "Gmail token or request timestamp is unavailable."
            )
        query = str(
            context.get("gmail_query")
            or 'newer_than:2d (verification OR confirmation OR "security code")'
        )
        try:
            code = find_verification_code(
                str(token),
                requested_after_ms=requested_after_ms,
                query=query,
            )
            link = None if code else find_verification_link(
                str(token),
                requested_after_ms=requested_after_ms,
                query=query,
                url_pattern=str(
                    context.get("verification_url_pattern")
                    or r"greenhouse|workday|ashby|lever"
                ),
            )
        except GmailVerificationError as exc:
            return {
                "status": "failed",
                "evidence": ["verification_request"],
                "message": type(exc).__name__,
            }
        if not code and not link:
            return {
                "status": "pending",
                "evidence": ["verification_request"],
                "message": "No request-specific verification message was found.",
            }
        self._private_state["verification_value"] = code or link
        self._private_state["verification_kind"] = "code" if code else "link"
        return {
            "status": "completed",
            "evidence": ["verification_request"],
            "message": "A request-specific verification message was matched.",
            "details": {"match_type": self._private_state["verification_kind"]},
        }

    def _action_apply_tenant_cooldown(
        self,
        action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._cooldown(action, context)

    def _action_cooldown_affected_tenant(
        self,
        action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._cooldown(action, context)

    def _cooldown(
        self,
        action: RecoveryAction,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        now = self._now()
        prior = self._prior_action_details(context, action.action)
        ready_at = self._parse_datetime(prior.get("ready_at"))
        if ready_at is None:
            seconds = max(
                1,
                int(action.parameters.get("cooldown_seconds", 3600) or 3600),
            )
            ready_at = now + timedelta(seconds=seconds)
        if now >= ready_at:
            return {
                "status": "completed",
                "evidence": ["cooldown_elapsed"],
                "message": "The scoped cooldown has elapsed.",
                "details": {"ready_at": ready_at.isoformat()},
            }
        return {
            "status": "pending",
            "evidence": [],
            "message": "The affected tenant remains in scoped cooldown.",
            "details": {"ready_at": ready_at.isoformat()},
        }

    def _inspect_evidence(
        self,
        context: Mapping[str, Any],
        *,
        evidence_name: str,
        required_markers: tuple[str, ...] = (),
    ) -> Mapping[str, Any]:
        path = self._evidence_path(context.get("evidence"))
        if path is None:
            return self._pending("Saved recovery evidence is unavailable.")
        if required_markers:
            text = self._read_text(path).casefold()
            if not any(marker in text for marker in required_markers):
                return self._pending(
                    "Saved evidence does not identify a supported challenge."
                )
        return {
            "status": "completed",
            "evidence": [evidence_name],
            "message": "Saved recovery evidence was validated.",
        }

    def _evidence_path(self, raw: Any) -> Path | None:
        if not raw:
            return None
        path = Path(str(raw)).expanduser()
        if not path.is_absolute():
            path = self.run_dir / path
        try:
            resolved = path.resolve()
            resolved.relative_to(self.run_dir)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(errors="replace")[:131072]
        except OSError:
            return ""

    def _gmail_token(self) -> Path | None:
        raw = str(self.environ.get("JOB_AGENT_GMAIL_TOKEN_FILE") or "").strip()
        path = (
            Path(raw).expanduser()
            if raw
            else (
                self.database.parent
                if self.database is not None
                else self.run_dir
            )
            / ".job-agent-secrets"
            / "gmail-token.json"
        )
        return path if path.is_file() else None

    def _requested_after_ms(
        self,
        context: Mapping[str, Any],
    ) -> int | None:
        raw = context.get("verification_requested_after_ms")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        evidence = self._evidence_path(context.get("evidence"))
        return int(evidence.stat().st_mtime * 1000) if evidence is not None else None

    def _tracked_submission(self, raw_application_id: Any) -> bool:
        application_id = self._application_id(raw_application_id)
        if self.database is None or application_id is None:
            return False
        connection = connect(self.database)
        try:
            init_db(connection)
            row = connection.execute(
                "select submitted_at, status from applications where id = ?",
                (application_id,),
            ).fetchone()
        finally:
            connection.close()
        return bool(
            row is not None
            and (row["submitted_at"] is not None or row["status"] == "submitted")
        )

    @staticmethod
    def _application_id(raw: Any) -> int | None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_datetime(raw: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _prior_action_details(
        context: Mapping[str, Any],
        action_name: str,
    ) -> dict[str, Any]:
        prior = context.get("recovery_execution")
        if not isinstance(prior, Mapping):
            return {}
        actions = prior.get("actions")
        if not isinstance(actions, list):
            return {}
        for item in actions:
            if (
                isinstance(item, Mapping)
                and item.get("action") == action_name
                and isinstance(item.get("details"), Mapping)
            ):
                return dict(item["details"])
        return {}

    @staticmethod
    def _pending(message: str) -> Mapping[str, Any]:
        return {"status": "pending", "evidence": [], "message": message}


def execute_audit_recovery(
    audit: dict[str, Any],
    *,
    run_dir: Path,
    database: Path | None,
    environ: Mapping[str, str] | None = None,
    handlers: Mapping[str, RecoveryHandler] | None = None,
    now: Callable[[], datetime] | None = None,
) -> RecoveryBatchResult:
    applications = audit.get("applications")
    records = (
        [item for item in applications if isinstance(item, dict)]
        if isinstance(applications, list)
        else []
    )
    verified_targets: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for record in records:
        registry = ToolRegistry()
        long_term_memory = (
            SQLiteApplicationMemory(database)
            if database is not None
            else NullLongTermMemory()
        )
        initial_observation = latest_trajectory_observation(
            record.get("package_dir")
        ) or Observation(
            kind="terminal_outcome",
            source="execution_audit",
            payload={
                "phase": "recovery",
                "status": str(record.get("status") or "unknown"),
                "application_id": str(record.get("application_id") or ""),
            },
        )
        agent = JobApplicationAgent.resume_runtime(
            name="job-application-agent",
            llm=DeterministicSessionLLM(),
            initial_observation=initial_observation,
            agent_runtime_id=str(
                record.get("agent_runtime_id")
                or f"application-{record.get('application_id') or 'unknown'}"
            ),
            tool_registry=registry,
            database_path=database,
            long_term_memory=long_term_memory,
            policy_gate=JobApplicationPolicyGate(),
        )
        core = agent.agent_core
        planner = JobApplicationRecoveryPlanner(environ)
        executor = JobApplicationRecoveryExecutor(
            run_dir=run_dir,
            database=database,
            environ=environ,
            handlers=handlers,
            now=now,
        )
        executor.bind_agent_runtime(core, initial_observation)
        core.register_recovery_planner(planner.name, planner)
        core.register_recovery_executor(executor.name, executor)
        plan = core.plan_recovery(
            str(record.get("status") or ""),
            record,
            planner=planner.name,
        )
        if plan is None:
            continue
        record["recovery_plan"] = recovery_plan_to_dict(plan)
        result = core.execute_recovery(
            executor.name,
            plan,
            {
                **record,
                "terminal_status": plan.status,
                "retry_scope": plan.retry_scope,
                "recovery_execution": record.get("recovery_execution"),
            },
        )
        serialized = recovery_execution_result_to_dict(result)
        record["recovery_execution"] = serialized
        if "confirmed_outcome_persisted" in set(serialized.get("evidence") or []):
            record["reconciled_from_status"] = str(record.get("status") or "")
            record["status"] = "submitted"
            record["submit_gate"] = "submitted"
            record["error"] = None
        _append_recovery_trajectory(
            record,
            serialized,
            run_dir=run_dir,
        )
        counts[result.status] = counts.get(result.status, 0) + 1
        if result.retry_ready and int(record.get("recovery_attempt") or 0) < 1:
            serialized_actions = serialized.get("actions")
            replacement_summary = _replacement_summary_from_actions(
                serialized_actions,
                run_dir=run_dir,
            )
            verified_targets.append(
                {
                    "company": str(record.get("company") or "unknown"),
                    "title": str(record.get("title") or "unknown"),
                    "source_package_dir": str(record.get("package_dir") or ""),
                    "package_dir": str(
                        (replacement_summary or {}).get("package_dir")
                        or record.get("package_dir")
                        or ""
                    ),
                    "application_id": str(record.get("application_id") or ""),
                    "terminal_status": str(record.get("status") or ""),
                    "recovery_strategy": result.strategy,
                    "recovery_verified": True,
                    "retry_scope": result.retry_scope,
                    "replacement_summary": replacement_summary,
                }
            )
    audit["recovery"] = {
        "status_counts": counts,
        "verified_target_count": len(verified_targets),
    }
    return RecoveryBatchResult(
        applications=tuple(records),
        verified_targets=tuple(verified_targets),
        status_counts=counts,
    )


def _append_recovery_trajectory(
    record: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    run_dir: Path,
) -> None:
    package_value = str(record.get("package_dir") or "")
    if not package_value:
        return
    package_dir = Path(package_value)
    trajectory_path = package_dir / "agent-trajectory.json"
    try:
        package_dir.resolve().relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return
    if not trajectory_path.is_file():
        return
    try:
        trajectory = json.loads(trajectory_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(trajectory, dict):
        return
    stages = trajectory.setdefault("stages", {})
    if not isinstance(stages, dict):
        return
    stages["recovery"] = dict(execution)
    temporary = trajectory_path.with_name(
        f".{trajectory_path.name}.{os.getpid()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(trajectory, indent=2, ensure_ascii=True)
        )
        temporary.replace(trajectory_path)
    finally:
        temporary.unlink(missing_ok=True)


def write_recovery_retry_batch(
    batch_summary_path: Path,
    *,
    verified_targets: tuple[dict[str, Any], ...],
    output_path: Path,
) -> Path | None:
    """Create one policy-annotated batch for verified recovery targets."""
    try:
        batch = json.loads(batch_summary_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(batch, list):
        return None
    targets = {
        (
            str(
                item.get("source_package_dir")
                or item.get("package_dir")
                or ""
            ),
            str(item.get("application_id") or ""),
            str(item.get("company") or "").casefold(),
            str(item.get("title") or "").casefold(),
        ): item
        for item in verified_targets
        if isinstance(item, Mapping)
    }
    selected: list[dict[str, Any]] = []
    for raw_item in batch:
        if not isinstance(raw_item, Mapping):
            continue
        item = dict(raw_item)
        keys = [
            (
                str(item.get("package_dir") or ""),
                str(item.get("application_id") or ""),
                str(item.get("company") or "").casefold(),
                str(item.get("title") or "").casefold(),
            ),
            (
                str(item.get("package_dir") or ""),
                "",
                str(item.get("company") or "").casefold(),
                str(item.get("title") or "").casefold(),
            ),
        ]
        target = next((targets[key] for key in keys if key in targets), None)
        if target is None:
            continue
        replacement_summary = target.get("replacement_summary")
        if isinstance(replacement_summary, Mapping):
            item.update(dict(replacement_summary))
        item.update(
            {
                "retry": True,
                "terminal_status": target["terminal_status"],
                "recovery_verified": True,
                "retry_scope": "single_application",
                "recovery_attempt": 1,
            }
        )
        selected.append(item)
    if not selected:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        f".{output_path.name}.{os.getpid()}.tmp"
    )
    temporary.write_text(json.dumps(selected, indent=2, ensure_ascii=True) + "\n")
    temporary.replace(output_path)
    return output_path


def _replacement_summary_from_actions(
    actions: Any,
    *,
    run_dir: Path,
) -> dict[str, Any] | None:
    if not isinstance(actions, list):
        return None
    for item in actions:
        if not isinstance(item, Mapping) or item.get("action") != "rebuild_scoped_application":
            continue
        details = item.get("details")
        if not isinstance(details, Mapping):
            continue
        summary_value = str(details.get("replacement_summary_path") or "").strip()
        if not summary_value:
            continue
        summary_path = Path(summary_value)
        if not summary_path.is_absolute():
            summary_path = run_dir / summary_path
        try:
            summary_path = summary_path.resolve()
            summary_path.relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        try:
            payload = json.loads(summary_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return None
