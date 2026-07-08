from __future__ import annotations

import sqlite3
from pathlib import Path


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
            submitted_at text,
            updated_at text not null default current_timestamp,
            foreign key(job_id) references jobs(id),
            foreign key(generated_resume_id) references generated_documents(id)
        );
        """
    )
    conn.commit()
