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

    @classmethod
    def from_env(cls) -> "AppConfig":
        resume_source = os.getenv("RESUME_SOURCE_DIR", "")
        output_dir = os.getenv("OUTPUT_DIR", "output")
        database_path = os.getenv("DATABASE_PATH", "job-agent.db")
        api_key = os.getenv("OPENAI_API_KEY") or None

        return cls(
            resume_source_dir=Path(resume_source).expanduser(),
            output_dir=Path(output_dir).expanduser(),
            database_path=Path(database_path).expanduser(),
            openai_api_key=api_key,
        )
