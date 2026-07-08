from __future__ import annotations

from pathlib import Path

import typer

from job_agent.db import connect, init_db
from job_agent.jobs import import_job_from_text
from job_agent.reports import render_markdown_review
from job_agent.resumes import index_resume_templates
from job_agent.scoring import score_fit

app = typer.Typer(help="Personal job application agent.")
jobs_app = typer.Typer(help="Job intake and review commands.")
resumes_app = typer.Typer(help="Resume template commands.")


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
) -> None:
    job = import_job_from_text(jd_file.read_text())
    review = render_markdown_review(job, score_fit(job))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(review)
    typer.echo(f"Wrote review packet to {out}")


app.add_typer(jobs_app, name="jobs")
app.add_typer(resumes_app, name="resumes")
