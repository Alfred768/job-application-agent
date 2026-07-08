from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen

import typer

from job_agent.db import connect, init_db
from job_agent.forms import build_form_fill_plan, inspect_form_snapshot, render_playwright_fill_script
from job_agent.jobs import (
    format_job_as_jd_text,
    jobs_to_dicts,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_remotive_jobs,
    parse_rss_jobs,
)
from job_agent.models import Job
from job_agent.resumes import index_resume_templates
from hello_agents.agents.job_application_agent import JobApplicationAgent

app = typer.Typer(help="Personal job application agent.")
forms_app = typer.Typer(help="Application form automation commands.")
jobs_app = typer.Typer(help="Job intake and review commands.")
resumes_app = typer.Typer(help="Resume template commands.")


class DeterministicLLM:
    provider = "deterministic"

    def invoke(self, messages, **kwargs):
        return ""


def _review_slug(index: int, job: Job) -> str:
    raw = f"{index:03d}-{job.company}-{job.title}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or f"{index:03d}-job"


def _read_json_source(payload: Optional[Path], url: str):
    if payload:
        return json.loads(payload.read_text())
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _write_jobs_json(jobs: list[Job], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True))


@app.command()
def init(db: Path = typer.Option(Path("job-agent.db"), "--db", help="SQLite database path.")) -> None:
    conn = connect(db)
    init_db(conn)
    typer.echo(f"Initialized database at {db}")


@resumes_app.command("index")
def index_resumes(source_dir: Path) -> None:
    templates = index_resume_templates(source_dir)
    for template in templates:
        typer.echo(f"{template.track}: docx={template.docx_path} pdf={template.pdf_path}")
    typer.echo(f"Indexed {len(templates)} resume templates")


@forms_app.command("build-script")
def build_form_script(
    form_snapshot: Path = typer.Option(
        ...,
        "--form-snapshot",
        help="JSON file containing captured application form fields.",
    ),
    profile: Path = typer.Option(
        ...,
        "--profile",
        help="JSON file containing approved profile facts.",
    ),
    out: Path = typer.Option(Path("fill-form.js"), "--out", help="JavaScript output path."),
    application_url: Optional[str] = typer.Option(
        None,
        "--application-url",
        help="Optional application page URL to open before filling fields.",
    ),
) -> None:
    plan = build_form_fill_plan(
        inspect_form_snapshot(form_snapshot.read_text()),
        json.loads(profile.read_text()),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_playwright_fill_script(plan, application_url=application_url))
    typer.echo(f"Wrote guarded form-fill script to {out}")


@jobs_app.command("review")
def review_job(
    jd_file: Path,
    out: Path = typer.Option(Path("application-review.md"), "--out", help="Markdown output path."),
    resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--resume-source-dir",
        help="Optional local directory containing role-specific resume templates.",
    ),
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help="Optional SQLite database path for application tracking.",
    ),
    package_dir: Optional[Path] = typer.Option(
        None,
        "--package-dir",
        help="Optional directory to export application package artifacts.",
    ),
    form_snapshot: Optional[Path] = typer.Option(
        None,
        "--form-snapshot",
        help="Optional JSON file containing captured application form fields.",
    ),
    profile: Optional[Path] = typer.Option(
        None,
        "--profile",
        help="Optional JSON file containing approved profile facts for form filling.",
    ),
) -> None:
    form_snapshot_json = form_snapshot.read_text() if form_snapshot else None
    profile_json = profile.read_text() if profile else None
    agent = JobApplicationAgent(
        name="job-application-agent",
        llm=DeterministicLLM(),
        resume_source_dir=resume_source_dir,
        database_path=db,
        package_dir=package_dir,
        form_snapshot_json=form_snapshot_json,
        profile_json=profile_json,
    )
    review = agent.run(jd_file.read_text())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(review)
    typer.echo(f"Wrote review packet to {out}")


@jobs_app.command("import-rss")
def import_rss_jobs(
    rss_file: Path,
    out: Path = typer.Option(Path("jobs.json"), "--out", help="JSON output path."),
    source: str = typer.Option("rss", "--source", help="Source label for provenance."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to import."),
) -> None:
    jobs = parse_rss_jobs(rss_file.read_text(), source=source, limit=limit)
    _write_jobs_json(jobs, out)
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


@jobs_app.command("import-greenhouse")
def import_greenhouse_jobs(
    board_token: str,
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Greenhouse JSON payload. If omitted, fetches the public API.",
    ),
    out: Path = typer.Option(Path("jobs.json"), "--out", help="JSON output path."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to import."),
) -> None:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    jobs = parse_greenhouse_jobs(_read_json_source(payload, url), board_token=board_token, limit=limit)
    _write_jobs_json(jobs, out)
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


@jobs_app.command("import-lever")
def import_lever_jobs(
    site: str,
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Lever JSON payload. If omitted, fetches the public API.",
    ),
    out: Path = typer.Option(Path("jobs.json"), "--out", help="JSON output path."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to import."),
) -> None:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    jobs = parse_lever_jobs(_read_json_source(payload, url), site=site, limit=limit)
    _write_jobs_json(jobs, out)
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


@jobs_app.command("import-remotive")
def import_remotive_jobs(
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Remotive JSON payload. If omitted, fetches the public API.",
    ),
    out: Path = typer.Option(Path("jobs.json"), "--out", help="JSON output path."),
    search: Optional[str] = typer.Option(None, "--search", help="Optional Remotive search query."),
    category: Optional[str] = typer.Option(None, "--category", help="Optional Remotive category or slug."),
    company_name: Optional[str] = typer.Option(None, "--company-name", help="Optional company-name filter."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to import."),
) -> None:
    query = {
        key: value
        for key, value in {
            "search": search,
            "category": category,
            "company_name": company_name,
            "limit": limit,
        }.items()
        if value is not None
    }
    suffix = f"?{urlencode(query)}" if query else ""
    jobs = parse_remotive_jobs(_read_json_source(payload, f"https://remotive.com/api/remote-jobs{suffix}"), limit=limit)
    _write_jobs_json(jobs, out)
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


@jobs_app.command("review-rss")
def review_rss_jobs(
    rss_file: Path,
    out_dir: Path = typer.Option(Path("reviews"), "--out-dir", help="Directory for markdown review packets."),
    source: str = typer.Option("rss", "--source", help="Source label for provenance."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to review."),
    resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--resume-source-dir",
        help="Optional local directory containing role-specific resume templates.",
    ),
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help="Optional SQLite database path for application tracking.",
    ),
    package_dir: Optional[Path] = typer.Option(
        None,
        "--package-dir",
        help="Optional directory root to export per-job application package artifacts.",
    ),
) -> None:
    jobs = parse_rss_jobs(rss_file.read_text(), source=source, limit=limit)
    out_dir.mkdir(parents=True, exist_ok=True)

    for index, job in enumerate(jobs, start=1):
        slug = _review_slug(index, job)
        agent = JobApplicationAgent(
            name="job-application-agent",
            llm=DeterministicLLM(),
            resume_source_dir=resume_source_dir,
            database_path=db,
            package_dir=(package_dir / slug) if package_dir else None,
        )
        review = agent.run(format_job_as_jd_text(job))
        (out_dir / f"{slug}.md").write_text(review)

    typer.echo(f"Reviewed {len(jobs)} jobs into {out_dir}")


app.add_typer(jobs_app, name="jobs")
app.add_typer(forms_app, name="forms")
app.add_typer(resumes_app, name="resumes")
