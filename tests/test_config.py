from job_agent.config import AppConfig, load_env


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


def test_load_env_reads_file_without_overriding_existing(monkeypatch, tmp_path):
    import os

    # isolate from any LLM_* values that may have leaked into the session env
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment\n"
        'OPENAI_API_KEY="sk-from-file"\n'
        "LLM_MODEL_ID=gpt-4o-mini\n"
        "RESUME_SOURCE_DIR=\n"  # blank value must be skipped
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")

    loaded = load_env(env_path)

    # existing shell var is NOT overridden by the file
    assert os.environ["OPENAI_API_KEY"] == "sk-from-shell"
    # file-only var is loaded
    assert os.environ["LLM_MODEL_ID"] == "gpt-4o-mini"
    assert loaded == {"LLM_MODEL_ID": "gpt-4o-mini"}
    # blank value was skipped
    assert "RESUME_SOURCE_DIR" not in loaded


def test_load_env_missing_file_is_noop(tmp_path):
    assert load_env(tmp_path / "no-such.env") == {}
