from __future__ import annotations

import hashlib
from contextlib import contextmanager
from importlib import resources
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

# Some public job APIs (e.g. Remotive) reject the default Python-urllib
# User-Agent with HTTP 403. A browser-like UA keeps the agent's autonomous
# source fetching working against compliant public endpoints.
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

import typer

from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.career.recovery import attach_recovery_plan
from job_agent.db import (
    application_dedupe_key,
    connect,
    create_application,
    create_job,
    init_db,
    update_application_resume_evidence,
    update_application_execution_status,
)
from job_agent.document_export import (
    convert_docx_to_pdf,
    markdown_to_docx_bytes,
)
from job_agent.execution import SUBMIT_GATE, execute_application_batch, summarize_execution
from job_agent.config import AppConfig, load_env
from job_agent.application_answers import enrich_profile_for_job
from job_agent.candidate_screening import screen_job_for_candidate
from job_agent.forms import (
    build_form_fill_plan,
    inspect_form_snapshot,
    render_playwright_fill_script,
    render_playwright_form_snapshot_script,
)
from job_agent.gmail_verification import GmailVerificationError, authorize_gmail
from job_agent.jobs import (
    canonical_job_url,
    format_job_as_jd_text,
    jobs_to_dicts,
    parse_ashby_jobs,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_remotive_jobs,
    parse_rss_jobs,
)
from job_agent.jd_analysis import parse_jd
from job_agent.models import Job
from job_agent.resumes import (
    ResumePathError,
    extract_resume_text,
    index_resume_templates,
    resolve_original_resume_pdf,
    select_best_resume_template,
)
from job_agent.runners import render_batch_fill_runner
from job_agent.profile import parse_resume_to_profile, render_profile_template
from job_agent.profile_vector_store import (
    export_profile_chunks,
    index_profile_embeddings,
    search_profile_embeddings,
    sync_profile_summary_documents,
)
from job_agent.python_runtime import _detect_submission_confirmation, load_runtime_payload
from job_agent.runtime_filler import render_runtime_autofill_script
from job_agent.scoring import ROLE_KEYWORDS, classify_role
from job_agent.sensitive_kb import load_sensitive_kb, render_sensitive_kb_template
from job_agent.shortlist import shortlisted_jobs_to_dicts, shortlist_jobs
from job_agent.source_config import load_jobs_from_source_config
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.core.contracts import ToolCall, ToolEffect
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.memory import NullLongTermMemory, ShortTermMemory
from hello_agents.core.perception import StructuredPerception
from hello_agents.core.runtime import AgentCore
from hello_agents.core.trace import agent_loop_result_to_dict
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.registry import ToolRegistry
from job_agent.memory import SQLiteApplicationMemory

app = typer.Typer(help="Personal job application agent.")
applications_app = typer.Typer(help="End-to-end application preparation commands.")
examples_app = typer.Typer(help="Export packaged offline examples and smoke-test fixtures.")
forms_app = typer.Typer(help="Application form automation commands.")
jobs_app = typer.Typer(help="Job intake and review commands.")
llm_app = typer.Typer(help="LLM configuration and connectivity commands.")
pipeline_app = typer.Typer(help="Auditable end-to-end job application workflows.")
profiles_app = typer.Typer(help="Private profile store and embeddings commands.")
resumes_app = typer.Typer(help="Resume template commands.")
inbox_app = typer.Typer(help="Email verification providers and authorization.")

EXAMPLE_FILENAMES = [
    "offline-sources.json",
    "offline-jobs.xml",
    "sample-resume.md",
    "profile.json",
    "form-snapshot.json",
    "sensitive-answers.json",
]

_FAKE_RUNTIME_PLAYWRIGHT = """
const values = {};
let submitted = false;

function locator(selector) {
  const isSubmit = selector.includes("submit");
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async inputValue() { return values[selector] || ''; },
    async selectOption(option) { values[selector] = option.label || option.value || ''; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async isChecked() { return Boolean(values[selector]); },
    async click() { if (isSubmit) submitted = true; },
    async screenshot() {},
  };
}

const page = {
  async goto(url) { console.log('fake goto ' + url); },
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async screenshot() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: 'name', name: '', required: true, options: [], value: values.name || '' },
        { kind: 'single', tag: 'input', type: 'email', label: 'Email', id: 'email', name: '', required: true, options: [], value: values.email || '' },
      ];
    }
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    if (body.includes('window.location.href') && body.includes('document.body') && body.includes('innerText')) {
      return {
        url: submitted ? 'https://jobs.example.com/thanks' : 'https://jobs.example.com/apply',
        title: submitted ? 'Thank you for applying' : 'Application form',
        text: submitted ? 'Thank you for applying. Your application has been submitted.' : 'Application form',
        recaptcha: false,
      };
    }
    if (body.includes('h1,h2,h3,h4,legend')) return submitted;
    return null;
  },
};

module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake launch headless=' + options.headless);
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
      };
    },
  },
};
"""


def _render_source_config_template() -> dict[str, object]:
    return {
        "notes": [
            "Replace the starter source entries before running live imports.",
            "Keep only compliant public job sources such as Greenhouse, Lever, Remotive, or company RSS/Atom feeds.",
            "For offline debugging, replace a live fetch with payload_file pointing at a saved JSON response.",
        ],
        "sources": [
            {
                "type": "greenhouse",
                "board_token": "company-board-token",
                "limit": 20,
            },
            {
                "type": "lever",
                "site": "company-site-slug",
                "limit": 20,
            },
            {
                "type": "remotive",
                "search": "agent engineer",
                "limit": 20,
            },
            {
                "type": "rss",
                "source": "company-rss",
                "rss_url": "https://company.example/jobs.xml",
                "limit": 20,
            },
        ],
    }


TRACK_ALIASES = {
    "agent": "Agent Engineer",
    "agent engineer": "Agent Engineer",
    "agentic": "Agent Engineer",
    "ml infra": "ML Infra",
    "ml infrastructure": "ML Infra",
    "mle": "MLE",
    "machine learning engineer": "MLE",
    "sde": "SDE",
    "software engineer": "SDE",
    "data scientist": "Data Scientist",
    "ai algorithm engineer": "AI Algorithm Engineer",
    "unity ml infrastructure": "Unity ML Infrastructure",
}

TRACK_STARTER_SEARCHES = {
    "Agent Engineer": ["agent engineer", "llm engineer", "ai agent engineer"],
    "ML Infra": ["ml infrastructure engineer", "ml platform engineer", "machine learning infrastructure engineer"],
    "MLE": ["machine learning engineer", "applied machine learning engineer", "ai engineer"],
    "SDE": ["backend engineer", "software engineer", "platform engineer"],
    "Data Scientist": ["data scientist", "applied scientist", "machine learning scientist"],
    "AI Algorithm Engineer": ["algorithm engineer", "ai algorithm engineer", "llm evaluation engineer"],
    "Unity ML Infrastructure": ["unity machine learning", "simulation engineer", "reinforcement learning infrastructure"],
}


def _normalize_job_track(job_track: str | None) -> str:
    if not job_track:
        return "Agent Engineer"
    raw = " ".join(str(job_track).strip().lower().replace("_", " ").split())
    if raw in TRACK_ALIASES:
        return TRACK_ALIASES[raw]
    for track in ROLE_KEYWORDS:
        if raw == track.lower():
            return track
    raise typer.BadParameter(
        "Unsupported --job-track. Choose one of: "
        + ", ".join(sorted(ROLE_KEYWORDS))
    )


def _render_source_config_template_for_track(job_track: str) -> dict[str, object]:
    searches = TRACK_STARTER_SEARCHES.get(job_track, [job_track.lower()])
    keywords = ROLE_KEYWORDS.get(job_track, [])
    return {
        "target_track": job_track,
        "notes": [
            f"Starter template for {job_track}. Replace the placeholder company board tokens before running live imports.",
            "Keep only compliant public job sources such as Greenhouse, Lever, Remotive, or company RSS/Atom feeds.",
            f"Suggested JD keywords for this track: {', '.join(keywords) if keywords else 'customize manually'}.",
        ],
        "sources": [
            {
                "type": "greenhouse",
                "board_token": "company-board-token",
                "limit": 20,
            },
            {
                "type": "lever",
                "site": "company-site-slug",
                "limit": 20,
            },
            {
                "type": "remotive",
                "search": searches[0],
                "limit": 20,
            },
            *[
                {
                    "type": "remotive",
                    "search": search,
                    "limit": 20,
                }
                for search in searches[1:]
            ],
            {
                "type": "rss",
                "source": f"{job_track.lower().replace(' ', '-')}-company-rss",
                "rss_url": "https://company.example/jobs.xml",
                "limit": 20,
            },
        ],
    }


def _render_workspace_readme(
    workspace_dir: Path,
    profile_path: Path,
    sensitive_kb_path: Path,
    sources_path: Path,
    *,
    job_track: str,
) -> str:
    rel = lambda path: path.relative_to(workspace_dir)
    searches = TRACK_STARTER_SEARCHES.get(job_track, [job_track.lower()])
    return (
        "# Job Agent Workspace\n\n"
        f"Target track: `{job_track}`\n\n"
        "1. Put your role-specific resume files into `resumes/`.\n"
        "2. Review and edit the generated profile and sensitive-answer files.\n"
        "3. Replace the starter job sources before running live imports.\n"
        "4. Run the pipeline command shown below.\n\n"
        "## Files\n\n"
        f"- Profile: `{rel(profile_path)}`\n"
        f"- Sensitive answers: `{rel(sensitive_kb_path)}`\n"
        f"- Sources: `{rel(sources_path)}`\n"
        "- Resume directory: `resumes/`\n"
        "- Output directory: `output/`\n\n"
        "## Starter search focus\n\n"
        f"- Remotive searches: `{', '.join(searches)}`\n"
        f"- Suggested JD keywords: `{', '.join(ROLE_KEYWORDS.get(job_track, []))}`\n\n"
        "## Recommended commands\n\n"
        "```bash\n"
        "job-agent resumes index resumes\n\n"
        "job-agent pipeline run-execute sources.json \\\n"
        "  --out-dir output/pipeline-run \\\n"
        "  --required-resume-pdf resumes/path-to-selected-resume.pdf \\\n"
        f"  --profile {rel(profile_path)} \\\n"
        f"  --sensitive-kb {rel(sensitive_kb_path)} \\\n"
        "  --db output/job-agent.db \\\n"
        "  --min-score 70 \\\n"
        "  --limit 5 \\\n"
        "  --timeout-seconds 300\n"
        "```\n\n"
        "Use `--resume-source-dir resumes` to select the closest original PDF by role, or\n"
        "`--required-resume-pdf path/to/resume.pdf` to force one exact existing PDF path.\n"
    )


@app.callback()
def _load_env_before_commands() -> None:
    """Load a local .env (gitignored) before any command runs, so
    OPENAI_API_KEY / LLM_* take effect for `--use-llm` without exporting
    shell variables. Runs on CLI invocation only, not at import."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    load_env()


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


def _read_resume_text(path: Path) -> str:
    extracted = extract_resume_text(path)
    if extracted is not None:
        return extracted
    return path.read_text()


def _runtime_browser_headless() -> bool:
    return AppConfig.from_env().browser_headless


@contextmanager
def _temporary_llm_answers_env(enabled: bool | None):
    """Temporarily force the guarded runtime LLM-answer switch for one command."""
    if enabled is None:
        yield
        return
    previous = os.environ.get("JOB_AGENT_LLM_ANSWERS")
    os.environ["JOB_AGENT_LLM_ANSWERS"] = "1" if enabled else "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("JOB_AGENT_LLM_ANSWERS", None)
        else:
            os.environ["JOB_AGENT_LLM_ANSWERS"] = previous


def _load_profile_facts(profile: Optional[Path], sensitive_kb: Optional[Path] = None) -> dict | None:
    if not profile:
        return None
    profile_facts = json.loads(profile.read_text())
    email_override = str(os.getenv("JOB_AGENT_PROFILE_EMAIL_OVERRIDE") or "").strip()
    if email_override:
        if "@" not in email_override or email_override.startswith("@") or email_override.endswith("@"):
            raise typer.BadParameter("JOB_AGENT_PROFILE_EMAIL_OVERRIDE must be a valid email-like value")
        profile_facts["email"] = email_override
        contact = profile_facts.get("contact")
        if isinstance(contact, dict):
            contact["email"] = email_override
    if sensitive_kb:
        profile_facts["sensitive_answers"] = load_sensitive_kb(sensitive_kb)
    return profile_facts


def _render_upload_pdf(docx_path: Path) -> Path | None:
    pdf_path = docx_path.with_suffix(".pdf")
    if convert_docx_to_pdf(docx_path, pdf_path):
        return pdf_path
    return None


def _required_resume_pdf_path(required_resume_pdf: Path | None = None) -> Path | None:
    if required_resume_pdf is not None:
        return required_resume_pdf.expanduser()
    raw = str(os.getenv("JOB_AGENT_REQUIRED_RESUME_PDF") or "").strip()
    return Path(raw).expanduser() if raw else None


def _required_resume_pdf_error(
    resume_path: Path, required_resume_pdf: Path | None = None
) -> str | None:
    required = _required_resume_pdf_path(required_resume_pdf)
    if required is None:
        return None
    try:
        resolve_original_resume_pdf(resume_path, required_pdf=required)
    except ResumePathError as exc:
        return str(exc)
    return None


def _validate_required_resume_pdf(required_resume_pdf: Path | None = None) -> Path | None:
    required = _required_resume_pdf_path(required_resume_pdf)
    if required is None:
        return None
    required_error = _required_resume_pdf_error(required, required)
    if required_error:
        raise typer.BadParameter(required_error)
    return required


def _effective_required_resume_pdf(
    required_resume_pdf: Path | None = None,
    *,
    resume: Path | None = None,
    required_resume_source_dir: Path | None = None,
    package_dir: Path | None = None,
) -> Path | None:
    """Return the concrete PDF path that must be uploaded for this run.

    An explicit --required-resume-pdf or JOB_AGENT_REQUIRED_RESUME_PDF remains
    the strongest lock. If the caller instead passes --resume, treat that exact
    existing PDF as the required upload so downstream preflight and execution
    audits record the user's chosen path and hash.
    """
    required = _validate_required_resume_pdf(required_resume_pdf)
    if required is not None or resume is None:
        return required
    try:
        return resolve_original_resume_pdf(
            resume,
            source_dir=required_resume_source_dir,
            package_dir=package_dir,
        )
    except ResumePathError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _validate_required_resume_source_dir(required_resume_source_dir: Path | None = None) -> Path | None:
    if required_resume_source_dir is None:
        return None
    source_dir = required_resume_source_dir.expanduser()
    if not source_dir.is_dir():
        raise typer.BadParameter(f"required resume source dir does not exist: {source_dir}")
    return source_dir.resolve()


def _configured_resume_source_dir() -> Path | None:
    raw = str(os.getenv("RESUME_SOURCE_DIR") or "").strip()
    if not raw:
        return None
    source_dir = Path(raw).expanduser()
    if not source_dir.is_dir():
        raise typer.BadParameter(f"RESUME_SOURCE_DIR does not exist: {source_dir}")
    return source_dir


def _effective_resume_source_dir(
    resume_source_dir: Path | None,
    *,
    resume: Path | None = None,
    required_resume_pdf: Path | None = None,
) -> Path | None:
    """Return the hard source directory boundary for uploadable resume PDFs.

    Explicit PDF paths only choose or lock a candidate file; they must not
    disable the configured source directory guard.
    """
    if resume_source_dir is not None:
        return _validate_required_resume_source_dir(resume_source_dir)
    return _configured_resume_source_dir()


def _required_resume_source_dir_error(
    resume_path: Path,
    required_resume_source_dir: Path | None = None,
) -> str | None:
    source_dir = _validate_required_resume_source_dir(required_resume_source_dir)
    if source_dir is None:
        return None
    try:
        resolve_original_resume_pdf(resume_path, source_dir=source_dir)
    except ResumePathError as exc:
        return str(exc)
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resume_pdf_audit_fields(path: Path | None, prefix: str) -> dict[str, object | None]:
    if path is None:
        return {
            f"{prefix}_path": None,
            f"{prefix}_resolved_path": None,
            f"{prefix}_exists": False,
            f"{prefix}_size_bytes": None,
            f"{prefix}_sha256": None,
        }
    expanded = path.expanduser()
    try:
        resolved = expanded.resolve()
    except OSError:
        resolved = None
    fields: dict[str, object | None] = {
        f"{prefix}_path": str(expanded),
        f"{prefix}_resolved_path": str(resolved) if resolved is not None else None,
        f"{prefix}_exists": expanded.is_file(),
        f"{prefix}_size_bytes": None,
        f"{prefix}_sha256": None,
    }
    if expanded.is_file():
        fields[f"{prefix}_size_bytes"] = expanded.stat().st_size
        fields[f"{prefix}_sha256"] = _sha256_file(expanded)
    return fields


def _selected_resume_upload_path(
    resume: Path | None,
    out_dir: Path,
    required_resume_pdf: Path | None = None,
    required_resume_source_dir: Path | None = None,
) -> Path | None:
    if not resume:
        return None
    try:
        return resolve_original_resume_pdf(
            resume,
            source_dir=required_resume_source_dir,
            package_dir=out_dir,
            required_pdf=required_resume_pdf,
        )
    except ResumePathError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _missing_resume_source_error() -> str:
    return (
        "Refusing to prepare executable application without an original PDF resume. "
        "Pass --resume-source-dir/RESUME_SOURCE_DIR to select the most relevant source PDF, "
        "or pass --resume/--required-resume-pdf for an exact existing PDF that still satisfies "
        "the configured source-dir guard."
    )


def _render_cover_letter_markdown(job: Job, profile: dict | None) -> str:
    profile = profile or {}
    answers = profile.get("answers") or {}
    company = job.company or "your team"
    title = job.title or "this role"
    name = profile.get("name") or "Gaoyi Wu"
    motivation = (
        answers.get(f"Why {company}?")
        or answers.get(f"Why do you want to work at {company}?")
        or answers.get("Why are you interested in this role?")
        or (
            f"I am interested in {company} because the {title} role aligns with my "
            "background building practical software and AI/ML systems for real users."
        )
    )
    current = next(
        (item for item in profile.get("work_history") or [] if isinstance(item, dict) and item.get("current")),
        None,
    )
    prior = next(
        (
            item
            for item in profile.get("work_history") or []
            if isinstance(item, dict) and not item.get("current")
        ),
        None,
    )
    project = next(
        (
            item
            for item in profile.get("projects") or []
            if isinstance(item, dict) and item.get("title")
        ),
        None,
    )
    evidence = []
    if current:
        current_title = str(current.get("title") or "my current role").strip()
        evidence.append(
            f"At {current.get('company')}, as a {current_title}, I have worked on projects "
            "including LLM/RAG evaluation, model training workflows, and production-minded ML infrastructure."
        )
    if prior:
        evidence.append(
            f"At {prior.get('company')}, I built machine learning and data workflows involving "
            "model development, deployment, CI/CD, and analytics."
        )
    if project:
        evidence.append(
            f"My project experience includes {project.get('title')}, which reflects my interest in building "
            "usable engineering systems and learning quickly across a stack."
        )
    if not evidence:
        skills = ", ".join(str(item) for item in (profile.get("skills") or [])[:8])
        evidence.append(f"My technical background includes {skills}.")
    return "\n\n".join(
        [
            f"# Cover Letter - {company} {title}",
            f"Dear {company} Hiring Team,",
            str(motivation).strip(),
            "\n\n".join(evidence),
            (
                "I would be glad to bring this combination of software engineering, applied ML, "
                "and product-minded execution to the team. Thank you for your consideration."
            ),
            f"Sincerely,\n{name}",
        ]
    )


def _render_cover_letter_pdf(job: Job, profile: dict | None, out_dir: Path) -> tuple[Path, Path | None]:
    markdown_path = out_dir / "cover-letter.md"
    docx_path = out_dir / "cover-letter.docx"
    markdown_text = _render_cover_letter_markdown(job, profile)
    markdown_path.write_text(markdown_text)
    docx_path.write_bytes(markdown_to_docx_bytes(markdown_text))
    return markdown_path, _render_upload_pdf(docx_path) or docx_path


def _review_slug(index: int, job: Job) -> str:
    raw = f"{index:03d}-{job.company}-{job.title}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or f"{index:03d}-job"


def _read_json_source(payload: Optional[Path], url: str):
    if payload:
        return json.loads(payload.read_text())
    request = Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _export_example_fixtures(out_dir: Path, force: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    resource_root = resources.files("job_agent.example_data")
    for filename in EXAMPLE_FILENAMES:
        target = out_dir / filename
        text = resource_root.joinpath(filename).read_text()
        if target.exists() and target.read_text() != text and not force:
            raise typer.BadParameter(f"{target} exists; pass --force to overwrite it")
        target.write_text(text)


def _write_fake_runtime_playwright(package_dir: Path) -> Path:
    playwright_dir = package_dir / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True, exist_ok=True)
    target = playwright_dir / "index.js"
    target.write_text(_FAKE_RUNTIME_PLAYWRIGHT)
    return target


def _write_jobs_json(jobs: list[Job], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True))


def _normalized_application_url(value: str | None) -> str:
    return canonical_job_url(value) or ""


def _application_host(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (parsed.netloc or "").split("@")[-1].split(":")[0].lower()


def _anti_spam_host_key(value: str | None) -> str:
    """Compute a grouping key for anti-spam host blocking.

    Shared ATS hosts include the company or board identifier so that a block
    for one tenant does not suppress candidates from every tenant.
    """
    host = _application_host(value)
    if host not in (
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "jobs.ashbyhq.com",
    ):
        return host
    raw = str(value or "").strip()
    path = urlparse(raw if "://" in raw else f"https://{raw}").path
    segments = [s for s in path.split("/") if s]
    if host == "job-boards.greenhouse.io":
        if len(segments) >= 2 and segments[0] not in ("jobs", "v1"):
            return f"job-boards.greenhouse.io/{segments[0]}"
    elif host in ("jobs.lever.co", "jobs.eu.lever.co"):
        if segments:
            return f"{host}/{segments[0]}"
    elif host == "jobs.ashbyhq.com":
        if segments:
            return f"jobs.ashbyhq.com/{segments[0]}"
    return host


def _anti_spam_cooldown_hours() -> int:
    try:
        value = int(os.getenv("JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS") or "24")
    except ValueError:
        value = 24
    return max(1, value)


def _anti_spam_host_cooldown_threshold() -> int:
    try:
        value = int(os.getenv("JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD") or "5")
    except ValueError:
        value = 5
    return max(1, value)


def _failure_circuit_breaker_hours() -> int:
    try:
        value = int(os.getenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS") or "6")
    except ValueError:
        value = 6
    return max(1, value)


def _failure_circuit_breaker_threshold() -> int:
    try:
        value = int(os.getenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD") or "2")
    except ValueError:
        value = 2
    return max(2, value)


ORDINARY_FAILURE_STATUSES = {
    "application_form_unavailable",
    "autofill_completed_blocked",
    "autofill_failed",
    "autofill_timed_out",
    "submission_processing_error",
}

FAILURE_CIRCUIT_OUTCOME_STATUSES = ORDINARY_FAILURE_STATUSES | {
    "autofill_completed_blocked",
    "candidate_account_required",
    "email_verification_required",
    "submission_blocked_by_anti_spam",
    "submit_clicked_unconfirmed",
    "submitted",
}


def _failure_adapter_key(value: str | None, company: str | None) -> str:
    host_key = _anti_spam_host_key(value)
    if not host_key:
        return ""
    host = host_key.split("/", 1)[0]
    if host in {
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "jobs.ashbyhq.com",
    } and "/" in host_key:
        return host_key
    company_key = " ".join(str(company or "").casefold().split())
    return f"{host_key}/{company_key}" if company_key else host_key


def _failure_circuit_breakers(
    db: Path | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return company and adapter scopes with repeated equivalent failures."""
    if db is None or not db.is_file():
        return {}, {}
    conn = connect(db)
    init_db(conn)
    cutoff = f"-{_failure_circuit_breaker_hours()} hours"
    rows = conn.execute(
        f"""
        select id, company, apply_url, status
        from applications
        where status in ({",".join("?" for _ in FAILURE_CIRCUIT_OUTCOME_STATUSES)})
          and datetime(updated_at) >= datetime('now', ?)
        order by datetime(updated_at) desc, id desc
        """,
        (*sorted(FAILURE_CIRCUIT_OUTCOME_STATUSES), cutoff),
    ).fetchall()
    conn.close()

    grouped_companies: dict[str, list[str]] = {}
    grouped_adapters: dict[str, list[str]] = {}
    for row in rows:
        status = str(row["status"] or "")
        company_key = " ".join(str(row["company"] or "").casefold().split())
        if company_key:
            grouped_companies.setdefault(company_key, []).append(status)
        adapter_key = _failure_adapter_key(row["apply_url"], row["company"])
        if adapter_key:
            grouped_adapters.setdefault(adapter_key, []).append(status)

    threshold = _failure_circuit_breaker_threshold()

    def open_scopes(grouped: dict[str, list[str]]) -> dict[str, str]:
        opened: dict[str, str] = {}
        for key, statuses in grouped.items():
            latest = statuses[0] if statuses else ""
            if latest not in ORDINARY_FAILURE_STATUSES:
                continue
            consecutive = 0
            for status in statuses:
                if status != latest:
                    break
                consecutive += 1
            if consecutive >= threshold:
                opened[key] = latest
        return opened

    return open_scopes(grouped_companies), open_scopes(grouped_adapters)


BLOCKING_RETRY_STATUSES = {
    "autofill_timed_out",
    "submit_clicked_unconfirmed",
    "submission_blocked_by_anti_spam",
    "submission_processing_error",
    "autofill_completed_blocked",
    "autofill_failed",
    "email_verification_required",
    "candidate_account_required",
}


def _submitted_application_keys(db: Path | None) -> tuple[set[str], set[tuple[str, str]]]:
    """Load prior verified submissions so a new pipeline cannot resubmit them."""
    if db is None or not db.is_file():
        return set(), set()
    conn = connect(db)
    init_db(conn)
    rows = conn.execute(
        """
        select company, title, apply_url
        from applications
        where status = 'submitted'
        """
    ).fetchall()
    conn.close()
    urls = {_normalized_application_url(row["apply_url"]) for row in rows if row["apply_url"]}
    titles = {
        (str(row["company"] or "").strip().lower(), str(row["title"] or "").strip().lower())
        for row in rows
    }
    return urls, titles


def _terminal_application_outcomes(db: Path | None) -> dict[str, str]:
    """Load prior unconfirmed/blocked outcomes that should not be retried blindly."""
    if db is None or not db.is_file():
        return {}
    conn = connect(db)
    init_db(conn)
    rows = conn.execute(
        f"""
        select apply_url, status
        from applications
        where apply_url is not null
          and status in ({",".join("?" for _ in BLOCKING_RETRY_STATUSES)})
        """,
        tuple(sorted(BLOCKING_RETRY_STATUSES)),
    ).fetchall()
    conn.close()
    return {
        _normalized_application_url(row["apply_url"]): str(row["status"])
        for row in rows
        if row["apply_url"]
    }


def _anti_spam_blocked_companies(db: Path | None) -> set[str]:
    """Load companies whose latest recent outcome is an anti-spam blocker."""
    if db is None or not db.is_file():
        return set()
    conn = connect(db)
    init_db(conn)
    cutoff = f"-{_anti_spam_cooldown_hours()} hours"
    rows = conn.execute(
        """
        select id, lower(trim(company)) as company_key, status
        from applications
        where status in ('submission_blocked_by_anti_spam', 'submitted')
          and coalesce(trim(company), '') != ''
          and datetime(updated_at) >= datetime('now', ?)
        order by lower(trim(company)), datetime(updated_at) desc, id desc
        """,
        (cutoff,),
    ).fetchall()
    conn.close()
    latest_statuses: dict[str, str] = {}
    for row in rows:
        company_key = str(row["company_key"] or "").strip().lower()
        if company_key and company_key not in latest_statuses:
            latest_statuses[company_key] = str(row["status"] or "")
    return {
        company_key
        for company_key, status in latest_statuses.items()
        if status == "submission_blocked_by_anti_spam"
    }


def _anti_spam_blocked_hosts(db: Path | None) -> set[str]:
    """Load hosts with repeated unresolved anti-spam blockers across companies."""
    if db is None or not db.is_file():
        return set()
    conn = connect(db)
    init_db(conn)
    cutoff = f"-{_anti_spam_cooldown_hours()} hours"
    rows = conn.execute(
        """
        select apply_url, status
        from applications
        where status in ('submission_blocked_by_anti_spam', 'submitted')
          and apply_url is not null
          and datetime(updated_at) >= datetime('now', ?)
        """,
        (cutoff,),
    ).fetchall()
    conn.close()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        host = _anti_spam_host_key(row["apply_url"])
        if host:
            host_counts = counts.setdefault(host, {"blocked": 0, "submitted": 0})
            if row["status"] == "submission_blocked_by_anti_spam":
                host_counts["blocked"] += 1
            elif row["status"] == "submitted":
                host_counts["submitted"] += 1
    threshold = _anti_spam_host_cooldown_threshold()
    return {
        host
        for host, count in counts.items()
        if max(0, count["blocked"] - count["submitted"]) >= threshold
    }


def _was_previously_submitted(
    job: Job,
    submitted_urls: set[str],
    submitted_titles: set[tuple[str, str]],
) -> bool:
    application_url = _normalized_application_url(job.apply_url or job.source_url)
    if application_url and application_url in submitted_urls:
        return True
    return (job.company.strip().lower(), job.title.strip().lower()) in submitted_titles


def _previous_submission_reason(
    *,
    company: str,
    title: str,
    apply_url: str | None,
    submitted_urls: set[str],
    submitted_titles: set[tuple[str, str]],
) -> str | None:
    application_url = _normalized_application_url(apply_url)
    if application_url and application_url in submitted_urls:
        return f"matching submitted apply_url already exists: {apply_url}"
    if not application_url and (company.strip().lower(), title.strip().lower()) in submitted_titles:
        return f"matching submitted company/title already exists: {company} - {title}"
    return None


def _previous_submission_reason_for_job(job: Job, db: Path | None) -> str | None:
    submitted_urls, submitted_titles = _submitted_application_keys(db)
    return _previous_submission_reason(
        company=job.company,
        title=job.title,
        apply_url=job.apply_url or job.source_url,
        submitted_urls=submitted_urls,
        submitted_titles=submitted_titles,
    )


def _previous_submission_reason_for_summary(summary: dict[str, object], db: Path | None) -> str | None:
    submitted_urls, submitted_titles = _submitted_application_keys(db)
    return _previous_submission_reason(
        company=str(summary.get("company") or ""),
        title=str(summary.get("title") or ""),
        apply_url=_summary_application_url(summary),
        submitted_urls=submitted_urls,
        submitted_titles=submitted_titles,
    )


def _previous_terminal_outcome_reason_for_job(job: Job, db: Path | None) -> str | None:
    outcomes = _terminal_application_outcomes(db)
    application_url = _normalized_application_url(job.apply_url or job.source_url)
    status = outcomes.get(application_url)
    if status:
        return f"matching prior terminal outcome {status} already exists: {job.apply_url or job.source_url}"
    return None


def _previous_terminal_outcome_reason_for_summary(summary: dict[str, object], db: Path | None) -> str | None:
    outcomes = _terminal_application_outcomes(db)
    application_url = _normalized_application_url(_summary_application_url(summary))
    status = outcomes.get(application_url)
    if status:
        return f"matching prior terminal outcome {status} already exists: {_summary_application_url(summary)}"
    return None


def _execution_resume_upload_error(
    item: dict[str, object],
    required_resume_pdf: Path | None = None,
    required_resume_source_dir: Path | None = None,
) -> str | None:
    """Return a blocking reason when execution would not upload an existing PDF."""
    raw_path = item.get("upload_resume_path")
    runtime_resume = _runtime_resume_upload_path(item)
    effective_required_resume = required_resume_pdf or _summary_required_resume_pdf(item)
    effective_required_source_dir = required_resume_source_dir or _summary_required_resume_source_dir(item)
    if raw_path and runtime_resume is not None:
        summary_resume = Path(str(raw_path)).expanduser()
        try:
            if summary_resume.resolve() != runtime_resume.resolve():
                return (
                    "runtime resumeFile does not match summary upload_resume_path: "
                    f"{runtime_resume}; expected: {summary_resume}"
                )
        except OSError:
            return (
                "runtime resumeFile does not match summary upload_resume_path: "
                f"{runtime_resume}; expected: {summary_resume}"
            )
    if not raw_path and runtime_resume is not None:
        raw_path = str(runtime_resume)
    if not raw_path:
        return "missing required PDF resume upload path"
    resume_path = Path(str(raw_path)).expanduser()
    raw_package_dir = item.get("package_dir")
    try:
        resolved_resume = resolve_original_resume_pdf(
            resume_path,
            source_dir=effective_required_source_dir,
            package_dir=Path(str(raw_package_dir)) if raw_package_dir else None,
            required_pdf=effective_required_resume,
        )
    except ResumePathError as exc:
        return str(exc)
    expected_sha = str(item.get("upload_resume_pdf_sha256") or "").strip()
    if expected_sha:
        actual_sha = _sha256_file(resolved_resume)
        if actual_sha != expected_sha:
            return (
                "resume upload PDF hash does not match prepared summary: "
                f"{actual_sha}; expected: {expected_sha}"
            )
    return None


def _runtime_payload_for_summary_item(item: dict[str, object]) -> dict[str, object] | None:
    script_path = Path(str(item.get("runtime_script_path") or ""))
    if not script_path.is_file():
        return None
    try:
        return load_runtime_payload(script_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _runtime_resume_upload_path(item: dict[str, object]) -> Path | None:
    script_path = Path(str(item.get("runtime_script_path") or ""))
    payload = _runtime_payload_for_summary_item(item)
    if payload is None:
        return None
    raw_path = payload.get("resumeFile")
    if not raw_path:
        return None
    resume_path = Path(str(raw_path)).expanduser()
    if not resume_path.is_absolute():
        resume_path = script_path.parent / resume_path
    return resume_path


def _summary_required_resume_pdf(item: dict[str, object]) -> Path | None:
    raw_path = item.get("required_resume_pdf")
    if raw_path:
        return _validate_required_resume_pdf(Path(str(raw_path)).expanduser())
    payload = _runtime_payload_for_summary_item(item)
    runtime_required = payload.get("requiredResumePdf") if payload else None
    if runtime_required:
        return _validate_required_resume_pdf(Path(str(runtime_required)).expanduser())
    return None


def _summary_required_resume_source_dir(item: dict[str, object]) -> Path | None:
    raw_path = item.get("required_resume_source_dir")
    if raw_path:
        return _validate_required_resume_source_dir(Path(str(raw_path)).expanduser())
    payload = _runtime_payload_for_summary_item(item)
    runtime_source = payload.get("resumeSourceDir") if payload else None
    if runtime_source:
        return _validate_required_resume_source_dir(Path(str(runtime_source)).expanduser())
    return None


def _summary_resume_upload_path(item: dict[str, object]) -> Path | None:
    raw_path = item.get("upload_resume_path")
    if not raw_path:
        runtime_resume = _runtime_resume_upload_path(item)
        raw_path = str(runtime_resume) if runtime_resume is not None else None
    return Path(str(raw_path)).expanduser() if raw_path else None


def _attach_resume_audit_fields(
    records: list[dict[str, object]],
    summary_items: list[dict[str, object]],
    required_resume: Path | None,
    required_resume_source_dir: Path | None = None,
) -> None:
    by_script = {
        str(item.get("runtime_script_path") or item.get("fill_script_path") or ""): item
        for item in summary_items
    }
    for record in records:
        item = by_script.get(str(record.get("script_path") or ""))
        resume_path = _summary_resume_upload_path(item) if item else None
        effective_required_resume = required_resume or (
            _summary_required_resume_pdf(item) if item else None
        )
        effective_required_source_dir = required_resume_source_dir or (
            _summary_required_resume_source_dir(item) if item else None
        )
        record.update(_resume_pdf_audit_fields(resume_path, "upload_resume_pdf"))
        record.update(_resume_pdf_audit_fields(effective_required_resume, "required_resume_pdf"))
        record["required_resume_source_dir"] = (
            str(effective_required_source_dir)
            if effective_required_source_dir is not None
            else None
        )


def _resume_preflight_record(
    item: dict[str, object],
    required_resume: Path | None,
    required_resume_source_dir: Path | None = None,
) -> dict[str, object]:
    effective_required_resume = required_resume or _summary_required_resume_pdf(item)
    effective_required_source_dir = (
        required_resume_source_dir or _summary_required_resume_source_dir(item)
    )
    error = _execution_resume_upload_error(
        item,
        effective_required_resume,
        effective_required_source_dir,
    )
    upload_resume = _summary_resume_upload_path(item)
    record = {
        "company": item.get("company") or "Unknown Company",
        "title": item.get("title") or "Unknown Role",
        "script_path": item.get("runtime_script_path") or item.get("fill_script_path"),
        "package_dir": item.get("package_dir"),
        "status": "invalid" if error else "verified",
        "submit_gate": "invalid_resume_upload" if error else "resume_preflight_verified",
        "error": error,
    }
    record.update(_resume_pdf_audit_fields(upload_resume, "upload_resume_pdf"))
    record.update(_resume_pdf_audit_fields(effective_required_resume, "required_resume_pdf"))
    record["required_resume_source_dir"] = (
        str(effective_required_source_dir)
        if effective_required_source_dir is not None
        else None
    )
    return record


def _summarize_resume_preflight(records: list[dict[str, object]]) -> dict[str, int]:
    verified = sum(1 for record in records if record.get("status") == "verified")
    invalid = sum(1 for record in records if record.get("status") == "invalid")
    return {
        "total": len(records),
        "verified": verified,
        "invalid": invalid,
    }


def _write_resume_preflight(
    summary_items: list[dict[str, object]],
    out: Path,
    *,
    required_resume_pdf: Path | None = None,
    required_resume_source_dir: Path | None = None,
) -> dict[str, object]:
    required_resume = _validate_required_resume_pdf(required_resume_pdf)
    required_source_dir = _validate_required_resume_source_dir(required_resume_source_dir)
    records = [
        _resume_preflight_record(item, required_resume, required_source_dir)
        for item in summary_items
    ]
    preflight = {
        "schema_version": 1,
        "counts": _summarize_resume_preflight(records),
        "required_resume_pdf": str(required_resume) if required_resume is not None else None,
        "required_resume_source_dir": str(required_source_dir) if required_source_dir is not None else None,
        "applications": records,
    }
    preflight.update(_resume_pdf_audit_fields(required_resume, "required_resume_pdf"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(preflight, indent=2, ensure_ascii=True))
    return preflight


def _skipped_invalid_resume_record(item: dict[str, object], error: str) -> dict[str, object]:
    return {
        "company": item.get("company") or "Unknown Company",
        "title": item.get("title") or "Unknown Role",
        "script_path": item.get("runtime_script_path") or item.get("fill_script_path"),
        "status": "skipped_invalid_resume",
        "exit_code": None,
        "submit_gate": "invalid_resume_upload",
        "error": error,
        "filled_count": None,
        "review_count": None,
    }


def _skipped_resume_preflight_failed_record(item: dict[str, object]) -> dict[str, object]:
    return {
        "company": item.get("company") or "Unknown Company",
        "title": item.get("title") or "Unknown Role",
        "script_path": item.get("runtime_script_path") or item.get("fill_script_path"),
        "status": "skipped_resume_preflight_failed",
        "exit_code": None,
        "submit_gate": "resume_preflight_failed",
        "error": "resume_preflight_failed: at least one package has an invalid resume upload",
        "filled_count": None,
        "review_count": None,
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=True))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_package_agent_trajectory(
    summary_item: Mapping[str, Any],
    *,
    stage: str,
    payload: Mapping[str, Any],
) -> None:
    package_dir = Path(str(summary_item.get("package_dir") or ""))
    trajectory_value = str(
        summary_item.get("agent_trajectory_path") or ""
    )
    trajectory_path = (
        Path(trajectory_value)
        if trajectory_value
        else package_dir / "agent-trajectory.json"
    )
    try:
        safe_trajectory = (
            trajectory_path.name == "agent-trajectory.json"
            and trajectory_path.resolve().parent == package_dir.resolve()
        )
    except OSError:
        safe_trajectory = False
    if not safe_trajectory or not trajectory_path.is_file():
        return
    try:
        trajectory = json.loads(trajectory_path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(trajectory, dict):
        return
    stages = trajectory.setdefault("stages", {})
    if not isinstance(stages, dict):
        return
    stages[stage] = dict(payload)
    _write_json_atomic(trajectory_path, trajectory)


def _write_execution_audit(
    summary_items: list[dict[str, object]],
    audit_out: Path,
    *,
    node_binary: str = "node",
    timeout_seconds: int = 300,
    db: Path | None = None,
    browser_headless: bool | None = None,
    required_resume_pdf: Path | None = None,
    required_resume_source_dir: Path | None = None,
    resume_preflight_failed: bool = False,
    retry_prior_terminal_outcome: bool = False,
    resume_existing_audit: bool = False,
) -> dict[str, object]:
    required_resume = _validate_required_resume_pdf(required_resume_pdf)
    required_source_dir = _validate_required_resume_source_dir(required_resume_source_dir)
    records: list[dict[str, object]] = []
    agent_runtime_traces: dict[str, dict[str, object]] = {}
    summary_by_runtime_script = {
        str(
            item.get("runtime_script_path")
            or item.get("fill_script_path")
            or ""
        ): item
        for item in summary_items
        if isinstance(item, dict)
    }
    candidate_items = summary_items
    resume_metadata: dict[str, object] | None = None
    existing_tracking: dict[str, int] | None = None
    if resume_existing_audit:
        try:
            existing_audit = json.loads(audit_out.read_text())
        except FileNotFoundError as exc:
            raise typer.BadParameter(
                f"--resume-existing-audit requires an existing audit: {audit_out}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise typer.BadParameter(
                f"existing execution audit is unreadable: {audit_out}"
            ) from exc
        if not isinstance(existing_audit, dict):
            raise typer.BadParameter("existing execution audit must be a JSON object")
        progress = existing_audit.get("progress")
        existing_records = existing_audit.get("applications")
        if not isinstance(progress, dict) or not isinstance(existing_records, list):
            raise typer.BadParameter(
                "existing execution audit is missing progress or applications"
            )
        if bool(progress.get("complete")):
            raise typer.BadParameter("existing execution audit is already complete")
        if int(progress.get("planned", -1)) != len(summary_items):
            raise typer.BadParameter(
                "existing execution audit planned count does not match batch summary"
            )
        if int(progress.get("terminal", -1)) != len(existing_records):
            raise typer.BadParameter(
                "existing execution audit terminal count does not match its records"
            )

        summary_by_script = {
            str(item.get("runtime_script_path") or item.get("fill_script_path") or ""): item
            for item in summary_items
        }
        if "" in summary_by_script or len(summary_by_script) != len(summary_items):
            raise typer.BadParameter(
                "batch summary must contain unique runtime script paths to resume"
            )
        recorded_scripts: set[str] = set()
        for raw_record in existing_records:
            if not isinstance(raw_record, dict):
                raise typer.BadParameter(
                    "existing execution audit contains a non-object application record"
                )
            script_path = str(raw_record.get("script_path") or "")
            if (
                not script_path
                or script_path not in summary_by_script
                or script_path in recorded_scripts
            ):
                raise typer.BadParameter(
                    "existing execution audit contains an unknown or duplicate script path"
                )
            recorded_scripts.add(script_path)
            records.append(dict(raw_record))

        unrecorded_items = [
            item
            for item in summary_items
            if str(
                item.get("runtime_script_path")
                or item.get("fill_script_path")
                or ""
            )
            not in recorded_scripts
        ]
        if not unrecorded_items:
            raise typer.BadParameter(
                "existing execution audit is incomplete but has no unrecorded applications"
            )

        interrupted_item = unrecorded_items[0]
        records.append(
            {
                "company": interrupted_item.get("company") or "Unknown Company",
                "title": interrupted_item.get("title") or "Unknown Role",
                "script_path": (
                    interrupted_item.get("runtime_script_path")
                    or interrupted_item.get("fill_script_path")
                ),
                "status": "submit_clicked_unconfirmed",
                "exit_code": None,
                "submit_gate": "interrupted_execution_outcome_unknown",
                "error": "interrupted_execution_outcome_unknown",
                "filled_count": None,
                "review_count": None,
            }
        )
        candidate_items = unrecorded_items[1:]
        resume_metadata = {
            "preserved_terminal": len(existing_records),
            "interrupted_marked_unconfirmed": 1,
            "remaining_after_interrupted": len(candidate_items),
        }
        raw_tracking = existing_audit.get("tracking")
        if isinstance(raw_tracking, dict):
            existing_tracking = {
                key: int(raw_tracking.get(key, 0) or 0)
                for key in (
                    "updated",
                    "missing_application_id",
                    "application_not_found",
                )
            }
        raw_agent_runtime = existing_audit.get("agent_runtime")
        if isinstance(raw_agent_runtime, dict):
            raw_traces = raw_agent_runtime.get("applications")
            if isinstance(raw_traces, list):
                for raw_trace in raw_traces:
                    if not isinstance(raw_trace, dict):
                        continue
                    application = raw_trace.get("application")
                    script_path = (
                        str(application.get("script_path") or "")
                        if isinstance(application, dict)
                        else ""
                    )
                    if script_path:
                        agent_runtime_traces[script_path] = dict(raw_trace)

    preserved_record_count = len(records) - (1 if resume_existing_audit else 0)
    executable_items: list[dict[str, object]] = []
    for item in candidate_items:
        previous_submission = _previous_submission_reason_for_summary(item, db)
        if previous_submission:
            records.append(
                {
                    "company": item.get("company") or "Unknown Company",
                    "title": item.get("title") or "Unknown Role",
                    "script_path": item.get("runtime_script_path") or item.get("fill_script_path"),
                    "status": "skipped_previously_submitted",
                    "exit_code": None,
                    "submit_gate": "previously_submitted",
                    "error": previous_submission,
                    "filled_count": None,
                    "review_count": None,
                }
            )
            continue
        previous_terminal_outcome = (
            None
            if retry_prior_terminal_outcome
            else _previous_terminal_outcome_reason_for_summary(item, db)
        )
        if previous_terminal_outcome:
            records.append(
                {
                    "company": item.get("company") or "Unknown Company",
                    "title": item.get("title") or "Unknown Role",
                    "script_path": item.get("runtime_script_path") or item.get("fill_script_path"),
                    "status": "skipped_prior_terminal_outcome",
                    "exit_code": None,
                    "submit_gate": "prior_terminal_outcome",
                    "error": previous_terminal_outcome,
                    "filled_count": None,
                    "review_count": None,
                }
            )
            continue
        resume_upload_error = _execution_resume_upload_error(
            item,
            required_resume,
            required_source_dir,
        )
        if resume_upload_error:
            records.append(_skipped_invalid_resume_record(item, resume_upload_error))
            continue
        if resume_preflight_failed:
            records.append(_skipped_resume_preflight_failed_record(item))
            continue
        executable_items.append(item)
    _attach_resume_audit_fields(records, summary_items, required_resume, required_source_dir)
    tracking: dict[str, int] | None = None
    if db is not None:
        records_to_persist = (
            records[preserved_record_count:]
            if resume_existing_audit
            else records
        )
        tracking = existing_tracking or {
            "updated": 0,
            "missing_application_id": 0,
            "application_not_found": 0,
        }
        update = _persist_execution_statuses(records_to_persist, summary_items, db)
        for key, value in update.items():
            tracking[key] = tracking.get(key, 0) + value

    def write_snapshot() -> dict[str, object]:
        for record in records:
            attach_recovery_plan(record)
        terminal = len(records)
        planned = len(summary_items)
        audit: dict[str, object] = {
            "schema_version": 1,
            "counts": summarize_execution(records),
            "progress": {
                "planned": planned,
                "terminal": terminal,
                "remaining": max(0, planned - terminal),
                "complete": terminal >= planned,
            },
            "submit_gate": SUBMIT_GATE,
            "required_resume_pdf": str(required_resume) if required_resume is not None else None,
            "required_resume_source_dir": (
                str(required_source_dir) if required_source_dir is not None else None
            ),
            "applications": records,
        }
        audit.update(_resume_pdf_audit_fields(required_resume, "required_resume_pdf"))
        if tracking is not None:
            audit["tracking"] = tracking
        if resume_metadata is not None:
            audit["resume"] = resume_metadata
        if agent_runtime_traces:
            audit["agent_runtime"] = {
                "schema_version": 1,
                "closed_loop": True,
                "applications": list(agent_runtime_traces.values()),
            }
        _write_json_atomic(audit_out, audit)
        return audit

    write_snapshot()
    persisted_record_ids: set[int] = set()

    def persist_terminal(
        record: dict[str, object],
        _position: int,
        _total: int,
    ) -> None:
        record_id = id(record)
        if record_id in persisted_record_ids:
            return
        persisted_record_ids.add(record_id)
        records.append(record)
        _attach_resume_audit_fields(
            [record],
            summary_items,
            required_resume,
            required_source_dir,
        )
        if tracking is not None and db is not None:
            update = _persist_execution_statuses([record], summary_items, db)
            for key, value in update.items():
                tracking[key] = tracking.get(key, 0) + value
        write_snapshot()

    def persist_agent_loop(
        trace: dict[str, Any],
        _position: int,
        _total: int,
    ) -> None:
        application = trace.get("application")
        script_path = (
            str(application.get("script_path") or "")
            if isinstance(application, dict)
            else ""
        )
        if not script_path:
            return
        agent_runtime_traces[script_path] = dict(trace)
        summary_item = summary_by_runtime_script.get(script_path)
        if isinstance(summary_item, dict):
            _append_package_agent_trajectory(
                summary_item,
                stage="execution",
                payload=trace,
            )
        write_snapshot()

    if executable_items:
        executed_records = execute_application_batch(
            executable_items,
            node_binary=node_binary,
            timeout_seconds=timeout_seconds,
            browser_headless=browser_headless,
            required_resume_pdf=required_resume,
            required_resume_source_dir=required_source_dir,
            database_path=db,
            on_record=persist_terminal,
            on_agent_loop=persist_agent_loop,
        )
        for record in executed_records:
            persist_terminal(record, 0, len(executable_items))
    return write_snapshot()


def _audit_records_submission(audit_path: Path) -> bool:
    """Return whether a prior package audit has already verified submission."""
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    records = audit.get("applications") if isinstance(audit, dict) else None
    return isinstance(records, list) and any(
        isinstance(record, dict) and record.get("status") == "submitted"
        for record in records
    )


def _audit_blocks_automatic_retry(audit_path: Path) -> bool:
    """Prevent duplicate submission after an ATS rejection or unknown outcome."""
    try:
        audit = json.loads(audit_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    records = audit.get("applications") if isinstance(audit, dict) else None
    return isinstance(records, list) and any(
        isinstance(record, dict)
        and (
            record.get("status") == "submission_blocked_by_anti_spam"
            or record.get("status") == "submit_clicked_unconfirmed"
            or record.get("status") == "autofill_timed_out"
            or record.get("status") == "submission_processing_error"
            or record.get("error") == "execution_interrupted_unconfirmed"
            or record.get("error") == "application_form_unavailable"
        )
        for record in records
    )


def _acquire_package_execution_lock(package_dir: Path) -> Path:
    """Prevent simultaneous browser runs from re-submitting one package."""
    lock_path = package_dir / ".execution.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        try:
            match = re.search(r"^pid=(\d+)$", lock_path.read_text(), flags=re.MULTILINE)
            owner_pid = int(match.group(1)) if match else None
            os.kill(owner_pid, 0) if owner_pid is not None else None
        except ProcessLookupError:
            lock_path.unlink(missing_ok=True)
            return _acquire_package_execution_lock(package_dir)
        except (OSError, ValueError):
            # A malformed or unreadable lock might belong to a process that is
            # still creating it, so preserve it rather than risk a duplicate.
            pass
        raise typer.BadParameter(
            f"already being executed; wait for its audit before retrying: {package_dir}"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"pid={os.getpid()}\n")
    return lock_path


def _has_stale_package_execution_lock(package_dir: Path) -> bool:
    """Return whether a prior package run ended without releasing its lock."""
    lock_path = package_dir / ".execution.lock"
    if not lock_path.is_file():
        return False
    try:
        match = re.search(r"^pid=(\d+)$", lock_path.read_text(), flags=re.MULTILINE)
        owner_pid = int(match.group(1)) if match else None
        if owner_pid is None:
            return False
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    return False


def _write_interrupted_execution_audit(
    summary: dict[str, object],
    audit_path: Path,
    *,
    db: Path | None = None,
    required_resume_pdf: Path | None = None,
    required_resume_source_dir: Path | None = None,
) -> dict[str, object]:
    """Persist an unknown prior execution without permitting an automatic retry."""
    required_resume = _validate_required_resume_pdf(required_resume_pdf)
    required_source_dir = _validate_required_resume_source_dir(required_resume_source_dir)
    record = {
        "company": summary.get("company") or "Unknown Company",
        "title": summary.get("title") or "Unknown Role",
        "script_path": summary.get("runtime_script_path"),
        "status": "autofill_failed",
        "exit_code": None,
        "submit_gate": "execution_interrupted_unconfirmed",
        "error": "execution_interrupted_unconfirmed",
        "filled_count": None,
        "review_count": None,
    }
    attach_recovery_plan(record)
    record.update(_resume_pdf_audit_fields(_summary_resume_upload_path(summary), "upload_resume_pdf"))
    record.update(_resume_pdf_audit_fields(required_resume, "required_resume_pdf"))
    audit: dict[str, object] = {
        "schema_version": 1,
        "counts": summarize_execution([record]),
        "submit_gate": "execution_interrupted_unconfirmed",
        "required_resume_pdf": str(required_resume) if required_resume is not None else None,
        "required_resume_source_dir": str(required_source_dir) if required_source_dir is not None else None,
        "applications": [record],
    }
    audit.update(_resume_pdf_audit_fields(required_resume, "required_resume_pdf"))
    if db is not None:
        audit["tracking"] = _persist_execution_statuses([record], [summary], db)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=True))
    return audit


def _execution_attempt_path(package_dir: Path) -> Path:
    return package_dir / ".execution-attempt.json"


def _write_execution_attempt(package_dir: Path, summary: dict[str, object]) -> Path:
    """Durably mark a package before a browser process can perform a submit."""
    path = _execution_attempt_path(package_dir)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "company": summary.get("company") or "Unknown Company",
                "title": summary.get("title") or "Unknown Role",
                "application_id": summary.get("application_id"),
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return path


def _persist_execution_statuses(
    records: list[dict[str, object]],
    summary_items: list[dict[str, object]],
    db: Path,
) -> dict[str, int]:
    """Write runtime outcomes back only through package-bound tracker IDs."""
    by_script = {
        str(item.get("runtime_script_path") or item.get("fill_script_path") or ""): item
        for item in summary_items
    }
    conn = connect(db)
    init_db(conn)
    updated = 0
    missing_application_id = 0
    not_found = 0
    for record in records:
        if str(record.get("status") or "").startswith("skipped_"):
            continue
        item = by_script.get(str(record.get("script_path") or ""))
        application_id = item.get("application_id") if item else None
        try:
            application_id = int(application_id) if application_id is not None else None
        except (TypeError, ValueError):
            application_id = None
        if application_id is None:
            missing_application_id += 1
            continue
        if update_application_execution_status(conn, application_id, str(record["status"])):
            update_application_resume_evidence(conn, application_id, record)
            updated += 1
        else:
            not_found += 1
    conn.close()
    return {
        "updated": updated,
        "missing_application_id": missing_application_id,
        "application_not_found": not_found,
    }


def _reconcile_confirmed_evidence(
    summary_items: list[dict[str, object]],
    db: Path,
) -> dict[str, int]:
    """Recover tracker rows from previously written confirmation evidence.

    This is intentionally narrow: it accepts only a runtime's own
    ``submission-confirmation.txt`` artifact and never promotes a click or a
    verification prompt to a submitted application.
    """
    conn = connect(db)
    init_db(conn)
    confirmed_evidence = 0
    created = 0
    updated = 0
    for item in summary_items:
        package_dir = Path(
            str(item.get("package_dir") or Path(str(item.get("runtime_script_path") or "")).parent)
        )
        evidence_path = package_dir / "submission-confirmation.txt"
        try:
            evidence = evidence_path.read_text()
        except OSError:
            continue
        if not re.search(r"^confirmation:\s*matched\s+.+", evidence, flags=re.MULTILINE | re.IGNORECASE):
            continue
        confirmed_evidence += 1
        company = str(item.get("company") or "Unknown Company")
        title = str(item.get("title") or "Unknown Role")
        apply_url = _summary_application_url(item)
        row = conn.execute(
            """
            select id from applications
            where company = ? and title = ?
              and (coalesce(apply_url, '') = ? or coalesce(apply_url, '') = '')
            order by case when coalesce(apply_url, '') = ? then 0 else 1 end, id desc
            limit 1
            """,
            (company, title, apply_url, apply_url),
        ).fetchone()
        if row is None:
            job_id = create_job(
                conn,
                Job(
                    company=company,
                    title=title,
                    apply_url=apply_url or None,
                    source="execution-evidence",
                    raw_jd="Recovered from confirmed runtime submission evidence.",
                ),
            )
            application_id = create_application(
                conn,
                job_id,
                Job(
                    company=company,
                    title=title,
                    apply_url=apply_url or None,
                    source="execution-evidence",
                    raw_jd="Recovered from confirmed runtime submission evidence.",
                ),
            )
            created += 1
        else:
            application_id = int(row["id"])
            if apply_url:
                conn.execute(
                    """
                    update applications
                    set apply_url = ?
                    where id = ? and coalesce(apply_url, '') = ''
                    """,
                    (apply_url, application_id),
                )
                conn.execute(
                    """
                    update jobs
                    set apply_url = ?
                    where id = (select job_id from applications where id = ?)
                      and coalesce(apply_url, '') = ''
                    """,
                    (apply_url, application_id),
                )
                conn.commit()
        if update_application_execution_status(conn, application_id, "submitted"):
            resume_evidence: dict[str, object | None] = {}
            resume_evidence.update(
                _resume_pdf_audit_fields(_summary_resume_upload_path(item), "upload_resume_pdf")
            )
            resume_evidence.update(
                _resume_pdf_audit_fields(_summary_required_resume_pdf(item), "required_resume_pdf")
            )
            update_application_resume_evidence(conn, application_id, resume_evidence)
            updated += 1
    conn.close()
    return {
        "confirmed_evidence": confirmed_evidence,
        "created": created,
        "updated": updated,
    }


def _summaries_from_confirmation_roots(roots: list[Path]) -> list[dict[str, object]]:
    """Recover package summaries only for packages with terminal confirmation evidence."""
    summaries: list[dict[str, object]] = []
    seen: set[Path] = set()
    for root in roots:
        for evidence_path in sorted(root.rglob("submission-confirmation.txt")):
            package_dir = evidence_path.parent.resolve()
            if package_dir in seen:
                continue
            seen.add(package_dir)
            script_path = package_dir / "autofill-runtime.js"
            if not script_path.is_file():
                continue
            summaries.append(_execution_summary_for_package(package_dir))
    return summaries


def _saved_page_confirmation(
    package_dir: Path,
) -> tuple[str, Path, str] | None:
    """Verify saved post-submit text without revisiting or re-submitting a page."""
    for filename in ("submission-click-unconfirmed.txt", "submission-processing-error.txt"):
        evidence_path = package_dir / filename
        try:
            evidence = evidence_path.read_text()
        except OSError:
            continue
        url_match = re.search(r"^url:\s*(.+)$", evidence, flags=re.MULTILINE)
        title_match = re.search(r"^title:\s*(.+)$", evidence, flags=re.MULTILINE)

        class SavedEvidencePage:
            def evaluate(self, _script):
                return {
                    "url": url_match.group(1).strip() if url_match else "",
                    "title": title_match.group(1).strip() if title_match else "",
                    "text": evidence,
                }

        confirmation = _detect_submission_confirmation(SavedEvidencePage())
        if confirmation:
            return confirmation, evidence_path, evidence
    return None


def _confirmation_from_saved_click_evidence(package_dir: Path) -> str | None:
    """Compatibility wrapper for saved click or delayed-success evidence."""
    recovered = _saved_page_confirmation(package_dir)
    return recovered[0] if recovered else None


def _promote_saved_page_confirmation(package_dir: Path, db: Path) -> bool:
    """Promote only a saved page whose deterministic text now verifies submission."""
    recovered = _saved_page_confirmation(package_dir)
    if not recovered:
        return False
    confirmation, source_path, evidence = recovered
    confirmation_path = package_dir / "submission-confirmation.txt"
    confirmation_path.write_text(
        f"confirmation: {confirmation}\n"
        f"reconciled_from: {source_path.name}\n\n"
        f"{evidence}"
    )
    screenshot = source_path.with_suffix(".png")
    if screenshot.is_file():
        shutil.copyfile(screenshot, confirmation_path.with_suffix(".png"))

    summary = _execution_summary_for_package(package_dir)
    _reconcile_confirmed_evidence([summary], db)
    audit_path = package_dir / "execution-audit.json"
    try:
        audit = json.loads(audit_path.read_text())
        records = audit.get("applications") or []
        for record in records:
            if str(record.get("script_path") or "") == str(summary["runtime_script_path"]):
                record.update(
                    {
                        "status": "submitted",
                        "exit_code": 0,
                        "submit_gate": "submitted",
                        "error": None,
                        "evidence": str(confirmation_path.resolve()),
                    }
                )
        audit["counts"] = summarize_execution(records)
        audit["tracking"] = _persist_execution_statuses(records, [summary], db)
        audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=True))
    except (OSError, json.JSONDecodeError):
        pass
    return True


def _summary_application_url(item: dict[str, object]) -> str:
    supplied = str(item.get("apply_url") or "").strip()
    if supplied:
        return supplied
    script_path = Path(str(item.get("runtime_script_path") or ""))
    try:
        script = script_path.read_text()
    except OSError:
        return ""
    match = re.search(r'"applicationUrl"\s*:\s*"([^"\\]+)"', script)
    return match.group(1) if match else ""


def _execution_summary_for_package(package_dir: Path) -> dict[str, object]:
    script_path = package_dir / "autofill-runtime.js"
    if not script_path.is_file():
        raise typer.BadParameter(f"{package_dir} does not contain autofill-runtime.js")
    payload = load_runtime_payload(script_path)
    review_path = package_dir / "review.md"
    try:
        review = review_path.read_text()
    except OSError:
        review = ""
    application_id_match = re.search(r"^application_id=(\d+)$", review, flags=re.MULTILINE)
    review_company_match = re.search(r"^Company:\s*(.+?)\s*$", review, flags=re.MULTILINE)
    review_title_match = re.search(r"^Title:\s*(.+?)\s*$", review, flags=re.MULTILINE)
    profile = payload.get("profile") or {}
    company = profile.get("target_company") or (review_company_match.group(1) if review_company_match else None)
    title = profile.get("target_title") or (review_title_match.group(1) if review_title_match else None)
    return {
        "company": str(company or "Unknown Company"),
        "title": str(title or "Unknown Role"),
        "apply_url": str(payload.get("applicationUrl") or ""),
        "package_dir": str(package_dir),
        "review_path": str(review_path) if review_path.is_file() else None,
        "runtime_script_path": str(script_path),
        "application_id": application_id_match.group(1) if application_id_match else None,
        "upload_resume_path": str(payload.get("resumeFile") or "") or None,
        "required_resume_pdf": str(payload.get("requiredResumePdf") or "") or None,
        "required_resume_source_dir": str(payload.get("resumeSourceDir") or "") or None,
        "upload_cover_letter_path": str(payload.get("coverLetterFile") or "") or None,
    }


class _PipelinePreparationTool(Tool):
    """Import, screen, deduplicate, and package one bounded candidate cohort."""

    def __init__(
        self,
        builder: Callable[[], dict[str, object]],
    ) -> None:
        super().__init__(
            "prepare_application_cohort",
            "Prepare one policy-screened cohort and its application packages.",
            effect=ToolEffect.WRITE,
        )
        self._builder = builder

    def run(self, _parameters: dict[str, Any]) -> dict[str, object]:
        return self._builder()

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="source_config",
                type="string",
                description="Configured job-source file name.",
            )
        ]


def _run_pipeline_direct(
    config_file: Path,
    *,
    out_dir: Path,
    min_score: int,
    limit: Optional[int],
    resume_source_dir: Optional[Path],
    resume: Optional[Path],
    profile: Optional[Path],
    sensitive_kb: Optional[Path],
    db: Optional[Path],
    use_llm: bool,
    llm_model: Optional[str],
    llm_provider: Optional[str],
    llm_base_url: Optional[str],
    profile_vector_db: Optional[Path],
    required_resume_pdf: Optional[Path] = None,
) -> dict[str, object]:
    effective_resume_source_dir = _effective_resume_source_dir(
        resume_source_dir,
        resume=resume,
        required_resume_pdf=required_resume_pdf,
    )
    required_resume = _effective_required_resume_pdf(
        required_resume_pdf,
        resume=resume,
        required_resume_source_dir=effective_resume_source_dir,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = out_dir / "jobs.json"
    shortlist_path = out_dir / "shortlist.json"
    applications_dir = out_dir / "applications"

    jobs = load_jobs_from_source_config(config_file)
    _write_jobs_json(jobs, jobs_path)
    candidate_profile = _load_profile_facts(profile, sensitive_kb) or {}
    eligible_jobs: list[Job] = []
    screened_out: list[dict[str, object]] = []
    already_submitted: list[dict[str, object]] = []
    skipped_terminal_outcomes: list[dict[str, object]] = []
    submitted_urls, submitted_titles = _submitted_application_keys(db)
    terminal_outcomes = _terminal_application_outcomes(db)
    anti_spam_blocked_companies = _anti_spam_blocked_companies(db)
    anti_spam_blocked_hosts = _anti_spam_blocked_hosts(db)
    failure_blocked_companies, failure_blocked_adapters = _failure_circuit_breakers(db)
    for job in jobs:
        if _was_previously_submitted(job, submitted_urls, submitted_titles):
            already_submitted.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "apply_url": job.apply_url or job.source_url,
                    "reason": "matching verified submission already exists",
                }
            )
            continue
        company_key = job.company.strip().lower()
        if company_key and company_key in anti_spam_blocked_companies:
            skipped_terminal_outcomes.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "apply_url": job.apply_url or job.source_url,
                    "reason": "matching company has prior anti-spam blocker",
                }
            )
            continue
        raw_application_url = job.apply_url or job.source_url
        application_url = _normalized_application_url(raw_application_url)
        application_host = _anti_spam_host_key(raw_application_url)
        if application_host and application_host in anti_spam_blocked_hosts:
            skipped_terminal_outcomes.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "apply_url": job.apply_url or job.source_url,
                    "reason": f"matching apply host has repeated anti-spam blockers: {application_host}",
                }
            )
            continue
        failure_status = failure_blocked_companies.get(company_key)
        if failure_status:
            skipped_terminal_outcomes.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "apply_url": raw_application_url,
                    "reason": (
                        "matching company failure circuit is open: "
                        f"{failure_status}"
                    ),
                }
            )
            continue
        failure_adapter = _failure_adapter_key(raw_application_url, job.company)
        failure_status = failure_blocked_adapters.get(failure_adapter)
        if failure_status:
            skipped_terminal_outcomes.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "apply_url": raw_application_url,
                    "reason": (
                        "matching adapter failure circuit is open: "
                        f"{failure_adapter} ({failure_status})"
                    ),
                }
            )
            continue
        prior_terminal_status = terminal_outcomes.get(application_url)
        if prior_terminal_status:
            skipped_terminal_outcomes.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "apply_url": job.apply_url or job.source_url,
                    "reason": f"matching prior terminal outcome already exists: {prior_terminal_status}",
                }
            )
            continue
        screening = screen_job_for_candidate(job, candidate_profile)
        if screening.eligible:
            eligible_jobs.append(job)
            continue
        screened_out.append(
            {
                "title": job.title,
                "company": job.company,
                "apply_url": job.apply_url or job.source_url,
                "reasons": screening.reasons,
            }
        )
    shortlisted = shortlist_jobs(
        eligible_jobs,
        min_score=min_score,
        limit=limit,
        diversify_companies=True,
    )
    shortlist_rows = shortlisted_jobs_to_dicts(shortlisted)
    shortlist_path.write_text(json.dumps(shortlist_rows, indent=2, ensure_ascii=True))

    applications_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for index, item in enumerate(shortlisted, start=1):
        package_dir = applications_dir / _review_slug(index, item.job)
        summary = _prepare_application_package(
            item.job,
            package_dir,
            resume_source_dir=effective_resume_source_dir,
            db=db,
            profile=profile,
            sensitive_kb=sensitive_kb,
            resume=resume,
            upload_resume=True,
            use_llm=use_llm,
            llm_model=llm_model,
            llm_provider=llm_provider,
            llm_base_url=llm_base_url,
            profile_vector_db=profile_vector_db,
            required_resume_pdf=required_resume,
        )
        summary["index"] = str(index)
        summary["fit_score"] = str(item.fit.score)
        summaries.append(summary)

    summary_path = applications_dir / "batch-summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=True))
    manifest = {
        "schema_version": 1,
        "counts": {
            "imported": len(jobs),
            "shortlisted": len(shortlisted),
            "prepared": len(summaries),
        },
        "artifacts": {
            "jobs": str(jobs_path),
            "shortlist": str(shortlist_path),
            "batch_summary": str(summary_path),
        },
        "submit_gate": "automatic_when_no_blocking_review",
        "required_resume_pdf": str(required_resume) if required_resume else None,
        "required_resume_source_dir": (
            str(effective_resume_source_dir) if effective_resume_source_dir else None
        ),
    }
    manifest.update(_resume_pdf_audit_fields(required_resume, "required_resume_pdf"))
    if screened_out:
        screened_out_path = out_dir / "candidate-screening.json"
        screened_out_path.write_text(json.dumps(screened_out, indent=2, ensure_ascii=True))
        manifest["artifacts"]["candidate_screening"] = str(screened_out_path)
    if already_submitted:
        already_submitted_path = out_dir / "already-submitted.json"
        already_submitted_path.write_text(json.dumps(already_submitted, indent=2, ensure_ascii=True))
        manifest["artifacts"]["already_submitted"] = str(already_submitted_path)
    if skipped_terminal_outcomes:
        terminal_outcomes_path = out_dir / "prior-terminal-outcomes.json"
        terminal_outcomes_path.write_text(
            json.dumps(skipped_terminal_outcomes, indent=2, ensure_ascii=True)
        )
        manifest["artifacts"]["prior_terminal_outcomes"] = str(terminal_outcomes_path)
    manifest_path = out_dir / "pipeline-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True))
    manifest["artifacts"]["manifest"] = str(manifest_path)
    return manifest


def _run_pipeline(
    config_file: Path,
    *,
    out_dir: Path,
    min_score: int,
    limit: Optional[int],
    resume_source_dir: Optional[Path],
    resume: Optional[Path],
    profile: Optional[Path],
    sensitive_kb: Optional[Path],
    db: Optional[Path],
    use_llm: bool,
    llm_model: Optional[str],
    llm_provider: Optional[str],
    llm_base_url: Optional[str],
    profile_vector_db: Optional[Path],
    required_resume_pdf: Optional[Path] = None,
) -> dict[str, object]:
    """Run cohort preparation through the same controlled Agent Core contract."""
    registry = ToolRegistry()
    registry.register_tool(
        _PipelinePreparationTool(
            lambda: _run_pipeline_direct(
                config_file,
                out_dir=out_dir,
                min_score=min_score,
                limit=limit,
                resume_source_dir=resume_source_dir,
                resume=resume,
                profile=profile,
                sensitive_kb=sensitive_kb,
                db=db,
                use_llm=use_llm,
                llm_model=llm_model,
                llm_provider=llm_provider,
                llm_base_url=llm_base_url,
                profile_vector_db=profile_vector_db,
                required_resume_pdf=required_resume_pdf,
            )
        )
    )
    perception = StructuredPerception()
    long_term_memory = (
        SQLiteApplicationMemory(db)
        if db is not None
        else NullLongTermMemory()
    )
    core = AgentCore(
        ControlledExecution(
            registry,
            policy_gate=JobApplicationPolicyGate(),
            short_term_memory=ShortTermMemory(),
            long_term_memory=long_term_memory,
            perception=perception,
        )
    )
    initial_observation = perception.observe(
        "daily_application_request",
        "source_config",
        {
            "phase": "prepare",
            "status": "ready",
        },
    )
    loop_result = core.run_loop(
        core.create_plan(
            "Prepare a truthful, deduplicated cohort for application.",
            [
                ToolCall(
                    tool_name="prepare_application_cohort",
                    parameters={"source_config": config_file.name},
                    effect=ToolEffect.WRITE,
                    purpose=(
                        "Import, screen, deduplicate, and package the cohort."
                    ),
                    context={
                        "phase": "prepare",
                        "duplicate": False,
                    },
                )
            ],
        ),
        initial_observation=initial_observation,
        memory_query="daily application cohort",
        remember_rounds=db is not None,
    )
    result = loop_result.results[0] if loop_result.results else None
    pipeline_trace = agent_loop_result_to_dict(loop_result)
    if (
        result is None
        or not result.ok
        or not isinstance(result.output, Mapping)
    ):
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            out_dir / "pipeline-agent-trace.json",
            {
                "schema_version": 1,
                "closed_loop": True,
                "pipeline": pipeline_trace,
            },
        )
        message = (
            result.error
            if result is not None
            else "prepare_application_cohort_returned_no_result"
        )
        raise typer.BadParameter(str(message))
    manifest = dict(result.output)
    manifest["agent_runtime"] = {
        "schema_version": 1,
        "closed_loop": True,
        "pipeline": pipeline_trace,
    }
    manifest_path = out_dir / "pipeline-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True)
    )
    return manifest


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


def _backfill_application_source(
    application_id: int,
    job: Job,
    db_path: Path | None,
) -> int:
    """Retain authoritative intake URLs when the text-based tracker omits them."""
    if db_path is None:
        return application_id
    conn = connect(db_path)
    try:
        init_db(conn)
        apply_url = (job.apply_url or job.source_url or "").strip()
        source_url = (job.source_url or job.apply_url or "").strip()
        source = (job.source or "").strip()
        tracked_application_id = application_id
        if apply_url:
            row = conn.execute(
                "select company, title, apply_url from applications where id = ?",
                (application_id,),
            ).fetchone()
            dedupe_key = (
                application_dedupe_key(
                    row["company"],
                    row["title"],
                    row["apply_url"] or apply_url,
                )
                if row is not None
                else None
            )
            existing = (
                conn.execute(
                    """
                    select id from applications
                    where dedupe_key = ? and id <> ?
                    limit 1
                    """,
                    (dedupe_key, application_id),
                ).fetchone()
                if dedupe_key is not None
                else None
            )
            if existing is not None:
                tracked_application_id = int(existing["id"])
            else:
                try:
                    conn.execute(
                        """
                        update applications
                        set apply_url = case
                                when coalesce(apply_url, '') = '' then ?
                                else apply_url
                            end,
                            dedupe_key = coalesce(?, dedupe_key)
                        where id = ?
                        """,
                        (apply_url, dedupe_key, application_id),
                    )
                except sqlite3.IntegrityError:
                    existing = conn.execute(
                        "select id from applications where dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is not None:
                        tracked_application_id = int(existing["id"])
        conn.execute(
            """
            update jobs
            set apply_url = case when coalesce(apply_url, '') = '' then ? else apply_url end,
                source_url = case when coalesce(source_url, '') = '' then ? else source_url end,
                source = case when source = '' or source = 'manual' then ? else source end
            where id = (select job_id from applications where id = ?)
            """,
            (apply_url, source_url, source, application_id),
        )
        conn.commit()
        return tracked_application_id
    finally:
        conn.close()


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


class _ApplicationRuntimePackageTool(Tool):
    """Build executable package artifacts inside the application Agent Core."""

    def __init__(
        self,
        builder: Callable[[], dict[str, str | None]],
    ) -> None:
        super().__init__(
            "runtime_package_builder",
            "Build resume-bound application files and guarded runtime scripts.",
            effect=ToolEffect.WRITE,
        )
        self._builder = builder

    def run(self, _parameters: dict[str, Any]) -> dict[str, str | None]:
        return self._builder()

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="application_id",
                type="string",
                description="Stable application identity when already tracked.",
                required=False,
            )
        ]


def _build_runtime_package_artifacts(
    *,
    job: Job,
    out_dir: Path,
    review: str,
    profile_facts: dict[str, Any] | None,
    resume: Path | None,
    selected_resume_source_dir: Path | None,
    required_resume_pdf: Path | None,
    form_snapshot: Path | None,
    runtime_headless: bool | None,
    db: Path | None,
) -> dict[str, str | None]:
    """Build all environment-bound package files as one controlled Tool."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "review.md").write_text(review)
    selected_resume_path = None
    selected_resume_track = None
    cover_letter_path = None
    upload_cover_letter_path = None
    upload_cover_letter_docx_path = None
    required_resume = _effective_required_resume_pdf(
        required_resume_pdf,
        resume=resume,
        required_resume_source_dir=selected_resume_source_dir,
        package_dir=out_dir,
    )
    effective_resume = resume or required_resume
    if effective_resume:
        selected_resume_path = _selected_resume_upload_path(
            effective_resume,
            out_dir,
            required_resume_pdf=required_resume,
            required_resume_source_dir=selected_resume_source_dir,
        )
    elif selected_resume_source_dir:
        selected_template = select_best_resume_template(
            index_resume_templates(selected_resume_source_dir),
            target_track=classify_role(job),
            required_skills=parse_jd(
                format_job_as_jd_text(job)
            ).required_skills,
        )
        if selected_template:
            selected_resume_path = _selected_resume_upload_path(
                selected_template.upload_path,
                out_dir,
                required_resume_pdf=required_resume,
                required_resume_source_dir=selected_resume_source_dir,
            )
            selected_resume_track = selected_template.track
        elif profile_facts is not None:
            raise typer.BadParameter(
                "No uploadable PDF resumes found in source directory: "
                f"{selected_resume_source_dir}"
            )
    elif profile_facts is not None:
        raise typer.BadParameter(_missing_resume_source_error())

    if profile_facts is not None:
        # Package-only values never modify the user's saved profile.
        profile_facts.pop("submission_blockers", None)
        if selected_resume_path:
            profile_facts["resume_file"] = str(selected_resume_path)
        cover_letter_path, upload_cover_letter_path = (
            _render_cover_letter_pdf(job, profile_facts, out_dir)
        )
        upload_cover_letter_docx_path = out_dir / "cover-letter.docx"
        profile_facts["cover_letter_file"] = str(upload_cover_letter_path)

    fill_script_path = None
    runtime_script_path = None
    if form_snapshot and profile_facts is not None:
        form_profile_facts = dict(profile_facts)
        if selected_resume_path:
            form_profile_facts["resume_file"] = str(selected_resume_path)
        plan = build_form_fill_plan(
            inspect_form_snapshot(form_snapshot.read_text()),
            form_profile_facts,
        )
        fill_script_path = out_dir / "fill-form.js"
        fill_script_path.write_text(
            render_playwright_fill_script(
                plan,
                application_url=job.apply_url or job.source_url,
            )
        )
    if profile_facts is not None:
        runtime_script_path = out_dir / "autofill-runtime.js"
        try:
            runtime_max_pages = int(
                os.getenv("JOB_AGENT_RUNTIME_MAX_PAGES") or "12"
            )
        except ValueError:
            runtime_max_pages = 12
        runtime_script_path.write_text(
            render_runtime_autofill_script(
                profile=profile_facts,
                resume_file=(
                    str(selected_resume_path)
                    if selected_resume_path
                    else None
                ),
                resume_source_dir=(
                    str(selected_resume_source_dir)
                    if selected_resume_source_dir
                    else None
                ),
                required_resume_pdf=(
                    str(required_resume) if required_resume else None
                ),
                cover_letter_file=(
                    str(upload_cover_letter_path)
                    if upload_cover_letter_path
                    else None
                ),
                application_url=job.apply_url or job.source_url,
                max_pages=max(1, runtime_max_pages),
                headless=(
                    _runtime_browser_headless()
                    if runtime_headless is None
                    else runtime_headless
                ),
            )
        )

    application_id_match = re.search(
        r"^application_id=(\d+)$",
        review,
        flags=re.MULTILINE,
    )
    tracked_application_id = (
        _backfill_application_source(
            int(application_id_match.group(1)),
            job,
            db,
        )
        if application_id_match
        else None
    )
    summary = {
        "company": job.company,
        "title": job.title,
        "apply_url": job.apply_url or job.source_url,
        "package_dir": str(out_dir),
        "review_path": str(out_dir / "review.md"),
        "selected_resume_path": (
            str(selected_resume_path) if selected_resume_path else None
        ),
        "selected_resume_track": selected_resume_track,
        "upload_resume_path": (
            str(selected_resume_path) if selected_resume_path else None
        ),
        "required_resume_pdf": (
            str(required_resume) if required_resume else None
        ),
        "required_resume_source_dir": (
            str(selected_resume_source_dir)
            if selected_resume_source_dir
            else None
        ),
        "cover_letter_path": (
            str(cover_letter_path) if cover_letter_path else None
        ),
        "upload_cover_letter_path": (
            str(upload_cover_letter_path)
            if upload_cover_letter_path
            else None
        ),
        "upload_cover_letter_docx_path": (
            str(upload_cover_letter_docx_path)
            if upload_cover_letter_docx_path
            else None
        ),
        "fill_script_path": (
            str(fill_script_path) if fill_script_path else None
        ),
        "runtime_script_path": (
            str(runtime_script_path) if runtime_script_path else None
        ),
        "application_id": (
            str(tracked_application_id)
            if tracked_application_id is not None
            else None
        ),
    }
    summary.update(
        _resume_pdf_audit_fields(
            selected_resume_path,
            "upload_resume_pdf",
        )
    )
    summary.update(
        _resume_pdf_audit_fields(required_resume, "required_resume_pdf")
    )
    return summary


def _prepare_application_package(
    job: Job,
    out_dir: Path,
    resume_source_dir: Optional[Path] = None,
    db: Optional[Path] = None,
    form_snapshot: Optional[Path] = None,
    profile: Optional[Path] = None,
    sensitive_kb: Optional[Path] = None,
    resume: Optional[Path] = None,
    upload_resume: bool = False,
    use_llm: bool = False,
    llm_model: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_base_url: Optional[str] = None,
    runtime_headless: Optional[bool] = None,
    profile_vector_db: Optional[Path] = Path("profiles/gaoyi-profile.db"),
    required_resume_pdf: Optional[Path] = None,
) -> dict[str, str | None]:
    form_snapshot_json = form_snapshot.read_text() if form_snapshot else None
    profile_facts = _load_profile_facts(profile, sensitive_kb)
    screening = screen_job_for_candidate(job, profile_facts)
    if not screening.eligible:
        raise typer.BadParameter(
            "Refusing to prepare ineligible application: "
            + "; ".join(screening.reasons)
        )
    llm = _build_llm(
        use_llm=use_llm,
        model=llm_model,
        provider=llm_provider,
        base_url=llm_base_url,
    )
    if profile_facts is not None:
        profile_facts = enrich_profile_for_job(
            profile_facts,
            job,
            llm=llm,
            use_llm=use_llm,
            profile_vector_db=profile_vector_db,
        )
    profile_json = json.dumps(profile_facts, ensure_ascii=True) if profile_facts is not None else None
    selected_resume_source_dir = _effective_resume_source_dir(
        resume_source_dir,
        resume=resume,
        required_resume_pdf=required_resume_pdf,
    )
    agent = JobApplicationAgent(
        name="job-application-agent",
        llm=llm,
        resume_source_dir=selected_resume_source_dir,
        database_path=db,
        package_dir=out_dir,
        form_snapshot_json=form_snapshot_json,
        profile_json=profile_json,
    )
    review = agent.run(format_job_as_jd_text(job))

    agent.tool_registry.register_tool(
        _ApplicationRuntimePackageTool(
            lambda: _build_runtime_package_artifacts(
                job=job,
                out_dir=out_dir,
                review=review,
                profile_facts=profile_facts,
                resume=resume,
                selected_resume_source_dir=selected_resume_source_dir,
                required_resume_pdf=required_resume_pdf,
                form_snapshot=form_snapshot,
                runtime_headless=runtime_headless,
                db=db,
            )
        )
    )
    tracked_match = re.search(
        r"^application_id=(\d+)$",
        review,
        flags=re.MULTILINE,
    )
    package_loop = agent.continue_with_tools(
        "Build the executable package for the prepared application.",
        [
            ToolCall(
                tool_name="runtime_package_builder",
                parameters={
                    "application_id": (
                        tracked_match.group(1) if tracked_match else ""
                    )
                },
                effect=ToolEffect.WRITE,
                purpose=(
                    "Select an approved resume and build guarded runtime files."
                ),
                context={
                    "phase": "package_build",
                    "duplicate": False,
                },
            )
        ],
        memory_query=f"{job.company} {job.title}",
    )
    package_result = (
        package_loop.results[0] if package_loop.results else None
    )
    serialized_preparation = [
        agent_loop_result_to_dict(loop)
        for loop in agent.loop_results
    ]
    agent_runtime_id = (
        f"application-{tracked_match.group(1)}"
        if tracked_match
        else f"application-{uuid4().hex}"
    )
    trajectory_path = out_dir / "agent-trajectory.json"
    if (
        package_result is None
        or not package_result.ok
        or not isinstance(package_result.output, Mapping)
    ):
        _write_json_atomic(
            trajectory_path,
            {
                "schema_version": 1,
                "application": {
                    "application_id": (
                        tracked_match.group(1) if tracked_match else None
                    ),
                    "agent_runtime_id": agent_runtime_id,
                    "company": job.company,
                    "title": job.title,
                },
                "stages": {
                    "preparation": serialized_preparation,
                },
            },
        )
        package_error = str(
            package_result.error
            if package_result is not None
            else "runtime_package_builder_returned_no_result"
        )
        for prefix in (
            "BadParameter:",
            "ResumePathError:",
            "ValueError:",
        ):
            if package_error.startswith(prefix):
                package_error = package_error[len(prefix) :]
                break
        raise typer.BadParameter(
            package_error
        )
    summary = dict(package_result.output)
    summary["agent_runtime_id"] = agent_runtime_id
    final_observation = serialized_preparation[-1]["observations"][-1]
    summary["agent_handoff"] = {
        "observation_id": final_observation["observation_id"],
        "kind": final_observation["kind"],
        "source": final_observation["source"],
        "observed_at": final_observation["observed_at"],
        "payload": dict(final_observation.get("payload") or {}),
    }
    _write_json_atomic(
        trajectory_path,
        {
            "schema_version": 1,
            "application": {
                "application_id": summary.get("application_id"),
                "agent_runtime_id": summary["agent_runtime_id"],
                "company": job.company,
                "title": job.title,
            },
            "stages": {
                "preparation": serialized_preparation,
            },
        },
    )
    summary["agent_trajectory_path"] = str(trajectory_path)
    return summary


@app.command()
def init(db: Path = typer.Option(Path("job-agent.db"), "--db", help="SQLite database path.")) -> None:
    conn = connect(db)
    init_db(conn)
    typer.echo(f"Initialized database at {db}")


@examples_app.command("export")
def export_examples(
    out_dir: Path = typer.Option(Path("examples"), "--out-dir", help="Directory to write packaged example fixtures."),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files when their contents differ."),
) -> None:
    """Export offline fixtures that are bundled inside the installed package."""
    _export_example_fixtures(out_dir, force=force)
    typer.echo(f"Exported {len(EXAMPLE_FILENAMES)} example files to {out_dir}")


@examples_app.command("verify-offline")
def verify_offline_examples(
    out_dir: Path = typer.Option(
        Path("output/offline-verify"),
        "--out-dir",
        help="Directory to write exported fixtures, pipeline artifacts, and execution audit.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite packaged example fixtures if they already exist with different contents.",
    ),
    timeout_seconds: int = typer.Option(
        10,
        "--timeout-seconds",
        help="Per-application timeout for the fake-runtime execution smoke test.",
    ),
) -> None:
    """Run a local end-to-end smoke test with packaged fixtures and a fake Playwright runtime."""
    if timeout_seconds < 1:
        raise typer.BadParameter("--timeout-seconds must be greater than 0")
    if shutil.which("node") is None:
        raise typer.BadParameter("node executable is required for examples verify-offline")

    fixtures_dir = out_dir / "examples"
    pipeline_dir = out_dir / "pipeline-run"
    audit_path = out_dir / "execution-audit.json"

    _export_example_fixtures(fixtures_dir, force=force)
    example_resume_pdf = fixtures_dir / "sample-resume.pdf"
    example_resume_pdf.write_bytes(b"%PDF-1.4\n% job-agent offline fixture resume\n")
    run_application_pipeline(
        fixtures_dir / "offline-sources.json",
        out_dir=pipeline_dir,
        min_score=0,
        limit=None,
        resume_source_dir=fixtures_dir,
        resume=example_resume_pdf,
        profile=fixtures_dir / "profile.json",
        sensitive_kb=fixtures_dir / "sensitive-answers.json",
        db=pipeline_dir / "job-agent.db",
        use_llm=False,
        llm_model=None,
        llm_provider=None,
        llm_base_url=None,
        profile_vector_db=Path("profiles/gaoyi-profile.db"),
        required_resume_pdf=example_resume_pdf,
    )
    manifest_path = pipeline_dir / "pipeline-manifest.json"
    manifest = json.loads(manifest_path.read_text())

    summary_path = pipeline_dir / "applications" / "batch-summary.json"
    summary_items = json.loads(summary_path.read_text())
    for item in summary_items:
        package_dir = Path(str(item["package_dir"]))
        _write_fake_runtime_playwright(package_dir)

    offline_traces: list[dict[str, Any]] = []

    def capture_offline_loop(
        trace: dict[str, Any],
        _position: int,
        _total: int,
    ) -> None:
        offline_traces.append(trace)
        application = trace.get("application")
        script_path = (
            str(application.get("script_path") or "")
            if isinstance(application, Mapping)
            else ""
        )
        summary_item = next(
            (
                item
                for item in summary_items
                if str(item.get("runtime_script_path") or "") == script_path
            ),
            None,
        )
        if isinstance(summary_item, Mapping):
            _append_package_agent_trajectory(
                summary_item,
                stage="execution",
                payload=trace,
            )

    records = execute_application_batch(
        summary_items,
        timeout_seconds=timeout_seconds,
        use_gmail_verification=False,
        required_resume_pdf=example_resume_pdf,
        database_path=pipeline_dir / "job-agent.db",
        on_agent_loop=capture_offline_loop,
        unified_runtime=False,
    )
    _attach_resume_audit_fields(records, summary_items, example_resume_pdf)
    audit = {
        "schema_version": 1,
        "counts": summarize_execution(records),
        "submit_gate": SUBMIT_GATE,
        "required_resume_pdf": str(example_resume_pdf),
        "applications": records,
        "agent_runtime": {
            "schema_version": 1,
            "closed_loop": True,
            "applications": offline_traces,
        },
    }
    audit.update(_resume_pdf_audit_fields(example_resume_pdf, "required_resume_pdf"))
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, indent=2, ensure_ascii=True))
    from job_agent.daily_sop import (
        _build_evaluation_metrics,
        _write_agent_runtime_trace,
    )

    offline_state = {
        "run_id": "offline-verify",
        "phase": "offline_verified",
        "repair_attempts": [],
    }
    metrics = _build_evaluation_metrics(
        offline_state,
        manifest,
        audit,
        settings={},
        run_dir=out_dir,
    )
    metrics_path = out_dir / "evaluation-metrics.json"
    _write_json_atomic(metrics_path, metrics)
    runtime_trace_path = _write_agent_runtime_trace(
        out_dir,
        state=offline_state,
        manifest=manifest,
        audit=audit,
        metrics=metrics,
    )
    artifacts = manifest.setdefault("artifacts", {})
    artifacts["execution_audit"] = str(audit_path)
    artifacts["evaluation_metrics"] = str(metrics_path)
    artifacts["agent_runtime_trace"] = str(runtime_trace_path)
    _write_json_atomic(manifest_path, manifest)
    typer.echo(
        "Offline verification complete: "
        f"prepared {len(summary_items)} package(s), "
        f"submitted {audit['counts']['submitted']}, "
        f"completed {audit['counts']['completed']}, "
        f"failed {audit['counts']['failed']}. "
        f"Audit: {audit_path}"
    )


@resumes_app.command("index")
def index_resumes(source_dir: Path) -> None:
    templates = index_resume_templates(source_dir)
    for template in templates:
        typer.echo(f"{template.track}: pdf={template.pdf_path}")
    typer.echo(f"Indexed {len(templates)} PDF resumes")


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


@inbox_app.command("gmail-authorize")
def gmail_authorize(
    client_secret: Optional[Path] = typer.Option(None, "--client-secret", exists=True, readable=True, help="Google OAuth client JSON. Defaults to JOB_AGENT_GMAIL_CLIENT_SECRET_FILE."),
    token_out: Path = typer.Option(Path(".job-agent-secrets") / "gmail-token.json", "--token-out", help="Where to store the local refresh token."),
    open_browser: bool = typer.Option(True, "--open-browser/--no-open-browser", help="Open the OAuth URL automatically."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local OAuth callback port; use a fixed port for Web OAuth clients."),
) -> None:
    """Authorize read-only Gmail access once for automatic verification codes."""
    if client_secret is None:
        configured_client_secret = str(os.getenv("JOB_AGENT_GMAIL_CLIENT_SECRET_FILE") or "").strip()
        if configured_client_secret:
            client_secret = Path(configured_client_secret)
    if client_secret is None:
        raise typer.BadParameter(
            "Pass --client-secret or set JOB_AGENT_GMAIL_CLIENT_SECRET_FILE in .env."
        )
    if not client_secret.is_file():
        raise typer.BadParameter(f"Google OAuth client JSON not found: {client_secret}")
    token_out.parent.mkdir(parents=True, exist_ok=True)
    try:
        authorize_gmail(str(client_secret), str(token_out), open_browser=open_browser, port=port)
    except GmailVerificationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Stored Gmail read-only token at {token_out}")


@applications_app.command("prepare")
def prepare_application(
    jobs_file: Path,
    index: int = typer.Option(1, "--index", help="1-based job index in the normalized jobs JSON file."),
    out_dir: Path = typer.Option(Path("application-package"), "--out-dir", help="Application package output directory."),
    resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--resume-source-dir",
        help="Directory containing original PDF resumes; the closest one for the JD is uploaded unchanged.",
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
    sensitive_kb: Optional[Path] = typer.Option(
        None,
        "--sensitive-kb",
        help="Optional sensitive-answer knowledge base JSON (pre-approved answers).",
    ),
    resume: Optional[Path] = typer.Option(
        None,
        "--resume",
        help="Optional PDF resume file to upload unchanged.",
    ),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require this package to upload the specified existing PDF resume path.",
    ),
    upload_resume: bool = typer.Option(
        False,
        "--upload-resume",
        help="Deprecated compatibility option. A selected uploadable resume is uploaded automatically.",
        hidden=True,
    ),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
    profile_vector_db: Optional[Path] = typer.Option(
        Path("profiles/gaoyi-profile.db"),
        "--profile-vector-db",
        help="Optional private profile vector SQLite DB for job-scoped screening answers.",
    ),
) -> None:
    raw_jobs = json.loads(jobs_file.read_text())
    if index < 1 or index > len(raw_jobs):
        raise typer.BadParameter(f"--index must be between 1 and {len(raw_jobs)}")
    job = _job_from_dict(raw_jobs[index - 1])
    previous_submission = _previous_submission_reason_for_job(job, db)
    if previous_submission:
        raise typer.BadParameter(f"Refusing to prepare duplicate application: {previous_submission}")
    _prepare_application_package(
        job,
        out_dir,
        resume_source_dir=resume_source_dir,
        db=db,
        form_snapshot=form_snapshot,
        profile=profile,
        sensitive_kb=sensitive_kb,
        resume=resume,
        upload_resume=upload_resume,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        profile_vector_db=profile_vector_db,
        required_resume_pdf=required_resume_pdf,
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
        help="Directory containing original PDF resumes; the closest one for each JD is uploaded unchanged.",
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
    sensitive_kb: Optional[Path] = typer.Option(
        None,
        "--sensitive-kb",
        help="Optional sensitive-answer knowledge base JSON (pre-approved answers).",
    ),
    resume: Optional[Path] = typer.Option(
        None,
        "--resume",
        help="Optional PDF resume file to upload unchanged.",
    ),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require every prepared package to upload the specified existing PDF resume path.",
    ),
    upload_resume: bool = typer.Option(
        False,
        "--upload-resume",
        help="Deprecated compatibility option. A selected uploadable resume is uploaded automatically.",
        hidden=True,
    ),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
    profile_vector_db: Optional[Path] = typer.Option(
        Path("profiles/gaoyi-profile.db"),
        "--profile-vector-db",
        help="Optional private profile vector SQLite DB for job-scoped screening answers.",
    ),
) -> None:
    if limit is not None and limit < 1:
        raise typer.BadParameter("--limit must be greater than 0")
    raw_jobs = json.loads(jobs_file.read_text())
    selected_raw_jobs = raw_jobs[:limit] if limit is not None else raw_jobs
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    skipped_duplicates = 0
    skipped_terminal_outcomes = 0
    for index, raw_job in enumerate(selected_raw_jobs, start=1):
        job = _job_from_dict(raw_job)
        previous_submission = _previous_submission_reason_for_job(job, db)
        if previous_submission:
            skipped_duplicates += 1
            typer.echo(f"Skipped duplicate submitted application: {previous_submission}")
            continue
        previous_terminal_outcome = _previous_terminal_outcome_reason_for_job(job, db)
        if previous_terminal_outcome:
            skipped_terminal_outcomes += 1
            typer.echo(f"Skipped prior terminal application outcome: {previous_terminal_outcome}")
            continue
        package_dir = out_dir / _review_slug(index, job)
        summary = _prepare_application_package(
            job,
            package_dir,
            resume_source_dir=resume_source_dir,
            db=db,
            form_snapshot=form_snapshot,
            profile=profile,
            sensitive_kb=sensitive_kb,
            resume=resume,
            upload_resume=upload_resume,
            use_llm=use_llm,
            llm_model=llm_model,
            llm_provider=llm_provider,
            llm_base_url=llm_base_url,
            profile_vector_db=profile_vector_db,
            required_resume_pdf=required_resume_pdf,
        )
        summary["index"] = str(index)
        summaries.append(summary)

    summary_path = out_dir / "batch-summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, ensure_ascii=True))
    message = f"Prepared {len(summaries)} application packages at {out_dir}"
    if skipped_duplicates:
        message += f" ({skipped_duplicates} duplicate submitted skipped)"
    if skipped_terminal_outcomes:
        message += f" ({skipped_terminal_outcomes} prior terminal outcome skipped)"
    typer.echo(message)


@applications_app.command("build-batch-runner")
def build_batch_runner(
    summary: Path,
    out: Path = typer.Option(Path("run-application-batch.js"), "--out", help="JavaScript runner output path."),
    resume_preflight_out: Optional[Path] = typer.Option(
        None,
        "--resume-preflight-out",
        help="Resume verification JSON path. Defaults to <runner-dir>/resume-preflight.json.",
    ),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require the generated runner to upload this existing external PDF resume path.",
    ),
    required_resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--required-resume-source-dir",
        help="Require every uploaded PDF resume to come from this source directory.",
    ),
) -> None:
    summary_items = json.loads(summary.read_text())
    preflight_out = resume_preflight_out or (out.parent / "resume-preflight.json")
    preflight = _write_resume_preflight(
        summary_items,
        preflight_out,
        required_resume_pdf=required_resume_pdf,
        required_resume_source_dir=required_resume_source_dir,
    )
    if preflight["counts"]["invalid"]:
        typer.echo(
            f"Refusing to build runner: resume preflight has "
            f"{preflight['counts']['invalid']} invalid package(s). Report: {preflight_out}"
        )
        raise typer.Exit(code=1)
    required_resume = _validate_required_resume_pdf(required_resume_pdf)
    required_resume_metadata = None
    if required_resume is not None:
        required_resume_metadata = {
            "path": str(required_resume),
            "sha256": _sha256_file(required_resume),
        }
    required_source_dir = _validate_required_resume_source_dir(required_resume_source_dir)
    required_source_dir_metadata = None
    if required_source_dir is not None:
        required_source_dir_metadata = {
            "path": str(required_source_dir),
        }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_batch_fill_runner(
            summary_items,
            required_resume_pdf=required_resume_metadata,
            required_resume_source_dir=required_source_dir_metadata,
        )
    )
    typer.echo(
        f"Wrote guarded batch runner to {out}. "
        f"Resume preflight: {preflight['counts']['verified']} verified at {preflight_out}"
    )


@applications_app.command("verify-resumes")
def verify_application_resumes(
    summary: Path,
    out: Path = typer.Option(
        Path("resume-preflight.json"),
        "--out",
        help="JSON path for pre-execution resume verification evidence.",
    ),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require every package to upload this existing external PDF resume path.",
    ),
    required_resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--required-resume-source-dir",
        help="Require every uploaded PDF resume to come from this source directory.",
    ),
) -> None:
    """Verify resume upload paths without opening browsers or submitting applications."""
    summary_items = json.loads(summary.read_text())
    preflight = _write_resume_preflight(
        summary_items,
        out,
        required_resume_pdf=required_resume_pdf,
        required_resume_source_dir=required_resume_source_dir,
    )
    counts = preflight["counts"]
    message = (
        f"Verified resume uploads for {counts['total']} application(s): "
        f"{counts['verified']} verified, {counts['invalid']} invalid. Report: {out}"
    )
    typer.echo(message)
    if counts["invalid"]:
        raise typer.Exit(code=1)


@applications_app.command("execute-batch")
def execute_batch(
    summary: Path,
    audit_out: Path = typer.Option(Path("execution-audit.json"), "--audit-out", help="Privacy-safe execution audit JSON path."),
    resume_preflight_out: Optional[Path] = typer.Option(
        None,
        "--resume-preflight-out",
        help="Pre-execution resume verification JSON path. Defaults to <audit-dir>/resume-preflight.json.",
    ),
    node_binary: str = typer.Option("node", "--node-binary", help="Node.js executable used to run Playwright scripts."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Per-application execution timeout."),
    db: Optional[Path] = typer.Option(None, "--db", help="Optional SQLite tracking database to update with runtime outcomes."),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require every executed application to upload this existing external PDF resume path.",
    ),
    required_resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--required-resume-source-dir",
        help="Require every uploaded PDF resume to come from this source directory.",
    ),
    headless: Optional[bool] = typer.Option(
        None,
        "--headless/--headed",
        help="Override BROWSER_HEADLESS for this execution only.",
    ),
    retry_prior_terminal_outcome: bool = typer.Option(
        False,
        "--retry-prior-terminal-outcome",
        help="Explicitly retry DB-tracked non-submitted terminal outcomes such as verification/captcha failures.",
    ),
    resume_existing_audit: bool = typer.Option(
        False,
        "--resume-existing-audit",
        help=(
            "Resume one incomplete canonical audit: preserve terminal records, "
            "quarantine the interrupted item, and execute only later unrecorded items."
        ),
    ),
    llm_answers: Optional[bool] = typer.Option(
        None,
        "--llm-answers/--no-llm-answers",
        help=(
            "Override JOB_AGENT_LLM_ANSWERS for this execution. When enabled, "
            "unknown non-sensitive screening questions can be answered from profile facts."
        ),
    ),
) -> None:
    """Execute runtime autofill scripts with automatic submit when no blockers remain."""
    if timeout_seconds < 1:
        raise typer.BadParameter("--timeout-seconds must be greater than 0")
    if resume_existing_audit and retry_prior_terminal_outcome:
        raise typer.BadParameter(
            "--resume-existing-audit cannot be combined with "
            "--retry-prior-terminal-outcome"
        )
    summary_items = json.loads(summary.read_text())
    preflight_out = resume_preflight_out or (audit_out.parent / "resume-preflight.json")
    preflight = _write_resume_preflight(
        summary_items,
        preflight_out,
        required_resume_pdf=required_resume_pdf,
        required_resume_source_dir=required_resume_source_dir,
    )
    with _temporary_llm_answers_env(llm_answers):
        audit = _write_execution_audit(
            summary_items,
            audit_out,
            node_binary=node_binary,
            timeout_seconds=timeout_seconds,
            db=db,
            browser_headless=headless,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=required_resume_source_dir,
            resume_preflight_failed=bool(preflight["counts"]["invalid"]),
            retry_prior_terminal_outcome=retry_prior_terminal_outcome,
            resume_existing_audit=resume_existing_audit,
        )
    preflight_message = (
        "preflight failed; no browser runtime executed"
        if preflight["counts"]["invalid"]
        else "preflight passed"
    )
    typer.echo(
        f"Executed {audit['counts']['total']} application scripts: "
        f"{audit['counts']['completed']} completed, {audit['counts']['submitted']} submitted, "
        f"{audit['counts']['submit_clicked_unconfirmed']} clicked-unconfirmed, "
        f"{audit['counts']['email_verification_required']} email-verification-required, "
        f"{audit['counts']['submission_processing_error']} processing-error, "
        f"{audit['counts']['submission_blocked_by_anti_spam']} anti-spam-blocked, "
        f"{audit['counts']['failed']} failed, "
        f"{audit['counts']['skipped']} skipped. "
        f"Resume preflight: {preflight['counts']['verified']} verified, "
        f"{preflight['counts']['invalid']} invalid at {preflight_out} ({preflight_message}). "
        f"Audit: {audit_out}"
    )


@applications_app.command("reconcile-confirmed")
def reconcile_confirmed_applications(
    summary: Path,
    db: Path = typer.Option(..., "--db", help="SQLite tracking database to update."),
) -> None:
    """Recover submitted tracker rows only from verified confirmation artifacts."""
    result = _reconcile_confirmed_evidence(json.loads(summary.read_text()), db)
    typer.echo(
        f"Reconciled {result['confirmed_evidence']} confirmed evidence file(s): "
        f"{result['updated']} submitted record(s) updated, {result['created']} created."
    )


@applications_app.command("reconcile-root")
def reconcile_confirmation_roots(
    roots: list[Path] = typer.Argument(..., exists=True, file_okay=False, readable=True),
    db: Path = typer.Option(..., "--db", help="SQLite tracking database to update."),
) -> None:
    """Reconcile terminal confirmation evidence found below one or more package roots."""
    summaries = _summaries_from_confirmation_roots(roots)
    result = _reconcile_confirmed_evidence(summaries, db)
    typer.echo(
        f"Scanned {len(roots)} root(s), found {len(summaries)} confirmed package(s): "
        f"{result['updated']} submitted record(s) updated, {result['created']} created."
    )


@applications_app.command("reconcile-page-evidence")
def reconcile_page_evidence(
    package_dir: Path,
    db: Path = typer.Option(..., "--db", help="SQLite tracking database to update."),
) -> None:
    """Recover a verified submission from a saved post-click browser page."""
    if not _promote_saved_page_confirmation(package_dir, db):
        raise typer.BadParameter("saved page evidence does not contain a verified submission confirmation")
    typer.echo(f"Recovered verified submission from saved page evidence: {package_dir}")


@applications_app.command("execute-package")
def execute_application_package(
    package_dir: Path,
    db: Optional[Path] = typer.Option(None, "--db", help="Optional SQLite tracking database to update with the runtime outcome."),
    audit_out: Optional[Path] = typer.Option(None, "--audit-out", help="Optional execution audit JSON path. Defaults to <package-dir>/execution-audit.json."),
    resume_preflight_out: Optional[Path] = typer.Option(
        None,
        "--resume-preflight-out",
        help="Optional resume verification JSON path. Defaults to <package-dir>/resume-preflight.json.",
    ),
    node_binary: str = typer.Option("node", "--node-binary", help="Node.js executable used to run Playwright scripts."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Runtime timeout."),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require this package to upload the specified existing external PDF resume path.",
    ),
    required_resume_source_dir: Optional[Path] = typer.Option(
        None,
        "--required-resume-source-dir",
        help="Require this package to upload a PDF resume from this source directory.",
    ),
    headless: Optional[bool] = typer.Option(
        None,
        "--headless/--headed",
        help="Override BROWSER_HEADLESS for this execution only.",
    ),
    retry_prior_terminal_outcome: bool = typer.Option(
        False,
        "--retry-prior-terminal-outcome",
        help="Explicitly retry a non-submitted terminal outcome recorded for this package or DB row.",
    ),
    llm_answers: Optional[bool] = typer.Option(
        None,
        "--llm-answers/--no-llm-answers",
        help=(
            "Override JOB_AGENT_LLM_ANSWERS for this execution. When enabled, "
            "unknown non-sensitive screening questions can be answered from profile facts."
        ),
    ),
) -> None:
    """Execute one prepared package and persist its verified runtime state."""
    if timeout_seconds < 1:
        raise typer.BadParameter("--timeout-seconds must be greater than 0")
    audit_path = audit_out or (package_dir / "execution-audit.json")
    if _audit_records_submission(audit_path):
        typer.echo(f"Skipped already submitted package. Audit: {audit_path}")
        return
    if not retry_prior_terminal_outcome and _audit_blocks_automatic_retry(audit_path):
        typer.echo(f"Skipped package blocked by a prior execution outcome. Audit: {audit_path}")
        return

    existing_lock = package_dir / ".execution.lock"
    stale_lock = _has_stale_package_execution_lock(package_dir)
    if existing_lock.is_file() and not stale_lock:
        # Preserve the concurrency gate even when the package is incomplete.
        # The helper raises the user-facing error for a live or malformed lock.
        acquired_lock = _acquire_package_execution_lock(package_dir)
        acquired_lock.unlink(missing_ok=True)

    summary = _execution_summary_for_package(package_dir)
    previous_submission = _previous_submission_reason_for_summary(summary, db)
    if previous_submission:
        typer.echo(f"Skipped package already submitted in DB: {previous_submission}")
        return
    attempt_path = _execution_attempt_path(package_dir)
    if attempt_path.is_file():
        attempt_path.unlink(missing_ok=True)
        _write_interrupted_execution_audit(
            summary,
            audit_path,
            db=db,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=required_resume_source_dir,
        )
        typer.echo(
            "Skipped package after an interrupted execution with unconfirmed outcome. "
            f"Audit: {audit_path}"
        )
        return

    if stale_lock:
        (package_dir / ".execution.lock").unlink(missing_ok=True)
        _write_interrupted_execution_audit(
            summary,
            audit_path,
            db=db,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=required_resume_source_dir,
        )
        typer.echo(
            "Skipped package after an interrupted execution with unconfirmed outcome. "
            f"Audit: {audit_path}"
        )
        return

    lock_path = _acquire_package_execution_lock(package_dir)
    audit_persisted = False
    try:
        # A peer can complete between the initial audit check and lock acquisition.
        if _audit_records_submission(audit_path):
            typer.echo(f"Skipped already submitted package. Audit: {audit_path}")
            return
        if not retry_prior_terminal_outcome and _audit_blocks_automatic_retry(audit_path):
            typer.echo(f"Skipped package blocked by a prior execution outcome. Audit: {audit_path}")
            return
        attempt_path = _write_execution_attempt(package_dir, summary)
        preflight_path = resume_preflight_out or (package_dir / "resume-preflight.json")
        preflight = _write_resume_preflight(
            [summary],
            preflight_path,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=required_resume_source_dir,
        )
        with _temporary_llm_answers_env(llm_answers):
            audit = _write_execution_audit(
                [summary],
                audit_path,
                node_binary=node_binary,
                timeout_seconds=timeout_seconds,
                db=db,
                browser_headless=headless,
                required_resume_pdf=required_resume_pdf,
                required_resume_source_dir=required_resume_source_dir,
                resume_preflight_failed=bool(preflight["counts"]["invalid"]),
                retry_prior_terminal_outcome=retry_prior_terminal_outcome,
            )
        audit_persisted = True
        record = audit["applications"][0]
        preflight_message = (
            "preflight failed; no browser runtime executed"
            if preflight["counts"]["invalid"]
            else "preflight passed"
        )
        typer.echo(
            f"Executed {summary['company']} - {summary['title']}: {record['status']}. "
            f"Resume preflight: {preflight['counts']['verified']} verified, "
            f"{preflight['counts']['invalid']} invalid at {preflight_path} ({preflight_message}). "
            f"Audit: {audit_path}"
        )
    finally:
        if audit_persisted:
            attempt_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


@pipeline_app.command("run")
def run_application_pipeline(
    config_file: Path,
    out_dir: Path = typer.Option(Path("pipeline-run"), "--out-dir", help="Root directory for all pipeline artifacts."),
    min_score: int = typer.Option(70, "--min-score", help="Minimum fit score to prepare."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of applications to prepare."),
    resume_source_dir: Optional[Path] = typer.Option(None, "--resume-source-dir", help="Directory containing original PDF resumes."),
    resume: Optional[Path] = typer.Option(None, "--resume", help="Optional PDF resume file to upload unchanged."),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require every prepared package to upload the specified existing PDF resume path.",
    ),
    profile: Optional[Path] = typer.Option(None, "--profile", help="Approved application profile JSON."),
    sensitive_kb: Optional[Path] = typer.Option(None, "--sensitive-kb", help="Optional sensitive-answer knowledge base JSON (pre-approved answers)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Optional SQLite application tracking database."),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for JD review notes."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model"),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider"),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url"),
    profile_vector_db: Optional[Path] = typer.Option(
        Path("profiles/gaoyi-profile.db"),
        "--profile-vector-db",
        help="Optional private profile vector SQLite DB for job-scoped screening answers.",
    ),
) -> None:
    """Import, deduplicate, rank, select original PDFs, and package jobs in one guarded run."""
    if min_score < 0 or min_score > 100:
        raise typer.BadParameter("--min-score must be between 0 and 100")
    if limit is not None and limit < 1:
        raise typer.BadParameter("--limit must be greater than 0")
    manifest = _run_pipeline(
        config_file,
        out_dir=out_dir,
        min_score=min_score,
        limit=limit,
        resume_source_dir=resume_source_dir,
        resume=resume,
        required_resume_pdf=required_resume_pdf,
        profile=profile,
        sensitive_kb=sensitive_kb,
        db=db,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        profile_vector_db=profile_vector_db,
    )
    typer.echo(
        f"Pipeline imported {manifest['counts']['imported']}, shortlisted {manifest['counts']['shortlisted']}, "
        f"and prepared {manifest['counts']['prepared']} applications at {out_dir}"
    )


@pipeline_app.command("init-workspace")
def init_pipeline_workspace(
    out_dir: Path = typer.Option(
        Path("job-agent-workspace"),
        "--out-dir",
        help="Directory to initialize with profile, source config, resume folder, and runbook files.",
    ),
    resume: Optional[Path] = typer.Option(
        None,
        "--resume",
        help="Optional base resume text/markdown file to parse into the initial profile.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite generated files when they already exist.",
    ),
    job_track: str = typer.Option(
        "Agent Engineer",
        "--job-track",
        help="Primary target track for starter search queries and workspace instructions.",
    ),
) -> None:
    """Scaffold a personal job-application workspace for repeated pipeline runs."""
    normalized_track = _normalize_job_track(job_track)
    out_dir.mkdir(parents=True, exist_ok=True)
    resumes_dir = out_dir / "resumes"
    output_dir = out_dir / "output"
    profile_path = out_dir / "profile.json"
    sensitive_kb_path = out_dir / "sensitive-answers.json"
    sources_path = out_dir / "sources.json"
    readme_path = out_dir / "WORKSPACE.md"
    resume_readme_path = resumes_dir / "README.txt"

    resumes_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    profile_payload = (
        parse_resume_to_profile(_read_resume_text(resume)).to_dict()
        if resume is not None
        else render_profile_template()
    )
    generated_files: list[tuple[Path, str]] = [
        (profile_path, json.dumps(profile_payload, indent=2, ensure_ascii=False) + "\n"),
        (
            sensitive_kb_path,
            json.dumps(render_sensitive_kb_template(), indent=2, ensure_ascii=False) + "\n",
        ),
        (
            sources_path,
            json.dumps(
                _render_source_config_template_for_track(normalized_track),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        ),
        (
            readme_path,
            _render_workspace_readme(
                out_dir,
                profile_path,
                sensitive_kb_path,
                sources_path,
                job_track=normalized_track,
            ),
        ),
        (
            resume_readme_path,
            "Put role-specific resume files here, for example:\n"
            "GAOYI_WU_Agent_Engineer.pdf\n"
            "GAOYI_WU_ML_Infra.pdf\n"
            "GAOYI_WU_Data_Scientist.pdf\n",
        ),
    ]
    for path, content in generated_files:
        if path.exists() and not force:
            raise typer.BadParameter(f"{path} exists; pass --force to overwrite it")
        path.write_text(content)

    typer.echo(
        f"Initialized job-agent workspace at {out_dir} "
        f"for {normalized_track} with profile, sensitive answers, source config, resumes/, and WORKSPACE.md"
    )


@pipeline_app.command("run-execute")
def run_and_execute_application_pipeline(
    config_file: Path,
    out_dir: Path = typer.Option(Path("pipeline-run"), "--out-dir", help="Root directory for all pipeline and execution artifacts."),
    min_score: int = typer.Option(70, "--min-score", help="Minimum fit score to prepare."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of applications to prepare."),
    resume_source_dir: Optional[Path] = typer.Option(None, "--resume-source-dir", help="Directory containing original PDF resumes."),
    resume: Optional[Path] = typer.Option(None, "--resume", help="Optional PDF resume file to upload unchanged."),
    profile: Optional[Path] = typer.Option(None, "--profile", help="Approved application profile JSON."),
    sensitive_kb: Optional[Path] = typer.Option(None, "--sensitive-kb", help="Optional sensitive-answer knowledge base JSON (pre-approved answers)."),
    db: Optional[Path] = typer.Option(None, "--db", help="Optional SQLite application tracking database."),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for JD review notes."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model"),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider"),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url"),
    profile_vector_db: Optional[Path] = typer.Option(
        Path("profiles/gaoyi-profile.db"),
        "--profile-vector-db",
        help="Optional private profile vector SQLite DB for job-scoped screening answers.",
    ),
    audit_out: Optional[Path] = typer.Option(
        None,
        "--audit-out",
        help="Optional execution audit JSON path. Defaults to <out-dir>/execution-audit.json.",
    ),
    resume_preflight_out: Optional[Path] = typer.Option(
        None,
        "--resume-preflight-out",
        help="Optional resume verification JSON path. Defaults to <out-dir>/resume-preflight.json.",
    ),
    node_binary: str = typer.Option("node", "--node-binary", help="Node.js executable used to run Playwright scripts."),
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", help="Per-application execution timeout."),
    required_resume_pdf: Optional[Path] = typer.Option(
        None,
        "--required-resume-pdf",
        help="Require every executed application to upload this existing external PDF resume path.",
    ),
    llm_answers: Optional[bool] = typer.Option(
        None,
        "--llm-answers/--no-llm-answers",
        help=(
            "Override runtime LLM fallback for unknown non-sensitive screening questions. "
            "Defaults to enabled when --use-llm is set."
        ),
    ),
) -> None:
    """Run the guarded pipeline and immediately execute the generated runtime batch."""
    if min_score < 0 or min_score > 100:
        raise typer.BadParameter("--min-score must be between 0 and 100")
    if limit is not None and limit < 1:
        raise typer.BadParameter("--limit must be greater than 0")
    if timeout_seconds < 1:
        raise typer.BadParameter("--timeout-seconds must be greater than 0")

    manifest = _run_pipeline(
        config_file,
        out_dir=out_dir,
        min_score=min_score,
        limit=limit,
        resume_source_dir=resume_source_dir,
        resume=resume,
        required_resume_pdf=required_resume_pdf,
        profile=profile,
        sensitive_kb=sensitive_kb,
        db=db,
        use_llm=use_llm,
        llm_model=llm_model,
        llm_provider=llm_provider,
        llm_base_url=llm_base_url,
        profile_vector_db=profile_vector_db,
    )
    summary_path = Path(str(manifest["artifacts"]["batch_summary"]))
    summary_items = json.loads(summary_path.read_text())
    execution_resume_source_dir = (
        Path(str(manifest["required_resume_source_dir"]))
        if manifest.get("required_resume_source_dir")
        else resume_source_dir
    )
    resolved_audit_out = audit_out or (out_dir / "execution-audit.json")
    resolved_preflight_out = resume_preflight_out or (out_dir / "resume-preflight.json")
    preflight = _write_resume_preflight(
        summary_items,
        resolved_preflight_out,
        required_resume_pdf=required_resume_pdf,
        required_resume_source_dir=execution_resume_source_dir,
    )
    effective_llm_answers = use_llm if llm_answers is None else llm_answers
    with _temporary_llm_answers_env(effective_llm_answers):
        audit = _write_execution_audit(
            summary_items,
            resolved_audit_out,
            node_binary=node_binary,
            timeout_seconds=timeout_seconds,
            db=db,
            required_resume_pdf=required_resume_pdf,
            required_resume_source_dir=execution_resume_source_dir,
            resume_preflight_failed=bool(preflight["counts"]["invalid"]),
        )
    manifest["artifacts"]["execution_audit"] = str(resolved_audit_out)
    manifest["artifacts"]["resume_preflight"] = str(resolved_preflight_out)
    manifest["execution_counts"] = audit["counts"]
    manifest["resume_preflight_counts"] = preflight["counts"]
    manifest["runtime_llm_answers_enabled"] = effective_llm_answers
    manifest_path = out_dir / "pipeline-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True))
    preflight_message = (
        "preflight failed; no browser runtime executed"
        if preflight["counts"]["invalid"]
        else "preflight passed"
    )
    typer.echo(
        f"Pipeline imported {manifest['counts']['imported']}, shortlisted {manifest['counts']['shortlisted']}, "
        f"prepared {manifest['counts']['prepared']}, and executed {audit['counts']['total']} applications at {out_dir}. "
        f"Submitted {audit['counts']['submitted']}, completed {audit['counts']['completed']}, failed {audit['counts']['failed']}. "
        f"Resume preflight: {preflight['counts']['verified']} verified, "
        f"{preflight['counts']['invalid']} invalid at {resolved_preflight_out} ({preflight_message}). "
        f"Audit: {resolved_audit_out}"
    )


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
    sensitive_kb: Optional[Path] = typer.Option(
        None,
        "--sensitive-kb",
        help="Optional sensitive-answer knowledge base JSON (pre-approved answers).",
    ),
) -> None:
    profile_facts = _load_profile_facts(profile, sensitive_kb) or {}
    if resume_file:
        try:
            profile_facts["resume_file"] = str(
                resolve_original_resume_pdf(
                    resume_file,
                    source_dir=_configured_resume_source_dir(),
                )
            )
        except ResumePathError as exc:
            raise typer.BadParameter(str(exc)) from exc
    plan = build_form_fill_plan(
        inspect_form_snapshot(form_snapshot.read_text()),
        profile_facts,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_playwright_fill_script(plan, application_url=application_url))
    typer.echo(f"Wrote guarded form-fill script to {out}")


@forms_app.command("init-profile")
def forms_init_profile(
    out: Path = typer.Option(Path("profile.json"), "--out", help="Rich profile output path."),
) -> None:
    """Generate a Simplify-style rich profile template.

    Fill in contact, work_history (multiple entries), education, links,
    demographics/EEO, and an answers bank. Then pass it to ``forms autofill
    --profile`` so the runtime engine can fill multi-entry work/education
    sections, screening questions, and sensitive fields.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(render_profile_template(), indent=2, ensure_ascii=False) + "\n")
    typer.echo(f"Wrote rich profile template to {out}")


@forms_app.command("build-profile-from-resume")
def forms_build_profile_from_resume(
    resume: Path = typer.Option(..., "--resume", help="Resume text/markdown file to parse into a profile."),
    out: Path = typer.Option(Path("profile.json"), "--out", help="Rich profile output path."),
) -> None:
    """Parse a resume's text into a structured rich profile (Simplify imports
    your resume at sign-up). Edit the result before using it to autofill."""
    profile = parse_resume_to_profile(_read_resume_text(resume))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False) + "\n")
    typer.echo(f"Wrote parsed profile to {out}")
    typer.echo(f"work_history entries: {len(profile.work_history)} | education entries: {len(profile.education)}")


@forms_app.command("init-sensitive-kb")
def forms_init_sensitive_kb(
    out: Path = typer.Option(Path("sensitive-answers.json"), "--out", help="Sensitive answer knowledge base output path."),
) -> None:
    """Generate a fill-in template for the sensitive-answer knowledge base.

    Fill in each ``answer`` and set ``approved: true`` for the entries you want
    auto-filled (work authorization, sponsorship, salary, relocation, etc.).
    Then pass the file to ``forms autofill --sensitive-kb`` /
    ``forms build-script --sensitive-kb`` so those fields are filled
    automatically instead of left for manual review.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(render_sensitive_kb_template(), indent=2, ensure_ascii=False) + "\n")
    typer.echo(f"Wrote sensitive-answer knowledge base template to {out}")
    typer.echo("Fill each 'answer' and set 'approved: true' for entries to auto-fill.")


@forms_app.command("autofill")
def forms_autofill(
    profile: Path = typer.Option(
        ...,
        "--profile",
        help="JSON file containing approved profile facts and an `answers` bank.",
    ),
    out: Path = typer.Option(Path("autofill.js"), "--out", help="Runtime autofill script output path."),
    application_url: Optional[str] = typer.Option(
        None,
        "--application-url",
        help="Application page URL to open and autofill at runtime.",
    ),
    resume_file: Optional[Path] = typer.Option(
        None,
        "--resume-file",
        help="Approved resume file path for Resume/CV upload fields.",
    ),
    sensitive_kb: Optional[Path] = typer.Option(
        None,
        "--sensitive-kb",
        help="Optional sensitive-answer knowledge base JSON (pre-approved answers).",
    ),
    headless: Optional[bool] = typer.Option(
        None,
        "--headless/--headed",
        help="Run the browser headless or with a visible window. Defaults to BROWSER_HEADLESS.",
    ),
    max_pages: int = typer.Option(
        12,
        "--max-pages",
        help="Safety cap for multi-page application navigation.",
    ),
) -> None:
    """Generate a Simplify-style generic runtime autofill script.

    Unlike the per-snapshot fill script, this emits a single generic Playwright
    script that live-scrapes the application page DOM, maps fields to the
    profile, answers screening questions from the `answers` bank and the
    sensitive-answer knowledge base, advances through multi-page forms, uploads
    the resume, and submits automatically when no blocking review fields remain.
    """
    profile_facts = _load_profile_facts(profile, sensitive_kb) or {}
    selected_resume_file = None
    selected_resume_source_dir = None
    if resume_file:
        selected_resume_source_dir = _configured_resume_source_dir()
        try:
            selected_resume_file = resolve_original_resume_pdf(
                resume_file,
                source_dir=selected_resume_source_dir,
                package_dir=out.parent,
            )
        except ResumePathError as exc:
            raise typer.BadParameter(str(exc)) from exc
    script = render_runtime_autofill_script(
        profile=profile_facts,
        resume_file=str(selected_resume_file) if selected_resume_file else None,
        resume_source_dir=str(selected_resume_source_dir) if selected_resume_source_dir else None,
        application_url=application_url,
        max_pages=max_pages,
        headless=_runtime_browser_headless() if headless is None else headless,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script)
    typer.echo(f"Wrote Simplify-style runtime autofill script to {out}")


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
    sensitive_kb: Optional[Path] = typer.Option(
        None,
        "--sensitive-kb",
        help="Optional sensitive-answer knowledge base JSON (pre-approved answers).",
    ),
    use_llm: bool = typer.Option(False, "--use-llm", help="Use configured HelloAgentsLLM for LLM-backed steps."),
    llm_model: Optional[str] = typer.Option(None, "--llm-model", help="LLM model id. Defaults to LLM_MODEL_ID or provider default."),
    llm_provider: Optional[str] = typer.Option(None, "--llm-provider", help="Optional provider name, such as openai."),
    llm_base_url: Optional[str] = typer.Option(None, "--llm-base-url", help="Optional OpenAI-compatible base URL."),
) -> None:
    form_snapshot_json = form_snapshot.read_text() if form_snapshot else None
    profile_facts = _load_profile_facts(profile, sensitive_kb)
    profile_json = json.dumps(profile_facts, ensure_ascii=True) if profile_facts is not None else None
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


@jobs_app.command("import-ashby")
def import_ashby_jobs(
    organization: str,
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Ashby JSON payload. If omitted, fetches the public API.",
    ),
    out: Path = typer.Option(Path("jobs.json"), "--out", help="JSON output path."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional maximum number of jobs to import."),
) -> None:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{organization}"
    jobs = parse_ashby_jobs(_read_json_source(payload, url), organization=organization, limit=limit)
    _write_jobs_json(jobs, out)
    typer.echo(f"Imported {len(jobs)} jobs to {out}")


@jobs_app.command("review-ashby")
def review_ashby_jobs(
    organization: str,
    payload: Optional[Path] = typer.Option(
        None,
        "--payload",
        help="Optional local Ashby JSON payload. If omitted, fetches the public API.",
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
    url = f"https://api.ashbyhq.com/posting-api/job-board/{organization}"
    jobs = parse_ashby_jobs(_read_json_source(payload, url), organization=organization, limit=limit)
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


@profiles_app.command("export-chunks")
def profiles_export_chunks(
    db: Path = typer.Option(Path("profiles/gaoyi-profile.db"), "--db", help="Profile SQLite database path."),
    out: Path = typer.Option(Path("profiles/gaoyi-profile-chunks.jsonl"), "--out", help="JSONL chunk output path."),
) -> None:
    """Export profile documents as JSONL chunks for embedding pipelines."""
    count = export_profile_chunks(db, out)
    typer.echo(f"Exported {count} profile chunks to {out}")


@profiles_app.command("sync-docs")
def profiles_sync_docs(
    db: Path = typer.Option(Path("profiles/gaoyi-profile.db"), "--db", help="Profile SQLite database path."),
) -> None:
    """Sync profile facts and approved answer-bank entries into vector documents."""
    count = sync_profile_summary_documents(db)
    typer.echo(f"Synced {count} profile summary documents to {db}")


@profiles_app.command("embed")
def profiles_embed(
    db: Path = typer.Option(Path("profiles/gaoyi-profile.db"), "--db", help="Profile SQLite database path."),
    model: Optional[str] = typer.Option(None, "--model", help="Embedding model id. Defaults to EMBEDDING_MODEL_ID or text-embedding-3-small."),
    provider: str = typer.Option("openai", "--provider", help="Embedding provider: openai or local."),
    no_fallback: bool = typer.Option(False, "--no-fallback", help="Fail instead of falling back to local hash embeddings."),
) -> None:
    """Generate and store embeddings for profile chunks."""
    result = index_profile_embeddings(
        db,
        model=model,
        provider=provider,
        fallback_local=not no_fallback,
    )
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


@profiles_app.command("search")
def profiles_search(
    query: str = typer.Argument(..., help="Search query over profile chunks."),
    db: Path = typer.Option(Path("profiles/gaoyi-profile.db"), "--db", help="Profile SQLite database path."),
    top_k: int = typer.Option(5, "--top-k", help="Number of chunks to return."),
) -> None:
    """Search profile chunks using stored embeddings."""
    results = search_profile_embeddings(db, query=query, top_k=top_k)
    typer.echo(json.dumps(results, indent=2, ensure_ascii=False))


app.add_typer(applications_app, name="applications")
app.add_typer(examples_app, name="examples")
app.add_typer(jobs_app, name="jobs")
app.add_typer(forms_app, name="forms")
app.add_typer(llm_app, name="llm")
app.add_typer(inbox_app, name="inbox")
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(profiles_app, name="profiles")
app.add_typer(resumes_app, name="resumes")


if __name__ == "__main__":  # pragma: no cover
    app()
