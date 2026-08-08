"""Shared semantic classification for job-application form fields.

ATS products frequently rename labels while retaining enough semantic hints in
IDs, autocomplete values, ARIA labels, and surrounding sections to identify a
field.  This module keeps that interpretation in one place so runtime changes
are based on the field's meaning instead of a growing list of site-specific
exceptions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


def normalize(value: Any) -> str:
    """Return a comparison-safe representation of arbitrary field metadata."""
    raw = str(value or "")
    # ATS controls often encode the only useful semantic hint in a camel-case
    # ID, for example ``startDate-dateSectionMonth-input``.  Split that form
    # before lower-casing so the same rules work for labels, ARIA, and IDs.
    raw = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)
    raw = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", raw)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", raw.lower())).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    """Match a normalized term as whole words, not as a substring."""
    normalized_phrase = normalize(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in f" {text} "


@dataclass(frozen=True)
class SemanticRule:
    key: str
    any_of: tuple[str, ...] = ()
    all_of: tuple[str, ...] = ()
    none_of: tuple[str, ...] = ()
    section: str | None = None
    confidence: float = 1.0
    max_tokens: int | None = None

    def matches(self, text: str, section: str) -> bool:
        if self.section and self.section != section:
            return False
        if self.max_tokens is not None and len(text.split()) > self.max_tokens:
            return False
        if self.all_of and not all(_contains_phrase(text, token) for token in self.all_of):
            return False
        if self.any_of and not any(_contains_phrase(text, token) for token in self.any_of):
            return False
        return not any(_contains_phrase(text, token) for token in self.none_of)


# Keep high-specificity rules before broad identity and location rules.  The
# rules deliberately describe applicant data only; screening questions remain
# governed by the answer bank and sensitive-answer policy in the runtimes.
SEMANTIC_RULES: tuple[SemanticRule, ...] = (
    SemanticRule("education.start.month", all_of=("start", "month"), section="education"),
    SemanticRule("education.start.day", all_of=("start", "day"), section="education"),
    SemanticRule("education.start.year", all_of=("start", "year"), section="education"),
    SemanticRule("education.end.month", all_of=("end", "month"), section="education"),
    SemanticRule("education.end.day", all_of=("end", "day"), section="education"),
    SemanticRule("education.end.year", all_of=("end", "year"), section="education"),
    SemanticRule("education.graduation.month", all_of=("graduation", "month"), section="education"),
    SemanticRule("education.graduation.day", all_of=("graduation", "day"), section="education"),
    SemanticRule("education.graduation.year", all_of=("graduation", "year"), section="education"),
    SemanticRule(
        "education.start.date",
        any_of=("start date", "date from", "from date"),
        none_of=("month", "year"),
        section="education",
    ),
    SemanticRule(
        "education.end.date",
        any_of=("end date", "date to", "to date"),
        none_of=("month", "year"),
        section="education",
    ),
    SemanticRule(
        "education.graduation.date",
        any_of=("graduation", "graduate", "anticipated graduation"),
        none_of=("month", "year"),
        section="education",
    ),
    SemanticRule("work.start.month", all_of=("start", "month"), section="work"),
    SemanticRule("work.start.day", all_of=("start", "day"), section="work"),
    SemanticRule("work.start.year", all_of=("start", "year"), section="work"),
    SemanticRule("work.end.month", all_of=("end", "month"), section="work"),
    SemanticRule("work.end.day", all_of=("end", "day"), section="work"),
    SemanticRule("work.end.year", all_of=("end", "year"), section="work"),
    SemanticRule(
        "work.start.date",
        any_of=("start date", "date from", "from date"),
        none_of=("month", "year"),
        section="work",
    ),
    SemanticRule(
        "work.end.date",
        any_of=("end date", "date to", "to date"),
        none_of=("month", "year"),
        section="work",
    ),
    SemanticRule("identity.pronunciation", any_of=("pronunciation", "pronounce")),
    SemanticRule("identity.preferred_name", any_of=("preferred name", "chosen name", "display name")),
    SemanticRule("identity.full_name", all_of=("first", "last", "name")),
    SemanticRule("identity.first_name", any_of=("first name", "given name", "forename")),
    SemanticRule("identity.last_name", any_of=("last name", "family name", "surname")),
    SemanticRule("contact.email", any_of=("email", "e mail", "emailaddress")),
    SemanticRule("contact.phone.country_code", any_of=("country phone code", "phone country code", "phonecountrycode")),
    SemanticRule("contact.phone.extension", any_of=("phone extension", "extension")),
    SemanticRule("contact.phone.type", any_of=("phone device type", "phonetype")),
    SemanticRule("contact.phone", any_of=("phone", "mobile", "telephone", "contact number", "phonenumber")),
    SemanticRule("link.linkedin", any_of=("linkedin",)),
    SemanticRule("link.github", any_of=("github",)),
    SemanticRule("link.portfolio", any_of=("portfolio",)),
    SemanticRule("link.website", any_of=("website", "personal site", "homepage")),
    SemanticRule("address.line2", any_of=("address line 2", "addressline2"), max_tokens=8),
    SemanticRule("address.postal_code", any_of=("postal code", "zip code", "postcode", "zipcode"), max_tokens=8),
    SemanticRule("address.city", any_of=("city",), max_tokens=8),
    SemanticRule("address.region", any_of=("state province", "province", "countryregion", "state region", "state of residence", "residence state", "state you reside", "state you live", "your state"), max_tokens=8),
    SemanticRule(
        "employment.eligible_country",
        any_of=("employment eligible countries", "employment eligible country", "countries are you seeking to work", "country are you seeking to work"),
        max_tokens=24,
    ),
    SemanticRule("address.country", any_of=("country", "nation"), none_of=("country phone",), max_tokens=8),
    SemanticRule(
        "address.line1",
        any_of=("address line 1", "addressline1", "street address", "mailing address", "resumatoraddressvalue"),
        max_tokens=8,
    ),
    SemanticRule("address.line1", all_of=("address",), none_of=("line 2", "addressing", "email address"), max_tokens=5),
    SemanticRule("location.current", any_of=("currently located", "current location", "currently based", "where are you based")),
    SemanticRule("location.current", any_of=("location",), none_of=("relocation", "location preference", "job location")),
    SemanticRule(
        "work.current.company",
        any_of=("current company", "current employer", "current or most recent employer", "most recent employer", "companyname"),
    ),
    SemanticRule("work.current.company", any_of=("company", "employer", "organization"), section="work"),
    SemanticRule("work.current.title", any_of=("current title", "current role", "current position", "job title", "jobtitle")),
    SemanticRule("work.current.title", any_of=("title", "position", "role"), section="work"),
    SemanticRule("work.current.description", any_of=("role description", "roledescription", "responsibilities"), section="work"),
    SemanticRule("work.current.description", any_of=("role description", "roledescription")),
    SemanticRule("career.years_experience", any_of=("year", "years"), all_of=("experience",)),
    SemanticRule("education.school", any_of=("university", "school", "college", "institution"), max_tokens=14),
    SemanticRule("education.degree", any_of=("degree",), none_of=("degree field",), max_tokens=14),
    SemanticRule("education.field", any_of=("field of study", "major", "discipline", "academic field"), max_tokens=14),
    SemanticRule("education.gpa", any_of=("gpa", "gradeaverage", "grade average"), max_tokens=14),
    SemanticRule("education.start.year", any_of=("firstyearattended", "first year attended")),
    SemanticRule("education.end.year", any_of=("lastyearattended", "last year attended")),
    SemanticRule("education.graduation.date", any_of=("graduation date", "anticipated graduation", "when will you graduate")),
    SemanticRule(
        "identity.full_name",
        any_of=("legal name", "full name", "your name", "candidate name", "applicant name", "cname"),
        none_of=("pronunciation", "pronounce"),
    ),
    SemanticRule(
        "identity.full_name",
        all_of=("name",),
        none_of=("company", "employer", "organization", "school", "university", "reference", "manager", "emergency", "pronunciation", "pronounce"),
        confidence=0.75,
    ),
)


# WHATWG autocomplete tokens are provider-independent and survive localized or
# visually hidden labels.  Keep this separate from the text rules because
# tokens such as ``tel`` are too short to safely match arbitrary prose.
AUTOCOMPLETE_SEMANTICS = {
    "name": "identity.full_name",
    "given name": "identity.first_name",
    "family name": "identity.last_name",
    "email": "contact.email",
    "tel": "contact.phone",
    "tel national": "contact.phone",
    "tel local": "contact.phone",
    "tel country code": "contact.phone.country_code",
    "url": "link.website",
    "street address": "address.line1",
    "address line1": "address.line1",
    "address line2": "address.line2",
    "address level2": "address.city",
    "address level1": "address.region",
    "country name": "address.country",
    "postal code": "address.postal_code",
}


@dataclass(frozen=True)
class FieldSemantic:
    key: str
    confidence: float
    text: str
    section: str


def _field_parts(field_or_label: Mapping[str, Any] | str | Any) -> tuple[list[str], str]:
    if not isinstance(field_or_label, Mapping):
        return [str(field_or_label or "")], ""
    parts = [
        field_or_label.get("label"),
        field_or_label.get("id"),
        field_or_label.get("name"),
        field_or_label.get("section"),
        field_or_label.get("ariaLabel"),
        field_or_label.get("aria_label"),
        field_or_label.get("ariaDescription"),
        field_or_label.get("aria_description"),
        field_or_label.get("placeholder"),
        field_or_label.get("autocomplete"),
    ]
    section = normalize(field_or_label.get("section"))
    if not section:
        # A generic scraper cannot always identify the surrounding repeatable
        # section. Stable control IDs still commonly encode it.
        structural = normalize(
            " ".join(str(field_or_label.get(key) or "") for key in ("id", "name", "automationId"))
        )
        if "education" in structural or "academic" in structural:
            section = "education"
        elif any(token in structural for token in ("employment", "work history", "work experience")):
            section = "work"
    return [str(part) for part in parts if part], section


def _field_evidence(field_or_label: Mapping[str, Any] | str | Any) -> tuple[list[tuple[str, float]], str, str]:
    """Return independent metadata sources, their reliability, and a context string.

    Combining every field attribute before matching lets a long help text cancel
    a concise label or makes an unrelated ID win.  Treat each source as
    evidence instead, while retaining the full context for negative guards.
    """
    if not isinstance(field_or_label, Mapping):
        text = normalize(field_or_label)
        return ([(text, 1.0)] if text else []), "", text

    source_values = (
        (field_or_label.get("label"), 1.0),
        (field_or_label.get("ariaLabel") or field_or_label.get("aria_label"), 0.95),
        (" ".join(str(field_or_label.get(key) or "") for key in ("id", "name")), 0.82),
        (field_or_label.get("placeholder"), 0.72),
        (field_or_label.get("ariaDescription") or field_or_label.get("aria_description"), 0.45),
    )
    evidence = [(normalize(value), weight) for value, weight in source_values if normalize(value)]
    parts, section = _field_parts(field_or_label)
    return evidence, section, normalize(" ".join(parts))


def _rule_score(rule: SemanticRule, source_weight: float) -> float:
    """Prefer specific all-of rules over broad one-word fallback rules."""
    all_specificity = sum(len(normalize(token).split()) for token in rule.all_of)
    any_specificity = max((len(normalize(token).split()) for token in rule.any_of), default=0)
    return rule.confidence * source_weight + all_specificity * 0.09 + any_specificity * 0.04


def classify_field(field_or_label: Mapping[str, Any] | str | Any) -> FieldSemantic | None:
    """Classify a form field using visible and accessibility metadata."""
    if isinstance(field_or_label, Mapping):
        autocomplete = normalize(field_or_label.get("autocomplete"))
        key = AUTOCOMPLETE_SEMANTICS.get(autocomplete)
        if key:
            _, section, context = _field_evidence(field_or_label)
            return FieldSemantic(key, 1.0, context, section)
        for token in autocomplete.split():
            key = AUTOCOMPLETE_SEMANTICS.get(token)
            if key:
                _, section, context = _field_evidence(field_or_label)
                return FieldSemantic(key, 1.0, context, section)

    evidence, section, context = _field_evidence(field_or_label)
    if not context:
        return None
    primary_label = normalize(field_or_label.get("label")) if isinstance(field_or_label, Mapping) else context
    candidates: list[tuple[float, int, SemanticRule, str]] = []
    for index, rule in enumerate(SEMANTIC_RULES):
        if rule.section and rule.section != section:
            continue
        # A negative term anywhere in the field's semantic context is more
        # reliable than a positive term in a generated ID.
        if any(_contains_phrase(context, token) for token in rule.none_of):
            continue
        for source, source_weight in evidence:
            if rule.max_tokens is not None and len(source.split()) > rule.max_tokens:
                continue
            if rule.max_tokens is not None and primary_label and len(primary_label.split()) > rule.max_tokens:
                # Do not infer a profile field from a short generated ID when
                # the user-facing label is a long screening question.
                continue
            if not rule.matches(source, section):
                continue
            candidates.append((_rule_score(rule, source_weight), -index, rule, source))
            break
    if not candidates:
        return None
    _, _, rule, source = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
    return FieldSemantic(rule.key, rule.confidence, source, section)


def runtime_semantic_rules() -> list[dict[str, Any]]:
    """Return the declarative rules embedded into the generated Node runtime."""
    return [
        {
            "key": rule.key,
            "any": list(rule.any_of),
            "all": list(rule.all_of),
            "none": list(rule.none_of),
            "section": rule.section,
            "confidence": rule.confidence,
            "maxTokens": rule.max_tokens,
        }
        for rule in SEMANTIC_RULES
    ]


def runtime_autocomplete_semantics() -> dict[str, str]:
    """Return normalized autocomplete-token mappings for the Node runtime."""
    return dict(AUTOCOMPLETE_SEMANTICS)


_MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}


def _first_entry(entries: Any) -> dict[str, Any] | None:
    if not isinstance(entries, list):
        return None
    return next((entry for entry in entries if isinstance(entry, dict)), None)


def _current_work_value(profile: Mapping[str, Any], key: str) -> Any | None:
    entries = profile.get("work_history")
    if not isinstance(entries, list):
        return None
    current = next(
        (entry for entry in entries if isinstance(entry, dict) and entry.get("current")),
        None,
    )
    entry = current or _first_entry(entries)
    return entry.get(key) if entry else None


def _education_value(profile: Mapping[str, Any], key: str) -> Any | None:
    entry = _first_entry(profile.get("education"))
    return entry.get(key) if entry else None


def _date_part(entry: Mapping[str, Any] | None, boundary: str, part: str) -> str | None:
    if not entry:
        return None
    explicit = entry.get(f"{boundary}_{part}")
    if explicit not in {None, ""}:
        raw = str(explicit).strip()
    else:
        source = str(entry.get(f"{boundary}_date") or "").strip()
        match = re.search(r"(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?", source)
        if not match:
            return None
        if part == "month":
            raw = match.group(2)
        elif part == "day":
            raw = match.group(3) or ""
        else:
            raw = match.group(1)
    if not raw:
        return None
    if part == "year":
        return raw
    if part == "day":
        try:
            return f"{int(raw):02d}"
        except ValueError:
            return raw
    try:
        return _MONTH_NAMES.get(int(raw), raw)
    except ValueError:
        return raw


def _country(profile: Mapping[str, Any]) -> str | None:
    if profile.get("country"):
        return str(profile["country"])
    location = normalize(profile.get("location"))
    tokens = set(location.split())
    if tokens & _US_STATE_CODES or "united states" in location or "usa" in tokens or "us" in tokens:
        return "United States"
    return None


def _city(location: Any) -> str | None:
    raw = str(location or "").strip()
    if "," not in raw:
        return None
    return raw.split(",", 1)[0].strip() or None


def _phone_number(profile: Mapping[str, Any], text: str) -> str | None:
    phone = profile.get("phone")
    if not phone:
        return None
    if "phone number" not in text and "phonenumber" not in text:
        return str(phone)
    digits = re.sub(r"\D+", "", str(phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or str(phone)


def value_for_semantic(
    semantic: FieldSemantic | str | None,
    profile: Mapping[str, Any],
    *,
    field_text: str = "",
    today: date | None = None,
) -> Any | None:
    """Resolve a classified field against a structured applicant profile."""
    key = semantic.key if isinstance(semantic, FieldSemantic) else semantic
    if not key:
        return None
    normalized_text = normalize(field_text or (semantic.text if isinstance(semantic, FieldSemantic) else ""))
    if key == "identity.full_name":
        return profile.get("name")
    if key == "identity.first_name":
        return profile.get("first_name") or str(profile.get("name") or "").split(" ")[0] or None
    if key == "identity.last_name":
        return profile.get("last_name") or " ".join(str(profile.get("name") or "").split(" ")[1:]) or None
    if key == "identity.preferred_name":
        return profile.get("preferred_name") or profile.get("first_name") or str(profile.get("name") or "").split(" ")[0] or None
    if key == "identity.pronunciation":
        return profile.get("name_pronunciation") or profile.get("pronunciation")
    if key == "contact.email":
        return profile.get("email")
    if key == "contact.phone":
        return _phone_number(profile, normalized_text)
    if key == "contact.phone.country_code":
        return profile.get("phone_country_code")
    if key == "contact.phone.extension":
        return profile.get("phone_extension")
    if key == "contact.phone.type":
        return profile.get("phone_type") or "Mobile"
    if key == "link.linkedin":
        return profile.get("linkedin")
    if key == "link.github":
        return profile.get("github")
    if key == "link.portfolio":
        return profile.get("portfolio") or profile.get("website")
    if key == "link.website":
        return profile.get("website") or profile.get("portfolio")
    if key == "address.line1":
        return profile.get("address_line1") or profile.get("street_address")
    if key == "address.line2":
        return profile.get("address_line2")
    if key == "address.city":
        return profile.get("city") or _city(profile.get("location"))
    if key == "address.region":
        return profile.get("region") or profile.get("state")
    if key == "address.country":
        return _country(profile)
    if key == "employment.eligible_country":
        return _country(profile)
    if key == "address.postal_code":
        return profile.get("postal_code") or profile.get("zip")
    if key == "location.current":
        return profile.get("location") or profile.get("city")
    if key == "work.current.company":
        return _current_work_value(profile, "company")
    if key == "work.current.title":
        return _current_work_value(profile, "title")
    if key == "work.current.description":
        return _current_work_value(profile, "description")
    if key.startswith("work.") and key.endswith((".month", ".day", ".year")):
        _, boundary, part = key.split(".")
        return _date_part(_first_entry(profile.get("work_history")), boundary, part)
    if key.startswith("work.") and key.endswith(".date"):
        _, boundary, _ = key.split(".")
        return _current_work_value(profile, f"{boundary}_date")
    if key == "career.years_experience":
        return (
            profile.get("years_experience")
            or profile.get("relevant_years_experience")
            or profile.get("post_college_years_experience")
        )
    if key == "education.school":
        return _education_value(profile, "school")
    if key == "education.degree":
        return _education_value(profile, "degree")
    if key == "education.field":
        return _education_value(profile, "field")
    if key == "education.gpa":
        return _education_value(profile, "gpa")
    if key.startswith("education.") and key.endswith((".month", ".day", ".year")):
        _, boundary, part = key.split(".")
        if boundary == "graduation":
            boundary = "end"
        return _date_part(_first_entry(profile.get("education")), boundary, part)
    if key.startswith("education.") and key.endswith(".date"):
        _, boundary, _ = key.split(".")
        if boundary == "graduation":
            raw = str(profile.get("graduation_date") or _education_value(profile, "end_date") or "")
            match = re.fullmatch(r"(\d{4})-(\d{1,2})", raw)
            if match:
                return f"{_MONTH_NAMES.get(int(match.group(2)), match.group(2))} {match.group(1)}"
            return raw or None
        return _education_value(profile, f"{boundary}_date")
    if key == "signature.date":
        return (today or date.today()).isoformat()
    return None
