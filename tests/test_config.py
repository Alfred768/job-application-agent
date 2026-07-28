from pathlib import Path

from job_agent.config import AppConfig, load_env


def test_env_example_documents_runtime_config_keys():
    keys = {
        line.split("=", 1)[0]
        for line in Path(".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert {
        "OPENAI_API_KEY",
        "LLM_PROVIDER",
        "LLM_MODEL_ID",
        "LLM_BASE_URL",
        "RESUME_SOURCE_DIR",
        "OUTPUT_DIR",
        "DATABASE_PATH",
        "JOB_SOURCE_CONFIG_PATH",
        "BROWSER_HEADLESS",
        "AUTO_SUBMIT_ALLOWLIST",
        "CAPMONSTER_API_KEY",
        "CAPMONSTER_SOLVE_CAPTCHA",
        "CAPMONSTER_POLL_INTERVAL_SECONDS",
        "CAPMONSTER_TIMEOUT_SECONDS",
        "JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS",
    } <= keys


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


def test_config_reads_safe_policy_settings(monkeypatch, tmp_path):
    source_config = tmp_path / "sources.json"
    monkeypatch.setenv("JOB_SOURCE_CONFIG_PATH", str(source_config))
    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    monkeypatch.setenv("AUTO_SUBMIT_ALLOWLIST", "greenhouse:acme, lever:example ,,")

    config = AppConfig.from_env()

    assert config.job_source_config_path == source_config
    assert config.browser_headless is False
    assert config.auto_submit_allowlist == ("greenhouse:acme", "lever:example")


def test_config_reads_capmonster_settings(monkeypatch):
    monkeypatch.setenv("CAPMONSTER_API_KEY", "capmonster-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.setenv("CAPMONSTER_POLL_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("CAPMONSTER_TIMEOUT_SECONDS", "90")

    config = AppConfig.from_env()

    assert config.capmonster_api_key == "capmonster-key"
    assert config.capmonster_solve_captcha is True
    assert config.capmonster_poll_interval_seconds == 2.5
    assert config.capmonster_timeout_seconds == 90


def test_config_does_not_enable_capmonster_without_key(monkeypatch):
    monkeypatch.delenv("CAPMONSTER_API_KEY", raising=False)
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")

    config = AppConfig.from_env()

    assert config.capmonster_api_key is None
    assert config.capmonster_solve_captcha is False


def test_config_policy_defaults_are_safe(monkeypatch):
    monkeypatch.delenv("JOB_SOURCE_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    monkeypatch.delenv("AUTO_SUBMIT_ALLOWLIST", raising=False)
    monkeypatch.delenv("CAPMONSTER_API_KEY", raising=False)
    monkeypatch.delenv("CAPMONSTER_SOLVE_CAPTCHA", raising=False)

    config = AppConfig.from_env()

    assert config.job_source_config_path is None
    assert config.browser_headless is True
    assert config.auto_submit_allowlist == ()
    assert config.capmonster_solve_captcha is False


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
