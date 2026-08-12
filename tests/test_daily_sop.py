from __future__ import annotations

import csv
import hashlib
import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import job_agent.daily_sop as daily_sop_module
from job_agent.db import (
    connect,
    create_application,
    create_job,
    init_db,
    update_application_execution_status,
)
from job_agent.daily_sop import (
    DailyConfig,
    PROJECT_ROOT,
    SopError,
    build_execute_command,
    build_prepare_command,
    cleanup_managed_temp,
    create_run_dir,
    daily_submission_progress,
    execute_daily_run,
    fingerprint_inputs,
    main,
    managed_temp_workspace,
    prepare_daily_run,
    recover_daily_run,
    repair_daily_run,
    run_preflight,
    run_until_daily_target,
    validate_run_inputs,
    write_run_report,
)
from job_agent.models import Job
from job_agent.repair_orchestrator import RepairAgentReadiness, RepairPolicy


def _write_workspace(tmp_path: Path) -> dict[str, object]:
    source_config = tmp_path / "sources.json"
    source_config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "type": "remotive",
                        "search": "agent engineer",
                        "limit": 5,
                    }
                ]
            }
        )
    )
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "name": "Test Candidate",
                "email": "candidate@example.com",
                "phone": "+1 555 0100",
            }
        )
    )
    sensitive_kb = tmp_path / "sensitive.json"
    sensitive_kb.write_text(
        json.dumps(
            {
                "work_authorization": "Authorized",
                "sponsorship": "No",
            }
        )
    )
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    (resumes / "agent-engineer.pdf").write_bytes(b"%PDF-1.7\nfixture")

    database = tmp_path / "job-agent.db"
    connection = connect(database)
    init_db(connection)
    connection.close()

    profile_vector_db = tmp_path / "profile-vector.db"
    profile_vector_db.write_bytes(b"fixture")
    cli = tmp_path / "job-agent"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)

    payload: dict[str, object] = {
        "schema_version": 1,
        "source_config": str(source_config),
        "profile": str(profile),
        "sensitive_kb": str(sensitive_kb),
        "database": str(database),
        "profile_vector_db": str(profile_vector_db),
        "resume_source_dir": "${TEST_RESUME_DIR}",
        "required_resume_pdf": None,
        "output_root": str(tmp_path / "output" / "daily"),
        "min_score": 72,
        "limit": 4,
        "timeout_seconds": 240,
        "use_llm": True,
        "llm_answers": True,
        "browser_headless": True,
        "submit_complete": False,
        "require_gmail_token": False,
    }
    return {
        "payload": payload,
        "resumes": resumes,
        "cli": cli,
        "source_config": source_config,
        "profile": profile,
        "sensitive_kb": sensitive_kb,
        "database": database,
    }


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DailyConfig:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JOB_AGENT_CLI", str(workspace["cli"]))
    return DailyConfig.from_mapping(
        workspace["payload"],
        config_path=tmp_path / "daily.local.json",
        root=tmp_path,
    )


def _repair_resume_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DailyConfig, Path, Path]:
    workspace = _write_workspace(tmp_path)
    payload = workspace["payload"]
    assert isinstance(payload, dict)
    payload["auto_repair"] = {
        "enabled": True,
        "max_cycles": 2,
        "agent_binary": "codex",
        "agent_timeout_seconds": 30,
        "verification_timeout_seconds": 30,
        "retry_after_verified_repair": True,
    }
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JOB_AGENT_CLI", str(workspace["cli"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(payload))
    config = DailyConfig.load(config_path, root=tmp_path)
    monkeypatch.setattr(
        daily_sop_module,
        "_rebuild_verified_repair_retry_packages",
        lambda _config, *, retry_summary, **_kwargs: retry_summary,
    )

    run_dir = config.output_root / "2026-07-27" / "221010"
    applications_dir = run_dir / "applications"
    repairable_dir = applications_dir / "001-repairable"
    protected_dir = applications_dir / "002-protected"
    repairable_dir.mkdir(parents=True)
    protected_dir.mkdir()
    batch_summary = applications_dir / "batch-summary.json"
    batch_summary.write_text(
        json.dumps(
            [
                {
                    "company": "Repairable",
                    "title": "Engineer",
                    "package_dir": str(repairable_dir),
                },
                {
                    "company": "Protected",
                    "title": "Engineer",
                    "package_dir": str(protected_dir),
                },
            ]
        )
    )
    repair_dir = run_dir / "repair"
    repair_dir.mkdir()
    request_path = repair_dir / "repair-request-cycle-02.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cycle": 2,
                "findings": [
                    {
                        "company": "Repairable",
                        "title": "Engineer",
                        "package_dir": str(repairable_dir),
                        "status": "autofill_completed_blocked",
                        "fingerprints": [
                            {
                                "code": "country_combobox_commit_mismatch",
                                "field_label": "Country",
                            }
                        ],
                    }
                ],
                "retry_targets": [
                    {
                        "company": "Repairable",
                        "title": "Engineer",
                        "package_dir": str(repairable_dir),
                    }
                ],
                "constraints": {
                    "real_browser_verification": False,
                    "real_submission": False,
                    "network_access_for_shell_commands": False,
                    "approved_paths_only": True,
                },
            }
        )
    )
    old_results: list[Path] = []
    old_cycles: list[dict[str, object]] = []
    for cycle in (1, 2):
        result_path = repair_dir / f"repair-result-cycle-{cycle:02d}.json"
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "cycle": cycle,
                    "status": "agent_failed",
                    "reason": "repair_agent_exit_code_1",
                    "agent_stderr": (
                        "401 Unauthorized: invalid_refresh_token; "
                        "Provided authentication token is expired"
                    ),
                }
            )
        )
        old_results.append(result_path)
        old_cycles.append(
            {
                "cycle": cycle,
                "status": "agent_failed",
                "reason": "repair_agent_exit_code_1",
                "result_path": str(result_path),
            }
        )
    manifest_path = run_dir / "pipeline-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "counts": {"imported": 2, "shortlisted": 2, "prepared": 2},
                "artifacts": {
                    "batch_summary": str(batch_summary),
                    "repair_request": str(request_path),
                },
            }
        )
    )
    state = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "phase": "repair_exhausted",
        "created_at": "2026-07-27T22:10:10-04:00",
        "updated_at": "2026-07-28T09:17:54-04:00",
        "config_path": str(config.config_path),
        "config_sha256": hashlib.sha256(config.config_path.read_bytes()).hexdigest(),
        "input_sha256": fingerprint_inputs(config),
        "settings": config.snapshot(),
        "artifacts": {
            "manifest": str(manifest_path),
            "batch_summary": str(batch_summary),
            "repair_request": str(request_path),
            "repair_result": str(old_results[-1]),
        },
        "execution_attempts": [
            {"finished_at": "2026-07-28T09:17:49-04:00", "exit_code": 0}
        ],
        "repair_cycles": old_cycles,
        "history": [
            {
                "at": "2026-07-28T09:17:54-04:00",
                "phase": "repair_exhausted",
            }
        ],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))
    return config, run_dir, batch_summary


def test_daily_config_expands_environment_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)

    assert config.resume_source_dir == (tmp_path / "resumes").resolve()
    assert config.limit == 4
    assert config.daily_submit_target == 4
    assert config.empty_wake_minutes == 15
    assert config.submit_complete is False


def test_write_state_does_not_move_latest_pointer_back_to_an_older_run(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output" / "daily"
    older_run = output_root / "2026-07-29" / "192743"
    newer_run = output_root / "2026-07-29" / "193937"
    older_run.mkdir(parents=True)
    newer_run.mkdir()
    older_state = {
        "schema_version": 1,
        "run_id": older_run.name,
        "phase": "repair_unavailable",
        "created_at": "2026-07-29T19:27:43-04:00",
        "updated_at": "2026-07-29T19:49:33-04:00",
    }
    newer_state = {
        "schema_version": 1,
        "run_id": newer_run.name,
        "phase": "waiting_for_candidates",
        "created_at": "2026-07-29T19:39:37-04:00",
        "updated_at": "2026-07-29T19:40:31-04:00",
        "next_wake_at": "2026-07-29T19:45:31-04:00",
    }

    daily_sop_module._write_state(newer_run, newer_state, output_root)
    older_state["phase"] = "executed_with_blockers"
    daily_sop_module._write_state(older_run, older_state, output_root)

    latest = json.loads((output_root / "latest.json").read_text())
    assert latest["run_id"] == newer_run.name
    assert latest["phase"] == "waiting_for_candidates"
    assert json.loads((older_run / "run-state.json").read_text())["phase"] == (
        "executed_with_blockers"
    )


def test_daily_config_accepts_limit_100_and_rejects_larger_batches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    payload = workspace["payload"]
    assert isinstance(payload, dict)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))

    payload["limit"] = 100
    payload["daily_submit_target"] = 100
    config = DailyConfig.from_mapping(
        payload,
        config_path=tmp_path / "daily.local.json",
        root=tmp_path,
    )
    assert config.limit == 100
    assert config.daily_submit_target == 100

    payload["limit"] = 101
    with pytest.raises(SopError, match="between 1 and 100"):
        DailyConfig.from_mapping(
            payload,
            config_path=tmp_path / "daily.local.json",
            root=tmp_path,
        )

    payload["limit"] = 100
    payload["daily_submit_target"] = 101
    with pytest.raises(SopError, match="daily_submit_target.*between 1 and 100"):
        DailyConfig.from_mapping(
            payload,
            config_path=tmp_path / "daily.local.json",
            root=tmp_path,
        )


def test_verified_repair_retry_rebuilds_stale_package_with_current_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import job_agent.cli as cli_module

    config = _config(tmp_path, monkeypatch)
    run_dir = config.output_root / "2026-07-29" / "101010"
    original_package = run_dir / "applications" / "001-example"
    original_package.mkdir(parents=True)
    jobs_path = run_dir / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "company": "Example",
                    "title": "Engineer",
                    "raw_jd": "Build reliable systems.",
                    "source": "test",
                    "apply_url": "https://jobs.example.com/engineer",
                }
            ]
        )
    )
    retry_summary = run_dir / "repair" / "retry-batch-cycle-03.json"
    retry_summary.parent.mkdir()
    retry_summary.write_text(
        json.dumps(
            [
                {
                    "application_id": "17",
                    "company": "Example",
                    "title": "Engineer",
                    "apply_url": "https://jobs.example.com/engineer",
                    "package_dir": str(original_package),
                    "retry": True,
                    "repair_verified": True,
                    "retry_scope": "single_application",
                    "repair_cycle": 3,
                    "original_terminal_status": "autofill_completed_blocked",
                }
            ]
        )
    )
    captured: dict[str, object] = {}

    def fake_prepare(job, out_dir, **kwargs):
        captured["job"] = job
        captured["out_dir"] = out_dir
        captured["profile"] = kwargs["profile"]
        out_dir.mkdir(parents=True)
        runtime_script = out_dir / "autofill-runtime.js"
        runtime_script.write_text("// rebuilt from current code and profile")
        return {
            "application_id": "17",
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(out_dir),
            "runtime_script_path": str(runtime_script),
        }

    monkeypatch.setattr(
        cli_module,
        "_prepare_application_package",
        fake_prepare,
    )

    rebuilt_path = daily_sop_module._rebuild_verified_repair_retry_packages(
        config,
        run_dir=run_dir,
        manifest={"artifacts": {"jobs": str(jobs_path)}},
        retry_summary=retry_summary,
        cycle=3,
    )

    assert rebuilt_path == retry_summary
    item = json.loads(retry_summary.read_text())[0]
    assert item["application_id"] == "17"
    assert item["repair_verified"] is True
    assert item["retry_scope"] == "single_application"
    assert item["original_package_dir"] == str(original_package)
    assert item["package_dir"] != str(original_package)
    assert "/repair/rebuilt-cycle-03-application-17" in item["package_dir"]
    assert Path(item["runtime_script_path"]).read_text() == (
        "// rebuilt from current code and profile"
    )
    assert Path(item["package_dir"], "repair-package-summary.json").is_file()
    assert captured["profile"] == config.profile


def test_daily_submission_progress_counts_only_confirmed_local_day_submissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    connection = connect(config.database)
    submitted_job = Job(
        title="Submitted Engineer",
        company="Submitted Co",
        raw_jd="Build systems.",
        source="test",
        apply_url="https://jobs.example.com/submitted",
    )
    blocked_job = Job(
        title="Blocked Engineer",
        company="Blocked Co",
        raw_jd="Build systems.",
        source="test",
        apply_url="https://jobs.example.com/blocked",
    )
    submitted_id = create_application(
        connection,
        create_job(connection, submitted_job),
        submitted_job,
    )
    blocked_id = create_application(
        connection,
        create_job(connection, blocked_job),
        blocked_job,
    )
    update_application_execution_status(connection, submitted_id, "submitted")
    update_application_execution_status(
        connection,
        blocked_id,
        "autofill_completed_blocked",
    )
    connection.close()

    progress = daily_submission_progress(config)

    assert progress["target"] == 4
    assert progress["submitted"] == 1
    assert progress["remaining"] == 3
    assert progress["reached"] is False

    cohort_progress = daily_submission_progress(
        config,
        raw_imported=10,
    )
    assert cohort_progress["base_target"] == 4
    assert cohort_progress["raw_imported"] == 10
    assert cohort_progress["rate_target"] == 8
    assert cohort_progress["target"] == 8
    assert cohort_progress["confirmed_rate"] == 0.1
    assert cohort_progress["remaining"] == 7
    assert cohort_progress["reached"] is False


def test_run_accounting_uses_execution_date_across_midnight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    connection = connect(config.database)
    job = Job(
        title="Executed Tomorrow",
        company="Date Co",
        raw_jd="Build systems.",
        source="test",
        apply_url="https://jobs.example.com/date",
    )
    application_id = create_application(
        connection,
        create_job(connection, job),
        job,
    )
    connection.execute(
        """
        update applications
        set status = 'submitted', submitted_at = '2026-07-28 13:05:00'
        where id = ?
        """,
        (application_id,),
    )
    connection.commit()
    connection.close()
    run_dir = config.output_root / "2026-07-27" / "230000"
    run_dir.mkdir(parents=True)
    state = {
        "schema_version": 1,
        "run_id": "230000",
        "phase": "executed",
        "created_at": "2026-07-27T23:00:00-04:00",
        "updated_at": "2026-07-28T09:10:00-04:00",
        "settings": config.snapshot(),
        "execution_attempts": [
            {
                "finished_at": "2026-07-28T09:10:00-04:00",
                "exit_code": 0,
            }
        ],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))

    progress = daily_sop_module._update_daily_target_state(config, run_dir)

    assert progress["local_date"] == "2026-07-28"
    assert progress["submitted"] == 1
    updated = json.loads((run_dir / "run-state.json").read_text())
    assert updated["accounting_date_source"] == "execution_finished_at"


def test_repair_resume_stops_once_when_agent_becomes_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    repair_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: RepairAgentReadiness(
            True,
            "ready",
            "ready",
            "codex",
        ),
    )

    def unavailable_repair(*args, **kwargs):
        request = dict(kwargs["request"])
        repair_calls.append(request)
        result_path = (
            run_dir
            / "repair"
            / "repair-result-attempt-03-cycle-01.json"
        )
        result = {
            "schema_version": 1,
            "attempt": 3,
            "cycle": 1,
            "status": "agent_unavailable",
            "reason": "repair_agent_authentication_failed",
            "retryable": False,
            "changed_files": [],
            "result_path": str(result_path),
        }
        result_path.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(
        daily_sop_module,
        "run_repair_cycle",
        unavailable_repair,
    )

    with pytest.raises(SopError, match="without consuming a repair cycle"):
        repair_daily_run(config, run_dir=run_dir)

    assert len(repair_calls) == 1
    assert repair_calls[0]["cycle"] == 1
    assert repair_calls[0]["attempt"] == 3
    state = json.loads((run_dir / "run-state.json").read_text())
    assert state["phase"] == "repair_unavailable"
    assert state["consumed_repair_cycles"] == 0
    assert len(state["repair_attempts"]) == 1
    assert daily_sop_module._repair_cycle_count(state, run_dir) == 0


def test_repair_cycle_budget_counts_only_failures_after_latest_verified_attempt(
    tmp_path: Path,
) -> None:
    state = {
        "execution_attempts": [
            {"finished_at": "2026-08-10T12:05:00-04:00"}
        ],
        "repair_attempts": [
            {"attempt": 1, "cycle": 1, "status": "verification_failed"},
            {
                "attempt": 2,
                "cycle": 2,
                "status": "already_fixed_verified",
                "finished_at": "2026-08-10T12:00:00-04:00",
            },
            {"attempt": 3, "cycle": 1, "status": "verification_failed"},
            {"attempt": 4, "cycle": 2, "status": "exhausted"},
        ],
        "repair_cycles": [
            {"attempt": 1, "cycle": 1, "status": "verification_failed"},
            {"attempt": 2, "cycle": 2, "status": "already_fixed_verified"},
            {"attempt": 3, "cycle": 1, "status": "verification_failed"},
            {"attempt": 4, "cycle": 2, "status": "exhausted"},
        ],
    }

    assert daily_sop_module._repair_cycle_count(state, tmp_path) == 1


def test_repair_resume_uses_retained_scope_without_browser_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    old_result = run_dir / "repair" / "repair-result-cycle-01.json"
    old_result_contents = old_result.read_text()
    browser_calls: list[object] = []

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: RepairAgentReadiness(
            True,
            "ready",
            "ready",
            "codex",
        ),
    )

    def promoted_repair(*args, **kwargs):
        request = dict(kwargs["request"])
        assert request["cycle"] == 1
        assert request["attempt"] == 3
        result_path = (
            run_dir
            / "repair"
            / "repair-result-attempt-03-cycle-01.json"
        )
        result = {
            "schema_version": 1,
            "attempt": 3,
            "cycle": 1,
            "status": "promoted",
            "reason": "all_verification_passed",
            "changed_files": ["src/job_agent/python_runtime.py"],
            "result_path": str(result_path),
        }
        result_path.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(
        daily_sop_module,
        "run_repair_cycle",
        promoted_repair,
    )
    monkeypatch.setattr(
        daily_sop_module,
        "execute_daily_run",
        lambda *args, **kwargs: browser_calls.append((args, kwargs)),
    )

    repaired = repair_daily_run(config, run_dir=run_dir)

    assert repaired == run_dir
    assert browser_calls == []
    assert old_result.read_text() == old_result_contents
    state = json.loads((run_dir / "run-state.json").read_text())
    assert state["phase"] == "repair_verified"
    assert state["consumed_repair_cycles"] == 1
    scoped_batch = Path(state["artifacts"]["scoped_retry_batch"])
    batch = json.loads(scoped_batch.read_text())
    assert [item["company"] for item in batch] == ["Repairable"]
    assert batch[0]["retry"] is True
    assert batch[0]["repair_verified"] is True
    assert batch[0]["retry_scope"] == "single_application"
    assert batch[0]["original_terminal_status"] == "autofill_completed_blocked"


def test_verified_repair_resume_drops_stale_recovery_targets_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    state_path = run_dir / "run-state.json"
    manifest_path = run_dir / "pipeline-manifest.json"
    state = json.loads(state_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    repair_batch = run_dir / "repair" / "retry-batch-cycle-02.json"
    repair_item = {
        "application_id": "17",
        "company": "Repairable",
        "title": "Engineer",
        "package_dir": str(run_dir / "repair" / "rebuilt-17"),
        "original_package_dir": str(
            run_dir / "applications" / "001-repairable"
        ),
        "retry": True,
        "repair_verified": True,
        "retry_scope": "single_application",
        "repair_cycle": 2,
    }
    repair_batch.write_text(json.dumps([repair_item]))
    stale_combined = run_dir / "repair" / "stale-combined.json"
    stale_combined.write_text(
        json.dumps(
            [
                repair_item,
                {
                    "application_id": "99",
                    "company": "Stale Recovery",
                    "title": "Already Submitted",
                    "recovery_verified": True,
                    "retry_scope": "single_application",
                },
            ]
        )
    )
    state["phase"] = "repair_verified"
    state["artifacts"]["repair_retry_batch"] = str(repair_batch)
    state["artifacts"]["scoped_retry_batch"] = str(stale_combined)
    state["artifacts"].pop("recovery_retry_batch", None)
    manifest["artifacts"]["repair_retry_batch"] = str(repair_batch)
    manifest["artifacts"]["scoped_retry_batch"] = str(stale_combined)
    manifest["artifacts"].pop("recovery_retry_batch", None)
    state_path.write_text(json.dumps(state))
    manifest_path.write_text(json.dumps(manifest))
    executed: list[Path] = []

    def fake_execute(*_args, **kwargs):
        executed.append(Path(kwargs["_retry_summary_path"]))
        return run_dir

    monkeypatch.setattr(daily_sop_module, "execute_daily_run", fake_execute)

    repaired = repair_daily_run(
        config,
        run_dir=run_dir,
        retry_verified=True,
    )

    assert repaired == run_dir
    assert executed == [repair_batch]
    assert json.loads(executed[0].read_text()) == [repair_item]
    refreshed_state = json.loads(state_path.read_text())
    assert refreshed_state["artifacts"]["scoped_retry_batch"] == str(
        repair_batch
    )


def test_repair_resume_rebuilds_current_audit_and_accepts_already_fixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    state_path = run_dir / "run-state.json"
    manifest_path = run_dir / "pipeline-manifest.json"
    state = json.loads(state_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    repairable_dir = run_dir / "applications" / "001-repairable"
    (repairable_dir / "autofill-runtime.js").write_text(
        "const CFG = "
        + json.dumps(
            {
                "profile": {
                    "answers": {"Country": "United States"},
                    "screening_answer_rules": [],
                }
            }
        )
        + ";\n"
    )
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "progress": {"complete": True},
                "applications": [
                    {
                        "company": "Repairable",
                        "title": "Engineer",
                        "package_dir": str(repairable_dir),
                        "status": "autofill_failed",
                        "review_items": [
                            {
                                "label": "Application form",
                                "reason": (
                                    "no visible job-application form was found"
                                ),
                                "sensitive": False,
                                "blocking": True,
                            },
                            {
                                "label": "Country",
                                "reason": (
                                    "fill error: combobox made no progress "
                                    "before field repair deadline"
                                ),
                                "sensitive": False,
                                "blocking": True,
                            },
                        ],
                    },
                    {
                        "company": "Netic",
                        "title": "Engineer",
                        "status": "autofill_completed_blocked",
                        "review_items": [
                            {
                                "label": (
                                    "What's the most interesting paper, blog post, "
                                    "or documentation you've read in the past month?"
                                ),
                                "reason": "unmapped field",
                                "sensitive": False,
                                "blocking": True,
                            }
                        ],
                    },
                ],
            }
        )
    )
    state["artifacts"]["execution_audit"] = str(audit_path)
    manifest["artifacts"]["execution_audit"] = str(audit_path)
    state_path.write_text(json.dumps(state))
    manifest_path.write_text(json.dumps(manifest))
    captured_requests: list[dict[str, object]] = []

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: RepairAgentReadiness(
            True,
            "ready",
            "ready",
            "codex",
        ),
    )

    def already_fixed(*args, **kwargs):
        request = dict(kwargs["request"])
        captured_requests.append(request)
        result_path = (
            run_dir
            / "repair"
            / "repair-result-attempt-03-cycle-01.json"
        )
        result = {
            "schema_version": 1,
            "attempt": 3,
            "cycle": 1,
            "status": "already_fixed_verified",
            "reason": "repair_agent_made_no_changes_all_verification_passed",
            "changed_files": [],
            "verification": [{"command": ["verify"], "status": "passed"}],
            "result_path": str(result_path),
        }
        result_path.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(daily_sop_module, "run_repair_cycle", already_fixed)

    repaired = repair_daily_run(config, run_dir=run_dir)

    assert repaired == run_dir
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request["rebuilt_from_audit"] == str(audit_path)
    assert "supersedes_retained_request" in request
    codes = {
        fingerprint["code"]
        for finding in request["findings"]
        for fingerprint in finding["fingerprints"]
    }
    assert "application_form_navigation_failure" in codes
    assert "country_combobox_commit_mismatch" in codes
    assert "unmapped_field_classification_gap" not in codes
    final_state = json.loads(state_path.read_text())
    assert final_state["phase"] == "repair_verified"
    assert final_state["repair_cycles"][-1]["status"] == (
        "already_fixed_verified"
    )
    verified_event = next(
        event
        for event in reversed(final_state["history"])
        if event["phase"] == "repair_verified"
    )
    assert verified_event["changed_files"] == []
    assert Path(verified_event["scoped_retry_batch"]).is_file()


def test_repair_resume_persists_rebuilt_scope_before_readiness_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    state_path = run_dir / "run-state.json"
    manifest_path = run_dir / "pipeline-manifest.json"
    state = json.loads(state_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    repairable_dir = run_dir / "applications" / "001-repairable"
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "progress": {"complete": True},
                "applications": [
                    {
                        "company": "Repairable",
                        "title": "Engineer",
                        "package_dir": str(repairable_dir),
                        "status": "autofill_failed",
                        "review_items": [
                            {
                                "label": "Application form",
                                "reason": (
                                    "no visible job-application form was found"
                                ),
                                "sensitive": False,
                                "blocking": True,
                            }
                        ],
                    },
                    {
                        "company": "Netic",
                        "title": "Engineer",
                        "status": "autofill_completed_blocked",
                        "review_items": [
                            {
                                "label": (
                                    "What's the most interesting paper, blog post, "
                                    "or documentation you've read in the past month?"
                                ),
                                "reason": "unmapped field",
                                "sensitive": False,
                                "blocking": True,
                            }
                        ],
                    },
                ],
            }
        )
    )
    state["artifacts"]["execution_audit"] = str(audit_path)
    manifest["artifacts"]["execution_audit"] = str(audit_path)
    state_path.write_text(json.dumps(state))
    manifest_path.write_text(json.dumps(manifest))
    repair_calls: list[object] = []

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: pytest.fail(
            "refresh-request-only must not check Codex readiness"
        ),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "run_repair_cycle",
        lambda *args, **kwargs: repair_calls.append((args, kwargs)),
    )

    refreshed = repair_daily_run(
        config,
        run_dir=run_dir,
        refresh_request_only=True,
    )

    assert refreshed == run_dir
    assert repair_calls == []
    refreshed_state = json.loads(state_path.read_text())
    assert refreshed_state["phase"] == "repair_exhausted"
    assert refreshed_state["consumed_repair_cycles"] == 0
    first_request_path = Path(
        refreshed_state["artifacts"]["repair_request"]
    )
    assert first_request_path.name == (
        "repair-request-refresh-01-cycle-01.json"
    )

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: RepairAgentReadiness(
            False,
            "repair_agent_authentication_failed",
            "authentication failed",
            "codex",
        ),
    )

    with pytest.raises(SopError, match="current-audit scoped request"):
        repair_daily_run(config, run_dir=run_dir)

    assert repair_calls == []
    final_state = json.loads(state_path.read_text())
    assert final_state["phase"] == "repair_unavailable"
    assert final_state["consumed_repair_cycles"] == 0
    assert final_state.get("repair_attempts", []) == []
    assert len(final_state["repair_request_refreshes"]) == 2
    request_path = Path(final_state["artifacts"]["repair_request"])
    assert request_path.name == "repair-request-refresh-02-cycle-01.json"
    request = json.loads(request_path.read_text())
    assert request["rebuilt_from_audit"] == str(audit_path)
    assert "supersedes_retained_request" in request
    assert {
        fingerprint["code"]
        for finding in request["findings"]
        for fingerprint in finding["fingerprints"]
    } == {"application_form_navigation_failure"}
    assert [finding["company"] for finding in request["findings"]] == [
        "Repairable"
    ]
    final_manifest = json.loads(manifest_path.read_text())
    assert final_manifest["artifacts"]["repair_request"] == str(request_path)


def test_repair_resume_rechecks_one_transient_authentication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    readiness_results = iter(
        [
            RepairAgentReadiness(
                False,
                "repair_agent_authentication_failed",
                "token refresh in progress",
                "codex",
            ),
            RepairAgentReadiness(True, "ready", "ready", "codex"),
        ]
    )
    readiness_calls = 0

    def readiness(_policy):
        nonlocal readiness_calls
        readiness_calls += 1
        return next(readiness_results)

    def already_fixed(*_args, **_kwargs):
        result_path = run_dir / "repair" / "transient-auth-result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result = {
            "status": "already_fixed_verified",
            "reason": "repair_agent_made_no_changes_all_verification_passed",
            "changed_files": [],
            "verification": [{"command": ["verify"], "status": "passed"}],
            "result_path": str(result_path),
        }
        result_path.write_text(json.dumps(result))
        return result

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        readiness,
    )
    monkeypatch.setattr(daily_sop_module, "run_repair_cycle", already_fixed)

    repaired = repair_daily_run(config, run_dir=run_dir)

    assert repaired == run_dir
    assert readiness_calls == 2
    state = json.loads((run_dir / "run-state.json").read_text())
    assert state["phase"] == "repair_verified"
    assert state["consumed_repair_cycles"] == 1


def test_latest_verified_repair_attempt_survives_later_failed_attempt(
    tmp_path: Path,
) -> None:
    repair_dir = tmp_path / "repair"
    repair_dir.mkdir()
    verified_result = repair_dir / "repair-result-cycle-05.json"
    verified_result.write_text(
        json.dumps(
            {
                "status": "already_fixed_verified",
                "changed_files": [],
                "verification": [
                    {"command": ["target"], "status": "passed"},
                    {"command": ["full"], "status": "passed"},
                ],
            }
        )
    )
    (repair_dir / "repair-request-cycle-05.json").write_text(
        json.dumps({"findings": [], "retry_targets": []})
    )
    state = {
        "repair_attempts": [
            {
                "attempt": 5,
                "cycle": 5,
                "status": "already_fixed_verified",
                "result_path": str(verified_result),
            },
            {
                "attempt": 6,
                "cycle": 5,
                "status": "verification_failed",
            },
        ]
    }

    recovered = daily_sop_module._latest_verified_repair_attempt(
        state,
        run_dir=tmp_path,
    )

    assert recovered is not None
    attempt, result, request = recovered
    assert attempt["attempt"] == 5
    assert result["status"] == "already_fixed_verified"
    assert request["findings"] == []


def test_repair_resume_supersedes_stale_scope_when_complete_audit_has_only_candidate_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, run_dir, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    state_path = run_dir / "run-state.json"
    manifest_path = run_dir / "pipeline-manifest.json"
    state = json.loads(state_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "counts": {"completed": 1},
                "progress": {
                    "planned": 1,
                    "terminal": 1,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "company": "Palantir",
                        "title": "Software Engineer, New Grad",
                        "status": "autofill_completed_blocked",
                        "review_items": [
                            {
                                "label": "High School Name",
                                "reason": "unmapped field",
                                "sensitive": False,
                                "blocking": True,
                            },
                            {
                                "label": (
                                    "Do you have any, or anticipate any "
                                    "upcoming offer deadlines?"
                                ),
                                "reason": (
                                    "candidate fact needs explicit approved answer"
                                ),
                                "sensitive": False,
                                "blocking": True,
                            },
                        ],
                    }
                ],
            }
        )
    )
    state["phase"] = "repair_unavailable"
    state["artifacts"]["execution_audit"] = str(audit_path)
    manifest["artifacts"]["execution_audit"] = str(audit_path)
    state_path.write_text(json.dumps(state))
    manifest_path.write_text(json.dumps(manifest))

    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: pytest.fail(
            "an empty current-audit repair scope must not check Codex readiness"
        ),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "run_repair_cycle",
        lambda *args, **kwargs: pytest.fail(
            "an empty current-audit repair scope must not run Codex"
        ),
    )

    refreshed = repair_daily_run(
        config,
        run_dir=run_dir,
        refresh_request_only=True,
    )

    assert refreshed == run_dir
    final_state = json.loads(state_path.read_text())
    assert final_state["phase"] == "executed_with_blockers"
    assert final_state["consumed_repair_cycles"] == 0
    request_path = Path(final_state["artifacts"]["repair_request"])
    request = json.loads(request_path.read_text())
    assert request["no_repairable_scope"] is True
    assert request["findings"] == []
    assert request["retry_targets"] == []
    assert request["rebuilt_from_audit"] == str(audit_path)
    assert "supersedes_retained_request" in request
    assert (
        final_state["history"][-1]["repair_reason"]
        == "current_audit_has_no_repairable_scope"
    )
    assert "Restore the repair-agent session" not in (
        run_dir / "RUN_SUMMARY.md"
    ).read_text()


def test_historical_recover_replays_plans_without_browser_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    run_dir = config.output_root / "2026-07-27" / "221010"
    package_dir = run_dir / "applications" / "001-netic"
    package_dir.mkdir(parents=True)
    batch_summary = run_dir / "applications" / "batch-summary.json"
    batch_summary.write_text(
        json.dumps(
            [
                {
                    "company": "Netic",
                    "title": "Engineer",
                    "package_dir": str(package_dir),
                    "application_id": "17",
                }
            ]
        )
    )
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "completed": 1,
                    "submitted": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                "progress": {
                    "planned": 1,
                    "terminal": 1,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "company": "Netic",
                        "title": "Engineer",
                        "package_dir": str(package_dir),
                        "application_id": "17",
                        "status": "autofill_completed_blocked",
                        "review_items": [
                            {
                                "label": (
                                    "What's the most interesting paper, blog post, "
                                    "or documentation you've read in the past month?"
                                ),
                                "reason": "unmapped field",
                                "sensitive": False,
                                "blocking": True,
                            }
                        ],
                        "recovery_plan": {
                            "strategy": "bounded_field_recovery"
                        },
                    }
                ],
            }
        )
    )
    manifest_path = run_dir / "pipeline-manifest.json"
    stale_retry_batch = run_dir / "recovery" / "retry-batch-recovery-01.json"
    stale_retry_batch.parent.mkdir()
    stale_retry_batch.write_text(
        json.dumps(
            [
                {
                    "company": "Stale",
                    "title": "Already Resolved",
                    "recovery_verified": True,
                    "retry_scope": "single_application",
                }
            ]
        )
    )
    manifest_path.write_text(
        json.dumps(
            {
                "counts": {
                    "imported": 1,
                    "shortlisted": 1,
                    "prepared": 1,
                },
                "artifacts": {
                    "batch_summary": str(batch_summary),
                    "execution_audit": str(audit_path),
                    "recovery_retry_batch": str(stale_retry_batch),
                },
            }
        )
    )
    state_path = run_dir / "run-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "phase": "repair_unavailable",
                "created_at": "2026-07-27T22:10:10-04:00",
                "updated_at": "2026-07-28T09:17:54-04:00",
                "config_path": str(config.config_path),
                "settings": config.snapshot(),
                "artifacts": {
                    "manifest": str(manifest_path),
                    "batch_summary": str(batch_summary),
                    "execution_audit": str(audit_path),
                    "recovery_retry_batch": str(stale_retry_batch),
                },
                "execution_attempts": [],
                "history": [],
            }
        )
    )

    recovered = recover_daily_run(config, run_dir=run_dir)

    assert recovered == run_dir
    execution_path = run_dir / "recovery-execution.json"
    execution = json.loads(execution_path.read_text())
    assert execution["status_counts"] == {"waiting_for_user": 1}
    assert execution["applications"][0]["recovery_plan"]["strategy"] == (
        "candidate_fact_resolution"
    )
    assert execution["applications"][0]["recovery_execution"]["strategy"] == (
        "candidate_fact_resolution"
    )
    updated_audit = json.loads(audit_path.read_text())
    record = updated_audit["applications"][0]
    assert record["recovery_plan"]["strategy"] == "candidate_fact_resolution"
    assert record["recovery_execution"]["status"] == "waiting_for_user"
    updated_state = json.loads(state_path.read_text())
    assert updated_state["phase"] == "repair_unavailable"
    assert updated_state["artifacts"]["recovery_execution"] == str(
        execution_path
    )
    assert "recovery_retry_batch" not in updated_state["artifacts"]
    updated_manifest = json.loads(manifest_path.read_text())
    assert "recovery_retry_batch" not in updated_manifest["artifacts"]
    assert len(updated_state["recovery_attempts"]) == 1
    assert "not_run" not in (run_dir / "RUN_SUMMARY.md").read_text()


def test_historical_recover_rebuilds_package_after_candidate_fact_is_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import job_agent.cli as cli_module

    config = _config(tmp_path, monkeypatch)
    config.config_path.write_text('{"schema_version": 1}\n')
    old_inputs = fingerprint_inputs(config)
    config.source_config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "type": "remotive",
                        "search": "new source configuration",
                        "limit": 5,
                    }
                ]
            }
        )
    )
    relationship_label = (
        "Are you related to, or in a close personal relationship with, "
        "anyone who currently works for Example?"
    )
    config.profile.write_text(
        json.dumps(
            {
                "name": "Test Candidate",
                "email": "candidate@example.com",
                "phone": "+1 555 0100",
                "answers": {relationship_label: "No"},
            }
        )
    )

    run_dir = config.output_root / "2026-07-29" / "101010"
    original_package = run_dir / "applications" / "001-example"
    original_package.mkdir(parents=True)
    (original_package / "autofill-runtime.js").write_text(
        "const CFG = "
        + json.dumps(
            {
                "profile": {
                    "name": "Test Candidate",
                    "email": "candidate@example.com",
                    "phone": "+1 555 0100",
                    "answers": {},
                }
            }
        )
        + ";\n"
    )
    jobs_path = run_dir / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "company": "Example",
                    "title": "Engineer",
                    "apply_url": "https://example.test/jobs/42",
                    "raw_jd": "Build reliable systems.",
                }
            ]
        )
    )
    batch_summary = run_dir / "applications" / "batch-summary.json"
    batch_summary.write_text(
        json.dumps(
            [
                {
                    "company": "Example",
                    "title": "Engineer",
                    "apply_url": "https://example.test/jobs/42",
                    "package_dir": str(original_package),
                    "runtime_script_path": str(
                        original_package / "autofill-runtime.js"
                    ),
                    "application_id": "17",
                }
            ]
        )
    )
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "completed": 1,
                    "submitted": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                "progress": {
                    "planned": 1,
                    "terminal": 1,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "company": "Example",
                        "title": "Engineer",
                        "apply_url": "https://example.test/jobs/42",
                        "package_dir": str(original_package),
                        "application_id": "17",
                        "status": "autofill_completed_blocked",
                        "review_items": [
                            {
                                "label": relationship_label,
                                "reason": (
                                    "combobox needs saved answer / manual selection"
                                ),
                                "sensitive": False,
                                "blocking": True,
                            }
                        ],
                    }
                ],
            }
        )
    )
    manifest_path = run_dir / "pipeline-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "counts": {
                    "imported": 1,
                    "shortlisted": 1,
                    "prepared": 1,
                },
                "artifacts": {
                    "jobs": str(jobs_path),
                    "batch_summary": str(batch_summary),
                    "execution_audit": str(audit_path),
                },
                "daily_sop": {"input_sha256": old_inputs},
            }
        )
    )
    state_path = run_dir / "run-state.json"
    historical_settings = config.snapshot()
    historical_settings["evaluation"] = {
        "imported_cohort_target": 500,
        "min_confirmed_submission_rate": 0.8,
        "min_terminal_audit_coverage": 1.0,
    }
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "phase": "executed_with_blockers",
                "created_at": "2026-07-29T10:10:10-04:00",
                "updated_at": "2026-07-29T10:12:10-04:00",
                "config_path": str(config.config_path),
                "config_sha256": "fixture",
                "input_sha256": old_inputs,
                "settings": historical_settings,
                "artifacts": {
                    "manifest": str(manifest_path),
                    "jobs": str(jobs_path),
                    "batch_summary": str(batch_summary),
                    "execution_audit": str(audit_path),
                },
                "execution_attempts": [],
                "history": [],
            }
        )
    )

    def fake_prepare(job, out_dir, **_kwargs):
        out_dir.mkdir(parents=True)
        runtime_script = out_dir / "autofill-runtime.js"
        runtime_script.write_text("// rebuilt with approved facts")
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(out_dir),
            "runtime_script_path": str(runtime_script),
            "application_id": "17",
        }

    monkeypatch.setattr(
        cli_module,
        "_prepare_application_package",
        fake_prepare,
    )

    recovered = recover_daily_run(config, run_dir=run_dir)

    assert recovered == run_dir
    recovery = json.loads(
        (run_dir / "recovery-execution.json").read_text()
    )
    assert recovery["status_counts"] == {"verified": 1}
    assert recovery["verified_targets"][0]["recovery_strategy"] == (
        "candidate_fact_resolution"
    )
    retry_path = Path(
        json.loads(state_path.read_text())["artifacts"][
            "recovery_retry_batch"
        ]
    )
    retry_item = json.loads(retry_path.read_text())[0]
    assert retry_item["application_id"] == "17"
    assert retry_item["recovery_verified"] is True
    assert retry_item["retry_scope"] == "single_application"
    assert "/recovery/candidate-facts-" in retry_item["package_dir"]
    assert json.loads(state_path.read_text())["input_sha256"] == (
        fingerprint_inputs(config)
    )
    assert json.loads(state_path.read_text())["config_sha256"] == hashlib.sha256(
        config.config_path.read_bytes()
    ).hexdigest()


def test_recovery_merge_reads_all_attempts_and_preserves_attempt_files(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    original_path = run_dir / "execution-audit.json"
    retry_path = run_dir / "execution-audit-retry-01.json"
    original_path.write_text(
        json.dumps(
            {
                "progress": {
                    "planned": 2,
                    "terminal": 2,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "application_id": "1",
                        "company": "Acme",
                        "title": "Engineer",
                        "status": "autofill_completed_blocked",
                    },
                    {
                        "application_id": "2",
                        "company": "Collective",
                        "title": "Fullstack Engineer",
                        "status": "autofill_completed_blocked",
                    },
                ],
            }
        )
    )
    retry_path.write_text(
        json.dumps(
            {
                "progress": {
                    "planned": 1,
                    "terminal": 1,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "application_id": "1",
                        "company": "Acme",
                        "title": "Engineer",
                        "status": "submitted",
                    }
                ],
            }
        )
    )
    state = {
        "execution_attempts": [
            {"attempt": 1, "audit": str(original_path)},
            {"attempt": 2, "audit": str(retry_path)},
        ]
    }

    merged = daily_sop_module._execution_audit_for_report(
        state,
        run_dir=run_dir,
        root=tmp_path,
        fallback=json.loads(retry_path.read_text()),
    )

    assert {
        item["application_id"]: item["status"]
        for item in merged["applications"]
    } == {
        "1": "submitted",
        "2": "autofill_completed_blocked",
    }
    target = next(
        item
        for item in merged["applications"]
        if item["application_id"] == "2"
    )
    target["recovery_plan"] = {"strategy": "candidate_fact_resolution"}
    target["recovery_execution"] = {"status": "verified"}

    daily_sop_module._persist_recovery_annotations(
        state,
        run_dir=run_dir,
        root=tmp_path,
        fallback_path=retry_path,
        recovery_audit=merged,
    )

    original = json.loads(original_path.read_text())
    retry = json.loads(retry_path.read_text())
    original_target = next(
        item
        for item in original["applications"]
        if item["application_id"] == "2"
    )
    assert original_target["recovery_execution"]["status"] == "verified"
    assert retry["applications"] == [
        {
            "application_id": "1",
            "company": "Acme",
            "title": "Engineer",
            "status": "submitted",
        }
    ]


def test_candidate_fact_recovery_requires_new_fact_and_no_mixed_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    relationship_label = (
        "Are you related to, or in a close personal relationship with, "
        "anyone who currently works for Example?"
    )
    config.profile.write_text(
        json.dumps(
            {
                "name": "Test Candidate",
                "email": "candidate@example.com",
                "phone": "+1 555 0100",
                "answers": {relationship_label: "No"},
            }
        )
    )
    run_dir = config.output_root / "2026-07-29" / "111111"
    unchanged_package = run_dir / "applications" / "unchanged"
    unchanged_package.mkdir(parents=True)
    (unchanged_package / "autofill-runtime.js").write_text(
        "const CFG = "
        + json.dumps(
            {
                "profile": {
                    "answers": {relationship_label: "No"},
                }
            }
        )
        + ";\n"
    )
    mixed_package = run_dir / "applications" / "mixed"
    mixed_package.mkdir(parents=True)
    (mixed_package / "autofill-runtime.js").write_text(
        "const CFG = "
        + json.dumps({"profile": {"answers": {}}})
        + ";\n"
    )
    jobs_path = run_dir / "jobs.json"
    jobs_path.write_text("[]")
    handlers = daily_sop_module._candidate_fact_recovery_handlers(
        config,
        run_dir=run_dir,
        jobs_path=jobs_path,
        attempt_number=1,
    )
    fact_item = {
        "label": relationship_label,
        "reason": "combobox needs saved answer / manual selection",
        "sensitive": False,
        "blocking": True,
    }

    unchanged = handlers["request_candidate_facts"](
        None,
        {
            "package_dir": str(unchanged_package),
            "review_items": [fact_item],
        },
        {},
    )
    mixed = handlers["request_candidate_facts"](
        None,
        {
            "package_dir": str(mixed_package),
            "review_items": [
                fact_item,
                {
                    "label": "Describe prior startup work",
                    "reason": "unmapped field",
                    "sensitive": False,
                    "blocking": True,
                },
            ],
        },
        {},
    )

    # An existing approved answer is enough to rebuild and retry after the
    # closest-match runtime fix; only genuinely unresolved facts wait for user.
    assert unchanged["status"] == "completed"
    assert unchanged["evidence"] == ["approved_candidate_facts"]
    assert mixed["status"] == "completed"
    assert mixed["evidence"] == ["approved_candidate_facts"]


def test_expired_repair_auth_is_a_nonblocking_preflight_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, _ = _repair_resume_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        daily_sop_module,
        "check_repair_agent_readiness",
        lambda _policy: RepairAgentReadiness(
            False,
            "repair_agent_authentication_failed",
            "Repair-agent authentication is unavailable.",
            "codex",
        ),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "_check_playwright",
        lambda _checks: None,
    )

    report = run_preflight(config)

    repair_check = next(
        check for check in report.checks if check.name == "automatic repair"
    )
    assert repair_check.level == "WARN"
    assert "repair_unavailable" in repair_check.message
    assert report.ok is True


def test_incremental_repair_starts_before_execution_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    config = replace(
        config,
        auto_repair=RepairPolicy(
            enabled=True,
            max_cycles=1,
            agent_binary="codex",
        ),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    audit_path = run_dir / "execution-audit.json"
    repair_started = threading.Event()

    def fake_command(_command, _config):
        audit_path.write_text(
            json.dumps(
                {
                    "applications": [
                        {
                            "company": "Example",
                            "title": "Engineer",
                            "status": "autofill_completed_blocked",
                            "review_items": [
                                {
                                    "label": "New ATS option control",
                                    "reason": (
                                        "combobox adapter mapping is unavailable"
                                    ),
                                    "sensitive": False,
                                    "blocking": True,
                                }
                            ],
                        }
                    ]
                }
            )
        )
        assert repair_started.wait(timeout=2)
        return 0

    def fake_repair(*args, **kwargs):
        assert kwargs["defer_promotion"] is True
        repair_started.set()
        return {
            "status": "agent_failed",
            "reason": "fixture",
            "changed_files": [],
            "result_path": str(run_dir / "repair-result.json"),
        }

    monkeypatch.setattr(daily_sop_module, "_run_command", fake_command)
    monkeypatch.setattr(daily_sop_module, "run_repair_cycle", fake_repair)

    exit_code, outcome = daily_sop_module._run_execution_command(
        ["job-agent", "applications", "execute-batch"],
        config,
        audit_path=audit_path,
        run_dir=run_dir,
        repair_cycle=1,
        poll_seconds=0.01,
    )

    assert exit_code == 0
    assert outcome is not None
    assert outcome.result["status"] == "agent_failed"


def test_run_until_daily_target_continues_after_complete_blocked_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    payload = workspace["payload"]
    assert isinstance(payload, dict)
    payload["limit"] = 1
    payload["daily_submit_target"] = 1
    payload["submit_complete"] = True
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    config = DailyConfig.from_mapping(
        payload,
        config_path=tmp_path / "daily.local.json",
        root=tmp_path,
    )

    connection = connect(config.database)
    applications = []
    for index in range(2):
        job = Job(
            title=f"Engineer {index}",
            company=f"Company {index}",
            raw_jd="Build systems.",
            source="test",
            apply_url=f"https://jobs.example.com/{index}",
        )
        applications.append(
            create_application(connection, create_job(connection, job), job)
        )
    connection.close()

    prepared_runs: list[Path] = []

    def fake_prepare(_config: DailyConfig) -> Path:
        run_dir = create_run_dir(config.output_root)
        prepared_runs.append(run_dir)
        (run_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_dir.name,
                    "phase": "prepared",
                    "created_at": datetime.now().astimezone().isoformat(),
                    "updated_at": datetime.now().astimezone().isoformat(),
                    "config_path": str(config.config_path),
                    "config_sha256": "test",
                    "settings": config.snapshot(),
                    "artifacts": {},
                    "execution_attempts": [],
                    "history": [],
                }
            )
        )
        (run_dir / "pipeline-manifest.json").write_text(
            json.dumps(
                {
                    "counts": {"imported": 1, "shortlisted": 1, "prepared": 1},
                    "artifacts": {},
                }
            )
        )
        return run_dir

    def fake_execute(
        _config: DailyConfig,
        *,
        run_dir: Path | None = None,
        **_kwargs: object,
    ) -> Path:
        assert run_dir is not None
        index = prepared_runs.index(run_dir)
        connection = connect(config.database)
        status = "autofill_completed_blocked" if index == 0 else "submitted"
        update_application_execution_status(connection, applications[index], status)
        connection.close()
        (run_dir / "execution-audit.json").write_text(
            json.dumps(
                {
                    "counts": {
                        "total": 1,
                        "completed": int(index == 0),
                        "submitted": int(index == 1),
                    },
                    "progress": {
                        "planned": 1,
                        "terminal": 1,
                        "remaining": 0,
                        "complete": True,
                    },
                    "applications": [],
                }
            )
        )
        state_path = run_dir / "run-state.json"
        state = json.loads(state_path.read_text())
        state["phase"] = "executed_with_blockers" if index == 0 else "executed"
        state["history"] = [{"phase": state["phase"]}]
        state_path.write_text(json.dumps(state))
        return run_dir

    monkeypatch.setattr(daily_sop_module, "prepare_daily_run", fake_prepare)
    monkeypatch.setattr(daily_sop_module, "execute_daily_run", fake_execute)

    final_run = run_until_daily_target(config)

    assert final_run == prepared_runs[1]
    assert len(prepared_runs) == 2
    assert daily_submission_progress(config)["submitted"] == 1


def test_run_until_daily_target_does_not_stop_at_absolute_floor_when_raw_rate_is_unmet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    payload = workspace["payload"]
    assert isinstance(payload, dict)
    payload["daily_submit_target"] = 1
    payload["submit_complete"] = True
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    config = DailyConfig.from_mapping(
        payload,
        config_path=tmp_path / "daily.local.json",
        root=tmp_path,
    )

    connection = connect(config.database)
    job = Job(
        title="Already Confirmed",
        company="Confirmed Co",
        raw_jd="Build systems.",
        source="test",
        apply_url="https://jobs.example.com/confirmed",
    )
    application_id = create_application(
        connection,
        create_job(connection, job),
        job,
    )
    update_application_execution_status(
        connection,
        application_id,
        "submitted",
    )
    connection.close()

    previous_run = config.output_root / "current" / "run"
    previous_run.mkdir(parents=True)
    local_date = datetime.now().astimezone().date().isoformat()
    (previous_run / "run-state.json").write_text(
        json.dumps(
            {
                "daily_target": {
                    "local_date": local_date,
                    "submitted": 1,
                }
            }
        )
    )
    (previous_run / "pipeline-manifest.json").write_text(
        json.dumps({"counts": {"imported": 2}})
    )
    config.output_root.mkdir(parents=True, exist_ok=True)
    (config.output_root / "latest.json").write_text(
        json.dumps({"run_dir": str(previous_run)})
    )

    def fail_if_preparation_continues(_config: DailyConfig) -> Path:
        raise SopError("raw-rate-target-still-unmet")

    monkeypatch.setattr(
        daily_sop_module,
        "prepare_daily_run",
        fail_if_preparation_continues,
    )

    with pytest.raises(SopError, match="raw-rate-target-still-unmet"):
        run_until_daily_target(config)


def test_daily_config_loads_bounded_auto_repair_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    workspace["payload"]["auto_repair"] = {
        "enabled": True,
        "max_cycles": 2,
        "agent_binary": "codex",
        "agent_timeout_seconds": 900,
        "verification_timeout_seconds": 1200,
        "combobox_no_progress_seconds": 20,
        "retry_after_verified_repair": True,
    }
    workspace["payload"]["evaluation"] = {
        "imported_cohort_target": 500,
        "confirmation_rate_denominator": "raw_imported",
        "min_confirmed_submission_rate": 0.8,
        "min_terminal_audit_coverage": 1.0,
    }
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))

    config = DailyConfig.from_mapping(
        workspace["payload"],
        config_path=tmp_path / "daily.local.json",
        root=tmp_path,
    )

    assert config.auto_repair.enabled is True
    assert config.auto_repair.max_cycles == 2
    assert config.auto_repair.agent_binary == "codex"
    assert config.auto_repair.agent_timeout_seconds == 900
    assert config.auto_repair.verification_timeout_seconds == 1200
    assert config.auto_repair.combobox_no_progress_seconds == 20
    assert config.auto_repair.retry_after_verified_repair is True
    assert config.evaluation.imported_cohort_target == 500
    assert (
        config.evaluation.confirmation_rate_denominator
        == "raw_imported"
    )
    assert config.evaluation.min_confirmed_submission_rate == 0.8
    assert config.evaluation.min_terminal_audit_coverage == 1.0


def test_daily_config_caps_auto_repair_at_five_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    workspace["payload"]["auto_repair"] = {
        "enabled": True,
        "max_cycles": 5,
    }

    config = DailyConfig.from_mapping(
        workspace["payload"],
        config_path=tmp_path / "daily.local.json",
        root=tmp_path,
    )
    assert config.auto_repair.max_cycles == 5

    workspace["payload"]["auto_repair"]["max_cycles"] = 6
    with pytest.raises(SopError, match="between 1 and 5"):
        DailyConfig.from_mapping(
            workspace["payload"],
            config_path=tmp_path / "daily.local.json",
            root=tmp_path,
        )


def test_daily_config_rejects_non_raw_confirmation_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    workspace["payload"]["evaluation"] = {
        "confirmation_rate_denominator": "final_eligible",
    }
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))

    with pytest.raises(
        SopError,
        match="confirmation_rate_denominator.*raw_imported",
    ):
        DailyConfig.from_mapping(
            workspace["payload"],
            config_path=tmp_path / "daily.local.json",
            root=tmp_path,
        )


def test_daily_config_rejects_missing_environment_variable(
    tmp_path: Path,
) -> None:
    workspace = _write_workspace(tmp_path)

    with pytest.raises(SopError, match="TEST_RESUME_DIR"):
        DailyConfig.from_mapping(
            workspace["payload"],
            config_path=tmp_path / "daily.local.json",
            root=tmp_path,
            environ={},
        )


def test_preflight_checks_complete_workspace_without_live_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)

    report = run_preflight(config, check_runtime=False)

    assert report.ok is True
    assert report.error_count == 0
    assert any(
        check.name == "resume directory" and check.level == "PASS"
        for check in report.checks
    )


def test_preflight_stops_on_placeholder_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.profile.write_text(
        json.dumps(
            {
                "name": "Test Candidate",
                "email": "candidate@example.com",
                "phone": "TBD",
            }
        )
    )

    report = run_preflight(config, check_runtime=False)

    assert report.ok is False
    assert any(
        check.name == "profile"
        and check.level == "ERROR"
        and "phone" in check.message
        for check in report.checks
    )


def test_daily_commands_are_built_from_single_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    run_dir = tmp_path / "output" / "daily" / "2026-07-25" / "090000"
    prepare = build_prepare_command(config, run_dir)
    execute = build_execute_command(
        config,
        summary_path=run_dir / "applications" / "batch-summary.json",
        audit_path=run_dir / "execution-audit.json",
        preflight_path=run_dir / "resume-preflight.json",
        retry=False,
    )

    assert prepare[1:4] == ["pipeline", "run", str(config.source_config)]
    assert prepare[prepare.index("--limit") + 1] == "4"
    assert "--use-llm" in prepare
    assert execute[1:3] == ["applications", "execute-batch"]
    assert "--required-resume-source-dir" in execute
    assert "--headless" in execute
    assert "--llm-answers" in execute
    assert "--retry-prior-terminal-outcome" not in execute
    assert "--resume-existing-audit" not in execute

    resume_execute = build_execute_command(
        config,
        summary_path=run_dir / "applications" / "batch-summary.json",
        audit_path=run_dir / "execution-audit.json",
        preflight_path=run_dir / "resume-preflight.json",
        retry=False,
        resume_existing_audit=True,
    )
    assert "--resume-existing-audit" in resume_execute
    assert "--retry-prior-terminal-outcome" not in resume_execute


def test_execute_daily_run_resumes_only_incomplete_canonical_audit_after_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    monkeypatch.setenv("JOB_AGENT_CLI", str(workspace["cli"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(workspace["payload"]))
    config = DailyConfig.load(config_path, root=tmp_path)

    run_dir = config.output_root / "2026-07-27" / "160625"
    applications_dir = run_dir / "applications"
    applications_dir.mkdir(parents=True)
    summary_items = [
        {
            "company": f"Company {index}",
            "title": f"Role {index}",
            "runtime_script_path": str(
                applications_dir / f"{index:03d}" / "autofill-runtime.js"
            ),
        }
        for index in range(1, 4)
    ]
    summary_path = applications_dir / "batch-summary.json"
    summary_path.write_text(json.dumps(summary_items))
    manifest_path = run_dir / "pipeline-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "counts": {"imported": 3, "shortlisted": 3, "prepared": 3},
                "artifacts": {"batch_summary": str(summary_path)},
            }
        )
    )
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "progress": {
                    "planned": 3,
                    "terminal": 1,
                    "remaining": 2,
                    "complete": False,
                },
                "applications": [
                    {
                        "company": "Company 1",
                        "title": "Role 1",
                        "script_path": summary_items[0]["runtime_script_path"],
                        "status": "submission_blocked_by_anti_spam",
                    }
                ],
            }
        )
    )
    state = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "phase": "execution_failed",
        "created_at": "2026-07-27T16:06:25+00:00",
        "updated_at": "2026-07-27T16:26:52+00:00",
        "config_path": str(config.config_path),
        "config_sha256": hashlib.sha256(config.config_path.read_bytes()).hexdigest(),
        "input_sha256": fingerprint_inputs(config),
        "settings": config.snapshot(),
        "artifacts": {
            "manifest": str(manifest_path),
            "execution_audit": str(audit_path),
        },
        "execution_attempts": [{"attempt": 1, "exit_code": -15}],
        "history": [
            {"at": "2026-07-27T16:26:52+00:00", "phase": "executing"}
        ],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))

    def fake_run_command(command: list[str], _config: DailyConfig) -> int:
        assert "--resume-existing-audit" in command
        assert "--retry-prior-terminal-outcome" not in command
        assert Path(command[command.index("--audit-out") + 1]) == audit_path
        audit_path.write_text(
            json.dumps(
                {
                    "counts": {
                        "total": 3,
                        "submitted": 1,
                        "completed": 0,
                        "failed": 0,
                        "skipped": 0,
                        "submit_clicked_unconfirmed": 1,
                        "submission_blocked_by_anti_spam": 1,
                    },
                    "progress": {
                        "planned": 3,
                        "terminal": 3,
                        "remaining": 0,
                        "complete": True,
                    },
                    "applications": [
                        {
                            "company": "Company 1",
                            "title": "Role 1",
                            "status": "submission_blocked_by_anti_spam",
                        },
                        {
                            "company": "Company 2",
                            "title": "Role 2",
                            "status": "submit_clicked_unconfirmed",
                        },
                        {
                            "company": "Company 3",
                            "title": "Role 3",
                            "status": "submitted",
                        },
                    ],
                }
            )
        )
        return 0

    monkeypatch.setattr(
        daily_sop_module,
        "run_preflight",
        lambda _config: daily_sop_module.PreflightReport(()),
    )
    monkeypatch.setattr(daily_sop_module, "_run_command", fake_run_command)

    execute_daily_run(
        config,
        run_dir=run_dir,
        resume_incomplete=True,
    )

    final_state = json.loads((run_dir / "run-state.json").read_text())
    assert final_state["phase"] == "executed_with_blockers"
    assert final_state["execution_attempts"][-1]["audit"] == str(audit_path)


def test_resume_incomplete_reconciles_complete_audit_without_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JOB_AGENT_CLI", str(workspace["cli"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(workspace["payload"]))
    config = DailyConfig.load(config_path, root=tmp_path)
    run_dir = config.output_root / "2026-08-10" / "141217"
    applications_dir = run_dir / "applications"
    applications_dir.mkdir(parents=True)
    summary_path = applications_dir / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "application_id": "1",
                    "company": "Example",
                    "title": "Engineer",
                    "package_dir": str(applications_dir / "001-example-engineer"),
                }
            ]
        )
    )
    manifest_path = run_dir / "pipeline-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "counts": {"imported": 1, "shortlisted": 1, "prepared": 1},
                "artifacts": {"batch_summary": str(summary_path)},
            }
        )
    )
    audit_path = run_dir / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "submitted": 1,
                    "completed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "submit_clicked_unconfirmed": 0,
                    "submission_processing_error": 0,
                    "submission_blocked_by_anti_spam": 0,
                },
                "progress": {
                    "planned": 1,
                    "terminal": 1,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "application_id": "1",
                        "company": "Example",
                        "title": "Engineer",
                        "status": "submitted",
                    }
                ],
            }
        )
    )
    (run_dir / "resume-preflight.json").write_text("{}")
    state = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "phase": "executing",
        "created_at": "2026-08-10T14:12:17-04:00",
        "updated_at": "2026-08-10T14:12:57-04:00",
        "config_path": str(config.config_path),
        "config_sha256": hashlib.sha256(config.config_path.read_bytes()).hexdigest(),
        # Deliberately stale: reconciliation must not consume current inputs.
        "input_sha256": "stale-after-browser-exit",
        "settings": config.snapshot(),
        "artifacts": {
            "manifest": str(manifest_path),
            "execution_audit": str(audit_path),
        },
        "execution_attempts": [],
        "history": [{"at": "2026-08-10T14:12:57-04:00", "phase": "executing"}],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))

    monkeypatch.setattr(
        daily_sop_module,
        "run_preflight",
        lambda _config: pytest.fail("reconciliation must not run browser preflight"),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "_run_execution_command",
        lambda *_args, **_kwargs: pytest.fail("reconciliation must not execute a browser"),
    )

    execute_daily_run(config, run_dir=run_dir, resume_incomplete=True)

    final_state = json.loads((run_dir / "run-state.json").read_text())
    assert final_state["phase"] == "executed"
    assert final_state["execution_attempts"][-1]["reconciled_complete_audit"] is True
    assert final_state["execution_attempts"][-1]["audit"] == str(audit_path)


def test_verified_auto_repair_retries_only_repairable_applications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    payload = workspace["payload"]
    assert isinstance(payload, dict)
    payload["auto_repair"] = {
        "enabled": True,
        "max_cycles": 1,
        "agent_binary": "codex",
        "agent_timeout_seconds": 30,
        "verification_timeout_seconds": 30,
        "combobox_no_progress_seconds": 10,
        "retry_after_verified_repair": True,
    }
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    monkeypatch.setenv("JOB_AGENT_CLI", str(workspace["cli"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(payload))
    config = DailyConfig.load(config_path, root=tmp_path)

    run_dir = config.output_root / "2026-07-27" / "100000"
    applications_dir = run_dir / "applications"
    repairable_dir = applications_dir / "001-repairable"
    protected_dir = applications_dir / "002-protected"
    repairable_dir.mkdir(parents=True)
    protected_dir.mkdir()
    (repairable_dir / "autofill-runtime.js").write_text(
        "const CFG = "
        + json.dumps(
            {
                "profile": {
                    "answers": {"Country": "United States"},
                    "screening_answer_rules": [],
                }
            }
        )
        + ";\n"
    )
    batch_summary = applications_dir / "batch-summary.json"
    batch_summary.write_text(
        json.dumps(
            [
                {
                    "company": "Repairable",
                    "title": "Engineer",
                    "package_dir": str(repairable_dir),
                },
                {
                    "company": "Protected",
                    "title": "Engineer",
                    "package_dir": str(protected_dir),
                },
            ]
        )
    )
    manifest_path = run_dir / "pipeline-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "counts": {"imported": 2, "shortlisted": 2, "prepared": 2},
                "artifacts": {"batch_summary": str(batch_summary)},
            }
        )
    )
    state = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "phase": "prepared",
        "created_at": "2026-07-27T10:00:00+00:00",
        "updated_at": "2026-07-27T10:00:00+00:00",
        "config_path": str(config.config_path),
        "config_sha256": hashlib.sha256(config.config_path.read_bytes()).hexdigest(),
        "input_sha256": fingerprint_inputs(config),
        "settings": config.snapshot(),
        "artifacts": {"manifest": str(manifest_path)},
        "execution_attempts": [],
        "history": [{"at": "2026-07-27T10:00:00+00:00", "phase": "prepared"}],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))

    executed_batches: list[list[dict[str, object]]] = []

    def fake_run_command(command: list[str], _config: DailyConfig) -> int:
        summary = Path(command[3])
        batch = json.loads(summary.read_text())
        executed_batches.append(batch)
        audit_path = Path(command[command.index("--audit-out") + 1])
        if len(executed_batches) == 1:
            audit = {
                "counts": {
                    "total": 2,
                    "submitted": 0,
                    "completed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "submission_blocked_by_anti_spam": 1,
                },
                "applications": [
                    {
                        "company": "Repairable",
                        "title": "Engineer",
                        "status": "autofill_completed_blocked",
                        "script_path": str(repairable_dir / "autofill-runtime.js"),
                        "review_items": [
                            {
                                "label": "Country",
                                "reason": (
                                    "combobox adapter mapping is unavailable"
                                ),
                                "sensitive": False,
                                "blocking": True,
                            }
                        ],
                    },
                    {
                        "company": "Protected",
                        "title": "Engineer",
                        "status": "submission_blocked_by_anti_spam",
                        "script_path": str(protected_dir / "autofill-runtime.js"),
                    },
                ],
            }
        else:
            assert "--retry-prior-terminal-outcome" in command
            audit = {
                "counts": {
                    "total": 1,
                    "submitted": 1,
                    "completed": 0,
                    "failed": 0,
                    "skipped": 0,
                },
                "applications": [
                    {
                        "company": "Repairable",
                        "title": "Engineer",
                        "status": "submitted",
                    }
                ],
            }
        audit_path.write_text(json.dumps(audit))
        return 0

    def fake_repair(*args, **kwargs):
        result_path = run_dir / "repair" / "repair-result-cycle-01.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text("{}")
        return {
            "status": "promoted",
            "reason": "all_verification_passed",
            "changed_files": ["src/job_agent/python_runtime.py"],
            "result_path": str(result_path),
        }

    monkeypatch.setattr(
        daily_sop_module,
        "run_preflight",
        lambda _config: daily_sop_module.PreflightReport(()),
    )
    monkeypatch.setattr(daily_sop_module, "_run_command", fake_run_command)
    monkeypatch.setattr(daily_sop_module, "run_repair_cycle", fake_repair)
    monkeypatch.setattr(
        daily_sop_module,
        "_rebuild_verified_repair_retry_packages",
        lambda _config, *, retry_summary, **_kwargs: retry_summary,
    )

    execute_daily_run(config, run_dir=run_dir)

    assert len(executed_batches) == 2
    assert [item["company"] for item in executed_batches[1]] == ["Repairable"]
    final_state = json.loads((run_dir / "run-state.json").read_text())
    assert final_state["phase"] == "executed"
    assert final_state["repair_cycles"][0]["status"] == "promoted"
    phases = [event["phase"] for event in final_state["history"]]
    assert "needs_repair" in phases
    assert "repairing" in phases
    assert "repair_verified" in phases


def test_create_run_dir_never_overwrites_existing_run(tmp_path: Path) -> None:
    output_root = tmp_path / "output" / "daily"
    now = datetime(2026, 7, 25, 9, 30, 0, tzinfo=timezone.utc)

    first = create_run_dir(output_root, now=now)
    second = create_run_dir(output_root, now=now)

    assert first.name == "093000"
    assert second.name == "093000-02"


def test_prepare_empty_batch_waits_for_external_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.config_path.write_text("{}")

    def fake_run_command(command: list[str], _config: DailyConfig) -> int:
        run_dir = Path(command[command.index("--out-dir") + 1])
        (run_dir / "pipeline-manifest.json").write_text(
            json.dumps(
                {
                    "counts": {
                        "imported": 10,
                        "shortlisted": 0,
                        "prepared": 0,
                    },
                    "artifacts": {},
                }
            )
        )
        return 0

    monkeypatch.setattr(
        daily_sop_module,
        "run_preflight",
        lambda _config: daily_sop_module.PreflightReport(()),
    )
    monkeypatch.setattr(daily_sop_module, "_run_command", fake_run_command)
    before = datetime.now().astimezone()

    run_dir = prepare_daily_run(config)

    state = json.loads((run_dir / "run-state.json").read_text())
    latest = json.loads((config.output_root / "latest.json").read_text())
    next_wake_at = datetime.fromisoformat(state["next_wake_at"])
    assert state["phase"] == "waiting_for_candidates"
    assert next_wake_at > before
    assert latest["phase"] == "waiting_for_candidates"
    assert latest["next_wake_at"] == state["next_wake_at"]
    assert state["daily_target"]["raw_imported"] == 10
    assert state["daily_target"]["rate_target"] == 8
    assert state["daily_target"]["target"] == 8
    report = (run_dir / "RUN_SUMMARY.md").read_text()
    assert "external scheduler" in report
    assert "do not sleep inside the Goal" in report


def test_empty_batch_wake_uses_configured_interval_after_repeated_polls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = replace(_config(tmp_path, monkeypatch), empty_wake_minutes=5)
    heartbeat = {"empty_wake_count": 6, "no_progress_count": 0}
    written = {}
    monkeypatch.setattr(daily_sop_module, "_read_heartbeat_state", lambda _config: heartbeat)
    monkeypatch.setattr(
        daily_sop_module,
        "_write_heartbeat_state",
        lambda _config, state: written.update(state),
    )
    monkeypatch.setattr(daily_sop_module, "_next_wake_at", lambda minutes: str(minutes))
    state = {"phase": "waiting_for_candidates"}
    transition = {}

    daily_sop_module._apply_heartbeat_wake_logic(
        config,
        prepared_count=0,
        run_dir=tmp_path / "run",
        state=state,
        transition_details=transition,
    )

    assert transition["wake_after_minutes"] == 5
    assert state["next_wake_at"] == "5"
    assert written["empty_wake_count"] == 7


def test_managed_temp_workspace_is_isolated_and_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    original = {key: os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP")}

    with managed_temp_workspace(config, "execute") as workspace:
        assert workspace.parent == config.output_root / ".tmp"
        assert all(os.environ[key] == str(workspace) for key in original)
        (workspace / "playwright-artifacts-fixture").mkdir()

    assert not workspace.exists()
    for key, previous in original.items():
        if previous is None:
            assert key not in os.environ
        else:
            assert os.environ[key] == previous


def test_managed_temp_workspace_cleans_after_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="fixture failure"):
        with managed_temp_workspace(config, "check") as workspace:
            (workspace / "playwright_chromiumdev_profile-fixture").mkdir()
            raise RuntimeError("fixture failure")

    assert not workspace.exists()


def test_cleanup_removes_only_inactive_owned_temp_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    temp_root = config.output_root / ".tmp"
    temp_root.mkdir(parents=True)

    def write_managed(name: str, *, owner_pid: int, created_at: float) -> Path:
        directory = temp_root / name
        directory.mkdir()
        (directory / ".job-agent-temp.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_root": str(config.root.resolve()),
                    "output_root": str(config.output_root.resolve()),
                    "owner_pid": owner_pid,
                    "created_at_epoch": created_at,
                }
            )
        )
        return directory

    stale = write_managed(
        "job-agent-execute-stale",
        owner_pid=0,
        created_at=0,
    )
    active = write_managed(
        "job-agent-execute-active",
        owner_pid=os.getpid(),
        created_at=0,
    )
    recent = write_managed(
        "job-agent-execute-recent",
        owner_pid=0,
        created_at=7_100,
    )
    unmanaged = temp_root / "playwright-artifacts-unowned"
    unmanaged.mkdir()

    report = cleanup_managed_temp(
        config,
        min_age_seconds=3_600,
        now_epoch=7_200,
    )

    assert report.ok is True
    assert report.removed_count == 1
    assert report.skipped_active_count == 1
    assert report.skipped_recent_count == 1
    assert report.skipped_unmanaged_count == 1
    assert not stale.exists()
    assert active.exists()
    assert recent.exists()
    assert unmanaged.exists()


def test_cleanup_command_removes_inactive_managed_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(workspace["payload"]))
    config = DailyConfig.load(config_path, root=PROJECT_ROOT)
    stale = config.output_root / ".tmp" / "job-agent-check-stale"
    stale.mkdir(parents=True)
    (stale / ".job-agent-temp.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_root": str(config.root.resolve()),
                "output_root": str(config.output_root.resolve()),
                "owner_pid": 0,
                "created_at_epoch": 0,
            }
        )
    )

    exit_code = main(
        [
            "--config",
            str(config_path),
            "cleanup",
            "--older-than-hours",
            "0",
        ]
    )

    assert exit_code == 0
    assert not stale.exists()


def test_ledger_command_exports_tracking_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(workspace["payload"]))
    connection = connect(workspace["database"])
    job = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="test",
        apply_url="https://jobs.example.com/acme-agent",
    )
    application_id = create_application(connection, create_job(connection, job), job)
    assert update_application_execution_status(connection, application_id, "submitted")
    connection.close()

    exit_code = main(["--config", str(config_path), "ledger"])

    ledger_path = Path(workspace["payload"]["output_root"]) / "APPLICATION_LEDGER.csv"
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert exit_code == 0
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["role"] == "Agent Engineer"
    assert rows[0]["submitted_at_utc"]


def test_execute_command_continues_by_default_and_one_batch_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.config_path.write_text("{}")
    run_dir = config.output_root / "2026-07-28" / "100000"
    run_dir.mkdir(parents=True)
    execution_calls: list[dict[str, object]] = []
    target_calls: list[DailyConfig] = []

    monkeypatch.setattr(
        DailyConfig,
        "load",
        classmethod(lambda cls, path, **kwargs: config),
    )
    monkeypatch.setattr(daily_sop_module, "load_env", lambda _path: {})
    monkeypatch.setattr(
        daily_sop_module,
        "refresh_application_ledger",
        lambda _config: (config.output_root / "APPLICATION_LEDGER.csv", 0),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "resolve_run_dir",
        lambda _config, _run_dir: run_dir,
    )

    def fake_execute(_config, **kwargs):
        execution_calls.append(dict(kwargs))
        return run_dir

    monkeypatch.setattr(daily_sop_module, "execute_daily_run", fake_execute)
    monkeypatch.setattr(
        daily_sop_module,
        "run_until_daily_target",
        lambda current: target_calls.append(current) or run_dir,
    )
    monkeypatch.setattr(
        daily_sop_module,
        "_read_optional_json",
        lambda _path: {"progress": {"complete": True}},
    )
    monkeypatch.setattr(
        daily_sop_module,
        "_update_daily_target_state",
        lambda _config, _run_dir: {},
    )
    monkeypatch.setattr(
        daily_sop_module,
        "write_run_report",
        lambda _run_dir: _run_dir / "RUN_SUMMARY.md",
    )

    default_exit = main(
        [
            "--config",
            str(config.config_path),
            "execute",
            "--run-dir",
            str(run_dir),
        ]
    )
    one_batch_exit = main(
        [
            "--config",
            str(config.config_path),
            "execute",
            "--run-dir",
            str(run_dir),
            "--one-batch",
        ]
    )

    assert default_exit == 0
    assert one_batch_exit == 0
    assert len(execution_calls) == 2
    assert target_calls == [config]


def test_repair_command_routes_refresh_request_only_to_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.config_path.write_text("{}")
    run_dir = config.output_root / "2026-07-28" / "100000"
    run_dir.mkdir(parents=True)
    repair_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        DailyConfig,
        "load",
        classmethod(lambda cls, path, **kwargs: config),
    )
    monkeypatch.setattr(daily_sop_module, "load_env", lambda _path: {})
    monkeypatch.setattr(
        daily_sop_module,
        "refresh_application_ledger",
        lambda _config: (config.output_root / "APPLICATION_LEDGER.csv", 0),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "resolve_run_dir",
        lambda _config, _run_dir: run_dir,
    )
    monkeypatch.setattr(
        daily_sop_module,
        "repair_daily_run",
        lambda _config, **kwargs: repair_calls.append(dict(kwargs)) or run_dir,
    )
    monkeypatch.setattr(
        daily_sop_module,
        "_update_daily_target_state",
        lambda _config, _run_dir: {},
    )
    monkeypatch.setattr(
        daily_sop_module,
        "write_run_report",
        lambda _run_dir: _run_dir / "RUN_SUMMARY.md",
    )

    exit_code = main(
        [
            "--config",
            str(config.config_path),
            "repair",
            "--run-dir",
            str(run_dir),
            "--refresh-request-only",
        ]
    )

    assert exit_code == 0
    assert repair_calls == [
        {
            "run_dir": run_dir,
            "retry_verified": False,
            "refresh_request_only": True,
            "recover_interrupted": False,
        }
    ]


def test_repair_command_routes_interrupted_recovery_to_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, monkeypatch)
    config.config_path.write_text("{}")
    run_dir = config.output_root / "2026-07-28" / "100000"
    run_dir.mkdir(parents=True)
    repair_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        DailyConfig,
        "load",
        classmethod(lambda cls, path, **kwargs: config),
    )
    monkeypatch.setattr(daily_sop_module, "load_env", lambda _path: {})
    monkeypatch.setattr(
        daily_sop_module,
        "refresh_application_ledger",
        lambda _config: (config.output_root / "APPLICATION_LEDGER.csv", 0),
    )
    monkeypatch.setattr(
        daily_sop_module,
        "resolve_run_dir",
        lambda _config, _run_dir: run_dir,
    )
    monkeypatch.setattr(
        daily_sop_module,
        "repair_daily_run",
        lambda _config, **kwargs: repair_calls.append(dict(kwargs)) or run_dir,
    )
    monkeypatch.setattr(
        daily_sop_module,
        "_update_daily_target_state",
        lambda _config, _run_dir: {},
    )
    monkeypatch.setattr(
        daily_sop_module,
        "write_run_report",
        lambda _run_dir: _run_dir / "RUN_SUMMARY.md",
    )

    exit_code = main(
        [
            "--config",
            str(config.config_path),
            "repair",
            "--run-dir",
            str(run_dir),
            "--recover-interrupted",
        ]
    )

    assert exit_code == 0
    assert repair_calls == [
        {
            "run_dir": run_dir,
            "retry_verified": False,
            "refresh_request_only": False,
            "recover_interrupted": True,
        }
    ]


def test_execution_input_guard_detects_profile_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _write_workspace(tmp_path)
    monkeypatch.setenv("TEST_RESUME_DIR", str(workspace["resumes"]))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JOB_AGENT_CLI", str(workspace["cli"]))
    config_path = tmp_path / "daily.local.json"
    config_path.write_text(json.dumps(workspace["payload"]))
    config = DailyConfig.load(config_path, root=tmp_path)
    state = {
        "config_path": str(config_path),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "input_sha256": fingerprint_inputs(config),
    }

    validate_run_inputs(config, state)
    config.profile.write_text(
        json.dumps(
            {
                "name": "Changed Candidate",
                "email": "changed@example.com",
                "phone": "+1 555 0101",
            }
        )
    )

    with pytest.raises(SopError, match="profile"):
        validate_run_inputs(config, state)


def test_report_records_terminal_states_and_next_action(tmp_path: Path) -> None:
    run_dir = tmp_path / "output" / "daily" / "2026-07-25" / "093000"
    run_dir.mkdir(parents=True)
    audit_path = run_dir / "execution-audit.json"
    state = {
        "run_id": "093000",
        "phase": "executed_with_blockers",
        "created_at": "2026-07-25T09:30:00+00:00",
        "updated_at": "2026-07-25T09:35:00+00:00",
        "config_path": str(tmp_path / "ops" / "daily.local.json"),
        "settings": {
            "submit_complete": True,
            "evaluation": {
                "imported_cohort_target": 10,
                "min_confirmed_submission_rate": 0.8,
                "min_terminal_audit_coverage": 1.0,
            },
        },
        "artifacts": {"execution_audit": str(audit_path)},
        "history": [
            {
                "at": "2026-07-25T09:30:03+00:00",
                "phase": "prepared",
                "stage": "prepare",
                "duration_seconds": 3,
            },
            {
                "at": "2026-07-25T09:31:00+00:00",
                "phase": "executing",
                "waiting_seconds": 57,
            },
            {
                "at": "2026-07-25T09:32:30+00:00",
                "phase": "executed_with_blockers",
                "stage": "execute",
                "duration_seconds": 90,
            },
        ],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))
    (run_dir / "pipeline-manifest.json").write_text(
        json.dumps(
            {
                "counts": {
                    "imported": 10,
                    "shortlisted": 2,
                    "prepared": 1,
                }
            }
        )
    )
    audit_path.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "submitted": 0,
                    "completed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "email_verification_required": 1,
                },
                "progress": {"complete": True},
                "applications": [
                    {
                        "company": "Acme",
                        "title": "Agent Engineer",
                        "status": "email_verification_required",
                    }
                ],
            }
        )
    )

    report_path = write_run_report(run_dir)
    report = report_path.read_text()

    assert "executed_with_blockers" in report
    assert "email_verification_required" in report
    assert "Gmail authorization" in report
    assert "## Recovery Plans" in report
    assert "email_verification_resume" in report
    assert "Resume only the matching application" in report
    assert "Acme" in report
    assert "APPLICATION_LEDGER.csv" in report
    assert "## Efficiency" in report
    assert "| 3s | 1m 30s | 1m 33s | 57s | 0.0% (0/1) |" in report
    assert "## Agent Evaluation" in report
    assert "Evaluator: `job_application_round`" in report
    assert "overall status: `needs_attention`" in report
    assert "### Evaluation Recommendations" in report
    assert "Confirmed / raw imported" in report
    assert "Confirmed / final eligible" in report
    metrics = json.loads((run_dir / "evaluation-metrics.json").read_text())
    assert metrics["counts"]["final_eligible"] == 1
    assert metrics["counts"]["confirmed_for_raw_import_rate"] == 0
    assert metrics["rates"]["confirmed_submission_rate_final_eligible"] == 0.0
    assert metrics["rates"]["raw_import_to_confirmed_rate"] == 0.0
    assert metrics["assessment"]["terminal_audit_coverage"]["status"] == "met"
    assert (
        metrics["assessment"]["raw_import_to_confirmed_rate"]["status"]
        == "not_met"
    )
    assert (
        metrics["assessment"][
            "confirmed_submission_rate_final_eligible"
        ]["status"]
        == "monitor"
    )
    assert metrics["agent_core"]["evaluator"] == "job_application_round"
    assert metrics["agent_core"]["round_id"] == "093000"
    assert metrics["agent_core"]["status"] == "needs_attention"
    assert metrics["agent_core"]["recommendations"]


def test_report_rejects_complete_audit_left_in_prepared_phase(tmp_path: Path) -> None:
    run_dir = tmp_path / "output" / "daily" / "2026-07-25" / "093000"
    run_dir.mkdir(parents=True)
    audit_path = run_dir / "execution-audit.json"
    (run_dir / "run-state.json").write_text(
        json.dumps({"run_id": "093000", "phase": "prepared", "artifacts": {"execution_audit": str(audit_path)}})
    )
    audit_path.write_text(json.dumps({"progress": {"complete": True}, "applications": []}))

    with pytest.raises(SopError, match="bypassed Daily SOP"):
        write_run_report(run_dir)


def test_report_combines_scoped_retry_with_prior_terminal_audit(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "output" / "daily" / "2026-07-29" / "124250"
    run_dir.mkdir(parents=True)
    original_audit = run_dir / "execution-audit.json"
    retry_audit = run_dir / "execution-audit-retry-01.json"
    state = {
        "run_id": "124250",
        "phase": "executed",
        "created_at": "2026-07-29T12:42:50-04:00",
        "updated_at": "2026-07-29T13:40:15-04:00",
        "config_path": str(tmp_path / "ops" / "daily.local.json"),
        "settings": {
            "submit_complete": True,
            "evaluation": {
                "imported_cohort_target": 3,
                "min_confirmed_submission_rate": 0.8,
                "min_terminal_audit_coverage": 1.0,
            },
        },
        "artifacts": {"execution_audit": str(retry_audit)},
        "execution_attempts": [
            {"attempt": 1, "audit": str(original_audit)},
            {"attempt": 2, "audit": str(retry_audit)},
        ],
    }
    (run_dir / "run-state.json").write_text(json.dumps(state))
    (run_dir / "pipeline-manifest.json").write_text(
        json.dumps(
            {
                "counts": {
                    "imported": 3,
                    "shortlisted": 3,
                    "prepared": 3,
                }
            }
        )
    )
    original_audit.write_text(
        json.dumps(
            {
                "progress": {
                    "planned": 3,
                    "terminal": 3,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "application_id": "1",
                        "company": "MongoDB",
                        "title": "Engineer",
                        "status": "submitted",
                    },
                    {
                        "application_id": "2",
                        "company": "Sony",
                        "title": "Engineer II",
                        "status": "autofill_completed_blocked",
                    },
                    {
                        "application_id": "3",
                        "company": "Cerebras",
                        "title": "Systems Engineer",
                        "status": "submission_blocked_by_anti_spam",
                    },
                ],
            }
        )
    )
    retry_audit.write_text(
        json.dumps(
            {
                "progress": {
                    "planned": 1,
                    "terminal": 1,
                    "remaining": 0,
                    "complete": True,
                },
                "applications": [
                    {
                        "application_id": "2",
                        "company": "Sony",
                        "title": "Engineer II",
                        "status": "submitted",
                    }
                ],
            }
        )
    )

    report = write_run_report(run_dir).read_text()

    assert "MongoDB" in report
    assert "Sony" in report
    assert "Cerebras" in report
    assert "| 3 | 2 | 0 | 0 | 0 |" in report
    metrics = json.loads((run_dir / "evaluation-metrics.json").read_text())
    assert metrics["counts"]["prepared"] == 3
    assert metrics["counts"]["terminal_records"] == 3
    assert metrics["counts"]["submitted"] == 2
    assert metrics["assessment"]["terminal_audit_coverage"]["status"] == "met"


def test_report_indexes_unified_application_trajectory_and_continuity(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "output" / "daily" / "2026-07-28" / "120000"
    package_dir = run_dir / "applications" / "one"
    package_dir.mkdir(parents=True)
    audit_path = run_dir / "execution-audit.json"
    (run_dir / "run-state.json").write_text(
        json.dumps(
            {
                "run_id": "120000",
                "phase": "executed",
                "settings": {"submit_complete": False},
                "artifacts": {"execution_audit": str(audit_path)},
            }
        )
    )
    (run_dir / "pipeline-manifest.json").write_text(
        json.dumps(
            {
                "counts": {
                    "imported": 1,
                    "shortlisted": 1,
                    "prepared": 1,
                },
                "agent_runtime": {
                    "closed_loop": True,
                    "pipeline": {"status": "completed", "rounds": [{}]},
                },
            }
        )
    )
    trajectory_path = package_dir / "agent-trajectory.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "stages": {
                    "preparation": [
                        {
                            "observations": [
                                {"observation_id": "handoff-observation"}
                            ]
                        }
                    ],
                    "execution": {
                        "rounds": [
                            {
                                "input_observation": {
                                    "observation_id": "handoff-observation"
                                }
                            }
                        ]
                    },
                },
            }
        )
    )
    audit_path.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "submitted": 0,
                    "completed": 1,
                    "failed": 0,
                    "skipped": 0,
                },
                "progress": {"complete": True},
                "applications": [
                    {
                        "application_id": "1",
                        "company": "Acme",
                        "title": "Engineer",
                        "status": "autofill_completed_blocked",
                        "package_dir": str(package_dir),
                    }
                ],
            }
        )
    )

    write_run_report(run_dir)

    runtime = json.loads(
        (run_dir / "agent-runtime-trace.json").read_text()
    )
    assert runtime["closed_loop"] is True
    assert runtime["continuity"] == {
        "continuous": 1,
        "not_executed": 0,
        "disconnected": 0,
        "missing": 0,
    }
    assert runtime["applications"][0]["handoff"]["status"] == "continuous"
    trajectory = json.loads(trajectory_path.read_text())
    assert trajectory["stages"]["evaluation"]["agent_core"][
        "evaluator"
    ] == "job_application_round"
