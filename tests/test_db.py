import csv
import hashlib

from job_agent.cli import _persist_execution_statuses, _reconcile_confirmed_evidence
from job_agent.db import (
    connect,
    create_application,
    create_job,
    export_application_ledger,
    init_db,
    update_application_execution_status,
)
from job_agent.models import Job


def test_init_db_creates_core_tables(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = connect(db_path)

    init_db(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    }
    assert {
        "jobs",
        "resume_templates",
        "fit_scores",
        "applications",
        "generated_documents",
    } <= tables
    application_columns = {
        row["name"]
        for row in conn.execute("pragma table_info(applications)").fetchall()
    }
    assert {
        "upload_resume_pdf_path",
        "upload_resume_pdf_resolved_path",
        "upload_resume_pdf_size_bytes",
        "upload_resume_pdf_sha256",
        "required_resume_pdf_path",
        "required_resume_pdf_resolved_path",
        "required_resume_pdf_size_bytes",
        "required_resume_pdf_sha256",
        "created_at",
        "dedupe_key",
    } <= application_columns


def test_init_db_migrates_existing_applications_table_with_resume_evidence_columns(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = connect(db_path)
    conn.execute(
        """
        create table applications (
            id integer primary key autoincrement,
            job_id integer not null,
            company text not null,
            title text not null,
            apply_url text,
            status text not null default 'needs_review',
            submitted_at text,
            updated_at text not null default current_timestamp
        )
        """
    )
    conn.commit()

    init_db(conn)

    application_columns = {
        row["name"]
        for row in conn.execute("pragma table_info(applications)").fetchall()
    }
    assert "upload_resume_pdf_sha256" in application_columns
    assert "required_resume_pdf_sha256" in application_columns
    assert "created_at" in application_columns
    assert "dedupe_key" in application_columns


def test_create_application_reuses_canonical_tracking_url(tmp_path):
    conn = connect(tmp_path / "agent.db")
    init_db(conn)
    first = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="rss",
        apply_url="https://jobs.example.com/acme-agent?utm_source=rss",
    )
    second = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="greenhouse",
        apply_url="https://jobs.example.com/acme-agent?ref=careers",
    )

    first_id = create_application(conn, create_job(conn, first), first)
    second_id = create_application(conn, create_job(conn, second), second)

    assert second_id == first_id
    assert conn.execute("select count(*) from applications").fetchone()[0] == 1


def test_create_application_reuses_greenhouse_requisition_across_url_aliases(tmp_path):
    conn = connect(tmp_path / "agent.db")
    init_db(conn)
    custom = Job(
        title="Machine Learning Systems Engineer",
        company="Motional",
        raw_jd="Build ML systems.",
        source="company-site",
        apply_url="https://motional.com/open-positions/?gh_jid=7730609003#/7730609003",
    )
    official = Job(
        title="Machine Learning Systems Engineer",
        company="Motional",
        raw_jd="Build ML systems.",
        source="greenhouse:motional",
        apply_url="https://job-boards.greenhouse.io/motional/jobs/7730609003",
    )

    first_id = create_application(conn, create_job(conn, custom), custom)
    second_id = create_application(conn, create_job(conn, official), official)

    assert second_id == first_id
    assert conn.execute("select count(*) from applications").fetchone()[0] == 1


def test_create_application_reuses_lever_requisition_across_url_aliases(tmp_path):
    conn = connect(tmp_path / "agent.db")
    init_db(conn)
    posting = Job(
        title="Machine Learning Engineer",
        company="Acme",
        raw_jd="Build ML systems.",
        source="lever:acme",
        apply_url="https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000",
    )
    apply = Job(
        title="Machine Learning Engineer",
        company="Acme",
        raw_jd="Build ML systems.",
        source="company-site",
        apply_url=(
            "https://jobs.lever.co/acme/123e4567-e89b-12d3-a456-426614174000/apply"
            "?lever-source=company-site"
        ),
    )

    first_id = create_application(conn, create_job(conn, posting), posting)
    second_id = create_application(conn, create_job(conn, apply), apply)

    assert second_id == first_id
    assert conn.execute("select count(*) from applications").fetchone()[0] == 1


def test_create_application_keeps_distinct_requisition_urls(tmp_path):
    conn = connect(tmp_path / "agent.db")
    init_db(conn)
    first = Job(
        title="Machine Learning Engineer",
        company="Acme",
        raw_jd="First team.",
        source="test",
        apply_url="https://jobs.example.com/acme/jobs/100",
    )
    second = Job(
        title="Machine Learning Engineer",
        company="Acme",
        raw_jd="Second team.",
        source="test",
        apply_url="https://jobs.example.com/acme/jobs/200",
    )

    first_id = create_application(conn, create_job(conn, first), first)
    second_id = create_application(conn, create_job(conn, second), second)

    assert second_id != first_id
    assert conn.execute("select count(*) from applications").fetchone()[0] == 2


def test_application_ledger_exports_submission_time_company_and_role(tmp_path):
    conn = connect(tmp_path / "agent.db")
    init_db(conn)
    job = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="test",
        apply_url="https://jobs.example.com/acme-agent",
    )
    application_id = create_application(conn, create_job(conn, job), job)
    assert update_application_execution_status(conn, application_id, "submitted")
    ledger_path = tmp_path / "APPLICATION_LEDGER.csv"

    row_count = export_application_ledger(conn, ledger_path)

    with ledger_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert row_count == 1
    assert rows == [
        {
            "submitted_at_utc": rows[0]["submitted_at_utc"],
            "company": "Acme",
            "role": "Agent Engineer",
            "status": "submitted",
            "application_url": "https://jobs.example.com/acme-agent",
            "first_recorded_at_utc": rows[0]["first_recorded_at_utc"],
            "last_updated_at_utc": rows[0]["last_updated_at_utc"],
            "application_id": str(application_id),
            "legacy_duplicate_of_application_id": "",
        }
    ]
    assert rows[0]["submitted_at_utc"]
    assert rows[0]["first_recorded_at_utc"]


def test_legacy_duplicate_rows_are_preserved_and_marked_in_ledger(tmp_path):
    conn = connect(tmp_path / "agent.db")
    init_db(conn)
    job = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="test",
        apply_url="https://jobs.example.com/acme-agent?utm_source=rss",
    )
    first_id = create_application(conn, create_job(conn, job), job)
    assert update_application_execution_status(conn, first_id, "submitted")
    second_job_id = create_job(conn, job)
    conn.execute("drop index idx_applications_dedupe_key")
    conn.execute("update applications set dedupe_key = null")
    second_id = conn.execute(
        """
        insert into applications (
            job_id, company, title, apply_url, status, created_at, updated_at
        )
        values (?, ?, ?, ?, 'needs_review', current_timestamp, current_timestamp)
        """,
        (
            second_job_id,
            job.company,
            job.title,
            "https://jobs.example.com/acme-agent?ref=careers",
        ),
    ).lastrowid
    conn.commit()

    init_db(conn)
    ledger_path = tmp_path / "APPLICATION_LEDGER.csv"
    export_application_ledger(conn, ledger_path)
    repeated = Job(
        title=job.title,
        company=job.company,
        raw_jd=job.raw_jd,
        source="another-source",
        apply_url="https://jobs.example.com/acme-agent?source=search",
    )
    repeated_id = create_application(conn, create_job(conn, repeated), repeated)

    assert conn.execute("select count(*) from applications").fetchone()[0] == 2
    assert repeated_id == first_id
    assert conn.execute(
        "select count(*) from applications where dedupe_key is not null"
    ).fetchone()[0] == 1
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    duplicate_rows = [
        row for row in rows if row["legacy_duplicate_of_application_id"]
    ]
    assert len(duplicate_rows) == 1
    assert duplicate_rows[0]["legacy_duplicate_of_application_id"] == str(first_id)
    assert duplicate_rows[0]["application_id"] == str(second_id)


def test_execution_status_persistence_requires_confirmed_submission(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = connect(db_path)
    init_db(conn)
    job = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="test",
        apply_url="https://jobs.example.com/acme-agent",
    )
    job_id = create_job(conn, job)
    application_id = create_application(conn, job_id, job)

    assert update_application_execution_status(conn, application_id, "email_verification_required")
    row = conn.execute(
        "select status, submitted_at from applications where id = ?", (application_id,)
    ).fetchone()
    assert tuple(row) == ("email_verification_required", None)

    records = [
        {
            "script_path": "/tmp/acme-runtime.js",
            "status": "submitted",
            "upload_resume_pdf_path": "/resumes/agent.pdf",
            "upload_resume_pdf_resolved_path": "/resumes/agent.pdf",
            "upload_resume_pdf_size_bytes": 12345,
            "upload_resume_pdf_sha256": "upload-sha",
            "required_resume_pdf_path": "/resumes/agent.pdf",
            "required_resume_pdf_resolved_path": "/resumes/agent.pdf",
            "required_resume_pdf_size_bytes": 12345,
            "required_resume_pdf_sha256": "required-sha",
        }
    ]
    summary = [
        {
            "runtime_script_path": "/tmp/acme-runtime.js",
            "application_id": str(application_id),
        }
    ]
    assert _persist_execution_statuses(records, summary, db_path) == {
        "updated": 1,
        "missing_application_id": 0,
        "application_not_found": 0,
    }

    row = conn.execute(
        """
        select status, submitted_at,
               upload_resume_pdf_path, upload_resume_pdf_resolved_path,
               upload_resume_pdf_size_bytes, upload_resume_pdf_sha256,
               required_resume_pdf_path, required_resume_pdf_resolved_path,
               required_resume_pdf_size_bytes, required_resume_pdf_sha256
        from applications
        where id = ?
        """,
        (application_id,),
    ).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] is not None
    assert row["upload_resume_pdf_path"] == "/resumes/agent.pdf"
    assert row["upload_resume_pdf_resolved_path"] == "/resumes/agent.pdf"
    assert row["upload_resume_pdf_size_bytes"] == 12345
    assert row["upload_resume_pdf_sha256"] == "upload-sha"
    assert row["required_resume_pdf_path"] == "/resumes/agent.pdf"
    assert row["required_resume_pdf_resolved_path"] == "/resumes/agent.pdf"
    assert row["required_resume_pdf_size_bytes"] == 12345
    assert row["required_resume_pdf_sha256"] == "required-sha"
    assert conn.execute("select status from jobs where id = ?", (job_id,)).fetchone()[0] == "submitted"


def test_confirmed_submission_does_not_overwrite_existing_submitted_timestamp(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = connect(db_path)
    init_db(conn)
    job = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="test",
        apply_url="https://jobs.example.com/acme-agent",
    )
    job_id = create_job(conn, job)
    application_id = create_application(conn, job_id, job)
    conn.execute(
        """
        update applications
        set status = 'submitted',
            submitted_at = '2026-07-19 01:05:31',
            updated_at = '2026-07-19 01:05:31'
        where id = ?
        """,
        (application_id,),
    )
    conn.commit()

    assert update_application_execution_status(conn, application_id, "submitted")

    row = conn.execute(
        "select status, submitted_at, updated_at from applications where id = ?",
        (application_id,),
    ).fetchone()
    assert tuple(row) == ("submitted", "2026-07-19 01:05:31", "2026-07-19 01:05:31")


def test_non_submission_outcome_does_not_downgrade_submitted_application(tmp_path):
    db_path = tmp_path / "agent.db"
    conn = connect(db_path)
    init_db(conn)
    job = Job(
        title="Agent Engineer",
        company="Acme",
        raw_jd="Build agent systems.",
        source="test",
        apply_url="https://jobs.example.com/acme-agent",
    )
    job_id = create_job(conn, job)
    application_id = create_application(conn, job_id, job)
    conn.execute(
        """
        update applications
        set status = 'submitted',
            submitted_at = '2026-07-19 01:05:31',
            updated_at = '2026-07-19 01:05:31'
        where id = ?
        """,
        (application_id,),
    )
    conn.commit()

    assert update_application_execution_status(conn, application_id, "autofill_timed_out")

    row = conn.execute(
        "select status, submitted_at, updated_at from applications where id = ?",
        (application_id,),
    ).fetchone()
    assert tuple(row) == ("submitted", "2026-07-19 01:05:31", "2026-07-19 01:05:31")


def test_reconcile_confirmed_evidence_creates_only_verified_submission_rows(tmp_path):
    package_dir = tmp_path / "confirmed-package"
    package_dir.mkdir()
    resume = tmp_path / "resume.pdf"
    resume_bytes = b"%PDF-1.4\nconfirmed resume"
    resume.write_bytes(resume_bytes)
    runtime = package_dir / "autofill-runtime.js"
    runtime.write_text(
        f'const CFG = {{"applicationUrl":"https://jobs.example.com/acme-agent","resumeFile":"{resume}"}};'
    )
    (package_dir / "submission-confirmation.txt").write_text(
        "confirmation: matched 'thank you for applying' at https://jobs.example.com/confirmation\n"
    )
    blocked_dir = tmp_path / "blocked-package"
    blocked_dir.mkdir()
    (blocked_dir / "submission-confirmation.txt").write_text("confirmation: not detected\n")
    db_path = tmp_path / "agent.db"

    result = _reconcile_confirmed_evidence(
        [
            {
                "package_dir": str(package_dir),
                "runtime_script_path": str(runtime),
                "company": "Acme",
                "title": "Agent Engineer",
                "upload_resume_path": str(resume),
            },
            {
                "package_dir": str(blocked_dir),
                "company": "Blocked",
                "title": "Role",
                "apply_url": "https://jobs.example.com/blocked",
            },
        ],
        db_path,
    )

    assert result == {"confirmed_evidence": 1, "created": 1, "updated": 1}
    conn = connect(db_path)
    rows = conn.execute(
        "select company, apply_url, status, submitted_at, upload_resume_pdf_path, upload_resume_pdf_sha256 from applications"
    ).fetchall()
    assert [
        (
            row["company"],
            row["apply_url"],
            row["status"],
            row["submitted_at"] is not None,
            row["upload_resume_pdf_path"],
            row["upload_resume_pdf_sha256"],
        )
        for row in rows
    ] == [
        (
            "Acme",
            "https://jobs.example.com/acme-agent",
            "submitted",
            True,
            str(resume),
            hashlib.sha256(resume_bytes).hexdigest(),
        )
    ]
