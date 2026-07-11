"""Sensitive-answer knowledge base.

A pre-filled, user-approved bank for sensitive application fields (work
authorization, sponsorship, salary, relocation, start date, EEO/demographic,
disability, veteran, legal attestation). The user fills it once and marks each
entry ``approved: true``; the form fillers then auto-fill those fields instead
of leaving them for manual review.

Safety contract (matches the PEAS design):
- A sensitive field is auto-filled ONLY when the KB has an ``approved`` answer
  whose label patterns match the field. That approval IS the user's explicit
  confirmation the PEAS "sensitive-field gate" requires.
- Unmatched or unapproved sensitive fields stay review-required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Standard sensitive question families. ``patterns`` are lowercased substrings
# matched against a normalized field label; ``example`` is a safe placeholder.
SENSITIVE_FIELD_DEFS: list[dict[str, Any]] = [
    {
        "key": "work_authorization",
        "label": "Work Authorization",
        "patterns": ["authorized to work", "work authorization", "legally authorized", "eligible to work"],
        "example": "Yes",
    },
    {
        "key": "sponsorship",
        "label": "Visa Sponsorship",
        "patterns": ["sponsorship", "require sponsorship", "visa sponsorship", "sponsor", "require visa"],
        "example": "No",
    },
    {
        "key": "salary",
        "label": "Salary Expectation",
        "patterns": ["salary", "compensation", "desired salary", "salary expectation", "pay expectation"],
        "example": "120000",
    },
    {
        "key": "relocation",
        "label": "Relocation",
        "patterns": ["relocation", "relocate", "willing to relocate", "open to relocate"],
        "example": "Yes",
    },
    {
        "key": "start_date",
        "label": "Start Date",
        "patterns": ["start date", "earliest start", "available to start", "start date"],
        "example": "2026-09-01",
    },
    {
        "key": "eeo_gender",
        "label": "EEO: Gender",
        "patterns": ["gender", "sex"],
        "example": "Prefer not to say",
    },
    {
        "key": "eeo_race",
        "label": "EEO: Race/Ethnicity",
        "patterns": ["race", "ethnicity"],
        "example": "Prefer not to say",
    },
    {
        "key": "disability",
        "label": "Disability Status",
        "patterns": ["disability", "disabled"],
        "example": "Prefer not to say",
    },
    {
        "key": "veteran",
        "label": "Veteran Status",
        "patterns": ["veteran", "protected veteran"],
        "example": "I am not a veteran",
    },
    {
        "key": "legal_attestation",
        "label": "Legal Attestation",
        "patterns": ["legal attestation", "i attest", "i certify", "background check", "i authorize"],
        "example": "Yes",
    },
]


def normalize(text: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in (text or ""))
        .lower()
        .replace("\n", " ")
        .strip()
    ).replace("  ", " ")


def render_sensitive_kb_template() -> dict[str, dict[str, Any]]:
    """Return a fill-in KB template. The user sets ``answer`` and flips
    ``approved`` to true for each entry they want auto-filled."""
    return {
        item["key"]: {
            "label": item["label"],
            "patterns": list(item["patterns"]),
            "answer": "",
            "approved": False,
        }
        for item in SENSITIVE_FIELD_DEFS
    }


def load_sensitive_kb(path: str | Path | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def match_sensitive_answer(label: str, kb: dict[str, Any]) -> str | None:
    """Return the approved answer for a sensitive field label, or None.

    Also accepts the legacy flat profile fields (``sponsorship`` /
    ``work_authorization`` / ``salary``) as a fallback so existing profiles
    keep working.
    """
    if not kb or not label:
        return None
    n = normalize(label)
    for entry in kb.values():
        if not isinstance(entry, dict):
            continue
        if not entry.get("approved") or not entry.get("answer"):
            continue
        patterns = [normalize(p) for p in entry.get("patterns", []) if p]
        if patterns and any(p and p in n for p in patterns):
            return str(entry["answer"])
    return None


def merge_legacy_sensitive(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a KB view from legacy flat profile fields for backward compat."""
    legacy = {
        "work_authorization": ["authorized to work", "work authorization", "legally authorized"],
        "sponsorship": ["sponsorship", "sponsor", "visa"],
        "salary": ["salary", "compensation"],
    }
    out: dict[str, dict[str, Any]] = {}
    for key, patterns in legacy.items():
        value = profile.get(key)
        if value and str(value).strip() and str(value).strip().lower() != "needs review":
            out[key] = {"patterns": patterns, "answer": str(value), "approved": True}
    return out


def resolve_sensitive_answer(label: str, profile: dict[str, Any]) -> str | None:
    """Resolve a sensitive field answer from the KB and legacy profile fields."""
    kb = profile.get("sensitive_answers") or {}
    ans = match_sensitive_answer(label, kb)
    if ans:
        return ans
    return match_sensitive_answer(label, merge_legacy_sensitive(profile))
