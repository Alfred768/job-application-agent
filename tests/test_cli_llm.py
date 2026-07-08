from typer.testing import CliRunner

from job_agent import cli
from job_agent.cli import DeterministicLLM, _build_llm, app


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
