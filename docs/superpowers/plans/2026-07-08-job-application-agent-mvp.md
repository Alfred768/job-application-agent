# Job Application Agent MVP Implementation Plan

Status: Superseded as an execution baseline after the 2026-07-09 readiness audit. Do not execute this file as-is against the current repository state.

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-first MVP job application agent that imports compliant jobs/JDs, indexes existing resume templates, scores role fit, prepares review packets, and provides a guarded browser form-fill entrypoint that stops before final submission.

**Architecture:** The MVP is a Python package with a Typer CLI, SQLite persistence, Pydantic domain models, and focused service modules. LLM-backed features sit behind an adapter so tests can use deterministic fakes and the OpenAI-compatible API key is never committed.

**Tech Stack:** Python 3.11+, Typer, Pydantic, SQLite, pytest, python-docx, pypdf, Jinja2, Playwright-ready browser abstraction.

---

## File Structure

- Create: `pyproject.toml` - package metadata, dependencies, CLI entrypoint, pytest config.
- Create: `.env.example` - documented environment variables without secrets.
- Create: `README.md` - setup, safety boundaries, CLI usage.
- Create: `src/job_agent/__init__.py` - package marker.
- Create: `src/job_agent/config.py` - environment and path configuration.
- Create: `src/job_agent/models.py` - Pydantic models for jobs, scores, templates, applications, documents.
- Create: `src/job_agent/db.py` - SQLite connection, schema creation, simple repository functions.
- Create: `src/job_agent/resumes.py` - index existing DOCX/PDF resume templates.
- Create: `src/job_agent/jobs.py` - manual JD import and normalized job construction.
- Create: `src/job_agent/scoring.py` - deterministic role classifier and explainable fit scorer.
- Create: `src/job_agent/reports.py` - Markdown/HTML review packet generation.
- Create: `src/job_agent/forms.py` - form-fill planning primitives and submit-gate policy.
- Create: `src/job_agent/cli.py` - Typer commands.
- Create: `tests/` - focused tests for each module.

## Chunk 1: Project Foundation

### Task 1: Package and Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `src/job_agent/__init__.py`
- Create: `src/job_agent/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

from job_agent.config import AppConfig


def test_config_uses_env_resume_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(tmp_path))
    config = AppConfig.from_env()
    assert config.resume_source_dir == tmp_path


def test_config_defaults_output_dir_to_project_output(monkeypatch):
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    config = AppConfig.from_env()
    assert config.output_dir.name == "output"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL because `job_agent.config` does not exist.

- [ ] **Step 3: Implement minimal package/config**

Implement `AppConfig.from_env()` with `RESUME_SOURCE_DIR`, `OUTPUT_DIR`, `DATABASE_PATH`, and optional `OPENAI_API_KEY`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add pyproject.toml .env.example src/job_agent/__init__.py src/job_agent/config.py tests/test_config.py && git commit -m "feat: add project configuration"`

### Task 2: SQLite Schema

**Files:**
- Create: `src/job_agent/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing tests**

```python
from job_agent.db import connect, init_db


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
    assert {"jobs", "resume_templates", "fit_scores", "applications", "generated_documents"} <= tables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL because database module is missing.

- [ ] **Step 3: Implement schema**

Create tables matching the PEAS design with JSON stored as text for parsed JD, edit plans, and quality checks.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_db.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/db.py tests/test_db.py && git commit -m "feat: add sqlite schema"`

## Chunk 2: Resume and JD Intake

### Task 3: Resume Template Indexing

**Files:**
- Create: `src/job_agent/models.py`
- Create: `src/job_agent/resumes.py`
- Test: `tests/test_resumes.py`

- [ ] **Step 1: Write failing tests**

```python
from job_agent.resumes import infer_track_from_filename


def test_infer_track_from_known_resume_names():
    assert infer_track_from_filename("GAOYI_WU_Agent_Engineer.docx") == "Agent Engineer"
    assert infer_track_from_filename("GAOYI_WU_ML_Infra.docx") == "ML Infra"
    assert infer_track_from_filename("GAOYI_WU_Data_Scientist.pdf") == "Data Scientist"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_resumes.py -v`
Expected: FAIL because resume module is missing.

- [ ] **Step 3: Implement resume models/indexer**

Implement `ResumeTemplate` and functions that find supported `.docx` and `.pdf` files, infer track names, and pair DOCX/PDF variants by stem.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_resumes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/models.py src/job_agent/resumes.py tests/test_resumes.py && git commit -m "feat: index resume templates"`

### Task 4: Manual JD Import

**Files:**
- Create: `src/job_agent/jobs.py`
- Test: `tests/test_jobs.py`

- [ ] **Step 1: Write failing tests**

```python
from job_agent.jobs import import_job_from_text


def test_import_job_from_text_extracts_basic_fields():
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents with Python and FastAPI."
    job = import_job_from_text(jd)
    assert job.company == "Acme AI"
    assert job.title == "Agent Engineer"
    assert job.location == "Remote"
    assert "FastAPI" in job.raw_jd
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_jobs.py -v`
Expected: FAIL because import module is missing.

- [ ] **Step 3: Implement manual JD parser**

Implement lightweight parsing for explicit `Company:`, `Title:`, and `Location:` lines with safe fallback values.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_jobs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/jobs.py tests/test_jobs.py && git commit -m "feat: add manual jd import"`

## Chunk 3: Scoring and Review Packets

### Task 5: Role Classification and Fit Scoring

**Files:**
- Create: `src/job_agent/scoring.py`
- Test: `tests/test_scoring.py`

- [ ] **Step 1: Write failing tests**

```python
from job_agent.jobs import import_job_from_text
from job_agent.scoring import classify_role, score_fit


def test_classify_agent_engineer_from_llm_agent_keywords():
    job = import_job_from_text("Title: AI Agent Engineer\n\nBuild LangChain tools, RAG workflows, FastAPI services.")
    assert classify_role(job) == "Agent Engineer"


def test_score_fit_returns_explainable_result():
    job = import_job_from_text("Title: ML Infrastructure Engineer\n\nKubernetes, Kafka, MLflow, FastAPI.")
    score = score_fit(job)
    assert score.score >= 70
    assert score.role_track == "ML Infra"
    assert score.reasons
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scoring.py -v`
Expected: FAIL because scoring module is missing.

- [ ] **Step 3: Implement deterministic scorer**

Use keyword maps for MVP. Keep LLM scoring as a future adapter behind the same output model.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/scoring.py tests/test_scoring.py && git commit -m "feat: add fit scoring"`

### Task 6: Review Packet Generation

**Files:**
- Create: `src/job_agent/reports.py`
- Test: `tests/test_reports.py`

- [ ] **Step 1: Write failing tests**

```python
from job_agent.jobs import import_job_from_text
from job_agent.reports import render_markdown_review
from job_agent.scoring import score_fit


def test_render_markdown_review_includes_submit_boundary():
    job = import_job_from_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    md = render_markdown_review(job, score_fit(job))
    assert "# Application Review" in md
    assert "Final Submit remains manual" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reports.py -v`
Expected: FAIL because report module is missing.

- [ ] **Step 3: Implement Markdown report**

Render job details, score, reasons, missing keywords, recommended resume track, and safety gates.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_reports.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/reports.py tests/test_reports.py && git commit -m "feat: render application review packet"`

## Chunk 4: CLI and Guarded Form-Fill Entry

### Task 7: CLI Commands

**Files:**
- Create: `src/job_agent/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
from typer.testing import CliRunner

from job_agent.cli import app


def test_cli_init_db(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--db", str(tmp_path / "agent.db")])
    assert result.exit_code == 0
    assert "Initialized" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py -v`
Expected: FAIL because CLI is missing.

- [ ] **Step 3: Implement CLI**

Commands:

- `job-agent init`
- `job-agent resumes index`
- `job-agent jobs import-text`
- `job-agent jobs review`

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/cli.py tests/test_cli.py && git commit -m "feat: add cli"`

### Task 8: Form-Fill Policy Primitives

**Files:**
- Create: `src/job_agent/forms.py`
- Test: `tests/test_forms.py`

- [ ] **Step 1: Write failing tests**

```python
from job_agent.forms import FieldPlan, FormFillPlan


def test_form_plan_requires_manual_submit():
    plan = FormFillPlan(fields=[FieldPlan(label="Email", value="user@example.com", sensitive=False)])
    assert plan.can_auto_submit is False
    assert "manual" in plan.submit_gate_reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_forms.py -v`
Expected: FAIL because form policy module is missing.

- [ ] **Step 3: Implement guarded form plan**

Create typed primitives that later Playwright automation will consume. Encode the submit gate in the model, not in UI copy only.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_forms.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

Run: `git add src/job_agent/forms.py tests/test_forms.py && git commit -m "feat: add guarded form fill plan"`

## Chunk 5: Documentation and Publishing

### Task 9: README and Public Repo Hygiene

**Files:**
- Modify: `README.md`
- Modify: `.gitignore`
- Modify: `docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md`

- [ ] **Step 1: Document setup**

Include:

- Project purpose.
- Safety boundaries.
- Install command.
- `.env.example` usage.
- No API keys in git.
- No resume source files committed.

- [ ] **Step 2: Sanitize public docs**

Replace user-specific local paths in public-facing docs with configurable environment variable references where possible.

- [ ] **Step 3: Run full verification**

Run: `pytest -v`
Expected: PASS.

- [ ] **Step 4: Commit**

Run: `git add README.md .gitignore docs/superpowers/specs/2026-07-08-job-application-agent-peas-design.md && git commit -m "docs: add public repo guidance"`

### Task 10: Create Public GitHub Repository and Push

**Files:**
- No code files.

- [ ] **Step 1: Check authentication**

Run: `gh auth status`
Expected: authenticated GitHub account.

- [ ] **Step 2: Create public repo**

Run: `gh repo create job-application-agent --public --source=. --remote=origin --description "Local-first personal job application agent with PEAS-designed safety gates" --push`
Expected: public repository created and `main` pushed.

- [ ] **Step 3: Verify remote**

Run: `git remote -v && gh repo view --web=false`
Expected: `origin` points to the new public GitHub repo.
