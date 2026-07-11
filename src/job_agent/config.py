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
    openai_api_key: str | None = None
    llm_model_id: str | None = None
    llm_provider: str | None = None
    llm_base_url: str | None = None

    @classmethod
    def from_env(cls) -> "AppConfig":
        resume_source = os.getenv("RESUME_SOURCE_DIR", "")
        output_dir = os.getenv("OUTPUT_DIR", "output")
        database_path = os.getenv("DATABASE_PATH", "job-agent.db")
        api_key = os.getenv("OPENAI_API_KEY") or None
        llm_model_id = os.getenv("LLM_MODEL_ID") or None
        llm_provider = os.getenv("LLM_PROVIDER") or None
        llm_base_url = os.getenv("LLM_BASE_URL") or None

        return cls(
            resume_source_dir=Path(resume_source).expanduser(),
            output_dir=Path(output_dir).expanduser(),
            database_path=Path(database_path).expanduser(),
            openai_api_key=api_key,
            llm_model_id=llm_model_id,
            llm_provider=llm_provider,
            llm_base_url=llm_base_url,
        )
