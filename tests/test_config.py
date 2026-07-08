from job_agent.config import AppConfig


def test_config_uses_env_resume_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(tmp_path))

    config = AppConfig.from_env()

    assert config.resume_source_dir == tmp_path


def test_config_defaults_output_dir_to_project_output(monkeypatch):
    monkeypatch.delenv("OUTPUT_DIR", raising=False)

    config = AppConfig.from_env()

    assert config.output_dir.name == "output"


def test_config_reads_llm_settings(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_ID", "gpt-4o-mini")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")

    config = AppConfig.from_env()

    assert config.llm_model_id == "gpt-4o-mini"
    assert config.llm_provider == "openai"
    assert config.llm_base_url == "https://api.openai.com/v1"
