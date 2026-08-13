from __future__ import annotations

import json
import hashlib
import io
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.career.recovery import (
    attach_recovery_plan,
    classify_processing_failure,
)
from hello_agents.core.contracts import (
    AgentLoopContext,
    AgentLoopResult,
    AgentThought,
    Observation,
    PolicyDecision,
    ToolCall,
    ToolEffect,
)
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.memory import NullLongTermMemory, ShortTermMemory
from hello_agents.core.perception import StructuredPerception
from hello_agents.core.runtime import AgentCore
from hello_agents.core.trace import agent_loop_result_to_dict
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.registry import ToolRegistry
from job_agent.llm_answer_resolver import llm_answers_enabled
from job_agent.memory import SQLiteApplicationMemory
from job_agent.agent_session import DeterministicSessionLLM
from job_agent.db import connect, init_db
from job_agent.python_runtime import (
    RuntimeActionDenied,
    RuntimeActionRunner,
    load_runtime_payload,
    run_runtime_payload,
)
from job_agent.resumes import ResumePathError, resolve_original_resume_pdf


SUBMIT_GATE = "automatic_submission_enabled"
SUBMIT_GATE_STDOUT_MARKER = "Submit gate:"
SUBMITTED_STDOUT_MARKER = "Submission confirmed:"
SUBMIT_CLICKED_UNCONFIRMED_STDOUT_MARKER = "Submit clicked but confirmation not detected:"
EMAIL_VERIFICATION_REQUIRED_STDOUT_MARKER = "Email verification required:"
SUBMISSION_PROCESSING_ERROR_STDOUT_MARKER = "Submission processing error:"
CANDIDATE_ACCOUNT_REQUIRED_STDOUT_MARKER = "Candidate account required:"
APPLICATION_FORM_UNAVAILABLE_STDOUT_MARKER = "Application form unavailable:"
REVIEW_ITEM_STDOUT_MARKER = "Review item:"
NODE_PLAYWRIGHT_MISSING = "node_playwright_missing_used_python_runtime"


def _runtime_timeout_flag_triggered() -> bool:
    from job_agent import python_runtime

    return bool(python_runtime._RUNTIME_TIMEOUT_TRIGGERED)
TERMINAL_EVIDENCE_FILENAMES = {
    "submission-confirmation.txt",
    "submission-confirmation.png",
    "submission-click-unconfirmed.txt",
    "submission-click-unconfirmed.png",
    "submission-processing-error.txt",
    "submission-processing-error.png",
    "email-verification-required.txt",
    "email-verification-required.png",
    "execution-timeout.txt",
    "review-required.txt",
    "review-required.png",
}
ORDINARY_FAILURE_CIRCUIT_STATUSES = frozenset(
    {
        "application_form_unavailable",
        "autofill_completed_blocked",
        "autofill_failed",
        "autofill_timed_out",
        "submission_processing_error",
    }
)
FAILURE_CIRCUIT_OUTCOME_STATUSES = ORDINARY_FAILURE_CIRCUIT_STATUSES | frozenset(
    {
        "candidate_account_required",
        "email_verification_required",
        "submission_blocked_by_anti_spam",
        "submit_clicked_unconfirmed",
        "submitted",
    }
)
PRE_ACTION_BROWSER_RETRY_DELAY_SECONDS = 1.0
PRE_ACTION_BROWSER_RETRYABLE_ERRORS = frozenset(
    {
        "browser_launch_error",
        "browser_navigation_network_error",
        "browser_session_closed",
        "browser_timeout",
        "playwright_runtime_error",
    }
)
NETWORK_FAILURE_ERRORS = frozenset(
    {
        "browser_navigation_network_error",
        "browser_launch_error",
        "browser_session_closed",
        "network_circuit_breaker_active",
    }
)

TIMEOUT_EVIDENCE_PREFIXES = (
    "Autofill progress:",
    "Autofill field:",
    "Autofill CAPTCHA retry",
    "Autofill stats:",
    "Detected ATS:",
    "Pages filled:",
    "Filled fields",
    "Review-required",
    "Final submit button present:",
    "Submit gate:",
    "Submission confirmed:",
    "Submit clicked but confirmation not detected:",
    "Email verification required:",
    "Submission processing error:",
    "CapMonster CAPTCHA:",
    "Gmail verification is configured;",
    "LLM fallback answers are configured;",
    "Node Playwright is not available",
    "Node Playwright failed to load;",
)


def _record(
    item: dict[str, Any],
    script_path: str | None,
    status: str,
    exit_code: int | None,
    error: str | None = None,
    filled_count: int | None = None,
    review_count: int | None = None,
    submit_gate: str = SUBMIT_GATE,
    cleanup_deleted_files: list[str] | None = None,
    cleanup_errors: list[str] | None = None,
    review_items: list[dict[str, Any]] | None = None,
    evidence: str | None = None,
    recovery_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "company": item.get("company") or "Unknown Company",
        "title": item.get("title") or "Unknown Role",
        "script_path": script_path,
        "status": status,
        "exit_code": exit_code,
        "submit_gate": submit_gate,
        "error": error,
        "filled_count": filled_count,
        "review_count": review_count,
    }
    for key in (
        "application_id",
        "agent_runtime_id",
        "apply_url",
        "package_dir",
        "terminal_status",
        "retry_scope",
        "recovery_attempt",
        "repair_cycle",
    ):
        if item.get(key) is not None:
            record[key] = item.get(key)
    for key in ("retry", "recovery_verified", "repair_verified"):
        if bool(item.get(key)):
            record[key] = True
    if cleanup_deleted_files is not None:
        record["cleanup_deleted_files"] = cleanup_deleted_files
    if cleanup_errors is not None:
        record["cleanup_errors"] = cleanup_errors
    if review_items is not None:
        record["review_items"] = review_items
    if evidence is not None:
        record["evidence"] = evidence
    return attach_recovery_plan(record, recovery_context)


def _network_recovery_context(error: str | None) -> dict[str, Any] | None:
    if str(error or "") not in NETWORK_FAILURE_ERRORS:
        return None
    return {
        "network_failure": True,
        "network_health_rechecked": False,
    }


def _gmail_verification_configured() -> bool:
    configured_token = os.getenv("JOB_AGENT_GMAIL_TOKEN_FILE")
    if configured_token is not None:
        return bool(configured_token.strip())
    return (Path(".job-agent-secrets") / "gmail-token.json").is_file()


def build_browser_execution_tool_call(
    item: Mapping[str, Any],
    script_path: str | Path,
    *,
    real_runtime: bool,
    environ: Mapping[str, str] | None = None,
) -> ToolCall:
    """Build the exact browser action that Agent Core must authorize."""
    environment = os.environ if environ is None else environ
    submit_complete = _configured_bool(
        environment.get("JOB_AGENT_SUBMIT_COMPLETE"),
        default=True,
    )
    try:
        payload = load_runtime_payload(script_path)
    except (OSError, ValueError):
        payload = {}
    structured_runtime = real_runtime and bool(payload)
    profile = payload.get("profile")
    # This outer Tool opens and owns the browser session. The actual final
    # click is a separate live ``ats_submit_application`` ToolCall built from
    # current page blockers, so the session itself is never classified as the
    # submission action.
    effect = ToolEffect.WRITE
    return ToolCall(
        tool_name="browser_execute",
        parameters={
            "application_id": str(item.get("application_id") or ""),
        },
        effect=effect,
        purpose="Fill an ATS form and submit only behind runtime field gates.",
        context={
            "application_url": (
                item.get("apply_url")
                or payload.get("applicationUrl")
                or ""
            ),
            "submit_complete": submit_complete,
            "facts_verified": (
                isinstance(profile, Mapping) and bool(profile)
            ) if structured_runtime else True,
            "blocking_review_items": [],
            "unapproved_sensitive_fields": [],
            "resume_verified": (
                bool(
                    payload.get("resumeFile")
                    or item.get("upload_resume_path")
                )
                or not bool(
                    payload.get("requiredResumePdf")
                    or payload.get("resumeSourceDir")
                    or item.get("required_resume_pdf")
                    or item.get("required_resume_source_dir")
                )
                if structured_runtime
                else True
            ),
            "confirmation_required": True,
            "duplicate": bool(item.get("duplicate")),
            "retry": bool(item.get("retry")),
            "terminal_status": item.get("terminal_status"),
            "recovery_verified": bool(item.get("recovery_verified")),
            "repair_verified": bool(item.get("repair_verified")),
            "retry_scope": item.get("retry_scope"),
        },
    )


def evaluate_browser_execution_policy(
    item: Mapping[str, Any],
    script_path: str | Path,
    *,
    real_runtime: bool,
    environ: Mapping[str, str] | None = None,
) -> PolicyDecision:
    """Compatibility policy probe using the production Agent ToolCall."""
    return JobApplicationPolicyGate().evaluate(
        build_browser_execution_tool_call(
            item,
            script_path,
            real_runtime=real_runtime,
            environ=environ,
        ),
        short_term_memory=ShortTermMemory(),
        long_term_memory=NullLongTermMemory(),
    )


def _execute_application_batch_direct(
    summary_items: list[dict[str, Any]],
    node_binary: str = "node",
    timeout_seconds: int = 300,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    on_record: Callable[[dict[str, Any], int, int], None] | None = None,
    use_gmail_verification: bool = True,
    browser_headless: bool | None = None,
    required_resume_pdf: str | Path | None = None,
    required_resume_source_dir: str | Path | None = None,
    policy_prechecked: bool = False,
    emit_terminal: bool = True,
    runtime_action_runner: RuntimeActionRunner | None = None,
) -> list[dict[str, Any]]:
    """Run generated runtime autofill scripts and return privacy-safe records.

    Gmail verification is enabled by default for real application execution.
    Offline fixture verification must disable it explicitly because its local
    Node Playwright shim is not a browser that can receive verification codes.
    """
    records = []
    total = len(summary_items)

    def publish_terminal(record: dict[str, Any], position: int) -> None:
        if not emit_terminal:
            return
        disposition = "batch complete" if position == total else "continuing"
        print(
            f"Application {position}/{total} terminal: "
            f"{record['status']}; {disposition}."
        )
        if on_record is not None:
            on_record(record, position, total)

    runtime_env = None
    if browser_headless is not None:
        runtime_env = os.environ.copy()
        runtime_env["BROWSER_HEADLESS"] = "true" if browser_headless else "false"
    for position, item in enumerate(summary_items, start=1):
        raw_script_path = item.get("runtime_script_path") or item.get("fill_script_path")
        if not raw_script_path:
            records.append(
                _record(item, None, "skipped_missing_runtime_script", None)
            )
            publish_terminal(records[-1], position)
            continue
        script_path = str(raw_script_path)
        if not Path(script_path).is_file():
            records.append(
                _record(item, script_path, "skipped_runtime_script_not_found", None)
            )
            publish_terminal(records[-1], position)
            continue
        resume_error = _runtime_resume_upload_error(
            item,
            script_path,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=required_resume_source_dir,
        )
        if resume_error:
            records.append(
                _record(
                    item,
                    script_path,
                    "skipped_invalid_resume",
                    None,
                    resume_error,
                    submit_gate="invalid_resume_upload",
                )
            )
            publish_terminal(records[-1], position)
            continue
        if not policy_prechecked:
            policy_decision = evaluate_browser_execution_policy(
                item,
                script_path,
                real_runtime=runner is None,
                environ=runtime_env,
            )
            if not policy_decision.allowed:
                records.append(
                    _record(
                        item,
                        script_path,
                        "skipped_policy_denied",
                        None,
                        policy_decision.code,
                        submit_gate=f"policy_denied:{policy_decision.code}",
                    )
                )
                publish_terminal(records[-1], position)
                continue
        _clear_previous_terminal_evidence(script_path)
        try:
            if runner is None:
                gmail_verification_enabled = use_gmail_verification and _gmail_verification_configured()
                runtime_uses_node_playwright = _script_requires_node_playwright(script_path)
                if runtime_action_runner is not None:
                    result = _run_python_runtime_in_process(
                        script_path,
                        runtime_env=runtime_env,
                        action_runner=runtime_action_runner,
                        timeout_seconds=timeout_seconds,
                    )
                elif runtime_uses_node_playwright and llm_answers_enabled():
                    print("LLM fallback answers are configured; using Python Playwright.")
                    result = _run_python_runtime(
                        script_path, timeout_seconds, runtime_env=runtime_env
                    )
                elif gmail_verification_enabled and runtime_uses_node_playwright:
                    print("Gmail verification is configured; using Python Playwright.")
                    result = _run_python_runtime(
                        script_path, timeout_seconds, runtime_env=runtime_env
                    )
                elif runtime_uses_node_playwright and not _node_playwright_available_for(node_binary, script_path):
                    print("Node Playwright is not available for this runtime; using Python Playwright.")
                    result = _run_python_runtime(
                        script_path, timeout_seconds, runtime_env=runtime_env
                    )
                else:
                    result = _run_runtime_command(
                        [node_binary, script_path], timeout_seconds, runtime_env=runtime_env
                    )
                if _node_playwright_missing(result):
                    print("Node Playwright failed to load; using Python Playwright.")
                    result = _run_python_runtime(
                        script_path, timeout_seconds, runtime_env=runtime_env
                    )
            else:
                run_kwargs: dict[str, Any] = {
                    "capture_output": True,
                    "text": True,
                    "timeout": timeout_seconds,
                    "check": False,
                }
                if runtime_env is not None:
                    run_kwargs["env"] = runtime_env
                result = runner([node_binary, script_path], **run_kwargs)
        except subprocess.TimeoutExpired as exc:
            timeout_stdout = _timeout_stream_text(exc.stdout)
            stats = _parse_autofill_stats(timeout_stdout)
            evidence = _write_timeout_evidence(script_path, timeout_seconds, exc)
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_timed_out",
                    None,
                    "timeout",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    evidence=evidence,
                )
            )
            publish_terminal(records[-1], position)
            continue
        except OSError as exc:
            error_code = _runtime_exception_code(exc)
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    None,
                    error_code,
                    recovery_context=_network_recovery_context(error_code),
                )
            )
            publish_terminal(records[-1], position)
            continue
        except Exception as exc:
            if (
                "wall-clock deadline exceeded" in str(exc)
                or _runtime_timeout_flag_triggered()
            ):
                timeout_exc = subprocess.TimeoutExpired(
                    [sys.executable, "-m", "job_agent.python_runtime", script_path],
                    timeout_seconds,
                )
                stats = _parse_autofill_stats(None)
                evidence = _write_timeout_evidence(
                    script_path,
                    timeout_seconds,
                    timeout_exc,
                )
                records.append(
                    _record(
                        item,
                        script_path,
                        "autofill_timed_out",
                        None,
                        "watchdog_deadline",
                        filled_count=stats[0] if stats else None,
                        review_count=stats[1] if stats else None,
                        evidence=evidence,
                    )
                )
                publish_terminal(records[-1], position)
                continue
            error_code = _runtime_exception_code(exc)
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    None,
                    error_code,
                    recovery_context=_network_recovery_context(error_code),
                )
            )
            publish_terminal(records[-1], position)
            continue

        stdout = result.stdout or ""
        stats = _parse_autofill_stats(stdout)
        review_items = _parse_review_items(stdout)
        blocking_review_items = [
            item for item in review_items if item.get("blocking", True)
        ]
        reported_review_count = stats[1] if stats is not None else 0
        has_blocking_review = bool(blocking_review_items) or (
            reported_review_count > len(review_items)
        )
        effective_review_count = (
            max(reported_review_count, len(review_items))
            if stats is not None or review_items
            else None
        )
        requires_field_gate = _script_requires_node_playwright(script_path)
        if result.returncode == 0 and SUBMITTED_STDOUT_MARKER in stdout:
            deleted_files, cleanup_errors = _cleanup_generated_resume_files(item, script_path)
            records.append(
                _record(
                    item,
                    script_path,
                    "submitted",
                    0,
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    submit_gate="submitted",
                    cleanup_deleted_files=deleted_files,
                    cleanup_errors=cleanup_errors,
                    evidence=_terminal_evidence_path(script_path, "submitted"),
                )
            )
        elif result.returncode == 0 and SUBMIT_CLICKED_UNCONFIRMED_STDOUT_MARKER in stdout:
            records.append(
                _record(
                    item,
                    script_path,
                    "submit_clicked_unconfirmed",
                    0,
                    "submission_confirmation_not_detected",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    submit_gate="submit_clicked_unconfirmed",
                    evidence=_terminal_evidence_path(script_path, "submit_clicked_unconfirmed"),
                )
            )
        elif result.returncode == 0 and EMAIL_VERIFICATION_REQUIRED_STDOUT_MARKER in stdout:
            records.append(
                _record(
                    item,
                    script_path,
                    "email_verification_required",
                    0,
                    "email_verification_required",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    submit_gate="email_verification_required",
                    evidence=_terminal_evidence_path(script_path, "email_verification_required"),
                )
            )
        elif (
            result.returncode == 0
            and SUBMISSION_PROCESSING_ERROR_STDOUT_MARKER in stdout
            and has_blocking_review
        ):
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_completed_blocked",
                    0,
                    filled_count=stats[0] if stats else None,
                    review_count=effective_review_count,
                    review_items=review_items or None,
                    submit_gate="blocked_review_required",
                    evidence=_review_evidence_path(script_path),
                )
            )
        elif result.returncode == 0 and SUBMISSION_PROCESSING_ERROR_STDOUT_MARKER in stdout:
            anti_spam_blocked = _is_anti_spam_rejection(stdout)
            processing_error_kind = classify_processing_failure(stdout)
            records.append(
                _record(
                    item,
                    script_path,
                    "submission_blocked_by_anti_spam" if anti_spam_blocked else "submission_processing_error",
                    0,
                    "submission_blocked_by_anti_spam" if anti_spam_blocked else "submission_processing_error",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    review_items=review_items or None,
                    submit_gate=(
                        "submission_blocked_by_anti_spam"
                        if anti_spam_blocked
                        else "submission_processing_error"
                    ),
                    evidence=_terminal_evidence_path(script_path, "submission_processing_error"),
                    recovery_context={
                        "processing_error_kind": processing_error_kind,
                    },
                )
            )
        elif result.returncode == 0 and CANDIDATE_ACCOUNT_REQUIRED_STDOUT_MARKER in stdout:
            records.append(
                _record(
                    item,
                    script_path,
                    "candidate_account_required",
                    0,
                    "candidate_account_required",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    review_items=review_items or None,
                    submit_gate="candidate_account_required",
                    evidence=_review_evidence_path(script_path),
                )
            )
        elif result.returncode == 0 and APPLICATION_FORM_UNAVAILABLE_STDOUT_MARKER in stdout:
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    0,
                    "application_form_unavailable",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                    submit_gate="application_form_unavailable",
                    review_items=review_items or None,
                    evidence=_review_evidence_path(script_path),
                )
            )
        elif (
            result.returncode == 0
            and SUBMIT_GATE_STDOUT_MARKER in stdout
            and (not requires_field_gate or _stats_show_form_was_seen(stats))
        ):
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_completed_blocked",
                    0,
                    filled_count=stats[0] if stats else None,
                    review_count=effective_review_count,
                    review_items=review_items or None,
                    evidence=_review_evidence_path(script_path),
                )
            )
        elif result.returncode == 0 and SUBMIT_GATE_STDOUT_MARKER in stdout:
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    0,
                    "no_application_fields_detected",
                    filled_count=stats[0] if stats else None,
                    review_count=stats[1] if stats else None,
                )
            )
        elif result.returncode == 0:
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    0,
                    "terminal_status_not_confirmed",
                )
            )
        else:
            # Browser stderr can contain form values or page text. Keep the
            # audit trail useful without copying arbitrary page content.
            records.append(
                _record(
                    item,
                    script_path,
                    "autofill_failed",
                    result.returncode,
                    "runtime_script_nonzero_exit",
                )
            )
        publish_terminal(records[-1], position)
    return records


AgentLoopCallback = Callable[[dict[str, Any], int, int], None]


class _RuntimeCallableTool(Tool):
    """Expose one live page callback without returning private page values."""

    def __init__(
        self,
        name: str,
        *,
        effect: ToolEffect,
        callback: Callable[[], Any],
    ) -> None:
        super().__init__(
            name,
            "Execute one bounded live ATS page action.",
            effect=effect,
        )
        self._callback = callback
        self.private_output: Any = None

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        attempts = 2 if self.name in {"ats_observe_page", "ats_fill_fields"} else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                self.private_output = self._callback()
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt + 1 >= attempts or not _retryable_runtime_action_error(exc):
                    raise
        if last_error is not None and self.private_output is None:
            raise last_error
        return _sanitize_runtime_action_output(self.name, self.private_output)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="application_id",
                type="string",
                description="Stable tracked application identifier.",
                required=False,
            )
        ]


class _RuntimeStopTool(Tool):
    """Represent a bounded Agent decision not to perform a live page action."""

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(
            name,
            "Stop before a live ATS page action whose prerequisites are unmet.",
            effect=ToolEffect.READ,
        )
        self.reason = reason

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "stopped",
            "reason": self.reason,
        }

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="application_id",
                type="string",
                description="Stable tracked application identifier.",
                required=False,
            )
        ]


class _ApplicationRuntimeController:
    """Drive live Python Playwright actions through the original Agent Core."""

    def __init__(
        self,
        agent: JobApplicationAgent,
        *,
        initial_observation: Observation,
        item: Mapping[str, Any],
        parent_call_id: str,
        timeout_seconds: int,
    ) -> None:
        self.agent = agent
        self.current_observation = initial_observation
        self.item = dict(item)
        self.parent_call_id = parent_call_id
        self.timeout_seconds = timeout_seconds
        self.started_at = time.monotonic()
        self.loops: list[AgentLoopResult] = []

    def run_action(
        self,
        name: str,
        effect: str,
        context: Mapping[str, Any],
        callback: Callable[[], Any],
    ) -> Any:
        if time.monotonic() - self.started_at > self.timeout_seconds:
            raise subprocess.TimeoutExpired(
                ["agent_core", name],
                self.timeout_seconds,
            )
        tool_effect = ToolEffect(effect)
        tool = _RuntimeCallableTool(
            name,
            effect=tool_effect,
            callback=callback,
        )
        stop_name = self._stop_tool_name(name)
        stop_reason = self._stop_reason(name, context)
        stop_tool = (
            _RuntimeStopTool(stop_name, stop_reason)
            if stop_name is not None
            else None
        )
        self.agent.tool_registry.register_tool(tool)
        if stop_tool is not None:
            self.agent.tool_registry.register_tool(stop_tool)
        action_context = {
            **dict(context),
            "agent_runtime_id": self.item.get("agent_runtime_id"),
            "parent_call_id": self.parent_call_id,
            "duplicate": bool(self.item.get("duplicate")),
            "retry": bool(self.item.get("retry")),
            "terminal_status": self.item.get("terminal_status"),
            "recovery_verified": bool(self.item.get("recovery_verified")),
            "retry_scope": self.item.get("retry_scope"),
        }
        action_call = ToolCall(
            tool_name=name,
            parameters={
                "application_id": str(
                    self.item.get("application_id") or ""
                )
            },
            effect=tool_effect,
            purpose=(
                "Observe or change the current ATS page and feed its "
                "structured result back to Agent Core."
            ),
            context=action_context,
        )
        steps = [action_call]
        if stop_tool is not None:
            steps.append(
                ToolCall(
                    tool_name=stop_tool.name,
                    parameters={
                        "application_id": str(
                            self.item.get("application_id") or ""
                        )
                    },
                    effect=ToolEffect.READ,
                    purpose=(
                        "Stop this bounded live ATS action when its current "
                        "prerequisites are not satisfied."
                    ),
                    context=action_context,
                )
            )
        try:
            loop = self.agent.agent_core.run_loop(
                self.agent.agent_core.create_plan(
                    f"Execute live ATS action: {name}",
                    steps,
                ),
                initial_observation=self.current_observation,
                thought_builder=(
                    lambda loop_context: self._build_runtime_thought(
                        loop_context,
                        action_name=name,
                        action_context=action_context,
                        stop_name=stop_name,
                    )
                ),
                memory_query=(
                    f"{self.item.get('company') or ''} "
                    f"{self.item.get('title') or ''}"
                ).strip(),
                remember_rounds=self.agent.database_path is not None,
                memory_namespace="agent_run",
                max_rounds=1 if stop_tool is not None else None,
            )
        finally:
            self.agent.tool_registry.unregister(name)
            if stop_tool is not None:
                self.agent.tool_registry.unregister(stop_tool.name)
        self.loops.append(loop)
        self.current_observation = loop.observations[-1]
        result = loop.results[0] if loop.results else None
        if result is None:
            raise RuntimeError(f"Live ATS action '{name}' returned no result.")
        if (
            result.policy_decision is not None
            and not result.policy_decision.allowed
        ):
            raise RuntimeActionDenied(result.policy_decision.code)
        if not result.ok:
            raise RuntimeError(
                result.error or f"Live ATS action '{name}' failed."
            )
        if result.tool_name != name:
            reason = (
                str(result.output.get("reason") or stop_reason)
                if isinstance(result.output, Mapping)
                else stop_reason
            )
            raise RuntimeActionDenied(reason)
        return tool.private_output

    def _build_runtime_thought(
        self,
        context: AgentLoopContext,
        *,
        action_name: str,
        action_context: Mapping[str, Any],
        stop_name: str | None,
    ) -> AgentThought:
        thought = self.agent._build_loop_thought(context)
        if stop_name is None:
            return thought

        selected_name = (
            action_name
            if self._action_prerequisites_met(action_name, action_context)
            else stop_name
        )
        selected_action = next(
            action
            for action in context.remaining_actions
            if action.tool_name == selected_name
        )
        return replace(
            thought,
            selected_action=selected_action,
            summary=(
                f"Selected bounded live ATS action '{selected_name}' from "
                f"the current Observation and {len(context.remaining_actions)} "
                "registered candidates."
            ),
            self_criticism=(
                "This selection does not prove the page changed. Policy Gate "
                "must authorize it and only its ToolResult Observation may "
                "advance the Agent session."
            ),
        )

    @staticmethod
    def _stop_tool_name(action_name: str) -> str | None:
        return {
            "ats_advance_page": "ats_stop_page_navigation",
            "ats_submit_application": "ats_stop_before_submit",
        }.get(action_name)

    @classmethod
    def _action_prerequisites_met(
        cls,
        action_name: str,
        context: Mapping[str, Any],
    ) -> bool:
        return cls._pre_click_assertion_error(action_name, context) is None

    @classmethod
    def _stop_reason(
        cls,
        action_name: str,
        context: Mapping[str, Any],
    ) -> str:
        assertion_error = cls._pre_click_assertion_error(action_name, context)
        if assertion_error is None:
            return "agent_core_selected_stop"
        return assertion_error

    @staticmethod
    def _pre_click_assertion_error(
        action_name: str,
        context: Mapping[str, Any],
    ) -> str | None:
        if action_name == "ats_advance_page":
            if "blocking_review_count" not in context:
                return "missing_navigation_state_assertion"
            return (
                None
                if int(context.get("blocking_review_count") or 0) == 0
                else "blocking_review_items"
            )
        if action_name != "ats_submit_application":
            return None
        required_keys = {
            "submit_complete",
            "facts_verified",
            "blocking_review_items",
            "unapproved_sensitive_fields",
            "resume_verified",
            "confirmation_required",
        }
        if any(key not in context for key in required_keys):
            return "missing_submit_state_assertion"
        if not bool(context.get("submit_complete")):
            return "submission_disabled"
        if not bool(context.get("facts_verified")):
            return "unverified_candidate_facts"
        if bool(context.get("blocking_review_items")):
            return "blocking_review_items"
        if bool(context.get("unapproved_sensitive_fields")):
            return "unapproved_sensitive_fields"
        if not bool(context.get("resume_verified")):
            return "resume_provenance_unverified"
        if not bool(context.get("confirmation_required", True)):
            return "submission_confirmation_not_required"
        return None


def _sanitize_runtime_action_output(name: str, output: Any) -> dict[str, Any]:
    if name == "ats_observe_page":
        fields = output if isinstance(output, list) else []
        compact_fields = [
            _runtime_field_projection(field)
            for field in fields
            if isinstance(field, Mapping)
        ]
        return {
            "status": "observed",
            "field_count": len(compact_fields),
            "fields": compact_fields,
        }
    if name == "ats_fill_fields" and isinstance(output, Mapping):
        filled = output.get("filled")
        review = output.get("review")
        return {
            "status": "filled",
            "filled_count": len(filled) if isinstance(filled, list) else 0,
            "review_count": len(review) if isinstance(review, list) else 0,
            "blocking_review_count": sum(
                bool(item.get("blocking", True))
                for item in review
                if isinstance(item, Mapping)
            ) if isinstance(review, list) else 0,
        }
    return {"status": "completed"}


def _retryable_runtime_action_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}:{exc}".casefold()
    return any(
        marker in text
        for marker in (
            "timeout",
            "detached",
            "not attached",
            "target closed",
            "execution context was destroyed",
            "element is not visible",
        )
    )


def _runtime_exception_code(exc: Exception) -> str:
    """Reduce a runtime exception to a privacy-safe operational code."""
    name = type(exc).__name__
    if name != "Error":
        return name
    text = str(exc).casefold()
    if (
        "using playwright sync api inside the asyncio loop" in text
        or "playwright sync api inside the asyncio loop" in text
    ):
        return "playwright_sync_api_context_error"
    if "executable doesn't exist" in text:
        return "browser_executable_missing"
    if "host system is missing dependencies" in text:
        return "browser_dependencies_missing"
    if any(
        marker in text
        for marker in (
            "target page, context or browser has been closed",
            "browser has been closed",
            "target closed",
        )
    ):
        return "browser_session_closed"
    if "net::err_" in text:
        return "browser_navigation_network_error"
    if "timeout" in text:
        return "browser_timeout"
    if "browsertype.launch" in text:
        return "browser_launch_error"
    return "playwright_runtime_error"


def _runtime_field_projection(field: Mapping[str, Any]) -> dict[str, Any]:
    options = field.get("options")
    option_labels: list[str] = []
    if isinstance(options, list):
        for option in options[:12]:
            if isinstance(option, Mapping):
                raw = option.get("label") or option.get("text") or option.get("value")
            else:
                raw = option
            option_labels.append(_compact_runtime_text(raw, 100))
    return {
        "label": _compact_runtime_text(
            field.get("label")
            or field.get("ariaLabel")
            or field.get("placeholder")
            or field.get("name")
            or "",
            180,
        ),
        "type": _compact_runtime_text(field.get("type") or "", 40),
        "role": _compact_runtime_text(field.get("role") or "", 40),
        "kind": _compact_runtime_text(field.get("kind") or "", 40),
        "required": bool(field.get("required")),
        "sensitive": bool(field.get("sensitive") or field.get("isSensitive")),
        "options": option_labels,
        "option_count": len(options) if isinstance(options, list) else 0,
    }


def _compact_runtime_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


class _BrowserApplicationTool(Tool):
    """Execute one real application through the existing validated runtime."""

    def __init__(
        self,
        item: dict[str, Any],
        *,
        effect: ToolEffect,
        node_binary: str,
        timeout_seconds: int,
        runner: Callable[..., subprocess.CompletedProcess] | None,
        use_gmail_verification: bool,
        browser_headless: bool | None,
        required_resume_pdf: str | Path | None,
        required_resume_source_dir: str | Path | None,
        parent_call_id: str,
    ) -> None:
        super().__init__(
            "browser_execute",
            "Execute one ATS application and return a structured terminal result.",
            effect=effect,
        )
        self._item = item
        self._node_binary = node_binary
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._use_gmail_verification = use_gmail_verification
        self._browser_headless = browser_headless
        self._required_resume_pdf = required_resume_pdf
        self._required_resume_source_dir = required_resume_source_dir
        self._parent_call_id = parent_call_id
        self.runtime_controller: _ApplicationRuntimeController | None = None
        self.record: dict[str, Any] | None = None

    def bind_runtime_controller(
        self,
        controller: _ApplicationRuntimeController,
    ) -> None:
        self.runtime_controller = controller

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        records = self._execute_once()
        if (
            len(records) == 1
            and self.runtime_controller is not None
            and not self.runtime_controller.loops
            and records[0].get("status") == "autofill_failed"
            and records[0].get("error")
            in PRE_ACTION_BROWSER_RETRYABLE_ERRORS
        ):
            first_error = str(records[0].get("error") or "")
            print(
                "Browser runtime failed before any ATS action; "
                "retrying once with a fresh browser session."
            )
            time.sleep(PRE_ACTION_BROWSER_RETRY_DELAY_SECONDS)
            records = self._execute_once()
            if len(records) == 1:
                records[0]["runtime_retry_count"] = 1
                records[0]["runtime_retry_reason"] = first_error
        if len(records) != 1:
            raise RuntimeError("Browser runtime did not return one terminal record.")
        self.record = records[0]
        return self.record

    def _execute_once(self) -> list[dict[str, Any]]:
        return _execute_application_batch_direct(
            [self._item],
            node_binary=self._node_binary,
            timeout_seconds=self._timeout_seconds,
            runner=self._runner,
            use_gmail_verification=self._use_gmail_verification,
            browser_headless=self._browser_headless,
            required_resume_pdf=self._required_resume_pdf,
            required_resume_source_dir=self._required_resume_source_dir,
            policy_prechecked=True,
            emit_terminal=False,
            runtime_action_runner=(
                self.runtime_controller.run_action
                if self.runtime_controller is not None
                else None
            ),
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="application_id",
                type="string",
                description="Stable tracked application identifier.",
                required=False,
            )
        ]


class _TerminalOutcomeRouterTool(Tool):
    """Route the observed browser terminal state without another side effect."""

    def __init__(self, browser_tool: _BrowserApplicationTool) -> None:
        super().__init__(
            "terminal_outcome_router",
            "Classify the observed browser outcome for completion or recovery.",
            effect=ToolEffect.READ,
        )
        self._browser_tool = browser_tool

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        record = self._browser_tool.record
        if record is None:
            raise RuntimeError(
                "Browser outcome is unavailable for terminal routing."
            )
        recovery_plan = record.get("recovery_plan")
        strategy = (
            str(recovery_plan.get("strategy") or "")
            if isinstance(recovery_plan, Mapping)
            else ""
        )
        terminal_status = str(record.get("status") or "unknown")
        return {
            "status": terminal_status,
            "application_id": record.get("application_id"),
            "next_action": (
                "complete"
                if terminal_status == "submitted"
                else "recovery"
                if strategy
                else "record_terminal"
            ),
            "recovery_strategy": strategy or None,
            "retry_scope": (
                recovery_plan.get("retry_scope")
                if isinstance(recovery_plan, Mapping)
                else None
            ),
        }

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="application_id",
                type="string",
                description="Stable tracked application identifier.",
                required=False,
            )
        ]


class _RuntimePackageInspectTool(Tool):
    def __init__(self, script_path: str) -> None:
        super().__init__(
            "runtime_package_inspect",
            "Inspect the prepared runtime without opening a browser.",
            effect=ToolEffect.READ,
        )
        self._script_path = script_path

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        path = Path(self._script_path)
        payload = _runtime_payload_from_script(self._script_path)
        return {
            "status": "ready" if path.is_file() else "missing",
            "runtime_exists": path.is_file(),
            "structured_payload": isinstance(payload, Mapping),
        }

    def get_parameters(self) -> list[ToolParameter]:
        return []


class _ResumeProvenanceInspectTool(Tool):
    def __init__(
        self,
        item: dict[str, Any],
        script_path: str,
        *,
        required_resume_pdf: str | Path | None,
        required_resume_source_dir: str | Path | None,
    ) -> None:
        super().__init__(
            "resume_provenance_inspect",
            "Verify the prepared resume source and hash without uploading it.",
            effect=ToolEffect.READ,
        )
        self._item = item
        self._script_path = script_path
        self._required_resume_pdf = required_resume_pdf
        self._required_resume_source_dir = required_resume_source_dir

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        error = _runtime_resume_upload_error(
            self._item,
            self._script_path,
            required_resume_pdf=self._required_resume_pdf,
            required_resume_source_dir=self._required_resume_source_dir,
        )
        return {
            "status": "verified" if error is None else "invalid",
            "resume_verified": error is None,
            "error": error,
        }

    def get_parameters(self) -> list[ToolParameter]:
        return []


def execute_application_batch(
    summary_items: list[dict[str, Any]],
    node_binary: str = "node",
    timeout_seconds: int = 300,
    runner: Callable[..., subprocess.CompletedProcess] | None = None,
    on_record: Callable[[dict[str, Any], int, int], None] | None = None,
    use_gmail_verification: bool = True,
    browser_headless: bool | None = None,
    required_resume_pdf: str | Path | None = None,
    required_resume_source_dir: str | Path | None = None,
    database_path: str | Path | None = None,
    on_agent_loop: AgentLoopCallback | None = None,
    unified_runtime: bool = True,
) -> list[dict[str, Any]]:
    """Execute every real browser mutation through an auditable Agent Core loop."""
    records: list[dict[str, Any]] = []
    total = len(summary_items)
    blocked_companies, blocked_tenants = _active_anti_spam_scopes(database_path)
    failure_company_sequences, failure_adapter_sequences = (
        _active_failure_circuit_sequences(database_path)
    )
    network_consecutive_failures = 0
    network_circuit_open = False
    network_last_error = ""
    network_threshold = _network_health_circuit_threshold()
    runtime_env = None
    if browser_headless is not None:
        runtime_env = os.environ.copy()
        runtime_env["BROWSER_HEADLESS"] = (
            "true" if browser_headless else "false"
        )

    for position, item in enumerate(summary_items, start=1):
        raw_script_path = (
            item.get("runtime_script_path")
            or item.get("fill_script_path")
            or ""
        )
        call = build_browser_execution_tool_call(
            item,
            str(raw_script_path),
            real_runtime=runner is None,
            environ=runtime_env,
        )
        company_scope = _company_scope(item.get("company"))
        tenant_scope = _anti_spam_tenant_scope(
            item.get("apply_url"),
            item.get("company"),
        )
        failure_adapter_scope = _failure_adapter_scope(
            item.get("apply_url"),
            item.get("company"),
        )
        if (
            company_scope in blocked_companies
            or tenant_scope in blocked_tenants
        ):
            call = replace(
                call,
                context={
                    **dict(call.context),
                    "anti_spam_cooldown_active": True,
                    "anti_spam_scope": tenant_scope or company_scope,
                },
            )
        company_failure_status = _open_failure_circuit_status(
            failure_company_sequences.get(company_scope)
        )
        adapter_failure_status = _open_failure_circuit_status(
            failure_adapter_sequences.get(failure_adapter_scope)
        )
        verified_scoped_retry = (
            bool(item.get("retry"))
            and item.get("retry_scope") == "single_application"
            and bool(
                item.get("repair_verified")
                or item.get("recovery_verified")
            )
        )
        if (
            company_failure_status or adapter_failure_status
        ) and not verified_scoped_retry:
            call = replace(
                call,
                context={
                    **dict(call.context),
                    "failure_circuit_breaker_active": True,
                    "failure_circuit_scope": (
                        failure_adapter_scope
                        if adapter_failure_status
                        else company_scope
                    ),
                    "failure_circuit_status": (
                        adapter_failure_status or company_failure_status
                    ),
                },
            )
        elif (company_failure_status or adapter_failure_status) and verified_scoped_retry:
            call = replace(
                call,
                context={
                    **dict(call.context),
                    "failure_circuit_bypass": "verified_scoped_retry",
                    "failure_circuit_scope": (
                        failure_adapter_scope
                        if adapter_failure_status
                        else company_scope
                    ),
                    "failure_circuit_status": (
                        adapter_failure_status or company_failure_status
                    ),
                },
            )
        if network_circuit_open:
            call = replace(
                call,
                context={
                    **dict(call.context),
                    "network_health_circuit_active": True,
                    "network_health_scope": "global_batch",
                    "network_health_consecutive_failures": network_consecutive_failures,
                    "network_health_threshold": network_threshold,
                },
            )
        registry = ToolRegistry()
        long_term_memory = (
            SQLiteApplicationMemory(database_path)
            if database_path is not None
            else NullLongTermMemory()
        )
        perception = StructuredPerception()
        handoff = item.get("agent_handoff")
        if (
            isinstance(handoff, Mapping)
            and str(handoff.get("observation_id") or "")
        ):
            handoff_payload = handoff.get("payload")
            initial_observation = Observation(
                kind=str(handoff.get("kind") or "application_handoff"),
                source=str(handoff.get("source") or "package_builder"),
                payload=(
                    dict(handoff_payload)
                    if isinstance(handoff_payload, Mapping)
                    else {
                        "phase": "execution",
                        "status": "ready",
                        "application_id": str(
                            item.get("application_id") or ""
                        ),
                        "agent_runtime_id": str(
                            item.get("agent_runtime_id") or ""
                        ),
                        "handoff_from_prepare": True,
                    }
                ),
                observed_at=str(
                    handoff.get("observed_at")
                    or datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds"
                    )
                ),
                observation_id=str(handoff["observation_id"]),
            )
        else:
            initial_observation = perception.observe(
                "application_execution_request",
                "batch_summary",
                {
                    "phase": "execution",
                    "status": "ready",
                    "application_id": str(
                        item.get("application_id") or ""
                    ),
                    "agent_runtime_id": str(
                        item.get("agent_runtime_id") or ""
                    ),
                },
            )
        browser_tool = _BrowserApplicationTool(
            item,
            effect=call.effect,
            node_binary=node_binary,
            timeout_seconds=timeout_seconds,
            runner=runner,
            use_gmail_verification=use_gmail_verification,
            browser_headless=browser_headless,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=required_resume_source_dir,
            parent_call_id=call.call_id,
        )
        registry.register_tool(browser_tool)
        registry.register_tool(_TerminalOutcomeRouterTool(browser_tool))
        registry.register_tool(_RuntimePackageInspectTool(str(raw_script_path)))
        registry.register_tool(
            _ResumeProvenanceInspectTool(
                item,
                str(raw_script_path),
                required_resume_pdf=required_resume_pdf,
                required_resume_source_dir=required_resume_source_dir,
            )
        )
        agent = JobApplicationAgent.resume_runtime(
            name="job-application-agent",
            llm=DeterministicSessionLLM(),
            initial_observation=initial_observation,
            agent_runtime_id=str(
                item.get("agent_runtime_id")
                or f"application-{item.get('application_id') or position}"
            ),
            tool_registry=registry,
            database_path=database_path,
            long_term_memory=long_term_memory,
            policy_gate=JobApplicationPolicyGate(),
        )
        core = agent.agent_core
        preflight_loop = core.run_concurrent_read_loop(
            core.create_plan(
                "Inspect the runtime package and resume provenance concurrently.",
                [
                    ToolCall(
                        tool_name="runtime_package_inspect",
                        parameters={},
                        effect=ToolEffect.READ,
                        purpose="Confirm the prepared runtime is present.",
                        context={"phase": "runtime_preflight"},
                    ),
                    ToolCall(
                        tool_name="resume_provenance_inspect",
                        parameters={},
                        effect=ToolEffect.READ,
                        purpose="Confirm the approved resume provenance.",
                        context={"phase": "runtime_preflight"},
                    ),
                ],
            ),
            initial_observation=initial_observation,
            memory_query=(
                f"{item.get('company') or ''} {item.get('title') or ''}"
            ).strip(),
            remember_rounds=database_path is not None,
            memory_namespace="agent_run",
            max_workers=2,
        )
        agent.last_loop_result = preflight_loop
        agent.loop_results.append(preflight_loop)
        controller = _ApplicationRuntimeController(
            agent,
            initial_observation=preflight_loop.observations[-1],
            item=item,
            parent_call_id=call.call_id,
            timeout_seconds=timeout_seconds,
        )
        if (
            runner is None
            and unified_runtime
            and isinstance(
                _runtime_payload_from_script(str(raw_script_path)),
                Mapping,
            )
            and not (
                Path(str(raw_script_path)).parent
                / "node_modules"
                / "playwright"
            ).is_dir()
        ):
            browser_tool.bind_runtime_controller(controller)
        loop_result = agent.continue_with_tools(
            "Execute one truthful application and observe its terminal outcome.",
            [
                call,
                ToolCall(
                    tool_name="terminal_outcome_router",
                    parameters={
                        "application_id": str(
                            item.get("application_id") or ""
                        )
                    },
                    effect=ToolEffect.READ,
                    purpose=(
                        "Consume the browser ToolResult and route its terminal "
                        "outcome."
                    ),
                    context={
                        "phase": "terminal_routing",
                        "terminal_status": item.get("terminal_status"),
                    },
                ),
            ],
            memory_query=(
                f"{item.get('company') or ''} {item.get('title') or ''}"
            ).strip(),
        )
        tool_result = (
            loop_result.results[0] if loop_result.results else None
        )
        if tool_result is not None and isinstance(tool_result.output, Mapping):
            record = dict(tool_result.output)
        elif (
            tool_result is not None
            and tool_result.policy_decision is not None
            and not tool_result.policy_decision.allowed
        ):
            decision = tool_result.policy_decision
            if decision.code == "network_health_circuit_active":
                record = _record(
                    item,
                    str(raw_script_path) or None,
                    "autofill_failed",
                    None,
                    "network_circuit_breaker_active",
                    submit_gate="network_health_circuit_active",
                    recovery_context={
                        "network_failure": True,
                        "network_health_rechecked": False,
                    },
                )
            else:
                record = _record(
                    item,
                    str(raw_script_path) or None,
                    "skipped_policy_denied",
                    None,
                    decision.code,
                    submit_gate=f"policy_denied:{decision.code}",
                )
        else:
            record = _record(
                item,
                str(raw_script_path) or None,
                "autofill_failed",
                None,
                "agent_browser_tool_failed",
            )

        if record.get("status") == "submission_blocked_by_anti_spam":
            if company_scope:
                blocked_companies.add(company_scope)
            if tenant_scope:
                blocked_tenants.add(tenant_scope)
        terminal_status = str(record.get("status") or "")
        terminal_error = str(record.get("error") or "")
        network_failure = terminal_error in NETWORK_FAILURE_ERRORS
        if network_failure:
            network_consecutive_failures += 1
            network_last_error = terminal_error
            network_circuit_open = network_consecutive_failures >= network_threshold
        elif terminal_status not in {"skipped_policy_denied", "skipped_network_circuit"}:
            network_consecutive_failures = 0
            network_circuit_open = False
        if network_failure or network_consecutive_failures or network_circuit_open:
            record["network_health_observation"] = {
                "kind": "batch_network_health",
                "scope": "global_batch",
                "status": "open" if network_circuit_open else "healthy",
                "consecutive_failures": network_consecutive_failures,
                "threshold": network_threshold,
                "last_error": network_last_error or None,
            }
        _advance_failure_circuit_sequence(
            failure_company_sequences,
            company_scope,
            terminal_status,
        )
        _advance_failure_circuit_sequence(
            failure_adapter_sequences,
            failure_adapter_scope,
            terminal_status,
        )

        trace = agent_loop_result_to_dict(loop_result)
        trace["preflight"] = agent_loop_result_to_dict(preflight_loop)
        trace["runtime_steps"] = [
            agent_loop_result_to_dict(runtime_loop)
            for runtime_loop in controller.loops
        ]
        trace["application"] = {
            "application_id": str(item.get("application_id") or ""),
            "agent_runtime_id": str(item.get("agent_runtime_id") or ""),
            "company": str(item.get("company") or "Unknown Company"),
            "title": str(item.get("title") or "Unknown Role"),
            "script_path": str(raw_script_path),
            "package_dir": str(item.get("package_dir") or ""),
        }
        if on_agent_loop is not None:
            on_agent_loop(trace, position, total)

        records.append(record)
        disposition = "batch complete" if position == total else "continuing"
        print(
            f"Application {position}/{total} terminal: "
            f"{record['status']}; {disposition}."
        )
        if on_record is not None:
            on_record(record, position, total)
    return records


def _company_scope(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _anti_spam_tenant_scope(
    application_url: Any,
    company: Any,
) -> str:
    raw = str(application_url or "").strip()
    if not raw:
        return _company_scope(company)
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").casefold()
    segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    if host == "job-boards.greenhouse.io":
        if segments and segments[0] not in {"jobs", "v1"}:
            return f"{host}/{segments[0]}"
    elif host in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"}:
        if segments:
            return f"{host}/{segments[0]}"
    return host or _company_scope(company)


def _failure_adapter_scope(
    application_url: Any,
    company: Any,
) -> str:
    tenant_scope = _anti_spam_tenant_scope(application_url, company)
    if not tenant_scope:
        return ""
    host = tenant_scope.split("/", 1)[0]
    if host in {
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "jobs.ashbyhq.com",
    } and "/" in tenant_scope:
        return tenant_scope
    company_scope = _company_scope(company)
    return (
        f"{tenant_scope}/{company_scope}"
        if company_scope
        else tenant_scope
    )


def _failure_circuit_breaker_hours() -> int:
    try:
        value = int(
            os.getenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS")
            or "6"
        )
    except ValueError:
        value = 6
    return max(1, value)


def _failure_circuit_breaker_threshold() -> int:
    try:
        value = int(
            os.getenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD")
            or "2"
        )
    except ValueError:
        value = 2
    return max(2, value)


def _network_health_circuit_threshold() -> int:
    try:
        value = int(
            os.getenv("JOB_AGENT_NETWORK_HEALTH_CIRCUIT_THRESHOLD") or "3"
        )
    except ValueError:
        value = 3
    return max(2, value)


def _open_failure_circuit_status(
    sequence: tuple[str, int] | None,
) -> str:
    if sequence is None:
        return ""
    status, consecutive = sequence
    if (
        status in ORDINARY_FAILURE_CIRCUIT_STATUSES
        and consecutive >= _failure_circuit_breaker_threshold()
    ):
        return status
    return ""


def _advance_failure_circuit_sequence(
    sequences: dict[str, tuple[str, int]],
    scope: str,
    status: str,
) -> None:
    if not scope or status not in FAILURE_CIRCUIT_OUTCOME_STATUSES:
        return
    previous_status, previous_count = sequences.get(scope, ("", 0))
    sequences[scope] = (
        (status, previous_count + 1)
        if status == previous_status
        else (status, 1)
    )


def _active_failure_circuit_sequences(
    database_path: str | Path | None,
) -> tuple[dict[str, tuple[str, int]], dict[str, tuple[str, int]]]:
    if database_path is None:
        return {}, {}
    path = Path(database_path)
    if not path.is_file():
        return {}, {}
    conn = connect(path)
    try:
        init_db(conn)
        placeholders = ",".join(
            "?" for _ in FAILURE_CIRCUIT_OUTCOME_STATUSES
        )
        rows = conn.execute(
            f"""
            select id, company, apply_url, status
            from applications
            where status in ({placeholders})
              and datetime(updated_at) >= datetime('now', ?)
            order by datetime(updated_at) desc, id desc
            """,
            (
                *sorted(FAILURE_CIRCUIT_OUTCOME_STATUSES),
                f"-{_failure_circuit_breaker_hours()} hours",
            ),
        ).fetchall()
    finally:
        conn.close()

    grouped_companies: dict[str, list[str]] = {}
    grouped_adapters: dict[str, list[str]] = {}
    for row in rows:
        status = str(row["status"] or "")
        company_scope = _company_scope(row["company"])
        if company_scope:
            grouped_companies.setdefault(company_scope, []).append(status)
        adapter_scope = _failure_adapter_scope(
            row["apply_url"],
            row["company"],
        )
        if adapter_scope:
            grouped_adapters.setdefault(adapter_scope, []).append(status)

    def latest_sequences(
        grouped: Mapping[str, list[str]],
    ) -> dict[str, tuple[str, int]]:
        sequences: dict[str, tuple[str, int]] = {}
        for scope, statuses in grouped.items():
            latest = statuses[0] if statuses else ""
            consecutive = 0
            for status in statuses:
                if status != latest:
                    break
                consecutive += 1
            if latest:
                sequences[scope] = (latest, consecutive)
        return sequences

    return latest_sequences(grouped_companies), latest_sequences(grouped_adapters)


def _active_anti_spam_scopes(
    database_path: str | Path | None,
) -> tuple[set[str], set[str]]:
    if database_path is None:
        return set(), set()
    path = Path(database_path)
    if not path.is_file():
        return set(), set()
    try:
        cooldown_hours = max(
            1,
            int(os.getenv("JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS") or "24"),
        )
    except ValueError:
        cooldown_hours = 24
    conn = connect(path)
    try:
        init_db(conn)
        rows = conn.execute(
            """
            select id, company, apply_url, status
            from applications
            where status in ('submission_blocked_by_anti_spam', 'submitted')
              and datetime(updated_at) >= datetime('now', ?)
            order by datetime(updated_at) desc, id desc
            """,
            (f"-{cooldown_hours} hours",),
        ).fetchall()
    finally:
        conn.close()

    latest_company_status: dict[str, str] = {}
    latest_tenant_status: dict[str, str] = {}
    for row in rows:
        status = str(row["status"] or "")
        company_scope = _company_scope(row["company"])
        if company_scope and company_scope not in latest_company_status:
            latest_company_status[company_scope] = status
        tenant_scope = _anti_spam_tenant_scope(
            row["apply_url"],
            row["company"],
        )
        if tenant_scope and tenant_scope not in latest_tenant_status:
            latest_tenant_status[tenant_scope] = status

    return (
        {
            scope
            for scope, status in latest_company_status.items()
            if status == "submission_blocked_by_anti_spam"
        },
        {
            scope
            for scope, status in latest_tenant_status.items()
            if status == "submission_blocked_by_anti_spam"
        },
    )


def _configured_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _runtime_resume_upload_error(
    item: dict[str, Any],
    script_path: str,
    *,
    required_resume_pdf: str | Path | None = None,
    required_resume_source_dir: str | Path | None = None,
) -> str | None:
    raw_path = item.get("upload_resume_path")
    runtime_resume = _runtime_resume_file_from_script(script_path)
    runtime_payload = _runtime_payload_from_script(script_path)
    item_required_resume_pdf = item.get("required_resume_pdf") or (
        runtime_payload.get("requiredResumePdf") if runtime_payload else None
    )
    item_required_source_dir = item.get("required_resume_source_dir") or (
        runtime_payload.get("resumeSourceDir") if runtime_payload else None
    )
    effective_required_resume_pdf = required_resume_pdf or item_required_resume_pdf
    effective_required_source_dir = required_resume_source_dir or item_required_source_dir
    if raw_path and runtime_resume is not None:
        summary_resume = Path(str(raw_path)).expanduser()
        try:
            if summary_resume.resolve() != runtime_resume.resolve():
                return (
                    "runtime resumeFile does not match summary upload_resume_path: "
                    f"{runtime_resume}; expected: {summary_resume}"
                )
        except OSError:
            return (
                "runtime resumeFile does not match summary upload_resume_path: "
                f"{runtime_resume}; expected: {summary_resume}"
            )
    if not raw_path and runtime_resume is not None:
        raw_path = str(runtime_resume)
    if not raw_path:
        if effective_required_resume_pdf is not None or effective_required_source_dir is not None:
            return "missing required PDF resume upload path"
        return None
    raw_package_dir = item.get("package_dir") or Path(script_path).resolve().parent
    try:
        resolved_resume = resolve_original_resume_pdf(
            Path(str(raw_path)).expanduser(),
            source_dir=effective_required_source_dir,
            package_dir=Path(str(raw_package_dir)),
            required_pdf=effective_required_resume_pdf,
        )
    except ResumePathError as exc:
        return str(exc)
    expected_sha = str(item.get("upload_resume_pdf_sha256") or "").strip()
    if expected_sha:
        actual_sha = _sha256_file(resolved_resume)
        if actual_sha != expected_sha:
            return (
                "resume upload PDF hash does not match prepared summary: "
                f"{actual_sha}; expected: {expected_sha}"
            )
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_payload_from_script(script_path: str) -> dict[str, Any] | None:
    try:
        payload = load_runtime_payload(Path(script_path))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _runtime_resume_file_from_script(script_path: str) -> Path | None:
    path = Path(script_path)
    payload = _runtime_payload_from_script(script_path)
    if payload is None:
        return None
    raw_path = payload.get("resumeFile")
    if not raw_path:
        return None
    resume_path = Path(str(raw_path)).expanduser()
    if not resume_path.is_absolute():
        resume_path = path.parent / resume_path
    return resume_path


def _parse_autofill_stats(stdout: str) -> tuple[int, int] | None:
    matches = list(
        re.finditer(r"Autofill stats:\s*filled=(\d+)\s+review=(\d+)", stdout or "")
    )
    if not matches:
        return None
    match = matches[-1]
    return int(match.group(1)), int(match.group(2))


def _is_anti_spam_rejection(stdout: str) -> bool:
    text = (stdout or "").lower()
    return any(
        marker in text
        for marker in (
            "flagged as possible spam",
            "application was flagged as spam",
            "too many requests",
            "rate limit",
            "rate-limit",
            "rate limited",
            "rate-limited",
            "http 429",
            "status 429",
            "reached your application limit",
            "application limit",
            "already applied",
            "you have already applied",
            "only one application",
        )
    )


def _parse_review_items(stdout: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_line in (stdout or "").splitlines():
        if not raw_line.startswith(REVIEW_ITEM_STDOUT_MARKER):
            continue
        payload = raw_line[len(REVIEW_ITEM_STDOUT_MARKER):].strip()
        if not payload:
            continue
        try:
            item = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "label": str(item.get("label") or ""),
                "reason": str(item.get("reason") or ""),
                "sensitive": bool(item.get("sensitive", False)),
                "blocking": bool(item.get("blocking", True)),
            }
        )
    return items


def _stats_show_form_was_seen(stats: tuple[int, int] | None) -> bool:
    return stats is not None and (stats[0] + stats[1]) > 0


def _review_evidence_path(script_path: str) -> str | None:
    path = Path(script_path).resolve().parent / "review-required.txt"
    if path.is_file():
        return str(path)
    return None


def _terminal_evidence_path(script_path: str, status: str) -> str | None:
    package_dir = Path(script_path).resolve().parent
    mapping = {
        "submitted": "submission-confirmation.txt",
        "submit_clicked_unconfirmed": "submission-click-unconfirmed.txt",
        "email_verification_required": "email-verification-required.txt",
        "submission_processing_error": "submission-processing-error.txt",
        "autofill_timed_out": "execution-timeout.txt",
    }
    filename = mapping.get(status)
    if not filename:
        return None
    path = package_dir / filename
    if path.is_file():
        return str(path)
    return None


def _clear_previous_terminal_evidence(script_path: str) -> None:
    package_dir = Path(script_path).resolve().parent
    for filename in TERMINAL_EVIDENCE_FILENAMES:
        path = package_dir / filename
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            continue


def _timeout_stream_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _redact_timeout_line(line: str) -> str:
    redacted = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email-redacted>", line)
    redacted = re.sub(
        r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
        "<phone-redacted>",
        redacted,
    )
    return redacted[:500]


def _timeout_evidence_lines(stdout: str, stderr: str, *, max_lines: int = 80) -> list[str]:
    selected: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line and line.startswith(TIMEOUT_EVIDENCE_PREFIXES):
            selected.append(_redact_timeout_line(line))
    if stderr.strip():
        selected.append(f"stderr_present: {len(stderr.splitlines())} line(s) captured but omitted")
    return selected[-max_lines:]


def _write_timeout_evidence(
    script_path: str,
    timeout_seconds: int,
    exc: subprocess.TimeoutExpired,
) -> str | None:
    package_dir = Path(script_path).resolve().parent
    path = package_dir / "execution-timeout.txt"
    stdout = _timeout_stream_text(exc.stdout)
    stderr = _timeout_stream_text(exc.stderr)
    lines = [
        "status: autofill_timed_out",
        f"timeout_seconds: {timeout_seconds}",
        f"script: {Path(script_path).name}",
        "",
        "safe_runtime_trace:",
    ]
    evidence_lines = _timeout_evidence_lines(stdout, stderr)
    lines.extend(evidence_lines or ["not available"])
    try:
        path.write_text("\n".join(lines).rstrip() + "\n")
    except OSError:
        return None
    return str(path)


def _cleanup_generated_resume_files(
    item: dict[str, Any], script_path: str
) -> tuple[list[str], list[str]]:
    """Remove package-local generated resume variants after confirmation."""
    package_dir = Path(script_path).resolve().parent
    deleted_files: list[str] = []
    cleanup_errors: list[str] = []
    seen: set[Path] = set()
    for key in ("tailored_resume_path", "upload_resume_path", "upload_resume_docx_path"):
        raw_path = item.get(key)
        if not raw_path:
            continue
        try:
            candidate = Path(str(raw_path)).resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.parent != package_dir or candidate.stem != "tailored-resume":
            continue
        if candidate.suffix.lower() not in {".md", ".docx", ".pdf"}:
            continue
        try:
            if candidate.is_file():
                candidate.unlink()
                deleted_files.append(str(candidate))
        except OSError as exc:
            cleanup_errors.append(f"{candidate.name}: {type(exc).__name__}")
    return deleted_files, cleanup_errors


def _node_playwright_missing(result: subprocess.CompletedProcess) -> bool:
    if result.returncode == 0:
        return False
    combined = f"{result.stdout or ''}\n{result.stderr or ''}"
    return "Cannot find module 'playwright'" in combined or "MODULE_NOT_FOUND" in combined


def _script_requires_node_playwright(script_path: str) -> bool:
    try:
        head = Path(script_path).read_text()[:2048]
    except OSError:
        return False
    return 'require("playwright")' in head or "require('playwright')" in head


def _node_playwright_available_for(node_binary: str, script_path: str) -> bool:
    try:
        result = subprocess.run(
            [node_binary, "-e", "require.resolve('playwright')"],
            cwd=str(Path(script_path).parent),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run_python_runtime_streaming(
    script_path: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess:
    """Run the generated runtime through the package's Python Playwright path."""
    return _run_script_streaming(
        [sys.executable, "-u", "-m", "job_agent.python_runtime", script_path],
        timeout_seconds,
    )


class _TeeText(io.StringIO):
    """Capture runtime output while preserving the visible CLI stream."""

    def __init__(self, sink) -> None:
        super().__init__()
        self._sink = sink

    def write(self, value: str) -> int:
        self._sink.write(value)
        self._sink.flush()
        return super().write(value)


def _wall_clock_runtime_deadline(timeout_seconds: int | None) -> int | None:
    """Give in-process runtimes a slightly larger wall-clock safety margin."""
    if timeout_seconds is None or timeout_seconds <= 0:
        return None
    return timeout_seconds + 60


def _run_python_runtime_in_process(
    script_path: str,
    *,
    runtime_env: Mapping[str, str] | None,
    action_runner: RuntimeActionRunner,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess:
    """Run Playwright in-process so the original Agent Core owns live actions."""
    payload = load_runtime_payload(script_path)
    # ``run_runtime_script`` normally injects this non-serialized runtime
    # value.  The Agent Core in-process path bypasses that wrapper, so it must
    # provide the package directory itself; otherwise terminal screenshots and
    # confirmation/processing evidence silently have nowhere to be written.
    payload["_runtimeScriptDir"] = str(Path(script_path).resolve().parent)
    stdout = _TeeText(sys.stdout)
    stderr = _TeeText(sys.stderr)
    prior_headless = os.environ.get("BROWSER_HEADLESS")
    previous_alarm_handler: Any = None
    deadline_set = False
    if (
        timeout_seconds is not None
        and timeout_seconds > 0
        and hasattr(signal, "alarm")
        and threading.current_thread() is threading.main_thread()
    ):
        previous_alarm_handler = signal.getsignal(signal.SIGALRM)

        def _raise_runtime_timeout(signum: int, frame: Any) -> None:
            raise subprocess.TimeoutExpired(
                ["python_runtime_in_process", script_path],
                timeout_seconds,
            )

        signal.signal(signal.SIGALRM, _raise_runtime_timeout)
        signal.alarm(timeout_seconds)
        deadline_set = True
    watchdog_deadline_seconds = _wall_clock_runtime_deadline(timeout_seconds)
    try:
        if runtime_env is not None and "BROWSER_HEADLESS" in runtime_env:
            os.environ["BROWSER_HEADLESS"] = str(
                runtime_env["BROWSER_HEADLESS"]
            )
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = run_runtime_payload(
                payload,
                action_runner=action_runner,
                watchdog_deadline_seconds=watchdog_deadline_seconds,
            )
    finally:
        if deadline_set:
            signal.alarm(0)
            if previous_alarm_handler is not None:
                signal.signal(signal.SIGALRM, previous_alarm_handler)
        if prior_headless is None:
            os.environ.pop("BROWSER_HEADLESS", None)
        else:
            os.environ["BROWSER_HEADLESS"] = prior_headless
    return subprocess.CompletedProcess(
        [sys.executable, "-m", "job_agent.python_runtime", script_path],
        return_code,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def _run_python_runtime(
    script_path: str,
    timeout_seconds: int,
    *,
    runtime_env: dict[str, str] | None,
) -> subprocess.CompletedProcess:
    if runtime_env is None:
        return _run_python_runtime_streaming(script_path, timeout_seconds)
    return _run_script_streaming(
        [sys.executable, "-u", "-m", "job_agent.python_runtime", script_path],
        timeout_seconds,
        env=runtime_env,
    )


def _run_runtime_command(
    command: list[str],
    timeout_seconds: int,
    *,
    runtime_env: dict[str, str] | None,
) -> subprocess.CompletedProcess:
    if runtime_env is None:
        return _run_script_streaming(command, timeout_seconds)
    return _run_script_streaming(command, timeout_seconds, env=runtime_env)


def _run_script_streaming(
    command: list[str],
    timeout_seconds: int,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run a script with visible output while retaining stdout for gate checks."""
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    if env is not None:
        popen_kwargs["env"] = env
    process = subprocess.Popen(command, **popen_kwargs)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _forward(stream, sink, chunks: list[str]) -> None:
        if stream is None:
            return
        for chunk in iter(stream.readline, ""):
            chunks.append(chunk)
            sink.write(chunk)
            sink.flush()
        stream.close()

    stdout_thread = threading.Thread(
        target=_forward,
        args=(process.stdout, sys.stdout, stdout_chunks),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_forward,
        args=(process.stderr, sys.stderr, stderr_chunks),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    def _drain_streams() -> None:
        for _ in range(3):
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            if not (stdout_thread.is_alive() or stderr_thread.is_alive()):
                return
            # A descendant can inherit the pipe after the runtime exits; kill
            # the whole session so the forwarding threads can finish.
            _terminate_process_tree(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    try:
        return_code = process.wait(timeout=timeout_seconds)
        _drain_streams()
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        process.wait()
        _drain_streams()
        exc.stdout = "".join(stdout_chunks)
        exc.stderr = "".join(stderr_chunks)
        raise exc
    return subprocess.CompletedProcess(
        command,
        return_code,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    process.kill()


def summarize_execution(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(records),
        "completed": sum(record["status"].startswith("autofill_completed") for record in records),
        "submitted": sum(record["status"] == "submitted" for record in records),
        "submit_clicked_unconfirmed": sum(record["status"] == "submit_clicked_unconfirmed" for record in records),
        "email_verification_required": sum(record["status"] == "email_verification_required" for record in records),
        "submission_processing_error": sum(record["status"] == "submission_processing_error" for record in records),
        "submission_blocked_by_anti_spam": sum(
            record["status"] == "submission_blocked_by_anti_spam" for record in records
        ),
        "candidate_account_required": sum(record["status"] == "candidate_account_required" for record in records),
        "failed": sum(record["status"] in {"autofill_failed", "autofill_timed_out"} for record in records),
        "skipped": sum(record["status"].startswith("skipped_") for record in records),
    }
