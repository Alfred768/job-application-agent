from typer.testing import CliRunner

from job_agent import cli
from job_agent.cli import DeterministicLLM, _build_llm, app
import json


def test_build_llm_defaults_to_deterministic():
    llm = _build_llm(use_llm=False)

    assert isinstance(llm, DeterministicLLM)
    assert llm.provider == "deterministic"


def test_build_llm_uses_hello_agents_llm_when_requested(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    llm = _build_llm(use_llm=True, model="gpt-4o-mini", provider="openai")

    assert llm.provider == "openai"
    assert llm.model == "gpt-4o-mini"
    assert llm.api_key == "test-openai-key"


def test_cli_llm_smoke_invokes_configured_llm(monkeypatch):
    class FakeLLM:
        provider = "fake-provider"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def invoke(self, messages, **kwargs):
            return f"ok:{messages[0]['content']}:{self.kwargs['model']}"

    monkeypatch.setattr(cli, "HelloAgentsLLM", FakeLLM)
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["llm", "smoke", "--use-llm", "--llm-model", "fake-model", "--prompt", "ping"],
    )

    assert result.exit_code == 0
    assert "ok:ping:fake-model" in result.output


def test_cli_applications_prepare_uses_llm_to_rewrite_resume(monkeypatch, tmp_path):
    """When --use-llm is set, the tailored resume is rewritten by the LLM
    (with a truthfulness review section), not just deterministic keyword emphasis."""

    class FakeLLM:
        provider = "openai"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def invoke(self, messages, **kwargs):
            return "# LLM-REWRITTEN RESUME\n\nSummary targeting Agent Engineer with LangChain and FastAPI."

    monkeypatch.setattr(cli, "HelloAgentsLLM", FakeLLM)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            [
                {
                    "title": "Agent Engineer",
                    "company": "Acme AI",
                    "location": "Remote",
                    "raw_jd": "Build LLM agents with LangChain and FastAPI.",
                    "source": "greenhouse:acme",
                    "source_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
                    "remote_policy": None,
                }
            ]
        )
    )
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Gaoyi Wu\n\nBuilt FastAPI services.")
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
            "--use-llm",
        ],
    )

    assert result.exit_code == 0, result.output
    tailored = (out_dir / "tailored-resume.md").read_text()
    assert "LLM-REWRITTEN RESUME" in tailored
    assert "Truthfulness Review (LLM draft)" in tailored
