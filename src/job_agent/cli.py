from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen

import typer

from job_agent.db import connect, init_db
from job_agent.forms import (
    build_form_fill_plan,
    inspect_form_snapshot,
    render_playwright_fill_script,
    render_playwright_form_snapshot_script,
)
from job_agent.jobs import (
    format_job_as_jd_text,
    jobs_to_dicts,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_remotive_jobs,
    parse_rss_jobs,
)
from job_agent.models import Job
from job_agent.resume_plans import propose_resume_edit_plan, render_tailored_resume_draft
from job_agent.resumes import index_resume_templates
from job_agent.runners import render_batch_fill_runner
from job_agent.shortlist import shortlisted_jobs_to_dicts, shortlist_jobs
from job_agent.source_config import load_jobs_from_source_config
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.core.llm import HelloAgentsLLM

app = typer.Typer(help="Personal job application agent.")
applications_app = typer.Typer(help="End-to-end application preparation commands.")
forms_app = typer.Typer(help="Application form automation commands.")
jobs_app = typer.Typer(help="Job intake and review commands.")
llm_app = typer.Typer(help="LLM configuration and connectivity commands.")
resumes_app = typer.Typer(help="Resume template commands.")


class DeterministicLLM:
    provider = "deterministic"

    def invoke(self, messages, **kwargs):
        return ""


def _build_llm(
    use_llm: bool = False,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
):
    if not use_llm:
        return DeterministicLLM()
    return HelloAgentsLLM(
        model=model,
        provider=provider,
        base_url=base_url,
        temperature=0.2,
    )


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


def _job_from_dict(raw: dict) -> Job:
    return Job(
        title=raw.get("title") or "Unknown Role",
        company=raw.get("company") or "Unknown Company",
        raw_jd=raw.get("raw_jd") or "",
        location=raw.get("location"),
        source=raw.get("source") or "json",
        source_url=raw.get("source_url"),
        apply_url=raw.get("apply_url"),
        remote_policy=raw.get("remote_policy"),
    )


def _write_review_packets(
    jobs: list[Job],
    out_dir: Path,
    resume_source_dir: Optional[Path] = None,
    db: Optional[Path] = None,
    package_dir: Optional[Path] = None,
    use_llm: bool = False,
    llm_model: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_base_url: Optional[str] = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = _build_llm(
        use_llm=use_llm,
        model=llm_model,
        provider=llm_provider,
        base_url=llm_base_url,
    )
    for index, job in enumerate(jobs, start=1):
        slug = _review_slug(index, job)
        agent = JobApplicationAgent(
            name="job-application-agent",
            llm=llm,
            resume_source_dir=resume_source_dir,
            database_path=db,
            package_dir=(package_dir / slug) if package_dir else None,
        )
        review = agent.run(format_job_as_jd_text(job))
        (out_dir / f"{slug}.md").write_text(review)


def _prepare_application_package(
    job: Job,
    out_dir: Path,
    resume_source_dir: Optional[Path] = None,
    db: Optional[Path] = None,
    form_snapshot: Optional[Path] = None,
    profile: Optional[Path] = None,
    resume: Optional[Path] = None,
    upload_resume: bool = False,
    use_llm: bool = False,
    llm_model: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_base_url: Optional[str] = None,
) -> dict[str, str | None]:
    form_snapshot_json = form_snapshot.read_text() if form_snapshot else None
    profile_json = profile.read_text() if profile else None
    agent = JobApplicationAgent(
        name="job-application-agent",
        llm=_build_llm(
            use_llm=use_llm,
            model=llm_model,
            provider=llm_provider,
            base_url=llm_base_url,
        ),
        resume_source_dir=resume_source_dir,
        database_path=db,
        package_dir=out_dir,
        form_snapshot_json=form_snapshot_json,
        profile_json=profile_json,
    )
    review = agent.run(format_job_as_jd_text(job))
    out_dir.mkdir(parents=True, exist_ok=True)
    review_path = out_dir / "review.md"
    review_path.write_text(review)

    tailored_resume_path = None
    if resume:
        tailored_resume_path = out_dir / "tailored-resume.md"
        resume_plan = propose_resume_edit_plan(format_job_as_jd_text(job))
        tailored_resume_path.write_text(
            render_tailored_resume_draft(resume.read_text(), resume_plan)
        )

    fill_script_path = None
    if form_snapshot and profile:
        profile_facts = json.loads(profile.read_text())
        if upload_resume and tailored_resume_path:
            profile_facts["resume_file"] = str(tailored_resume_path)
        plan = build_form_fill_plan(
            inspect_form_snapshot(form_snapshot.read_text()),
            profile_facts,
        )
        fill_script_path = out_dir / "fill-form.js"
        fill_script_path.write_text(
            render_playwright_fill_script(plan, application_url=job.apply_url or job.source_url)
        )

    return {
        "company": job.company,
        "title": job.title,
        "apply_url": job.apply_url or job.source_url,
        "package_dir": str(out_dir),
        "review_path": str(review_path),
        "tailored_resume_path": str(tailored_resume_path) if tailored_resume_path else None,
        "fill_script_path": str(fill_script_path) if fill_script_path else None,
    }


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


@resumes_app.command("tailor")
def tailor_resume(
    jd_file: Path,
    resume: Path = typer.Option(..., "--resume", help="Base resume text/markdown file to preserve."),
    out: Path = typer.Option(Path("tailored-resume.md"), "--out", help="Tailored resume draft output path."),
    resume_track: Optional[str] = typer.Option(None, "--resume-track", help="Optional selected resume track."),
) -> None:
    plan = propose_resume_edit_plan(jd_file.read_text(), resume_track=resume_track)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_tailored_resume_draft(resume.read_text(), plan))
    typer.echo(f"Wrote tailored resume draft to {out}")


@llm_app.command("smoke")
def smoke_llm(
    prompt: str = typer.Option("ping", "--prompt", help="Prompt to send to the configured LLM."),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use HelloAgentsLLM instead of deterministic mode."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    llm = _build_llm(
        use_llm=use_llm,
        model=llm_model,
        provider=llm_provider,
        base_url=llm_base_url,
    )
    typer.echo(llm.invoke([{"role": "user", "content": prompt}]))


@applications_app.command("prepare")
def prepare_application(
    jobs_file: Path,
    index: int = typer.Option(1, "--index", help="1-based job index in the normalized jobs JSON file."),
    out_dir: Path = typer.Option(Path("application-package"), "--out-dir", help="Application package output directory."),
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
    resume: Optional[Path] = typer.Option(
        None,
        "--resume",
        help="Optional base resume text/markdown file for tailored resume draft generation.",
    ),
    upload_resume: bool = typer.Option(
        False,
        "--upload-resume",
        help="When --resume and form data are provided, wire tailored-resume.md into Resume/CV upload fields.",
    ),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    raw_jobs = json.loads(jobs_file.read_text())
    if index < 1 or index > len(raw_jobs):
        raise typer.BadParameter(f"--index must be between 1 and {len(raw_jobs)}")
    job = _job_from_dict(raw_jobs[index - 1])
    _prepare_application_package(
        job,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        form_snapshot=form_snapshot,
        profile=profile,
        resume=resume,
        upload_resume=upload_resume,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
    )
    typer.echo(f"Prepared application package at {out_dir}")


@applications_app.command("prepare-shortlist")
def prepare_shortlisted_applications(
    jobs_file: Path,
    out_dir: Path = typer.Option(Path("application-batch"), "--out-dir", help="Batch application package output directory."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to prepare."),
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
    resume: Optional[Path] = typer.Option(
        None,
        "--resume",
        help="Optional base resume text/markdown file for tailored resume draft generation.",
    ),
    upload_resume: bool = typer.Option(
        False,
        "--upload-resume",
        help="When --resume and form data are provided, wire tailored-resume.md into Resume/CV upload fields.",
    ),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    if limit is not None and limit < 1:
        raise typer.BadParameter("--limit must be greater than 0")
    raw_jobs = json.loads(jobs_file.read_text())
    selected_raw_jobs = raw_jobs[:limit] if limit is not None else raw_jobs
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for index, raw_job in enumerate(selected_raw_jobs, start=1):
        job = _job_from_dict(raw_job)
        package_dir = out_dir / _review_slug(index, job)
        summary = _prepare_application_package(
            job,
            package_dir,
            resume_source_dir=resume_source_dir,
            db=db,
            form_snapshot=form_snapshot,
            profile=profile,
            resume=resume,
            upload_resume=upload_resume,
            use_llm=use_llm,
            llm_model=llm_model,
            llm_provider=llm_provider,
            llm_base_url=llm_base_url,
        )
        summary["index"] = str(index)
        summaries.append(summary)

    summary_path = out_dir / "batch-summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=True))
    typer.echo(f"Prepared {len(summaries)} application packages at {out_dir}")


@applications_app.command("build-batch-runner")
def build_batch_runner(
    summary: Path,
    out: Path = typer.Option(Path("run-application-batch.js"), "--out", help="JavaScript runner output path."),
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_batch_fill_runner(json.loads(summary.read_text())))
    typer.echo(f"Wrote guarded batch runner to {out}")


@forms_app.command("build-snapshot-script")
def build_form_snapshot_script(
    out: Path = typer.Option(Path("capture-form-snapshot.js"), "--out", help="JavaScript output path."),
    application_url: Optional[str] = typer.Option(
        None,
        "--application-url",
        help="Optional application page URL to open before inspecting fields.",
    ),
    snapshot_out: str = typer.Option(
        "form-snapshot.json",
        "--snapshot-out",
        help="JSON file path where the captured form snapshot should be written by the generated script.",
    ),
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_playwright_form_snapshot_script(
            application_url=application_url,
            output_path=snapshot_out,
        )
    )
    typer.echo(f"Wrote guarded form snapshot script to {out}")


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
    resume_file: Optional[Path] = typer.Option(
        None,
        "--resume-file",
        help="Optional approved resume file path for Resume/CV upload fields.",
    ),
) -> None:
    profile_facts = json.loads(profile.read_text())
    if resume_file:
        profile_facts["resume_file"] = str(resume_file)
    plan = build_form_fill_plan(
        inspect_form_snapshot(form_snapshot.read_text()),
        profile_facts,
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
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    form_snapshot_json = form_snapshot.read_text() if form_snapshot else None
    profile_json = profile.read_text() if profile else None
    agent = JobApplicationAgent(
        name="job-application-agent",
        llm=_build_llm(
            use_llm=use_llm,
            model=llm_model,
            provider=llm_provider,
            base_url=llm_base_url,
        ),
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


@jobs_app.command("import-sources")
def import_configured_sources(
    config_file: Path,
    out: Path = typer.Option(Path("jobs.json"), "--out", help="JSON output path."),
) -> None:
    jobs = load_jobs_from_source_config(config_file)
    _write_jobs_json(jobs, out)
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


@jobs_app.command("review-sources")
def review_configured_sources(
    config_file: Path,
    out_dir: Path = typer.Option(Path("reviews"), "--out-dir", help="Directory for markdown review packets."),
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
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    jobs = load_jobs_from_source_config(config_file)
    _write_review_packets(
        jobs,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        package_dir=package_dir,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
    )
    typer.echo(f"Reviewed {len(jobs)} jobs into {out_dir}")


@jobs_app.command("shortlist")
def shortlist_job_pool(
    jobs_file: Path,
    out: Path = typer.Option(Path("shortlist.json"), "--out", help="JSON output path."),
    min_score: int = typer.Option(70, "--min-score", help="Minimum fit score to keep."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to keep."),
) -> None:
    raw_jobs = json.loads(jobs_file.read_text())
    jobs = [_job_from_dict(raw) for raw in raw_jobs]
    shortlisted = shortlist_jobs(jobs, min_score=min_score, limit=limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(shortlisted_jobs_to_dicts(shortlisted), indent=2, ensure_ascii=True))
    typer.echo(f"Shortlisted {len(shortlisted)} jobs to {out}")


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


@jobs_app.command("review-greenhouse")
def review_greenhouse_jobs(
    board_token: str,
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Greenhouse JSON payload. If omitted, fetches the public API.",
    ),
    out_dir: Path = typer.Option(Path("reviews"), "--out-dir", help="Directory for markdown review packets."),
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
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    jobs = parse_greenhouse_jobs(_read_json_source(payload, url), board_token=board_token, limit=limit)
    _write_review_packets(
        jobs,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        package_dir=package_dir,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
    )
    typer.echo(f"Reviewed {len(jobs)} jobs into {out_dir}")


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


@jobs_app.command("review-lever")
def review_lever_jobs(
    site: str,
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Lever JSON payload. If omitted, fetches the public API.",
    ),
    out_dir: Path = typer.Option(Path("reviews"), "--out-dir", help="Directory for markdown review packets."),
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
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    url = f"https://api.lever.co/v0/postings/{site}?mode=json"
    jobs = parse_lever_jobs(_read_json_source(payload, url), site=site, limit=limit)
    _write_review_packets(
        jobs,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        package_dir=package_dir,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
    )
    typer.echo(f"Reviewed {len(jobs)} jobs into {out_dir}")


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


@jobs_app.command("review-remotive")
def review_remotive_jobs(
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Remotive JSON payload. If omitted, fetches the public API.",
    ),
    out_dir: Path = typer.Option(Path("reviews"), "--out-dir", help="Directory for markdown review packets."),
    search: Optional[str] = typer.Option(None, "--search", help="Optional Remotive search query."),
    category: Optional[str] = typer.Option(None, "--category", help="Optional Remotive category or slug."),
    company_name: Optional[str] = typer.Option(None, "--company-name", help="Optional company-name filter."),
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
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
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
    _write_review_packets(
        jobs,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        package_dir=package_dir,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
    )
    typer.echo(f"Reviewed {len(jobs)} jobs into {out_dir}")


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
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    jobs = parse_rss_jobs(rss_file.read_text(), source=source, limit=limit)
    _write_review_packets(
        jobs,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        package_dir=package_dir,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
    )
    typer.echo(f"Reviewed {len(jobs)} jobs into {out_dir}")


app.add_typer(applications_app, name="applications")
app.add_typer(jobs_app, name="jobs")
app.add_typer(forms_app, name="forms")
app.add_typer(llm_app, name="llm")
app.add_typer(resumes_app, name="resumes")
