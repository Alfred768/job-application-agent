import json
from pathlib import Path
import shutil
import subprocess

import pytest

from typer.testing import CliRunner

from job_agent.cli import app


def test_prepare_selects_and_uploads_closest_original_pdf_without_creating_resume_files(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "title": "Agent Engineer",
                    "company": "Acme",
                    "raw_jd": "Build FastAPI services and LLM agent workflows.",
                    "apply_url": "https://jobs.example.com/acme",
                }
            ]
        )
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    selected_pdf = resume_dir / "GAOYI_WU_Agent_Engineer.pdf"
    selected_pdf.write_bytes(b"%PDF-1.4\nagent resume")
    (resume_dir / "GAOYI_WU_ML_Infra.pdf").write_bytes(b"%PDF-1.4\nml resume")
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    out_dir = tmp_path / "package"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume-source-dir",
            str(resume_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0, result.output
    script = (out_dir / "fill-form.js").read_text()
    assert str(selected_pdf) in script
    assert not list(out_dir.glob("tailored-resume.*"))
    assert selected_pdf.exists()


def test_prepare_falls_back_to_closest_available_pdf_when_track_is_not_exact(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [{"title": "Agent Engineer", "company": "Acme", "raw_jd": "Build FastAPI APIs."}]
        )
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    closest_pdf = resume_dir / "backend-platform.pdf"
    closest_pdf.write_bytes(b"%PDF-1.4\nPython FastAPI APIs")
    (resume_dir / "research.pdf").write_bytes(b"%PDF-1.4\nPyTorch models")
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    out_dir = tmp_path / "package"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume-source-dir",
            str(resume_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert str(closest_pdf) in (out_dir / "fill-form.js").read_text()


def test_selected_pdf_is_passed_to_the_actual_ats_upload_call(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for the upload execution test")

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [{"title": "Agent Engineer", "company": "Acme", "raw_jd": "Build agent systems."}]
        )
    )
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    selected_pdf = resume_dir / "GAOYI_WU_Agent_Engineer.pdf"
    selected_pdf.write_bytes(b"%PDF-1.4\nsource resume")
    form_path = tmp_path / "form.json"
    form_path.write_text('[{"label": "Resume", "type": "file", "required": true}]')
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}")
    out_dir = tmp_path / "package"

    result = CliRunner().invoke(
        app,
        [
            "applications",
            "prepare",
            str(jobs_path),
            "--out-dir",
            str(out_dir),
            "--resume-source-dir",
            str(resume_dir),
            "--form-snapshot",
            str(form_path),
            "--profile",
            str(profile_path),
        ],
    )
    assert result.exit_code == 0, result.output

    playwright_dir = out_dir / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const page = {
  async goto() {},
  getByLabel() {
    return {
      async setInputFiles(path) { console.log(`uploaded=${path}`); },
      async fill() {},
      async selectOption() {},
      async check() {},
    };
  },
};
module.exports = {
  chromium: {
    async launch() {
      return { async newPage() { return page; }, async close() {} };
    },
  },
};
"""
    )

    execution = subprocess.run(
        ["node", str(out_dir / "fill-form.js")],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert execution.returncode == 0, execution.stderr
    assert f"uploaded={selected_pdf}" in execution.stdout
    assert selected_pdf.exists()
