from pathlib import Path
from subprocess import CompletedProcess
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from job_agent.db import (
    connect,
    create_application,
    create_job,
    init_db,
    update_application_execution_status,
)
from job_agent.execution import (
    _is_anti_spam_rejection,
    _sanitize_runtime_action_output,
    execute_application_batch,
)
from job_agent.models import Job
from job_agent.python_runtime import RuntimeActionDenied
from job_agent.runtime_filler import render_runtime_autofill_script


def test_is_anti_spam_rejection_application_limit():
    assert _is_anti_spam_rejection("You have reached your application limit.") is True
    assert _is_anti_spam_rejection("This company accepts only one application.") is True
    assert _is_anti_spam_rejection("You have already applied to this role.") is True


def test_execute_application_batch_records_success_without_sensitive_stdout(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('candidate@example.com'); console.log('Submit gate: STOPPED before final Submit')")

    def fake_run(command, **kwargs):
        assert command == ["node", str(script)]
        assert kwargs["timeout"] == 300
        return CompletedProcess(
            command,
            0,
            stdout="candidate@example.com\nSubmit gate: STOPPED before final Submit",
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    recovery_plan = records[0].pop("recovery_plan")
    assert records == [
        {
            "company": "Acme",
            "title": "Agent Engineer",
            "script_path": str(script),
            "status": "autofill_completed_blocked",
            "exit_code": 0,
            "submit_gate": "automatic_submission_enabled",
            "error": None,
            "filled_count": None,
            "review_count": None,
        }
    ]
    assert recovery_plan["strategy"] == "bounded_field_recovery"
    assert "candidate@example.com" not in str(records)


def test_execute_application_batch_rejects_success_without_submit_gate_marker(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('filled fields')")

    def fake_run(command, **kwargs):
        return CompletedProcess(command, 0, stdout="filled fields", stderr="")

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_failed"
    assert records[0]["exit_code"] == 0
    assert records[0]["error"] == "terminal_status_not_confirmed"
    assert records[0]["submit_gate"] == "automatic_submission_enabled"


def test_execute_application_batch_records_submitted_marker(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('Autofill stats: filled=4 review=0'); console.log('Submission confirmed: matched thank you')")

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout="Autofill stats: filled=4 review=0\nSubmission confirmed: matched thank you",
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "submitted"
    assert records[0]["submit_gate"] == "submitted"
    assert records[0]["filled_count"] == 4
    assert records[0]["review_count"] == 0
    assert records[0]["cleanup_deleted_files"] == []
    assert records[0]["cleanup_errors"] == []


def test_execute_application_batch_rejects_package_local_generated_resume_before_runtime(
    tmp_path,
):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    review = package_dir / "review.md"
    pdf = package_dir / "tailored-resume.pdf"
    docx = package_dir / "tailored-resume.docx"
    markdown = package_dir / "tailored-resume.md"
    script.write_text("console.log('Submission confirmed: matched thank you')")
    review.write_text("review stays")
    pdf.write_text("generated pdf")
    docx.write_text("generated docx")
    markdown.write_text("generated markdown")

    def fake_run(command, **kwargs):
        pytest.fail("runtime must not execute with a package-local generated PDF resume")

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "review_path": str(review),
                "tailored_resume_path": str(markdown),
                "upload_resume_path": str(pdf),
                "upload_resume_docx_path": str(docx),
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "skipped_invalid_resume"
    assert records[0]["submit_gate"] == "invalid_resume_upload"
    assert "must be an original external path" in records[0]["error"]
    assert pdf.exists()
    assert docx.exists()
    assert markdown.exists()
    assert script.exists()
    assert review.exists()


def test_execute_application_batch_rejects_package_local_generated_resume_without_package_dir(
    tmp_path,
):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    pdf = package_dir / "tailored-resume.pdf"
    docx = package_dir / "tailored-resume.docx"
    markdown = package_dir / "tailored-resume.md"
    script.write_text("console.log('Submit clicked but confirmation not detected: clicked Apply')")
    pdf.write_text("generated pdf")
    docx.write_text("generated docx")
    markdown.write_text("generated markdown")

    def fake_run(command, **kwargs):
        pytest.fail("runtime must not execute with a package-local generated PDF resume")

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "tailored_resume_path": str(markdown),
                "upload_resume_path": str(pdf),
                "upload_resume_docx_path": str(docx),
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "skipped_invalid_resume"
    assert records[0]["submit_gate"] == "invalid_resume_upload"
    assert "must be an original external path" in records[0]["error"]
    assert "cleanup_deleted_files" not in records[0]
    assert pdf.exists()
    assert docx.exists()
    assert markdown.exists()


def test_execute_application_batch_uses_summary_required_source_dir_before_runtime(
    tmp_path,
):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    outside_pdf = tmp_path / "outside.pdf"
    outside_pdf.write_bytes(b"%PDF-1.4\noutside")
    script.write_text(f'const CFG = {{"resumeFile": "{outside_pdf}"}};\n')

    def fake_run(command, **kwargs):
        pytest.fail("runtime must not execute with a resume outside the summary source dir")

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "package_dir": str(package_dir),
                "runtime_script_path": str(script),
                "upload_resume_path": str(outside_pdf),
                "required_resume_source_dir": str(source_dir),
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "skipped_invalid_resume"
    assert records[0]["submit_gate"] == "invalid_resume_upload"
    assert "must come from required resume source dir" in records[0]["error"]


def test_execute_application_batch_rejects_changed_pdf_hash_before_runtime(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\noriginal")
    prepared_sha = hashlib.sha256(b"%PDF-1.4\noriginal").hexdigest()
    resume.write_bytes(b"%PDF-1.4\ngenerated replacement")
    script.write_text(f'const CFG = {{"resumeFile": "{resume}"}};\n')

    def fake_run(command, **kwargs):
        pytest.fail("runtime must not execute when the prepared PDF hash changed")

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "package_dir": str(package_dir),
                "runtime_script_path": str(script),
                "upload_resume_path": str(resume),
                "upload_resume_pdf_sha256": prepared_sha,
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "skipped_invalid_resume"
    assert records[0]["submit_gate"] == "invalid_resume_upload"
    assert "hash does not match prepared summary" in records[0]["error"]


def test_execute_application_batch_does_not_delete_resume_outside_package(tmp_path):
    package_dir = tmp_path / "package"
    source_dir = tmp_path / "source"
    package_dir.mkdir()
    source_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    source_pdf = source_dir / "tailored-resume.pdf"
    docx = package_dir / "tailored-resume.docx"
    markdown = package_dir / "tailored-resume.md"
    script.write_text("console.log('Submission confirmed: matched thank you')")
    source_pdf.write_text("source resume")
    docx.write_text("generated docx")
    markdown.write_text("generated markdown")

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout="Autofill stats: filled=4 review=0\nSubmission confirmed: matched thank you",
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "tailored_resume_path": str(markdown),
                "upload_resume_path": str(source_pdf),
                "upload_resume_docx_path": str(docx),
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "submitted"
    assert sorted(Path(path).name for path in records[0]["cleanup_deleted_files"]) == [
        "tailored-resume.docx",
        "tailored-resume.md",
    ]
    assert source_pdf.exists()
    assert not docx.exists()
    assert not markdown.exists()


def test_execute_application_batch_records_submit_clicked_unconfirmed(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        "console.log('Autofill stats: filled=4 review=0'); "
        "console.log('Submit clicked but confirmation not detected: clicked Apply')"
    )

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=4 review=0\n"
                "Submit clicked but confirmation not detected: clicked Apply"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "submit_clicked_unconfirmed"
    assert records[0]["submit_gate"] == "submit_clicked_unconfirmed"
    assert records[0]["error"] == "submission_confirmation_not_detected"
    assert records[0]["filled_count"] == 4
    assert records[0]["review_count"] == 0
    assert (
        records[0]["recovery_plan"]["strategy"]
        == "confirmation_reconciliation"
    )
    assert records[0]["recovery_plan"]["retry_allowed"] is False


def test_execute_application_batch_records_email_verification_required(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        "console.log('Autofill stats: filled=4 review=0'); "
        "console.log('Email verification required: matched security code')"
    )

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=4 review=0\n"
                "Email verification required: matched security code"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "email_verification_required"
    assert records[0]["submit_gate"] == "email_verification_required"
    assert records[0]["error"] == "email_verification_required"
    assert records[0]["filled_count"] == 4
    assert records[0]["review_count"] == 0
    assert (
        records[0]["recovery_plan"]["strategy"]
        == "email_verification_resume"
    )


def test_execute_application_batch_records_submission_processing_error(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        "console.log('Autofill stats: filled=4 review=0'); "
        "console.log('Submission processing error: matched processing error')"
    )

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=4 review=0\n"
                "Submission processing error: matched processing error"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "submission_processing_error"
    assert records[0]["submit_gate"] == "submission_processing_error"
    assert records[0]["error"] == "submission_processing_error"
    assert records[0]["filled_count"] == 4
    assert records[0]["review_count"] == 0


def test_execute_application_batch_distinguishes_anti_spam_rejection(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('Submission processing error: flagged as possible spam')")

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=12 review=0\n"
                "Submission processing error: matched 'flagged as possible spam'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Baseten", "title": "AI Solutions Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "submission_blocked_by_anti_spam"
    assert records[0]["submit_gate"] == "submission_blocked_by_anti_spam"
    assert records[0]["error"] == "submission_blocked_by_anti_spam"
    assert records[0]["filled_count"] == 12
    assert (
        records[0]["recovery_plan"]["strategy"]
        == "tenant_cooldown_then_scoped_resume"
    )


def test_execute_application_batch_skips_same_tenant_after_anti_spam(tmp_path):
    blocked = tmp_path / "blocked-runtime.js"
    same_tenant = tmp_path / "same-tenant-runtime.js"
    other_tenant = tmp_path / "other-tenant-runtime.js"
    for script in (blocked, same_tenant, other_tenant):
        script.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        if command[1] == str(blocked):
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "Autofill stats: filled=12 review=0\n"
                    "Submission processing error: matched "
                    "'flagged as possible spam'"
                ),
                stderr="",
            )
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=18 review=0\n"
                "Submission confirmed: matched 'thank you for applying'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "BlockedCo",
                "title": "First Role",
                "apply_url": "https://jobs.ashbyhq.com/blockedco/first/application",
                "runtime_script_path": str(blocked),
            },
            {
                "company": "BlockedCo",
                "title": "Second Role",
                "apply_url": "https://jobs.ashbyhq.com/blockedco/second/application",
                "runtime_script_path": str(same_tenant),
            },
            {
                "company": "NextCo",
                "title": "Third Role",
                "apply_url": "https://jobs.ashbyhq.com/nextco/third/application",
                "runtime_script_path": str(other_tenant),
            },
        ],
        runner=fake_run,
    )

    assert calls == [str(blocked), str(other_tenant)]
    assert [record["status"] for record in records] == [
        "submission_blocked_by_anti_spam",
        "skipped_policy_denied",
        "submitted",
    ]
    assert records[1]["error"] == "anti_spam_cooldown_active"
    assert records[1]["submit_gate"] == (
        "policy_denied:anti_spam_cooldown_active"
    )


def test_execute_application_batch_restores_anti_spam_cooldown_from_db(
    tmp_path,
):
    db_path = tmp_path / "tracking.db"
    conn = connect(db_path)
    init_db(conn)
    blocked_job = Job(
        title="Blocked Role",
        company="BlockedCo",
        raw_jd="Role",
        apply_url="https://jobs.ashbyhq.com/blockedco/blocked/application",
    )
    application_id = create_application(
        conn,
        create_job(conn, blocked_job),
        blocked_job,
    )
    assert update_application_execution_status(
        conn,
        application_id,
        "submission_blocked_by_anti_spam",
    )
    conn.close()

    same_tenant = tmp_path / "same-tenant-runtime.js"
    other_tenant = tmp_path / "other-tenant-runtime.js"
    same_tenant.write_text("console.log('runtime')")
    other_tenant.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=18 review=0\n"
                "Submission confirmed: matched 'thank you for applying'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "BlockedCo",
                "title": "Later Role",
                "apply_url": (
                    "https://jobs.ashbyhq.com/blockedco/later/application"
                ),
                "runtime_script_path": str(same_tenant),
            },
            {
                "company": "NextCo",
                "title": "Other Role",
                "apply_url": (
                    "https://jobs.ashbyhq.com/nextco/other/application"
                ),
                "runtime_script_path": str(other_tenant),
            },
        ],
        runner=fake_run,
        database_path=db_path,
    )

    assert calls == [str(other_tenant)]
    assert [record["status"] for record in records] == [
        "skipped_policy_denied",
        "submitted",
    ]
    assert records[0]["error"] == "anti_spam_cooldown_active"


def test_execute_application_batch_opens_ordinary_failure_circuit_in_batch(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    acme_first = tmp_path / "acme-first.js"
    other_first = tmp_path / "other-first.js"
    acme_second = tmp_path / "acme-second.js"
    acme_third = tmp_path / "acme-third.js"
    other_second = tmp_path / "other-second.js"
    for script in (
        acme_first,
        other_first,
        acme_second,
        acme_third,
        other_second,
    ):
        script.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        if command[1] in {str(acme_first), str(acme_second)}:
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "Autofill stats: filled=12 review=1\n"
                    "Submit gate: STOPPED before final Submit"
                ),
                stderr="",
            )
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=18 review=0\n"
                "Submission confirmed: matched 'thank you for applying'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "First Role",
                "apply_url": "https://jobs.ashbyhq.com/acme/first/application",
                "runtime_script_path": str(acme_first),
            },
            {
                "company": "Other",
                "title": "First Role",
                "apply_url": "https://jobs.ashbyhq.com/other/first/application",
                "runtime_script_path": str(other_first),
            },
            {
                "company": "Acme",
                "title": "Second Role",
                "apply_url": "https://jobs.ashbyhq.com/acme/second/application",
                "runtime_script_path": str(acme_second),
            },
            {
                "company": "Acme",
                "title": "Third Role",
                "apply_url": "https://jobs.ashbyhq.com/acme/third/application",
                "runtime_script_path": str(acme_third),
            },
            {
                "company": "Other",
                "title": "Second Role",
                "apply_url": "https://jobs.ashbyhq.com/other/second/application",
                "runtime_script_path": str(other_second),
            },
        ],
        runner=fake_run,
    )

    assert calls == [
        str(acme_first),
        str(other_first),
        str(acme_second),
        str(other_second),
    ]
    assert [record["status"] for record in records] == [
        "autofill_completed_blocked",
        "submitted",
        "autofill_completed_blocked",
        "skipped_policy_denied",
        "submitted",
    ]
    assert records[3]["error"] == "failure_circuit_breaker_active"
    assert records[3]["submit_gate"] == (
        "policy_denied:failure_circuit_breaker_active"
    )


def test_execute_application_batch_resets_ordinary_failure_sequence_on_success(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    scripts = [tmp_path / f"acme-{index}.js" for index in range(4)]
    for script in scripts:
        script.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        if command[1] in {str(scripts[0]), str(scripts[2])}:
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "Autofill stats: filled=12 review=1\n"
                    "Submit gate: STOPPED before final Submit"
                ),
                stderr="",
            )
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=18 review=0\n"
                "Submission confirmed: matched 'thank you for applying'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": f"Role {index}",
                "apply_url": (
                    f"https://jobs.ashbyhq.com/acme/{index}/application"
                ),
                "runtime_script_path": str(script),
            }
            for index, script in enumerate(scripts)
        ],
        runner=fake_run,
    )

    assert calls == [str(script) for script in scripts]
    assert [record["status"] for record in records] == [
        "autofill_completed_blocked",
        "submitted",
        "autofill_completed_blocked",
        "submitted",
    ]


def test_execute_application_batch_restores_ordinary_failure_sequence_from_db(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    db_path = tmp_path / "tracking.db"
    conn = connect(db_path)
    init_db(conn)
    prior_job = Job(
        title="Prior Role",
        company="Acme",
        raw_jd="Role",
        apply_url="https://jobs.ashbyhq.com/acme/prior/application",
    )
    application_id = create_application(
        conn,
        create_job(conn, prior_job),
        prior_job,
    )
    assert update_application_execution_status(
        conn,
        application_id,
        "autofill_completed_blocked",
    )
    conn.close()

    first = tmp_path / "first.js"
    second = tmp_path / "second.js"
    first.write_text("console.log('runtime')")
    second.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=12 review=1\n"
                "Submit gate: STOPPED before final Submit"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "First Role",
                "apply_url": "https://jobs.ashbyhq.com/acme/first/application",
                "runtime_script_path": str(first),
            },
            {
                "company": "Acme",
                "title": "Second Role",
                "apply_url": "https://jobs.ashbyhq.com/acme/second/application",
                "runtime_script_path": str(second),
            },
        ],
        runner=fake_run,
        database_path=db_path,
    )

    assert calls == [str(first)]
    assert [record["status"] for record in records] == [
        "autofill_completed_blocked",
        "skipped_policy_denied",
    ]
    assert records[1]["error"] == "failure_circuit_breaker_active"


def test_execute_application_batch_verified_repair_retry_bypasses_old_failure_circuit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    db_path = tmp_path / "tracking.db"
    conn = connect(db_path)
    init_db(conn)
    for index in range(2):
        prior_job = Job(
            title=f"Prior Role {index}",
            company="Acme",
            raw_jd="Role",
            apply_url=(
                f"https://jobs.ashbyhq.com/acme/prior-{index}/application"
            ),
        )
        application_id = create_application(
            conn,
            create_job(conn, prior_job),
            prior_job,
        )
        assert update_application_execution_status(
            conn,
            application_id,
            "autofill_completed_blocked",
        )
    conn.close()

    script = tmp_path / "verified-retry.js"
    script.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=18 review=0\n"
                "Submission confirmed: matched 'thank you for applying'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "application_id": "42",
                "company": "Acme",
                "title": "Verified Retry",
                "apply_url": (
                    "https://jobs.ashbyhq.com/acme/verified/application"
                ),
                "runtime_script_path": str(script),
                "retry": True,
                "retry_scope": "single_application",
                "repair_verified": True,
                "repair_cycle": 3,
            }
        ],
        runner=fake_run,
        database_path=db_path,
    )

    assert calls == [str(script)]
    assert records[0]["status"] == "submitted"
    assert records[0]["repair_verified"] is True
    assert records[0]["repair_cycle"] == 3


def test_execute_application_batch_unverified_retry_does_not_bypass_failure_circuit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    db_path = tmp_path / "tracking.db"
    conn = connect(db_path)
    init_db(conn)
    for index in range(2):
        prior_job = Job(
            title=f"Prior Role {index}",
            company="Acme",
            raw_jd="Role",
            apply_url=(
                f"https://jobs.ashbyhq.com/acme/prior-{index}/application"
            ),
        )
        application_id = create_application(
            conn,
            create_job(conn, prior_job),
            prior_job,
        )
        assert update_application_execution_status(
            conn,
            application_id,
            "autofill_completed_blocked",
        )
    conn.close()

    script = tmp_path / "unverified-retry.js"
    script.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        return CompletedProcess(command, 0, stdout="", stderr="")

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Unverified Retry",
                "apply_url": (
                    "https://jobs.ashbyhq.com/acme/unverified/application"
                ),
                "runtime_script_path": str(script),
                "retry": True,
                "retry_scope": "single_application",
            }
        ],
        runner=fake_run,
        database_path=db_path,
    )

    assert calls == []
    assert records[0]["error"] == "failure_circuit_breaker_active"


def test_execute_application_batch_records_unsupported_captcha_as_processing_error(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('Submission processing error: captcha blocked automatic submission')")

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=16 review=0\n"
                "CapMonster CAPTCHA: unsupported (CapMonster token API: ERROR_TASK_NOT_SUPPORTED)\n"
                "Submission processing error: captcha blocked automatic submission: unsupported "
                "(CapMonster token API: ERROR_TASK_NOT_SUPPORTED)"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "WeRide", "title": "Software Engineer, Algorithm", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "submission_processing_error"
    assert records[0]["submit_gate"] == "submission_processing_error"
    assert records[0]["error"] == "submission_processing_error"
    assert records[0]["filled_count"] == 16
    assert records[0]["review_count"] == 0
    assert records[0]["recovery_plan"]["strategy"] == "captcha_resolution"
    assert (
        records[0]["recovery_plan"]["actions"][1]["action"]
        == "complete_captcha_interactively"
    )


def test_execute_application_batch_prioritizes_review_blockers_over_captcha_marker(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    script.write_text("console.log('runtime')")
    evidence = package_dir / "review-required.txt"

    def fake_run(command, **kwargs):
        evidence.write_text("review_count: 1")
        return CompletedProcess(
            command,
            0,
            stdout=(
                'Review item: {"label":"Work authorization","reason":"no matching option",'
                '"sensitive":true,"blocking":true}\n'
                "Autofill stats: filled=16 review=1\n"
                "Submission processing error: captcha blocked automatic submission: "
                "captcha present at current page"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "ML Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert records[0]["submit_gate"] == "blocked_review_required"
    assert records[0]["error"] is None
    assert records[0]["review_count"] == 1
    assert records[0]["review_items"] == [
        {
            "label": "Work authorization",
            "reason": "no matching option",
            "sensitive": True,
            "blocking": True,
        }
    ]
    assert records[0]["evidence"] == str(evidence.resolve())


def test_structured_blocking_review_overrides_inconsistent_stats_and_anti_spam(
    tmp_path,
):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    script.write_text("console.log('runtime')")
    evidence = package_dir / "review-required.txt"

    def fake_run(command, **kwargs):
        evidence.write_text("review_count: 1")
        return CompletedProcess(
            command,
            0,
            stdout=(
                'Review item: {"label":"Phone","reason":"fill readback remained empty",'
                '"sensitive":false,"blocking":true}\n'
                "Autofill stats: filled=15 review=0\n"
                "CapMonster CAPTCHA: skipped (blocking review fields present)\n"
                "Submission processing error: matched 'flagged as possible spam' "
                "with recaptcha present"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "ML Engineer",
                "runtime_script_path": str(script),
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert records[0]["submit_gate"] == "blocked_review_required"
    assert records[0]["error"] is None
    assert records[0]["review_count"] == 1
    assert records[0]["review_items"] == [
        {
            "label": "Phone",
            "reason": "fill readback remained empty",
            "sensitive": False,
            "blocking": True,
        }
    ]
    assert records[0]["evidence"] == str(evidence.resolve())


def test_post_submit_review_uses_latest_stats_and_structured_blocker(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    script.write_text("console.log('runtime')")
    evidence = package_dir / "review-required.txt"

    def fake_run(command, **kwargs):
        evidence.write_text("review_count: 1")
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=15 review=0\n"
                'Review item: {"label":"Your Location","reason":"required field remains empty after fill",'
                '"sensitive":false,"blocking":true}\n'
                "Autofill stats: filled=16 review=1\n"
                "Submit gate: STOPPED before final Submit\n"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "IXL Learning",
                "title": "Software Engineer",
                "runtime_script_path": str(script),
            }
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert records[0]["filled_count"] == 16
    assert records[0]["review_count"] == 1
    assert records[0]["review_items"][0]["label"] == "Your Location"


def test_python_runtime_starts_unbuffered(monkeypatch):
    from job_agent import execution

    seen = {}

    def fake_stream(command, timeout_seconds):
        seen["command"] = command
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(execution, "_run_script_streaming", fake_stream)
    execution._run_python_runtime_streaming("/tmp/autofill-runtime.js", 10)

    assert seen["command"] == [
        execution.sys.executable,
        "-u",
        "-m",
        "job_agent.python_runtime",
        "/tmp/autofill-runtime.js",
    ]


def test_execute_application_batch_uses_default_gmail_token_for_python_runtime(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOB_AGENT_GMAIL_TOKEN_FILE", raising=False)
    secrets_dir = tmp_path / ".job-agent-secrets"
    secrets_dir.mkdir()
    (secrets_dir / "gmail-token.json").write_text("{}")

    script = tmp_path / "autofill-runtime.js"
    script.write_text('const { chromium } = require("playwright");\n')
    seen = {}

    def fake_python_runtime(script_path, timeout_seconds, *, runtime_env):
        seen["script_path"] = script_path
        seen["timeout_seconds"] = timeout_seconds
        seen["runtime_env"] = runtime_env
        return CompletedProcess(
            [sys.executable, "-m", "job_agent.python_runtime", script_path],
            0,
            stdout="Autofill stats: filled=1 review=0\nSubmit gate: STOPPED before final Submit",
            stderr="",
        )

    monkeypatch.setattr(execution, "_run_python_runtime", fake_python_runtime)

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        timeout_seconds=17,
    )

    assert seen == {
        "script_path": str(script),
        "timeout_seconds": 17,
        "runtime_env": None,
    }
    assert records[0]["status"] == "autofill_completed_blocked"


def test_execute_application_batch_passes_headed_override_to_runtime(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('Submit gate: STOPPED before final Submit')")
    seen_env = {}

    def fake_run(command, **kwargs):
        seen_env.update(kwargs.get("env") or {})
        return CompletedProcess(command, 0, stdout="Submit gate: STOPPED before final Submit", stderr="")

    execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
        browser_headless=False,
    )

    assert seen_env["BROWSER_HEADLESS"] == "false"


def test_execute_application_batch_records_missing_script_and_failure(tmp_path):
    failed_script = tmp_path / "failed.js"
    failed_script.write_text("throw new Error('failed')")

    def fake_run(command, **kwargs):
        return CompletedProcess(command, 2, stdout="", stderr="browser failed")

    records = execute_application_batch(
        [
            {"company": "Missing", "title": "Role", "runtime_script_path": None},
            {"company": "Broken", "title": "Role", "runtime_script_path": str(failed_script)},
        ],
        runner=fake_run,
    )

    assert records[0]["status"] == "skipped_missing_runtime_script"
    assert records[0]["exit_code"] is None
    assert records[1]["status"] == "autofill_failed"
    assert records[1]["exit_code"] == 2
    assert records[1]["error"] == "runtime_script_nonzero_exit"
    assert all(record["submit_gate"] == "automatic_submission_enabled" for record in records)


def test_execute_application_batch_runs_generated_runtime_script_with_node(tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime execution test")

    monkeypatch.delenv("JOB_AGENT_GMAIL_TOKEN_FILE", raising=False)
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "0")

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={"name": "Candidate", "email": "candidate@example.com"},
            max_pages=1,
        )
    )

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async inputValue() { return values[selector] || ''; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: 'name', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'email', label: 'Email', id: 'email', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {},
      };
    },
  },
};
"""
    )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        timeout_seconds=10,
        use_gmail_verification=False,
    )

    recovery_plan = records[0].pop("recovery_plan")
    assert records == [
        {
            "company": "Acme",
            "title": "Agent Engineer",
            "script_path": str(script),
            "status": "submit_clicked_unconfirmed",
            "exit_code": 0,
            "submit_gate": "submit_clicked_unconfirmed",
            "error": "submission_confirmation_not_detected",
            "filled_count": 2,
            "review_count": 0,
            "evidence": str((tmp_path / "submission-click-unconfirmed.txt").resolve()),
        }
    ]
    assert recovery_plan["strategy"] == "confirmation_reconciliation"


def test_execute_application_batch_streams_output_without_storing_it(tmp_path, capsys):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for streaming execution test")

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        "console.log('candidate@example.com'); "
        "console.log('Submit gate: STOPPED before final Submit');"
    )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        timeout_seconds=10,
    )

    captured = capsys.readouterr()
    assert "candidate@example.com" in captured.out
    assert records[0]["status"] == "autofill_completed_blocked"
    assert "candidate@example.com" not in str(records)


def test_execute_application_batch_closes_stdin_for_headed_runtime(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for stdin execution test")

    child = tmp_path / "headed-runtime.js"
    child.write_text(
        """
console.log('child waiting for manual review');
process.stdin.resume();
process.stdin.once('data', () => {
  console.log('child received manual review confirmation');
  console.log('Submit gate: STOPPED before final Submit');
  process.exit(0);
});
process.stdin.once('end', () => {
  console.log('child stdin closed');
  console.log('Submit gate: STOPPED before final Submit');
  process.exit(0);
});
"""
    )
    runner = tmp_path / "run_execute.py"
    runner.write_text(
        "import json\n"
        "from job_agent.execution import execute_application_batch\n"
        f"records = execute_application_batch([{{'company': 'Acme', 'title': 'Agent Engineer', 'runtime_script_path': {str(child)!r}}}], timeout_seconds=10)\n"
        "print('RECORDS=' + json.dumps(records))\n"
    )
    repo_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(repo_root / "src")}

    result = subprocess.run(
        [sys.executable, str(runner)],
        cwd=repo_root,
        env=env,
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "child waiting for manual review" in result.stdout
    assert "child stdin closed" in result.stdout
    assert "child received manual review confirmation" not in result.stdout
    records_line = next(line for line in result.stdout.splitlines() if line.startswith("RECORDS="))
    records = json.loads(records_line.removeprefix("RECORDS="))
    assert records[0]["status"] == "autofill_completed_blocked"


def test_execute_application_batch_times_out_and_recovers_process(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for timeout execution test")

    script = tmp_path / "slow-runtime.js"
    script.write_text("setInterval(() => console.log('still running'), 100);")

    records = execute_application_batch(
        [{"company": "SlowCo", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        timeout_seconds=1,
    )

    assert records[0]["company"] == "SlowCo"
    assert records[0]["title"] == "Agent Engineer"
    assert records[0]["script_path"] == str(script)
    assert records[0]["status"] == "autofill_timed_out"
    assert records[0]["exit_code"] is None
    assert records[0]["submit_gate"] == "automatic_submission_enabled"
    assert records[0]["error"] == "timeout"
    assert records[0]["filled_count"] is None
    assert records[0]["review_count"] is None
    evidence = tmp_path / "execution-timeout.txt"
    assert records[0]["evidence"] == str(evidence)
    assert evidence.exists()
    assert "status: autofill_timed_out" in evidence.read_text()


def test_python_runtime_in_process_enforces_timeout(tmp_path, monkeypatch):
    from job_agent import execution

    script = tmp_path / "autofill-runtime.js"
    script.write_text("// runtime payload fixture")

    monkeypatch.setattr(execution, "load_runtime_payload", lambda _path: {})
    monkeypatch.setattr(
        execution,
        "run_runtime_payload",
        lambda payload, action_runner=None, watchdog_deadline_seconds=None: time.sleep(5),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        execution._run_python_runtime_in_process(
            str(script),
            runtime_env=None,
            action_runner=lambda name, effect, context, callback: None,
            timeout_seconds=1,
        )
    if hasattr(signal, "getalarm"):
        assert signal.getalarm() == 0


def test_python_runtime_in_process_injects_terminal_evidence_directory(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    package_dir = tmp_path / "application"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    script.write_text("// runtime payload fixture")
    captured = {}

    monkeypatch.setattr(execution, "load_runtime_payload", lambda _path: {})

    def fake_runtime(payload, *, action_runner, watchdog_deadline_seconds):
        captured.update(payload)
        return 0

    monkeypatch.setattr(execution, "run_runtime_payload", fake_runtime)

    result = execution._run_python_runtime_in_process(
        str(script),
        runtime_env=None,
        action_runner=lambda name, effect, context, callback: None,
    )

    assert result.returncode == 0
    assert captured["_runtimeScriptDir"] == str(package_dir.resolve())


def test_execute_application_batch_maps_watchdog_triggered_exception_to_timeout(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution
    from job_agent import python_runtime

    script = tmp_path / "autofill-runtime.js"
    script.write_text("// runtime payload fixture")

    monkeypatch.setattr(execution, "load_runtime_payload", lambda _path: {})
    python_runtime._RUNTIME_TIMEOUT_TRIGGERED = True
    try:
        monkeypatch.setattr(
            execution,
            "run_runtime_payload",
            lambda payload, action_runner=None, watchdog_deadline_seconds=None: (
                (_ for _ in ()).throw(RuntimeError("browser closed by watchdog"))
            ),
        )
        records = execution._execute_application_batch_direct(
            [
                {
                    "company": "SlowCo",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script),
                }
            ],
            timeout_seconds=2,
            runtime_action_runner=lambda name, effect, context, callback: None,
        )
    finally:
        python_runtime._RUNTIME_TIMEOUT_TRIGGERED = False

    assert records[0]["status"] == "autofill_timed_out"
    assert records[0]["error"] == "watchdog_deadline"
    assert (tmp_path / "execution-timeout.txt").exists()


def test_execute_application_batch_continues_after_block_and_reports_progress(
    tmp_path,
    capsys,
):
    blocked = tmp_path / "blocked-runtime.js"
    submitted = tmp_path / "submitted-runtime.js"
    blocked.write_text("console.log('runtime')")
    submitted.write_text("console.log('runtime')")
    callback_events = []

    def fake_run(command, **kwargs):
        if command[1] == str(blocked):
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "Autofill stats: filled=12 review=0\n"
                    "Submission processing error: matched 'flagged as possible spam' "
                    "with recaptcha present"
                ),
                stderr="",
            )
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Autofill stats: filled=18 review=0\n"
                "Submission confirmed: matched 'thank you for applying'"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "BlockedCo",
                "title": "First Role",
                "runtime_script_path": str(blocked),
            },
            {
                "company": "NextCo",
                "title": "Second Role",
                "runtime_script_path": str(submitted),
            },
        ],
        runner=fake_run,
        on_record=lambda record, position, total: callback_events.append(
            (record["status"], position, total)
        ),
    )

    assert [record["status"] for record in records] == [
        "submission_blocked_by_anti_spam",
        "submitted",
    ]
    assert callback_events == [
        ("submission_blocked_by_anti_spam", 1, 2),
        ("submitted", 2, 2),
    ]
    output = capsys.readouterr().out
    assert (
        "Application 1/2 terminal: submission_blocked_by_anti_spam; continuing."
        in output
    )
    assert "Application 2/2 terminal: submitted; batch complete." in output


def test_execute_application_batch_continues_after_timeout(tmp_path):
    timed_out = tmp_path / "timed-out-runtime.js"
    next_script = tmp_path / "next-runtime.js"
    timed_out.write_text("console.log('runtime')")
    next_script.write_text("console.log('runtime')")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[1])
        if command[1] == str(timed_out):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return CompletedProcess(
            command,
            0,
            stdout="Autofill stats: filled=2 review=0\nSubmit gate: STOPPED before final Submit",
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "SlowCo",
                "title": "First Role",
                "runtime_script_path": str(timed_out),
            },
            {
                "company": "NextCo",
                "title": "Second Role",
                "runtime_script_path": str(next_script),
            },
        ],
        runner=fake_run,
        timeout_seconds=1,
    )

    assert calls == [str(timed_out), str(next_script)]
    assert [record["status"] for record in records] == [
        "autofill_timed_out",
        "autofill_completed_blocked",
    ]


def test_execute_application_batch_isolates_unexpected_runtime_exception(tmp_path):
    broken = tmp_path / "broken-runtime.js"
    next_script = tmp_path / "next-runtime.js"
    broken.write_text("console.log('runtime')")
    next_script.write_text("console.log('runtime')")

    def fake_run(command, **kwargs):
        if command[1] == str(broken):
            raise RuntimeError("browser transport closed with private page data")
        return CompletedProcess(
            command,
            0,
            stdout="Autofill stats: filled=2 review=0\nSubmit gate: STOPPED before final Submit",
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "BrokenCo",
                "title": "First Role",
                "runtime_script_path": str(broken),
            },
            {
                "company": "NextCo",
                "title": "Second Role",
                "runtime_script_path": str(next_script),
            },
        ],
        runner=fake_run,
    )

    assert [record["status"] for record in records] == [
        "autofill_failed",
        "autofill_completed_blocked",
    ]
    assert records[0]["error"] == "RuntimeError"
    assert "private page data" not in str(records[0])


def test_execute_application_batch_timeout_writes_sanitized_progress_evidence(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text("console.log('runtime')")

    def fake_run(command, **kwargs):
        exc = subprocess.TimeoutExpired(command, kwargs["timeout"])
        exc.stdout = (
            "Autofill field: Email* candidate@example.com\n"
            "Autofill stats: filled=4 review=1\n"
            "raw page text with candidate@example.com should be omitted\n"
        )
        exc.stderr = "stack trace with candidate@example.com\n"
        raise exc

    records = execute_application_batch(
        [{"company": "SlowCo", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
        timeout_seconds=7,
    )

    evidence = tmp_path / "execution-timeout.txt"
    text = evidence.read_text()
    assert records[0]["status"] == "autofill_timed_out"
    assert records[0]["filled_count"] == 4
    assert records[0]["review_count"] == 1
    assert records[0]["evidence"] == str(evidence)
    assert "timeout_seconds: 7" in text
    assert "Autofill field: Email* <email-redacted>" in text
    assert "Autofill stats: filled=4 review=1" in text
    assert "stderr_present: 1 line(s) captured but omitted" in text
    assert "candidate@example.com" not in text
    assert "raw page text" not in text


def test_execute_application_batch_rejects_generated_runtime_with_zero_fields(tmp_path):
    script = tmp_path / "autofill-runtime.js"
    script.write_text(render_runtime_autofill_script(profile={"name": "Candidate"}))

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Filled fields (0):\n"
                "Review-required (0):\n"
                "Autofill stats: filled=0 review=0\n"
                "Submit gate: STOPPED before final Submit\n"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "EmptyCo", "title": "Role", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_failed"
    assert records[0]["error"] == "no_application_fields_detected"
    assert records[0]["filled_count"] == 0
    assert records[0]["review_count"] == 0


def test_execute_application_batch_records_review_items_and_review_evidence(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    evidence = package_dir / "review-required.txt"
    script.write_text("console.log('Submit gate: STOPPED before final Submit')")

    def fake_run(command, **kwargs):
        evidence.write_text("review_count: 1")
        return CompletedProcess(
            command,
            0,
            stdout=(
                'Review item: {"label":"Preferred programming language","reason":"dropdown remained open without a committed selection","sensitive":false,"blocking":true}\n'
                "Autofill stats: filled=13 review=1\n"
                "Submit gate: STOPPED before final Submit\n"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Quora", "title": "MLE", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert records[0]["review_count"] == 1
    assert records[0]["evidence"] == str(evidence.resolve())
    assert records[0]["review_items"] == [
        {
            "label": "Preferred programming language",
            "reason": "dropdown remained open without a committed selection",
            "sensitive": False,
            "blocking": True,
        }
    ]


def test_execute_application_batch_distinguishes_candidate_account_gate(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    evidence = package_dir / "review-required.txt"
    script.write_text("console.log('Submit gate: STOPPED before final Submit')")

    def fake_run(command, **kwargs):
        evidence.write_text("review_count: 1")
        return CompletedProcess(
            command,
            0,
            stdout=(
                'Review item: {"label":"Password*","reason":"candidate account creation required","sensitive":false,"blocking":true}\n'
                "Candidate account required: configured candidate account password is missing\n"
                "Autofill stats: filled=1 review=1\n"
                "Submit gate: STOPPED before final Submit\n"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "BMS", "title": "Associate Engineer", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "candidate_account_required"
    assert records[0]["submit_gate"] == "candidate_account_required"
    assert records[0]["error"] == "candidate_account_required"
    assert records[0]["evidence"] == str(evidence.resolve())
    assert (
        records[0]["recovery_plan"]["strategy"]
        == "candidate_account_resume"
    )


def test_execute_application_batch_classifies_unavailable_application_form(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    evidence = package_dir / "review-required.txt"
    script.write_text("console.log('Submit gate: STOPPED before final Submit')")

    def fake_run(command, **kwargs):
        evidence.write_text("review_count: 1")
        return CompletedProcess(
            command,
            0,
            stdout=(
                "Application form unavailable: no visible job-application form was found\n"
                'Review item: {"label":"Application form","reason":"no visible job-application form was found","sensitive":false,"blocking":true}\n'
                "Autofill stats: filled=0 review=1\n"
                "Submit gate: automatic submission not performed because blocking review fields remain or the final Submit control is unavailable.\n"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Quantifind", "title": "Associate Data Scientist", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_failed"
    assert records[0]["submit_gate"] == "application_form_unavailable"
    assert records[0]["error"] == "application_form_unavailable"
    assert records[0]["evidence"] == str(evidence.resolve())
    assert records[0]["recovery_plan"]["strategy"] == "application_form_reconciliation"


def test_execute_application_batch_opens_global_network_health_circuit(tmp_path, monkeypatch):
    class Error(Exception):
        pass

    scripts = []
    for index in range(4):
        script = tmp_path / f"autofill-{index}.js"
        script.write_text("runtime")
        scripts.append(script)

    def fake_run(_command, **_kwargs):
        raise Error("net::ERR_NAME_NOT_RESOLVED")

    monkeypatch.setenv("JOB_AGENT_NETWORK_HEALTH_CIRCUIT_THRESHOLD", "3")
    records = execute_application_batch(
        [
            {
                "company": f"Company {index}",
                "title": "Engineer",
                "runtime_script_path": str(script),
            }
            for index, script in enumerate(scripts)
        ],
        runner=fake_run,
    )

    assert [record["error"] for record in records[:3]] == [
        "browser_navigation_network_error"
    ] * 3
    assert records[2]["network_health_observation"]["status"] == "open"
    assert records[3]["submit_gate"] == "network_health_circuit_active"
    assert records[3]["recovery_plan"]["strategy"] == "batch_network_health_recovery"


def test_execute_application_batch_clears_stale_terminal_evidence_before_rerun(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script = package_dir / "autofill-runtime.js"
    stale_files = [
        package_dir / "submission-confirmation.txt",
        package_dir / "submission-confirmation.png",
        package_dir / "submission-processing-error.txt",
        package_dir / "submission-click-unconfirmed.txt",
        package_dir / "email-verification-required.txt",
        package_dir / "review-required.txt",
    ]
    script.write_text("console.log('Submit gate: STOPPED before final Submit')")
    for path in stale_files:
        path.write_text("stale")

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout="Autofill stats: filled=2 review=0\nSubmit gate: STOPPED before final Submit\n",
            stderr="",
        )

    records = execute_application_batch(
        [{"company": "Acme", "title": "Role", "runtime_script_path": str(script)}],
        runner=fake_run,
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert all(not path.exists() for path in stale_files)


def test_execute_application_batch_timeout_kills_child_process_group(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for process-group cleanup test")
    if not hasattr(os, "killpg"):
        pytest.skip("process-group cleanup is only available on POSIX")

    marker = tmp_path / "child-survived.txt"
    script = tmp_path / "spawns-child-runtime.js"
    script.write_text(
        f"""
const {{ spawn }} = require('child_process');
const childCode = `
setTimeout(() => {{
  require('fs').writeFileSync({json.dumps(str(marker))}, 'alive');
}}, 1500);
setInterval(() => {{}}, 100);
`;
const child = spawn(process.execPath, ['-e', childCode], {{ stdio: 'ignore' }});
console.log('parent started child ' + child.pid);
setInterval(() => {{}}, 100);
"""
    )

    records = execute_application_batch(
        [{"company": "SlowCo", "title": "Agent Engineer", "runtime_script_path": str(script)}],
        timeout_seconds=1,
    )
    time.sleep(1.0)

    assert records[0]["status"] == "autofill_timed_out"
    assert not marker.exists()


def test_execute_application_batch_records_agent_core_loop_and_handoff(
    tmp_path,
):
    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        "console.log('candidate@example.com'); "
        "console.log('Submit gate: STOPPED before final Submit')"
    )
    traces = []

    def fake_run(command, **kwargs):
        return CompletedProcess(
            command,
            0,
            stdout=(
                "candidate@example.com\n"
                "Submit gate: STOPPED before final Submit"
            ),
            stderr="",
        )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "agent_handoff": {
                    "observation_id": "prepare-observation",
                    "kind": "tool_result",
                    "source": "runtime_package_builder",
                    "observed_at": "2026-07-28T12:00:00.000+00:00",
                    "payload": {
                        "call_id": "prepare-call",
                        "ok": True,
                        "effect": "write",
                        "policy_code": "allowed",
                        "output": {"status": "prepared"},
                    },
                },
            }
        ],
        runner=fake_run,
        on_agent_loop=lambda trace, _position, _total: traces.append(trace),
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert len(traces) == 1
    trace = traces[0]
    assert trace["status"] == "completed"
    assert trace["plan"]["steps"][0]["tool_name"] == "browser_execute"
    assert len(trace["rounds"]) == 2
    preflight = trace["preflight"]
    assert len(preflight["rounds"]) == 2
    assert all(
        branch["input_observation"]["observation_id"]
        == "prepare-observation"
        for branch in preflight["rounds"]
    )
    assert all(
        branch["input_observation"]["payload"]
        == {
            "call_id": "prepare-call",
            "ok": True,
            "effect": "write",
            "policy_code": "allowed",
            "output": {"status": "prepared"},
        }
        for branch in preflight["rounds"]
    )
    assert {
        branch["thought"]["selected_tool"]
        for branch in preflight["rounds"]
    } == {
        "runtime_package_inspect",
        "resume_provenance_inspect",
    }
    joined_observation_id = preflight["observations"][-1]["observation_id"]
    round_ = trace["rounds"][0]
    assert round_["input_observation"]["observation_id"] == (
        joined_observation_id
    )
    assert round_["thought"]["selected_tool"] == "browser_execute"
    assert round_["policy_decision"]["allowed"] is True
    assert round_["tool_result"]["output"]["status"] == (
        "autofill_completed_blocked"
    )
    assert round_["new_observation"]["observation_id"] == (
        round_["memory_update"]["observation_id"]
    )
    routing_round = trace["rounds"][1]
    assert routing_round["input_observation"]["observation_id"] == (
        round_["new_observation"]["observation_id"]
    )
    assert routing_round["thought"]["selected_tool"] == (
        "terminal_outcome_router"
    )
    assert routing_round["tool_result"]["output"]["status"] == (
        "autofill_completed_blocked"
    )
    assert "parameters" not in json.dumps(trace)
    assert "candidate@example.com" not in json.dumps(trace)


def test_live_runtime_actions_return_to_same_agent_core(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={
                "name": "Candidate",
                "email": "candidate@example.com",
            },
            application_url="https://jobs.example.com/apply",
            max_pages=1,
        )
    )
    traces = []

    def fake_in_process(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        action_runner(
            "ats_observe_page",
            "observe",
            {"phase": "page_observation"},
            lambda: [
                {
                    "type": "email",
                    "required": True,
                    "value": "candidate@example.com",
                }
            ],
        )
        action_runner(
            "ats_fill_fields",
            "write",
            {"phase": "field_fill"},
            lambda: {
                "filled": [
                    {
                        "label": "Email",
                        "readback": "candidate@example.com",
                    }
                ],
                "review": [],
            },
        )
        action_runner(
            "ats_advance_page",
            "write",
            {"phase": "page_navigation", "blocking_review_count": 0},
            lambda: None,
        )
        action_runner(
            "ats_submit_application",
            "submit",
            {
                "phase": "final_submission",
                "application_url": "https://jobs.example.com/apply",
                "submit_complete": True,
                "facts_verified": True,
                "blocking_review_items": [],
                "unapproved_sensitive_fields": [],
                "resume_verified": True,
                "confirmation_required": True,
            },
            lambda: None,
        )
        return CompletedProcess(
            [sys.executable, script_path],
            0,
            stdout=(
                "Autofill stats: filled=1 review=0\n"
                "Submission confirmed: matched thank you"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        execution,
        "_run_python_runtime_in_process",
        fake_in_process,
    )
    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "agent_runtime_id": "application-42",
                "application_id": "42",
                "agent_handoff": {
                    "observation_id": "prepare-observation",
                    "kind": "tool_result",
                    "source": "runtime_package_builder",
                },
            }
        ],
        on_agent_loop=lambda trace, _position, _total: traces.append(trace),
    )

    assert records[0]["status"] == "submitted"
    runtime_steps = traces[0]["runtime_steps"]
    assert [
        loop["rounds"][0]["thought"]["selected_tool"]
        for loop in runtime_steps
    ] == [
        "ats_observe_page",
        "ats_fill_fields",
        "ats_advance_page",
        "ats_submit_application",
    ]
    for previous, current in zip(runtime_steps, runtime_steps[1:]):
        assert (
            previous["observations"][-1]["observation_id"]
            == current["rounds"][0]["input_observation"]["observation_id"]
        )
    submit_round = runtime_steps[-1]["rounds"][0]
    assert submit_round["policy_decision"]["allowed"] is True
    assert runtime_steps[-2]["status"] == "in_progress"
    assert runtime_steps[-1]["status"] == "in_progress"
    assert [
        step["tool_name"]
        for step in runtime_steps[-2]["plan"]["steps"]
    ] == [
        "ats_advance_page",
        "ats_stop_page_navigation",
    ]
    assert [
        step["tool_name"]
        for step in runtime_steps[-1]["plan"]["steps"]
    ] == [
        "ats_submit_application",
        "ats_stop_before_submit",
    ]
    serialized = json.dumps(traces[0])
    assert "candidate@example.com" not in serialized
    assert '"parameters"' not in serialized


def test_live_runtime_retries_transient_playwright_error_before_any_ats_action(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={"name": "Candidate"},
            application_url="https://jobs.example.com/apply",
            max_pages=1,
        )
    )
    calls = []

    class Error(Exception):
        pass

    def fake_in_process(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        calls.append(script_path)
        if len(calls) == 1:
            raise Error(
                "Target page, context or browser has been closed; "
                "private page content must not be persisted"
            )
        return CompletedProcess(
            [sys.executable, script_path],
            0,
            stdout=(
                "Autofill stats: filled=1 review=1\n"
                "Submit gate: automatic submission not performed"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        execution,
        "_run_python_runtime_in_process",
        fake_in_process,
    )
    monkeypatch.setattr(
        execution,
        "PRE_ACTION_BROWSER_RETRY_DELAY_SECONDS",
        0,
    )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "agent_runtime_id": "application-42",
                "application_id": "42",
                "agent_handoff": {
                    "observation_id": "prepare-observation",
                    "kind": "tool_result",
                    "source": "runtime_package_builder",
                },
            }
        ],
    )

    assert len(calls) == 2
    assert records[0]["status"] == "autofill_completed_blocked"
    assert records[0]["runtime_retry_count"] == 1
    assert records[0]["runtime_retry_reason"] == "browser_session_closed"
    assert "private page content" not in str(records[0])


def test_live_runtime_does_not_restart_after_an_ats_action(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={"name": "Candidate"},
            application_url="https://jobs.example.com/apply",
            max_pages=1,
        )
    )
    calls = []

    class Error(Exception):
        pass

    def fake_in_process(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        calls.append(script_path)
        action_runner(
            "ats_observe_page",
            "observe",
            {"phase": "page_observation"},
            lambda: [],
        )
        raise Error("Target page, context or browser has been closed")

    monkeypatch.setattr(
        execution,
        "_run_python_runtime_in_process",
        fake_in_process,
    )
    monkeypatch.setattr(
        execution,
        "PRE_ACTION_BROWSER_RETRY_DELAY_SECONDS",
        0,
    )

    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "agent_runtime_id": "application-42",
                "application_id": "42",
                "agent_handoff": {
                    "observation_id": "prepare-observation",
                    "kind": "tool_result",
                    "source": "runtime_package_builder",
                },
            }
        ],
    )

    assert len(calls) == 1
    assert records[0]["status"] == "autofill_failed"
    assert records[0]["error"] == "browser_session_closed"
    assert "runtime_retry_count" not in records[0]


def test_runtime_observation_projection_keeps_labels_without_values():
    observed = _sanitize_runtime_action_output(
        "ats_observe_page",
        [
            {
                "label": "Email",
                "type": "email",
                "required": True,
                "value": "candidate@example.com",
                "options": [{"label": "Yes", "value": "private"}],
            }
        ],
    )

    assert observed["field_count"] == 1
    assert observed["fields"][0]["label"] == "Email"
    assert observed["fields"][0]["type"] == "email"
    assert observed["fields"][0]["options"] == ["Yes"]
    serialized = json.dumps(observed)
    assert "candidate@example.com" not in serialized
    assert "private" not in serialized


def test_live_runtime_advance_requires_state_assertion(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={"name": "Candidate"},
            application_url="https://jobs.example.com/apply",
            max_pages=1,
        )
    )
    callback_calls = []
    denied_reasons = []

    def fake_in_process(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        try:
            action_runner(
                "ats_advance_page",
                "write",
                {
                    "phase": "page_navigation",
                    "application_url": "https://jobs.example.com/apply",
                },
                lambda: callback_calls.append("clicked"),
            )
        except RuntimeActionDenied as exc:
            denied_reasons.append(str(exc))
        return CompletedProcess(
            [sys.executable, script_path],
            0,
            stdout=(
                "Autofill stats: filled=0 review=1\n"
                "Submit gate: automatic submission not performed"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        execution,
        "_run_python_runtime_in_process",
        fake_in_process,
    )

    execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "agent_runtime_id": "application-42",
                "application_id": "42",
                "agent_handoff": {
                    "observation_id": "prepare-observation",
                    "kind": "tool_result",
                    "source": "runtime_package_builder",
                },
            }
        ],
    )

    assert callback_calls == []
    assert denied_reasons == ["missing_navigation_state_assertion"]


def test_live_runtime_submit_stop_is_selected_without_clicking(
    tmp_path,
    monkeypatch,
):
    from job_agent import execution

    script = tmp_path / "autofill-runtime.js"
    script.write_text(
        render_runtime_autofill_script(
            profile={"name": "Candidate"},
            application_url="https://jobs.example.com/apply",
            max_pages=1,
        )
    )
    callback_calls = []
    denied_reasons = []
    traces = []

    def fake_in_process(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        try:
            action_runner(
                "ats_submit_application",
                "submit",
                {
                    "phase": "final_submission",
                    "application_url": "https://jobs.example.com/apply",
                    "submit_complete": False,
                    "facts_verified": True,
                    "blocking_review_items": [],
                    "unapproved_sensitive_fields": [],
                    "resume_verified": True,
                    "confirmation_required": True,
                },
                lambda: callback_calls.append("clicked"),
            )
        except RuntimeActionDenied as exc:
            denied_reasons.append(str(exc))
        return CompletedProcess(
            [sys.executable, script_path],
            0,
            stdout=(
                "Autofill stats: filled=1 review=0\n"
                "Submit gate: automatic submission not performed"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        execution,
        "_run_python_runtime_in_process",
        fake_in_process,
    )
    records = execute_application_batch(
        [
            {
                "company": "Acme",
                "title": "Agent Engineer",
                "runtime_script_path": str(script),
                "agent_runtime_id": "application-42",
                "application_id": "42",
                "agent_handoff": {
                    "observation_id": "prepare-observation",
                    "kind": "tool_result",
                    "source": "runtime_package_builder",
                },
            }
        ],
        on_agent_loop=lambda trace, _position, _total: traces.append(trace),
    )

    assert records[0]["status"] == "autofill_completed_blocked"
    assert callback_calls == []
    assert denied_reasons == ["submission_disabled"]
    submit_loop = traces[0]["runtime_steps"][-1]
    assert submit_loop["rounds"][0]["thought"]["selected_tool"] == (
        "ats_stop_before_submit"
    )
    assert submit_loop["rounds"][0]["tool_result"]["output"] == {
        "status": "stopped",
    }
