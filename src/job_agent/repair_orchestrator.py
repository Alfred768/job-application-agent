from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.9/3.10 compatibility
    import tomli as tomllib

from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.career.recovery import requires_approved_candidate_fact
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.core.contracts import (
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
from job_agent.agent_session import (
    DeterministicSessionLLM,
    latest_trajectory_observation,
)
from job_agent.llm_answer_resolver import match_screening_rule
from job_agent.python_runtime import load_runtime_payload
from job_agent.sensitive_kb import resolve_sensitive_answer


@dataclass(frozen=True)
class RepairPolicy:
    enabled: bool = False
    max_cycles: int = 1
    agent_binary: str = "codex"
    agent_timeout_seconds: int = 900
    verification_timeout_seconds: int = 1200
    combobox_no_progress_seconds: int = 20
    retry_after_verified_repair: bool = True


@dataclass(frozen=True)
class RepairAgentReadiness:
    ready: bool
    code: str
    message: str
    agent_path: str | None = None
    auth_mode: str | None = None


@dataclass(frozen=True)
class _RepairAgentRoute:
    provider_name: str = "openai"
    provider_env_key: str | None = None
    auth_mode: str = "default_openai"
    config_overrides: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


Runner = Callable[..., subprocess.CompletedProcess[str]]
Fingerprint = tuple[str, int, str] | None


def _run_with_process_group_timeout(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a command and tear down its full process tree on timeout.

    ``subprocess.run(..., timeout=...)`` kills only the immediate child.  The
    Codex launcher is a Node wrapper which starts a native grandchild; that
    grandchild can keep stdout/stderr pipes open forever after the wrapper is
    killed.  A dedicated process group lets the timeout own and reap the
    complete isolated repair command.
    """
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:  # pragma: no cover - Windows fallback
            process.terminate()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:  # pragma: no cover - Windows fallback
                process.kill()
            process.communicate()
        raise subprocess.TimeoutExpired(
            list(command),
            timeout,
            output=exc.output,
            stderr=exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(
        list(command),
        process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _run_isolated_command(
    runner: Runner,
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    if runner is subprocess.run:
        return _run_with_process_group_timeout(
            command,
            cwd=cwd,
            env=env,
            input=input,
            timeout=timeout,
        )
    return runner(
        list(command),
        cwd=cwd,
        env=env,
        input=input,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )

_REPAIRABLE_STATUSES = {
    "autofill_completed_blocked",
    "autofill_failed",
    "autofill_timed_out",
}
_EXCLUDED_STATUS_MARKERS = (
    "anti_spam",
    "captcha",
    "candidate_account",
    "email_verification",
    "submit_clicked",
    "submission_processing",
)
_MANUAL_REASON_MARKERS = (
    "needs saved answer",
    "manual selection",
    "profile has no approved",
    "user-authored",
    "truthfulness gate",
)
_GENERATED_PATH_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "output",
}
_COPY_DIRECTORIES = ("src", "tests", "examples", "scripts")
_COPY_FILES = (
    ".env.example",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
)
_COPY_SELECTED_FILES = (
    "docs/DAILY_APPLICATION_SOP.md",
    "docs/PROJECT_MAP.md",
    "ops/daily.example.json",
)
_ALLOWED_FILE_PATTERNS = (
    re.compile(r"^src/job_agent(?:/|$)"),
    re.compile(r"^tests(?:/|$)"),
    re.compile(r"^docs/(?:DAILY_APPLICATION_SOP|PROJECT_MAP)\.md$"),
    re.compile(r"^ops/daily\.example\.json$"),
    re.compile(r"^AGENTS\.md$"),
)
_COMBOBOX_FIELD_PATTERN = re.compile(
    r"Autofill field:\s*(?P<label>.+?)\s+\(combobox\)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_REPAIR_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
_AUTH_FAILURE_MARKERS = (
    "401 unauthorized",
    "incorrect api key",
    "invalid_api_key",
    "invalid_refresh_token",
    "token_expired",
    "provided authentication token is expired",
    "access token could not be refreshed",
    "could not validate your refresh token",
    "please log out and sign in again",
)
_CONFIG_FAILURE_MARKERS = (
    "failed to load models cache",
    "unknown configuration",
    "invalid configuration",
    "model is not supported",
)
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROVIDER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


@contextmanager
def _repair_temporary_directory(*, prefix: str):
    """Create an isolated directory and tolerate short-lived Codex write races."""
    temporary = Path(
        tempfile.mkdtemp(
            prefix=prefix,
            dir=_REPAIR_TEMP_ROOT,
        )
    )
    try:
        yield str(temporary)
    finally:
        for attempt in range(8):
            try:
                shutil.rmtree(temporary)
                break
            except FileNotFoundError:
                break
            except OSError:
                if attempt == 7:
                    shutil.rmtree(temporary, ignore_errors=True)
                    break
                time.sleep(0.05 * (attempt + 1))


def check_repair_agent_readiness(
    policy: RepairPolicy,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: int = 30,
) -> RepairAgentReadiness:
    """Verify the configured Codex binary with a read-only remote exec probe."""
    if not policy.enabled:
        return RepairAgentReadiness(
            ready=True,
            code="disabled",
            message="Automatic repair is disabled.",
        )
    configured = Path(policy.agent_binary).expanduser()
    discovered = (
        configured
        if configured.is_file() and os.access(configured, os.X_OK)
        else Path(shutil.which(policy.agent_binary) or "")
    )
    if not discovered or not discovered.is_file():
        return RepairAgentReadiness(
            ready=False,
            code="repair_agent_missing",
            message=f"Repair agent is unavailable: {policy.agent_binary}",
        )
    route = _repair_agent_route()
    if route.error_code:
        return RepairAgentReadiness(
            ready=False,
            code=route.error_code,
            message=route.error_message or "Repair-agent provider configuration is invalid.",
            agent_path=str(discovered),
        )
    explicit_codex_key = bool(str(os.environ.get("CODEX_API_KEY") or "").strip())
    projected_openai_key = (
        route.auth_mode == "default_openai"
        and not explicit_codex_key
        and bool(str(os.environ.get("OPENAI_API_KEY") or "").strip())
    )
    exec_env = _repair_agent_environment(for_exec=True, route=route)
    if route.provider_env_key and not exec_env.get(route.provider_env_key):
        return RepairAgentReadiness(
            ready=False,
            code="repair_agent_provider_key_missing",
            message=(
                "Repair-agent provider credential is unavailable in environment "
                f"variable {route.provider_env_key}."
            ),
            agent_path=str(discovered),
        )
    requires_login = route.auth_mode == "openai_login" or (
        route.auth_mode == "default_openai" and not exec_env.get("CODEX_API_KEY")
    )
    if requires_login:
        try:
            login_status = runner(
                [str(discovered), "login", "status"],
                cwd=Path.cwd(),
                env=_repair_agent_environment(route=route),
                capture_output=True,
                text=True,
                check=False,
                timeout=max(1, int(timeout_seconds)),
            )
        except subprocess.TimeoutExpired:
            return RepairAgentReadiness(
                ready=False,
                code="repair_agent_probe_timed_out",
                message="Repair-agent readiness check timed out.",
                agent_path=str(discovered),
            )
        except OSError:
            return RepairAgentReadiness(
                ready=False,
                code="repair_agent_probe_failed",
                message="Repair-agent readiness check could not start.",
                agent_path=str(discovered),
            )
        if login_status.returncode != 0:
            return _readiness_failure(discovered, login_status)

    try:
        with _repair_temporary_directory(
            prefix="job-agent-repair-readiness-",
        ) as temporary:
            if not requires_login:
                isolated_codex_home = Path(temporary) / "codex-home"
                isolated_codex_home.mkdir()
                exec_env["CODEX_HOME"] = str(isolated_codex_home)

            def run_probe(environment: Mapping[str, str]):
                return runner(
                    [
                        str(discovered),
                        "exec",
                        "--ephemeral",
                        "--ignore-user-config",
                        "--sandbox",
                        "read-only",
                        "--skip-git-repo-check",
                        *_repair_agent_override_args(route),
                        "-c",
                        "shell_environment_policy.inherit=none",
                        "-C",
                        temporary,
                        (
                            "Reply with exactly READY. Do not inspect files, call "
                            "tools, or change anything."
                        ),
                    ],
                    cwd=Path(temporary),
                    env=dict(environment),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=max(1, int(timeout_seconds)),
                )

            probe = run_probe(exec_env)
            if probe.returncode != 0 and projected_openai_key:
                # The project .env may contain an expired OpenAI API key while
                # the desktop Codex session still has a valid ChatGPT login.
                # Try that login exactly once, without carrying any API key
                # into the isolated probe or repair workspace.
                login_env = _repair_agent_environment(
                    route=route,
                    auth_mode="login",
                )
                login_status = runner(
                    [str(discovered), "login", "status"],
                    cwd=Path.cwd(),
                    env=login_env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=max(1, int(timeout_seconds)),
                )
                if login_status.returncode == 0:
                    probe = run_probe(login_env)
                    if probe.returncode == 0:
                        return RepairAgentReadiness(
                            ready=True,
                            code="ready",
                            message=(
                                "Repair agent completed a read-only remote readiness "
                                "probe using the machine-level ChatGPT login after "
                                "the configured API key was rejected."
                            ),
                            agent_path=str(discovered),
                            auth_mode="login",
                        )
    except subprocess.TimeoutExpired:
        return RepairAgentReadiness(
            ready=False,
            code="repair_agent_probe_timed_out",
            message="Repair-agent remote readiness probe timed out.",
            agent_path=str(discovered),
        )
    except OSError:
        return RepairAgentReadiness(
            ready=False,
            code="repair_agent_probe_failed",
            message="Repair-agent remote readiness probe could not start.",
            agent_path=str(discovered),
        )
    if probe.returncode != 0:
        return _readiness_failure(discovered, probe)
    return RepairAgentReadiness(
        ready=True,
        code="ready",
        message=(
            "Repair agent completed a read-only remote readiness probe using "
            f"provider {route.provider_name}."
        ),
        agent_path=str(discovered),
        auth_mode="login" if requires_login else "api_key",
    )


def _readiness_failure(
    discovered: Path,
    completed: subprocess.CompletedProcess[str],
) -> RepairAgentReadiness:
    combined = "\n".join(
        (
            str(getattr(completed, "stdout", "") or ""),
            str(getattr(completed, "stderr", "") or ""),
        )
    )
    _, reason, _ = _classify_agent_failure(combined)
    return RepairAgentReadiness(
        ready=False,
        code=reason,
        message=(
            "Repair-agent authentication is unavailable."
            if reason == "repair_agent_authentication_failed"
            else "Repair-agent readiness check failed."
        ),
        agent_path=str(discovered),
    )


def repair_result_consumes_cycle(result: Mapping[str, Any]) -> bool:
    """Return false for infrastructure failures that never reached code repair."""
    if repair_result_is_verified(result):
        return True
    result_status = str(result.get("status") or "")
    if result_status in {"agent_unavailable", "exhausted"}:
        return False
    # Once the isolated agent returned and trusted verification actually ran,
    # the cycle was consumed even if the agent's prose or diff happens to
    # contain words such as "rate limit".  Classifying arbitrary agent output
    # as infrastructure would strand a real verification failure as
    # repair_unavailable and skip the remaining bounded cycles.
    if result_status == "verification_failed":
        return True
    reason = str(result.get("reason") or "")
    if reason.startswith("repair_agent_") and reason.endswith(
        (
            "authentication_failed",
            "configuration_failed",
            "network_unavailable",
            "rate_limited",
        )
    ):
        return False
    combined = "\n".join(
        (
            str(result.get("agent_stdout") or ""),
            str(result.get("agent_stderr") or ""),
        )
    )
    status, _, _ = _classify_agent_failure(combined)
    return status != "agent_unavailable"


def repair_result_is_verified(result: Mapping[str, Any]) -> bool:
    """Return true when code was promoted or the current code already passed verification."""
    return result.get("status") in {"promoted", "already_fixed_verified"}


def evaluate_repair_policy(request: Mapping[str, Any]) -> PolicyDecision:
    """Authorize the independent repair-agent branch before starting Codex."""
    constraints = request.get("constraints")
    if not isinstance(constraints, Mapping):
        constraints = {}
    findings = request.get("findings")
    statuses = [
        str(finding.get("status") or "").strip().lower()
        for finding in findings
        if isinstance(finding, Mapping)
    ] if isinstance(findings, list) else []
    statuses = statuses or ["autofill_failed"]
    gate = JobApplicationPolicyGate()
    short_term_memory = ShortTermMemory()
    long_term_memory = NullLongTermMemory()
    decision: PolicyDecision | None = None
    for status in statuses:
        decision = gate.evaluate(
            ToolCall(
                tool_name="codex_repair_agent",
                parameters={},
                effect=ToolEffect.REPAIR,
                purpose="Repair a sanitized field or runtime defect.",
                context={
                    "failure_status": status,
                    "isolated_workspace": True,
                    "offline_verification": True,
                    "real_browser_verification": bool(
                        constraints.get("real_browser_verification", False)
                    ),
                    "real_submission": bool(
                        constraints.get("real_submission", False)
                    ),
                },
            ),
            short_term_memory=short_term_memory,
            long_term_memory=long_term_memory,
        )
        if not decision.allowed:
            return decision
    assert decision is not None
    return decision


def build_repair_request(
    audit: Mapping[str, Any],
    *,
    run_dir: Path,
    cycle: int,
) -> dict[str, Any] | None:
    """Build a sanitized request only for failures that code can plausibly repair."""
    findings: list[dict[str, Any]] = []
    retry_targets: list[dict[str, str]] = []
    applications = audit.get("applications")
    if not isinstance(applications, list):
        return None

    for raw_application in applications:
        if not isinstance(raw_application, Mapping):
            continue
        status = str(raw_application.get("status") or "").strip().lower()
        if (
            status not in _REPAIRABLE_STATUSES
            or any(marker in status for marker in _EXCLUDED_STATUS_MARKERS)
        ):
            continue

        fingerprints: list[dict[str, Any]] = []
        sanitized_reviews: list[dict[str, Any]] = []
        unresolved_candidate_fact = False
        review_items = raw_application.get("review_items")
        if isinstance(review_items, list):
            for raw_item in review_items:
                if not isinstance(raw_item, Mapping):
                    continue
                reason = _compact_text(raw_item.get("reason"))
                label = _compact_text(raw_item.get("label")) or "unlabeled field"
                normalized_reason = reason.lower()
                candidate_fact = requires_approved_candidate_fact(raw_item)
                approved_answer = _prior_package_approved_answer(
                    raw_application,
                    raw_item,
                    run_dir=run_dir,
                )
                approved_fact_mapping_failure = bool(
                    candidate_fact
                    and approved_answer is not None
                    and _approved_answer_is_compatible_with_control_failure(
                        raw_item,
                        approved_answer,
                    )
                    and _is_control_mapping_failure(normalized_reason)
                )
                if (
                    raw_item.get("blocking", True)
                    and candidate_fact
                    and not approved_fact_mapping_failure
                ):
                    unresolved_candidate_fact = True
                if (
                    not raw_item.get("blocking", True)
                    or (candidate_fact and not approved_fact_mapping_failure)
                    or (
                        any(
                            marker in normalized_reason
                            for marker in _MANUAL_REASON_MARKERS
                        )
                        and not _is_control_mapping_failure(normalized_reason)
                    )
                ):
                    continue
                item_fingerprints = _review_fingerprints(label, reason)
                if approved_fact_mapping_failure and not item_fingerprints:
                    item_fingerprints = [
                        {
                            "code": "approved_fact_control_mapping_failure",
                            "field_label": label,
                        }
                    ]
                if not item_fingerprints:
                    continue
                sanitized_reviews.append(
                    {
                        "label": label,
                        "reason": reason,
                        "blocking": True,
                    }
                )
                fingerprints.extend(item_fingerprints)

        repeated_fields = _repeated_combobox_fields(
            raw_application.get("evidence"),
            run_dir=run_dir,
        )
        if repeated_fields:
            fingerprints.append(
                {
                    "code": "combobox_no_progress_timeout",
                    "field_labels": repeated_fields,
                }
            )

        fingerprints = _deduplicate_fingerprints(fingerprints)
        if not fingerprints:
            continue

        company = _compact_text(raw_application.get("company")) or "unknown"
        title = _compact_text(raw_application.get("title")) or "unknown"
        package_dir = _application_package_dir(raw_application, run_dir=run_dir)
        finding: dict[str, Any] = {
            "company": company,
            "title": title,
            "status": status,
            "fingerprints": fingerprints,
            "review_items": sanitized_reviews,
            "repeated_fields": repeated_fields,
        }
        agent_runtime_id = _compact_text(
            raw_application.get("agent_runtime_id")
        )
        if agent_runtime_id:
            finding["agent_runtime_id"] = agent_runtime_id
        if package_dir is not None:
            finding["package_dir"] = str(package_dir)
        if unresolved_candidate_fact:
            finding["retry_withheld"] = True
            finding["retry_withheld_reason"] = "unresolved_candidate_fact"
        findings.append(finding)

        target = {"company": company, "title": title}
        if agent_runtime_id:
            target["agent_runtime_id"] = agent_runtime_id
        if package_dir is not None:
            target["package_dir"] = str(package_dir)
        if not unresolved_candidate_fact:
            retry_targets.append(target)

    if not findings:
        return None
    return {
        "schema_version": 1,
        "cycle": max(1, int(cycle)),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "findings": findings,
        "retry_targets": retry_targets,
        "constraints": {
            "real_browser_verification": False,
            "real_submission": False,
            "network_access_for_shell_commands": False,
            "approved_paths_only": True,
        },
    }


def _run_repair_cycle_direct(
    policy: RepairPolicy,
    *,
    root: Path,
    run_dir: Path,
    request: Mapping[str, Any],
    agent_runner: Runner = subprocess.run,
    verification_runner: Runner = subprocess.run,
    verification_commands: Sequence[Sequence[str]] | None = None,
    defer_promotion: bool = False,
    auth_mode: str | None = None,
) -> dict[str, Any]:
    """Run one coding repair in an isolated copy and promote verified changes."""
    root = root.resolve()
    run_dir = run_dir.resolve()
    cycle = max(1, int(request.get("cycle", 1) or 1))
    budget_cycle = max(
        1,
        int(request.get("budget_cycle", cycle) or cycle),
    )
    attempt = max(1, int(request.get("attempt", cycle) or cycle))
    result_dir = run_dir / "repair"
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = (
        result_dir / f"repair-result-cycle-{cycle:02d}.json"
        if attempt == cycle
        else result_dir
        / f"repair-result-attempt-{attempt:02d}-cycle-{cycle:02d}.json"
    )
    architecture_decision = evaluate_repair_policy(request)

    def finish(status: str, **details: Any) -> dict[str, Any]:
        result = {
            "schema_version": 1,
            "cycle": cycle,
            "budget_cycle": budget_cycle,
            "attempt": attempt,
            "status": status,
            "changed_files": [],
            "disallowed_files": [],
            "reason": "",
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "architecture": {
                "effect": ToolEffect.REPAIR.value,
                "policy_decision": asdict(architecture_decision),
            },
            **details,
            "result_path": str(result_path),
        }
        _write_json(result_path, result)
        return result

    if not architecture_decision.allowed:
        return finish(
            "policy_denied",
            reason=(
                f"{architecture_decision.code}: "
                f"{architecture_decision.reason}"
            ),
        )
    if not policy.enabled:
        return finish("disabled", reason="automatic_repair_disabled")
    if budget_cycle > policy.max_cycles:
        return finish("exhausted", reason="maximum_repair_cycles_reached")
    if not policy.agent_binary.strip():
        return finish("agent_failed", reason="repair_agent_binary_is_empty")
    route = _repair_agent_route()
    if route.error_code:
        return finish(
            "agent_unavailable",
            reason=route.error_code,
            retryable=False,
            provider=route.provider_name,
        )
    agent_env = _repair_agent_environment(
        for_exec=True,
        route=route,
        auth_mode=auth_mode,
    )
    if route.provider_env_key and not agent_env.get(route.provider_env_key):
        return finish(
            "agent_unavailable",
            reason="repair_agent_provider_key_missing",
            retryable=False,
            provider=route.provider_name,
            provider_env_key=route.provider_env_key,
        )
    requires_login = auth_mode == "login" or route.auth_mode == "openai_login" or (
        route.auth_mode == "default_openai"
        and not agent_env.get("CODEX_API_KEY")
    )

    with _repair_temporary_directory(
        prefix=f"job-agent-repair-cycle-{cycle:02d}-",
    ) as temporary:
        if not requires_login:
            isolated_codex_home = Path(temporary) / "codex-home"
            isolated_codex_home.mkdir()
            agent_env["CODEX_HOME"] = str(isolated_codex_home)
        staging = Path(temporary) / "workspace"
        staging.mkdir()
        try:
            _copy_repair_workspace(root, staging)
        except OSError as exc:
            return finish("agent_failed", reason=f"workspace_copy_failed: {exc}")
        baseline_tests = Path(temporary) / "baseline-tests"
        if verification_commands is None:
            try:
                shutil.copytree(staging / "tests", baseline_tests)
            except OSError as exc:
                return finish(
                    "agent_failed",
                    reason=f"trusted_test_copy_failed: {exc}",
                )

        staging_baseline = _workspace_snapshot(staging)
        root_baseline = _selected_snapshot(root, staging_baseline)
        prompt = _repair_prompt(request)
        agent_command = [
            policy.agent_binary,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            *_repair_agent_override_args(route),
            "-c",
            "shell_environment_policy.inherit=none",
            "-C",
            str(staging),
            "-",
        ]
        try:
            agent_completed = _run_isolated_command(
                agent_runner,
                agent_command,
                cwd=staging,
                env=agent_env,
                input=prompt,
                timeout=policy.agent_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return finish("agent_timeout", reason="repair_agent_timed_out")
        except OSError as exc:
            return finish("agent_failed", reason=f"repair_agent_start_failed: {exc}")

        agent_stdout = _tail(getattr(agent_completed, "stdout", ""))
        agent_stderr = _tail(getattr(agent_completed, "stderr", ""))
        if agent_completed.returncode != 0:
            status, reason, retryable = _classify_agent_failure(
                "\n".join((agent_stdout, agent_stderr))
            )
            return finish(
                status,
                reason=reason,
                retryable=retryable,
                exit_code=agent_completed.returncode,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
            )

        unsafe_symlinks = _unsafe_symlinks(staging)
        if unsafe_symlinks:
            return finish(
                "rejected",
                reason="unsafe_symlink_changes",
                disallowed_files=unsafe_symlinks,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
            )

        staging_after = _workspace_snapshot(staging)
        changed_files = _changed_paths(staging_baseline, staging_after)
        disallowed_files = sorted(
            path for path in changed_files if not _is_allowed_change(path)
        )
        if disallowed_files:
            return finish(
                "rejected",
                reason="disallowed_file_changes",
                changed_files=changed_files,
                disallowed_files=disallowed_files,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
            )

        if verification_commands is None:
            trusted_project = Path(temporary) / "trusted-project"
            try:
                _copy_repair_workspace(staging, trusted_project)
                shutil.rmtree(trusted_project / "tests", ignore_errors=True)
                shutil.copytree(baseline_tests, trusted_project / "tests")
            except OSError as exc:
                return finish(
                    "verification_failed",
                    reason=f"trusted_verification_workspace_failed: {exc}",
                    changed_files=changed_files,
                    agent_stdout=agent_stdout,
                    agent_stderr=agent_stderr,
                )
            commands = _default_verification_commands(staging, trusted_project)
        else:
            commands = tuple(verification_commands)
        verification_results: list[dict[str, Any]] = []
        verification_env = _verification_environment(staging)
        for raw_command in commands:
            command = [str(part) for part in raw_command]
            try:
                completed = _run_isolated_command(
                    verification_runner,
                    command,
                    cwd=staging,
                    env=verification_env,
                    timeout=policy.verification_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                verification_results.append(
                    {
                        "command": command,
                        "returncode": None,
                        "status": "timed_out",
                    }
                )
                return finish(
                    "verification_failed",
                    reason="verification_timed_out",
                    changed_files=changed_files,
                    verification=verification_results,
                    agent_stdout=agent_stdout,
                    agent_stderr=agent_stderr,
                )
            except OSError as exc:
                verification_results.append(
                    {
                        "command": command,
                        "returncode": None,
                        "status": "start_failed",
                        "stderr": str(exc),
                    }
                )
                return finish(
                    "verification_failed",
                    reason="verification_start_failed",
                    changed_files=changed_files,
                    verification=verification_results,
                    agent_stdout=agent_stdout,
                    agent_stderr=agent_stderr,
                )
            verification_results.append(
                {
                    "command": command,
                    "returncode": completed.returncode,
                    "status": "passed" if completed.returncode == 0 else "failed",
                    "stdout": _tail(getattr(completed, "stdout", "")),
                    "stderr": _tail(getattr(completed, "stderr", "")),
                }
            )
            if completed.returncode != 0:
                return finish(
                    "verification_failed",
                    reason="verification_command_failed",
                    changed_files=changed_files,
                    verification=verification_results,
                    agent_stdout=agent_stdout,
                    agent_stderr=agent_stderr,
                )

        if not changed_files:
            return finish(
                "already_fixed_verified",
                reason="repair_agent_made_no_changes_all_verification_passed",
                verification=verification_results,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
                policy=asdict(policy),
            )

        root_now = _selected_snapshot(root, changed_files)
        concurrent_changes = sorted(
            path
            for path in changed_files
            if root_baseline.get(path) != root_now.get(path)
        )
        if concurrent_changes:
            return finish(
                "rejected",
                reason="main_workspace_changed_during_repair",
                changed_files=changed_files,
                disallowed_files=concurrent_changes,
                verification=verification_results,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
            )

        if defer_promotion:
            candidate_dir = result_dir / f"candidate-cycle-{cycle:02d}"
            try:
                _store_deferred_candidate(
                    staging,
                    candidate_dir,
                    changed_files,
                    root_baseline,
                )
            except OSError as exc:
                return finish(
                    "promotion_failed",
                    reason=f"candidate_storage_failed: {exc}",
                    changed_files=changed_files,
                    verification=verification_results,
                    agent_stdout=agent_stdout,
                    agent_stderr=agent_stderr,
                )
            return finish(
                "verified_pending_promotion",
                reason="all_verification_passed_promotion_deferred",
                changed_files=changed_files,
                verification=verification_results,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
                policy=asdict(policy),
                candidate_dir=str(candidate_dir),
            )

        try:
            _promote_changes(staging, root, changed_files)
        except OSError as exc:
            return finish(
                "promotion_failed",
                reason=f"promotion_failed: {exc}",
                changed_files=changed_files,
                verification=verification_results,
                agent_stdout=agent_stdout,
                agent_stderr=agent_stderr,
            )

        return finish(
            "promoted",
            reason="all_verification_passed",
            changed_files=changed_files,
            verification=verification_results,
            agent_stdout=agent_stdout,
            agent_stderr=agent_stderr,
            policy=asdict(policy),
        )


class _CodexRepairTool(Tool):
    """Run one already-scoped isolated repair behind ControlledExecution."""

    def __init__(
        self,
        policy: RepairPolicy,
        *,
        root: Path,
        run_dir: Path,
        request: Mapping[str, Any],
        agent_runner: Runner,
        verification_runner: Runner,
        verification_commands: Sequence[Sequence[str]] | None,
        defer_promotion: bool,
        auth_mode: str | None,
    ) -> None:
        super().__init__(
            "codex_repair_agent",
            "Execute one isolated coding repair and offline verification cycle.",
            effect=ToolEffect.REPAIR,
        )
        self._policy = policy
        self._root = root
        self._run_dir = run_dir
        self._request = request
        self._agent_runner = agent_runner
        self._verification_runner = verification_runner
        self._verification_commands = verification_commands
        self._defer_promotion = defer_promotion
        self._auth_mode = auth_mode

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        return _run_repair_cycle_direct(
            self._policy,
            root=self._root,
            run_dir=self._run_dir,
            request=self._request,
            agent_runner=self._agent_runner,
            verification_runner=self._verification_runner,
            verification_commands=self._verification_commands,
            defer_promotion=self._defer_promotion,
            auth_mode=self._auth_mode,
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="cycle",
                type="integer",
                description="Bounded repair cycle number.",
            )
        ]


class _RepairResultObservationTool(Tool):
    def __init__(self, result: Mapping[str, Any]) -> None:
        super().__init__(
            "repair_result_observe",
            "Consume a verified or failed isolated repair result.",
            effect=ToolEffect.OBSERVE,
        )
        self._result = dict(result)

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": str(self._result.get("status") or "unknown"),
            "retry_ready": repair_result_is_verified(self._result),
            "retry_scope": "single_application",
        }

    def get_parameters(self) -> list[ToolParameter]:
        return []


def _repair_result_observation_loop(
    target: Mapping[str, Any],
    result: Mapping[str, Any],
):
    initial_observation = latest_trajectory_observation(
        target.get("package_dir"),
        exclude_stages={"repair", "evaluation"},
    )
    if initial_observation is None:
        return None
    registry = ToolRegistry()
    registry.register_tool(_RepairResultObservationTool(result))
    agent = JobApplicationAgent.resume_runtime(
        name="job-application-agent",
        llm=DeterministicSessionLLM(),
        initial_observation=initial_observation,
        agent_runtime_id=str(
            target.get("agent_runtime_id") or "repair-parent-agent"
        ),
        tool_registry=registry,
        long_term_memory=NullLongTermMemory(),
        policy_gate=JobApplicationPolicyGate(),
    )
    return agent.continue_with_tools(
        "Observe the isolated repair result for this application.",
        [
            ToolCall(
                tool_name="repair_result_observe",
                parameters={},
                effect=ToolEffect.OBSERVE,
                purpose=(
                    "Feed the isolated repair result back to the owning "
                    "application Agent."
                ),
                context={
                    "phase": "repair_result",
                    "retry_scope": "single_application",
                },
            )
        ],
        memory_query=(
            f"{target.get('company') or ''} "
            f"{target.get('title') or ''} repair"
        ).strip(),
    )


def run_repair_cycle(
    policy: RepairPolicy,
    *,
    root: Path,
    run_dir: Path,
    request: Mapping[str, Any],
    agent_runner: Runner = subprocess.run,
    verification_runner: Runner = subprocess.run,
    verification_commands: Sequence[Sequence[str]] | None = None,
    defer_promotion: bool = False,
    auth_mode: str | None = None,
) -> dict[str, Any]:
    """Run an isolated repair as a Policy-controlled Agent Core action."""
    findings = request.get("findings")
    statuses = [
        str(item.get("status") or "").strip().lower()
        for item in findings
        if isinstance(item, Mapping)
    ] if isinstance(findings, list) else []
    failure_status = statuses[0] if statuses else "autofill_failed"
    constraints = request.get("constraints")
    normalized_constraints = (
        dict(constraints) if isinstance(constraints, Mapping) else {}
    )
    cycle = max(1, int(request.get("cycle", 1) or 1))
    registry = ToolRegistry()
    registry.register_tool(
        _CodexRepairTool(
            policy,
            root=root,
            run_dir=run_dir,
            request=request,
            agent_runner=agent_runner,
            verification_runner=verification_runner,
            verification_commands=verification_commands,
            defer_promotion=defer_promotion,
            auth_mode=auth_mode,
        )
    )
    targets = request.get("retry_targets")
    primary_target = next(
        (
            item
            for item in targets
            if isinstance(item, Mapping)
        ),
        {},
    ) if isinstance(targets, list) else {}
    initial_observation = latest_trajectory_observation(
        primary_target.get("package_dir")
    ) or Observation(
        kind="repair_request",
        source="execution_audit",
        payload={
            "phase": "repair",
            "status": failure_status,
            "cycle": cycle,
        },
    )
    agent = JobApplicationAgent.resume_runtime(
        name="job-application-agent",
        llm=DeterministicSessionLLM(),
        initial_observation=initial_observation,
        agent_runtime_id=str(
            primary_target.get("agent_runtime_id")
            or "repair-parent-agent"
        ),
        tool_registry=registry,
        long_term_memory=NullLongTermMemory(),
        policy_gate=JobApplicationPolicyGate(),
    )
    core = agent.agent_core
    call = ToolCall(
        tool_name="codex_repair_agent",
        parameters={"cycle": cycle},
        effect=ToolEffect.REPAIR,
        purpose="Repair a sanitized field or runtime defect.",
        context={
            "failure_status": failure_status,
            "isolated_workspace": True,
            "offline_verification": True,
            "real_browser_verification": bool(
                normalized_constraints.get(
                    "real_browser_verification",
                    False,
                )
            ),
            "real_submission": bool(
                normalized_constraints.get("real_submission", False)
            ),
        },
    )
    loop_result = agent.continue_with_tools(
        "Repair one sanitized reproducible application defect.",
        [call],
        memory_query=(
            f"{primary_target.get('company') or ''} "
            f"{primary_target.get('title') or ''} repair"
        ).strip(),
    )
    tool_result = loop_result.results[0] if loop_result.results else None
    if tool_result is not None and isinstance(tool_result.output, Mapping):
        result = dict(tool_result.output)
    else:
        # A policy denial has no external effect, but the existing result
        # contract still needs a durable reason/result path.
        result = _run_repair_cycle_direct(
            policy,
            root=root,
            run_dir=run_dir,
            request=request,
            agent_runner=agent_runner,
            verification_runner=verification_runner,
            verification_commands=verification_commands,
            defer_promotion=defer_promotion,
            auth_mode=auth_mode,
        )
    result["agent_loop"] = agent_loop_result_to_dict(loop_result)
    result_path = Path(str(result.get("result_path") or ""))
    if result_path.is_file():
        _write_json(result_path, result)
    _append_repair_trajectories(
        request,
        result,
        run_dir=run_dir,
    )
    return result


def _append_repair_trajectories(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    run_dir: Path,
) -> None:
    targets = request.get("retry_targets")
    if not isinstance(targets, list):
        return
    for target_index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            continue
        target_loop = result.get("agent_loop") if target_index == 0 else None
        if target_index > 0:
            observed = _repair_result_observation_loop(target, result)
            if observed is not None:
                target_loop = agent_loop_result_to_dict(observed)
        stage = {
            "cycle": result.get("cycle"),
            "attempt": result.get("attempt"),
            "status": result.get("status"),
            "reason": result.get("reason"),
            "agent_loop": target_loop,
        }
        package_value = str(target.get("package_dir") or "")
        if not package_value:
            continue
        package_dir = Path(package_value)
        trajectory_path = package_dir / "agent-trajectory.json"
        try:
            package_dir.resolve().relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        if not trajectory_path.is_file():
            continue
        try:
            trajectory = json.loads(trajectory_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(trajectory, dict):
            continue
        stages = trajectory.setdefault("stages", {})
        if not isinstance(stages, dict):
            continue
        repair_stages = stages.setdefault("repair", [])
        if not isinstance(repair_stages, list):
            continue
        repair_stages.append(stage)
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


def promote_deferred_repair(
    *,
    root: Path,
    run_dir: Path,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Promote a verified candidate after live browser execution has stopped."""
    result_path = Path(str(result.get("result_path") or ""))
    candidate_dir = Path(str(result.get("candidate_dir") or ""))
    if (
        result.get("status") != "verified_pending_promotion"
        or not result_path.is_file()
        or not candidate_dir.is_dir()
    ):
        return {
            **dict(result),
            "status": "promotion_failed",
            "reason": "deferred_candidate_is_incomplete",
        }
    root = root.resolve()
    run_dir = run_dir.resolve()
    try:
        result_path.resolve().relative_to(run_dir)
        candidate_dir.resolve().relative_to(run_dir)
    except (OSError, ValueError):
        return {
            **dict(result),
            "status": "promotion_failed",
            "reason": "deferred_candidate_is_outside_run_directory",
        }
    manifest_path = candidate_dir / "candidate-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        manifest = None
    if not isinstance(manifest, Mapping):
        return {
            **dict(result),
            "status": "promotion_failed",
            "reason": "deferred_candidate_manifest_is_invalid",
        }
    changed_files = [
        str(item)
        for item in manifest.get("changed_files", [])
        if str(item).strip()
    ]
    raw_baseline = manifest.get("root_baseline")
    baseline = (
        {
            str(path): _fingerprint_from_json(value)
            for path, value in raw_baseline.items()
        }
        if isinstance(raw_baseline, Mapping)
        else {}
    )
    current = _selected_snapshot(root, changed_files)
    concurrent_changes = sorted(
        path
        for path in changed_files
        if baseline.get(path) != current.get(path)
    )
    if concurrent_changes:
        promoted = {
            **dict(result),
            "status": "rejected",
            "reason": "main_workspace_changed_before_deferred_promotion",
            "disallowed_files": concurrent_changes,
        }
        _write_json(result_path, promoted)
        return promoted
    try:
        _promote_changes(candidate_dir / "workspace", root, changed_files)
    except OSError as exc:
        promoted = {
            **dict(result),
            "status": "promotion_failed",
            "reason": f"promotion_failed: {exc}",
        }
        _write_json(result_path, promoted)
        return promoted
    promoted = {
        **dict(result),
        "status": "promoted",
        "reason": "all_verification_passed",
        "promoted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _write_json(result_path, promoted)
    return promoted


def write_retry_batch(
    batch_summary_path: Path,
    *,
    request: Mapping[str, Any],
    output_path: Path,
) -> Path | None:
    """Create a retry batch containing only applications tied to repaired findings."""
    try:
        batch = json.loads(batch_summary_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(batch, list):
        return None

    targets = request.get("retry_targets")
    if not isinstance(targets, list):
        return None
    package_dirs = {
        str(Path(str(item.get("package_dir"))).resolve())
        for item in targets
        if isinstance(item, Mapping) and item.get("package_dir")
    }
    names = {
        (
            _compact_text(item.get("company")).lower(),
            _compact_text(item.get("title")).lower(),
        )
        for item in targets
        if isinstance(item, Mapping)
    }
    original_statuses = {
        _repair_target_identity(item): _compact_text(item.get("status"))
        for item in request.get("findings", [])
        if isinstance(item, Mapping)
    }
    selected: list[Any] = []
    for item in batch:
        if not isinstance(item, Mapping):
            continue
        package_dir = str(Path(str(item.get("package_dir"))).resolve())
        name = (
            _compact_text(item.get("company")).lower(),
            _compact_text(item.get("title")).lower(),
        )
        if (package_dirs and package_dir in package_dirs) or name in names:
            selected_item = dict(item)
            original_status = original_statuses.get(
                (
                    package_dir,
                    name[0],
                    name[1],
                )
            ) or original_statuses.get(("", name[0], name[1]))
            selected_item.update(
                {
                    "retry": True,
                    "repair_verified": True,
                    "retry_scope": "single_application",
                    "repair_cycle": max(
                        1,
                        int(request.get("cycle", 1) or 1),
                    ),
                }
            )
            if original_status:
                selected_item["original_terminal_status"] = original_status
            selected.append(selected_item)
    if not selected:
        return None
    _write_json_list(output_path, selected)
    return output_path


def _repair_target_identity(
    item: Mapping[str, Any],
) -> tuple[str, str, str]:
    package_dir = (
        str(Path(str(item.get("package_dir"))).resolve())
        if item.get("package_dir")
        else ""
    )
    return (
        package_dir,
        _compact_text(item.get("company")).lower(),
        _compact_text(item.get("title")).lower(),
    )


def _review_fingerprints(label: str, reason: str) -> list[dict[str, Any]]:
    normalized_label = label.lower()
    normalized_reason = reason.lower()
    fingerprints: list[dict[str, Any]] = []
    if "no visible job-application form was found" in normalized_reason:
        fingerprints.append(
            {
                "code": "application_form_navigation_failure",
                "field_label": label,
            }
        )
    if normalized_reason in {
        "unmapped field",
        "unsupported field type",
    }:
        fingerprints.append(
            {
                "code": "unmapped_field_classification_gap",
                "field_label": label,
            }
        )
    is_combobox_failure = (
        "combobox" in normalized_reason
        or "dropdown selection readback" in normalized_reason
        or "dropdown selection could not be verified" in normalized_reason
    )
    country_like = "country" in normalized_label
    if country_like and is_combobox_failure:
        fingerprints.append(
            {
                "code": "country_combobox_commit_mismatch",
                "field_label": label,
            }
        )
    if "combobox made no progress" in normalized_reason:
        fingerprints.append(
            {
                "code": "combobox_no_progress_timeout",
                "field_label": label,
            }
        )
    elif is_combobox_failure:
        fingerprints.append(
            {
                "code": "combobox_option_or_commit_failure",
                "field_label": label,
            }
        )
    elif "checkbox" in normalized_reason and (
        "needs saved answer" in normalized_reason
        or "no checkbox option matches" in normalized_reason
        or "browser reports field as invalid" in normalized_reason
    ):
        fingerprints.append(
            {
                "code": "checkbox_option_or_commit_failure",
                "field_label": label,
            }
        )
    elif "browser reports field as invalid" in normalized_reason:
        fingerprints.append(
            {
                "code": "dynamic_field_validation_failure",
                "field_label": label,
            }
        )
    elif "no matching option" in normalized_reason:
        fingerprints.append(
            {
                "code": "option_mapping_failure",
                "field_label": label,
            }
        )
    elif normalized_reason.startswith("fill error:"):
        fingerprints.append(
            {
                "code": "runtime_fill_failure",
                "field_label": label,
            }
        )
    return fingerprints


def _is_control_mapping_failure(reason: str) -> bool:
    return any(
        marker in reason
        for marker in (
            "browser reports field as invalid",
            "checkbox",
            "combobox",
            "dropdown selection",
            "fill error:",
            "no matching option",
            "option mismatch",
        )
    )


def _is_reproducible_control_defect(reason: str) -> bool:
    return any(
        marker in reason
        for marker in (
            "adapter mapping",
            "made no progress",
            "selection readback",
            "selection could not be verified",
        )
    )


def _prior_package_has_approved_answer(
    application: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    run_dir: Path,
) -> bool:
    """Detect a persisted fact without copying its value into a repair request."""
    return _prior_package_approved_answer(
        application,
        item,
        run_dir=run_dir,
    ) is not None


def _prior_package_approved_answer(
    application: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    run_dir: Path,
) -> Any | None:
    """Return a persisted approved fact only inside the trusted main process."""
    script_value = str(application.get("script_path") or "").strip()
    if script_value:
        script_path = Path(script_value)
    else:
        package_value = str(application.get("package_dir") or "").strip()
        if not package_value:
            return None
        script_path = Path(package_value) / "autofill-runtime.js"
    if not script_path.is_absolute():
        script_path = run_dir / script_path
    try:
        resolved = script_path.resolve()
        resolved.relative_to(run_dir.resolve())
        payload = load_runtime_payload(resolved)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    profile = payload.get("profile")
    if not isinstance(profile, Mapping):
        return None
    label = str(item.get("label") or "").strip()
    if not label:
        return None
    if bool(item.get("sensitive")):
        answer = resolve_sensitive_answer(label, dict(profile))
        if answer is not None and str(answer).strip():
            return answer
    answers = profile.get("answers")
    normalized_label = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
    if isinstance(answers, Mapping):
        for key, value in answers.items():
            normalized_key = re.sub(
                r"[^a-z0-9]+",
                " ",
                str(key).casefold(),
            ).strip()
            if normalized_key == normalized_label and str(value).strip():
                return value
    return match_screening_rule(
        label,
        profile.get("screening_answer_rules"),
    )


def _approved_answer_is_compatible_with_control_failure(
    item: Mapping[str, Any],
    answer: Any,
) -> bool:
    """Distinguish a commit defect from an incompatible saved candidate fact."""
    reason = str(item.get("reason") or "")
    marker = re.search(r"available options:\s*(.+)$", reason, flags=re.IGNORECASE)
    if marker is None:
        return True
    available = re.sub(r"[^a-z0-9]+", " ", marker.group(1).casefold()).strip()
    wanted = re.sub(r"[^a-z0-9]+", " ", str(answer).casefold()).strip()
    if not available or not wanted:
        return False
    if wanted in available:
        return True
    binary_available = bool(
        re.search(r"(?:^|\s)yes(?:\s|$)", available)
        and re.search(r"(?:^|\s)no(?:\s|$)", available)
    )
    if binary_available:
        positive_prefixes = (
            "yes", "true", "i agree", "i acknowledge", "i have ",
            "i am ", "i can ", "i do ", "i require", "i will require",
        )
        negative_prefixes = (
            "no", "false", "i do not", "i have not", "i am not",
            "i cannot", "never",
        )
        raw = str(answer).strip().casefold()
        if raw.startswith((*positive_prefixes, *negative_prefixes)):
            return True
    if wanted in {"not applicable", "n a", "none"} and "none of the above" in available:
        return True
    return False


def _repeated_combobox_fields(value: Any, *, run_dir: Path) -> list[str]:
    evidence_path = _safe_evidence_path(value, run_dir=run_dir)
    if evidence_path is None:
        return []
    try:
        text = evidence_path.read_text(errors="replace")
    except OSError:
        return []
    labels = [_compact_text(match.group("label")) for match in _COMBOBOX_FIELD_PATTERN.finditer(text)]
    counts = Counter(label for label in labels if label)
    return list(dict.fromkeys(label for label in labels if counts[label] >= 2))


def _safe_evidence_path(value: Any, *, run_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = run_dir / path
    try:
        resolved = path.resolve()
        resolved.relative_to(run_dir.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _application_package_dir(
    application: Mapping[str, Any],
    *,
    run_dir: Path,
) -> Path | None:
    for key in ("script_path", "evidence"):
        value = application.get(key)
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            path = run_dir / path
        try:
            parent = path.resolve().parent
            parent.relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        return parent
    return None


def _deduplicate_fingerprints(
    fingerprints: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fingerprint in fingerprints:
        serialized = json.dumps(dict(fingerprint), sort_keys=True, ensure_ascii=True)
        if serialized in seen:
            continue
        seen.add(serialized)
        result.append(dict(fingerprint))
    return result


def _copy_repair_workspace(root: Path, staging: Path) -> None:
    for name in _COPY_DIRECTORIES:
        source = root / name
        if source.is_dir():
            shutil.copytree(
                source,
                staging / name,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".mypy_cache",
                    ".pytest_cache",
                    ".ruff_cache",
                    "__pycache__",
                    "*.pyc",
                    "output",
                ),
            )
    for name in _COPY_FILES:
        source = root / name
        if source.is_file():
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    for name in _COPY_SELECTED_FILES:
        source = root / name
        if source.is_file():
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _workspace_snapshot(root: Path) -> dict[str, Fingerprint]:
    snapshot: dict[str, Fingerprint] = {}
    if not root.is_dir():
        return snapshot
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _is_generated_path(relative) or path.is_dir():
            continue
        if path.is_symlink():
            snapshot[relative] = ("symlink", 0, os.readlink(path))
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[relative] = ("file", path.stat().st_mode & 0o777, digest)
    return snapshot


def _selected_snapshot(
    root: Path,
    relative_paths: Sequence[str] | Mapping[str, Any],
) -> dict[str, Fingerprint]:
    snapshot: dict[str, Fingerprint] = {}
    for relative in relative_paths:
        path = root / relative
        if path.is_symlink():
            snapshot[relative] = ("symlink", 0, os.readlink(path))
        elif path.is_file():
            snapshot[relative] = (
                "file",
                path.stat().st_mode & 0o777,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        else:
            snapshot[relative] = None
    return snapshot


def _is_generated_path(relative: str) -> bool:
    parts = Path(relative).parts
    return any(part in _GENERATED_PATH_PARTS or part.endswith(".pyc") for part in parts)


def _changed_paths(
    before: Mapping[str, Fingerprint],
    after: Mapping[str, Fingerprint],
) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _is_allowed_change(relative: str) -> bool:
    return any(pattern.match(relative) for pattern in _ALLOWED_FILE_PATTERNS)


def _unsafe_symlinks(root: Path) -> list[str]:
    unsafe: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() and not _is_generated_path(path.relative_to(root).as_posix()):
            unsafe.append(path.relative_to(root).as_posix())
    return sorted(unsafe)


def _default_verification_commands(
    staging: Path,
    trusted_project: Path,
) -> tuple[tuple[str, ...], ...]:
    python = sys.executable
    return (
        (
            python,
            "-m",
            "pytest",
            "-q",
            "tests/test_repair_orchestrator.py",
            "tests/test_python_runtime.py",
            "tests/test_daily_sop.py",
        ),
        (
            python,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(trusted_project / "pyproject.toml"),
            str(trusted_project / "tests"),
        ),
        (
            python,
            "-m",
            "job_agent.cli",
            "examples",
            "verify-offline",
            "--out-dir",
            str(staging / "output" / "offline-verify-auto-repair"),
        ),
    )


def _promote_changes(staging: Path, root: Path, changed_files: Sequence[str]) -> None:
    backup_root = Path(tempfile.mkdtemp(prefix="job-agent-repair-backup-"))
    applied: list[str] = []
    try:
        for relative in changed_files:
            source = staging / relative
            destination = root / relative
            backup = backup_root / relative
            if destination.is_file():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup)
            elif destination.exists():
                raise OSError(f"promotion target is not a regular file: {relative}")

            if source.is_file() and not source.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(
                    f".{destination.name}.repair-{os.getpid()}.tmp"
                )
                shutil.copy2(source, temporary)
                temporary.replace(destination)
            elif source.exists():
                raise OSError(f"repair source is not a regular file: {relative}")
            else:
                destination.unlink(missing_ok=True)
            applied.append(relative)
    except OSError:
        for relative in reversed(applied):
            destination = root / relative
            backup = backup_root / relative
            if backup.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, destination)
            else:
                destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(backup_root, ignore_errors=True)


def _store_deferred_candidate(
    staging: Path,
    candidate_dir: Path,
    changed_files: Sequence[str],
    root_baseline: Mapping[str, Fingerprint],
) -> None:
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)
    workspace = candidate_dir / "workspace"
    workspace.mkdir(parents=True)
    for relative in changed_files:
        source = staging / relative
        destination = workspace / relative
        if source.is_file() and not source.is_symlink():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        elif source.exists():
            raise OSError(f"candidate source is not a regular file: {relative}")
    _write_json(
        candidate_dir / "candidate-manifest.json",
        {
            "schema_version": 1,
            "changed_files": list(changed_files),
            "root_baseline": {
                path: _fingerprint_to_json(root_baseline.get(path))
                for path in changed_files
            },
        },
    )


def _fingerprint_to_json(value: Fingerprint) -> list[Any] | None:
    return list(value) if value is not None else None


def _fingerprint_from_json(value: Any) -> Fingerprint:
    if (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[0], str)
        and isinstance(value[1], int)
        and isinstance(value[2], str)
    ):
        return (value[0], value[1], value[2])
    return None


def _repair_prompt(request: Mapping[str, Any]) -> str:
    serialized = json.dumps(request, indent=2, ensure_ascii=True, sort_keys=True)
    return (
        "Repair the job application agent failure described below in this isolated "
        "workspace. Work only from the supplied failure labels/reasons and repository "
        "tests. Do not use a real browser, real job site, network command, profile, "
        "resume, database, local daily configuration, or secret. Do not invent personal "
        "answers. You may modify only src/job_agent/, related tests, AGENTS.md, "
        "docs/DAILY_APPLICATION_SOP.md, docs/PROJECT_MAP.md, and "
        "ops/daily.example.json. Do not weaken or delete regression tests. Make the "
        "smallest product fix that addresses the fingerprints. First inspect whether "
        "the current code and existing tests already address every supplied fingerprint; "
        "if they do, do not edit files because an unchanged workspace can be independently "
        "verified as already fixed. Otherwise run focused offline "
        "tests, and finish after the code repair is ready for independent verification.\n\n"
        f"Repair request:\n{serialized}\n"
    )


def _repair_agent_route() -> _RepairAgentRoute:
    codex_home = Path(
        os.environ.get("CODEX_HOME")
        or (Path.home() / ".codex")
    ).expanduser()
    config_path = codex_home / "config.toml"
    if not config_path.is_file():
        return _RepairAgentRoute()
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return _RepairAgentRoute(
            error_code="repair_agent_configuration_failed",
            error_message="Repair-agent Codex configuration could not be parsed.",
        )
    if not isinstance(config, Mapping):
        return _RepairAgentRoute(
            error_code="repair_agent_configuration_failed",
            error_message="Repair-agent Codex configuration must be a TOML table.",
        )

    provider_name = str(config.get("model_provider") or "openai").strip()
    if not provider_name:
        provider_name = "openai"
    if not _PROVIDER_NAME_PATTERN.fullmatch(provider_name):
        return _RepairAgentRoute(
            provider_name=provider_name,
            error_code="repair_agent_configuration_failed",
            error_message="Repair-agent provider name contains unsupported characters.",
        )
    overrides: list[str] = []
    model = str(config.get("model") or "").strip()
    if model:
        overrides.append(_codex_config_override("model", model))

    if provider_name in {"openai", "ollama", "lmstudio"}:
        if provider_name != "openai":
            overrides.append(
                _codex_config_override("model_provider", provider_name)
            )
            return _RepairAgentRoute(
                provider_name=provider_name,
                auth_mode="none",
                config_overrides=tuple(overrides),
            )
        openai_base_url = str(config.get("openai_base_url") or "").strip()
        if openai_base_url:
            overrides.append(
                _codex_config_override("openai_base_url", openai_base_url)
            )
        return _RepairAgentRoute(
            provider_name=provider_name,
            config_overrides=tuple(overrides),
        )

    providers = config.get("model_providers")
    provider = (
        providers.get(provider_name)
        if isinstance(providers, Mapping)
        else None
    )
    if not isinstance(provider, Mapping):
        return _RepairAgentRoute(
            provider_name=provider_name,
            error_code="repair_agent_configuration_failed",
            error_message=(
                "Selected repair-agent provider has no matching "
                "model_providers configuration."
            ),
        )
    if provider.get("auth"):
        return _RepairAgentRoute(
            provider_name=provider_name,
            error_code="repair_agent_provider_projection_unsupported",
            error_message=(
                "Command-backed provider authentication is not projected into "
                "the isolated repair process; configure env_key instead."
            ),
        )

    provider_env_key = str(provider.get("env_key") or "").strip() or None
    if provider_env_key and not _ENVIRONMENT_VARIABLE_PATTERN.fullmatch(
        provider_env_key
    ):
        return _RepairAgentRoute(
            provider_name=provider_name,
            error_code="repair_agent_configuration_failed",
            error_message="Repair-agent provider env_key is not a valid variable name.",
        )
    requires_openai_auth = bool(provider.get("requires_openai_auth", False))
    if requires_openai_auth and provider_env_key:
        return _RepairAgentRoute(
            provider_name=provider_name,
            error_code="repair_agent_configuration_failed",
            error_message=(
                "Repair-agent provider cannot combine env_key with "
                "requires_openai_auth."
            ),
        )

    provider_key = f"model_providers.{_toml_key_segment(provider_name)}"
    overrides.append(_codex_config_override("model_provider", provider_name))
    for key in (
        "name",
        "base_url",
        "wire_api",
        "requires_openai_auth",
        "supports_websockets",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
    ):
        if key in provider:
            overrides.append(
                _codex_config_override(f"{provider_key}.{key}", provider[key])
            )
    if provider_env_key:
        overrides.append(
            _codex_config_override(
                f"{provider_key}.env_key",
                provider_env_key,
            )
        )
    if not str(provider.get("base_url") or "").strip():
        return _RepairAgentRoute(
            provider_name=provider_name,
            error_code="repair_agent_configuration_failed",
            error_message="Repair-agent custom provider has no base_url.",
        )

    if requires_openai_auth:
        auth_mode = "openai_login"
    elif provider_env_key:
        auth_mode = "provider_env"
    else:
        auth_mode = "none"
    return _RepairAgentRoute(
        provider_name=provider_name,
        provider_env_key=provider_env_key,
        auth_mode=auth_mode,
        config_overrides=tuple(overrides),
    )


def _toml_key_segment(value: str) -> str:
    return str(value)


def _codex_config_override(path: str, value: Any) -> str:
    if isinstance(value, bool):
        serialized = "true" if value else "false"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        serialized = str(value)
    else:
        serialized = json.dumps(str(value), ensure_ascii=True)
    return f"{path}={serialized}"


def _repair_agent_override_args(route: _RepairAgentRoute) -> list[str]:
    return [
        part
        for override in route.config_overrides
        for part in ("-c", override)
    ]


def _repair_agent_environment(
    *,
    for_exec: bool = False,
    route: _RepairAgentRoute | None = None,
    auth_mode: str | None = None,
) -> dict[str, str]:
    selected_route = route or _RepairAgentRoute()
    allowed = {
        "CODEX_HOME",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "OPENAI_API_KEY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("CODEX_")
    }
    if auth_mode == "login":
        env.pop("CODEX_ACCESS_TOKEN", None)
        env.pop("CODEX_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
    elif selected_route.auth_mode == "provider_env":
        for key in ("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"):
            if key != selected_route.provider_env_key:
                env.pop(key, None)
        if selected_route.provider_env_key:
            provider_key = str(
                os.environ.get(selected_route.provider_env_key) or ""
            ).strip()
            if provider_key:
                env[selected_route.provider_env_key] = provider_key
    elif selected_route.auth_mode in {"none", "openai_login"}:
        env.pop("CODEX_ACCESS_TOKEN", None)
        env.pop("CODEX_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
    elif for_exec and not env.get("CODEX_API_KEY"):
        api_key = str(env.get("OPENAI_API_KEY") or "").strip()
        if api_key:
            env["CODEX_API_KEY"] = api_key
    return env


def _classify_agent_failure(text: str) -> tuple[str, str, bool]:
    normalized = str(text or "").casefold()
    if any(marker in normalized for marker in _AUTH_FAILURE_MARKERS):
        return (
            "agent_unavailable",
            "repair_agent_authentication_failed",
            False,
        )
    if any(marker in normalized for marker in _CONFIG_FAILURE_MARKERS):
        return (
            "agent_unavailable",
            "repair_agent_configuration_failed",
            False,
        )
    if "429" in normalized or "rate limit" in normalized:
        return ("agent_unavailable", "repair_agent_rate_limited", True)
    if (
        "failed to connect" in normalized
        or "connection refused" in normalized
        or "network is unreachable" in normalized
    ):
        return ("agent_unavailable", "repair_agent_network_unavailable", True)
    return ("agent_failed", "repair_agent_nonzero_exit", True)


def _verification_environment(staging: Path) -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SHELL",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update(
        {
            "CAPMONSTER_SOLVE_CAPTCHA": "0",
            "PYTHONPATH": str(staging / "src"),
            "RESUME_SOURCE_DIR": "",
        }
    )
    return env


def _compact_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _tail(value: Any, *, limit: int = 20000) -> str:
    text = str(value or "")
    return text[-limit:]


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
    temporary.replace(path)


def _write_json_list(path: Path, payload: Sequence[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(list(payload), indent=2, ensure_ascii=True) + "\n")
    temporary.replace(path)
