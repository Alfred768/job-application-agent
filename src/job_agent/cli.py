from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from job_agent.db import connect, init_db
from job_agent.jobs import jobs_to_dicts, parse_rss_jobs
from job_agent.resumes import index_resume_templates
from hello_agents.agents.job_application_agent import JobApplicationAgent

app = typer.Typer(help="Personal job application agent.")
jobs_app = typer.Typer(help="Job intake and review commands.")
resumes_app = typer.Typer(help="Resume template commands.")


class DeterministicLLM:
    provider = "deterministic"

    def invoke(self, messages, **kwargs):
        return ""


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
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True))
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


app.add_typer(jobs_app, name="jobs")
app.add_typer(resumes_app, name="resumes")
