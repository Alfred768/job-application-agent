from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ResumeTemplate:
    track: str
    docx_path: Path | None = None
    pdf_path: Path | None = None
    parsed_text: str | None = None


@dataclass(frozen=True)
class Job:
    title: str
    company: str
    raw_jd: str
    location: str | None = None
    source: str = "manual"
    source_url: str | None = None
    apply_url: str | None = None
    remote_policy: str | None = None


@dataclass(frozen=True)
class FitScore:
    score: int
    role_track: str
    matched_skills: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    recommendation: str = "review"
    explanation: str = ""
