from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env(env_path: str | Path | None = None) -> dict[str, str]:
    """Load a ``.env`` file into ``os.environ`` without overriding existing vars.

    Keeps secrets out of git (``.env`` is gitignored) while letting the agent
    pick up ``OPENAI_API_KEY`` / ``LLM_*`` / ``RESUME_SOURCE_DIR`` etc. from a
    local file. Returns the variables it loaded.
    """
    path = Path(env_path) if env_path else Path.cwd() / ".env"
    if not path.is_file():
        return {}
    loaded: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ and value:
            os.environ[key] = value
            loaded[key] = value
    return loaded


@dataclass(frozen=True)
class AppConfig:
    resume_source_dir: Path
    output_dir: Path
    database_path: Path
    job_source_config_path: Path | None = None
    browser_headless: bool = True
    auto_submit_allowlist: tuple[str, ...] = ()
    capmonster_api_key: str | None = None
    capmonster_solve_captcha: bool = False
    capmonster_poll_interval_seconds: float = 3.0
    capmonster_timeout_seconds: float = 120.0
    openai_api_key: str | None = None
    llm_model_id: str | None = None
    llm_provider: str | None = None
    llm_base_url: str | None = None

    @classmethod
    def from_env(cls) -> "AppConfig":
        resume_source = os.getenv("RESUME_SOURCE_DIR", "")
        output_dir = os.getenv("OUTPUT_DIR", "output")
        database_path = os.getenv("DATABASE_PATH", "job-agent.db")
        source_config = os.getenv("JOB_SOURCE_CONFIG_PATH") or None
        browser_headless = _parse_bool(os.getenv("BROWSER_HEADLESS"), default=True)
        auto_submit_allowlist = _parse_csv(os.getenv("AUTO_SUBMIT_ALLOWLIST"))
        capmonster_api_key = os.getenv("CAPMONSTER_API_KEY") or None
        capmonster_solve_captcha = _parse_bool(os.getenv("CAPMONSTER_SOLVE_CAPTCHA"), default=False)
        capmonster_poll_interval = _parse_float(os.getenv("CAPMONSTER_POLL_INTERVAL_SECONDS"), default=3.0)
        capmonster_timeout = _parse_float(os.getenv("CAPMONSTER_TIMEOUT_SECONDS"), default=120.0)
        api_key = os.getenv("OPENAI_API_KEY") or None
        llm_model_id = os.getenv("LLM_MODEL_ID") or None
        llm_provider = os.getenv("LLM_PROVIDER") or None
        llm_base_url = os.getenv("LLM_BASE_URL") or None

        return cls(
            resume_source_dir=Path(resume_source).expanduser(),
            output_dir=Path(output_dir).expanduser(),
            database_path=Path(database_path).expanduser(),
            job_source_config_path=Path(source_config).expanduser() if source_config else None,
            browser_headless=browser_headless,
            auto_submit_allowlist=auto_submit_allowlist,
            capmonster_api_key=capmonster_api_key,
            capmonster_solve_captcha=capmonster_solve_captcha and bool(capmonster_api_key),
            capmonster_poll_interval_seconds=capmonster_poll_interval,
            capmonster_timeout_seconds=capmonster_timeout,
            openai_api_key=api_key,
            llm_model_id=llm_model_id,
            llm_provider=llm_provider,
            llm_base_url=llm_base_url,
        )


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_csv(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _parse_float(raw: str | None, default: float) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default
