from __future__ import annotations

from dataclasses import asdict, dataclass, field
import html
import re

from job_agent.jobs import import_job_from_text
from job_agent.scoring import classify_role


KNOWN_SKILL_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("LangChain", (r"\blangchain\b",)),
    ("RAG", (r"\brag\b", r"\bretrieval\s+augmented\s+generation\b")),
    ("FastAPI", (r"\bfastapi\b",)),
    ("Kafka", (r"\bkafka\b",)),
    ("Kubernetes", (r"\bkubernetes\b",)),
    ("MLflow", (r"\bmlflow\b",)),
    ("Docker", (r"\bdocker\b",)),
    ("Python", (r"\bpython\b",)),
    ("TypeScript", (r"\btypescript\b",)),
    ("Postgres", (r"\bpostgres(?:ql)?\b",)),
    ("Redis", (r"\bredis\b",)),
    ("PyTorch", (r"\bpytorch\b",)),
    ("XGBoost", (r"\bxgboost\b",)),
    ("SHAP", (r"\bshap\b",)),
    ("SQL", (r"\bsql\b",)),
    ("AWS", (r"\baws\b", r"\bamazon\s+web\s+services\b")),
    ("LoRA", (r"\blora\b",)),
    ("BERT", (r"\bbert\b",)),
    ("Rust", (r"\brust\b",)),
    ("Salesforce", (r"\bsalesforce\b",)),
    ("HubSpot", (r"\bhubspot\b",)),
    ("Snowflake", (r"\bsnowflake\b",)),
    ("Outreach", (r"\boutreach\b",)),
    ("Gong", (r"\bgong\b",)),
    ("Clay", (r"\bclay(?:gent)?\b",)),
]

_JD_METADATA_PREFIXES = (
    "company:",
    "title:",
    "location:",
    "source:",
    "source url:",
    "apply url:",
    "employment type:",
)

_RESPONSIBILITY_SECTION_HINTS = (
    "responsibilities:",
    "what you'll do:",
    "what you will do:",
    "in this role:",
)

_RESPONSIBILITY_SECTION_STOPS = (
    "minimum requirements:",
    "requirements:",
    "must-have:",
    "must have:",
    "what you bring:",
    "preferred requirements:",
    "preferred qualifications:",
    "qualifications:",
    "additional information:",
    "benefits:",
)


def _extract_required_skills(text: str) -> list[str]:
    required_text = _exclude_preferred_sections(text)
    matches: list[str] = []
    for skill, patterns in KNOWN_SKILL_PATTERNS:
        if any(re.search(pattern, required_text, flags=re.IGNORECASE) for pattern in patterns):
            matches.append(skill)
    return matches


def _exclude_preferred_sections(text: str) -> str:
    """Keep optional/nice-to-have skills out of a truthful required-skill gate."""
    preferred_markers = (
        "nice to have",
        "nice-to-have",
        "preferred qualifications",
        "preferred requirements",
        "strong candidates may",
        "bonus points",
    )
    heading_markers = (
        "logistics",
        "compensation",
        "benefits",
        "equal opportunity",
        "how to apply",
    )
    required_lines: list[str] = []
    in_preferred = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        normalized = line.lower().rstrip(":")
        if any(marker in normalized for marker in preferred_markers):
            in_preferred = True
            continue
        if in_preferred and normalized in heading_markers:
            in_preferred = False
        if not in_preferred:
            required_lines.append(line)
    return "\n".join(required_lines)


def _normalize_jd_text(text: str) -> str:
    """Turn public-ATS HTML fragments into readable, line-oriented JD text."""
    value = html.unescape(text or "")
    value = re.sub(r"<\s*(?:script|style)\b[^>]*>.*?<\s*/\s*(?:script|style)\s*>", "", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<\s*/?\s*(?:p|div|li|h[1-6]|section|article)\b[^>]*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value).replace("\\n", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_responsibilities(text: str) -> list[str]:
    responsibilities: list[str] = []
    section_lines: list[str] = []
    capture_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip("-• ").strip()
        if not line:
            continue
        raw_lower_line = line.lower()
        lower_line = raw_lower_line.rstrip(":").strip()
        if raw_lower_line.startswith(_JD_METADATA_PREFIXES):
            continue
        if lower_line in tuple(hint.rstrip(":") for hint in _RESPONSIBILITY_SECTION_HINTS):
            capture_section = True
            continue
        if capture_section and lower_line in tuple(stop.rstrip(":") for stop in _RESPONSIBILITY_SECTION_STOPS):
            break
        if line.endswith(":") and len(line) <= 80:
            continue
        if capture_section:
            section_lines.append(line)
        responsibilities.append(line)
    return (section_lines or responsibilities)[:5]


@dataclass(frozen=True)
class JDAnalysis:
    title: str
    company: str
    role_track: str
    required_skills: list[str] = field(default_factory=list)
    responsibilities: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def parse_jd(text: str) -> JDAnalysis:
    normalized_text = _normalize_jd_text(text)
    job = import_job_from_text(normalized_text)
    lower_text = normalized_text.lower()
    skills = _extract_required_skills(normalized_text)
    responsibilities = _extract_responsibilities(normalized_text)
    risks = []
    if "linkedin" in lower_text:
        risks.append("LinkedIn content should be handled only from user-provided JD text or compliant sources.")
    return JDAnalysis(
        title=job.title,
        company=job.company,
        role_track=classify_role(job),
        required_skills=skills,
        responsibilities=responsibilities,
        risks=risks,
    )
