"""Sensitive-answer knowledge base.

A pre-filled, user-approved bank for sensitive application fields (work
authorization, sponsorship, salary, relocation, start date, EEO/demographic,
disability, veteran, citizenship, security clearance, legal attestation). The
user fills it once and marks each entry ``approved: true``; the form fillers
then auto-fill those fields instead of leaving them for manual review.

Safety contract:
- A sensitive field is auto-filled ONLY when the KB has an ``approved`` answer
  whose label patterns match the field. That approval IS the user's explicit
  confirmation required by the sensitive-field policy gate.
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
        "patterns": ["race", "ethnicity", "hispanic", "latino", "hispanic/latino"],
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
        "key": "citizenship",
        "label": "Citizenship",
        "patterns": ["citizen", "citizenship", "us citizen", "u s citizen"],
        "example": "Needs review",
    },
    {
        "key": "security_clearance",
        "label": "Security Clearance",
        "patterns": ["security clearance", "clearance", "active clearance"],
        "example": "Needs review",
    },
    {
        "key": "legal_attestation",
        "label": "Legal Attestation",
        "patterns": [
            "legal attestation",
            "i attest",
            "i certify",
            "i hereby certify",
            "true and correct",
            "background check",
            "i authorize",
            "arbitration agreement",
            "agreement acknowledgement",
            "agreement acknowledgment",
        ],
        "example": "Yes",
    },
    {
        "key": "privacy_consent",
        "label": "Privacy / AI Notetaker Consent",
        "patterns": [
            "privacy policy",
            "personal data",
            "process your personal data",
            "ai notetaker",
            "ai notetakers",
            "transcribe conversations",
        ],
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


_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "be",
    "do",
    "for",
    "i",
    "in",
    "is",
    "of",
    "or",
    "the",
    "to",
    "you",
    "your",
}

_SENSITIVE_STEMS = {
    "attest",
    "authoriz",
    "background",
    "certif",
    "citizen",
    "compensat",
    "acknowledg",
    "arbitrat",
    "consent",
    "disab",
    "eligib",
    "ethnic",
    "gender",
    "hispanic",
    "legal",
    "latino",
    "pay",
    "privacy",
    "race",
    "relocat",
    "salary",
    "sex",
    "sponsor",
    "transcrib",
    "notetak",
    "veteran",
    "visa",
    "work",
}

_SINGLE_TOKEN_MATCH_STEMS = _SENSITIVE_STEMS - {"eligib", "legal", "work"}


def _stem_token(token: str) -> str:
    token = token.lower()
    for prefix, stem in [
        ("authoriz", "authoriz"),
        ("sponsor", "sponsor"),
        ("relocat", "relocat"),
        ("compensat", "compensat"),
        ("eligib", "eligib"),
        ("certif", "certif"),
        ("attest", "attest"),
        ("disab", "disab"),
        ("ethnic", "ethnic"),
    ]:
        if token.startswith(prefix):
            return stem
    for suffix in ["ation", "ions", "ing", "ed", "es", "s"]:
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _meaningful_tokens(text: str) -> set[str]:
    country_tokens = {"u", "s", "us", "usa", "united", "states"}
    return {
        _stem_token(token)
        for token in normalize(text).split()
        if token and token not in _STOPWORDS and token not in country_tokens
    }


def _country_markers(text: str) -> set[str]:
    normalized = f" {normalize(text)} "
    markers: set[str] = set()
    if any(token in normalized for token in [" united states ", " usa ", " u s a ", " u s ", " us "]):
        markers.add("us")
    if " canada " in normalized or " canadian " in normalized:
        markers.add("canada")
    if any(token in normalized for token in [" united kingdom ", " uk ", " u k ", " british ", " britain "]):
        markers.add("uk")
    return markers


def _sensitive_pattern_matches(label: str, pattern: str) -> bool:
    label_norm = normalize(label)
    pattern_norm = normalize(pattern)
    if not label_norm or not pattern_norm:
        return False
    label_countries = _country_markers(label_norm)
    pattern_countries = _country_markers(pattern_norm)
    if pattern_countries:
        if not label_countries:
            return False
        if label_countries.isdisjoint(pattern_countries):
            return False
    if pattern_norm in label_norm or label_norm in pattern_norm:
        return True

    label_tokens = _meaningful_tokens(label_norm)
    pattern_tokens = _meaningful_tokens(pattern_norm)
    if not label_tokens or not pattern_tokens:
        return False
    for exclusive_stem in {"citizen", "sponsor"}:
        if exclusive_stem in pattern_tokens and exclusive_stem not in label_tokens:
            return False
    clearance_tokens = {"security", "clearance", "ts", "sci"}
    if pattern_tokens & clearance_tokens and not label_tokens & clearance_tokens:
        return False
    if {"apply", "maintain"} <= pattern_tokens and not label_tokens & clearance_tokens:
        return False
    authorization_tokens = {"authoriz", "eligible", "legal", "right"}
    if pattern_tokens & authorization_tokens and not label_tokens & authorization_tokens:
        return False
    if "background" in pattern_tokens and "check" in pattern_tokens and not (
        "check" in label_tokens or "screen" in label_tokens or "screening" in label_tokens or "verif" in label_tokens
    ):
        return False
    visa_type_tokens = {"opt", "h1b", "tn"}
    if pattern_tokens & visa_type_tokens and not label_tokens & visa_type_tokens:
        return False
    # A broad future-sponsorship question can mention a visa without asking
    # for the candidate's visa *type*. Do not let a generic "visa type"
    # pattern override the approved Yes/No sponsorship answer in that case.
    visa_type_context_tokens = {"type", "status", "which", "kind"}
    if pattern_tokens & visa_type_context_tokens and not label_tokens & visa_type_context_tokens:
        return False
    # A pattern that is only about a visa type should not match a generic
    # sponsorship Yes/No question.
    if pattern_tokens & visa_type_tokens and not pattern_tokens & visa_type_context_tokens:
        if ("sponsor" in label_tokens or "require" in label_tokens) and "type" not in label_tokens:
            return False
    if pattern_tokens <= label_tokens or label_tokens <= pattern_tokens:
        return True

    common = label_tokens & pattern_tokens
    if len(common) >= 2:
        if "top" in pattern_tokens and "top" not in label_tokens:
            return False
        return bool(common & _SENSITIVE_STEMS)
    return bool(common & _SINGLE_TOKEN_MATCH_STEMS)


def _entry_priority(key: str, entry: dict[str, Any]) -> tuple[int, int]:
    """Prefer specific eligibility/identity answers over broad preferences."""
    highest_priority_keys = {
        "us_export_control_status",
        "security_clearance_level_never_held",
    }
    specific_keys = {
        "citizenship",
        "active_security_clearance",
        "security_clearance_eligibility",
        "security_clearance",
        "security_clearance_level_never_held",
        "sponsorship_type",
        "hispanic_or_latino",
        "terms_consent",
        "ai_notetaker_consent",
    }
    broad_preference_keys = {"security_clearance_interest"}
    if key in highest_priority_keys:
        group = -1
    elif key in specific_keys:
        group = 0
    elif key in broad_preference_keys:
        group = 2
    else:
        group = 1
    longest_pattern = max((len(str(pattern)) for pattern in entry.get("patterns", []) if pattern), default=0)
    return (group, -longest_pattern)


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
    entries = sorted(
        ((str(key), entry) for key, entry in kb.items() if isinstance(entry, dict)),
        key=lambda item: _entry_priority(item[0], item[1]),
    )
    for _key, entry in entries:
        if not entry.get("approved") or not entry.get("answer"):
            continue
        patterns = [str(p) for p in entry.get("patterns", []) if p]
        if patterns and any(_sensitive_pattern_matches(n, p) for p in patterns):
            return str(entry["answer"])
    return None


def merge_legacy_sensitive(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build a KB view from legacy flat profile fields for backward compat."""
    legacy = {
        item["key"]: list(item["patterns"])
        for item in SENSITIVE_FIELD_DEFS
    }
    out: dict[str, dict[str, Any]] = {}
    for key, patterns in legacy.items():
        value = profile.get(key)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        if value and str(value).strip().lower() not in {"needs review", "n/a", "na", "tbd"}:
            out[key] = {"patterns": patterns, "answer": str(value), "approved": True}
    return out


def resolve_sensitive_answer(label: str, profile: dict[str, Any]) -> str | None:
    """Resolve a sensitive field answer from the KB and legacy profile fields."""
    kb = profile.get("sensitive_answers") or {}
    ans = match_sensitive_answer(label, kb)
    if ans:
        return ans
    return match_sensitive_answer(label, merge_legacy_sensitive(profile))
