from __future__ import annotations

from job_agent.models import Job


def _field_from_text(text: str, label: str) -> str | None:
    prefix = f"{label}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix) :].strip() or None
    return None


def import_job_from_text(text: str) -> Job:
    return Job(
        title=_field_from_text(text, "Title") or "Unknown Role",
        company=_field_from_text(text, "Company") or "Unknown Company",
        location=_field_from_text(text, "Location"),
        raw_jd=text,
        source="manual",
    )
