import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from typer.testing import CliRunner

from job_agent import cli
from job_agent.cli import app
from job_agent.db import connect, create_application, create_job, init_db, update_application_execution_status
from job_agent.models import Job


def write_minimal_docx(path, paragraphs):
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", "<Types></Types>")
        docx.writestr("word/document.xml", document_xml)


def _write_submitted_application(db_path: Path, job: Job) -> int:
    conn = connect(db_path)
    init_db(conn)
    job_id = create_job(conn, job)
    application_id = create_application(conn, job_id, job)
    assert update_application_execution_status(conn, application_id, "submitted")
    conn.close()
    return application_id


def _write_application_status(db_path: Path, job: Job, status: str) -> int:
    conn = connect(db_path)
    init_db(conn)
    job_id = create_job(conn, job)
    application_id = create_application(conn, job_id, job)
    assert update_application_execution_status(conn, application_id, status)
    conn.close()
    return application_id


def _write_resume_source_dir(tmp_path: Path, text: bytes = b"%PDF-1.4\nPython FastAPI LangChain") -> Path:
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "GAOYI_WU_Agent_Engineer.pdf").write_bytes(text)
    return resume_dir


def _write_fake_runtime_playwright(package_dir: Path) -> None:
    playwright_dir = package_dir / "node_modules" / "playwright"
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
  async goto(url) { console.log('fake goto ' + url); },
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
    )


def test_cli_init_db(tmp_path):
    runner = CliRunner()

    result = runner.invoke(app, ["init", "--db", str(tmp_path / "agent.db")])

    assert result.exit_code == 0
    assert "Initialized" in result.output


def test_gmail_authorize_uses_client_secret_from_env(tmp_path, monkeypatch):
    client_secret = tmp_path / "client_secret.json"
    client_secret.write_text('{"web": {"client_id": "id"}}')
    token_out = tmp_path / "gmail-token.json"
    calls = []

    def fake_authorize(client_secret_file, token_file, *, open_browser=True, port=0):
        calls.append(
            {
                "client_secret_file": client_secret_file,
                "token_file": token_file,
                "open_browser": open_browser,
                "port": port,
            }
        )
        Path(token_file).write_text("{}")

    monkeypatch.setenv("JOB_AGENT_GMAIL_CLIENT_SECRET_FILE", str(client_secret))
    monkeypatch.setattr(cli, "authorize_gmail", fake_authorize)

    result = CliRunner().invoke(
        app,
        [
            "inbox",
            "gmail-authorize",
            "--token-out",
            str(token_out),
            "--no-open-browser",
            "--port",
            "8765",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "client_secret_file": str(client_secret),
            "token_file": str(token_out),
            "open_browser": False,
            "port": 8765,
        }
    ]
    assert token_out.is_file()


def test_console_entrypoint_target_runs_in_real_process(tmp_path):
    db_path = tmp_path / "agent.db"
    env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from job_agent.cli import app; app()",
            "init",
            "--db",
            str(db_path),
        ],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Initialized database" in result.stdout
    assert db_path.exists()


def test_module_entrypoint_runs_cli_in_real_process(tmp_path):
    db_path = tmp_path / "module-agent.db"
    env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "job_agent.cli",
            "init",
            "--db",
            str(db_path),
        ],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Initialized database" in result.stdout
    assert db_path.exists()


def test_cover_letter_describes_current_role_grammatically():
    markdown = cli._render_cover_letter_markdown(
        Job(
            title="Data Scientist",
            company="Acme",
            location="Remote",
            source_url="https://example.com/job",
            raw_jd="",
        ),
        {
            "name": "Gaoyi Wu",
            "work_history": [
                {"company": "Intellisys Lab", "title": "Research Assistant", "current": True}
            ],
            "projects": [],
        },
    )

    assert "as a Research Assistant" in markdown
    assert "worked on Research Assistant" not in markdown


def test_cli_examples_export_writes_runnable_offline_fixtures(tmp_path):
    export_dir = tmp_path / "examples"
    runner = CliRunner()

    export = runner.invoke(
        app,
        [
            "examples",
            "export",
            "--out-dir",
            str(export_dir),
        ],
    )

    assert export.exit_code == 0, export.output
    assert "Exported 6 example files" in export.output
    for filename in [
        "offline-sources.json",
        "offline-jobs.xml",
        "sample-resume.md",
        "profile.json",
        "form-snapshot.json",
        "sensitive-answers.json",
    ]:
        assert (export_dir / filename).exists()
    (export_dir / "sample-resume.pdf").write_bytes(b"%PDF-1.4\nsample resume")

    out_dir = tmp_path / "offline-pipeline"
    pipeline = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(export_dir / "offline-sources.json"),
            "--out-dir",
            str(out_dir),
            "--min-score",
            "0",
            "--resume",
            str(export_dir / "sample-resume.pdf"),
            "--profile",
            str(export_dir / "profile.json"),
            "--sensitive-kb",
            str(export_dir / "sensitive-answers.json"),
        ],
    )

    assert pipeline.exit_code == 0, pipeline.output
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    assert manifest["counts"] == {"imported": 1, "shortlisted": 1, "prepared": 1}
    assert manifest["agent_runtime"]["closed_loop"] is True
    pipeline_round = manifest["agent_runtime"]["pipeline"]["rounds"][0]
    assert pipeline_round["thought"]["selected_tool"] == (
        "prepare_application_cohort"
    )
    summary = json.loads((out_dir / "applications" / "batch-summary.json").read_text())
    assert Path(summary[0]["runtime_script_path"]).exists()
    trajectory = json.loads(
        Path(summary[0]["agent_trajectory_path"]).read_text()
    )
    preparation = trajectory["stages"]["preparation"]
    assert preparation[-1]["rounds"][0]["thought"]["selected_tool"] == (
        "runtime_package_builder"
    )
    assert (
        preparation[-2]["observations"][-1]["observation_id"]
        == preparation[-1]["rounds"][0]["input_observation"][
            "observation_id"
        ]
    )
    assert summary[0]["agent_handoff"]["observation_id"] == (
        preparation[-1]["observations"][-1]["observation_id"]
    )
    assert summary[0]["agent_handoff"]["payload"] == (
        preparation[-1]["observations"][-1]["payload"]
    )


def test_cli_examples_verify_offline_runs_end_to_end_smoke(tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for offline verification smoke test")

    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    monkeypatch.setenv("JOB_AGENT_GMAIL_TOKEN_FILE", str(tmp_path / "gmail-token.json"))
    real_resume_dir = tmp_path / "real-resumes"
    real_resume_dir.mkdir()
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(real_resume_dir))
    out_dir = tmp_path / "offline-verify"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "examples",
            "verify-offline",
            "--out-dir",
            str(out_dir),
            "--timeout-seconds",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Offline verification complete" in result.output
    assert (out_dir / "examples" / "offline-sources.json").exists()
    assert (out_dir / "pipeline-run" / "pipeline-manifest.json").exists()
    audit = json.loads((out_dir / "execution-audit.json").read_text())
    runtime_trace = json.loads(
        (out_dir / "agent-runtime-trace.json").read_text()
    )
    metrics = json.loads(
        (out_dir / "evaluation-metrics.json").read_text()
    )
    assert audit["counts"] == {
        "total": 1,
        "completed": 0,
        "submitted": 1,
        "submit_clicked_unconfirmed": 0,
        "email_verification_required": 0,
        "submission_processing_error": 0,
        "submission_blocked_by_anti_spam": 0,
        "candidate_account_required": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert audit["applications"][0]["status"] == "submitted"
    assert audit["agent_runtime"]["closed_loop"] is True
    execution_trace = audit["agent_runtime"]["applications"][0]
    assert execution_trace["rounds"][0]["thought"]["selected_tool"] == (
        "browser_execute"
    )
    assert runtime_trace["closed_loop"] is True
    assert runtime_trace["continuity"] == {
        "continuous": 1,
        "not_executed": 0,
        "disconnected": 0,
        "missing": 0,
    }
    assert runtime_trace["applications"][0]["agent_runtime_id"] == (
        "application-1"
    )
    assert metrics["agent_core"]["evaluator"] == (
        "job_application_round"
    )


def test_cli_review_job_from_text_file(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    out_path = tmp_path / "review.md"
    runner = CliRunner()

    result = runner.invoke(app, ["jobs", "review", str(jd_path), "--out", str(out_path)])

    assert result.exit_code == 0
    assert out_path.exists()
    text = out_path.read_text()
    assert "Application Review" in text
    assert "## JD Analysis" in text


def test_cli_review_job_can_select_resume_and_track_application(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    (resume_dir / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")
    db_path = tmp_path / "agent.db"
    out_path = tmp_path / "review.md"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--resume-source-dir",
            str(resume_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    assert "## Recommended Resume" in text
    assert "GAOYI_WU_Agent_Engineer.pdf" in text
    assert "## Tracking" in text
    assert "application_id=1" in text


def test_cli_review_job_can_export_application_package(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    out_path = tmp_path / "review.md"
    package_dir = tmp_path / "package"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--package-dir",
            str(package_dir),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    assert "## Application Package" in text
    assert (package_dir / "review.md").exists()
    assert (package_dir / "jd-analysis.json").exists()


def test_cli_review_job_can_include_form_fill_plan(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com", "sponsorship": "Needs review"}')
    out_path = tmp_path / "review.md"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    assert "## Form Fill Plan" in text
    assert "review_required=Do you require visa sponsorship?" in text


def test_cli_review_job_merges_sensitive_kb_into_form_plan(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Are you authorized to work in the United States?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "answers": {
                    "Are you authorized to work in the United States?": "No",
                }
            }
        )
    )
    kb_path = tmp_path / "sensitive-answers.json"
    kb_path.write_text(
        json.dumps(
            {
                "work_authorization": {
                    "patterns": ["authorized to work"],
                    "answer": "Yes",
                    "approved": True,
                }
            }
        )
    )
    out_path = tmp_path / "review.md"

    result = CliRunner().invoke(
        app,
        [
            "jobs",
            "review",
            str(jd_path),
            "--out",
            str(out_path),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--sensitive-kb",
            str(kb_path),
        ],
    )

    assert result.exit_code == 0, result.output
    text = out_path.read_text()
    assert "## Form Fill Plan" in text
    assert "Are you authorized to work in the United States?=Yes" in text
    assert "review_required=Are you authorized to work in the United States?" not in text


def test_cli_import_rss_jobs_writes_normalized_json(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-rss", str(rss_path), "--out", str(out_path), "--source", "example-rss"],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    text = out_path.read_text()
    assert '"title": "Agent Engineer"' in text
    assert '"company": "Acme AI"' in text
    assert '"location": "Remote"' in text


def test_cli_review_rss_jobs_writes_review_packets(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with LangChain and FastAPI.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-rss", str(rss_path), "--out-dir", str(out_dir), "--source", "example-rss"],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "Agent Engineer" in text
    assert "Acme AI" in text
    assert "## Submit Gate" in text


def test_cli_import_greenhouse_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "greenhouse.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Agent Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}, "content": "Build agents."}]}'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-greenhouse", "acme", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    assert '"source": "greenhouse:acme"' in out_path.read_text()


def test_cli_review_greenhouse_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "greenhouse.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Agent Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}, "content": "Build LLM agents with LangChain."}]}'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-greenhouse", "acme", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "Agent Engineer" in text
    assert "acme" in text
    assert "## Submit Gate" in text


def test_cli_import_lever_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "lever.json"
    payload_path.write_text(
        '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build platforms."}]'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-lever", "acme", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    assert '"source": "lever:acme"' in out_path.read_text()


def test_cli_review_lever_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "lever.json"
    payload_path.write_text(
        '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build ML platforms with Python."}]'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-lever", "acme", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "ML Platform Engineer" in text
    assert "acme" in text
    assert "## Submit Gate" in text


def test_cli_import_ashby_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "ashby.json"
    payload_path.write_text(
        '{"jobs": [{"title": "AI Product Engineer", "jobUrl": "https://jobs.ashbyhq.com/brainco/1", "applyUrl": "https://jobs.ashbyhq.com/brainco/1/application", "location": "San Francisco", "descriptionHtml": "Build AI products."}]}'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-ashby", "brainco", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    text = out_path.read_text()
    assert '"source": "ashby:brainco"' in text
    assert '"apply_url": "https://jobs.ashbyhq.com/brainco/1/application"' in text


def test_cli_review_ashby_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "ashby.json"
    payload_path.write_text(
        '{"jobs": [{"title": "AI Product Engineer", "jobUrl": "https://jobs.ashbyhq.com/brainco/1", "applyUrl": "https://jobs.ashbyhq.com/brainco/1/application", "location": "San Francisco", "descriptionHtml": "Build AI products with Python."}]}'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-ashby", "brainco", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "AI Product Engineer" in text
    assert "brainco" in text
    assert "## Submit Gate" in text


def test_cli_import_remotive_jobs_writes_normalized_json(tmp_path):
    payload_path = tmp_path / "remotive.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs."}]}'
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-remotive", "--payload", str(payload_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 1 jobs" in result.output
    assert '"source": "remotive"' in out_path.read_text()


def test_cli_import_sources_combines_configured_sources(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        </item></channel></rss>"""
    )
    lever_path = tmp_path / "lever.json"
    lever_path.write_text(
        '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build ML platforms."}]'
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        """
        {
          "sources": [
            {"type": "rss", "source": "example-rss", "rss_file": "jobs.xml"},
            {"type": "lever", "site": "acme", "payload_file": "lever.json"}
          ]
        }
        """
    )
    out_path = tmp_path / "jobs.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "import-sources", str(config_path), "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Imported 2 jobs" in result.output
    text = out_path.read_text()
    assert '"source": "example-rss"' in text
    assert '"source": "lever:acme"' in text


def test_cli_jobs_shortlist_filters_and_scores_jobs(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme",
            "location": "Remote",
            "raw_jd": "Build LangChain agents, RAG workflows, tools, and LLM systems.",
            "source": "test",
            "source_url": "https://jobs.example.com/agent",
            "apply_url": "https://jobs.example.com/agent",
            "remote_policy": null
          },
          {
            "title": "Store Manager",
            "company": "RetailCo",
            "location": "NYC",
            "raw_jd": "Manage retail operations and staffing.",
            "source": "test",
            "source_url": "https://jobs.example.com/store",
            "apply_url": "https://jobs.example.com/store",
            "remote_policy": null
          }
        ]"""
    )
    out_path = tmp_path / "shortlist.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "shortlist", str(jobs_path), "--min-score", "60", "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert "Shortlisted 1 jobs" in result.output
    text = out_path.read_text()
    assert '"title": "Agent Engineer"' in text
    assert '"fit_score":' in text
    assert "Store Manager" not in text


def test_cli_review_sources_writes_review_packets(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        </item></channel></rss>"""
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        """
        {
          "sources": [
            {"type": "rss", "source": "example-rss", "rss_file": "jobs.xml"}
          ]
        }
        """
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-sources", str(config_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    assert "# Application Review" in review_files[0].read_text()


def test_cli_review_remotive_jobs_writes_review_packets(tmp_path):
    payload_path = tmp_path / "remotive.json"
    payload_path.write_text(
        '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs with FastAPI."}]}'
    )
    out_dir = tmp_path / "reviews"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["jobs", "review-remotive", "--payload", str(payload_path), "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0
    assert "Reviewed 1 jobs" in result.output
    review_files = list(out_dir.glob("*.md"))
    assert len(review_files) == 1
    text = review_files[0].read_text()
    assert "# Application Review" in text
    assert "Backend Engineer" in text
    assert "RemoteCo" in text
    assert "## Submit Gate" in text


def test_cli_forms_build_script_writes_guarded_playwright_script(tmp_path):
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com", "sponsorship": "Needs review"}')
    out_path = tmp_path / "fill-form.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "build-script",
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--application-url",
            "https://jobs.example.com/apply",
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    assert "Wrote guarded form-fill script" in result.output
    text = out_path.read_text()
    assert 'await page.goto("https://jobs.example.com/apply");' in text
    assert 'await page.getByLabel("Email").fill("gaoyi@example.com");' in text
    assert "Do you require visa sponsorship?" in text
    assert ".click(" not in text


def test_cli_forms_build_script_can_upload_resume_file(tmp_path, monkeypatch):
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_path = resume_dir / "GAOYI_WU_SDE.pdf"
    resume_path.write_text("pdf")
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))
    out_path = tmp_path / "fill-form.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "build-script",
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume-file",
            str(resume_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    assert f'await page.getByLabel("Resume").setInputFiles("{resume_path}");' in out_path.read_text()


def test_cli_forms_build_snapshot_script_writes_inspection_only_script(tmp_path):
    out_path = tmp_path / "capture-form.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "build-snapshot-script",
            "--application-url",
            "https://jobs.example.com/apply",
            "--out",
            str(out_path),
            "--snapshot-out",
            "form-snapshot.json",
        ],
    )

    assert result.exit_code == 0
    assert "Wrote guarded form snapshot script" in result.output
    text = out_path.read_text()
    assert 'await page.goto("https://jobs.example.com/apply");' in text
    assert 'fs.writeFileSync("form-snapshot.json"' in text
    assert "querySelectorAll" in text
    assert ".fill(" not in text
    assert ".click(" not in text


def test_cli_applications_prepare_generates_package_and_fill_script(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com", "sponsorship": "Needs review"}')
    resume_dir = _write_resume_source_dir(tmp_path)
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--index",
            "1",
            "--out-dir",
            str(out_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume-source-dir",
            str(resume_dir),
        ],
    )

    assert result.exit_code == 0
    assert "Prepared application package" in result.output
    assert (out_dir / "review.md").exists()
    assert (out_dir / "jd-analysis.json").exists()
    assert not (out_dir / "resume-edit-plan.json").exists()
    script = (out_dir / "fill-form.js").read_text()
    assert 'await page.goto("https://boards.greenhouse.io/acme/jobs/1");' in script
    assert 'await page.getByLabel("Email").fill("gaoyi@example.com");' in script
    assert ".click(" not in script


def test_cli_applications_prepare_refuses_submitted_apply_url(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    db_path = tmp_path / "agent.db"
    _write_submitted_application(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
    )

    def unexpected_prepare(*args, **kwargs):
        pytest.fail("duplicate submitted applications must not be prepared")

    monkeypatch.setattr("job_agent.cli._prepare_application_package", unexpected_prepare)

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--index",
            "1",
            "--db",
            str(db_path),
            "--out-dir",
            str(tmp_path / "application"),
        ],
    )

    assert result.exit_code != 0
    assert "Refusing to prepare duplicate application" in result.output
    assert "https://boards.greenhouse.io/acme/jobs/1" in result.output


def test_cli_applications_prepare_refuses_ineligible_candidate_location(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Data Engineer",
            "company": "Acme AI",
            "location": "Warsaw, Poland",
            "raw_jd": "Build data pipelines.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/2",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/2",
            "remote_policy": null
          }
        ]"""
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"country": "United States", "work_history": [{"title": "ML Engineer Intern"}]}')

    def unexpected_prepare(*args, **kwargs):
        pytest.fail("candidate-ineligible applications must not be prepared")

    monkeypatch.setattr("job_agent.cli.JobApplicationAgent.run", unexpected_prepare)

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--index",
            "1",
            "--profile",
            str(profile_path),
            "--out-dir",
            str(tmp_path / "application"),
        ],
    )

    assert result.exit_code != 0
    assert "Refusing to prepare ineligible application" in result.output
    assert "outside" in result.output


def test_cli_applications_prepare_preserves_intake_urls_in_tracking_db(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "ML Platform Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build ML training infrastructure with Python.",
            "source": "greenhouse:acme",
            "source_url": "https://job-boards.greenhouse.io/acme/jobs/42",
            "apply_url": "https://job-boards.greenhouse.io/acme/jobs/42",
            "remote_policy": "Remote"
          }
        ]"""
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "candidate@example.com"}')
    resume_dir = _write_resume_source_dir(tmp_path, b"%PDF-1.4\nPython ML training infrastructure")
    db_path = tmp_path / "agent.db"
    out_dir = tmp_path / "application"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--profile",
            str(profile_path),
            "--resume-source-dir",
            str(resume_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    conn = sqlite3.connect(db_path)
    application_url = conn.execute("select apply_url from applications").fetchone()[0]
    source, source_url, job_url = conn.execute(
        "select source, source_url, apply_url from jobs"
    ).fetchone()
    conn.close()
    assert application_url == "https://job-boards.greenhouse.io/acme/jobs/42"
    assert (source, source_url, job_url) == (
        "greenhouse:acme",
        "https://job-boards.greenhouse.io/acme/jobs/42",
        "https://job-boards.greenhouse.io/acme/jobs/42",
    )


def test_cli_applications_prepare_merges_sensitive_kb_into_package_scripts(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Are you authorized to work in the United States?"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "answers": {
                    "Are you authorized to work in the United States?": "No",
                }
            }
        )
    )
    kb_path = tmp_path / "sensitive-answers.json"
    kb_path.write_text(
        json.dumps(
            {
                "work_authorization": {
                    "patterns": ["authorized to work"],
                    "answer": "Yes",
                    "approved": True,
                }
            }
        )
    )
    out_dir = tmp_path / "application"
    resume_dir = _write_resume_source_dir(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--sensitive-kb",
            str(kb_path),
            "--resume-source-dir",
            str(resume_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    fill_script = (out_dir / "fill-form.js").read_text()
    runtime_script = (out_dir / "autofill-runtime.js").read_text()
    assert 'getByLabel("Are you authorized to work in the United States?").fill("Yes")' in fill_script
    assert '"sensitive_answers"' in runtime_script
    assert '"answer": "Yes"' in runtime_script
    assert '"Are you authorized to work in the United States?": "No"' in runtime_script


def test_cli_applications_prepare_can_generate_tailored_resume_draft(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain, FastAPI, and Rust.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--index",
            "1",
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0
    assert not (out_dir / "tailored-resume.md").exists()


def test_cli_applications_prepare_uses_selected_resume_template_text(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain, FastAPI, and RAG.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    write_minimal_docx(
        resume_dir / "GAOYI_WU_Agent_Engineer.docx",
        ["Gaoyi Wu", "Built FastAPI services and LLM workflow tools."],
    )
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume-source-dir",
            str(resume_dir),
        ],
    )

    assert result.exit_code == 0
    assert not (out_dir / "tailored-resume.md").exists()


def test_cli_applications_prepare_can_wire_specified_pdf_resume_upload(tmp_path, monkeypatch):
    def fake_convert_docx_to_pdf(docx_path, pdf_path):
        Path(pdf_path).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr("job_agent.cli.convert_docx_to_pdf", fake_convert_docx_to_pdf)

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume",
            str(resume_path),
            "--upload-resume",
        ],
    )

    assert result.exit_code == 0
    script = (out_dir / "fill-form.js").read_text()
    assert f'await page.getByLabel("Resume").setInputFiles("{resume_path}");' in script
    assert f'"resumeFile": "{resume_path}"' in (out_dir / "autofill-runtime.js").read_text()
    assert not (out_dir / "tailored-resume.docx").exists()
    assert not (out_dir / "tailored-resume.pdf").exists()
    assert not (out_dir / "resume.docx").exists()


def test_cli_applications_prepare_defaults_to_configured_resume_source_dir(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Machine Learning Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build PyTorch training pipelines and ML evaluation systems.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_dir = tmp_path / "configured-resumes"
    resume_dir.mkdir()
    selected_resume = resume_dir / "GAOYI_WU_MLE.pdf"
    selected_resume.write_bytes(b"%PDF-1.4\nPyTorch training pipelines")
    (resume_dir / "GAOYI_WU_SDE.pdf").write_bytes(b"%PDF-1.4\nbackend APIs")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    out_dir = tmp_path / "application"
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0, result.output
    summary = cli._execution_summary_for_package(out_dir)
    assert summary["upload_resume_path"] == str(selected_resume)
    assert summary["required_resume_source_dir"] == str(resume_dir)
    runtime_script = (out_dir / "autofill-runtime.js").read_text()
    assert f'"resumeFile": "{selected_resume}"' in runtime_script
    assert f'"resumeSourceDir": "{resume_dir}"' in runtime_script
    assert not list(out_dir.glob("tailored-resume.*"))


def test_cli_applications_prepare_requires_original_pdf_source_for_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("RESUME_SOURCE_DIR", raising=False)
    monkeypatch.setattr("job_agent.cli.load_env", lambda: None)
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Machine Learning Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build PyTorch training pipelines and ML evaluation systems.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(tmp_path / "application"),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code != 0
    assert "Refusing to prepare executable application without an" in result.output
    assert "original PDF resume" in result.output


def test_cli_applications_prepare_rejects_explicit_resume_outside_source_dir(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    outside_resume = tmp_path / "outside.pdf"
    outside_resume.write_bytes(b"%PDF-1.4\noutside")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(tmp_path / "application"),
            "--profile",
            str(profile_path),
            "--resume-source-dir",
            str(resume_dir),
            "--resume",
            str(outside_resume),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload PDF must come from required resume source dir" in result.output


def test_cli_applications_prepare_rejects_explicit_resume_outside_env_source_dir(
    tmp_path, monkeypatch
):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_dir = tmp_path / "configured-resumes"
    resume_dir.mkdir()
    outside_resume = tmp_path / "outside.pdf"
    outside_resume.write_bytes(b"%PDF-1.4\noutside")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(tmp_path / "application"),
            "--profile",
            str(profile_path),
            "--resume",
            str(outside_resume),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload PDF must come from required resume source dir" in result.output


def test_cli_applications_prepare_rejects_required_resume_outside_env_source_dir(
    tmp_path, monkeypatch
):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Machine Learning Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build PyTorch training pipelines and ML evaluation systems.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_dir = tmp_path / "configured-resumes"
    resume_dir.mkdir()
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(tmp_path / "application"),
            "--profile",
            str(profile_path),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload PDF must come from required resume source dir" in result.output


def test_cli_applications_prepare_rejects_non_pdf_resume_upload(tmp_path, monkeypatch):
    def fake_convert_docx_to_pdf(docx_path, pdf_path):
        Path(pdf_path).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr("job_agent.cli.convert_docx_to_pdf", fake_convert_docx_to_pdf)

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    resume_path = tmp_path / "resume.md"
    resume_path.write_text("# Gaoyi Wu\n\nBuilt FastAPI services.")
    out_dir = tmp_path / "application"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload must be an existing PDF" in result.output
    assert not (out_dir / "resume.docx").exists()


def test_cli_applications_prepare_rejects_resume_that_does_not_match_required_pdf(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = tmp_path / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")
    monkeypatch.setenv("JOB_AGENT_REQUIRED_RESUME_PDF", str(required_resume))
    out_dir = tmp_path / "application"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(other_resume),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload PDF does not match required path" in result.output


def test_cli_applications_prepare_uses_explicit_required_resume_pdf_without_resume_option(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    out_dir = tmp_path / "application"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code == 0, result.output
    assert f'await page.getByLabel("Resume").setInputFiles("{required_resume}");' in (
        out_dir / "fill-form.js"
    ).read_text()
    runtime = (out_dir / "autofill-runtime.js").read_text()
    assert f'"resumeFile": "{required_resume}"' in runtime
    assert not (out_dir / "tailored-resume.pdf").exists()
    assert not (out_dir / "resume.pdf").exists()


def test_cli_applications_prepare_rejects_resume_that_does_not_match_explicit_required_pdf(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null
          }
        ]"""
    )
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = tmp_path / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(tmp_path / "application"),
            "--resume",
            str(other_resume),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload PDF does not match required path" in result.output


def test_cli_applications_prepare_shortlist_generates_batch_packages(tmp_path):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs with Postgres and Redis.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Email"}, {"label": "Resume", "type": "file"}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com"}')
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    out_dir = tmp_path / "batch"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--limit",
            "2",
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
            "--resume",
            str(resume_path),
            "--upload-resume",
        ],
    )

    assert result.exit_code == 0
    assert "Prepared 2 application packages" in result.output
    first = out_dir / "001-acme-ai-agent-engineer"
    second = out_dir / "002-webco-backend-engineer"
    assert (first / "review.md").exists()
    assert not (first / "tailored-resume.md").exists()
    assert 'await page.goto("https://boards.greenhouse.io/acme/jobs/1");' in (first / "fill-form.js").read_text()
    assert f'await page.getByLabel("Resume").setInputFiles("{resume_path}");' in (first / "fill-form.js").read_text()
    assert (second / "review.md").exists()
    summary = json.loads((out_dir / "batch-summary.json").read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\n").hexdigest()
    assert {item["package_dir"] for item in summary} == {str(first), str(second)}
    assert {item["required_resume_pdf"] for item in summary} == {str(resume_path)}
    assert {item["required_resume_pdf_sha256"] for item in summary} == {expected_sha}
    assert {item["upload_resume_path"] for item in summary} == {str(resume_path)}


def test_cli_applications_prepare_shortlist_uses_explicit_required_resume_pdf(tmp_path):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with LangChain and FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs with Postgres and Redis.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"email": "gaoyi@example.com"}')
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    out_dir = tmp_path / "batch"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--profile",
            str(profile_path),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "batch-summary.json").read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\nrequired").hexdigest()
    assert {item["upload_resume_path"] for item in summary} == {str(required_resume)}
    assert {item["required_resume_pdf"] for item in summary} == {str(required_resume)}
    assert {item["upload_resume_pdf_sha256"] for item in summary} == {expected_sha}
    assert {item["required_resume_pdf_sha256"] for item in summary} == {expected_sha}
    assert {item["upload_resume_pdf_size_bytes"] for item in summary} == {
        len(b"%PDF-1.4\nrequired")
    }
    for item in summary:
        runtime = Path(item["runtime_script_path"]).read_text()
        assert f'"resumeFile": "{required_resume}"' in runtime


def test_cli_applications_prepare_shortlist_skips_submitted_duplicates(tmp_path, monkeypatch):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    db_path = tmp_path / "agent.db"
    _write_submitted_application(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
    )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append(job.title)
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "batch"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == ["Backend Engineer"]
    assert "duplicate submitted skipped" in result.output
    summary = json.loads((out_dir / "batch-summary.json").read_text())
    assert [item["title"] for item in summary] == ["Backend Engineer"]


def test_cli_applications_prepare_shortlist_skips_prior_terminal_outcomes(tmp_path, monkeypatch):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    db_path = tmp_path / "agent.db"
    _write_application_status(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
        "autofill_timed_out",
    )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append(job.title)
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "batch"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == ["Backend Engineer"]
    assert "prior terminal outcome skipped" in result.output
    summary = json.loads((out_dir / "batch-summary.json").read_text())
    assert [item["title"] for item in summary] == ["Backend Engineer"]


def test_cli_terminal_outcomes_include_email_verification_and_candidate_account(tmp_path):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    db_path = tmp_path / "agent.db"
    _write_application_status(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
        "email_verification_required",
    )
    _write_application_status(
        db_path,
        Job(
            title="Backend Engineer",
            company="WebCo",
            raw_jd="Build backend APIs.",
            source="test",
            apply_url="https://jobs.lever.co/webco/1",
        ),
        "candidate_account_required",
    )

    outcomes = cli._terminal_application_outcomes(db_path)
    acme_key = cli._normalized_application_url("https://boards.greenhouse.io/acme/jobs/1")
    webco_key = cli._normalized_application_url("https://jobs.lever.co/webco/1")
    assert outcomes[acme_key] == "email_verification_required"
    assert outcomes[webco_key] == "candidate_account_required"

    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append(job.title)
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "batch"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == []
    assert "prior terminal outcome skipped" in result.output
    monkeypatch.undo()


def test_cli_execute_batch_skips_previously_submitted_db_application(tmp_path):
    db_path = tmp_path / "agent.db"
    application_id = _write_submitted_application(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
    )
    script_path = tmp_path / "runtime.js"
    script_path.write_text("throw new Error('must not run');")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "runtime_script_path": str(script_path),
                    "application_id": str(application_id),
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_previously_submitted"
    assert "must not run" not in result.output


def test_cli_execute_batch_skips_prior_terminal_outcome_without_overwriting_db(tmp_path):
    db_path = tmp_path / "agent.db"
    application_id = _write_application_status(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url="https://boards.greenhouse.io/acme/jobs/1",
        ),
        "autofill_timed_out",
    )
    script_path = tmp_path / "runtime.js"
    script_path.write_text("throw new Error('must not run');")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "runtime_script_path": str(script_path),
                    "application_id": str(application_id),
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
            "--db",
            str(db_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_prior_terminal_outcome"
    conn = connect(db_path)
    status = conn.execute(
        "select status from applications where id = ?", (application_id,)
    ).fetchone()[0]
    conn.close()
    assert status == "autofill_timed_out"
    assert "must not run" not in result.output


def test_cli_applications_verify_resumes_writes_preflight_without_runtime(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("resume verification must not execute runtime scripts")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nverified")
    script_path = tmp_path / "runtime.js"
    script_path.write_text(f'const CFG = {{"resumeFile": "{resume_path}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                }
            ]
        )
    )
    out_path = tmp_path / "resume-preflight.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "verify-resumes",
            str(summary_path),
            "--out",
            str(out_path),
            "--required-resume-pdf",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "1 verified, 0 invalid" in result.output
    preflight = json.loads(out_path.read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\nverified").hexdigest()
    assert preflight["counts"] == {"total": 1, "verified": 1, "invalid": 0}
    assert preflight["required_resume_pdf_sha256"] == expected_sha
    assert preflight["applications"][0]["status"] == "verified"
    assert preflight["applications"][0]["upload_resume_pdf_sha256"] == expected_sha
    assert preflight["applications"][0]["required_resume_pdf_sha256"] == expected_sha


def test_cli_applications_verify_resumes_returns_nonzero_for_wrong_pdf(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("resume verification must not execute runtime scripts")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = tmp_path / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")
    script_path = tmp_path / "runtime.js"
    script_path.write_text(f'const CFG = {{"resumeFile": "{other_resume}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(other_resume),
                }
            ]
        )
    )
    out_path = tmp_path / "resume-preflight.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "verify-resumes",
            str(summary_path),
            "--out",
            str(out_path),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "0 verified, 1 invalid" in result.output
    preflight = json.loads(out_path.read_text())
    assert preflight["counts"] == {"total": 1, "verified": 0, "invalid": 1}
    assert preflight["applications"][0]["status"] == "invalid"
    assert "does not match required path" in preflight["applications"][0]["error"]


def test_cli_applications_verify_resumes_rejects_changed_pdf_hash(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("resume verification must not execute runtime scripts")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\noriginal")
    prepared_sha = hashlib.sha256(b"%PDF-1.4\noriginal").hexdigest()
    resume_path.write_bytes(b"%PDF-1.4\ngenerated replacement")
    script_path = tmp_path / "runtime.js"
    script_path.write_text(f'const CFG = {{"resumeFile": "{resume_path}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                    "upload_resume_pdf_sha256": prepared_sha,
                }
            ]
        )
    )
    out_path = tmp_path / "resume-preflight.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "verify-resumes",
            str(summary_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 1, result.output
    preflight = json.loads(out_path.read_text())
    assert preflight["counts"] == {"total": 1, "verified": 0, "invalid": 1}
    assert "hash does not match prepared summary" in preflight["applications"][0]["error"]


def test_cli_applications_verify_resumes_rejects_runtime_resume_mismatch(
    tmp_path, monkeypatch
):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("resume verification must not execute runtime scripts")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    summary_resume = tmp_path / "summary.pdf"
    summary_resume.write_bytes(b"%PDF-1.4\nsummary")
    runtime_resume = tmp_path / "runtime.pdf"
    runtime_resume.write_bytes(b"%PDF-1.4\nruntime")
    script_path = tmp_path / "runtime.js"
    script_path.write_text(f'const CFG = {{"resumeFile": "{runtime_resume}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(summary_resume),
                }
            ]
        )
    )
    out_path = tmp_path / "resume-preflight.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "verify-resumes",
            str(summary_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 1, result.output
    preflight = json.loads(out_path.read_text())
    assert preflight["counts"] == {"total": 1, "verified": 0, "invalid": 1}
    assert "runtime resumeFile does not match summary upload_resume_path" in (
        preflight["applications"][0]["error"]
    )


def test_cli_applications_verify_resumes_rejects_resume_outside_required_source_dir(
    tmp_path, monkeypatch
):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("resume verification must not execute runtime scripts")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    outside_resume = tmp_path / "outside.pdf"
    outside_resume.write_bytes(b"%PDF-1.4\noutside")
    script_path = tmp_path / "runtime.js"
    script_path.write_text(f'const CFG = {{"resumeFile": "{outside_resume}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(outside_resume),
                }
            ]
        )
    )
    out_path = tmp_path / "resume-preflight.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "verify-resumes",
            str(summary_path),
            "--out",
            str(out_path),
            "--required-resume-source-dir",
            str(source_dir),
        ],
    )

    assert result.exit_code == 1, result.output
    preflight = json.loads(out_path.read_text())
    assert preflight["counts"] == {"total": 1, "verified": 0, "invalid": 1}
    assert "must come from required resume source dir" in (
        preflight["applications"][0]["error"]
    )


def test_cli_applications_verify_resumes_uses_summary_required_source_dir(
    tmp_path, monkeypatch
):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("resume verification must not execute runtime scripts")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    outside_resume = tmp_path / "outside.pdf"
    outside_resume.write_bytes(b"%PDF-1.4\noutside")
    script_path = tmp_path / "runtime.js"
    script_path.write_text(f'const CFG = {{"resumeFile": "{outside_resume}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(outside_resume),
                    "required_resume_source_dir": str(source_dir),
                }
            ]
        )
    )
    out_path = tmp_path / "resume-preflight.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "verify-resumes",
            str(summary_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 1, result.output
    preflight = json.loads(out_path.read_text())
    assert preflight["applications"][0]["required_resume_source_dir"] == str(source_dir)
    assert "must come from required resume source dir" in (
        preflight["applications"][0]["error"]
    )


def test_cli_execute_batch_skips_missing_resume_pdf_without_running_runtime(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute without an existing PDF resume")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    script_path = tmp_path / "runtime.js"
    script_path.write_text('const CFG = {"resumeFile": null};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "runtime_script_path": str(script_path),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_invalid_resume"
    assert audit["applications"][0]["submit_gate"] == "invalid_resume_upload"
    assert "missing required PDF resume upload path" in audit["applications"][0]["error"]


def test_cli_execute_batch_skips_docx_resume_without_running_runtime(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute with a non-PDF resume")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    script_path = tmp_path / "runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "resume.docx"};\n')
    resume_path = tmp_path / "resume.docx"
    resume_path.write_bytes(b"docx")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_invalid_resume"
    assert "resume upload must be an existing PDF" in audit["applications"][0]["error"]


def test_cli_execute_batch_skips_package_local_resume_pdf_without_running_runtime(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute with a package-local generated PDF resume")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "resume.pdf"};\n')
    resume_path = package_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_invalid_resume"
    assert "must be an original external path" in audit["applications"][0]["error"]


def test_cli_execute_batch_allows_external_original_resume_pdf(tmp_path, monkeypatch):
    called = {}

    def fake_execute(items, **kwargs):
        called["items"] = items
        return [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "script_path": items[0]["runtime_script_path"],
                "status": "autofill_completed",
                "exit_code": 0,
                "submit_gate": "automatic_submission_enabled",
                "error": None,
                "filled_count": 1,
                "review_count": 0,
            }
        ]

    monkeypatch.setattr("job_agent.cli.execute_application_batch", fake_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "../resumes/resume.pdf"};\n')
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_path = resume_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert called["items"][0]["upload_resume_path"] == str(resume_path)
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["completed"] == 1
    assert audit["counts"]["skipped"] == 0


def test_cli_execute_batch_llm_answers_flag_is_scoped_to_execution(tmp_path, monkeypatch):
    observed = {}

    def fake_execute(items, **kwargs):
        observed["env"] = os.environ.get("JOB_AGENT_LLM_ANSWERS")
        return [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "script_path": items[0]["runtime_script_path"],
                "status": "autofill_completed",
                "exit_code": 0,
                "submit_gate": "automatic_submission_enabled",
                "error": None,
                "filled_count": 1,
                "review_count": 0,
            }
        ]

    monkeypatch.delenv("JOB_AGENT_LLM_ANSWERS", raising=False)
    monkeypatch.setattr("job_agent.cli.execute_application_batch", fake_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "../resumes/resume.pdf"};\n')
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_path = resume_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                    "application_id": "1",
                }
            ]
        )
    )

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(tmp_path / "execution-audit.json"),
            "--llm-answers",
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed["env"] == "1"
    assert os.environ.get("JOB_AGENT_LLM_ANSWERS") is None


def test_cli_execute_batch_skips_resume_pdf_that_does_not_match_required_path(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute with a resume outside the required PDF path")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "../resumes/other.pdf"};\n')
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    required_resume = resume_dir / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = resume_dir / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")
    monkeypatch.setenv("JOB_AGENT_REQUIRED_RESUME_PDF", str(required_resume))
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(other_resume),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_invalid_resume"
    assert "does not match required path" in audit["applications"][0]["error"]


def test_cli_execute_batch_skips_resume_pdf_that_does_not_match_explicit_required_path(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute with a resume outside the explicit required PDF path")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "../resumes/other.pdf"};\n')
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    required_resume = resume_dir / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = resume_dir / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(other_resume),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\nrequired").hexdigest()
    assert audit["required_resume_pdf"] == str(required_resume)
    assert audit["required_resume_pdf_sha256"] == expected_sha
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_invalid_resume"
    assert "does not match required path" in audit["applications"][0]["error"]


def test_cli_execute_batch_blocks_all_runtime_when_any_resume_preflight_fails(
    tmp_path, monkeypatch
):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute when any package fails resume preflight")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    valid_script = package_dir / "valid-runtime.js"
    invalid_script = package_dir / "invalid-runtime.js"
    valid_resume = tmp_path / "required.pdf"
    valid_resume.write_bytes(b"%PDF-1.4\nrequired")
    invalid_resume = tmp_path / "other.pdf"
    invalid_resume.write_bytes(b"%PDF-1.4\nother")
    valid_script.write_text(f'const CFG = {{"resumeFile": "{valid_resume}"}};\n')
    invalid_script.write_text(f'const CFG = {{"resumeFile": "{invalid_resume}"}};\n')
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "runtime_script_path": str(valid_script),
                    "upload_resume_path": str(valid_resume),
                },
                {
                    "company": "WebCo",
                    "title": "Backend Engineer",
                    "runtime_script_path": str(invalid_script),
                    "upload_resume_path": str(invalid_resume),
                },
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
            "--required-resume-pdf",
            str(valid_resume),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "preflight failed; no browser runtime executed" in result.output
    preflight = json.loads((tmp_path / "resume-preflight.json").read_text())
    audit = json.loads(audit_path.read_text())
    assert preflight["counts"] == {"total": 2, "verified": 1, "invalid": 1}
    assert audit["counts"]["skipped"] == 2
    statuses = {record["title"]: record["status"] for record in audit["applications"]}
    assert statuses == {
        "Agent Engineer": "skipped_resume_preflight_failed",
        "Backend Engineer": "skipped_invalid_resume",
    }


def test_cli_execute_batch_allows_resume_pdf_matching_required_path(tmp_path, monkeypatch):
    called = {}

    def fake_execute(items, **kwargs):
        called["items"] = items
        return [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "script_path": items[0]["runtime_script_path"],
                "status": "autofill_completed",
                "exit_code": 0,
                "submit_gate": "automatic_submission_enabled",
                "error": None,
                "filled_count": 1,
                "review_count": 0,
            }
        ]

    monkeypatch.setattr("job_agent.cli.execute_application_batch", fake_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "../resumes/required.pdf"};\n')
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    required_resume = resume_dir / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    monkeypatch.setenv("JOB_AGENT_REQUIRED_RESUME_PDF", str(required_resume))
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(required_resume),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert called["items"][0]["upload_resume_path"] == str(required_resume)
    audit = json.loads(audit_path.read_text())
    assert audit["counts"]["completed"] == 1
    assert audit["counts"]["skipped"] == 0


def test_cli_execute_batch_explicit_required_resume_overrides_env_path(tmp_path, monkeypatch):
    called = {}

    def fake_execute(items, **kwargs):
        called["items"] = items
        return [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "script_path": items[0]["runtime_script_path"],
                "status": "autofill_completed",
                "exit_code": 0,
                "submit_gate": "automatic_submission_enabled",
                "error": None,
                "filled_count": 1,
                "review_count": 0,
            }
        ]

    monkeypatch.setattr("job_agent.cli.execute_application_batch", fake_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text('const CFG = {"resumeFile": "../resumes/selected.pdf"};\n')
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    env_resume = resume_dir / "env-required.pdf"
    env_resume.write_bytes(b"%PDF-1.4\nenv")
    selected_resume = resume_dir / "selected.pdf"
    selected_resume.write_bytes(b"%PDF-1.4\nselected")
    monkeypatch.setenv("JOB_AGENT_REQUIRED_RESUME_PDF", str(env_resume))
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(selected_resume),
                    "application_id": "1",
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
            "--required-resume-pdf",
            str(selected_resume),
        ],
    )

    assert result.exit_code == 0, result.output
    assert called["items"][0]["upload_resume_path"] == str(selected_resume)
    audit = json.loads(audit_path.read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\nselected").hexdigest()
    assert audit["required_resume_pdf"] == str(selected_resume)
    assert audit["required_resume_pdf_sha256"] == expected_sha
    assert audit["applications"][0]["upload_resume_pdf_sha256"] == expected_sha
    assert audit["applications"][0]["required_resume_pdf_sha256"] == expected_sha
    assert audit["counts"]["completed"] == 1
    assert audit["counts"]["skipped"] == 0
    preflight = json.loads((tmp_path / "resume-preflight.json").read_text())
    assert preflight["counts"] == {"total": 1, "verified": 1, "invalid": 0}
    assert preflight["applications"][0]["upload_resume_pdf_sha256"] == expected_sha


def test_cli_applications_prepare_shortlist_passes_sensitive_kb_to_each_runtime(tmp_path):
    jobs_path = tmp_path / "shortlist.json"
    jobs_path.write_text(
        """[
          {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "location": "Remote",
            "raw_jd": "Build LLM agents with FastAPI.",
            "source": "greenhouse:acme",
            "source_url": "https://boards.greenhouse.io/acme/jobs/1",
            "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
            "remote_policy": null,
            "fit_score": 88
          },
          {
            "title": "Backend Engineer",
            "company": "WebCo",
            "location": "Remote",
            "raw_jd": "Build backend APIs with Postgres.",
            "source": "lever:webco",
            "source_url": "https://jobs.lever.co/webco/1",
            "apply_url": "https://jobs.lever.co/webco/1",
            "remote_policy": null,
            "fit_score": 76
          }
        ]"""
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    kb_path = tmp_path / "sensitive-answers.json"
    kb_path.write_text(
        json.dumps(
            {
                "work_authorization": {
                    "patterns": ["authorized to work"],
                    "answer": "Yes",
                    "approved": True,
                }
            }
        )
    )
    out_dir = tmp_path / "batch"
    resume_dir = _write_resume_source_dir(tmp_path)

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare-shortlist",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--profile",
            str(profile_path),
            "--sensitive-kb",
            str(kb_path),
            "--resume-source-dir",
            str(resume_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    for package_dir in [
        out_dir / "001-acme-ai-agent-engineer",
        out_dir / "002-webco-backend-engineer",
    ]:
        runtime_script = (package_dir / "autofill-runtime.js").read_text()
        assert '"sensitive_answers"' in runtime_script
        assert '"answer": "Yes"' in runtime_script


def test_cli_applications_build_batch_runner_writes_guarded_runner(tmp_path):
    first = tmp_path / "batch" / "001-acme-ai-agent-engineer"
    second = tmp_path / "batch" / "002-webco-backend-engineer"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    summary_path = tmp_path / "batch" / "batch-summary.json"
    summary_path.write_text(
        f"""[
          {{"company": "Acme AI", "title": "Agent Engineer", "package_dir": "{first}", "fill_script_path": "{first / "fill-form.js"}", "upload_resume_path": "{resume_path}"}},
          {{"company": "WebCo", "title": "Backend Engineer", "package_dir": "{second}", "fill_script_path": "{second / "fill-form.js"}", "upload_resume_path": "{resume_path}"}}
        ]"""
    )
    out_path = tmp_path / "batch" / "run-batch.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "applications",
            "build-batch-runner",
            str(summary_path),
            "--out",
            str(out_path),
            "--required-resume-pdf",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0
    assert "Wrote guarded batch runner" in result.output
    preflight = json.loads((tmp_path / "batch" / "resume-preflight.json").read_text())
    assert preflight["counts"] == {"total": 2, "verified": 2, "invalid": 0}
    script = out_path.read_text()
    assert 'spawn("node"' in script
    assert "Resume preflight verified" in script
    assert str(resume_path) in script
    assert 'stdio: ["inherit", "pipe", "pipe"]' in script
    assert "application.script_path" in script
    assert str(first / "fill-form.js") in script
    assert str(second / "fill-form.js") in script
    assert "Runtime completed for each application." in script
    assert "Submit policy: final Submit is automatic unless JOB_AGENT_SUBMIT_COMPLETE=0." in script
    assert ".click(" not in script
    assert ".press(" not in script
    assert ".submit(" not in script


def test_cli_applications_build_batch_runner_refuses_invalid_resume_preflight(tmp_path):
    package_dir = tmp_path / "batch" / "001-acme-ai-agent-engineer"
    package_dir.mkdir(parents=True)
    required_resume = tmp_path / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = tmp_path / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")
    summary_path = tmp_path / "batch" / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(package_dir / "autofill-runtime.js"),
                    "upload_resume_path": str(other_resume),
                }
            ]
        )
    )
    out_path = tmp_path / "batch" / "run-batch.js"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "build-batch-runner",
            str(summary_path),
            "--out",
            str(out_path),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Refusing to build runner" in result.output
    assert not out_path.exists()
    preflight = json.loads((tmp_path / "batch" / "resume-preflight.json").read_text())
    assert preflight["counts"] == {"total": 1, "verified": 0, "invalid": 1}


def test_cli_forms_autofill_writes_simplify_style_runtime_script(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        '{"name": "Gaoyi Wu", "email": "gaoyi@example.com", '
        '"answers": {"Are you authorized to work in the United States?": "Yes"}}'
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_path = resume_dir / "GAOYI_WU_SDE.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nsource resume")
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    out_path = package_dir / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--out",
            str(out_path),
            "--application-url",
            "https://boards.greenhouse.io/acme/jobs/1",
            "--resume-file",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0
    assert "Simplify-style runtime autofill script" in result.output
    text = out_path.read_text()
    assert 'require("playwright")' in text
    assert "https://boards.greenhouse.io/acme/jobs/1" in text
    assert "Gaoyi Wu" in text
    assert str(resume_path) in text
    # blocks only when required fields remain unresolved
    assert "automatic submission not performed" in text


def test_cli_forms_autofill_rejects_non_pdf_resume_file(tmp_path, monkeypatch):
    monkeypatch.delenv("RESUME_SOURCE_DIR", raising=False)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"name": "Gaoyi Wu", "email": "gaoyi@example.com"}')
    resume_path = tmp_path / "tailored-resume.docx"
    resume_path.write_bytes(b"docx")
    out_path = tmp_path / "autofill.js"

    result = CliRunner().invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--out",
            str(out_path),
            "--resume-file",
            str(resume_path),
        ],
    )

    assert result.exit_code != 0
    assert "resume upload must be an existing PDF" in result.output
    assert not out_path.exists()


def test_cli_forms_autofill_uses_browser_headless_env_default(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"name": "Gaoyi Wu", "email": "gaoyi@example.com"}')
    out_path = tmp_path / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"headless": false' in out_path.read_text()


def test_cli_forms_autofill_flag_overrides_browser_headless_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"name": "Gaoyi Wu", "email": "gaoyi@example.com"}')
    out_path = tmp_path / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--out",
            str(out_path),
            "--headless",
        ],
    )

    assert result.exit_code == 0, result.output
    assert '"headless": true' in out_path.read_text()


def test_cli_forms_autofill_uses_automatic_submission_policy_when_allowlist_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ALLOWLIST", "*")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"name": "Gaoyi Wu", "email": "gaoyi@example.com"}')
    out_path = tmp_path / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--out",
            str(out_path),
            "--application-url",
            "https://boards.greenhouse.io/acme/jobs/1",
        ],
    )

    assert result.exit_code == 0, result.output
    script = out_path.read_text()
    assert "Submit gate: automatic submission not performed" in script
    assert ".submit(" not in script
    assert ".click('submit')" not in script
    assert '.click("submit")' not in script


def test_cli_forms_init_sensitive_kb_writes_template(tmp_path):
    out_path = tmp_path / "sensitive-answers.json"
    runner = CliRunner()

    result = runner.invoke(app, ["forms", "init-sensitive-kb", "--out", str(out_path)])

    assert result.exit_code == 0
    assert "knowledge base template" in result.output
    import json as _json

    kb = _json.loads(out_path.read_text())
    assert "salary" in kb
    assert "work_authorization" in kb
    assert kb["salary"]["approved"] is False


def test_cli_forms_autofill_merges_sensitive_kb(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text('{"name": "Gaoyi Wu", "email": "gaoyi@example.com"}')
    kb_path = tmp_path / "sensitive-answers.json"
    kb_path.write_text(
        '{"salary": {"patterns": ["salary"], "answer": "120000", "approved": true}}'
    )
    out_path = tmp_path / "autofill.js"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "forms",
            "autofill",
            "--profile",
            str(profile_path),
            "--sensitive-kb",
            str(kb_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0
    text = out_path.read_text()
    # the approved KB answer is embedded so the runtime engine can use it
    assert "120000" in text
    assert "sensitive_answers" in text


def test_cli_forms_init_profile_writes_rich_template(tmp_path):
    out_path = tmp_path / "profile.json"
    runner = CliRunner()

    result = runner.invoke(app, ["forms", "init-profile", "--out", str(out_path)])

    assert result.exit_code == 0
    assert "rich profile template" in result.output
    import json as _json

    profile = _json.loads(out_path.read_text())
    assert "work_history" in profile
    assert "education" in profile
    assert "demographics" in profile
    assert "answers" in profile


def test_cli_forms_build_profile_from_resume(tmp_path):
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text(
        "Gaoyi Wu\nNew York, NY  |  gaoyi@example.com\n\n"
        "Experience\nAI Engineer — Acme\nBuilt agents.\n\n"
        "Education\nB.S. CS — State U\n"
    )
    out_path = tmp_path / "profile.json"
    runner = CliRunner()

    result = runner.invoke(
        app, ["forms", "build-profile-from-resume", "--resume", str(resume_path), "--out", str(out_path)]
    )

    assert result.exit_code == 0
    assert "work_history entries: 1" in result.output
    import json as _json

    profile = _json.loads(out_path.read_text())
    assert profile["name"] == "Gaoyi Wu"
    assert profile["work_history"][0]["title"] == "AI Engineer"


def test_cli_forms_build_profile_from_pdf_resume(tmp_path, monkeypatch):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nfake")
    out_path = tmp_path / "profile.json"
    monkeypatch.setattr(
        "job_agent.cli.extract_resume_text",
        lambda path: (
            "Gaoyi Wu\nNew York, NY | gaoyi@example.com\n\n"
            "Experience\nAI Engineer — Acme\nBuilt agents.\n\n"
            "Education\nB.S. CS — State U\n"
        ),
    )

    result = CliRunner().invoke(
        app, ["forms", "build-profile-from-resume", "--resume", str(resume_path), "--out", str(out_path)]
    )

    assert result.exit_code == 0, result.output
    profile = json.loads(out_path.read_text())
    assert profile["name"] == "Gaoyi Wu"
    assert profile["work_history"][0]["title"] == "AI Engineer"


def test_cli_resumes_tailor_writes_grounded_resume_draft(tmp_path):
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text("Title: Agent Engineer\n\nBuild LangChain agents with FastAPI and Rust.")
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Gaoyi Wu\n\nBuilt Python and FastAPI services.")
    out_path = tmp_path / "tailored-resume.md"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "resumes",
            "tailor",
            str(jd_path),
            "--resume",
            str(resume_path),
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code != 0
    assert not out_path.exists()


def test_cli_read_json_source_sends_browser_user_agent(monkeypatch):
    """Regression guard: live job APIs (Remotive) 403 the default Python-urllib UA.

    The CLI's autonomous source fetcher must attach a browser-like User-Agent
    so `jobs import-remotive` / `import-greenhouse` / `import-lever` keep working
    against live public endpoints.
    """
    import job_agent.cli as cli

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"jobs": []}'

    def fake_urlopen(request, timeout=20):
        captured["user_agent"] = request.get_header("User-agent")
        return _FakeResponse()

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    cli._read_json_source(None, "https://remotive.com/api/remote-jobs")

    assert captured["user_agent"]
    assert "Python-urllib" not in captured["user_agent"]
    assert "Mozilla" in captured["user_agent"]


def test_cli_pipeline_run_builds_auditable_application_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    shortlist_calls = []
    real_shortlist_jobs = cli.shortlist_jobs

    def capture_shortlist_call(*args, **kwargs):
        shortlist_calls.append(kwargs)
        return real_shortlist_jobs(*args, **kwargs)

    monkeypatch.setattr(cli, "shortlist_jobs", capture_shortlist_call)
    monkeypatch.setattr(
        "job_agent.cli.convert_docx_to_pdf",
        lambda docx_path, pdf_path: Path(pdf_path).write_bytes(b"%PDF-1.4\n") > 0,
    )
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    out_dir = tmp_path / "pipeline-run"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--required-resume-pdf",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert shortlist_calls[-1]["unique_companies"] is True
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\n").hexdigest()
    assert manifest["counts"] == {"imported": 1, "shortlisted": 1, "prepared": 1}
    assert manifest["submit_gate"] == "automatic_when_no_blocking_review"
    assert manifest["required_resume_pdf"] == str(resume_path)
    assert manifest["required_resume_pdf_sha256"] == expected_sha
    assert (out_dir / "jobs.json").exists()
    assert (out_dir / "shortlist.json").exists()
    summary = json.loads((out_dir / "applications" / "batch-summary.json").read_text())
    assert summary[0]["upload_resume_path"] == str(resume_path)
    assert summary[0]["required_resume_pdf"] == str(resume_path)
    assert summary[0]["upload_resume_pdf_sha256"] == expected_sha
    assert summary[0]["required_resume_pdf_sha256"] == expected_sha
    package_dir = Path(summary[0]["package_dir"])
    assert not (package_dir / "tailored-resume.docx").exists()
    assert not (package_dir / "tailored-resume.pdf").exists()
    runtime_script = package_dir / "autofill-runtime.js"
    assert runtime_script.exists()
    script = runtime_script.read_text()
    assert "https://jobs.example.com/acme-agent" in script
    assert "tailored-resume.pdf" not in script
    assert "tailored-resume.docx" not in script
    assert '"headless": false' in script
    assert ".click('submit')" not in script


def test_cli_pipeline_run_excludes_sibling_batch_urls(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setattr(
        "job_agent.cli.convert_docx_to_pdf",
        lambda docx_path, pdf_path: Path(pdf_path).write_bytes(b"%PDF-1.4\n") > 0,
    )
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    day_dir = tmp_path / "daily"
    prior_run = day_dir / "100000"
    prior_run.mkdir(parents=True)
    (prior_run / "applications").mkdir()
    (prior_run / "run-state.json").write_text(
        json.dumps(
            {
                "run_id": "100000",
                "phase": "prepared",
                "updated_at": "2026-01-01T00:00:00-04:00",
            }
        )
    )
    (prior_run / "applications" / "batch-summary.json").write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://jobs.example.com/acme-agent",
                    "runtime_script_path": str(
                        prior_run / "applications" / "acme-runtime.js"
                    ),
                }
            ]
        )
    )
    (prior_run / "execution-audit.json").write_text(
        json.dumps(
            {
                "applications": [
                    {
                        "company": "Acme AI",
                        "apply_url": "https://jobs.example.com/acme-agent",
                        "script_path": str(
                            prior_run / "applications" / "acme-runtime.js"
                        ),
                        "status": "submitted",
                    }
                ]
            }
        )
    )
    out_dir = day_dir / "200000"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--required-resume-pdf",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    assert manifest["counts"] == {"imported": 1, "shortlisted": 0, "prepared": 0}
    screening = json.loads((out_dir / "candidate-screening.json").read_text())
    assert screening[0]["company"] == "Acme AI"
    assert "already prepared in a sibling batch" in screening[0]["reasons"][0]


def test_cli_pipeline_run_releases_sibling_batch_without_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setattr(
        "job_agent.cli.convert_docx_to_pdf",
        lambda docx_path, pdf_path: Path(pdf_path).write_bytes(b"%PDF-1.4\n") > 0,
    )
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Platform Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-platform</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    day_dir = tmp_path / "daily"
    prior_run = day_dir / "100000"
    prior_run.mkdir(parents=True)
    (prior_run / "applications").mkdir()
    (prior_run / "run-state.json").write_text(
        json.dumps(
            {
                "run_id": "100000",
                "phase": "prepared",
                "updated_at": "2026-01-01T00:00:00-04:00",
            }
        )
    )
    (prior_run / "applications" / "batch-summary.json").write_text(
        json.dumps(
            [
                {
                    "company": "Acme AI",
                    "title": "Agent Engineer",
                    "apply_url": "https://jobs.example.com/acme-agent",
                }
            ]
        )
    )
    out_dir = day_dir / "200000"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--required-resume-pdf",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    assert manifest["counts"] == {"imported": 1, "shortlisted": 1, "prepared": 1}


def test_cli_pipeline_init_workspace_writes_templates_and_runbook(tmp_path):
    resume_path = tmp_path / "resume.md"
    resume_path.write_text(
        "# Gaoyi Wu\n\nAI Engineer\n\nExperience\nAI Engineer at Acme\n\nEducation\nM.S. Computer Science, Stevens Institute of Technology"
    )
    out_dir = tmp_path / "workspace"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "init-workspace",
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
            "--job-track",
            "ML Infra",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Initialized job-agent workspace" in result.output
    assert (out_dir / "profile.json").exists()
    assert (out_dir / "sensitive-answers.json").exists()
    assert (out_dir / "sources.json").exists()
    assert (out_dir / "WORKSPACE.md").exists()
    assert (out_dir / "resumes" / "README.txt").exists()
    assert (out_dir / "output").is_dir()
    profile = json.loads((out_dir / "profile.json").read_text())
    assert "work_history" in profile
    assert "education" in profile
    sources = json.loads((out_dir / "sources.json").read_text())
    assert sources["target_track"] == "ML Infra"
    assert len(sources["sources"]) >= 4
    assert any(
        item.get("type") == "remotive" and item.get("search") == "ml infrastructure engineer"
        for item in sources["sources"]
    )
    workspace_readme = (out_dir / "WORKSPACE.md").read_text()
    assert "job-agent pipeline run-execute sources.json" in workspace_readme
    assert "--resume-source-dir resumes" in workspace_readme
    assert "Target track: `ML Infra`" in workspace_readme
    assert "Suggested JD keywords" in workspace_readme


def test_cli_pipeline_init_workspace_accepts_pdf_resume(tmp_path, monkeypatch):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nfake")
    monkeypatch.setattr(
        "job_agent.cli.extract_resume_text",
        lambda path: (
            "Gaoyi Wu\nNew York, NY | gaoyi@example.com\n\n"
            "Experience\nAI Engineer — Acme\nBuilt agents.\n\n"
            "Education\nM.S. Computer Science — Stevens Institute of Technology\n"
        ),
    )
    out_dir = tmp_path / "workspace-pdf"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "init-workspace",
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
        ],
    )

    assert result.exit_code == 0, result.output
    profile = json.loads((out_dir / "profile.json").read_text())
    assert profile["name"] == "Gaoyi Wu"
    assert "work_history" in profile


def test_cli_pipeline_init_workspace_rejects_unknown_track(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "init-workspace",
            "--out-dir",
            str(tmp_path / "workspace"),
            "--job-track",
            "Totally Unknown Track",
        ],
    )

    assert result.exit_code != 0
    assert "Unsupported --job-track" in result.output


def test_cli_pipeline_run_passes_sensitive_kb_to_runtime_package(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    kb_path = tmp_path / "sensitive-answers.json"
    kb_path.write_text(
        json.dumps(
            {
                "work_authorization": {
                    "patterns": ["authorized to work"],
                    "answer": "Yes",
                    "approved": True,
                }
            }
        )
    )
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--sensitive-kb",
            str(kb_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "applications" / "batch-summary.json").read_text())
    runtime_script = Path(summary[0]["runtime_script_path"]).read_text()
    assert '"sensitive_answers"' in runtime_script
    assert '"answer": "Yes"' in runtime_script


def test_cli_rejects_direct_production_execution_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "src" / "job_agent" / "cli.py"))
    production_dir = tmp_path / "output" / "daily" / "2026-08-10" / "120000"
    production_dir.mkdir(parents=True)
    summary = production_dir / "applications" / "batch-summary.json"
    summary.parent.mkdir()
    summary.write_text("[]")
    audit = production_dir / "execution-audit.json"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary),
            "--audit-out",
            str(audit),
        ],
    )

    assert result.exit_code != 0
    assert "Production execution must be started by scripts/daily_sop.py" in result.output


def test_cli_pipeline_execute_batch_runs_generated_runtime_with_fake_playwright(tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for end-to-end execution smoke test")

    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "0")
    # This test supplies a local Node Playwright shim; prevent the CLI's
    # project .env from routing it through the real Gmail/Python runtime.
    monkeypatch.setenv("JOB_AGENT_GMAIL_TOKEN_FILE", "")
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    out_dir = tmp_path / "pipeline-run"
    runner = CliRunner()

    pipeline = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
        ],
    )

    assert pipeline.exit_code == 0, pipeline.output
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\n").hexdigest()
    assert manifest["required_resume_pdf"] == str(resume_path)
    assert manifest["required_resume_pdf_sha256"] == expected_sha
    summary_path = out_dir / "applications" / "batch-summary.json"
    summary = json.loads(summary_path.read_text())
    assert summary[0]["required_resume_pdf"] == str(resume_path)
    assert summary[0]["required_resume_pdf_sha256"] == expected_sha
    package_dir = Path(summary[0]["package_dir"])
    playwright_dir = package_dir / "node_modules" / "playwright"
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
  async goto(url) { console.log('fake goto ' + url); },
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
    )
    audit_path = tmp_path / "execution-audit.json"

    execution = runner.invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
            "--timeout-seconds",
            "10",
        ],
    )

    assert execution.exit_code == 0, execution.output
    assert "fake goto https://jobs.example.com/acme-agent" in execution.output
    assert "Submit clicked but confirmation not detected:" in execution.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"] == {
        "total": 1,
        "completed": 0,
        "submitted": 0,
        "submit_clicked_unconfirmed": 1,
        "email_verification_required": 0,
        "submission_processing_error": 0,
        "submission_blocked_by_anti_spam": 0,
        "candidate_account_required": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert audit["applications"][0]["status"] == "submit_clicked_unconfirmed"
    assert "candidate@example.com" not in audit_path.read_text()


def test_cli_execute_package_skips_previously_submitted_audit(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "execution-audit.json").write_text(
        json.dumps({"applications": [{"status": "submitted"}]})
    )

    def unexpected_execution(*args, **kwargs):
        pytest.fail("a package with a submitted audit must not execute again")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "Skipped already submitted package" in result.output


def test_cli_execute_package_skips_submitted_apply_url_in_db(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    db_path = tmp_path / "agent.db"
    apply_url = "https://job-boards.greenhouse.io/acme/jobs/123"
    _write_submitted_application(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build LLM agents.",
            source="test",
            apply_url=apply_url,
        ),
    )
    summary = {
        "company": "Acme AI",
        "title": "Different Display Title",
        "apply_url": apply_url,
        "package_dir": str(package_dir),
        "runtime_script_path": str(package_dir / "autofill-runtime.js"),
        "application_id": "1",
    }
    monkeypatch.setattr("job_agent.cli._execution_summary_for_package", lambda _path: summary)

    def unexpected_execution(*args, **kwargs):
        pytest.fail("duplicate submitted applications must not execute")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(
        app,
        ["applications", "execute-package", str(package_dir), "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Skipped package already submitted in DB" in result.output
    assert "matching submitted apply_url already exists" in result.output


def test_cli_execute_package_skips_non_pdf_runtime_resume_without_running(tmp_path, monkeypatch):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute with a non-PDF package resume")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "resume.docx").write_bytes(b"docx")
    (package_dir / "review.md").write_text(
        "Company: Acme AI\nTitle: Agent Engineer\napplication_id=1\n"
    )
    payload = {
        "profile": {"target_company": "Acme AI", "target_title": "Agent Engineer"},
        "applicationUrl": "https://boards.greenhouse.io/acme/jobs/1",
        "resumeFile": str(package_dir / "resume.docx"),
        "coverLetterFile": None,
    }
    (package_dir / "autofill-runtime.js").write_text(f"const CFG = {json.dumps(payload)};\n")

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "skipped_invalid_resume" in (package_dir / "execution-audit.json").read_text()
    audit = json.loads((package_dir / "execution-audit.json").read_text())
    assert audit["applications"][0]["submit_gate"] == "invalid_resume_upload"
    assert "resume upload must be an existing PDF" in audit["applications"][0]["error"]


def test_cli_execute_package_skips_resume_pdf_that_does_not_match_explicit_required_path(
    tmp_path, monkeypatch
):
    def unexpected_execute(*args, **kwargs):
        pytest.fail("runtime must not execute when package resume is not the explicit required PDF")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", unexpected_execute)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    required_resume = resume_dir / "required.pdf"
    required_resume.write_bytes(b"%PDF-1.4\nrequired")
    other_resume = resume_dir / "other.pdf"
    other_resume.write_bytes(b"%PDF-1.4\nother")
    payload = {
        "profile": {"target_company": "Acme AI", "target_title": "Agent Engineer"},
        "applicationUrl": "https://boards.greenhouse.io/acme/jobs/1",
        "resumeFile": str(other_resume),
        "coverLetterFile": None,
    }
    (package_dir / "autofill-runtime.js").write_text(f"const CFG = {json.dumps(payload)};\n")

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-package",
            str(package_dir),
            "--required-resume-pdf",
            str(required_resume),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads((package_dir / "execution-audit.json").read_text())
    preflight = json.loads((package_dir / "resume-preflight.json").read_text())
    assert audit["required_resume_pdf"] == str(required_resume)
    assert preflight["required_resume_pdf"] == str(required_resume)
    assert preflight["counts"] == {"total": 1, "verified": 0, "invalid": 1}
    assert audit["counts"]["skipped"] == 1
    assert audit["applications"][0]["status"] == "skipped_invalid_resume"
    assert "does not match required path" in audit["applications"][0]["error"]


def test_cli_execute_package_skips_anti_spam_blocked_audit(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "execution-audit.json").write_text(
        json.dumps({"applications": [{"status": "submission_blocked_by_anti_spam"}]})
    )

    def unexpected_execution(*args, **kwargs):
        pytest.fail("an anti-spam-blocked package must not automatically retry")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "blocked by a prior execution outcome" in result.output


def test_cli_execute_package_skips_timed_out_audit(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "execution-audit.json").write_text(
        json.dumps({"applications": [{"status": "autofill_timed_out", "error": "timeout"}]})
    )

    def unexpected_execution(*args, **kwargs):
        pytest.fail("a timed-out package has an unknown outcome and must not automatically retry")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "blocked by a prior execution outcome" in result.output


def test_cli_execute_package_skips_submission_processing_error_audit(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "execution-audit.json").write_text(
        json.dumps({"applications": [{"status": "submission_processing_error"}]})
    )

    def unexpected_execution(*args, **kwargs):
        pytest.fail("a post-submit processing error must not automatically retry")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "blocked by a prior execution outcome" in result.output


def test_cli_execute_package_records_stale_lock_as_unconfirmed_without_retry(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / ".execution.lock").write_text("pid=99999\n")

    def missing_process(pid, signal):
        raise ProcessLookupError

    summary = {
        "company": "Acme",
        "title": "Agent Engineer",
        "runtime_script_path": str(package_dir / "autofill-runtime.js"),
        "application_id": None,
    }
    monkeypatch.setattr("job_agent.cli.os.kill", missing_process)
    monkeypatch.setattr("job_agent.cli._execution_summary_for_package", lambda _path: summary)

    def unexpected_execution(*args, **kwargs):
        pytest.fail("an interrupted package must not automatically retry")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "interrupted execution with unconfirmed outcome" in result.output
    audit = json.loads((package_dir / "execution-audit.json").read_text())
    assert audit["applications"][0]["status"] == "autofill_failed"
    assert audit["applications"][0]["error"] == "execution_interrupted_unconfirmed"
    assert (
        audit["applications"][0]["recovery_plan"]["strategy"]
        == "confirmation_reconciliation"
    )
    assert not (package_dir / ".execution.lock").exists()


def test_cli_execute_package_records_unfinished_attempt_as_unconfirmed_without_retry(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / ".execution-attempt.json").write_text('{"schema_version": 1}\n')
    summary = {
        "company": "Acme",
        "title": "Agent Engineer",
        "runtime_script_path": str(package_dir / "autofill-runtime.js"),
        "application_id": None,
    }
    monkeypatch.setattr("job_agent.cli._execution_summary_for_package", lambda _path: summary)

    def unexpected_execution(*args, **kwargs):
        pytest.fail("an unfinished attempt must not automatically retry")

    monkeypatch.setattr("job_agent.cli._write_execution_audit", unexpected_execution)

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code == 0, result.output
    assert "interrupted execution with unconfirmed outcome" in result.output
    audit = json.loads((package_dir / "execution-audit.json").read_text())
    assert audit["applications"][0]["error"] == "execution_interrupted_unconfirmed"
    assert not (package_dir / ".execution-attempt.json").exists()


def test_cli_execute_package_rejects_concurrent_lock(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / ".execution.lock").write_text(f"pid={os.getpid()}\n")

    result = CliRunner().invoke(app, ["applications", "execute-package", str(package_dir)])

    assert result.exit_code != 0
    assert "already being executed" in result.output


def test_package_execution_lock_recovers_when_owner_is_gone(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    lock_path = package_dir / ".execution.lock"
    lock_path.write_text("pid=99999\n")

    def missing_process(pid, signal):
        raise ProcessLookupError

    monkeypatch.setattr("job_agent.cli.os.kill", missing_process)

    acquired = cli._acquire_package_execution_lock(package_dir)

    assert acquired == lock_path
    assert acquired.is_file()
    acquired.unlink()


def test_execution_summary_for_package_falls_back_to_review_metadata(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "autofill-runtime.js").write_text(
        'const CFG = {"profile": {}, "applicationUrl": "https://jobs.example.com/acme"};\n'
    )
    (package_dir / "review.md").write_text(
        "# Application Review\n\n"
        "Company: Acme AI\n"
        "Title: Agent Engineer\n\n"
        "## Tracking\n\n"
        "application_id=123\n"
    )

    summary = cli._execution_summary_for_package(package_dir)

    assert summary["company"] == "Acme AI"
    assert summary["title"] == "Agent Engineer"
    assert summary["application_id"] == "123"


def test_cli_reconcile_root_recovers_confirmed_package(tmp_path, monkeypatch):
    root = tmp_path / "packages"
    package_dir = root / "acme"
    package_dir.mkdir(parents=True)
    (package_dir / "submission-confirmation.txt").write_text("confirmation: matched thank you\n")
    (package_dir / "autofill-runtime.js").write_text("// runtime")
    db_path = tmp_path / "applications.db"

    monkeypatch.setattr(
        "job_agent.cli._execution_summary_for_package",
        lambda path: {
            "company": "Acme AI",
            "title": "ML Engineer",
            "apply_url": "https://jobs.example.com/acme",
            "package_dir": str(path),
            "runtime_script_path": str(path / "autofill-runtime.js"),
        },
    )

    result = CliRunner().invoke(
        app,
        ["applications", "reconcile-root", str(root), "--db", str(db_path)],
    )

    assert result.exit_code == 0, result.output
    assert "found 1 confirmed package" in result.output
    conn = sqlite3.connect(db_path)
    row = conn.execute("select status from applications").fetchone()
    conn.close()
    assert row == ("submitted",)


def test_previously_submitted_filter_prefers_exact_url_and_uses_title_without_url():
    job = Job(
        company="Acme AI",
        title="Machine Learning Engineer",
        source="test",
        raw_jd="",
        apply_url="https://jobs.example.com/acme/new-role/",
    )
    submitted_titles = {("acme ai", "machine learning engineer")}

    assert not cli._was_previously_submitted(
        job,
        {"https://jobs.example.com/acme/old-role"},
        submitted_titles,
    )
    assert cli._was_previously_submitted(
        job,
        {"https://jobs.example.com/acme/new-role"},
        submitted_titles,
    )
    assert cli._was_previously_submitted(
        Job(company="Acme AI", title="Machine Learning Engineer", source="test", raw_jd=""),
        set(),
        submitted_titles,
    )


def test_saved_page_evidence_recovers_nuro_submission_copy(tmp_path):
    package_dir = tmp_path / "nuro"
    package_dir.mkdir()
    (package_dir / "submission-click-unconfirmed.txt").write_text(
        "url: https://www.nuro.ai/careersitem?gh_jid=7351066\n"
        "title: Work at Nuro | Nuro\n\n"
        "page_text_head:\nSubmitted, thanks!\n"
    )

    confirmation = cli._confirmation_from_saved_click_evidence(package_dir)

    assert confirmation is not None
    assert "submitted thanks" in confirmation


def test_saved_processing_error_evidence_recovers_delayed_success_copy(tmp_path):
    package_dir = tmp_path / "brainco"
    package_dir.mkdir()
    (package_dir / "submission-processing-error.txt").write_text(
        "url: https://jobs.ashbyhq.com/brainco/example/application\n"
        "title: Early Career AI/ML Engineer @ Brain Co.\n\n"
        "page_text_head:\nYour application was successfully submitted.\n"
    )

    confirmation = cli._confirmation_from_saved_click_evidence(package_dir)

    assert confirmation is not None
    assert "successfully submitted" in confirmation


def test_cli_pipeline_run_skips_prior_terminal_outcomes_before_prepare(tmp_path, monkeypatch):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel>
        <item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        <item>
        <title>Backend Engineer at WebCo</title>
        <link>https://jobs.example.com/webco-backend</link>
        <description>Build backend APIs with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        </channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    db_path = tmp_path / "agent.db"
    _write_application_status(
        db_path,
        Job(
            title="Agent Engineer",
            company="Acme AI",
            raw_jd="Build production LLM agents with Python and FastAPI.",
            source="company-rss",
            apply_url="https://jobs.example.com/acme-agent",
        ),
        "autofill_timed_out",
    )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append(job.title)
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
            "application_id": None,
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == ["Backend Engineer"]
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    assert manifest["counts"]["prepared"] == 1
    skipped = json.loads((out_dir / "prior-terminal-outcomes.json").read_text())
    assert skipped[0]["title"] == "Agent Engineer"


def test_cli_pipeline_run_skips_company_after_anti_spam_blocker(tmp_path, monkeypatch):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel>
        <item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        <item>
        <title>Backend Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-backend</link>
        <description>Build backend APIs with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        <item>
        <title>Backend Engineer at WebCo</title>
        <link>https://jobs.example.com/webco-backend</link>
        <description>Build backend APIs with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        </channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS", "24")
    _write_application_status(
        db_path,
        Job(
            title="Blocked Agent Engineer",
            company="Acme AI",
            raw_jd="Build production LLM agents with Python and FastAPI.",
            source="company-rss",
            apply_url="https://blocked.example.com/acme-agent",
        ),
        "submission_blocked_by_anti_spam",
    )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append((job.company, job.title))
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
            "application_id": None,
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == [("WebCo", "Backend Engineer")]
    skipped = json.loads((out_dir / "prior-terminal-outcomes.json").read_text())
    assert [item["title"] for item in skipped] == ["Agent Engineer", "Backend Engineer"]
    assert all(item["reason"] == "matching company has prior anti-spam blocker" for item in skipped)


def test_cli_pipeline_run_skips_open_failure_circuit_and_uses_other_company(
    tmp_path,
    monkeypatch,
):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel>
        <item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        <item>
        <title>Backend Engineer at WebCo</title>
        <link>https://jobs.other.example/webco-backend</link>
        <description>Build backend APIs with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        </channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "type": "rss",
                        "source": "company-rss",
                        "rss_file": str(rss_path),
                    }
                ]
            }
        )
    )
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    for index in range(2):
        _write_application_status(
            db_path,
            Job(
                title=f"Failed Role {index}",
                company="Acme AI",
                raw_jd="Build systems.",
                source="company-rss",
                apply_url=f"https://failed.example/acme-{index}",
            ),
            "autofill_failed",
        )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append((job.company, job.title))
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
            "application_id": None,
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == [("WebCo", "Backend Engineer")]
    skipped = json.loads((out_dir / "prior-terminal-outcomes.json").read_text())
    assert skipped == [
        {
            "title": "Agent Engineer",
            "company": "Acme AI",
            "apply_url": "https://jobs.example.com/acme-agent",
            "reason": "matching company failure circuit is open: autofill_failed",
        }
    ]


def test_anti_spam_company_cooldown_uses_latest_recent_outcome(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS", "24")
    _write_application_status(
        db_path,
        Job(
            title="Recent Role",
            company="Recent Co",
            raw_jd="Build systems.",
            source="test",
            apply_url="https://recent.example/jobs/1",
        ),
        "submission_blocked_by_anti_spam",
    )
    expired_id = _write_application_status(
        db_path,
        Job(
            title="Expired Role",
            company="Expired Co",
            raw_jd="Build systems.",
            source="test",
            apply_url="https://expired.example/jobs/1",
        ),
        "submission_blocked_by_anti_spam",
    )
    _write_application_status(
        db_path,
        Job(
            title="Blocked Role",
            company="Recovered Co",
            raw_jd="Build systems.",
            source="test",
            apply_url="https://recovered.example/jobs/1",
        ),
        "submission_blocked_by_anti_spam",
    )
    _write_application_status(
        db_path,
        Job(
            title="Submitted Role",
            company="Recovered Co",
            raw_jd="Build systems.",
            source="test",
            apply_url="https://recovered.example/jobs/2",
        ),
        "submitted",
    )
    conn = connect(db_path)
    conn.execute(
        "update applications set updated_at = datetime('now', '-25 hours') where id = ?",
        (expired_id,),
    )
    conn.commit()
    conn.close()

    assert cli._anti_spam_blocked_companies(db_path) == {"recent co"}


def test_anti_spam_host_key_isolates_ashby_boards():
    assert (
        cli._anti_spam_host_key(
            "https://jobs.ashbyhq.com/brainco/abc123/application"
        )
        == "jobs.ashbyhq.com/brainco"
    )
    assert (
        cli._anti_spam_host_key(
            "https://jobs.ashbyhq.com/anotherco/xyz789/application"
        )
        == "jobs.ashbyhq.com/anotherco"
    )


def test_failure_circuit_breaker_opens_after_two_consecutive_same_failures(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS", "6")
    for index in range(2):
        _write_application_status(
            db_path,
            Job(
                title=f"ML Engineer {index}",
                company="Acme AI",
                raw_jd="Build ML systems.",
                source="greenhouse:acme",
                apply_url=f"https://job-boards.greenhouse.io/acme/jobs/{100 + index}",
            ),
            "autofill_failed",
        )

    companies, adapters = cli._failure_circuit_breakers(db_path)

    assert companies == {"acme ai": "autofill_failed"}
    assert adapters == {"job-boards.greenhouse.io/acme": "autofill_failed"}


def test_failure_circuit_breaker_opens_after_two_field_blocked_outcomes(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS", "6")
    for index in range(2):
        _write_application_status(
            db_path,
            Job(
                title=f"Software Engineer {index}",
                company="Robinhood",
                raw_jd="Build financial systems.",
                source="greenhouse:robinhood",
                apply_url=f"https://job-boards.greenhouse.io/robinhood/jobs/{7975549 + index}",
            ),
            "autofill_completed_blocked",
        )

    companies, adapters = cli._failure_circuit_breakers(db_path)

    assert companies == {"robinhood": "autofill_completed_blocked"}
    assert adapters == {
        "job-boards.greenhouse.io/robinhood": "autofill_completed_blocked"
    }


def test_failure_circuit_breaker_requires_consecutive_equivalent_failures(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    for index, status in enumerate(["autofill_failed", "autofill_timed_out"]):
        _write_application_status(
            db_path,
            Job(
                title=f"ML Engineer {index}",
                company="Acme AI",
                raw_jd="Build ML systems.",
                source="greenhouse:acme",
                apply_url=f"https://job-boards.greenhouse.io/acme/jobs/{200 + index}",
            ),
            status,
        )

    assert cli._failure_circuit_breakers(db_path) == ({}, {})


def test_failure_circuit_breaker_closes_after_success_or_window_expiry(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_THRESHOLD", "2")
    monkeypatch.setenv("JOB_AGENT_FAILURE_CIRCUIT_BREAKER_HOURS", "6")
    application_ids = []
    for index in range(2):
        application_ids.append(
            _write_application_status(
                db_path,
                Job(
                    title=f"ML Engineer {index}",
                    company="Acme AI",
                    raw_jd="Build ML systems.",
                    source="greenhouse:acme",
                    apply_url=f"https://job-boards.greenhouse.io/acme/jobs/{300 + index}",
                ),
                "submission_processing_error",
            )
        )
    _write_submitted_application(
        db_path,
        Job(
            title="Recovered Engineer",
            company="Acme AI",
            raw_jd="Build ML systems.",
            source="greenhouse:acme",
            apply_url="https://job-boards.greenhouse.io/acme/jobs/399",
        ),
    )

    assert cli._failure_circuit_breakers(db_path) == ({}, {})

    expiry_db = tmp_path / "expired.db"
    expired_ids = []
    for index in range(2):
        expired_ids.append(
            _write_application_status(
                expiry_db,
                Job(
                    title=f"Old Engineer {index}",
                    company="Old Co",
                    raw_jd="Build systems.",
                    source="test",
                    apply_url=f"https://old.example/jobs/{index}",
                ),
                "autofill_failed",
            )
        )
    conn = connect(expiry_db)
    conn.executemany(
        "update applications set updated_at = datetime('now', '-7 hours') where id = ?",
        [(application_id,) for application_id in expired_ids],
    )
    conn.commit()
    conn.close()

    assert cli._failure_circuit_breakers(expiry_db) == ({}, {})


def test_failure_adapter_key_keeps_shared_ats_boards_isolated():
    assert (
        cli._failure_adapter_key(
            "https://job-boards.greenhouse.io/acme/jobs/123",
            "Acme",
        )
        == "job-boards.greenhouse.io/acme"
    )
    assert (
        cli._failure_adapter_key(
            "https://job-boards.greenhouse.io/other/jobs/456",
            "Other",
        )
        == "job-boards.greenhouse.io/other"
    )


def test_cli_pipeline_run_does_not_skip_company_after_matching_successful_submission(
    tmp_path,
    monkeypatch,
):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel>
        <item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        <item>
        <title>Backend Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-backend</link>
        <description>Build backend APIs with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        </channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    db_path = tmp_path / "agent.db"
    _write_application_status(
        db_path,
        Job(
            title="Blocked Role",
            company="Acme AI",
            raw_jd="Build production LLM agents with Python and FastAPI.",
            source="company-rss",
            apply_url="https://jobs.example.com/acme-blocked",
        ),
        "submission_blocked_by_anti_spam",
    )
    _write_submitted_application(
        db_path,
        Job(
            title="Submitted Role",
            company="Acme AI",
            raw_jd="Build production LLM agents with Python and FastAPI.",
            source="company-rss",
            apply_url="https://jobs.example.com/acme-submitted",
        ),
    )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append((job.company, job.title))
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
            "application_id": None,
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == [("Acme AI", "Agent Engineer")]
    assert not (out_dir / "prior-terminal-outcomes.json").exists()


def test_cli_pipeline_run_skips_host_after_repeated_anti_spam_blockers(tmp_path, monkeypatch):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel>
        <item>
        <title>Agent Engineer at FreshCo</title>
        <link>https://jobs.example.com/freshco-agent</link>
        <description>Build production LLM agents with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        <item>
        <title>Backend Engineer at WebCo</title>
        <link>https://jobs.other.example/webco-backend</link>
        <description>Build backend APIs with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        </channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD", "2")
    for index, company in enumerate(["Blocked One", "Blocked Two"], start=1):
        _write_application_status(
            db_path,
            Job(
                title=f"Blocked Role {index}",
                company=company,
                raw_jd="Build production LLM agents with Python and FastAPI.",
                source="company-rss",
                apply_url=f"https://jobs.example.com/blocked-{index}",
            ),
            "submission_blocked_by_anti_spam",
        )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append((job.company, job.title))
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
            "application_id": None,
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == [("WebCo", "Backend Engineer")]
    skipped = json.loads((out_dir / "prior-terminal-outcomes.json").read_text())
    assert skipped[0]["title"] == "Agent Engineer"
    assert skipped[0]["reason"] == "matching apply host has repeated anti-spam blockers: jobs.example.com"


def test_anti_spam_host_cooldown_ignores_expired_blocks(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD", "2")
    monkeypatch.setenv("JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS", "24")
    application_ids = []
    for index in range(2):
        application_ids.append(
            _write_application_status(
                db_path,
                Job(
                    title=f"Old Role {index}",
                    company=f"Old Co {index}",
                    raw_jd="Build systems.",
                    source="test",
                    apply_url=f"https://jobs.example.com/old-{index}",
                ),
                "submission_blocked_by_anti_spam",
            )
        )
    conn = connect(db_path)
    conn.executemany(
        "update applications set updated_at = datetime('now', '-25 hours') where id = ?",
        [(application_id,) for application_id in application_ids],
    )
    conn.commit()
    conn.close()

    assert cli._anti_spam_blocked_hosts(db_path) == set()


def test_cli_pipeline_run_does_not_skip_host_when_successes_offset_anti_spam_blockers(
    tmp_path,
    monkeypatch,
):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel>
        <item>
        <title>Agent Engineer at FreshCo</title>
        <link>https://jobs.example.com/freshco-agent</link>
        <description>Build production LLM agents with Python and FastAPI.</description>
        <category>Remote</category>
        </item>
        </channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("JOB_AGENT_ANTI_SPAM_HOST_COOLDOWN_THRESHOLD", "2")
    for index, status in enumerate(
        [
            "submission_blocked_by_anti_spam",
            "submission_blocked_by_anti_spam",
            "submitted",
            "submitted",
        ],
        start=1,
    ):
        _write_application_status(
            db_path,
            Job(
                title=f"Prior Role {index}",
                company=f"Prior Co {index}",
                raw_jd="Build production LLM agents with Python and FastAPI.",
                source="company-rss",
                apply_url=f"https://jobs.example.com/prior-{index}",
            ),
            status,
        )
    prepared = []

    def fake_prepare(job, package_dir, **kwargs):
        prepared.append((job.company, job.title))
        package_dir.mkdir(parents=True)
        return {
            "company": job.company,
            "title": job.title,
            "apply_url": job.apply_url,
            "package_dir": str(package_dir),
            "runtime_script_path": str(package_dir / "autofill-runtime.js"),
            "application_id": None,
        }

    monkeypatch.setattr("job_agent.cli._prepare_application_package", fake_prepare)
    out_dir = tmp_path / "pipeline-run"

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
            "--min-score",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    assert prepared == [("FreshCo", "Agent Engineer")]
    assert not (out_dir / "prior-terminal-outcomes.json").exists()


def test_cli_pipeline_run_execute_writes_manifest_and_audit(tmp_path, monkeypatch):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    out_dir = tmp_path / "pipeline-run"

    def fake_unified_runtime(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        from subprocess import CompletedProcess

        action_runner(
            "ats_observe_page",
            "observe",
            {"phase": "page_observation"},
            lambda: [{"type": "text", "required": True}],
        )
        action_runner(
            "ats_fill_fields",
            "write",
            {"phase": "field_fill"},
            lambda: {"filled": [{"label": "Name"}], "review": []},
        )
        action_runner(
            "ats_submit_application",
            "submit",
            {
                "phase": "final_submission",
                "application_url": "https://jobs.example.com/acme-agent",
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
            stdout="Autofill stats: filled=2 review=0\nSubmission confirmed: matched thank you",
            stderr="",
        )

    monkeypatch.setattr(
        "job_agent.execution._run_python_runtime_in_process",
        fake_unified_runtime,
    )

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run-execute",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
            "--required-resume-pdf",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
            "--timeout-seconds",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "executed 1 applications" in result.output
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    assert manifest["counts"] == {"imported": 1, "shortlisted": 1, "prepared": 1}
    assert manifest["artifacts"]["batch_summary"].endswith("batch-summary.json")
    assert manifest["artifacts"]["execution_audit"].endswith("execution-audit.json")
    assert manifest["artifacts"]["resume_preflight"].endswith("resume-preflight.json")
    assert manifest["execution_counts"] == {
        "total": 1,
        "completed": 0,
        "submitted": 1,
        "submit_clicked_unconfirmed": 0,
        "email_verification_required": 0,
        "submission_processing_error": 0,
        "submission_blocked_by_anti_spam": 0,
        "candidate_account_required": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert manifest["resume_preflight_counts"] == {"total": 1, "verified": 1, "invalid": 0}
    audit = json.loads((out_dir / "execution-audit.json").read_text())
    preflight = json.loads((out_dir / "resume-preflight.json").read_text())
    expected_sha = hashlib.sha256(b"%PDF-1.4\n").hexdigest()
    assert manifest["required_resume_pdf"] == str(resume_path)
    assert manifest["required_resume_pdf_sha256"] == expected_sha
    assert preflight["counts"] == {"total": 1, "verified": 1, "invalid": 0}
    assert preflight["applications"][0]["upload_resume_pdf_sha256"] == expected_sha
    assert audit["required_resume_pdf"] == str(resume_path)
    assert audit["required_resume_pdf_sha256"] == expected_sha
    assert audit["applications"][0]["upload_resume_pdf_sha256"] == expected_sha
    assert audit["applications"][0]["required_resume_pdf_sha256"] == expected_sha
    assert audit["applications"][0]["status"] == "submitted"
    assert "candidate@example.com" not in (out_dir / "execution-audit.json").read_text()


def test_cli_pipeline_run_execute_uses_configured_resume_source_dir_for_preflight(
    tmp_path,
    monkeypatch,
):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Machine Learning Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-mle</link>
        <description>Build PyTorch training pipelines and production ML evaluation systems.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_dir = tmp_path / "configured-resumes"
    resume_dir.mkdir()
    selected_resume = resume_dir / "GAOYI_WU_MLE.pdf"
    selected_resume.write_bytes(b"%PDF-1.4\nPyTorch training pipelines")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    out_dir = tmp_path / "pipeline-run"
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))

    def fake_unified_runtime(
        script_path,
        *,
        runtime_env,
        action_runner,
        timeout_seconds=None,
    ):
        from subprocess import CompletedProcess

        action_runner(
            "ats_observe_page",
            "observe",
            {"phase": "page_observation"},
            lambda: [{"type": "text", "required": True}],
        )
        action_runner(
            "ats_fill_fields",
            "write",
            {"phase": "field_fill"},
            lambda: {"filled": [{"label": "Name"}], "review": []},
        )
        return CompletedProcess(
            [sys.executable, script_path],
            0,
            stdout=(
                "Autofill stats: filled=2 review=0\n"
                "Submit gate: automatic submission not performed"
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "job_agent.execution._run_python_runtime_in_process",
        fake_unified_runtime,
    )

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run-execute",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
            "--timeout-seconds",
            "10",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    preflight = json.loads((out_dir / "resume-preflight.json").read_text())
    audit = json.loads((out_dir / "execution-audit.json").read_text())
    assert manifest["required_resume_source_dir"] == str(resume_dir)
    assert preflight["required_resume_source_dir"] == str(resume_dir)
    assert audit["required_resume_source_dir"] == str(resume_dir)
    assert preflight["applications"][0]["upload_resume_pdf_resolved_path"] == str(
        selected_resume.resolve()
    )


def test_cli_pipeline_run_execute_use_llm_enables_runtime_llm_answers(
    tmp_path,
    monkeypatch,
):
    observed = {}
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nPython RAG FastAPI Docker")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    out_dir = tmp_path / "pipeline-run"

    def fake_execute(items, **kwargs):
        observed["env"] = os.environ.get("JOB_AGENT_LLM_ANSWERS")
        return [
            {
                "company": "Acme AI",
                "title": "Agent Engineer",
                "script_path": items[0]["runtime_script_path"],
                "status": "autofill_completed",
                "exit_code": 0,
                "submit_gate": "automatic_submission_enabled",
                "error": None,
                "filled_count": 1,
                "review_count": 0,
            }
        ]

    monkeypatch.delenv("JOB_AGENT_LLM_ANSWERS", raising=False)
    monkeypatch.setattr("job_agent.cli._build_llm", lambda **kwargs: cli.DeterministicLLM())
    monkeypatch.setattr("job_agent.cli.execute_application_batch", fake_execute)

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run-execute",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
            "--required-resume-pdf",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
            "--use-llm",
        ],
    )

    assert result.exit_code == 0, result.output
    assert observed["env"] == "1"
    assert os.environ.get("JOB_AGENT_LLM_ANSWERS") is None
    manifest = json.loads((out_dir / "pipeline-manifest.json").read_text())
    assert manifest["runtime_llm_answers_enabled"] is True


def test_cli_pipeline_build_runner_executes_generated_runtime_with_fake_playwright(tmp_path, monkeypatch):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for build-runner end-to-end smoke test")

    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build production LLM agents with Python, RAG, FastAPI, and Docker.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    sources_path = tmp_path / "sources.json"
    sources_path.write_text(
        json.dumps({"sources": [{"type": "rss", "source": "company-rss", "rss_file": str(rss_path)}]})
    )
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"name": "Candidate", "email": "candidate@example.com"}))
    out_dir = tmp_path / "pipeline-run"
    runner = CliRunner()

    pipeline = runner.invoke(
        app,
        [
            "pipeline",
            "run",
            str(sources_path),
            "--out-dir",
            str(out_dir),
            "--resume",
            str(resume_path),
            "--profile",
            str(profile_path),
            "--min-score",
            "0",
        ],
    )

    assert pipeline.exit_code == 0, pipeline.output
    expected_sha = hashlib.sha256(b"%PDF-1.4\n").hexdigest()
    summary_path = out_dir / "applications" / "batch-summary.json"
    summary = json.loads(summary_path.read_text())
    package_dir = Path(summary[0]["package_dir"])
    _write_fake_runtime_playwright(package_dir)
    runner_path = out_dir / "applications" / "run-batch.js"

    build_runner = runner.invoke(
        app,
        [
            "applications",
            "build-batch-runner",
            str(summary_path),
            "--out",
            str(runner_path),
        ],
    )

    assert build_runner.exit_code == 0, build_runner.output
    runner_preflight = json.loads(
        (out_dir / "applications" / "resume-preflight.json").read_text()
    )
    assert runner_preflight["applications"][0]["required_resume_pdf_path"] == str(
        resume_path
    )
    assert runner_preflight["applications"][0]["required_resume_pdf_sha256"] == expected_sha
    result = subprocess.run(
        ["node", str(runner_path)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Preparing Acme AI - Agent Engineer" in result.stdout
    assert "fake goto https://jobs.example.com/acme-agent" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout
    assert "Submit policy: final Submit is automatic unless JOB_AGENT_SUBMIT_COMPLETE=0." in result.stdout


def test_cli_applications_execute_batch_writes_privacy_safe_audit(tmp_path, monkeypatch):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text("console.log('candidate@example.com'); console.log('Submit gate: STOPPED before final Submit')")
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_path = resume_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text(
        json.dumps(
            [
                {
                    "company": "Acme",
                    "title": "Agent Engineer",
                    "package_dir": str(package_dir),
                    "runtime_script_path": str(script_path),
                    "upload_resume_path": str(resume_path),
                }
            ]
        )
    )
    audit_path = tmp_path / "execution-audit.json"

    def fake_stream(command, timeout_seconds):
        from subprocess import CompletedProcess

        assert timeout_seconds == 300
        return CompletedProcess(
            command,
            0,
            stdout="candidate@example.com\nSubmit gate: STOPPED before final Submit",
            stderr="",
        )

    monkeypatch.setattr("job_agent.execution._run_script_streaming", fake_stream)
    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--audit-out",
            str(audit_path),
        ],
    )

    assert result.exit_code == 0, result.output
    audit = json.loads(audit_path.read_text())
    assert audit["counts"] == {
        "total": 1,
        "completed": 1,
        "submitted": 0,
        "submit_clicked_unconfirmed": 0,
        "email_verification_required": 0,
        "submission_processing_error": 0,
        "submission_blocked_by_anti_spam": 0,
        "candidate_account_required": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert audit["submit_gate"] == "automatic_submission_enabled"
    assert audit["applications"][0]["status"] == "autofill_completed_blocked"
    assert "candidate@example.com" not in audit_path.read_text()


def test_execution_audit_is_persisted_after_each_terminal_application(
    tmp_path,
    monkeypatch,
):
    first_script = tmp_path / "first-runtime.js"
    second_script = tmp_path / "second-runtime.js"
    first_script.write_text("console.log('runtime')")
    second_script.write_text("console.log('runtime')")
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    audit_path = tmp_path / "execution-audit.json"
    summary_items = [
        {
            "company": "BlockedCo",
            "title": "First Role",
            "runtime_script_path": str(first_script),
            "upload_resume_path": str(resume_path),
        },
        {
            "company": "NextCo",
            "title": "Second Role",
            "runtime_script_path": str(second_script),
            "upload_resume_path": str(resume_path),
        },
    ]

    def interrupted_execute(items, **kwargs):
        record = {
            "company": "BlockedCo",
            "title": "First Role",
            "script_path": str(first_script),
            "status": "submission_blocked_by_anti_spam",
            "exit_code": 0,
            "submit_gate": "submission_blocked_by_anti_spam",
            "error": "submission_blocked_by_anti_spam",
            "filled_count": 12,
            "review_count": 0,
        }
        kwargs["on_record"](record, 1, len(items))
        raise RuntimeError("simulated batch interruption")

    monkeypatch.setattr("job_agent.cli.execute_application_batch", interrupted_execute)

    with pytest.raises(RuntimeError, match="simulated batch interruption"):
        cli._write_execution_audit(summary_items, audit_path)

    audit = json.loads(audit_path.read_text())
    assert audit["progress"] == {
        "planned": 2,
        "terminal": 1,
        "remaining": 1,
        "complete": False,
    }
    assert [record["status"] for record in audit["applications"]] == [
        "submission_blocked_by_anti_spam"
    ]


def test_execution_audit_resume_preserves_terminal_records_and_skips_interrupted_item(
    tmp_path,
    monkeypatch,
):
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\n")
    summary_items = []
    for index in range(1, 5):
        script_path = tmp_path / f"{index:03d}-runtime.js"
        script_path.write_text("console.log('runtime')")
        summary_items.append(
            {
                "company": f"Company {index}",
                "title": f"Role {index}",
                "runtime_script_path": str(script_path),
                "upload_resume_path": str(resume_path),
            }
        )

    audit_path = tmp_path / "execution-audit.json"
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "progress": {
                    "planned": 4,
                    "terminal": 2,
                    "remaining": 2,
                    "complete": False,
                },
                "applications": [
                    {
                        "company": "Company 1",
                        "title": "Role 1",
                        "script_path": summary_items[0]["runtime_script_path"],
                        "status": "submitted",
                    },
                    {
                        "company": "Company 4",
                        "title": "Role 4",
                        "script_path": summary_items[3]["runtime_script_path"],
                        "status": "skipped_prior_terminal_outcome",
                    },
                ],
            }
        )
    )
    observed: dict[str, object] = {}

    def resumed_execute(items, **kwargs):
        observed["scripts"] = [item["runtime_script_path"] for item in items]
        record = {
            "company": "Company 3",
            "title": "Role 3",
            "script_path": summary_items[2]["runtime_script_path"],
            "status": "submitted",
            "exit_code": 0,
            "submit_gate": "submission_confirmed",
            "error": None,
            "filled_count": 5,
            "review_count": 0,
        }
        kwargs["on_record"](record, 1, len(items))
        return [record]

    monkeypatch.setattr("job_agent.cli.execute_application_batch", resumed_execute)

    audit = cli._write_execution_audit(
        summary_items,
        audit_path,
        resume_existing_audit=True,
    )

    assert observed["scripts"] == [summary_items[2]["runtime_script_path"]]
    by_script = {
        record["script_path"]: record for record in audit["applications"]
    }
    assert by_script[summary_items[0]["runtime_script_path"]]["status"] == "submitted"
    assert (
        by_script[summary_items[1]["runtime_script_path"]]["status"]
        == "submit_clicked_unconfirmed"
    )
    assert (
        by_script[summary_items[1]["runtime_script_path"]]["error"]
        == "interrupted_execution_outcome_unknown"
    )
    assert (
        by_script[summary_items[1]["runtime_script_path"]][
            "recovery_plan"
        ]["strategy"]
        == "confirmation_reconciliation"
    )
    assert by_script[summary_items[2]["runtime_script_path"]]["status"] == "submitted"
    assert (
        by_script[summary_items[3]["runtime_script_path"]]["status"]
        == "skipped_prior_terminal_outcome"
    )
    assert audit["progress"] == {
        "planned": 4,
        "terminal": 4,
        "remaining": 0,
        "complete": True,
    }
    assert audit["resume"] == {
        "preserved_terminal": 2,
        "interrupted_marked_unconfirmed": 1,
        "remaining_after_interrupted": 1,
    }


def test_cli_execute_batch_rejects_resume_combined_with_retry(tmp_path):
    summary_path = tmp_path / "batch-summary.json"
    summary_path.write_text("[]")

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "execute-batch",
            str(summary_path),
            "--resume-existing-audit",
            "--retry-prior-terminal-outcome",
        ],
    )

    assert result.exit_code != 0
    assert "cannot be combined" in result.output
