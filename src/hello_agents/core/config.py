"""Configuration shared by reusable agent strategies."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from pydantic import BaseModel


class Config(BaseModel):
    """Runtime defaults for generic agent strategies."""

    default_model: str = "gpt-3.5-turbo"
    default_provider: str = "openai"
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    debug: bool = False
    log_level: str = "INFO"
    max_history_length: int = 100

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            debug=os.getenv("DEBUG", "false").lower() == "true",
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            temperature=float(os.getenv("TEMPERATURE", "0.7")),
            max_tokens=(
                int(os.environ["MAX_TOKENS"])
                if os.getenv("MAX_TOKENS")
                else None
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.model_dump()
