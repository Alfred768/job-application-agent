"""Candidate-aware pre-screening before resume preparation and submission."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from job_agent.models import Job


_SENIOR_TITLE_PATTERN = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|director|manager|architect|lead|head of|vp|vice president)\b",
    flags=re.IGNORECASE,
)
_EARLY_CAREER_PATTERN = re.compile(r"\b(?:intern|student|new grad|graduate assistant)\b", flags=re.IGNORECASE)
_NON_US_LOCATION_PATTERN = re.compile(
    r"\b(?:europe|emea|mexico|uruguay|brazil|canada|india|china|argentina|colombia|"
    r"germany|france|spain|poland|netherlands|united kingdom|uk|australia|japan|singapore|"
    r"romania|ireland|new zealand|hungary|serbia|denmark|sweden|finland|israel|italy|"
    r"warsaw|wroclaw|bucharest|bangalore|galway|auckland|budapest|belgrade|aarhus|"
    r"stockholm|helsinki|herzliya|milan|osborne park|kingsgrove|"
    r"london|munich|berlin|dublin|paris|prague|amsterdam|lisbon|madrid|barcelona|"
    r"zurich|geneva|brussels|vienna|warsaw|oslo|copenhagen|toronto|vancouver|montreal|"
    r"ottawa|calgary|ontario|dubai|abu dhabi|doha|qatar|bahrain|hong kong|kuala lumpur|manila|jakarta|"
    r"bangkok|ho chi minh|hanoi|seoul|tokyo|osaka|taipei|mumbai|hyderabad|pune|delhi|"
    r"chennai|bengaluru|bangalore|sydney|melbourne|brisbane|perth|adelaide|"
    r"auckland|wellington|manchester|birmingham|edinburgh|glasgow|leeds|bristol|"
    r"athina|athens|antalya|istanbul|cairo|lagos|nairobi|johannesburg|cape town|"
    r"mexico city|sao paulo|buenos aires|santiago|lima|bogota|"
    r"tel aviv|lahore|greece|belgium|portugal|czech republic|czechia|"
    r"philippines|south korea|taiwan|vietnam|thailand|indonesia|pakistan|"
    r"norway|iceland|kuwait|gurugram|shanghai|düsseldorf|"
    r"sao jose dos campos|reykjavík|berlin|vienna|warsaw|"
    r"zurich|geneva|amsterdam|brussels|hong kong)\b",
    flags=re.IGNORECASE,
)
_US_LOCATION_PATTERN = re.compile(r"\b(?:united states|u\.?s\.?a?|usa|america)\b", flags=re.IGNORECASE)

_UNUSABLE_APPLICATION_URL_PATTERNS = (
    "ycombinator.com/companies",
    "workatastartup.com",
    "notion.so",
    "angel.co",
    "news.ycombinator.com",
    "greenhouse.io/coinbase",
    "greenhouse.io/epicgames",
    "greenhouse.io/wayve",
)


def application_url_unusable(url: str | None) -> bool:
    """Return True when a public listing points to a non-direct-application page."""
    raw = str(url or "").lower()
    return bool(raw) and any(pattern in raw for pattern in _UNUSABLE_APPLICATION_URL_PATTERNS)


_NO_SPONSORSHIP_PATTERN = re.compile(
    r"(?:unable|not\s+(?:currently\s+)?able|cannot|can't|can\s+not)[^.]{0,80}"
    r"(?:sponsor|visa sponsorship|sponsorship)|"
    r"(?:do|does|will)\s+not[^.]{0,80}(?:sponsor|visa sponsorship|sponsorship)|"
    r"(?:visa\s+)?sponsorship\s+(?:is|are)\s+not\s+"
    r"(?:available|provided|offered)|"
    r"(?:without|no)\s+(?:current\s+or\s+future\s+)?(?:employer[-\s])?"
    r"(?:visa\s+)?sponsorship|"
    r"(?:must|need to)[^.]{0,80}(?:independent|existing|unrestricted)[^.]{0,80}work authorization",
    flags=re.IGNORECASE,
)
_SPONSORSHIP_SUPPORTED_PATTERN = re.compile(
    r"(?:happy|willing|open)\s+to\s+sponsor|"
    r"(?:provide|provides|providing)\s+(?:visa\s+)?sponsorship(?:\s+support)?|"
    r"(?:visa|visas)[^.]{0,120}(?:happy|willing|open)\s+to\s+sponsor|"
    r"sponsor\s+international\s+candidates|"
    r"successful\s+in\s+sponsoring",
    flags=re.IGNORECASE,
)
_US_CITIZENSHIP_REQUIRED_PATTERN = re.compile(
    r"(?:requires?|must(?:\s+be)?|need(?:s)?(?:\s+to\s+be)?)[^.]{0,80}"
    r"(?:u\.?\s*s\.?\s*citizenship|u\.?\s*s\.?\s*citizen|united states citizen)|"
    r"(?:must(?:\s+be)?|requires?)[^.]{0,80}u\.?\s*s\.?\s*person|"
    r"(?:clearance)[^.]{0,80}(?:requires?|requiring)[^.]{0,80}"
    r"(?:u\.?\s*s\.?\s*citizenship|u\.?\s*s\.?\s*citizen)",
    flags=re.IGNORECASE,
)
_CLEARANCE_REQUIRED_PATTERN = re.compile(
    r"(?:requires?|must(?:\s+be)?|need(?:s)?(?:\s+to\s+be)?|eligible\s+for)"
    r"[^.]{0,80}(?:security\s+clearance|ts/sci|ts\s*/\s*sci|top\s+secret|secret\s+clearance)|"
    r"(?:security\s+clearance|ts/sci|ts\s*/\s*sci|top\s+secret|secret\s+clearance)"
    r"[^.]{0,80}(?:required|mandatory|must\s+hold|must\s+possess|must\s+have)|"
    r"ts\s*/\s*sci(?:\s+with\s+[a-z\s-]+)?\s+clearance[^.]{0,40}required",
    flags=re.IGNORECASE,
)
_MINIMUM_EXPERIENCE_PATTERN = re.compile(
    r"\b(?:at least\s+)?(\d+)(?:\s*-\s*(\d+))?\s*(?:\+|or more)?\s+years?"
    r"(?:\s+or\s+equivalent\b|"
    r"(?:(?:\s+of)?[^.]{0,80}?\bexperience\b)|"
    r"\s+(?:of\s+)?(?:building|developing|designing|working|operating|shipping|in|with)\b)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class CandidateScreeningResult:
    eligible: bool
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def screen_job_for_candidate(job: Job, profile: dict[str, Any] | None) -> CandidateScreeningResult:
    """Reject clear location and seniority mismatches using explicit profile facts.

    This is intentionally conservative: it only excludes jobs where the public
    listing explicitly narrows location outside the candidate's country, or
    where a clearly early-career profile is paired with an explicitly senior
    title. Everything else proceeds to the resume- and form-level gates.
    """
    if not profile:
        return CandidateScreeningResult(eligible=True, reasons=[])

    company = str(job.company or "").strip()
    title = str(job.title or "").strip()
    if company.lower() in {"", "unknown company"}:
        return CandidateScreeningResult(
            eligible=False,
            reasons=["listing does not identify an employer"],
        )
    if title.lower() in {"", "unknown role"}:
        return CandidateScreeningResult(
            eligible=False,
            reasons=["listing does not identify a role"],
        )
    if application_url_unusable(job.apply_url or job.source_url):
        return CandidateScreeningResult(
            eligible=False,
            reasons=["listing does not expose a direct application form"],
        )

    overrides = (profile or {}).get("screening_overrides") or {}

    reasons: list[str] = []
    country = str(profile.get("country") or "").strip().lower()
    location = " ".join(
        part for part in [str(job.location or ""), str(job.remote_policy or "")] if part
    )
    if (
        not overrides.get("ignore_location_filter", False)
        and country in {"united states", "us", "u.s.", "usa"}
        and location_is_outside_us(location)
    ):
        reasons.append(
            "listing location is outside the candidate's U.S. work "
            f"authorization: {location}"
        )

    if (
        not overrides.get("ignore_seniority_title_filter", False)
        and not profile.get("phd_equivalent")
        and _is_early_career_profile(profile)
        and _SENIOR_TITLE_PATTERN.search(job.title or "")
    ):
        reasons.append("listing title requires a seniority level not supported by the candidate profile")

    raw_jd = job.raw_jd or ""
    if (
        not overrides.get("ignore_sponsorship_filter", False)
        and _requires_sponsorship(profile)
        and _NO_SPONSORSHIP_PATTERN.search(raw_jd)
        and not _SPONSORSHIP_SUPPORTED_PATTERN.search(raw_jd)
    ):
        reasons.append("listing does not provide visa sponsorship required by the candidate profile")

    if (
        not overrides.get("ignore_citizenship_requirements", False)
        and not _is_us_citizen(profile)
        and _US_CITIZENSHIP_REQUIRED_PATTERN.search(job.raw_jd or "")
    ):
        reasons.append("listing requires U.S. citizenship not supported by the candidate profile")

    if (
        not overrides.get("ignore_clearance_requirements", False)
        and not _clearance_eligible(profile)
        and _CLEARANCE_REQUIRED_PATTERN.search(job.raw_jd or "")
    ):
        reasons.append("listing requires a security clearance not supported by the candidate profile")

    required_years = _minimum_required_years(raw_jd)
    candidate_years = _candidate_max_years(profile)
    if (
        not overrides.get("ignore_experience_requirements", False)
        and not profile.get("phd_equivalent")
        and required_years
        and candidate_years is not None
        and candidate_years < required_years
    ):
        reasons.append(
            f"listing requires at least {required_years} years of experience; "
            f"candidate profile states {candidate_years}"
        )

    return CandidateScreeningResult(eligible=not reasons, reasons=reasons)


def location_is_outside_us(location: str | None) -> bool:
    """Return true only for an explicitly non-U.S. location."""
    value = str(location or "")
    normalized = value.lower()
    return bool(
        _NON_US_LOCATION_PATTERN.search(value)
        and not _US_LOCATION_PATTERN.search(value)
        and "worldwide" not in normalized
    )


def _is_early_career_profile(profile: dict[str, Any]) -> bool:
    overrides = (profile or {}).get("screening_overrides") or {}
    if overrides.get("ignore_seniority_title_filter") or profile.get("phd_equivalent"):
        return False
    maximum = _candidate_max_years(profile)
    if maximum is not None and maximum >= 4:
        return False
    history = profile.get("work_history") or []
    return any(
        _EARLY_CAREER_PATTERN.search(
            " ".join(str(item.get(key) or "") for key in ("title", "employment_type"))
        )
        for item in history
        if isinstance(item, dict)
    )


def _candidate_max_years(profile: dict[str, Any]) -> int | None:
    raw_years = str(profile.get("years_experience") or profile.get("relevant_years_experience") or "")
    values = [int(value) for value in re.findall(r"\d+", raw_years)]
    return max(values) if values else None


def _minimum_required_years(raw_jd: str | None) -> int | None:
    requirements = [
        max(int(value) for value in match.groups() if value)
        for match in _MINIMUM_EXPERIENCE_PATTERN.finditer(raw_jd or "")
        if _experience_match_is_requirement(raw_jd or "", match)
    ]
    return max(requirements) if requirements else None


def _experience_match_is_requirement(raw_jd: str, match: re.Match[str]) -> bool:
    """Keep explicit requirements, but ignore growth-path outcome copy."""
    before = raw_jd[max(0, match.start() - 160):match.start()].lower()
    after = raw_jd[match.end():match.end() + 80].lower()
    if re.search(r"\b(?:path|grow|grows|growth)\b[^.]{0,80}\b(?:to|from)\b", before):
        return False
    if re.search(r"\bin\s+\d+\s+months?\b", after):
        return False
    return True


def _requires_sponsorship(profile: dict[str, Any]) -> bool:
    direct = str(profile.get("requires_sponsorship") or profile.get("sponsorship") or "").strip().lower()
    if direct in {"yes", "true", "1"}:
        return True
    for key, value in (profile.get("sensitive_answers") or {}).items():
        if "sponsor" not in str(key).lower() or not isinstance(value, dict):
            continue
        if value.get("approved") and str(value.get("answer") or "").strip().lower() in {"yes", "true", "1"}:
            return True
    return False


def _is_us_citizen(profile: dict[str, Any]) -> bool:
    direct = str(profile.get("citizenship") or profile.get("us_citizen") or "").strip().lower()
    if direct in {"yes", "true", "1", "u.s.", "us", "united states", "united states citizen"}:
        return True
    for key, value in (profile.get("sensitive_answers") or {}).items():
        if "citizen" not in str(key).lower() or not isinstance(value, dict):
            continue
        if value.get("approved") and str(value.get("answer") or "").strip().lower() in {"yes", "true", "1"}:
            return True
    return False


def _clearance_eligible(profile: dict[str, Any]) -> bool:
    direct = str(profile.get("security_clearance_eligibility") or "").strip().lower()
    if direct in {"yes", "true", "1"}:
        return True
    if direct in {"no", "false", "0"}:
        return False
    entry = (profile.get("sensitive_answers") or {}).get("security_clearance_eligibility")
    if isinstance(entry, dict) and entry.get("approved"):
        answer = str(entry.get("answer") or "").strip().lower()
        if answer in {"yes", "true", "1"}:
            return True
        if answer in {"no", "false", "0"}:
            return False
    return True
