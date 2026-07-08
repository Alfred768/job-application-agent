from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
