from __future__ import annotations

import csv
import os
import sqlite3
from pathlib import Path

from job_agent.jobs import canonical_job_url
from job_agent.models import Job


APPLICATION_LEDGER_COLUMNS = (
    "submitted_at_utc",
    "company",
    "role",
    "status",
    "application_url",
    "first_recorded_at_utc",
    "last_updated_at_utc",
    "application_id",
    "legacy_duplicate_of_application_id",
)


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists jobs (
            id integer primary key autoincrement,
            source text not null,
            source_url text,
            apply_url text,
            title text not null,
            company text not null,
            location text,
            remote_policy text,
            raw_jd text not null,
            parsed_jd_json text,
            retrieved_at text not null default current_timestamp,
            status text not null default 'new'
        );

        create table if not exists resume_templates (
            id integer primary key autoincrement,
            track text not null,
            docx_path text,
            pdf_path text,
            parsed_text text,
            last_indexed_at text not null default current_timestamp,
            unique(track, docx_path, pdf_path)
        );

        create table if not exists fit_scores (
            id integer primary key autoincrement,
            job_id integer not null,
            score integer not null,
            role_track text not null,
            matched_skills_json text not null,
            missing_keywords_json text not null,
            risks_json text not null,
            recommendation text not null,
            explanation text not null,
            created_at text not null default current_timestamp,
            foreign key(job_id) references jobs(id)
        );

        create table if not exists generated_documents (
            id integer primary key autoincrement,
            job_id integer not null,
            template_id integer,
            docx_path text,
            pdf_path text,
            edit_plan_json text,
            quality_checks_json text,
            created_at text not null default current_timestamp,
            foreign key(job_id) references jobs(id),
            foreign key(template_id) references resume_templates(id)
        );

        create table if not exists applications (
            id integer primary key autoincrement,
            job_id integer not null,
            company text not null,
            title text not null,
            apply_url text,
            status text not null default 'needs_review',
            generated_resume_id integer,
            cover_letter_id integer,
            form_snapshot_json text,
            user_review_notes text,
            upload_resume_pdf_path text,
            upload_resume_pdf_resolved_path text,
            upload_resume_pdf_size_bytes integer,
            upload_resume_pdf_sha256 text,
            required_resume_pdf_path text,
            required_resume_pdf_resolved_path text,
            required_resume_pdf_size_bytes integer,
            required_resume_pdf_sha256 text,
            dedupe_key text,
            created_at text not null default current_timestamp,
            submitted_at text,
            updated_at text not null default current_timestamp,
            foreign key(job_id) references jobs(id),
            foreign key(generated_resume_id) references generated_documents(id)
        );
        """
    )
    _ensure_application_resume_evidence_columns(conn)
    _ensure_application_tracking_columns(conn)
    _backfill_application_dedupe_keys(conn)
    conn.execute(
        """
        create unique index if not exists idx_applications_dedupe_key
        on applications(dedupe_key)
        where dedupe_key is not null
        """
    )
    conn.commit()


def _ensure_application_resume_evidence_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("pragma table_info(applications)").fetchall()
    }
    columns = {
        "upload_resume_pdf_path": "text",
        "upload_resume_pdf_resolved_path": "text",
        "upload_resume_pdf_size_bytes": "integer",
        "upload_resume_pdf_sha256": "text",
        "required_resume_pdf_path": "text",
        "required_resume_pdf_resolved_path": "text",
        "required_resume_pdf_size_bytes": "integer",
        "required_resume_pdf_sha256": "text",
    }
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"alter table applications add column {name} {column_type}")


def _ensure_application_tracking_columns(conn: sqlite3.Connection) -> None:
    existing = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("pragma table_info(applications)").fetchall()
    }
    if "dedupe_key" not in existing:
        conn.execute("alter table applications add column dedupe_key text")
    if "created_at" not in existing:
        conn.execute("alter table applications add column created_at text")

    required = {"submitted_at", "updated_at", "created_at"}
    current = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("pragma table_info(applications)").fetchall()
    }
    if required <= current:
        conn.execute(
            """
            update applications
            set created_at = coalesce(created_at, submitted_at, updated_at, current_timestamp)
            where created_at is null
            """
        )


def application_dedupe_key(
    company: str | None,
    title: str | None,
    apply_url: str | None,
) -> str | None:
    canonical_url = canonical_job_url(apply_url)
    if canonical_url:
        return f"url:{canonical_url}"

    company_key = " ".join(str(company or "").casefold().split())
    title_key = " ".join(str(title or "").casefold().split())
    if company_key in {"", "unknown company"} or title_key in {"", "unknown role"}:
        return None
    return f"role:{company_key}\x1f{title_key}"


def _backfill_application_dedupe_keys(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute("pragma table_info(applications)").fetchall()
    }
    required = {
        "id",
        "company",
        "title",
        "apply_url",
        "dedupe_key",
        "status",
        "submitted_at",
        "updated_at",
    }
    if not required <= columns:
        return

    rows = conn.execute(
        """
        select id, company, title, apply_url, dedupe_key
        from applications
        order by
            case when submitted_at is not null or status = 'submitted' then 0 else 1 end,
            coalesce(submitted_at, updated_at) desc,
            id desc
        """
    ).fetchall()
    claimed = {
        str(row["dedupe_key"])
        for row in rows
        if row["dedupe_key"] is not None and str(row["dedupe_key"]).strip()
    }
    for row in rows:
        if row["dedupe_key"] is not None and str(row["dedupe_key"]).strip():
            continue
        key = application_dedupe_key(
            row["company"],
            row["title"],
            row["apply_url"],
        )
        if key is None or key in claimed:
            continue
        try:
            conn.execute(
                "update applications set dedupe_key = ? where id = ?",
                (key, row["id"]),
            )
        except sqlite3.IntegrityError:
            continue
        claimed.add(key)


def create_job(conn: sqlite3.Connection, job: Job) -> int:
    cursor = conn.execute(
        """
        insert into jobs (
            source,
            source_url,
            apply_url,
            title,
            company,
            location,
            remote_policy,
            raw_jd
        )
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.source,
            job.source_url,
            job.apply_url,
            job.title,
            job.company,
            job.location,
            job.remote_policy,
            job.raw_jd,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def create_application(conn: sqlite3.Connection, job_id: int, job: Job) -> int:
    apply_url = job.apply_url or job.source_url
    dedupe_key = application_dedupe_key(job.company, job.title, apply_url)
    if dedupe_key is not None:
        existing = conn.execute(
            "select id from applications where dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

    try:
        cursor = conn.execute(
            """
            insert into applications (
                job_id,
                company,
                title,
                apply_url,
                status,
                dedupe_key,
                created_at
            )
            values (?, ?, ?, ?, ?, ?, current_timestamp)
            """,
            (
                job_id,
                job.company,
                job.title,
                apply_url,
                "needs_review",
                dedupe_key,
            ),
        )
    except sqlite3.IntegrityError:
        if dedupe_key is None:
            raise
        existing = conn.execute(
            "select id from applications where dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            raise
        return int(existing["id"])
    conn.commit()
    return int(cursor.lastrowid)


def export_application_ledger(
    conn: sqlite3.Connection,
    path: str | Path,
) -> int:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        """
        select id, company, title, apply_url, status,
               created_at, submitted_at, updated_at, dedupe_key
        from applications
        order by coalesce(submitted_at, updated_at, created_at) desc, id desc
        """
    ).fetchall()
    canonical_ids = {
        str(row["dedupe_key"]): int(row["id"])
        for row in rows
        if row["dedupe_key"] is not None and str(row["dedupe_key"]).strip()
    }
    temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=APPLICATION_LEDGER_COLUMNS)
            writer.writeheader()
            for row in rows:
                key = application_dedupe_key(
                    row["company"],
                    row["title"],
                    row["apply_url"],
                )
                canonical_id = canonical_ids.get(key or "")
                duplicate_of = (
                    str(canonical_id)
                    if canonical_id is not None and canonical_id != int(row["id"])
                    else ""
                )
                writer.writerow(
                    {
                        "submitted_at_utc": _ledger_cell(row["submitted_at"]),
                        "company": _ledger_cell(row["company"]),
                        "role": _ledger_cell(row["title"]),
                        "status": _ledger_cell(row["status"]),
                        "application_url": _ledger_cell(row["apply_url"]),
                        "first_recorded_at_utc": _ledger_cell(row["created_at"]),
                        "last_updated_at_utc": _ledger_cell(row["updated_at"]),
                        "application_id": str(row["id"]),
                        "legacy_duplicate_of_application_id": duplicate_of,
                    }
                )
        temporary.replace(target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return len(rows)


def _ledger_cell(value: object) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def update_application_execution_status(
    conn: sqlite3.Connection,
    application_id: int,
    status: str,
) -> bool:
    """Persist an execution outcome for an existing tracked application.

    Only a verified confirmation may set ``submitted_at``. Other runtime
    outcomes remain useful follow-up states but must never look submitted.
    """
    if status == "submitted":
        cursor = conn.execute(
            """
            update applications
            set status = ?,
                submitted_at = coalesce(submitted_at, current_timestamp),
                updated_at = case
                    when submitted_at is null or status <> 'submitted' then current_timestamp
                    else updated_at
                end
            where id = ?
            """,
            (status, application_id),
        )
        conn.execute(
            """
            update jobs
            set status = ?
            where id = (select job_id from applications where id = ?)
            """,
            (status, application_id),
        )
    else:
        cursor = conn.execute(
            """
            update applications
            set status = ?, updated_at = current_timestamp
            where id = ?
              and submitted_at is null
              and status <> 'submitted'
            """,
            (status, application_id),
        )
    conn.commit()
    if cursor.rowcount == 1:
        return True
    return (
        conn.execute("select 1 from applications where id = ?", (application_id,)).fetchone()
        is not None
    )


def update_application_resume_evidence(
    conn: sqlite3.Connection,
    application_id: int,
    evidence: dict[str, object],
) -> bool:
    """Persist privacy-safe proof of the resume PDF used by an execution."""
    cursor = conn.execute(
        """
        update applications
        set upload_resume_pdf_path = ?,
            upload_resume_pdf_resolved_path = ?,
            upload_resume_pdf_size_bytes = ?,
            upload_resume_pdf_sha256 = ?,
            required_resume_pdf_path = ?,
            required_resume_pdf_resolved_path = ?,
            required_resume_pdf_size_bytes = ?,
            required_resume_pdf_sha256 = ?,
            updated_at = current_timestamp
        where id = ?
        """,
        (
            evidence.get("upload_resume_pdf_path"),
            evidence.get("upload_resume_pdf_resolved_path"),
            evidence.get("upload_resume_pdf_size_bytes"),
            evidence.get("upload_resume_pdf_sha256"),
            evidence.get("required_resume_pdf_path"),
            evidence.get("required_resume_pdf_resolved_path"),
            evidence.get("required_resume_pdf_size_bytes"),
            evidence.get("required_resume_pdf_sha256"),
            application_id,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1
