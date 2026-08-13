from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.evaluation import (
    EvaluationPolicy,
    JobApplicationRoundEvaluator,
    evaluation_result_to_dict,
)
from hello_agents.career.recovery import (
    JobApplicationRecoveryPlanner,
    recovery_plan_to_dict,
    requires_approved_candidate_fact,
)
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.contracts import ToolCall, ToolEffect
from hello_agents.core.runtime import AgentCore
from hello_agents.core.trace import agent_loop_result_to_dict
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.registry import ToolRegistry
from job_agent.agent_session import (
    DeterministicSessionLLM,
    latest_trajectory_observation,
)
from job_agent.db import connect, export_application_ledger, init_db
from job_agent.gmail_verification import GmailVerificationError, check_gmail_token
from job_agent.recovery_executor import (
    execute_audit_recovery,
    write_recovery_retry_batch,
)
from job_agent.repair_orchestrator import (
    RepairPolicy,
    build_repair_request,
    check_repair_agent_readiness,
    promote_deferred_repair,
    repair_result_consumes_cycle,
    repair_result_is_verified,
    run_repair_cycle,
    write_retry_batch,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "ops" / "daily.local.json"
STATE_FILE_NAME = "run-state.json"
REPORT_FILE_NAME = "RUN_SUMMARY.md"
LATEST_FILE_NAME = "latest.json"
APPLICATION_LEDGER_FILE_NAME = "APPLICATION_LEDGER.csv"
HEARTBEAT_STATE_FILE_NAME = "heartbeat-state.json"
HEARTBEAT_MAX_NO_PROGRESS_BATCHES = 3
HEARTBEAT_NO_PROGRESS_PAUSE_MINUTES = 60
EVALUATION_METRICS_FILE_NAME = "evaluation-metrics.json"
RECOVERY_EXECUTION_FILE_NAME = "recovery-execution.json"
AGENT_RUNTIME_TRACE_FILE_NAME = "agent-runtime-trace.json"
ISSUE_COUNT_KEYS = (
    "completed",
    "submit_clicked_unconfirmed",
    "email_verification_required",
    "submission_processing_error",
    "submission_blocked_by_anti_spam",
    "candidate_account_required",
    "failed",
)
ENV_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
MANAGED_TEMP_DIR_NAME = ".tmp"
MANAGED_TEMP_PREFIX = "job-agent-"
MANAGED_TEMP_MARKER = ".job-agent-temp.json"
AUTO_CLEANUP_MIN_AGE_SECONDS = 24 * 60 * 60
TEMP_ENV_KEYS = ("TMPDIR", "TEMP", "TMP")


class SopError(RuntimeError):
    """Raised when a daily SOP stage cannot proceed safely."""


@dataclass(frozen=True)
class DailyConfig:
    config_path: Path
    root: Path
    source_config: Path
    profile: Path
    sensitive_kb: Path
    database: Path
    profile_vector_db: Path | None
    resume_source_dir: Path | None
    required_resume_pdf: Path | None
    output_root: Path
    min_score: int
    limit: int
    daily_submit_target: int
    timeout_seconds: int
    empty_wake_minutes: int
    use_llm: bool
    llm_answers: bool
    browser_headless: bool
    submit_complete: bool
    require_gmail_token: bool
    auto_repair: RepairPolicy
    evaluation: EvaluationPolicy

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        root: Path = PROJECT_ROOT,
        environ: Mapping[str, str] | None = None,
    ) -> "DailyConfig":
        if not path.is_absolute():
            path = root / path
        if not path.is_file():
            raise SopError(
                f"Daily config not found: {path}. "
                "Create it from ops/daily.example.json before running the SOP."
            )
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SopError(f"Cannot read daily config {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise SopError(f"Daily config must be a JSON object: {path}")
        return cls.from_mapping(
            payload,
            config_path=path,
            root=root,
            environ=environ,
        )

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        config_path: Path,
        root: Path,
        environ: Mapping[str, str] | None = None,
    ) -> "DailyConfig":
        schema_version = payload.get("schema_version", 1)
        if schema_version != 1:
            raise SopError(f"Unsupported daily config schema_version: {schema_version}")

        env = dict(os.environ if environ is None else environ)

        def required_path(name: str) -> Path:
            value = payload.get(name)
            if value is None or str(value).strip() == "":
                raise SopError(f"Daily config field '{name}' is required")
            return _resolve_path(value, name=name, root=root, environ=env)

        def optional_path(name: str) -> Path | None:
            value = payload.get(name)
            if value is None or str(value).strip() == "":
                return None
            return _resolve_path(value, name=name, root=root, environ=env)

        min_score = _read_int(payload, "min_score", default=70, minimum=0, maximum=100)
        limit = _read_int(payload, "limit", default=5, minimum=1, maximum=100)
        daily_submit_target = _read_int(
            payload,
            "daily_submit_target",
            default=limit,
            minimum=1,
            maximum=100,
        )
        timeout_seconds = _read_int(
            payload,
            "timeout_seconds",
            default=300,
            minimum=30,
            maximum=1800,
        )
        empty_wake_minutes = _read_int(
            payload,
            "empty_wake_minutes",
            default=15,
            minimum=1,
            maximum=1440,
        )
        resume_source_dir = optional_path("resume_source_dir")
        required_resume_pdf = optional_path("required_resume_pdf")
        if resume_source_dir is None and required_resume_pdf is None:
            raise SopError(
                "Daily config must set either 'resume_source_dir' or 'required_resume_pdf'"
            )
        auto_repair = _read_repair_policy(payload)
        evaluation = _read_evaluation_policy(payload)

        return cls(
            config_path=config_path.resolve(),
            root=root.resolve(),
            source_config=required_path("source_config"),
            profile=required_path("profile"),
            sensitive_kb=required_path("sensitive_kb"),
            database=required_path("database"),
            profile_vector_db=optional_path("profile_vector_db"),
            resume_source_dir=resume_source_dir,
            required_resume_pdf=required_resume_pdf,
            output_root=required_path("output_root"),
            min_score=min_score,
            limit=limit,
            daily_submit_target=daily_submit_target,
            timeout_seconds=timeout_seconds,
            empty_wake_minutes=empty_wake_minutes,
            use_llm=_read_bool(payload, "use_llm", default=True),
            llm_answers=_read_bool(payload, "llm_answers", default=True),
            browser_headless=_read_bool(payload, "browser_headless", default=True),
            submit_complete=_read_bool(payload, "submit_complete", default=False),
            require_gmail_token=_read_bool(
                payload,
                "require_gmail_token",
                default=False,
            ),
            auto_repair=auto_repair,
            evaluation=evaluation,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "source_config": str(self.source_config),
            "profile": str(self.profile),
            "sensitive_kb": str(self.sensitive_kb),
            "database": str(self.database),
            "profile_vector_db": (
                str(self.profile_vector_db) if self.profile_vector_db else None
            ),
            "resume_source_dir": (
                str(self.resume_source_dir) if self.resume_source_dir else None
            ),
            "required_resume_pdf": (
                str(self.required_resume_pdf) if self.required_resume_pdf else None
            ),
            "output_root": str(self.output_root),
            "min_score": self.min_score,
            "limit": self.limit,
            "daily_submit_target": self.daily_submit_target,
            "timeout_seconds": self.timeout_seconds,
            "empty_wake_minutes": self.empty_wake_minutes,
            "use_llm": self.use_llm,
            "llm_answers": self.llm_answers,
            "browser_headless": self.browser_headless,
            "submit_complete": self.submit_complete,
            "require_gmail_token": self.require_gmail_token,
            "evaluation": {
                "imported_cohort_target": self.evaluation.imported_cohort_target,
                "confirmation_rate_denominator": (
                    self.evaluation.confirmation_rate_denominator
                ),
                "min_confirmed_submission_rate": (
                    self.evaluation.min_confirmed_submission_rate
                ),
                "min_terminal_audit_coverage": (
                    self.evaluation.min_terminal_audit_coverage
                ),
            },
            "auto_repair": {
                "enabled": self.auto_repair.enabled,
                "max_cycles": self.auto_repair.max_cycles,
                "agent_binary": self.auto_repair.agent_binary,
                "agent_timeout_seconds": self.auto_repair.agent_timeout_seconds,
                "verification_timeout_seconds": (
                    self.auto_repair.verification_timeout_seconds
                ),
                "combobox_no_progress_seconds": (
                    self.auto_repair.combobox_no_progress_seconds
                ),
                "retry_after_verified_repair": (
                    self.auto_repair.retry_after_verified_repair
                ),
            },
        }


@dataclass(frozen=True)
class PreflightCheck:
    level: str
    name: str
    message: str


@dataclass(frozen=True)
class PreflightReport:
    checks: tuple[PreflightCheck, ...]

    @property
    def ok(self) -> bool:
        return not any(check.level == "ERROR" for check in self.checks)

    @property
    def error_count(self) -> int:
        return sum(check.level == "ERROR" for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(check.level == "WARN" for check in self.checks)


@dataclass(frozen=True)
class TempCleanupReport:
    removed_count: int
    skipped_active_count: int
    skipped_recent_count: int
    skipped_unmanaged_count: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class IncrementalRepairOutcome:
    request: Mapping[str, Any]
    request_path: Path
    result: Mapping[str, Any]


def load_env(path: Path) -> dict[str, str]:
    """Load simple KEY=VALUE entries without overriding the process environment."""
    if not path.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def run_preflight(
    config: DailyConfig,
    *,
    check_runtime: bool = True,
) -> PreflightReport:
    checks: list[PreflightCheck] = []

    source_payload = _check_json_file(
        checks,
        "source config",
        config.source_config,
        expected_type=dict,
    )
    if isinstance(source_payload, dict):
        sources = source_payload.get("sources")
        if not isinstance(sources, list) or not sources:
            _add(checks, "ERROR", "source config", "The sources list is empty or invalid")
        else:
            invalid = [
                index
                for index, source in enumerate(sources)
                if not isinstance(source, dict) or not source.get("type")
            ]
            if invalid:
                _add(
                    checks,
                    "ERROR",
                    "source config",
                    f"Source entries without a type: {invalid}",
                )
            else:
                _add(
                    checks,
                    "PASS",
                    "source config",
                    f"{len(sources)} configured sources",
                )

    profile_payload = _check_json_file(
        checks,
        "profile",
        config.profile,
        expected_type=dict,
    )
    if isinstance(profile_payload, dict):
        missing_profile_fields = [
            field
            for field in ("name", "email", "phone")
            if _is_placeholder(profile_payload.get(field))
        ]
        if missing_profile_fields:
            _add(
                checks,
                "ERROR",
                "profile",
                "Missing approved values for: " + ", ".join(missing_profile_fields),
            )
        else:
            _add(checks, "PASS", "profile", "Core identity fields are populated")

    sensitive_payload = _check_json_file(
        checks,
        "sensitive answers",
        config.sensitive_kb,
        expected_type=dict,
    )
    if isinstance(sensitive_payload, dict):
        approved_count = sum(
            not _is_placeholder(value) for value in sensitive_payload.values()
        )
        if approved_count == 0:
            _add(
                checks,
                "ERROR",
                "sensitive answers",
                "No approved sensitive answers are available",
            )
        else:
            _add(
                checks,
                "PASS",
                "sensitive answers",
                f"{approved_count} non-placeholder entries",
            )

    if config.required_resume_pdf is not None:
        resume = config.required_resume_pdf
        if not resume.is_file():
            _add(checks, "ERROR", "resume", "Required resume PDF does not exist")
        elif resume.suffix.lower() != ".pdf" or resume.stat().st_size == 0:
            _add(checks, "ERROR", "resume", "Required resume must be a non-empty PDF")
        else:
            _add(checks, "PASS", "resume", "Required resume PDF is readable")
    elif config.resume_source_dir is not None:
        directory = config.resume_source_dir
        if not directory.is_dir():
            _add(checks, "ERROR", "resume directory", "Resume directory does not exist")
        else:
            pdfs = [
                path
                for path in directory.rglob("*")
                if path.is_file()
                and path.suffix.lower() == ".pdf"
                and path.stat().st_size > 0
            ]
            if not pdfs:
                _add(
                    checks,
                    "ERROR",
                    "resume directory",
                    "No non-empty PDF resumes were found",
                )
            else:
                _add(
                    checks,
                    "PASS",
                    "resume directory",
                    f"{len(pdfs)} usable PDF resumes",
                )

    _check_database(checks, config.database)

    if config.profile_vector_db is not None:
        if config.profile_vector_db.is_file():
            _add(checks, "PASS", "profile vector DB", "Profile vector DB is available")
        else:
            _add(
                checks,
                "WARN",
                "profile vector DB",
                "Configured vector DB is missing; job-scoped retrieval may be unavailable",
            )

    if config.use_llm or config.llm_answers:
        if os.getenv("OPENAI_API_KEY"):
            _add(checks, "PASS", "LLM", "API credentials are configured")
        else:
            _add(
                checks,
                "ERROR",
                "LLM",
                "OPENAI_API_KEY is required by the selected daily settings",
            )

    gmail_token = Path(
        os.getenv(
            "JOB_AGENT_GMAIL_TOKEN_FILE",
            str(config.root / ".job-agent-secrets" / "gmail-token.json"),
        )
    ).expanduser()
    if gmail_token.is_file():
        _add(checks, "PASS", "Gmail verification", "OAuth token is available")
        try:
            check_gmail_token(str(gmail_token))
            _add(
                checks,
                "PASS",
                "Gmail verification",
                "OAuth token can refresh read-only access",
            )
        except GmailVerificationError as exc:
            _add(
                checks,
                "ERROR",
                "Gmail verification",
                f"OAuth token is invalid: {exc}",
            )
    elif config.require_gmail_token:
        _add(
            checks,
            "ERROR",
            "Gmail verification",
            "A Gmail OAuth token is required but missing",
        )
    else:
        _add(
            checks,
            "WARN",
            "Gmail verification",
            "No Gmail OAuth token found; email-code applications may stop",
        )

    if _env_bool("CAPMONSTER_SOLVE_CAPTCHA", default=False):
        if os.getenv("CAPMONSTER_API_KEY"):
            _add(checks, "PASS", "CAPTCHA", "Configured solver credentials are present")
        else:
            _add(
                checks,
                "ERROR",
                "CAPTCHA",
                "CAPMONSTER_SOLVE_CAPTCHA is enabled without CAPMONSTER_API_KEY",
            )

    cli_path = find_job_agent_cli(config.root)
    if cli_path is None:
        _add(
            checks,
            "ERROR",
            "job-agent CLI",
            "CLI not found; install the project into .venv first",
        )
    else:
        _add(checks, "PASS", "job-agent CLI", f"Using {cli_path}")

    if config.auto_repair.enabled:
        readiness = check_repair_agent_readiness(
            config.auto_repair,
        ) if check_runtime else None
        configured_agent = Path(config.auto_repair.agent_binary).expanduser()
        discovered_agent = (
            configured_agent
            if configured_agent.is_file() and os.access(configured_agent, os.X_OK)
            else Path(shutil.which(config.auto_repair.agent_binary) or "")
        )
        if readiness is not None and readiness.ready:
            _add(
                checks,
                "PASS",
                "automatic repair",
                readiness.message,
            )
        elif readiness is not None:
            _add(
                checks,
                "WARN",
                "automatic repair",
                (
                    f"{readiness.code}: {readiness.message} "
                    "Application execution may continue, but repairable defects "
                    "will remain retained as repair_unavailable."
                ),
            )
        elif discovered_agent and discovered_agent.is_file():
            _add(
                checks,
                "PASS",
                "automatic repair",
                f"Repair agent is available at {discovered_agent}",
            )
        else:
            _add(
                checks,
                "WARN",
                "automatic repair",
                (
                    "Repair agent is unavailable: "
                    f"{config.auto_repair.agent_binary}. Repairable defects will "
                    "be retained without stopping unrelated applications."
                ),
            )

    if check_runtime:
        _check_playwright(checks)

    active_attempts = list(config.output_root.glob("**/execution-attempt.json"))
    if active_attempts:
        _add(
            checks,
            "WARN",
            "unfinished attempts",
            f"{len(active_attempts)} prior execution attempt marker(s) need review",
        )

    output_parent = _nearest_existing_parent(config.output_root)
    if os.access(output_parent, os.W_OK):
        _add(checks, "PASS", "output", "Daily output location is writable")
    else:
        _add(checks, "ERROR", "output", "Daily output location is not writable")

    return PreflightReport(tuple(checks))


def print_preflight(report: PreflightReport) -> None:
    labels = {"PASS": "OK", "WARN": "WARN", "ERROR": "ERROR"}
    for check in report.checks:
        print(f"[{labels[check.level]}] {check.name}: {check.message}")
    summary = (
        "PREFLIGHT PASSED"
        if report.ok
        else f"PREFLIGHT FAILED ({report.error_count} error(s))"
    )
    if report.warning_count:
        summary += f", {report.warning_count} warning(s)"
    print(summary)


def refresh_application_ledger(config: DailyConfig) -> tuple[Path, int]:
    ledger_path = config.output_root / APPLICATION_LEDGER_FILE_NAME
    connection = connect(config.database)
    try:
        init_db(connection)
        row_count = export_application_ledger(connection, ledger_path)
    except sqlite3.DatabaseError as exc:
        raise SopError(f"Cannot refresh application ledger: {exc}") from exc
    finally:
        connection.close()
    return ledger_path, row_count


def daily_submission_progress(
    config: DailyConfig,
    *,
    now: datetime | None = None,
    raw_imported: int | None = None,
) -> dict[str, object]:
    local_now = (now or datetime.now().astimezone()).astimezone()
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = local_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    connection = connect(config.database)
    try:
        init_db(connection)
        row = connection.execute(
            """
            select count(*) as count
            from applications
            where submitted_at is not null
              and submitted_at >= ?
              and submitted_at < ?
            """,
            (start_utc, end_utc),
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise SopError(f"Cannot count today's confirmed submissions: {exc}") from exc
    finally:
        connection.close()

    submitted = int(row["count"] if row is not None else 0)
    imported = (
        max(0, int(raw_imported))
        if raw_imported is not None
        else None
    )
    rate_target = (
        math.ceil(
            imported * config.evaluation.min_confirmed_submission_rate
        )
        if imported is not None
        else None
    )
    target = max(
        config.daily_submit_target,
        rate_target if rate_target is not None else 0,
    )
    return {
        "local_date": local_start.date().isoformat(),
        "timezone": str(local_now.tzinfo),
        "base_target": config.daily_submit_target,
        "raw_imported": imported,
        "min_confirmation_rate": (
            config.evaluation.min_confirmed_submission_rate
        ),
        "rate_target": rate_target,
        "confirmed_rate": (
            submitted / imported
            if imported is not None and imported > 0
            else None
        ),
        "target": target,
        "submitted": submitted,
        "remaining": max(0, target - submitted),
        "reached": submitted >= target,
    }


def _run_raw_imported(run_dir: Path) -> int | None:
    manifest_path = run_dir / "pipeline-manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = _read_json_object(manifest_path, "pipeline manifest")
    counts = manifest.get("counts")
    if not isinstance(counts, Mapping):
        return None
    try:
        return max(0, int(counts.get("imported", 0) or 0))
    except (TypeError, ValueError):
        return None


def _latest_raw_imported_for_date(
    config: DailyConfig,
    local_date: str,
) -> int | None:
    latest_path = config.output_root / LATEST_FILE_NAME
    if not latest_path.is_file():
        return None
    try:
        latest = _read_json_object(latest_path, "latest run pointer")
        run_dir = Path(str(latest.get("run_dir") or "")).resolve()
        state = _read_json_object(
            run_dir / STATE_FILE_NAME,
            "run state",
        )
    except (OSError, SopError):
        return None
    daily_target = state.get("daily_target")
    if (
        not isinstance(daily_target, Mapping)
        or str(daily_target.get("local_date") or "") != local_date
    ):
        return None
    return _run_raw_imported(run_dir)


def _update_daily_target_state(
    config: DailyConfig,
    run_dir: Path,
) -> dict[str, object]:
    state = _read_json_object(run_dir / STATE_FILE_NAME, "run state")
    progress = daily_submission_progress(
        config,
        now=_execution_accounting_reference(state),
        raw_imported=_run_raw_imported(run_dir),
    )
    state["daily_target"] = progress
    state["accounting_date_source"] = (
        "execution_finished_at"
        if _last_execution_finished_at(state) is not None
        else "current_local_date"
    )
    _write_state(run_dir, state, config.output_root)
    return progress


def _last_execution_finished_at(
    state: Mapping[str, Any],
) -> datetime | None:
    attempts = state.get("execution_attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if not isinstance(attempt, Mapping):
            continue
        finished_at = _parse_datetime(attempt.get("finished_at"))
        if finished_at is not None:
            return finished_at
    return None


def _execution_accounting_reference(
    state: Mapping[str, Any],
) -> datetime:
    """Use the actual execution day once a batch has finished."""
    return _last_execution_finished_at(state) or datetime.now().astimezone()


def cleanup_managed_temp(
    config: DailyConfig,
    *,
    min_age_seconds: float = 0,
    now_epoch: float | None = None,
) -> TempCleanupReport:
    if min_age_seconds < 0:
        raise SopError("Temporary cleanup age cannot be negative")

    temp_root = _managed_temp_root(config)
    if not temp_root.exists():
        return TempCleanupReport(0, 0, 0, 0, ())
    if temp_root.is_symlink() or not temp_root.is_dir():
        return TempCleanupReport(
            0,
            0,
            0,
            0,
            (f"Managed temporary root is not a normal directory: {temp_root}",),
        )

    removed_count = 0
    skipped_active_count = 0
    skipped_recent_count = 0
    skipped_unmanaged_count = 0
    errors: list[str] = []
    current_time = time.time() if now_epoch is None else now_epoch

    for candidate in sorted(temp_root.iterdir()):
        if (
            candidate.is_symlink()
            or not candidate.is_dir()
            or not candidate.name.startswith(MANAGED_TEMP_PREFIX)
        ):
            skipped_unmanaged_count += 1
            continue

        marker_path = candidate / MANAGED_TEMP_MARKER
        marker = _read_temp_marker(marker_path)
        if (
            marker is None
            or marker.get("project_root") != str(config.root.resolve())
            or marker.get("output_root") != str(config.output_root.resolve())
        ):
            skipped_unmanaged_count += 1
            continue

        owner_pid = _marker_int(marker.get("owner_pid"))
        if _pid_is_running(owner_pid):
            skipped_active_count += 1
            continue

        created_at_epoch = _marker_float(marker.get("created_at_epoch"))
        if created_at_epoch is None:
            try:
                created_at_epoch = candidate.stat().st_mtime
            except OSError as exc:
                errors.append(f"Cannot inspect managed temporary directory {candidate}: {exc}")
                continue
        age_seconds = max(0.0, current_time - created_at_epoch)
        if age_seconds < min_age_seconds:
            skipped_recent_count += 1
            continue

        try:
            shutil.rmtree(candidate)
            removed_count += 1
        except OSError as exc:
            errors.append(f"Cannot remove managed temporary directory {candidate}: {exc}")

    try:
        temp_root.rmdir()
    except OSError:
        pass

    return TempCleanupReport(
        removed_count,
        skipped_active_count,
        skipped_recent_count,
        skipped_unmanaged_count,
        tuple(errors),
    )


@contextmanager
def managed_temp_workspace(
    config: DailyConfig,
    purpose: str,
) -> Iterator[Path]:
    temp_root = _managed_temp_root(config)
    if temp_root.exists() and (temp_root.is_symlink() or not temp_root.is_dir()):
        raise SopError(f"Managed temporary root is unsafe: {temp_root}")
    temp_root.mkdir(parents=True, exist_ok=True)

    safe_purpose = re.sub(r"[^a-z0-9]+", "-", purpose.lower()).strip("-") or "run"
    workspace = Path(
        tempfile.mkdtemp(
            prefix=f"{MANAGED_TEMP_PREFIX}{safe_purpose}-",
            dir=temp_root,
        )
    )
    previous_env = {key: os.environ.get(key) for key in TEMP_ENV_KEYS}
    try:
        _write_json(
            workspace / MANAGED_TEMP_MARKER,
            {
                "schema_version": 1,
                "project_root": str(config.root.resolve()),
                "output_root": str(config.output_root.resolve()),
                "owner_pid": os.getpid(),
                "purpose": safe_purpose,
                "created_at": _now(),
                "created_at_epoch": time.time(),
            },
        )
        for key in TEMP_ENV_KEYS:
            os.environ[key] = str(workspace)
        yield workspace
    finally:
        for key, previous in previous_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        try:
            shutil.rmtree(workspace)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(
                f"TMP CLEANUP WARNING: cannot remove {workspace}: {exc}",
                file=sys.stderr,
            )
        try:
            temp_root.rmdir()
        except OSError:
            pass


def prepare_daily_run(config: DailyConfig) -> Path:
    report = run_preflight(config)
    print_preflight(report)
    if not report.ok:
        raise SopError("Preflight failed; no pipeline command was started")

    run_dir = create_run_dir(config.output_root)
    state = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "phase": "created",
        "created_at": _now(),
        "updated_at": _now(),
        "config_path": str(config.config_path),
        "config_sha256": _sha256(config.config_path),
        "input_sha256": fingerprint_inputs(config),
        "settings": config.snapshot(),
        "artifacts": {},
        "execution_attempts": [],
        "history": [],
        "daily_target": daily_submission_progress(config),
    }
    _write_state(run_dir, state, config.output_root)

    command = build_prepare_command(config, run_dir)
    _transition(
        run_dir,
        state,
        config.output_root,
        "preparing",
        command=command,
    )
    prepare_started_at = _now()
    prepare_started = time.monotonic()
    exit_code = _run_command(command, config)
    prepare_duration_seconds = round(time.monotonic() - prepare_started, 3)
    manifest_path = run_dir / "pipeline-manifest.json"
    state["artifacts"]["manifest"] = str(manifest_path)

    if exit_code != 0 or not manifest_path.is_file():
        _transition(
            run_dir,
            state,
            config.output_root,
            "prepare_failed",
            exit_code=exit_code,
            stage="prepare",
            started_at=prepare_started_at,
            duration_seconds=prepare_duration_seconds,
        )
        write_run_report(run_dir)
        raise SopError(
            f"Preparation failed with exit code {exit_code}; inspect {run_dir}"
        )

    manifest = _read_json_object(manifest_path, "pipeline manifest")
    manifest["submit_gate"] = _effective_submit_gate(config)
    manifest["daily_sop"] = {
        "config_sha256": state["config_sha256"],
        "input_sha256": state["input_sha256"],
        "submit_complete": config.submit_complete,
        "browser_headless": config.browser_headless,
        "llm_answers": config.llm_answers,
    }
    _write_json(manifest_path, manifest)
    manifest_artifacts = manifest.get("artifacts")
    if isinstance(manifest_artifacts, dict):
        state["artifacts"].update(
            {
                str(key): str(value)
                for key, value in manifest_artifacts.items()
                if value is not None
            }
        )
    prepared_count = int(manifest.get("counts", {}).get("prepared", 0))
    next_phase = "prepared" if prepared_count else "waiting_for_candidates"
    transition_details: dict[str, Any] = {
        "exit_code": exit_code,
        "prepared_count": prepared_count,
        "stage": "prepare",
        "started_at": prepare_started_at,
        "duration_seconds": prepare_duration_seconds,
    }
    _apply_heartbeat_wake_logic(
        config,
        prepared_count,
        run_dir,
        state,
        transition_details,
    )
    _transition(
        run_dir,
        state,
        config.output_root,
        next_phase,
        **transition_details,
    )
    _update_daily_target_state(config, run_dir)
    report_path = write_run_report(run_dir)
    print(f"Prepared daily run: {run_dir}")
    print(f"Run report: {report_path}")
    return run_dir


def run_until_daily_target(config: DailyConfig) -> Path | None:
    if not config.submit_complete:
        raise SopError(
            "Daily confirmed-submission target requires submit_complete=true"
        )

    last_run: Path | None = None
    while True:
        before = daily_submission_progress(config)
        submitted_before = int(before.get("submitted", 0))
        latest_raw_imported = _latest_raw_imported_for_date(
            config,
            str(before["local_date"]),
        )
        if latest_raw_imported is not None:
            before = daily_submission_progress(
                config,
                raw_imported=latest_raw_imported,
            )
        if bool(before["reached"]):
            print(
                "Daily confirmed-submission target reached: "
                f"{before['submitted']}/{before['target']}"
            )
            return last_run

        run_dir = prepare_daily_run(config)
        last_run = run_dir
        manifest = _read_json_object(
            run_dir / "pipeline-manifest.json",
            "pipeline manifest",
        )
        prepared_count = int(manifest.get("counts", {}).get("prepared", 0))
        prepared_progress = _update_daily_target_state(config, run_dir)
        write_run_report(run_dir)
        if prepared_count == 0:
            print(
                "Daily target remains unmet with no current candidates: "
                f"{prepared_progress['submitted']}/"
                f"{prepared_progress['target']}"
            )
            return run_dir

        execution_error: SopError | None = None
        try:
            execute_daily_run(config, run_dir=run_dir)
        except SopError as exc:
            execution_error = exc

        progress = _update_daily_target_state(config, run_dir)
        write_run_report(run_dir)
        if bool(progress["reached"]):
            print(
                "Daily confirmed-submission target reached: "
                f"{progress['submitted']}/{progress['target']}"
            )
            return run_dir

        audit = _read_optional_json(run_dir / "execution-audit.json")
        audit_progress = audit.get("progress", {}) if isinstance(audit, dict) else {}
        if not bool(audit_progress.get("complete")):
            if execution_error is not None:
                raise execution_error
            raise SopError(
                "Execution did not produce a complete terminal audit; "
                "refusing to continue to another batch"
            )

        submitted_after = int(progress.get("submitted", 0))
        if _record_heartbeat_no_progress(
            config,
            run_dir,
            submitted_before,
            submitted_after,
        ):
            print(
                "Heartbeat guard: no confirmed submission after multiple "
                "consecutive executed batches. Pausing automation until "
                f"{datetime.now().astimezone() + timedelta(minutes=HEARTBEAT_NO_PROGRESS_PAUSE_MINUTES)}"
            )
            return run_dir

        if execution_error is not None:
            print(
                "Batch ended with an auditable repair/runtime outcome; "
                "continuing with new eligible jobs: "
                f"{progress['submitted']}/{progress['target']}"
            )
        else:
            print(
                "Batch complete; daily target remains: "
                f"{progress['submitted']}/{progress['target']}"
            )


def execute_daily_run(
    config: DailyConfig,
    *,
    run_dir: Path | None = None,
    retry: bool = False,
    resume_incomplete: bool = False,
    _retry_summary_path: Path | None = None,
) -> Path:
    resolved_run_dir = resolve_run_dir(config, run_dir)
    state = _read_json_object(resolved_run_dir / STATE_FILE_NAME, "run state")
    phase = str(state.get("phase", ""))
    completed_audit_reconciliation = False
    if resume_incomplete and phase in {"executing", "execution_failed"}:
        existing_audit_path = resolved_run_dir / "execution-audit.json"
        existing_audit = _read_optional_json(existing_audit_path)
        existing_progress = (
            existing_audit.get("progress")
            if isinstance(existing_audit, Mapping)
            else None
        )
        completed_audit_reconciliation = bool(
            isinstance(existing_progress, Mapping)
            and existing_progress.get("complete")
        )
    # A complete audit is immutable execution evidence. Reconciliation does
    # not reopen a browser or consume current profile/config inputs, so input
    # drift after the browser process exited must not make the run impossible
    # to finalize.
    if not completed_audit_reconciliation:
        validate_run_inputs(config, state)
    allowed_phases = {"prepared", "prepared_empty", "waiting_for_candidates"}
    if resume_incomplete and retry:
        raise SopError("--resume-incomplete cannot be combined with --retry")
    if resume_incomplete:
        if phase not in {"executing", "execution_failed"}:
            raise SopError(
                f"Run phase is '{phase}', not an interrupted execution; "
                "--resume-incomplete only resumes an interrupted execution"
            )
        if phase == "execution_failed":
            attempts = state.get("execution_attempts")
            last_attempt = attempts[-1] if isinstance(attempts, list) and attempts else {}
            last_exit_code = (
                last_attempt.get("exit_code")
                if isinstance(last_attempt, dict)
                else None
            )
            interrupted_codes = {
                -int(signal.SIGINT),
                -int(signal.SIGTERM),
                -int(signal.SIGKILL),
                128 + int(signal.SIGINT),
                128 + int(signal.SIGTERM),
                128 + int(signal.SIGKILL),
            }
            if last_exit_code not in interrupted_codes:
                raise SopError(
                    "--resume-incomplete requires the last execution to have "
                    "ended by interruption signal"
                )
    elif phase not in allowed_phases and not retry:
        raise SopError(
            f"Run phase is '{phase}', not prepared. "
            "Use --retry only after the recorded blocker has been resolved."
        )

    manifest_path = resolved_run_dir / "pipeline-manifest.json"
    manifest = _read_json_object(manifest_path, "pipeline manifest")
    prepared_count = int(manifest.get("counts", {}).get("prepared", 0))
    if prepared_count == 0:
        waited_seconds = _waiting_seconds_since_ready(state)
        next_wake_at = _next_wake_at(config.empty_wake_minutes)
        state["next_wake_at"] = next_wake_at
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "waiting_for_candidates",
            waiting_seconds=waited_seconds,
            next_wake_at=next_wake_at,
            wake_after_minutes=config.empty_wake_minutes,
        )
        write_run_report(resolved_run_dir)
        print(f"No prepared applications to execute: {resolved_run_dir}")
        return resolved_run_dir

    if not completed_audit_reconciliation:
        report = run_preflight(config)
        print_preflight(report)
        if not report.ok:
            raise SopError("Preflight failed; no browser execution was started")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts.get("batch_summary"):
        raise SopError("Pipeline manifest does not contain a batch summary")
    summary_path = (
        _retry_summary_path.resolve()
        if _retry_summary_path is not None
        else _artifact_path(artifacts["batch_summary"], config.root)
    )
    if not summary_path.is_file():
        raise SopError(f"Batch summary does not exist: {summary_path}")
    if not _is_within(summary_path, resolved_run_dir):
        raise SopError(
            "Batch summary belongs to a different run directory; refusing to mix runs"
        )

    attempt_number = len(state.get("execution_attempts", [])) + 1
    if resume_incomplete:
        audit_path = resolved_run_dir / "execution-audit.json"
        preflight_path = resolved_run_dir / "resume-preflight.json"
        existing_audit = _read_json_object(audit_path, "execution audit")
        progress = existing_audit.get("progress")
        if not isinstance(progress, dict):
            raise SopError(
                "--resume-incomplete requires an audit with incremental progress"
            )
        if (
            not bool(progress.get("complete"))
            and int(progress.get("planned", -1)) != prepared_count
        ):
            raise SopError(
                "Incomplete audit planned count does not match the prepared batch"
            )
    else:
        suffix = "" if attempt_number == 1 else f"-retry-{attempt_number - 1:02d}"
        audit_path = resolved_run_dir / f"execution-audit{suffix}.json"
        preflight_path = resolved_run_dir / f"resume-preflight{suffix}.json"
    command = (
        ["reconcile-complete-execution-audit", str(audit_path)]
        if completed_audit_reconciliation
        else build_execute_command(
            config,
            summary_path=summary_path,
            audit_path=audit_path,
            preflight_path=preflight_path,
            retry=retry,
            resume_existing_audit=resume_incomplete,
        )
    )

    waited_seconds = _waiting_seconds_since_ready(state)
    state.pop("next_wake_at", None)
    _transition(
        resolved_run_dir,
        state,
        config.output_root,
        "executing",
        command=command,
        attempt=attempt_number,
        waiting_seconds=waited_seconds,
    )
    execution_started_at = _now()
    execution_started = time.monotonic()
    initial_repair_cycle = _next_repair_cycle(state, resolved_run_dir)
    initial_repair_budget_cycle = (
        _repair_cycle_count(state, resolved_run_dir) + 1
    )
    initial_repair_attempt = _next_repair_attempt(state)
    if completed_audit_reconciliation:
        exit_code, incremental_repair = 0, None
    else:
        exit_code, incremental_repair = _run_execution_command(
            command,
            config,
            audit_path=audit_path,
            run_dir=resolved_run_dir,
            repair_cycle=initial_repair_cycle,
            repair_budget_cycle=initial_repair_budget_cycle,
            repair_attempt=initial_repair_attempt,
        )
    execution_duration_seconds = round(time.monotonic() - execution_started, 3)

    attempt = {
        "attempt": attempt_number,
        "started_at": execution_started_at,
        "finished_at": _now(),
        "duration_seconds": execution_duration_seconds,
        "exit_code": exit_code,
        "audit": str(audit_path),
        "resume_preflight": str(preflight_path),
        "batch_summary": str(summary_path),
        "reconciled_complete_audit": completed_audit_reconciliation,
    }
    state.setdefault("execution_attempts", []).append(attempt)
    state.setdefault("artifacts", {})["execution_audit"] = str(audit_path)
    state["artifacts"]["resume_preflight"] = str(preflight_path)

    if not audit_path.is_file():
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "execution_failed",
            exit_code=exit_code,
            attempt=attempt_number,
            stage="execute",
            started_at=execution_started_at,
            duration_seconds=execution_duration_seconds,
        )
        write_run_report(resolved_run_dir)
        raise SopError(f"Execution audit was not written: {audit_path}")

    audit = _read_json_object(audit_path, "execution audit")
    audit["effective_submit_gate"] = _effective_submit_gate(config)
    audit["daily_sop"] = {
        "config_sha256": state.get("config_sha256"),
        "submit_complete": config.submit_complete,
        "browser_headless": config.browser_headless,
        "llm_answers": config.llm_answers,
    }
    recovery_batch = execute_audit_recovery(
        audit,
        run_dir=resolved_run_dir,
        database=config.database,
        environ=os.environ,
    )
    recovery_path = resolved_run_dir / RECOVERY_EXECUTION_FILE_NAME
    _write_json(
        recovery_path,
        {
            "schema_version": 1,
            "status_counts": dict(recovery_batch.status_counts),
            "verified_targets": list(recovery_batch.verified_targets),
            "applications": [
                {
                    "company": item.get("company"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "application_id": item.get("application_id"),
                    "recovery_execution": item.get("recovery_execution"),
                }
                for item in recovery_batch.applications
                if item.get("recovery_execution") is not None
            ],
        },
    )
    state.setdefault("artifacts", {})["recovery_execution"] = str(recovery_path)
    manifest.setdefault("artifacts", {})["recovery_execution"] = str(recovery_path)
    recovery_retry_summary = write_recovery_retry_batch(
        summary_path,
        verified_targets=recovery_batch.verified_targets,
        output_path=(
            resolved_run_dir
            / "recovery"
            / f"retry-batch-attempt-{attempt_number:02d}.json"
        ),
    )
    if recovery_retry_summary is not None:
        state["artifacts"]["recovery_retry_batch"] = str(
            recovery_retry_summary
        )
        manifest["artifacts"]["recovery_retry_batch"] = str(
            recovery_retry_summary
        )
    _persist_recovery_annotations(
        state,
        run_dir=resolved_run_dir,
        root=config.root,
        fallback_path=audit_path,
        recovery_audit=audit,
    )
    counts = audit.get("counts") if isinstance(audit.get("counts"), dict) else {}
    issue_count = sum(int(counts.get(key, 0) or 0) for key in ISSUE_COUNT_KEYS)
    completed_repair_requests: list[Mapping[str, Any]] = []
    incremental_result: dict[str, Any] | None = None
    if incremental_repair is not None:
        incremental_result = dict(incremental_repair.result)
        state.setdefault("artifacts", {})["incremental_repair_request"] = str(
            incremental_repair.request_path
        )
        incremental_result_path = str(
            incremental_result.get("result_path") or ""
        )
        if incremental_result_path:
            state["artifacts"]["incremental_repair_result"] = (
                incremental_result_path
            )
        if incremental_result.get("status") == "verified_pending_promotion":
            incremental_result = promote_deferred_repair(
                root=config.root,
                run_dir=resolved_run_dir,
                result=incremental_result,
            )
        incremental_attempt = {
            "attempt": initial_repair_attempt,
            "cycle": initial_repair_cycle,
            "status": incremental_result.get("status"),
            "reason": incremental_result.get("reason"),
            "result_path": incremental_result.get("result_path"),
            "changed_files": incremental_result.get("changed_files", []),
            "incremental": True,
        }
        state.setdefault("repair_attempts", []).append(incremental_attempt)
        if repair_result_is_verified(incremental_result):
            if repair_result_consumes_cycle(incremental_result):
                state.setdefault("repair_cycles", []).append(incremental_attempt)
            completed_repair_requests.append(incremental_repair.request)
        elif repair_result_consumes_cycle(incremental_result):
            state.setdefault("repair_cycles", []).append(incremental_attempt)
        _write_state(resolved_run_dir, state, config.output_root)

    repair_cycle = _next_repair_cycle(state, resolved_run_dir)
    repair_budget_cycle = _repair_cycle_count(state, resolved_run_dir) + 1
    repair_attempt = _next_repair_attempt(state)
    repair_request = build_repair_request(
        audit,
        run_dir=resolved_run_dir,
        cycle=repair_cycle,
    )
    if completed_repair_requests and repair_request is not None:
        repair_request = _subtract_repair_request(
            repair_request,
            completed_repair_requests,
        )
    if repair_request is not None:
        repair_request["attempt"] = repair_attempt
        repair_request["budget_cycle"] = repair_budget_cycle
    effective_repair_request = (
        repair_request
        or _merge_repair_requests(completed_repair_requests)
    )
    next_phase = (
        "needs_repair"
        if effective_repair_request is not None
        else "execution_failed"
        if exit_code != 0
        else "executed_with_blockers"
        if issue_count
        else "executed"
    )
    _transition(
        resolved_run_dir,
        state,
        config.output_root,
        next_phase,
        exit_code=exit_code,
        attempt=attempt_number,
        execution_counts=counts,
        stage="execute",
        started_at=execution_started_at,
        duration_seconds=execution_duration_seconds,
    )

    manifest.setdefault("artifacts", {})["execution_audit"] = str(audit_path)
    manifest["artifacts"]["resume_preflight"] = str(preflight_path)
    manifest["execution_counts"] = counts
    manifest["runtime_llm_answers_enabled"] = config.llm_answers
    manifest["submit_gate"] = _effective_submit_gate(config)
    if effective_repair_request is not None:
        effective_request_cycle = int(
            effective_repair_request.get("cycle") or repair_cycle
        )
        effective_request_attempt = int(
            effective_repair_request.get("attempt")
            or effective_request_cycle
        )
        request_path = _repair_request_path(
            resolved_run_dir,
            cycle=effective_request_cycle,
            attempt=effective_request_attempt,
        )
        _write_json(request_path, effective_repair_request)
        attempt["repair_request"] = str(request_path)
        state.setdefault("artifacts", {})["repair_request"] = str(request_path)
        manifest["artifacts"]["repair_request"] = str(request_path)
        _write_state(resolved_run_dir, state, config.output_root)
    _write_json(manifest_path, manifest)

    report_path = write_run_report(resolved_run_dir)
    print(f"Executed daily run: {resolved_run_dir}")
    print(f"Run report: {report_path}")
    if effective_repair_request is not None:
        if not config.auto_repair.enabled:
            raise SopError(
                "Execution produced repairable blockers; state is needs_repair "
                "and automatic repair is disabled"
            )
        if (
            incremental_result is not None
            and not repair_result_consumes_cycle(incremental_result)
        ):
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_unavailable",
                repair_cycle=repair_cycle,
                repair_attempt=initial_repair_attempt,
                consumed_repair_cycles=_repair_cycle_count(
                    state,
                    resolved_run_dir,
                ),
                repair_status=incremental_result.get("status"),
                repair_reason=incremental_result.get("reason"),
                repair_retryable=bool(incremental_result.get("retryable")),
                stage="repair",
            )
            write_run_report(resolved_run_dir)
            raise SopError(
                "Automatic repair agent is unavailable; the scoped request "
                "was preserved without consuming another repair cycle"
            )
        if (
            repair_request is not None
            and repair_budget_cycle > config.auto_repair.max_cycles
        ):
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_exhausted",
                repair_cycle=repair_cycle,
                repair_reason="maximum_repair_cycles_reached",
            )
            write_run_report(resolved_run_dir)
            raise SopError(
                "Execution still has the same repairable blocker after all "
                "verified repair cycles"
            )

        active_request = (
            repair_request
            or _merge_repair_requests(completed_repair_requests)
        )
        repair_result = (
            incremental_result
            if incremental_result is not None
            and repair_result_is_verified(incremental_result)
            else None
        )
        repair_duration_seconds = 0.0
        while (
            repair_request is not None
            and repair_budget_cycle <= config.auto_repair.max_cycles
        ):
            repair_attempt_number = int(
                active_request.get("attempt")
                or _next_repair_attempt(state)
            )
            active_request = {
                **dict(active_request),
                "cycle": repair_cycle,
                "budget_cycle": repair_budget_cycle,
                "attempt": repair_attempt_number,
            }
            request_path = _repair_request_path(
                resolved_run_dir,
                cycle=repair_cycle,
                attempt=repair_attempt_number,
            )
            _write_json(request_path, active_request)
            state.setdefault("artifacts", {})["repair_request"] = str(request_path)
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repairing",
                repair_cycle=repair_cycle,
                repair_attempt=repair_attempt_number,
                consumed_repair_cycles=_repair_cycle_count(
                    state,
                    resolved_run_dir,
                ),
                repair_request=str(request_path),
            )
            repair_started = time.monotonic()
            repair_result = run_repair_cycle(
                config.auto_repair,
                root=config.root,
                run_dir=resolved_run_dir,
                request=active_request,
            )
            repair_duration_seconds = round(
                time.monotonic() - repair_started,
                3,
            )
            repair_attempt_record = {
                "attempt": repair_attempt_number,
                "cycle": repair_cycle,
                "status": repair_result.get("status"),
                "reason": repair_result.get("reason"),
                "result_path": repair_result.get("result_path"),
                "changed_files": repair_result.get("changed_files", []),
                "duration_seconds": repair_duration_seconds,
            }
            state.setdefault("repair_attempts", []).append(
                repair_attempt_record
            )
            consumes_cycle = repair_result_consumes_cycle(repair_result)
            if consumes_cycle:
                state.setdefault("repair_cycles", []).append(
                    repair_attempt_record
                )
            consumed_repair_cycles = _repair_cycle_count(
                state,
                resolved_run_dir,
            )
            state["consumed_repair_cycles"] = consumed_repair_cycles
            result_path = str(repair_result.get("result_path") or "")
            if result_path:
                state.setdefault("artifacts", {})["repair_result"] = result_path
                manifest["artifacts"]["repair_result"] = result_path
            _write_state(resolved_run_dir, state, config.output_root)
            _write_json(manifest_path, manifest)

            if not consumes_cycle:
                _transition(
                    resolved_run_dir,
                    state,
                    config.output_root,
                    "repair_unavailable",
                    repair_cycle=repair_cycle,
                    repair_attempt=repair_attempt_number,
                    consumed_repair_cycles=consumed_repair_cycles,
                    repair_status=repair_result.get("status"),
                    repair_reason=repair_result.get("reason"),
                    repair_retryable=bool(repair_result.get("retryable")),
                    stage="repair",
                    duration_seconds=repair_duration_seconds,
                )
                write_run_report(resolved_run_dir)
                raise SopError(
                    "Automatic repair agent is unavailable; the scoped request "
                    "was preserved without consuming another repair cycle"
                )
            if repair_result_is_verified(repair_result):
                completed_repair_requests.append(active_request)
                break
            if consumed_repair_cycles >= config.auto_repair.max_cycles:
                _transition(
                    resolved_run_dir,
                    state,
                    config.output_root,
                    "repair_exhausted",
                    repair_cycle=repair_cycle,
                    repair_attempt=repair_attempt_number,
                    consumed_repair_cycles=consumed_repair_cycles,
                    repair_status=repair_result.get("status"),
                    repair_reason=repair_result.get("reason"),
                    stage="repair",
                    duration_seconds=repair_duration_seconds,
                )
                write_run_report(resolved_run_dir)
                raise SopError(
                    "Automatic repair exhausted its bounded cycles without "
                    "a verified change"
                )
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_failed",
                repair_cycle=repair_cycle,
                repair_attempt=repair_attempt_number,
                consumed_repair_cycles=consumed_repair_cycles,
                repair_status=repair_result.get("status"),
                repair_reason=repair_result.get("reason"),
                will_retry=True,
                stage="repair",
                duration_seconds=repair_duration_seconds,
            )
            repair_cycle = _next_repair_cycle(state, resolved_run_dir)
            repair_budget_cycle = consumed_repair_cycles + 1
            next_request = build_repair_request(
                audit,
                run_dir=resolved_run_dir,
                cycle=repair_cycle,
            )
            if next_request is not None and completed_repair_requests:
                next_request = _subtract_repair_request(
                    next_request,
                    completed_repair_requests,
                )
            if next_request is None:
                _transition(
                    resolved_run_dir,
                    state,
                    config.output_root,
                    "repair_failed",
                    repair_cycle=repair_cycle,
                    repair_reason="repair_request_could_not_be_rebuilt",
                )
                write_run_report(resolved_run_dir)
                raise SopError("Automatic repair request could not be rebuilt")
            next_request["attempt"] = _next_repair_attempt(state)
            next_request["budget_cycle"] = repair_budget_cycle
            active_request = next_request

        if repair_result is None or not repair_result_is_verified(repair_result):
            raise SopError("Automatic repair did not produce a verified change")
        combined_request = _merge_repair_requests(completed_repair_requests)
        if combined_request is None:
            raise SopError("Verified repair has no scoped request")
        verified_cycle = int(
            repair_result.get("cycle")
            or combined_request.get("cycle")
            or repair_cycle
        )
        verified_attempt = int(
            repair_result.get("attempt")
            or combined_request.get("attempt")
            or verified_cycle
        )
        scoped_retry_summary = _write_verified_repair_retry_batch(
            config,
            state=state,
            manifest=manifest,
            run_dir=resolved_run_dir,
            request=combined_request,
            cycle=verified_cycle,
        )
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repair_verified",
            repair_cycle=verified_cycle,
            repair_attempt=verified_attempt,
            consumed_repair_cycles=_repair_cycle_count(
                state,
                resolved_run_dir,
            ),
            changed_files=repair_result.get("changed_files", []),
            scoped_retry_batch=str(scoped_retry_summary),
            stage="repair",
            duration_seconds=repair_duration_seconds,
        )
        write_run_report(resolved_run_dir)

        if config.auto_repair.retry_after_verified_repair:
            return execute_daily_run(
                config,
                run_dir=resolved_run_dir,
                retry=True,
                _retry_summary_path=scoped_retry_summary,
            )
        return resolved_run_dir
    if recovery_retry_summary is not None:
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "recovery_verified",
            recovery_retry_batch=str(recovery_retry_summary),
            stage="recovery",
        )
        write_run_report(resolved_run_dir)
        return execute_daily_run(
            config,
            run_dir=resolved_run_dir,
            retry=True,
            _retry_summary_path=recovery_retry_summary,
        )
    if exit_code != 0:
        raise SopError(f"Execution command exited with code {exit_code}")
    return resolved_run_dir


def _candidate_fact_recovery_handlers(
    config: DailyConfig,
    *,
    run_dir: Path,
    jobs_path: Path | None,
    attempt_number: int,
) -> dict[str, Any]:
    """Verify newly approved facts and rebuild only their blocked package."""
    from job_agent.cli import (
        _job_from_dict,
        _load_profile_facts,
        _prepare_application_package,
    )
    from job_agent.python_runtime import (
        PLACEHOLDER_ANSWERS,
        _norm,
        load_runtime_payload,
    )
    from job_agent.llm_answer_resolver import match_screening_rule
    from job_agent.sensitive_kb import resolve_sensitive_answer

    profile_facts = _load_profile_facts(config.profile, config.sensitive_kb)
    if not isinstance(profile_facts, dict):
        return {}

    raw_jobs: list[Mapping[str, Any]] = []
    if jobs_path is not None and jobs_path.is_file():
        try:
            payload = json.loads(jobs_path.read_text())
        except (OSError, json.JSONDecodeError):
            payload = []
        if isinstance(payload, list):
            raw_jobs = [item for item in payload if isinstance(item, Mapping)]

    profile_sha256 = _sha256(config.profile)
    sensitive_kb_sha256 = _sha256(config.sensitive_kb)
    prior_profile_cache: dict[str, tuple[dict[str, Any], bool]] = {}

    def blocking_items(context: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        review_items = context.get("review_items")
        if not isinstance(review_items, list):
            return []
        return [
            item
            for item in review_items
            if isinstance(item, Mapping)
            and bool(item.get("blocking", True))
        ]

    def approved_answer(
        label: str,
        item: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> str | None:
        if bool(item.get("sensitive")):
            answer = resolve_sensitive_answer(label, dict(facts))
        else:
            raw_answers = facts.get("answers")
            answers = raw_answers if isinstance(raw_answers, Mapping) else {}
            answer = next(
                (
                    value
                    for key, value in answers.items()
                    if _norm(key) == _norm(label)
                ),
                None,
            )
            if answer is None:
                answer = match_screening_rule(label, facts.get("screening_answer_rules"))
        if answer is None:
            return None
        raw = str(answer).strip()
        if (
            raw.casefold() in PLACEHOLDER_ANSWERS
            or not raw
        ):
            return None
        return raw

    def prior_profile(context: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        raw_script = str(context.get("script_path") or "").strip()
        if not raw_script:
            package_value = str(context.get("package_dir") or "").strip()
            if package_value:
                raw_script = str(Path(package_value) / "autofill-runtime.js")
        if raw_script in prior_profile_cache:
            return prior_profile_cache[raw_script]
        if not raw_script:
            return {}, False
        script_path = Path(raw_script)
        if not script_path.is_absolute():
            script_path = run_dir / script_path
        try:
            script_path = script_path.resolve()
            script_path.relative_to(run_dir.resolve())
            payload = load_runtime_payload(script_path)
        except (OSError, ValueError, json.JSONDecodeError):
            result = ({}, False)
        else:
            raw_profile = payload.get("profile")
            result = (
                (dict(raw_profile), True)
                if isinstance(raw_profile, Mapping)
                else ({}, False)
            )
        prior_profile_cache[raw_script] = result
        return result

    def fact_readiness(
        context: Mapping[str, Any],
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        labels: list[str] = []
        unresolved: list[str] = []
        unchanged: list[str] = []
        nonfact: list[str] = []
        previous_facts, previous_available = prior_profile(context)
        for item in blocking_items(context):
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            if not requires_approved_candidate_fact(item):
                nonfact.append(label)
                continue
            labels.append(label)
            answer = approved_answer(label, item, profile_facts)
            if answer is None:
                unresolved.append(label)
                continue
            previous = (
                approved_answer(label, item, previous_facts)
                if previous_available
                else None
            )
            if not previous_available or (
                previous is not None
                and previous.strip().casefold() == answer.strip().casefold()
            ):
                unchanged.append(label)
        return labels, unresolved, unchanged, nonfact

    def verify_candidate_facts(
        _action: Any,
        context: Mapping[str, Any],
        _private_state: dict[str, Any],
    ) -> Mapping[str, Any]:
        labels, unresolved, unchanged, nonfact = fact_readiness(context)
        if not labels or unresolved:
            return {
                "status": "waiting_for_user",
                "evidence": [],
                "message": (
                    "Approved candidate answers are still missing for "
                    f"{len(unresolved) or len(labels)} required field(s)."
                ),
                "details": {
                    "required_field_count": len(labels),
                    "unresolved_field_count": len(unresolved) or len(labels),
                },
            }
        # An existing approved answer is sufficient to rebuild and retry when the
        # runtime's closest-match logic has since been corrected; the fact does
        # not need to be newly added to the profile.
        return {
            "status": "completed",
            "evidence": ["approved_candidate_facts"],
            "message": (
                "Approved candidate answers are present in the fact source and "
                "the scoped application can be rebuilt."
            ),
            "details": {
                "approved_field_count": len(labels),
                "unchanged_field_count": len(unchanged),
                "nonfact_blocker_count": len(nonfact),
                "profile_sha256": profile_sha256,
                "sensitive_kb_sha256": sensitive_kb_sha256,
            },
        }

    def rebuild_scoped_application(
        _action: Any,
        context: Mapping[str, Any],
        _private_state: dict[str, Any],
    ) -> Mapping[str, Any]:
        labels, unresolved, unchanged, nonfact = fact_readiness(context)
        if not labels or unresolved:
            return {
                "status": "pending",
                "evidence": [],
                "message": (
                    "The scoped package cannot be rebuilt until every "
                    "candidate-fact blocker has an approved answer."
                ),
                "details": {
                    "required_field_count": len(labels),
                    "unresolved_field_count": len(unresolved) or len(labels),
                    "unchanged_field_count": len(unchanged),
                    "nonfact_blocker_count": len(nonfact),
                },
            }

        apply_url = str(context.get("apply_url") or "").strip()
        company = str(context.get("company") or "").strip().casefold()
        title = str(context.get("title") or "").strip().casefold()
        raw_job = next(
            (
                item
                for item in raw_jobs
                if apply_url
                and str(item.get("apply_url") or item.get("source_url") or "").strip()
                == apply_url
            ),
            None,
        )
        if raw_job is None:
            matches = [
                item
                for item in raw_jobs
                if str(item.get("company") or "").strip().casefold() == company
                and str(item.get("title") or "").strip().casefold() == title
            ]
            raw_job = matches[0] if len(matches) == 1 else None
        if raw_job is None:
            return {
                "status": "pending",
                "evidence": [],
                "message": "The original normalized job could not be located for scoped rebuilding.",
            }

        application_id = re.sub(
            r"[^A-Za-z0-9._-]+",
            "-",
            str(context.get("application_id") or "unknown"),
        ).strip("-") or "unknown"
        base_dir = (
            run_dir
            / "recovery"
            / f"candidate-facts-{attempt_number:02d}-application-{application_id}"
        )
        package_dir = base_dir
        suffix = 2
        while package_dir.exists():
            package_dir = base_dir.with_name(f"{base_dir.name}-{suffix:02d}")
            suffix += 1

        summary = _prepare_application_package(
            _job_from_dict(dict(raw_job)),
            package_dir,
            resume_source_dir=config.resume_source_dir,
            db=config.database,
            profile=config.profile,
            sensitive_kb=config.sensitive_kb,
            use_llm=config.use_llm,
            runtime_headless=config.browser_headless,
            profile_vector_db=config.profile_vector_db,
            required_resume_pdf=config.required_resume_pdf,
        )
        rebuilt_application_id = str(summary.get("application_id") or "")
        expected_application_id = str(context.get("application_id") or "")
        if (
            expected_application_id
            and rebuilt_application_id
            and rebuilt_application_id != expected_application_id
        ):
            raise SopError(
                "Scoped candidate-fact rebuild changed the tracked application ID"
            )
        summary_path = package_dir / "recovery-package-summary.json"
        _write_json(summary_path, summary)
        return {
            "status": "completed",
            "evidence": ["field_gate_passed"],
            "message": "The single application package was rebuilt from current approved facts.",
            "details": {
                "approved_field_count": len(labels),
                "profile_sha256": profile_sha256,
                "sensitive_kb_sha256": sensitive_kb_sha256,
                "replacement_summary_path": str(summary_path),
                "replacement_package_dir": str(package_dir),
            },
        }

    return {
        "request_candidate_facts": verify_candidate_facts,
        "update_approved_fact_source": verify_candidate_facts,
        "rebuild_scoped_application": rebuild_scoped_application,
    }


def recover_daily_run(
    config: DailyConfig,
    *,
    run_dir: Path | None = None,
    retry_verified: bool = False,
) -> Path:
    """Replay Recovery Plans from one completed audit without blind resubmission."""
    resolved_run_dir = resolve_run_dir(config, run_dir)
    state = _read_json_object(
        resolved_run_dir / STATE_FILE_NAME,
        "run state",
    )
    manifest_path = resolved_run_dir / "pipeline-manifest.json"
    manifest = _read_json_object(manifest_path, "pipeline manifest")
    state_artifacts = (
        state.get("artifacts")
        if isinstance(state.get("artifacts"), Mapping)
        else {}
    )
    manifest_artifacts = (
        manifest.get("artifacts")
        if isinstance(manifest.get("artifacts"), Mapping)
        else {}
    )
    audit_value = (
        state_artifacts.get("execution_audit")
        or manifest_artifacts.get("execution_audit")
        or resolved_run_dir / "execution-audit.json"
    )
    audit_path = Path(str(audit_value))
    if not audit_path.is_absolute():
        audit_path = config.root / audit_path
    audit_path = audit_path.resolve()
    if not _is_within(audit_path, resolved_run_dir) or not audit_path.is_file():
        raise SopError(
            "Historical recovery requires an execution audit inside the selected run"
        )
    audit = _execution_audit_for_report(
        state,
        run_dir=resolved_run_dir,
        root=config.root,
        fallback=_read_json_object(audit_path, "execution audit"),
    )
    progress = audit.get("progress")
    if isinstance(progress, Mapping) and not bool(progress.get("complete")):
        raise SopError(
            "Historical recovery requires a complete terminal audit"
        )

    summary_value = (
        manifest_artifacts.get("batch_summary")
        or state_artifacts.get("batch_summary")
    )
    if not summary_value:
        raise SopError("Historical recovery requires the original batch summary")
    summary_path = _artifact_path(summary_value, config.root)
    if not _is_within(summary_path, resolved_run_dir) or not summary_path.is_file():
        raise SopError(
            "Historical recovery batch summary is outside the selected run"
        )
    jobs_value = (
        manifest_artifacts.get("jobs")
        or state_artifacts.get("jobs")
    )
    jobs_path: Path | None = None
    if jobs_value:
        candidate_jobs_path = _artifact_path(jobs_value, config.root)
        if (
            _is_within(candidate_jobs_path, resolved_run_dir)
            and candidate_jobs_path.is_file()
        ):
            jobs_path = candidate_jobs_path

    recovery_attempts = state.get("recovery_attempts")
    prior_attempts = (
        [item for item in recovery_attempts if isinstance(item, Mapping)]
        if isinstance(recovery_attempts, list)
        else []
    )
    attempt_number = len(prior_attempts) + 1
    started_at = _now()
    started = time.monotonic()
    recovery_batch = execute_audit_recovery(
        audit,
        run_dir=resolved_run_dir,
        database=config.database,
        environ=os.environ,
        handlers=_candidate_fact_recovery_handlers(
            config,
            run_dir=resolved_run_dir,
            jobs_path=jobs_path,
            attempt_number=attempt_number,
        ),
    )
    duration_seconds = round(time.monotonic() - started, 3)
    recovery_path = resolved_run_dir / RECOVERY_EXECUTION_FILE_NAME
    _write_json(
        recovery_path,
        {
            "schema_version": 1,
            "attempt": attempt_number,
            "started_at": started_at,
            "finished_at": _now(),
            "duration_seconds": duration_seconds,
            "status_counts": dict(recovery_batch.status_counts),
            "verified_targets": list(recovery_batch.verified_targets),
            "applications": [
                {
                    "company": item.get("company"),
                    "title": item.get("title"),
                    "status": item.get("status"),
                    "application_id": item.get("application_id"),
                    "recovery_plan": item.get("recovery_plan"),
                    "recovery_execution": item.get("recovery_execution"),
                }
                for item in recovery_batch.applications
                if item.get("recovery_execution") is not None
            ],
        },
    )
    retry_summary = write_recovery_retry_batch(
        summary_path,
        verified_targets=recovery_batch.verified_targets,
        output_path=(
            resolved_run_dir
            / "recovery"
            / f"retry-batch-recovery-{attempt_number:02d}.json"
        ),
    )
    candidate_fact_retry_verified = any(
        item.get("recovery_strategy") == "candidate_fact_resolution"
        for item in recovery_batch.verified_targets
        if isinstance(item, Mapping)
    )
    recovery_input_sha256: dict[str, str] | None = None
    recovery_config_sha256: str | None = None
    if candidate_fact_retry_verified:
        current_inputs = fingerprint_inputs(config)
        recorded_inputs = state.get("input_sha256")
        prior_inputs = (
            dict(recorded_inputs)
            if isinstance(recorded_inputs, Mapping)
            else {}
        )
        changed_inputs = {
            key
            for key in set(prior_inputs) | set(current_inputs)
            if prior_inputs.get(key) != current_inputs.get(key)
        }
        disallowed_changes = sorted(
            changed_inputs - {"profile", "sensitive_kb", "source_config"}
        )
        if disallowed_changes:
            raise SopError(
                "Candidate-fact recovery found unrelated prepared-input changes: "
                + ", ".join(disallowed_changes)
            )
        recovery_input_sha256 = current_inputs
        state["input_sha256"] = dict(current_inputs)
        recorded_settings = (
            dict(state.get("settings"))
            if isinstance(state.get("settings"), Mapping)
            else {}
        )
        current_settings = config.snapshot()
        recovery_critical_keys = {
            "root",
            "source_config",
            "profile",
            "sensitive_kb",
            "database",
            "profile_vector_db",
            "resume_source_dir",
            "required_resume_pdf",
            "output_root",
            "use_llm",
            "llm_answers",
            "submit_complete",
            "require_gmail_token",
        }
        critical_changes = sorted(
            key
            for key in recovery_critical_keys
            if recorded_settings.get(key) != current_settings.get(key)
        )
        if critical_changes:
            raise SopError(
                "Candidate-fact recovery found safety-critical config changes: "
                + ", ".join(critical_changes)
            )
        if config.config_path.is_file():
            recovery_config_sha256 = _sha256(config.config_path)
            state["config_sha256"] = recovery_config_sha256
        state["settings"] = current_settings
        manifest_daily_sop = manifest.setdefault("daily_sop", {})
        if isinstance(manifest_daily_sop, dict):
            manifest_daily_sop["input_sha256"] = dict(current_inputs)
            if recovery_config_sha256 is not None:
                manifest_daily_sop["config_sha256"] = recovery_config_sha256
    state.setdefault("artifacts", {})["recovery_execution"] = str(
        recovery_path
    )
    manifest.setdefault("artifacts", {})["recovery_execution"] = str(
        recovery_path
    )
    if retry_summary is not None:
        state["artifacts"]["recovery_retry_batch"] = str(retry_summary)
        manifest["artifacts"]["recovery_retry_batch"] = str(retry_summary)
    else:
        state["artifacts"].pop("recovery_retry_batch", None)
        manifest["artifacts"].pop("recovery_retry_batch", None)
    state.setdefault("recovery_attempts", []).append(
        {
            "attempt": attempt_number,
            "started_at": started_at,
            "finished_at": _now(),
            "duration_seconds": duration_seconds,
            "status_counts": dict(recovery_batch.status_counts),
            "verified_target_count": len(recovery_batch.verified_targets),
            "recovery_execution": str(recovery_path),
            "retry_batch": str(retry_summary) if retry_summary else None,
            "input_sha256": recovery_input_sha256,
            "config_sha256": recovery_config_sha256,
        }
    )
    _persist_recovery_annotations(
        state,
        run_dir=resolved_run_dir,
        root=config.root,
        fallback_path=audit_path,
        recovery_audit=audit,
    )
    _write_json(manifest_path, manifest)
    _write_state(resolved_run_dir, state, config.output_root)
    write_run_report(resolved_run_dir)
    print(f"Recovered historical audit: {resolved_run_dir}")
    print(f"Recovery execution: {recovery_path}")

    if retry_verified:
        if retry_summary is None:
            raise SopError(
                "Historical recovery produced no verified scoped retry batch"
            )
        return execute_daily_run(
            config,
            run_dir=resolved_run_dir,
            retry=True,
            _retry_summary_path=retry_summary,
        )
    return resolved_run_dir


def _refresh_verified_scoped_retry_batch(
    config: DailyConfig,
    *,
    run_dir: Path,
    state: dict[str, Any],
) -> Path:
    """Recompose a verified Repair batch from current, non-stale pointers."""
    manifest_path = run_dir / "pipeline-manifest.json"
    manifest = _read_json_object(manifest_path, "pipeline manifest")
    state_artifacts = state.setdefault("artifacts", {})
    manifest_artifacts = manifest.setdefault("artifacts", {})
    repair_value = state_artifacts.get("repair_retry_batch")
    if not repair_value:
        raise SopError("Verified repair has no saved repair retry batch")
    repair_summary = Path(str(repair_value))
    if not repair_summary.is_absolute():
        repair_summary = run_dir / repair_summary
    if not _is_within(repair_summary, run_dir) or not repair_summary.is_file():
        raise SopError("Saved repair retry batch is missing or outside the run")

    try:
        repair_items = json.loads(repair_summary.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SopError("Saved repair retry batch cannot be read") from exc
    if not isinstance(repair_items, list) or not repair_items:
        raise SopError("Saved repair retry batch has no scoped targets")
    repair_cycle = max(
        (
            int(item.get("repair_cycle") or 1)
            for item in repair_items
            if isinstance(item, Mapping)
        ),
        default=1,
    )
    if any(
        isinstance(item, Mapping)
        and bool(item.get("repair_verified"))
        and not item.get("original_package_dir")
        for item in repair_items
    ):
        repair_summary = _rebuild_verified_repair_retry_packages(
            config,
            run_dir=run_dir,
            manifest=manifest,
            retry_summary=repair_summary,
            cycle=repair_cycle,
        )

    recovery_summary = None
    recovery_value = state_artifacts.get("recovery_retry_batch")
    if recovery_value:
        candidate = Path(str(recovery_value))
        if not candidate.is_absolute():
            candidate = run_dir / candidate
        if not _is_within(candidate, run_dir) or not candidate.is_file():
            raise SopError(
                "Saved recovery retry batch is missing or outside the run"
            )
        recovery_summary = candidate

    if recovery_summary is None:
        scoped_retry = repair_summary
    else:
        scoped_retry = _merge_retry_batches(
            [repair_summary, recovery_summary],
            output_path=(
                run_dir
                / "repair"
                / f"combined-retry-batch-resume-cycle-{repair_cycle:02d}.json"
            ),
        )
        if scoped_retry is None:
            raise SopError("Verified repair has no current scoped retry targets")
    state_artifacts["repair_retry_batch"] = str(repair_summary)
    state_artifacts["scoped_retry_batch"] = str(scoped_retry)
    manifest_artifacts["repair_retry_batch"] = str(repair_summary)
    manifest_artifacts["scoped_retry_batch"] = str(scoped_retry)
    _write_json(manifest_path, manifest)
    _write_state(run_dir, state, config.output_root)
    return scoped_retry


def repair_daily_run(
    config: DailyConfig,
    *,
    run_dir: Path | None = None,
    retry_verified: bool = False,
    refresh_request_only: bool = False,
    recover_interrupted: bool = False,
) -> Path:
    """Resume a retained scoped coding repair without replaying the original batch."""
    if retry_verified and refresh_request_only:
        raise SopError(
            "--retry-verified and --refresh-request-only cannot be combined"
        )
    resolved_run_dir = resolve_run_dir(config, run_dir)
    state = _read_json_object(
        resolved_run_dir / STATE_FILE_NAME,
        "run state",
    )
    validate_run_inputs(config, state)
    phase = str(state.get("phase") or "")

    if phase == "repairing":
        if not recover_interrupted:
            raise SopError(
                "Run phase is 'repairing'; verify the prior repair process is no "
                "longer active, then use --recover-interrupted"
            )
        active_event = next(
            (
                event
                for event in reversed(state.get("history", []))
                if str(event.get("phase") or "") == "repairing"
            ),
            {},
        )
        repair_attempt = int(active_event.get("repair_attempt") or 0)
        repair_cycle = int(active_event.get("repair_cycle") or 0)
        already_recorded = any(
            int(item.get("attempt") or 0) == repair_attempt
            and int(item.get("cycle") or 0) == repair_cycle
            for item in state.get("repair_attempts", [])
        )
        if repair_attempt and not already_recorded:
            state.setdefault("repair_attempts", []).append(
                {
                    "attempt": repair_attempt,
                    "cycle": repair_cycle,
                    "status": "agent_unavailable",
                    "reason": "repair_agent_process_interrupted",
                    "result_path": None,
                    "changed_files": [],
                    "resumed": True,
                    "interrupted": True,
                }
            )
        consumed_cycles = _repair_cycle_count(state, resolved_run_dir)
        state["consumed_repair_cycles"] = consumed_cycles
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repair_unavailable",
            repair_cycle=repair_cycle,
            repair_attempt=repair_attempt,
            consumed_repair_cycles=consumed_cycles,
            repair_status="agent_unavailable",
            repair_reason="repair_agent_process_interrupted",
            repair_retryable=True,
            recovered_interrupted=True,
            stage="repair_interruption_recovery",
        )
        phase = "repair_unavailable"
    elif recover_interrupted:
        raise SopError(
            "--recover-interrupted is valid only when the run phase is 'repairing'"
        )

    if phase == "repair_verified":
        if refresh_request_only:
            manifest_path = resolved_run_dir / "pipeline-manifest.json"
            manifest = _read_json_object(manifest_path, "pipeline manifest")
            repair_cycle = _next_repair_cycle(state, resolved_run_dir) - 1
            repair_cycle = max(1, repair_cycle)
            source_request, source_request_path = _load_scoped_repair_request(
                state,
                manifest,
                resolved_run_dir,
                cycle=repair_cycle,
            )
            source_request, source_request_path = _persist_rebuilt_repair_request(
                state=state,
                manifest=manifest,
                run_dir=resolved_run_dir,
                output_root=config.output_root,
                request=source_request,
                source_path=source_request_path,
                cycle=repair_cycle,
            )
            if source_request.get("no_repairable_scope"):
                raise SopError(
                    "The current audit no longer contains a verified repair scope"
                )
            retry_summary = _write_verified_repair_retry_batch(
                config,
                state=state,
                manifest=manifest,
                run_dir=resolved_run_dir,
                request=source_request,
                cycle=repair_cycle,
            )
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_verified",
                repair_cycle=repair_cycle,
                scoped_retry_batch=str(retry_summary),
                repair_request_refreshed=True,
                stage="repair_scope_refresh",
            )
            _write_json(manifest_path, manifest)
            write_run_report(resolved_run_dir)
            print(
                "Refreshed verified repair request and safe scoped retry: "
                f"{retry_summary}"
            )
            return resolved_run_dir
        retry_summary = _refresh_verified_scoped_retry_batch(
            config,
            run_dir=resolved_run_dir,
            state=state,
        )
        if not retry_verified:
            print(
                "Repair is already verified; the scoped retry remains pending "
                "explicit --retry-verified authorization."
            )
            return resolved_run_dir
        return execute_daily_run(
            config,
            run_dir=resolved_run_dir,
            retry=True,
            _retry_summary_path=retry_summary,
        )

    allowed_phases = {
        "needs_repair",
        "repair_unavailable",
        "repair_failed",
        "repair_exhausted",
    }
    if phase not in allowed_phases:
        raise SopError(
            f"Run phase is '{phase}', not a resumable repair phase"
        )
    if not config.auto_repair.enabled:
        raise SopError("Automatic repair is disabled in the daily configuration")

    consumed_cycles = _repair_cycle_count(state, resolved_run_dir)
    repair_cycle = _next_repair_cycle(state, resolved_run_dir)
    manifest_path = resolved_run_dir / "pipeline-manifest.json"
    manifest = _read_json_object(manifest_path, "pipeline manifest")
    prior_verified = _latest_verified_repair_attempt(
        state,
        run_dir=resolved_run_dir,
    )
    if (
        prior_verified is not None
        and _verified_repair_is_newer_than_execution(
            state,
            prior_verified[1],
        )
    ):
        verified_attempt, verified_result, verified_request = prior_verified
        verified_cycle = int(verified_attempt.get("cycle") or 1)
        retry_summary = _write_verified_repair_retry_batch(
            config,
            state=state,
            manifest=manifest,
            run_dir=resolved_run_dir,
            request=verified_request,
            cycle=verified_cycle,
        )
        state["consumed_repair_cycles"] = consumed_cycles
        state.setdefault("repair_reconciliations", []).append(
            {
                "attempt": int(verified_attempt.get("attempt") or 0),
                "cycle": verified_cycle,
                "status": verified_result.get("status"),
                "result_path": verified_attempt.get("result_path"),
                "reconciled_at": _now(),
            }
        )
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repair_verified",
            repair_cycle=verified_cycle,
            repair_attempt=int(verified_attempt.get("attempt") or 0),
            consumed_repair_cycles=state["consumed_repair_cycles"],
            changed_files=[],
            scoped_retry_batch=str(retry_summary),
            repair_reconciled=True,
            stage="repair_result_reconciliation",
        )
        _write_json(manifest_path, manifest)
        write_run_report(resolved_run_dir)
        if retry_verified:
            return execute_daily_run(
                config,
                run_dir=resolved_run_dir,
                retry=True,
                _retry_summary_path=retry_summary,
            )
        print(
            "Recovered a previously verified no-diff repair result; "
            "the scoped retry remains pending explicit --retry-verified authorization."
        )
        return resolved_run_dir
    if consumed_cycles >= config.auto_repair.max_cycles:
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repair_exhausted",
            repair_cycle=repair_cycle,
            consumed_repair_cycles=consumed_cycles,
            repair_reason="maximum_repair_cycles_reached",
            stage="repair",
        )
        write_run_report(resolved_run_dir)
        raise SopError(
            "Automatic repair has no remaining code-repair cycles"
        )

    source_request, source_request_path = _load_scoped_repair_request(
        state,
        manifest,
        resolved_run_dir,
        cycle=repair_cycle,
    )
    source_request, source_request_path = _persist_rebuilt_repair_request(
        state=state,
        manifest=manifest,
        run_dir=resolved_run_dir,
        output_root=config.output_root,
        request=source_request,
        source_path=source_request_path,
        cycle=repair_cycle,
    )
    if source_request.get("no_repairable_scope"):
        state["consumed_repair_cycles"] = consumed_cycles
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "executed_with_blockers",
            repair_cycle=repair_cycle,
            consumed_repair_cycles=consumed_cycles,
            repair_status="not_required",
            repair_reason="current_audit_has_no_repairable_scope",
            stage="repair_scope_refresh",
        )
        _write_json(manifest_path, manifest)
        write_run_report(resolved_run_dir)
        print(
            "Current complete audit has no coding-repair scope; "
            f"superseded retained request: {source_request_path}"
        )
        return resolved_run_dir
    if refresh_request_only:
        if not source_request.get("request_refresh"):
            raise SopError(
                "The current audit produced no repairable scope to refresh"
            )
        state["consumed_repair_cycles"] = consumed_cycles
        _write_state(resolved_run_dir, state, config.output_root)
        print(f"Refreshed scoped repair request: {source_request_path}")
        return resolved_run_dir

    readiness = check_repair_agent_readiness(config.auto_repair)
    if (
        not readiness.ready
        and readiness.code == "repair_agent_authentication_failed"
    ):
        # A ChatGPT-backed Codex session can refresh its short-lived token
        # during the first remote probe while that probe still returns 401.
        # One immediate recheck is bounded and does not consume a repair cycle.
        readiness = check_repair_agent_readiness(config.auto_repair)
    if not readiness.ready:
        state["consumed_repair_cycles"] = consumed_cycles
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repair_unavailable",
            repair_cycle=repair_cycle,
            consumed_repair_cycles=consumed_cycles,
            repair_status="agent_unavailable",
            repair_reason=readiness.code,
            repair_retryable=False,
            stage="repair",
        )
        write_run_report(resolved_run_dir)
        raise SopError(
            "Automatic repair agent is unavailable; the current-audit scoped "
            "request was retained without starting Codex or consuming a repair cycle"
        )

    result: Mapping[str, Any] | None = None
    last_request = source_request
    last_request_path = source_request_path
    repair_duration_seconds = 0.0

    while consumed_cycles < config.auto_repair.max_cycles:
        repair_cycle = _next_repair_cycle(state, resolved_run_dir)
        budget_cycle = consumed_cycles + 1
        repair_attempt = _next_repair_attempt(state)
        active_request = {
            **dict(last_request),
            "cycle": repair_cycle,
            "budget_cycle": budget_cycle,
            "attempt": repair_attempt,
            "resumed_from": str(last_request_path),
        }
        request_path = _repair_request_path(
            resolved_run_dir,
            cycle=repair_cycle,
            attempt=repair_attempt,
        )
        _write_json(request_path, active_request)
        state.setdefault("artifacts", {})["repair_request"] = str(request_path)
        manifest.setdefault("artifacts", {})["repair_request"] = str(request_path)
        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repairing",
            repair_cycle=repair_cycle,
            repair_attempt=repair_attempt,
            consumed_repair_cycles=consumed_cycles,
            repair_request=str(request_path),
            resumed=True,
            stage="repair",
        )

        repair_started = time.monotonic()
        result = run_repair_cycle(
            config.auto_repair,
            root=config.root,
            run_dir=resolved_run_dir,
            request=active_request,
            auth_mode=readiness.auth_mode,
        )
        repair_duration_seconds = round(
            time.monotonic() - repair_started,
            3,
        )
        attempt_record = {
            "attempt": repair_attempt,
            "cycle": repair_cycle,
            "status": result.get("status"),
            "reason": result.get("reason"),
            "result_path": result.get("result_path"),
            "changed_files": result.get("changed_files", []),
            "duration_seconds": repair_duration_seconds,
            "resumed": True,
        }
        state.setdefault("repair_attempts", []).append(attempt_record)
        consumes_cycle = repair_result_consumes_cycle(result)
        if consumes_cycle:
            state.setdefault("repair_cycles", []).append(attempt_record)
            consumed_cycles += 1
        state["consumed_repair_cycles"] = consumed_cycles
        result_path = str(result.get("result_path") or "")
        if result_path:
            state["artifacts"]["repair_result"] = result_path
            manifest["artifacts"]["repair_result"] = result_path
        _write_state(resolved_run_dir, state, config.output_root)
        _write_json(manifest_path, manifest)

        if not consumes_cycle:
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_unavailable",
                repair_cycle=repair_cycle,
                repair_attempt=repair_attempt,
                consumed_repair_cycles=consumed_cycles,
                repair_status=result.get("status"),
                repair_reason=result.get("reason"),
                repair_retryable=bool(result.get("retryable")),
                stage="repair",
                duration_seconds=repair_duration_seconds,
            )
            write_run_report(resolved_run_dir)
            raise SopError(
                "Automatic repair agent became unavailable; the scoped request "
                "was preserved without consuming a repair cycle"
            )

        if repair_result_is_verified(result):
            retry_summary = _write_verified_repair_retry_batch(
                config,
                state=state,
                manifest=manifest,
                run_dir=resolved_run_dir,
                request=active_request,
                cycle=repair_cycle,
            )
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_verified",
                repair_cycle=repair_cycle,
                repair_attempt=repair_attempt,
                consumed_repair_cycles=consumed_cycles,
                changed_files=result.get("changed_files", []),
                scoped_retry_batch=str(retry_summary),
                stage="repair",
                duration_seconds=repair_duration_seconds,
            )
            _write_json(manifest_path, manifest)
            write_run_report(resolved_run_dir)
            if retry_verified:
                return execute_daily_run(
                    config,
                    run_dir=resolved_run_dir,
                    retry=True,
                    _retry_summary_path=retry_summary,
                )
            return resolved_run_dir

        if consumed_cycles >= config.auto_repair.max_cycles:
            _transition(
                resolved_run_dir,
                state,
                config.output_root,
                "repair_exhausted",
                repair_cycle=repair_cycle,
                repair_attempt=repair_attempt,
                consumed_repair_cycles=consumed_cycles,
                repair_status=result.get("status"),
                repair_reason=result.get("reason"),
                stage="repair",
                duration_seconds=repair_duration_seconds,
            )
            write_run_report(resolved_run_dir)
            raise SopError(
                "Automatic repair exhausted its bounded code-repair cycles "
                "without a verified change"
            )

        _transition(
            resolved_run_dir,
            state,
            config.output_root,
            "repair_failed",
            repair_cycle=repair_cycle,
            repair_attempt=repair_attempt,
            consumed_repair_cycles=consumed_cycles,
            repair_status=result.get("status"),
            repair_reason=result.get("reason"),
            will_retry=True,
            stage="repair",
            duration_seconds=repair_duration_seconds,
        )
        last_request = active_request
        last_request_path = request_path

    raise SopError("Automatic repair did not produce a verified change")


def _repair_attempt_result(
    attempt: Mapping[str, Any],
    *,
    run_dir: Path,
) -> Mapping[str, Any]:
    result_path_value = attempt.get("result_path")
    if not result_path_value:
        return attempt
    result_path = Path(str(result_path_value))
    if not result_path.is_absolute():
        result_path = run_dir / result_path
    if not _is_within(result_path, run_dir):
        return attempt
    result = _read_optional_json(result_path)
    return result or attempt


def _latest_verified_repair_attempt(
    state: Mapping[str, Any],
    *,
    run_dir: Path,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]] | None:
    attempts = state.get("repair_attempts")
    if not isinstance(attempts, list):
        return None
    for attempt in reversed(attempts):
        if not isinstance(attempt, Mapping):
            continue
        result = _repair_attempt_result(attempt, run_dir=run_dir)
        if (
            not repair_result_is_verified(result)
            or list(result.get("changed_files") or [])
        ):
            continue
        verification = result.get("verification")
        if not isinstance(verification, list) or not verification:
            continue
        if any(
            not isinstance(item, Mapping) or item.get("status") != "passed"
            for item in verification
        ):
            continue
        attempt_number = int(attempt.get("attempt") or 0)
        cycle = int(attempt.get("cycle") or 0)
        if attempt_number <= 0 or cycle <= 0:
            continue
        request_path = _repair_request_path(
            run_dir,
            cycle=cycle,
            attempt=attempt_number,
        )
        request = _read_optional_json(request_path)
        if not isinstance(request, Mapping):
            continue
        return attempt, result, request
    return None


def _repair_cycle_count(
    state: Mapping[str, Any],
    run_dir: Path,
) -> int:
    latest_verified_attempt = 0
    attempts = state.get("repair_attempts")
    if isinstance(attempts, list):
        for item in attempts:
            if not isinstance(item, Mapping):
                continue
            result = _repair_attempt_result(item, run_dir=run_dir)
            if (
                repair_result_is_verified(result)
                and not _verified_repair_is_newer_than_execution(
                    state,
                    result,
                )
            ):
                latest_verified_attempt = max(
                    latest_verified_attempt,
                    int(item.get("attempt") or item.get("cycle") or 0),
                )
    cycles = state.get("repair_cycles")
    if not isinstance(cycles, list):
        return 0
    return sum(
        repair_result_consumes_cycle(
            _repair_attempt_result(item, run_dir=run_dir)
        )
        for item in cycles
        if isinstance(item, Mapping)
        and int(item.get("attempt") or item.get("cycle") or 0)
        > latest_verified_attempt
    )


def _next_repair_cycle(
    state: Mapping[str, Any],
    run_dir: Path,
) -> int:
    """Return a monotonic artifact cycle independent of the current budget epoch."""
    recorded: list[Mapping[str, Any]] = []
    for key in ("repair_attempts", "repair_cycles"):
        values = state.get(key)
        if isinstance(values, list):
            recorded.extend(item for item in values if isinstance(item, Mapping))
    return max(
        (
            int(item.get("cycle") or 0)
            for item in recorded
            if repair_result_consumes_cycle(
                _repair_attempt_result(item, run_dir=run_dir)
            )
        ),
        default=0,
    ) + 1


def _verified_repair_is_newer_than_execution(
    state: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    """Only reconcile a verified result that has not already been browser-tested."""
    verified_at = _parse_datetime(result.get("finished_at"))
    execution_at = _last_execution_finished_at(state)
    if verified_at is None:
        return True
    return execution_at is None or verified_at > execution_at


def _next_repair_attempt(state: Mapping[str, Any]) -> int:
    attempts = state.get("repair_attempts")
    cycles = state.get("repair_cycles")
    attempt_records = (
        [item for item in attempts if isinstance(item, Mapping)]
        if isinstance(attempts, list)
        else []
    )
    cycle_records = (
        [item for item in cycles if isinstance(item, Mapping)]
        if isinstance(cycles, list)
        else []
    )
    recorded = [*attempt_records, *cycle_records]
    explicit = [
        int(item.get("attempt") or item.get("cycle") or 0)
        for item in recorded
    ]
    return max(
        [len(attempt_records), len(cycle_records), *explicit],
        default=0,
    ) + 1


def _repair_request_path(
    run_dir: Path,
    *,
    cycle: int,
    attempt: int,
    incremental: bool = False,
) -> Path:
    prefix = "repair-request-incremental" if incremental else "repair-request"
    if cycle == attempt:
        name = f"{prefix}-cycle-{cycle:02d}.json"
    else:
        name = f"{prefix}-attempt-{attempt:02d}-cycle-{cycle:02d}.json"
    return run_dir / "repair" / name


def _load_scoped_repair_request(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    run_dir: Path,
    *,
    cycle: int,
) -> tuple[dict[str, Any], Path]:
    retained_request: dict[str, Any] | None = None
    retained_path: Path | None = None
    artifact_values: list[Any] = []
    for container in (state.get("artifacts"), manifest.get("artifacts")):
        if isinstance(container, Mapping):
            artifact_values.append(container.get("repair_request"))
    for value in artifact_values:
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            path = run_dir / path
        if not _is_within(path, run_dir):
            raise SopError(
                "Retained repair request is outside the selected run directory"
            )
        if path.is_file():
            retained_request = _read_json_object(path, "repair request")
            retained_path = path
            break

    audit_values: list[Any] = []
    for container in (state.get("artifacts"), manifest.get("artifacts")):
        if isinstance(container, Mapping):
            audit_values.append(container.get("execution_audit"))
    audit_values.append(run_dir / "execution-audit.json")
    for value in audit_values:
        if not value:
            continue
        path = Path(str(value))
        if not path.is_absolute():
            path = run_dir / path
        if not _is_within(path, run_dir) or not path.is_file():
            continue
        audit = _read_json_object(path, "execution audit")
        request = build_repair_request(
            audit,
            run_dir=run_dir,
            cycle=cycle,
        )
        if request is not None:
            request["rebuilt_from_audit"] = str(path)
            if retained_path is not None:
                request["supersedes_retained_request"] = str(retained_path)
            return request, path
        progress = audit.get("progress")
        if isinstance(progress, Mapping) and progress.get("complete") is True:
            constraints = (
                dict(retained_request.get("constraints") or {})
                if isinstance(retained_request, Mapping)
                else {}
            )
            no_scope_request: dict[str, Any] = {
                "schema_version": 1,
                "cycle": cycle,
                "findings": [],
                "retry_targets": [],
                "constraints": constraints,
                "no_repairable_scope": True,
                "rebuilt_from_audit": str(path),
            }
            if retained_path is not None:
                no_scope_request["supersedes_retained_request"] = str(
                    retained_path
                )
            return no_scope_request, path
    if retained_request is not None and retained_path is not None:
        return retained_request, retained_path
    raise SopError("No retained scoped repair request is available for this run")


def _persist_rebuilt_repair_request(
    *,
    state: dict[str, Any],
    manifest: dict[str, Any],
    run_dir: Path,
    output_root: Path,
    request: Mapping[str, Any],
    source_path: Path,
    cycle: int,
) -> tuple[dict[str, Any], Path]:
    """Persist a current-audit request before repair-agent readiness is checked."""
    if not request.get("rebuilt_from_audit"):
        return dict(request), source_path

    refresh_records = state.get("repair_request_refreshes")
    records = (
        [item for item in refresh_records if isinstance(item, Mapping)]
        if isinstance(refresh_records, list)
        else []
    )
    explicit = [
        int(item.get("refresh") or 0)
        for item in records
    ]
    refresh = max([len(records), *explicit], default=0) + 1
    request_path = (
        run_dir
        / "repair"
        / f"repair-request-refresh-{refresh:02d}-cycle-{cycle:02d}.json"
    )
    payload = {
        **dict(request),
        "cycle": cycle,
        "request_refresh": refresh,
        "refreshed_at": _now(),
        "source_request": str(source_path),
    }
    _write_json(request_path, payload)
    state.setdefault("artifacts", {})["repair_request"] = str(request_path)
    manifest.setdefault("artifacts", {})["repair_request"] = str(request_path)
    state.setdefault("repair_request_refreshes", []).append(
        {
            "refresh": refresh,
            "cycle": cycle,
            "request_path": str(request_path),
            "rebuilt_from_audit": payload.get("rebuilt_from_audit"),
            "supersedes_retained_request": payload.get(
                "supersedes_retained_request"
            ),
            "created_at": payload["refreshed_at"],
        }
    )
    _write_state(run_dir, state, output_root)
    _write_json(run_dir / "pipeline-manifest.json", manifest)
    return payload, request_path


def _saved_scoped_retry_batch(
    state: Mapping[str, Any],
    run_dir: Path,
) -> Path:
    artifacts = state.get("artifacts")
    value = None
    if isinstance(artifacts, Mapping):
        value = (
            artifacts.get("scoped_retry_batch")
            or artifacts.get("repair_retry_batch")
        )
    if not value:
        raise SopError("Verified repair has no saved scoped retry batch")
    path = Path(str(value))
    if not path.is_absolute():
        path = run_dir / path
    if not _is_within(path, run_dir) or not path.is_file():
        raise SopError("Saved scoped retry batch is missing or outside the run")
    return path


def _rebuild_verified_repair_retry_packages(
    config: DailyConfig,
    *,
    run_dir: Path,
    manifest: Mapping[str, Any],
    retry_summary: Path,
    cycle: int,
) -> Path:
    """Rebuild verified repair targets with current code and approved facts.

    Repair verification proves the source change, but prepared runtime packages
    embed both the runtime implementation and a snapshot of the approved
    profile.  Reusing the original package would therefore retry stale code or
    stale facts.  Rebuild only the already-scoped targets in the main process;
    the isolated Repair Agent still never receives private profile data.
    """
    from job_agent.cli import _job_from_dict, _prepare_application_package

    try:
        selected = json.loads(retry_summary.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SopError("Verified repair retry batch cannot be read") from exc
    if not isinstance(selected, list) or not selected:
        raise SopError("Verified repair retry batch has no scoped targets")

    artifacts = manifest.get("artifacts")
    jobs_value = artifacts.get("jobs") if isinstance(artifacts, Mapping) else None
    if not jobs_value:
        raise SopError("Pipeline manifest does not contain normalized jobs")
    jobs_path = _artifact_path(jobs_value, config.root)
    if not _is_within(jobs_path, run_dir) or not jobs_path.is_file():
        raise SopError("Normalized jobs are missing or belong to another run")
    try:
        raw_jobs_payload = json.loads(jobs_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SopError("Normalized jobs cannot be read for scoped rebuilding") from exc
    if not isinstance(raw_jobs_payload, list):
        raise SopError("Normalized jobs must be a list for scoped rebuilding")
    raw_jobs = [
        item for item in raw_jobs_payload if isinstance(item, Mapping)
    ]

    rebuilt: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, Mapping):
            raise SopError("Verified repair retry target is not an object")
        apply_url = str(item.get("apply_url") or "").strip()
        company = str(item.get("company") or "").strip().casefold()
        title = str(item.get("title") or "").strip().casefold()
        raw_job = next(
            (
                candidate
                for candidate in raw_jobs
                if apply_url
                and str(
                    candidate.get("apply_url")
                    or candidate.get("source_url")
                    or ""
                ).strip()
                == apply_url
            ),
            None,
        )
        if raw_job is None:
            matches = [
                candidate
                for candidate in raw_jobs
                if str(candidate.get("company") or "").strip().casefold()
                == company
                and str(candidate.get("title") or "").strip().casefold()
                == title
            ]
            raw_job = matches[0] if len(matches) == 1 else None
        if raw_job is None:
            raise SopError(
                "Original normalized job is unavailable for verified repair "
                f"target {item.get('application_id') or company or title}"
            )

        application_id = re.sub(
            r"[^A-Za-z0-9._-]+",
            "-",
            str(item.get("application_id") or "unknown"),
        ).strip("-") or "unknown"
        base_dir = (
            run_dir
            / "repair"
            / f"rebuilt-cycle-{cycle:02d}-application-{application_id}"
        )
        package_dir = base_dir
        suffix = 2
        while package_dir.exists():
            package_dir = base_dir.with_name(
                f"{base_dir.name}-{suffix:02d}"
            )
            suffix += 1

        summary = dict(
            _prepare_application_package(
                _job_from_dict(dict(raw_job)),
                package_dir,
                resume_source_dir=config.resume_source_dir,
                db=config.database,
                profile=config.profile,
                sensitive_kb=config.sensitive_kb,
                use_llm=config.use_llm,
                runtime_headless=config.browser_headless,
                profile_vector_db=config.profile_vector_db,
                required_resume_pdf=config.required_resume_pdf,
            )
        )
        expected_application_id = str(item.get("application_id") or "")
        rebuilt_application_id = str(summary.get("application_id") or "")
        if (
            expected_application_id
            and rebuilt_application_id
            and rebuilt_application_id != expected_application_id
        ):
            raise SopError(
                "Scoped repair rebuild changed the tracked application ID"
            )
        for key in (
            "retry",
            "repair_verified",
            "retry_scope",
            "repair_cycle",
            "original_terminal_status",
        ):
            if item.get(key) is not None:
                summary[key] = item.get(key)
        if expected_application_id and not rebuilt_application_id:
            summary["application_id"] = expected_application_id
        summary["original_package_dir"] = str(item.get("package_dir") or "")
        summary_path = package_dir / "repair-package-summary.json"
        _write_json(summary_path, summary)
        rebuilt.append(summary)

    _write_json_list(retry_summary, rebuilt)
    return retry_summary


def _write_verified_repair_retry_batch(
    config: DailyConfig,
    *,
    state: dict[str, Any],
    manifest: dict[str, Any],
    run_dir: Path,
    request: Mapping[str, Any],
    cycle: int,
) -> Path:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts.get("batch_summary"):
        raise SopError("Pipeline manifest does not contain a batch summary")
    batch_summary = _artifact_path(artifacts["batch_summary"], config.root)
    if not _is_within(batch_summary, run_dir) or not batch_summary.is_file():
        raise SopError("Batch summary is missing or belongs to another run")
    retry_summary = write_retry_batch(
        batch_summary,
        request=request,
        output_path=(
            run_dir / "repair" / f"retry-batch-cycle-{cycle:02d}.json"
        ),
    )
    if retry_summary is None:
        raise SopError("Verified repair has no safe, scoped applications to retry")
    retry_summary = _rebuild_verified_repair_retry_packages(
        config,
        run_dir=run_dir,
        manifest=manifest,
        retry_summary=retry_summary,
        cycle=cycle,
    )
    recovery_retry = None
    state_artifacts = state.get("artifacts")
    if isinstance(state_artifacts, Mapping):
        recovery_value = state_artifacts.get("recovery_retry_batch")
        if recovery_value:
            candidate = Path(str(recovery_value))
            if not candidate.is_absolute():
                candidate = run_dir / candidate
            if _is_within(candidate, run_dir) and candidate.is_file():
                recovery_retry = candidate
    scoped_retry = _merge_retry_batches(
        [retry_summary, recovery_retry],
        output_path=(
            run_dir / "repair" / f"combined-retry-batch-cycle-{cycle:02d}.json"
        ),
    )
    if scoped_retry is None:
        scoped_retry = retry_summary
    state.setdefault("artifacts", {})["repair_retry_batch"] = str(retry_summary)
    state["artifacts"]["scoped_retry_batch"] = str(scoped_retry)
    manifest.setdefault("artifacts", {})["repair_retry_batch"] = str(retry_summary)
    manifest["artifacts"]["scoped_retry_batch"] = str(scoped_retry)
    _write_state(run_dir, state, config.output_root)
    return scoped_retry


def _repair_target_key(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item.get("package_dir") or "").strip(),
        str(item.get("company") or "").strip().casefold(),
        str(item.get("title") or "").strip().casefold(),
    )


def _subtract_repair_request(
    request: Mapping[str, Any],
    completed: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    completed_keys = {
        _repair_target_key(item)
        for prior in completed
        for item in prior.get("retry_targets", [])
        if isinstance(item, Mapping)
    }
    findings = [
        dict(item)
        for item in request.get("findings", [])
        if isinstance(item, Mapping)
        and _repair_target_key(item) not in completed_keys
    ]
    retry_targets = [
        dict(item)
        for item in request.get("retry_targets", [])
        if isinstance(item, Mapping)
        and _repair_target_key(item) not in completed_keys
    ]
    if not findings or not retry_targets:
        return None
    return {
        **dict(request),
        "findings": findings,
        "retry_targets": retry_targets,
    }


def _merge_repair_requests(
    requests: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    valid = [request for request in requests if isinstance(request, Mapping)]
    if not valid:
        return None
    findings: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    seen_findings: set[tuple[str, str, str]] = set()
    seen_targets: set[tuple[str, str, str]] = set()
    for request in valid:
        for item in request.get("findings", []):
            if not isinstance(item, Mapping):
                continue
            key = _repair_target_key(item)
            if key not in seen_findings:
                seen_findings.add(key)
                findings.append(dict(item))
        for item in request.get("retry_targets", []):
            if not isinstance(item, Mapping):
                continue
            key = _repair_target_key(item)
            if key not in seen_targets:
                seen_targets.add(key)
                targets.append(dict(item))
    if not findings or not targets:
        return None
    merged = dict(valid[-1])
    merged["findings"] = findings
    merged["retry_targets"] = targets
    return merged


def _merge_retry_batches(
    paths: Sequence[Path | None],
    *,
    output_path: Path,
) -> Path | None:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for path in paths:
        if path is None or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            key = (
                str(item.get("package_dir") or ""),
                str(item.get("application_id") or ""),
                str(item.get("company") or "").casefold(),
                str(item.get("title") or "").casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    if not merged:
        return None
    _write_json_list(output_path, merged)
    return output_path


def build_prepare_command(config: DailyConfig, run_dir: Path) -> list[str]:
    cli = find_job_agent_cli(config.root)
    if cli is None:
        raise SopError("job-agent CLI is unavailable")
    command = [
        str(cli),
        "pipeline",
        "run",
        str(config.source_config),
        "--out-dir",
        str(run_dir),
        "--min-score",
        str(config.min_score),
        "--limit",
        str(config.limit),
        "--profile",
        str(config.profile),
        "--sensitive-kb",
        str(config.sensitive_kb),
        "--db",
        str(config.database),
    ]
    if config.required_resume_pdf is not None:
        command.extend(["--required-resume-pdf", str(config.required_resume_pdf)])
    elif config.resume_source_dir is not None:
        command.extend(["--resume-source-dir", str(config.resume_source_dir)])
    if config.profile_vector_db is not None:
        command.extend(["--profile-vector-db", str(config.profile_vector_db)])
    if config.use_llm:
        command.append("--use-llm")
    return command


def build_execute_command(
    config: DailyConfig,
    *,
    summary_path: Path,
    audit_path: Path,
    preflight_path: Path,
    retry: bool,
    resume_existing_audit: bool = False,
) -> list[str]:
    cli = find_job_agent_cli(config.root)
    if cli is None:
        raise SopError("job-agent CLI is unavailable")
    command = [
        str(cli),
        "applications",
        "execute-batch",
        str(summary_path),
        "--audit-out",
        str(audit_path),
        "--resume-preflight-out",
        str(preflight_path),
        "--timeout-seconds",
        str(config.timeout_seconds),
        "--db",
        str(config.database),
        "--headless" if config.browser_headless else "--headed",
        "--llm-answers" if config.llm_answers else "--no-llm-answers",
    ]
    if config.required_resume_pdf is not None:
        command.extend(["--required-resume-pdf", str(config.required_resume_pdf)])
    elif config.resume_source_dir is not None:
        command.extend(
            ["--required-resume-source-dir", str(config.resume_source_dir)]
        )
    if retry:
        command.append("--retry-prior-terminal-outcome")
    if resume_existing_audit:
        command.append("--resume-existing-audit")
    return command


def create_run_dir(output_root: Path, *, now: datetime | None = None) -> Path:
    timestamp = now or datetime.now().astimezone()
    day_dir = output_root / timestamp.strftime("%Y-%m-%d")
    base_name = timestamp.strftime("%H%M%S")
    candidate = day_dir / base_name
    index = 2
    while candidate.exists():
        candidate = day_dir / f"{base_name}-{index:02d}"
        index += 1
    candidate.mkdir(parents=True)
    return candidate


def fingerprint_inputs(config: DailyConfig) -> dict[str, str]:
    fingerprints = {
        "source_config": _sha256(config.source_config),
        "profile": _sha256(config.profile),
        "sensitive_kb": _sha256(config.sensitive_kb),
    }
    if config.required_resume_pdf is not None:
        fingerprints["required_resume_pdf"] = _sha256(config.required_resume_pdf)
    return fingerprints


def validate_run_inputs(
    config: DailyConfig,
    state: Mapping[str, Any],
) -> None:
    recorded_path = Path(str(state.get("config_path") or "")).expanduser()
    if not recorded_path.is_absolute():
        recorded_path = config.root / recorded_path
    if recorded_path.resolve() != config.config_path.resolve():
        raise SopError(
            "The prepared run belongs to a different daily config; prepare a new run"
        )

    recorded_config_sha = str(state.get("config_sha256") or "")
    if recorded_config_sha != _sha256(config.config_path):
        raise SopError(
            "Daily config changed after preparation; prepare a new run before execution"
        )

    recorded_inputs = state.get("input_sha256")
    if not isinstance(recorded_inputs, dict):
        raise SopError(
            "Prepared run has no input fingerprints; prepare a new run with the current SOP"
        )
    current_inputs = fingerprint_inputs(config)
    changed = sorted(
        key
        for key in set(recorded_inputs) | set(current_inputs)
        if recorded_inputs.get(key) != current_inputs.get(key)
    )
    if changed:
        raise SopError(
            "Prepared inputs changed after packaging: "
            + ", ".join(changed)
            + ". Prepare a new run."
        )


def resolve_run_dir(config: DailyConfig, run_dir: Path | None) -> Path:
    if run_dir is not None:
        resolved = run_dir if run_dir.is_absolute() else config.root / run_dir
    else:
        latest_path = config.output_root / LATEST_FILE_NAME
        latest = _read_json_object(latest_path, "latest run pointer")
        value = latest.get("run_dir")
        if not value:
            raise SopError(f"Latest run pointer has no run_dir: {latest_path}")
        resolved = Path(str(value))
    if not resolved.is_dir():
        raise SopError(f"Run directory does not exist: {resolved}")
    return resolved.resolve()


def _evaluation_policy_from_snapshot(settings: Mapping[str, Any]) -> EvaluationPolicy:
    raw = settings.get("evaluation")
    if not isinstance(raw, Mapping):
        return EvaluationPolicy()
    try:
        return _read_evaluation_policy({"evaluation": raw})
    except SopError:
        # Historical run states remain reportable even if they predate a policy.
        return EvaluationPolicy()


def _build_evaluation_metrics(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
    *,
    settings: Mapping[str, Any],
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the registered read-only evaluator through Agent Core."""
    policy = _evaluation_policy_from_snapshot(settings)
    evaluator = JobApplicationRoundEvaluator(policy)
    core: AgentCore
    application_rows = audit.get("applications")
    applications = (
        application_rows if isinstance(application_rows, list) else []
    )
    owner = next(
        (
            item
            for item in applications
            if isinstance(item, Mapping)
            and latest_trajectory_observation(
                item.get("package_dir"),
                exclude_stages={"evaluation"},
            ) is not None
        ),
        None,
    )
    if isinstance(owner, Mapping):
        initial_observation = latest_trajectory_observation(
            owner.get("package_dir"),
            exclude_stages={"evaluation"},
        )
        assert initial_observation is not None
        agent = JobApplicationAgent.resume_runtime(
            name="job-application-agent",
            llm=DeterministicSessionLLM(),
            initial_observation=initial_observation,
            agent_runtime_id=str(
                owner.get("agent_runtime_id")
                or f"application-{owner.get('application_id') or 'unknown'}"
            ),
            tool_registry=ToolRegistry(),
        )
        core = agent.agent_core
    else:
        core = AgentCore(
            ControlledExecution(ToolRegistry()),
            evaluation_history_limit=1,
        )
    core.register_evaluator(evaluator.name, evaluator)
    result = core.evaluate_round(
        evaluator.name,
        {
            "state": dict(state),
            "manifest": dict(manifest),
            "audit": dict(audit),
        },
        round_id=str(state.get("run_id") or "unknown"),
        targets=policy.targets(),
        metadata={
            "phase": str(state.get("phase") or "unknown"),
            "read_only": True,
        },
    )
    payload = evaluation_result_to_dict(result)
    daily_target = state.get("daily_target")
    if isinstance(daily_target, Mapping):
        payload["accounting"] = {
            "local_date": daily_target.get("local_date"),
            "timezone": daily_target.get("timezone"),
            "date_source": state.get(
                "accounting_date_source",
                "current_local_date",
            ),
        }
    return payload


class _AgentEvaluationObservationTool(Tool):
    def __init__(self, evaluation: Mapping[str, Any]) -> None:
        super().__init__(
            "agent_evaluation_observe",
            "Consume the read-only aggregate evaluation for this application.",
            effect=ToolEffect.OBSERVE,
        )
        self._evaluation = dict(evaluation)

    def run(self, _parameters: dict[str, Any]) -> dict[str, Any]:
        agent_core = self._evaluation.get("agent_core")
        return {
            "status": (
                str(agent_core.get("status") or "unknown")
                if isinstance(agent_core, Mapping)
                else "unknown"
            ),
            "evaluation_id": (
                agent_core.get("evaluation_id")
                if isinstance(agent_core, Mapping)
                else None
            ),
        }

    def get_parameters(self) -> list[ToolParameter]:
        return []


def _application_evaluation_loop(
    record: Mapping[str, Any],
    evaluation: Mapping[str, Any],
):
    initial_observation = latest_trajectory_observation(
        record.get("package_dir"),
        exclude_stages={"evaluation"},
    )
    if initial_observation is None:
        return None
    registry = ToolRegistry()
    registry.register_tool(_AgentEvaluationObservationTool(evaluation))
    agent = JobApplicationAgent.resume_runtime(
        name="job-application-agent",
        llm=DeterministicSessionLLM(),
        initial_observation=initial_observation,
        agent_runtime_id=str(
            record.get("agent_runtime_id")
            or f"application-{record.get('application_id') or 'unknown'}"
        ),
        tool_registry=registry,
    )
    return agent.continue_with_tools(
        "Observe the immutable daily Agent evaluation.",
        [
            ToolCall(
                tool_name="agent_evaluation_observe",
                parameters={},
                effect=ToolEffect.OBSERVE,
                purpose=(
                    "Add the read-only aggregate evaluation as a new "
                    "Observation without changing application state."
                ),
                context={
                    "phase": "evaluation",
                    "read_only": True,
                },
            )
        ],
        memory_query="job application round evaluation",
    )


def _format_evaluation_rate(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{value * 100:.1f}%"


def _format_evaluation_ratio(
    numerator: Any,
    denominator: Any,
    rate: Any,
) -> str:
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        return "n/a"
    return f"{numerator}/{denominator} ({_format_evaluation_rate(rate)})"


def _trajectory_handoff_status(
    stages: Mapping[str, Any],
) -> dict[str, Any]:
    preparation = stages.get("preparation")
    execution = stages.get("execution")
    if not isinstance(preparation, list) or not preparation:
        return {"status": "missing_preparation"}
    last_prepare = preparation[-1]
    prepare_observations = (
        last_prepare.get("observations")
        if isinstance(last_prepare, Mapping)
        else None
    )
    prepare_id = (
        str(prepare_observations[-1].get("observation_id") or "")
        if isinstance(prepare_observations, list)
        and prepare_observations
        and isinstance(prepare_observations[-1], Mapping)
        else ""
    )
    if not isinstance(execution, Mapping):
        return {
            "status": "not_executed",
            "preparation_observation_id": prepare_id,
        }
    preflight = execution.get("preflight")
    rounds = (
        preflight.get("rounds")
        if isinstance(preflight, Mapping)
        and isinstance(preflight.get("rounds"), list)
        else execution.get("rounds")
    )
    first_input = (
        rounds[0].get("input_observation")
        if isinstance(rounds, list)
        and rounds
        and isinstance(rounds[0], Mapping)
        else None
    )
    execution_id = (
        str(first_input.get("observation_id") or "")
        if isinstance(first_input, Mapping)
        else ""
    )
    return {
        "status": (
            "continuous"
            if prepare_id and prepare_id == execution_id
            else "disconnected"
        ),
        "preparation_observation_id": prepare_id,
        "execution_input_observation_id": execution_id,
    }


def _write_agent_runtime_trace(
    run_dir: Path,
    *,
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    audit: Mapping[str, Any],
    metrics: Mapping[str, Any],
) -> Path:
    application_rows = audit.get("applications")
    applications = (
        application_rows if isinstance(application_rows, list) else []
    )
    indexed: list[dict[str, Any]] = []
    handoff_statuses: list[str] = []
    evaluation_stage = {
        "agent_core": (
            dict(metrics.get("agent_core") or {})
            if isinstance(metrics.get("agent_core"), Mapping)
            else {}
        ),
        "assessment": (
            dict(metrics.get("assessment") or {})
            if isinstance(metrics.get("assessment"), Mapping)
            else {}
        ),
    }
    for record in applications:
        if not isinstance(record, Mapping):
            continue
        package_value = str(record.get("package_dir") or "")
        trajectory_path = (
            Path(package_value) / "agent-trajectory.json"
            if package_value
            else None
        )
        trajectory = None
        if trajectory_path is not None:
            try:
                trajectory_path.resolve().relative_to(run_dir.resolve())
            except (OSError, ValueError):
                trajectory_path = None
        if trajectory_path is not None and trajectory_path.is_file():
            trajectory = _read_optional_json(trajectory_path)
        stages = (
            trajectory.get("stages")
            if isinstance(trajectory, Mapping)
            else None
        )
        if isinstance(stages, dict):
            evaluation_loop = _application_evaluation_loop(
                record,
                evaluation_stage,
            )
            stages["evaluation"] = {
                **evaluation_stage,
                "agent_loop": (
                    agent_loop_result_to_dict(evaluation_loop)
                    if evaluation_loop is not None
                    else None
                ),
            }
            _write_json(trajectory_path, trajectory)
            handoff = _trajectory_handoff_status(stages)
            stage_names = sorted(stages)
        else:
            handoff = {"status": "trajectory_missing"}
            stage_names = []
        handoff_statuses.append(str(handoff.get("status") or "unknown"))
        indexed.append(
            {
                "application_id": record.get("application_id"),
                "agent_runtime_id": record.get("agent_runtime_id"),
                "company": record.get("company"),
                "title": record.get("title"),
                "terminal_status": record.get("status"),
                "trajectory_path": (
                    str(trajectory_path)
                    if trajectory_path is not None
                    else None
                ),
                "stages": stage_names,
                "handoff": handoff,
            }
        )

    runtime = manifest.get("agent_runtime")
    trace = {
        "schema_version": 1,
        "run_id": state.get("run_id"),
        "phase": state.get("phase"),
        "closed_loop": True,
        "pipeline": (
            runtime.get("pipeline")
            if isinstance(runtime, Mapping)
            else None
        ),
        "browser_execution": (
            audit.get("agent_runtime")
            if isinstance(audit.get("agent_runtime"), Mapping)
            else None
        ),
        "applications": indexed,
        "continuity": {
            "continuous": handoff_statuses.count("continuous"),
            "not_executed": handoff_statuses.count("not_executed"),
            "disconnected": handoff_statuses.count("disconnected"),
            "missing": sum(
                status
                not in {"continuous", "not_executed", "disconnected"}
                for status in handoff_statuses
            ),
        },
        "evaluation": evaluation_stage,
        "repair_attempts": list(state.get("repair_attempts") or []),
    }
    path = run_dir / AGENT_RUNTIME_TRACE_FILE_NAME
    _write_json(path, trace)
    return path


def _execution_application_key(
    record: Mapping[str, Any],
) -> tuple[str, ...]:
    application_id = str(record.get("application_id") or "").strip()
    if application_id:
        return ("application_id", application_id)
    return (
        "application",
        str(record.get("apply_url") or "").strip(),
        str(record.get("company") or "").strip().casefold(),
        str(record.get("title") or "").strip().casefold(),
    )


def _execution_attempt_audits(
    state: Mapping[str, Any],
    *,
    run_dir: Path,
    root: Path,
) -> list[tuple[Path, dict[str, Any]]]:
    attempts = state.get("execution_attempts")
    attempt_rows = (
        [item for item in attempts if isinstance(item, Mapping)]
        if isinstance(attempts, list)
        else []
    )
    audits: list[tuple[Path, dict[str, Any]]] = []
    registered: set[Path] = set()
    for attempt in attempt_rows:
        raw_path = str(attempt.get("audit") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        payload = _read_optional_json(resolved)
        if payload:
            audits.append((resolved, payload))
            registered.add(resolved)

    # A worker can be terminated after it has atomically written an audit but
    # before the parent process records the execution attempt in run-state.
    # Keep those same-run audit files visible to reports and recovery planning.
    # Only root-level execution audit files are considered; package-local
    # audits and unrelated artifacts must never be folded into the run.
    audit_candidates = [run_dir / "execution-audit.json"]
    audit_candidates.extend(sorted(run_dir.glob("execution-audit-*.json")))
    for candidate in audit_candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(run_dir.resolve())
        except (OSError, ValueError):
            continue
        if resolved in registered:
            continue
        payload = _read_optional_json(resolved)
        if not isinstance(payload, Mapping):
            continue
        records = payload.get("applications")
        progress = payload.get("progress")
        if not isinstance(records, list) or not isinstance(progress, Mapping):
            continue
        if not records and not progress.get("planned"):
            continue
        audits.append((resolved, dict(payload)))
        registered.add(resolved)
    return audits


def _persist_recovery_annotations(
    state: Mapping[str, Any],
    *,
    run_dir: Path,
    root: Path,
    fallback_path: Path,
    recovery_audit: Mapping[str, Any],
) -> None:
    """Write Recovery metadata back without flattening attempt audit history."""
    audits = _execution_attempt_audits(
        state,
        run_dir=run_dir,
        root=root,
    )
    if not audits:
        _write_json(fallback_path, recovery_audit)
        return

    updates: dict[tuple[str, ...], dict[str, Any]] = {}
    records = recovery_audit.get("applications")
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, Mapping):
                continue
            annotation = {
                key: record.get(key)
                for key in ("recovery_plan", "recovery_execution")
                if record.get(key) is not None
            }
            if annotation:
                updates[_execution_application_key(record)] = annotation

    owners: dict[tuple[str, ...], tuple[Path, dict[str, Any], int]] = {}
    for path, payload in audits:
        payload_records = payload.get("applications")
        if not isinstance(payload_records, list):
            continue
        for index, record in enumerate(payload_records):
            if isinstance(record, Mapping):
                owners[_execution_application_key(record)] = (
                    path,
                    payload,
                    index,
                )

    touched: dict[Path, dict[str, Any]] = {}
    for key, annotation in updates.items():
        owner = owners.get(key)
        if owner is None:
            continue
        path, payload, index = owner
        payload_records = payload.get("applications")
        if not isinstance(payload_records, list):
            continue
        updated_record = dict(payload_records[index])
        updated_record.update(annotation)
        payload_records[index] = updated_record
        touched[path] = payload
    for path, payload in touched.items():
        _write_json(path, payload)


def _execution_audit_for_report(
    state: Mapping[str, Any],
    *,
    run_dir: Path,
    root: Path,
    fallback: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge completed execution attempts by stable application identity."""
    audits = _execution_attempt_audits(
        state,
        run_dir=run_dir,
        root=root,
    )
    if not audits:
        return dict(fallback)
    if len(audits) == 1:
        return dict(audits[0][1])

    merged = dict(audits[-1][1])
    applications_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    planned_counts: list[int] = []
    all_complete = True
    runtime_applications: list[Any] = []
    runtime_closed_loop = True
    for _path, audit in audits:
        progress = audit.get("progress")
        if isinstance(progress, Mapping):
            try:
                planned_counts.append(int(progress.get("planned") or 0))
            except (TypeError, ValueError):
                pass
            all_complete = all_complete and bool(progress.get("complete"))
        else:
            all_complete = False
        records = audit.get("applications")
        if isinstance(records, list):
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                applications_by_key[
                    _execution_application_key(record)
                ] = dict(record)
        runtime = audit.get("agent_runtime")
        if isinstance(runtime, Mapping):
            runtime_closed_loop = runtime_closed_loop and bool(
                runtime.get("closed_loop", True)
            )
            runtime_rows = runtime.get("applications")
            if isinstance(runtime_rows, list):
                runtime_applications.extend(runtime_rows)

    applications = list(applications_by_key.values())
    from job_agent.execution import summarize_execution

    merged["applications"] = applications
    merged["counts"] = summarize_execution(applications)
    planned = max([len(applications), *planned_counts], default=len(applications))
    terminal = len(applications)
    merged["progress"] = {
        "planned": planned,
        "terminal": terminal,
        "remaining": max(0, planned - terminal),
        "complete": all_complete and terminal >= planned,
    }
    merged["attempt_audits"] = [str(path) for path, _audit in audits]
    if runtime_applications:
        merged["agent_runtime"] = {
            "schema_version": 1,
            "closed_loop": runtime_closed_loop,
            "applications": runtime_applications,
        }
    return merged


def _assert_audit_phase_consistency(
    run_dir: Path,
    state: Mapping[str, Any],
    audit: Mapping[str, Any] | None,
) -> None:
    """Reject a lower-layer execution that bypassed Daily SOP transitions."""
    if not isinstance(audit, Mapping):
        return
    progress = audit.get("progress")
    if not isinstance(progress, Mapping) or not bool(progress.get("complete")):
        return
    phase = str(state.get("phase") or "").strip().lower()
    if phase in {
        "created",
        "preparing",
        "prepared",
        "prepared_empty",
        "waiting_for_candidates",
        "executing",
    }:
        raise SopError(
            "Complete execution audit exists while the run is still in "
            f"phase '{phase}'; production execution bypassed Daily SOP. "
            "Use daily_sop.py execute/run --execute after reconciliation."
        )


def _validate_latest_run_phase(config: DailyConfig) -> None:
    latest_path = config.output_root / LATEST_FILE_NAME
    latest = _read_optional_json(latest_path)
    if not isinstance(latest, Mapping):
        return
    raw_run_dir = latest.get("run_dir")
    if not raw_run_dir:
        return
    run_dir = Path(str(raw_run_dir))
    if not run_dir.is_absolute():
        run_dir = config.root / run_dir
    if not run_dir.is_dir():
        return
    state = _read_optional_json(run_dir / STATE_FILE_NAME)
    if not isinstance(state, Mapping):
        return
    audit = _execution_audit_for_report(
        state,
        run_dir=run_dir.resolve(),
        root=config.root,
        fallback=_read_optional_json(run_dir / "execution-audit.json"),
    )
    _assert_audit_phase_consistency(run_dir, state, audit)


def write_run_report(run_dir: Path) -> Path:
    state = _read_json_object(run_dir / STATE_FILE_NAME, "run state")
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    root = Path(str(settings.get("root", PROJECT_ROOT)))
    manifest = _read_optional_json(run_dir / "pipeline-manifest.json")

    audit_path_value = (
        state.get("artifacts", {}).get("execution_audit")
        if isinstance(state.get("artifacts"), dict)
        else None
    )
    audit_path = (
        Path(str(audit_path_value))
        if audit_path_value
        else run_dir / "execution-audit.json"
    )
    if not audit_path.is_absolute():
        audit_path = root / audit_path
    audit = _execution_audit_for_report(
        state,
        run_dir=run_dir,
        root=root,
        fallback=_read_optional_json(audit_path),
    )
    _assert_audit_phase_consistency(run_dir, state, audit)

    pipeline_counts = (
        manifest.get("counts", {}) if isinstance(manifest, dict) else {}
    )
    daily_target = (
        state.get("daily_target", {})
        if isinstance(state.get("daily_target"), dict)
        else {}
    )
    execution_counts = audit.get("counts", {}) if isinstance(audit, dict) else {}
    applications = (
        audit.get("applications", []) if isinstance(audit, dict) else []
    )
    timing = _timing_summary(state)
    metrics = _build_evaluation_metrics(
        state,
        manifest,
        audit,
        settings=settings,
        run_dir=run_dir,
    )
    _write_json(run_dir / EVALUATION_METRICS_FILE_NAME, metrics)
    runtime_trace_path = _write_agent_runtime_trace(
        run_dir,
        state=state,
        manifest=manifest,
        audit=audit,
        metrics=metrics,
    )

    lines = [
        "# Daily Application Run",
        "",
        f"- Run ID: `{state.get('run_id', run_dir.name)}`",
        f"- Phase: `{state.get('phase', 'unknown')}`",
        f"- Created: `{state.get('created_at', 'unknown')}`",
        f"- Updated: `{state.get('updated_at', 'unknown')}`",
        f"- Config: `{state.get('config_path', 'unknown')}`",
        f"- Config hash: `{state.get('config_sha256', 'unknown')}`",
        f"- Automatic submit: `{'enabled' if settings.get('submit_complete') else 'disabled'}`",
        (
            f"- Application ledger: "
            f"`{Path(str(settings.get('output_root', root / 'output' / 'daily'))) / APPLICATION_LEDGER_FILE_NAME}`"
        ),
        "",
        "## Pipeline",
        "",
        "| Imported | Shortlisted | Prepared |",
        "| ---: | ---: | ---: |",
        (
            f"| {pipeline_counts.get('imported', 0)} "
            f"| {pipeline_counts.get('shortlisted', 0)} "
            f"| {pipeline_counts.get('prepared', 0)} |"
        ),
    ]

    if daily_target:
        lines.extend(
            [
                "",
                "## Daily Confirmed Submission Target",
                "",
                (
                    "| Date | Raw imported | Required by rate | "
                    "Effective target | Confirmed | Confirmed / raw | "
                    "Remaining | Reached |"
                ),
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
                (
                    f"| {daily_target.get('local_date', 'unknown')} "
                    f"| {daily_target.get('raw_imported', 'n/a')} "
                    f"| {daily_target.get('rate_target', 'n/a')} "
                    f"| {daily_target.get('target', 0)} "
                    f"| {daily_target.get('submitted', 0)} "
                    f"| {_format_evaluation_rate(daily_target.get('confirmed_rate'))} "
                    f"| {daily_target.get('remaining', 0)} "
                    f"| {'yes' if daily_target.get('reached') else 'no'} |"
                ),
            ]
        )

    metric_counts = metrics["counts"]
    metric_rates = metrics["rates"]
    metric_targets = metrics["targets"]
    metric_assessment = metrics["assessment"]
    metric_core = (
        metrics["agent_core"]
        if isinstance(metrics.get("agent_core"), Mapping)
        else {}
    )
    terminal_actual = _format_evaluation_ratio(
        metric_counts["terminal_records"],
        metric_counts["prepared"],
        metric_rates["terminal_audit_coverage"],
    )
    final_eligible_actual = _format_evaluation_ratio(
        metric_counts["submitted"],
        metric_counts["final_eligible"],
        metric_rates["confirmed_submission_rate_final_eligible"],
    )
    raw_import_actual = _format_evaluation_ratio(
        metric_counts["confirmed_for_raw_import_rate"],
        metric_counts["imported"],
        metric_rates["raw_import_to_confirmed_rate"],
    )
    lines.extend(
        [
            "",
            "## Agent Evaluation",
            "",
            f"- Structured metrics: `{EVALUATION_METRICS_FILE_NAME}`",
            f"- Unified runtime trace: `{runtime_trace_path.name}`",
            (
                f"- Evaluator: `{metric_core.get('evaluator', 'unknown')}`; "
                f"overall status: `{metric_core.get('status', 'unknown')}`"
            ),
            "- The primary confirmed-submission target uses the raw imported cohort as its denominator.",
            "",
            "| Metric | Actual | Target | Status |",
            "| --- | ---: | ---: | --- |",
            (
                f"| Imported cohort | {metric_counts['imported']} "
                f"| >= {metric_targets['imported_cohort_target']} "
                f"| {metric_assessment['imported_cohort']['status']} |"
            ),
            (
                f"| Terminal audit coverage | {terminal_actual} "
                f"| >= {_format_evaluation_rate(metric_targets['min_terminal_audit_coverage'])} "
                f"| {metric_assessment['terminal_audit_coverage']['status']} |"
            ),
            (
                f"| Confirmed / raw imported | {raw_import_actual} "
                f"| >= {_format_evaluation_rate(metric_targets['min_confirmed_submission_rate'])} "
                f"| {metric_assessment['raw_import_to_confirmed_rate']['status']} |"
            ),
            (
                f"| Confirmed / final eligible | {final_eligible_actual} "
                "| monitor | monitor |"
            ),
            (
                f"| Unconfirmed submit clicks | "
                f"{metric_counts['submit_clicked_unconfirmed'] if metric_counts['submit_clicked_unconfirmed'] is not None else 'n/a'} "
                f"| 0 | {metric_assessment['submit_clicked_unconfirmed']['status']} |"
            ),
        ]
    )
    metric_recommendations = metric_core.get("recommendations")
    if isinstance(metric_recommendations, list) and metric_recommendations:
        lines.extend(
            [
                "",
                "### Evaluation Recommendations",
                "",
                *[
                    f"- {str(item)}"
                    for item in metric_recommendations
                    if str(item).strip()
                ],
            ]
        )

    lines.extend(
        [
            "",
            "## Efficiency",
            "",
            "| Prepare active | Execute active | Productive | Waiting | Confirmed success rate |",
            "| ---: | ---: | ---: | ---: | ---: |",
            (
                f"| {_format_duration(timing['prepare_seconds'])} "
                f"| {_format_duration(timing['execute_seconds'])} "
                f"| {_format_duration(timing['productive_seconds'])} "
                f"| {_format_duration(timing['waiting_seconds'])} "
                f"| {_confirmed_success_rate(execution_counts)} |"
            ),
        ]
    )

    if execution_counts:
        lines.extend(
            [
                "",
                "## Execution",
                "",
                "| Total | Submitted | Blocked/Completed | Failed | Skipped |",
                "| ---: | ---: | ---: | ---: | ---: |",
                (
                    f"| {execution_counts.get('total', 0)} "
                    f"| {execution_counts.get('submitted', 0)} "
                    f"| {execution_counts.get('completed', 0)} "
                    f"| {execution_counts.get('failed', 0)} "
                    f"| {execution_counts.get('skipped', 0)} |"
                ),
            ]
        )
        for key in (
            "submit_clicked_unconfirmed",
            "email_verification_required",
            "submission_processing_error",
            "submission_blocked_by_anti_spam",
            "candidate_account_required",
        ):
            value = int(execution_counts.get(key, 0) or 0)
            if value:
                lines.append(f"- `{key}`: {value}")

    if isinstance(applications, list) and applications:
        lines.extend(
            [
                "",
                "## Application Results",
                "",
                "| Company | Role | Status |",
                "| --- | --- | --- |",
            ]
        )
        for item in applications:
            if not isinstance(item, dict):
                continue
            company = _escape_markdown_table(item.get("company", "unknown"))
            title = _escape_markdown_table(item.get("title", "unknown"))
            status = _escape_markdown_table(item.get("status", "unknown"))
            lines.append(f"| {company} | {title} | `{status}` |")

    recovery_rows = _recovery_plan_rows(applications)
    if recovery_rows:
        lines.extend(
            [
                "",
                "## Recovery Plans",
                "",
                "| Application | Strategy | Execution | Automatic actions | Candidate actions | Retry condition |",
                "| --- | --- | --- | --- | --- | --- |",
                *recovery_rows,
            ]
        )

    lines.extend(["", "## Next Action", ""])
    lines.extend(
        _next_action_lines(
            state.get("phase", ""),
            execution_counts,
            next_wake_at=state.get("next_wake_at"),
        )
    )
    lines.extend(
        [
            "",
            "Do not retry a terminal outcome until its recorded cause has been resolved.",
            "",
        ]
    )
    report_path = run_dir / REPORT_FILE_NAME
    report_path.write_text("\n".join(lines))
    return report_path


def find_job_agent_cli(root: Path) -> Path | None:
    override = os.getenv("JOB_AGENT_CLI")
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    local = root / ".venv" / "bin" / "job-agent"
    if local.is_file() and os.access(local, os.X_OK):
        return local.resolve()
    discovered = shutil.which("job-agent")
    return Path(discovered).resolve() if discovered else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repeatable daily SOP for the job application agent."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Daily JSON config (default: ops/daily.local.json).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="Validate all daily inputs without browsing.")
    subparsers.add_parser("prepare", help="Import, shortlist, and build packages.")

    execute_parser = subparsers.add_parser(
        "execute",
        help="Execute the latest prepared batch and write an audit.",
    )
    execute_parser.add_argument("--run-dir", type=Path)
    execute_parser.add_argument(
        "--retry",
        action="store_true",
        help="Retry only after the prior terminal blocker has been resolved.",
    )
    execute_parser.add_argument(
        "--resume-incomplete",
        action="store_true",
        help=(
            "Resume an interrupted incomplete audit without retrying recorded "
            "terminal or unknown-outcome applications."
        ),
    )
    execute_parser.add_argument(
        "--one-batch",
        action="store_true",
        help=(
            "Stop after this prepared batch. By default a complete audit keeps "
            "preparing eligible batches until the daily target is reached."
        ),
    )

    repair_parser = subparsers.add_parser(
        "repair",
        help=(
            "Resume a retained isolated coding repair without replaying the "
            "original batch."
        ),
    )
    repair_parser.add_argument("--run-dir", type=Path)
    repair_parser.add_argument(
        "--retry-verified",
        action="store_true",
        help=(
            "After verified promotion, execute only the saved scoped retry "
            "batch. Without this flag no browser is started."
        ),
    )
    repair_parser.add_argument(
        "--refresh-request-only",
        action="store_true",
        help=(
            "Rebuild and persist the current-audit repair request without "
            "checking Codex readiness or starting a repair."
        ),
    )
    repair_parser.add_argument(
        "--recover-interrupted",
        action="store_true",
        help=(
            "After confirming no prior repair process is active, record a "
            "stale 'repairing' phase as an infrastructure interruption and "
            "resume without consuming a code-repair cycle."
        ),
    )

    recover_parser = subparsers.add_parser(
        "recover",
        help=(
            "Execute Recovery Plans from a completed historical audit without "
            "opening a browser."
        ),
    )
    recover_parser.add_argument("--run-dir", type=Path)
    recover_parser.add_argument(
        "--retry-verified",
        action="store_true",
        help=(
            "After recovery evidence is verified, execute only the generated "
            "single-application retry batch. Without this flag no browser is started."
        ),
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Regenerate the concise report for the latest run.",
    )
    report_parser.add_argument("--run-dir", type=Path)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Remove stale temporary directories owned by this project.",
    )
    cleanup_parser.add_argument(
        "--older-than-hours",
        type=float,
        default=0,
        help="Only remove inactive managed temporary directories at least this old.",
    )

    subparsers.add_parser(
        "ledger",
        help="Regenerate the application history CSV from the tracking database.",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run preflight and preparation; add --execute for browser submission.",
    )
    run_parser.add_argument(
        "--execute",
        action="store_true",
        help="Continue into browser execution after packages are prepared.",
    )

    args = parser.parse_args(argv)
    root = PROJECT_ROOT
    load_env(root / ".env")

    try:
        config = DailyConfig.load(args.config, root=root)
        if args.command == "cleanup":
            cleanup_report = cleanup_managed_temp(
                config,
                min_age_seconds=args.older_than_hours * 60 * 60,
            )
            _print_temp_cleanup(cleanup_report, label="TMP cleanup")
            return 0 if cleanup_report.ok else 2

        stale_report = cleanup_managed_temp(
            config,
            min_age_seconds=AUTO_CLEANUP_MIN_AGE_SECONDS,
        )
        if stale_report.removed_count or stale_report.errors:
            _print_temp_cleanup(stale_report, label="Automatic TMP cleanup")

        with managed_temp_workspace(config, args.command):
            ledger_path, ledger_rows = refresh_application_ledger(config)
            print(f"Application ledger: {ledger_path} ({ledger_rows} records)")
            if args.command == "ledger":
                return 0
            if args.command == "check":
                _validate_latest_run_phase(config)
                report = run_preflight(config)
                print_preflight(report)
                return 0 if report.ok else 2
            if args.command == "prepare":
                try:
                    prepare_daily_run(config)
                finally:
                    refresh_application_ledger(config)
                return 0
            if args.command == "execute":
                run_dir = resolve_run_dir(config, args.run_dir)
                execution_error: SopError | None = None
                try:
                    try:
                        execute_daily_run(
                            config,
                            run_dir=run_dir,
                            retry=args.retry,
                            resume_incomplete=args.resume_incomplete,
                        )
                    except SopError as exc:
                        execution_error = exc
                finally:
                    refresh_application_ledger(config)
                    _update_daily_target_state(config, run_dir)
                    write_run_report(run_dir)
                if args.one_batch:
                    if execution_error is not None:
                        raise execution_error
                    return 0
                audit = _read_optional_json(run_dir / "execution-audit.json")
                progress = (
                    audit.get("progress", {})
                    if isinstance(audit, Mapping)
                    else {}
                )
                if not bool(progress.get("complete")):
                    if execution_error is not None:
                        raise execution_error
                    raise SopError(
                        "Execution did not produce a complete terminal audit; "
                        "refusing to continue to another batch"
                    )
                run_until_daily_target(config)
                return 0
            if args.command == "recover":
                run_dir = resolve_run_dir(config, args.run_dir)
                try:
                    recover_daily_run(
                        config,
                        run_dir=run_dir,
                        retry_verified=args.retry_verified,
                    )
                finally:
                    refresh_application_ledger(config)
                    _update_daily_target_state(config, run_dir)
                    write_run_report(run_dir)
                return 0
            if args.command == "repair":
                run_dir = resolve_run_dir(config, args.run_dir)
                try:
                    repair_daily_run(
                        config,
                        run_dir=run_dir,
                        retry_verified=args.retry_verified,
                        refresh_request_only=args.refresh_request_only,
                        recover_interrupted=args.recover_interrupted,
                    )
                finally:
                    refresh_application_ledger(config)
                    _update_daily_target_state(config, run_dir)
                    write_run_report(run_dir)
                return 0
            if args.command == "report":
                run_dir = resolve_run_dir(config, args.run_dir)
                _update_daily_target_state(config, run_dir)
                print(f"Run report: {write_run_report(run_dir)}")
                return 0
            if args.command == "run":
                try:
                    if args.execute:
                        run_until_daily_target(config)
                    else:
                        prepare_daily_run(config)
                finally:
                    refresh_application_ledger(config)
                return 0
        raise SopError(f"Unsupported command: {args.command}")
    except SopError as exc:
        print(f"SOP stopped: {exc}", file=sys.stderr)
        return 2


def _run_command(command: Sequence[str], config: DailyConfig) -> int:
    env = os.environ.copy()
    # The lower CLI remains useful for offline fixtures, but a real daily
    # output directory may only be executed by this SOP-owned subprocess.
    env["JOB_AGENT_DAILY_SOP_EXECUTION"] = "1"
    env["BROWSER_HEADLESS"] = "1" if config.browser_headless else "0"
    env["JOB_AGENT_SUBMIT_COMPLETE"] = "1" if config.submit_complete else "0"
    env["JOB_AGENT_LLM_ANSWERS"] = "1" if config.llm_answers else "0"
    env["JOB_AGENT_COMBOBOX_NO_PROGRESS_SECONDS"] = str(
        config.auto_repair.combobox_no_progress_seconds
    )
    completed = subprocess.run(
        list(command),
        cwd=config.root,
        env=env,
        check=False,
    )
    return completed.returncode


def _run_execution_command(
    command: Sequence[str],
    config: DailyConfig,
    *,
    audit_path: Path,
    run_dir: Path,
    repair_cycle: int,
    repair_budget_cycle: int | None = None,
    repair_attempt: int | None = None,
    poll_seconds: float = 0.25,
) -> tuple[int, IncrementalRepairOutcome | None]:
    """Run the browser batch while preparing the first isolated repair."""
    if not config.auto_repair.enabled:
        return _run_command(command, config), None

    repair_future: Future[dict[str, Any]] | None = None
    repair_request: dict[str, Any] | None = None
    request_path: Path | None = None
    with ThreadPoolExecutor(
        max_workers=2,
        thread_name_prefix="job-agent-execute",
    ) as executor:
        execution_future = executor.submit(_run_command, command, config)
        while not execution_future.done():
            if repair_future is None:
                incremental_audit = _read_optional_json(audit_path)
                candidate = build_repair_request(
                    incremental_audit,
                    run_dir=run_dir,
                    cycle=repair_cycle,
                )
                if candidate is not None:
                    attempt = repair_attempt or repair_cycle
                    candidate["attempt"] = attempt
                    candidate["budget_cycle"] = (
                        repair_budget_cycle or repair_cycle
                    )
                    repair_request = candidate
                    request_path = _repair_request_path(
                        run_dir,
                        cycle=repair_cycle,
                        attempt=attempt,
                        incremental=True,
                    )
                    _write_json(request_path, candidate)
                    repair_future = executor.submit(
                        run_repair_cycle,
                        config.auto_repair,
                        root=config.root,
                        run_dir=run_dir,
                        request=candidate,
                        defer_promotion=True,
                    )
            time.sleep(max(0.05, float(poll_seconds)))
        exit_code = execution_future.result()
        if repair_future is None or repair_request is None or request_path is None:
            return exit_code, None
        repair_result = repair_future.result()
    return (
        exit_code,
        IncrementalRepairOutcome(
            request=repair_request,
            request_path=request_path,
            result=repair_result,
        ),
    )


def _managed_temp_root(config: DailyConfig) -> Path:
    temp_root = config.output_root / MANAGED_TEMP_DIR_NAME
    if temp_root.resolve().parent != config.output_root.resolve():
        raise SopError(
            f"Managed temporary root must stay inside the daily output root: {temp_root}"
        )
    return temp_root


def _read_temp_marker(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _marker_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _marker_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _print_temp_cleanup(report: TempCleanupReport, *, label: str) -> None:
    print(
        f"{label}: removed={report.removed_count}, "
        f"active={report.skipped_active_count}, "
        f"recent={report.skipped_recent_count}, "
        f"unmanaged={report.skipped_unmanaged_count}, "
        f"errors={len(report.errors)}"
    )
    for error in report.errors:
        print(f"TMP CLEANUP ERROR: {error}", file=sys.stderr)


def _transition(
    run_dir: Path,
    state: dict[str, Any],
    output_root: Path,
    phase: str,
    **details: Any,
) -> None:
    event = {"at": _now(), "phase": phase}
    event.update(details)
    state["phase"] = phase
    state["updated_at"] = event["at"]
    state.setdefault("history", []).append(event)
    _write_state(run_dir, state, output_root)


def _write_state(
    run_dir: Path,
    state: Mapping[str, Any],
    output_root: Path,
) -> None:
    _write_json(run_dir / STATE_FILE_NAME, state)
    latest_path = output_root / LATEST_FILE_NAME
    if not _should_update_latest_pointer(
        run_dir,
        state,
        latest_path=latest_path,
    ):
        return
    _write_json(
        latest_path,
        {
            "schema_version": 1,
            "run_id": state.get("run_id"),
            "run_dir": str(run_dir.resolve()),
            "phase": state.get("phase"),
            "updated_at": state.get("updated_at"),
            "next_wake_at": state.get("next_wake_at"),
        },
    )


def _should_update_latest_pointer(
    run_dir: Path,
    state: Mapping[str, Any],
    *,
    latest_path: Path,
) -> bool:
    if not latest_path.is_file():
        return True
    try:
        latest = _read_json_object(latest_path, "latest run pointer")
    except (OSError, SopError):
        return True
    latest_value = str(latest.get("run_dir") or "").strip()
    if not latest_value:
        return True
    latest_run_dir = Path(latest_value)
    if not latest_run_dir.is_absolute():
        latest_run_dir = latest_path.parent / latest_run_dir
    if latest_run_dir.resolve() == run_dir.resolve():
        return True

    latest_state = _read_optional_json(latest_run_dir / STATE_FILE_NAME)
    candidate_created = _parse_datetime(state.get("created_at"))
    latest_created = (
        _parse_datetime(latest_state.get("created_at"))
        if isinstance(latest_state, Mapping)
        else None
    )
    if candidate_created is not None and latest_created is not None:
        if candidate_created != latest_created:
            return candidate_created > latest_created
    return str(run_dir.resolve()) > str(latest_run_dir.resolve())


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


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise SopError(f"{label.capitalize()} not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SopError(f"Cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SopError(f"{label.capitalize()} must be a JSON object: {path}")
    return payload


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _check_json_file(
    checks: list[PreflightCheck],
    name: str,
    path: Path,
    *,
    expected_type: type,
) -> Any:
    if not path.is_file():
        _add(checks, "ERROR", name, "File does not exist")
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _add(checks, "ERROR", name, f"Invalid JSON: {exc}")
        return None
    if not isinstance(payload, expected_type):
        _add(checks, "ERROR", name, f"Expected {expected_type.__name__}")
        return None
    return payload


def _check_database(checks: list[PreflightCheck], path: Path) -> None:
    if not path.exists():
        parent = _nearest_existing_parent(path)
        if os.access(parent, os.W_OK):
            _add(
                checks,
                "WARN",
                "tracking database",
                "Database does not exist yet and will be created",
            )
        else:
            _add(checks, "ERROR", "tracking database", "Database parent is not writable")
        return
    try:
        with sqlite3.connect(path) as connection:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
    except sqlite3.Error as exc:
        _add(checks, "ERROR", "tracking database", f"SQLite check failed: {exc}")
        return
    if not quick_check or quick_check[0] != "ok":
        _add(checks, "ERROR", "tracking database", "SQLite quick_check did not pass")
    elif not {"applications", "jobs"}.issubset(tables):
        _add(
            checks,
            "ERROR",
            "tracking database",
            "Required applications/jobs tables are missing",
        )
    else:
        _add(checks, "PASS", "tracking database", "SQLite integrity check passed")


def _check_playwright(checks: list[PreflightCheck]) -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if executable.is_file():
                _add(checks, "PASS", "browser runtime", "Chromium is installed")
            else:
                _add(
                    checks,
                    "ERROR",
                    "browser runtime",
                    "Playwright Chromium is not installed",
                )
    except Exception as exc:
        _add(
            checks,
            "ERROR",
            "browser runtime",
            f"Playwright check failed ({type(exc).__name__})",
        )


def _resolve_path(
    value: Any,
    *,
    name: str,
    root: Path,
    environ: Mapping[str, str],
) -> Path:
    raw = str(value)
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1) or match.group(2)
        resolved = environ.get(key)
        if resolved is None or resolved == "":
            missing.append(key)
            return match.group(0)
        return resolved

    expanded = ENV_PATTERN.sub(replace, raw)
    if missing:
        raise SopError(
            f"Daily config field '{name}' references missing environment variable(s): "
            + ", ".join(sorted(set(missing)))
        )
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _read_int(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SopError(f"Daily config field '{name}' must be an integer")
    if value < minimum or value > maximum:
        raise SopError(
            f"Daily config field '{name}' must be between {minimum} and {maximum}"
        )
    return value


def _read_bool(payload: Mapping[str, Any], name: str, *, default: bool) -> bool:
    value = payload.get(name, default)
    if not isinstance(value, bool):
        raise SopError(f"Daily config field '{name}' must be true or false")
    return value


def _read_rate(
    payload: Mapping[str, Any],
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SopError(f"Daily config field '{name}' must be a number")
    rate = float(value)
    if rate < minimum or rate > maximum:
        raise SopError(
            f"Daily config field '{name}' must be between {minimum} and {maximum}"
        )
    return rate


def _read_evaluation_policy(payload: Mapping[str, Any]) -> EvaluationPolicy:
    raw = payload.get("evaluation", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise SopError("Daily config field 'evaluation' must be an object")
    denominator = str(
        raw.get("confirmation_rate_denominator", "raw_imported")
        or ""
    ).strip()
    if denominator != "raw_imported":
        raise SopError(
            "Daily config field "
            "'evaluation.confirmation_rate_denominator' "
            "must be 'raw_imported'"
        )
    try:
        return EvaluationPolicy(
            imported_cohort_target=_read_int(
                raw,
                "imported_cohort_target",
                default=500,
                minimum=1,
                maximum=100_000,
            ),
            confirmation_rate_denominator=denominator,
            min_confirmed_submission_rate=_read_rate(
                raw,
                "min_confirmed_submission_rate",
                default=0.80,
                minimum=0.0,
                maximum=1.0,
            ),
            min_terminal_audit_coverage=_read_rate(
                raw,
                "min_terminal_audit_coverage",
                default=1.0,
                minimum=0.0,
                maximum=1.0,
            ),
        )
    except SopError as exc:
        message = str(exc).replace(
            "Daily config field '",
            "Daily config field 'evaluation.",
            1,
        )
        raise SopError(message) from exc


def _read_repair_policy(payload: Mapping[str, Any]) -> RepairPolicy:
    raw = payload.get("auto_repair", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise SopError("Daily config field 'auto_repair' must be an object")
    agent_binary = str(raw.get("agent_binary", "codex") or "").strip()
    if not agent_binary:
        raise SopError(
            "Daily config field 'auto_repair.agent_binary' cannot be empty"
        )
    try:
        return RepairPolicy(
            enabled=_read_bool(raw, "enabled", default=False),
            max_cycles=_read_int(
                raw,
                "max_cycles",
                default=1,
                minimum=1,
                maximum=5,
            ),
            agent_binary=agent_binary,
            agent_timeout_seconds=_read_int(
                raw,
                "agent_timeout_seconds",
                default=900,
                minimum=30,
                maximum=3600,
            ),
            verification_timeout_seconds=_read_int(
                raw,
                "verification_timeout_seconds",
                default=1200,
                minimum=30,
                maximum=7200,
            ),
            combobox_no_progress_seconds=_read_int(
                raw,
                "combobox_no_progress_seconds",
                default=20,
                minimum=5,
                maximum=120,
            ),
            retry_after_verified_repair=_read_bool(
                raw,
                "retry_after_verified_repair",
                default=True,
            ),
        )
    except SopError as exc:
        message = str(exc).replace(
            "Daily config field '",
            "Daily config field 'auto_repair.",
            1,
        )
        raise SopError(message) from exc


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (dict, list)):
        return not value
    normalized = str(value).strip().lower()
    if not normalized:
        return True
    return normalized in {
        "todo",
        "tbd",
        "needs review",
        "need review",
        "unknown",
        "your name",
        "your email",
        "your phone",
    }


def _add(
    checks: list[PreflightCheck],
    level: str,
    name: str,
    message: str,
) -> None:
    checks.append(PreflightCheck(level=level, name=name, message=message))


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _artifact_path(value: Any, root: Path) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else root / path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _effective_submit_gate(config: DailyConfig) -> str:
    return (
        "automatic_when_no_blocking_review"
        if config.submit_complete
        else "manual_submit_required"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")




def _heartbeat_state_path(config: DailyConfig) -> Path:
    return Path(config.output_root) / HEARTBEAT_STATE_FILE_NAME


def _default_heartbeat_state() -> dict[str, Any]:
    return {
        "version": 1,
        "empty_wake_count": 0,
        "no_progress_count": 0,
        "last_run_at": None,
        "last_phase": None,
        "last_run_id": None,
        "pause_until": None,
        "paused_reason": None,
        "backoff_minutes": None,
    }


def _read_heartbeat_state(config: DailyConfig) -> dict[str, Any]:
    path = _heartbeat_state_path(config)
    try:
        return _read_json_object(path, "heartbeat state")
    except (FileNotFoundError, SopError):
        return _default_heartbeat_state()


def _write_heartbeat_state(config: DailyConfig, state: dict[str, Any]) -> None:
    state = {**state, "updated_at": _now()}
    _write_json(_heartbeat_state_path(config), state)


def _apply_heartbeat_wake_logic(
    config: DailyConfig,
    prepared_count: int,
    run_dir: Path,
    state: dict[str, Any],
    transition_details: dict[str, Any],
) -> None:
    """Set next_wake_at using the configured interval when no candidate is prepared.

    Empty candidate polls are expected while sources refresh.  The configured
    interval is the external scheduler contract; exponential backoff can leave
    an unmet daily target idle for tens of minutes even when new listings have
    arrived.  Also resets the no-progress counter when a new batch appears and
    writes a small heartbeat-state file that external automations can read
    without opening every run directory.
    """
    hb = _read_heartbeat_state(config)
    hb["last_run_at"] = _now()
    hb["last_phase"] = str(state.get("phase") or "")
    hb["last_run_id"] = str(run_dir.name)

    if prepared_count > 0:
        hb["empty_wake_count"] = 0
        hb["no_progress_count"] = 0
        hb["pause_until"] = None
        hb["paused_reason"] = None
        hb["backoff_minutes"] = None
        state.pop("next_wake_at", None)
    else:
        empty_count = int(hb.get("empty_wake_count", 0)) + 1
        hb["empty_wake_count"] = empty_count
        base = max(1, config.empty_wake_minutes)
        backoff = base
        next_wake = _next_wake_at(backoff)
        hb["backoff_minutes"] = backoff
        hb["pause_until"] = None
        hb["paused_reason"] = None
        state["next_wake_at"] = next_wake
        transition_details["next_wake_at"] = next_wake
        transition_details["wake_after_minutes"] = backoff

    _write_heartbeat_state(config, hb)


def _record_heartbeat_no_progress(
    config: DailyConfig,
    run_dir: Path,
    submitted_before: int,
    submitted_after: int,
) -> bool:
    """Track consecutive executed batches that did not increase confirmed submissions.

    Returns True when a pause is set (the caller should stop the loop).
    """
    hb = _read_heartbeat_state(config)
    hb["last_run_at"] = _now()
    hb["last_phase"] = "executed"
    hb["last_run_id"] = str(run_dir.name)

    if submitted_after > submitted_before:
        hb["no_progress_count"] = 0
        hb["empty_wake_count"] = 0
        hb["pause_until"] = None
        hb["paused_reason"] = None
        _write_heartbeat_state(config, hb)
        return False

    no_progress = int(hb.get("no_progress_count", 0)) + 1
    hb["no_progress_count"] = no_progress

    if no_progress >= HEARTBEAT_MAX_NO_PROGRESS_BATCHES:
        pause_until = (
            datetime.now().astimezone()
            + timedelta(minutes=HEARTBEAT_NO_PROGRESS_PAUSE_MINUTES)
        ).isoformat()
        hb["pause_until"] = pause_until
        hb["paused_reason"] = (
            f"No confirmed submission after {no_progress} consecutive "
            "executed batches; waiting for candidate facts or source refresh."
        )
        _write_heartbeat_state(config, hb)
        return True

    _write_heartbeat_state(config, hb)
    return False

def _next_wake_at(minutes: int) -> str:
    return (
        datetime.now().astimezone() + timedelta(minutes=max(1, int(minutes)))
    ).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _waiting_seconds_since_ready(state: Mapping[str, Any]) -> float:
    history = state.get("history")
    if not isinstance(history, list):
        return 0.0
    for event in reversed(history):
        if not isinstance(event, dict):
            continue
        if event.get("phase") not in {
            "prepared",
            "prepared_empty",
            "waiting_for_candidates",
        }:
            continue
        ready_at = _parse_datetime(event.get("at"))
        if ready_at is None:
            return 0.0
        return round(max(0.0, (datetime.now().astimezone() - ready_at).total_seconds()), 3)
    return 0.0


def _timing_summary(state: Mapping[str, Any]) -> dict[str, float]:
    prepare_seconds = 0.0
    execute_seconds = 0.0
    waiting_seconds = 0.0
    history = state.get("history")
    if isinstance(history, list):
        for event in history:
            if not isinstance(event, dict):
                continue
            try:
                duration = max(0.0, float(event.get("duration_seconds") or 0))
            except (TypeError, ValueError):
                duration = 0.0
            if event.get("stage") == "prepare":
                prepare_seconds += duration
            elif event.get("stage") == "execute":
                execute_seconds += duration
            try:
                waiting_seconds += max(0.0, float(event.get("waiting_seconds") or 0))
            except (TypeError, ValueError):
                pass
    if state.get("phase") in {"prepared", "prepared_empty", "waiting_for_candidates"}:
        waiting_seconds += _waiting_seconds_since_ready(state)
    return {
        "prepare_seconds": prepare_seconds,
        "execute_seconds": execute_seconds,
        "productive_seconds": prepare_seconds + execute_seconds,
        "waiting_seconds": waiting_seconds,
    }


def _format_duration(value: Any) -> str:
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _confirmed_success_rate(counts: Mapping[str, Any]) -> str:
    try:
        total = max(0, int(counts.get("total", 0) or 0))
        submitted = max(0, int(counts.get("submitted", 0) or 0))
    except (TypeError, ValueError):
        return "n/a"
    if total == 0:
        return "n/a"
    return f"{submitted / total * 100:.1f}% ({submitted}/{total})"


def _escape_markdown_table(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _recovery_plan_rows(applications: Any) -> list[str]:
    if not isinstance(applications, list):
        return []
    planner = JobApplicationRecoveryPlanner()
    rows: list[str] = []
    for item in applications:
        if not isinstance(item, Mapping):
            continue
        raw_plan = item.get("recovery_plan")
        if isinstance(raw_plan, Mapping):
            plan = dict(raw_plan)
        else:
            generated = planner(
                str(item.get("status") or ""),
                item,
            )
            if generated is None:
                continue
            plan = recovery_plan_to_dict(generated)
        actions = plan.get("actions")
        action_items = (
            [action for action in actions if isinstance(action, Mapping)]
            if isinstance(actions, list)
            else []
        )
        automatic = "<br>".join(
            str(action.get("description") or action.get("action") or "")
            for action in action_items
            if bool(action.get("automatic"))
        ) or "None"
        candidate = "<br>".join(
            str(action.get("description") or action.get("action") or "")
            for action in action_items
            if bool(action.get("requires_user"))
        ) or "None"
        company = str(item.get("company") or "unknown")
        title = str(item.get("title") or "unknown")
        application = f"{company} / {title}"
        strategy = plan.get("strategy") or "unknown"
        raw_execution = item.get("recovery_execution")
        execution_status = (
            str(raw_execution.get("status") or "not_run")
            if isinstance(raw_execution, Mapping)
            else "not_run"
        )
        retry_condition = plan.get("retry_condition") or (
            "Not allowed"
            if not bool(plan.get("retry_allowed"))
            else "Required recovery evidence must be verified."
        )
        rows.append(
            "| "
            + " | ".join(
                _escape_markdown_table(value)
                for value in (
                    application,
                    strategy,
                    execution_status,
                    automatic,
                    candidate,
                    retry_condition,
                )
            )
            + " |"
        )
    return rows


def _next_action_lines(
    phase: Any,
    counts: Mapping[str, Any],
    *,
    next_wake_at: Any = None,
) -> list[str]:
    normalized_phase = str(phase)
    if normalized_phase == "prepared":
        return [
            "- Review `pipeline-manifest.json`, optional `candidate-screening.json`, and resume paths.",
            "- Run the SOP `execute` stage only after the prepared batch is acceptable.",
        ]
    if normalized_phase in {"prepared_empty", "waiting_for_candidates"}:
        wake_detail = (
            f" at `{next_wake_at}`" if str(next_wake_at or "").strip() else ""
        )
        return [
            "- No jobs currently meet the score, screening, duplicate, cooldown, and circuit-breaker gates.",
            (
                "- Exit this process and let the external scheduler start a new `run --execute` cycle"
                f"{wake_detail}; do not sleep inside the Goal."
            ),
        ]
    if normalized_phase == "prepare_failed":
        return ["- Fix the first preparation error, then start a new daily run."]
    if normalized_phase == "execution_failed":
        return ["- Diagnose the failed attempt from the audit before using `--retry`."]
    if normalized_phase == "needs_repair":
        return [
            "- A repairable runtime blocker was recorded in `repair/repair-request-*.json`.",
            "- Do not mark this run complete or retry its terminal outcomes before a repair is verified.",
        ]
    if normalized_phase == "repairing":
        return [
            "- The bounded coding repair is running in an isolated workspace.",
            "- A retry is permitted only if every configured offline verification passes.",
        ]
    if normalized_phase == "repair_unavailable":
        return [
            "- The isolated repair agent failed readiness or infrastructure checks; no code-repair cycle was consumed.",
            "- Restore the repair-agent session, then run the SOP `repair` command for this run; it reuses only the retained scoped request and does not start a browser by default.",
        ]
    if normalized_phase == "repair_failed":
        return [
            "- Inspect the latest `repair-result-cycle-*.json`; no code was promoted.",
            "- Keep the affected applications terminal until another bounded repair cycle is authorized.",
        ]
    if normalized_phase == "repair_exhausted":
        return [
            "- Automatic repair reached its configured cycle limit without resolving the fingerprint.",
            "- Diagnose the saved repair result before starting another execution attempt.",
        ]
    if normalized_phase == "repair_verified":
        return [
            "- The repair passed focused tests, the full suite, and offline verification.",
            "- Retry only the scoped batch recorded under `repair_retry_batch`.",
        ]
    actions: list[str] = []
    if int(counts.get("email_verification_required", 0) or 0):
        actions.append(
            "- Resolve Gmail authorization or verification-code delivery before retrying."
        )
    if int(counts.get("submission_blocked_by_anti_spam", 0) or 0):
        actions.append(
            "- Respect the site cooldown; do not retry the same host in the same daily run."
        )
    if int(counts.get("candidate_account_required", 0) or 0):
        actions.append(
            "- Confirm the candidate account/password flow before retrying."
        )
    if int(counts.get("submit_clicked_unconfirmed", 0) or 0):
        actions.append(
            "- Reconcile saved confirmation evidence before attempting another submit."
        )
    if int(counts.get("submission_processing_error", 0) or 0):
        actions.append(
            "- Inspect the saved page evidence and resolve the processing error first."
        )
    if int(counts.get("completed", 0) or 0):
        actions.append(
            "- Review each completed-but-blocked package's `review-required.txt`."
        )
    if int(counts.get("failed", 0) or 0):
        actions.append("- Diagnose failed runtimes from their structured audit records.")
    return actions or ["- Archive the report; no follow-up is required for this run."]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
