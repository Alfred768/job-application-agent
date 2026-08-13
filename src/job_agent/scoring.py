from __future__ import annotations

import re

from job_agent.models import FitScore, Job


ROLE_KEYWORDS: dict[str, list[str]] = {
    "Agent Engineer": ["agent", "agentic", "langchain", "tool", "rag", "llm", "workflow", "ai engineer", "generative ai", "genai", "prompt", "vector"],
    "ML Infra": ["kubernetes", "kafka", "mlflow", "infrastructure", "serving", "docker", "platform"],
    "MLE": ["machine learning", "transformer", "python", "c++", "experimentation", "reliability", "inference", "pytorch", "tensorflow", "training", "fine-tuning", "model"],
    "SDE": ["backend", "api", "distributed", "postgres", "redis", "typescript", "aws", "kubernetes", "python", "go", "react", "infrastructure", "cloud", "docker"],
    "Data Scientist": ["analysis", "experiment", "statistics", "sql", "dashboard", "shap"],
    "AI Algorithm Engineer": [
        "algorithm",
        "prediction",
        "planning",
        "simulation",
        "computer vision",
        "fine-tuning",
        "lora",
        "adversarial",
        "evaluation",
    ],
    "Unity ML Infrastructure": ["unity", "ray", "training dataset", "simulation"],
}

TITLE_ROLE_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("Unity ML Infrastructure", (r"\bunity\b",)),
    ("Data Scientist", (r"\bdata scientist\b",)),
    (
        "ML Infra",
        (
            r"\bml infrastructure\b",
            r"\bmachine learning infrastructure\b",
            r"\bml platform\b",
            r"\bmachine learning platform\b",
            r"\bml infrastructure engineer\b",
            r"\bmachine learning infrastructure engineer\b",
            r"\bml platform engineer\b",
            r"\bmachine learning platform engineer\b",
        ),
    ),
    (
        "MLE",
        (
            r"\bmachine learning engineer\b",
            r"\bml engineer\b",
            r"\bresearch engineer\b.*\bmachine learning\b",
            r"\bmachine learning\b.*\bresearch engineer\b",
        ),
    ),
    (
        "AI Algorithm Engineer",
        (
            r"\balgorithm engineer\b",
            r"\balgorithm software engineer\b",
            r"\bsoftware engineer\b.*\balgorithm\b",
            r"\bsoftware engineer,\s*algorithm\b",
        ),
    ),
    (
        "SDE",
        (
            r"\bsoftware engineer\b",
            r"\bbackend engineer\b",
            r"\bfull[\s-]?stack engineer\b",
        ),
    ),
    (
        "Agent Engineer",
        (
            r"\bagent engineer\b",
            r"\bai agent engineer\b",
            r"\bllm engineer\b",
        ),
    ),
]

# Agent-related words also occur in business-operations roles. A technical
# resume track must not be selected from the title alone when the role is
# explicitly marketing, revenue, sales, or GTM operations.
BUSINESS_OPERATIONS_TITLE_PATTERNS = (
    r"\bmarketing\s+(?:ops|operations)\b",
    r"\b(?:revenue|sales)\s+(?:ops|operations)\b",
    r"\brevops\b",
    r"\bgtm\s+(?:ops|operations|systems)\b",
    r"\bgo[\s-]?to[\s-]?market\b",
)

# Titles that carry generic business/operations "agent" words or non-engineering
# trade terms.  These are hard rejects even when the job description mentions
# AI/ML/software keywords, so the candidate does not spend an application slot
# on a role that is not an engineering track.
NON_TECHNICAL_TITLE_PATTERN = re.compile(
    r"\b(?:"
    r"customer\s+service\s+agent|"
    r"customer\s+experience\s+agent|"
    r"customer\s+support\s+agent|"
    r"front\s+desk\s+agent|"
    r"commissary\s+agent|"
    r"rental\s+agent|"
    r"loaner\s+agent|"
    r"executive\s+protection\s+agent|"
    r"field\s+agent|"
    r"field\s+flex\s+agent|"
    r"insurance\s+agent|"
    r"real\s+estate\s+agent|"
    r"survey\s+agent|"
    r"claims\s+agent|"
    r"qa\s*/\s*qc\s+agent|"
    r"business\s+development\s+representative|"
    r"sales\s+development\s+representative|"
    r"account\s+executive|"
    r"junior\s+accountant|"
    r"accountant\b|"
    r"auto\s+body\s+repair\s+technician|"
    r"repair\s+technician\b|"
    r"receptionist\b|"
    r"cashier\b|"
    r"mechanic\b|"
    r"recruiter\b|"
    r"talent\s+acquisition\b|"
    r"strategist\b"
    r")\b",
    flags=re.IGNORECASE,
)

# Hard blockers that no runtime autofill can truthfully satisfy.  These roles
# are rejected before scoring so the batch does not spend a slot on an
# application the candidate is ineligible for.
HARD_REQUIREMENT_PATTERNS = (
    r"must be a u\.?s\.? (?:citizen|national|person)",
    r"u\.?s\.? citizenship\s+is\s+required",
    r"only\s+u\.?s\.? citizens",
    r"u\.?s\.? citizens?\s+(?:or|and)\s+lawful\s+permanent\s+residents?",
    r"must\s+be\s+(?:a\s+)?u\.?s\.? (?:citizen|person)[^.]{0,80}required",
    r"u\.?s\.? person[^.]{0,80}required",
    r"active\s+(?:u\.?s\.? )?(?:government\s+)?security\s+clearance\s+(?:is\s+)?required",
    r"must\s+(?:hold|have|maintain)\s+(?:an\s+)?active\s+.*?security\s+clearance",
    r"current\s+.*?clearance\s+(?:is\s+)?required",
    r"interim\s+secret\s+within",
    r"not\s+eligible\s+for\s+visa\s+sponsorship",
    r"not\s+eligible\s+for\s+(?:h-?1b|visa)",
    r"no\s+visa\s+sponsorship",
    r"(?:does|will)\s+not\s+(?:provide|offer|support)\s+visa\s+sponsorship",
    r"(?:cannot|unable\s+to)\s+(?:provide\s+)?sponsorship",
    r"will\s+not\s+sponsor",
    r"does\s+not\s+sponsor",
)

NON_US_LOCATION_MARKERS = (
    "brasil",
    "brazil",
    "canada",
    "mexico",
    "ukraine",
    "ukrainian",
    "kyiv",
    "united kingdom",
    "uk,",
    "london",
    "germany",
    "berlin",
    "france",
    "paris",
    "netherlands",
    "amsterdam",
    "india",
    "singapore",
    "japan",
    "tokyo",
    "china",
    "beijing",
    "shanghai",
    "australia",
    "sydney",
    "cyprus",
    "nicosia",
    "limassol",
    "rotterdam",
    "wien",
    "vienna",
    "zurich",
    "zürich",
    "austria",
    "switzerland",
    "dusseldorf",
    "düsseldorf",
    "reykjavik",
    "reykjavík",
    "toulouse",
    "nantes",
    "espoo",
)


def _hard_requirement_conflict(job: Job) -> str | None:
    text = _job_text(job)
    for pattern in HARD_REQUIREMENT_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return f"Job requires eligibility the candidate does not have ({pattern})"
    location = (job.location or "").lower().strip()
    if location and location not in {"remote", "united states"}:
        has_us_marker = any(
            marker in location
            for marker in (
                "united states",
                " usa",
                " u.s.",
                " us,",
                " us;",
                "us only",
                ", us",
                "; us",
                "us and",
                "in the us",
            )
        )
        if not has_us_marker:
            for marker in NON_US_LOCATION_MARKERS:
                if marker in location:
                    return f"Job location is outside the United States ({job.location})"
    return None


def _job_text(job: Job) -> str:
    return f"{job.title}\n{job.raw_jd}".lower()


def _contains_keyword(text: str, keyword: str) -> bool:
    if not text or not keyword:
        return False
    if keyword == "c++":
        return bool(re.search(r"\bc\+\+\b", text, flags=re.IGNORECASE))
    tokens = [re.escape(token) for token in keyword.split() if token]
    if not tokens:
        return False
    pattern = r"(?<![a-z0-9])" + r"\s+".join(tokens) + r"(?![a-z0-9])"
    return bool(re.search(pattern, text, flags=re.IGNORECASE))


def _title_role_override(job: Job) -> str | None:
    title = job.title or ""
    if NON_TECHNICAL_TITLE_PATTERN.search(title):
        return "Other"
    if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in BUSINESS_OPERATIONS_TITLE_PATTERNS):
        return "Other"
    for role, patterns in TITLE_ROLE_HINTS:
        if any(re.search(pattern, title, flags=re.IGNORECASE) for pattern in patterns):
            return role
    return None


def classify_role(job: Job) -> str:
    title_override = _title_role_override(job)
    if title_override:
        return title_override
    text = _job_text(job)
    scores = {
        role: sum(1 for keyword in keywords if _contains_keyword(text, keyword))
        for role, keywords in ROLE_KEYWORDS.items()
    }
    best_role, best_score = max(scores.items(), key=lambda item: item[1])
    return best_role if best_score else "Other"


def score_fit(job: Job) -> FitScore:
    hard_conflict = _hard_requirement_conflict(job)
    if hard_conflict:
        return FitScore(
            score=5,
            role_track="Other",
            reasons=[hard_conflict],
            recommendation="reject",
            explanation="Candidate does not meet a hard job requirement.",
        )
    if NON_TECHNICAL_TITLE_PATTERN.search(job.title or ""):
        return FitScore(
            score=5,
            role_track="Other",
            reasons=["Non-technical role title"],
            recommendation="reject",
            explanation="Title is an obvious non-engineering role.",
        )
    text = _job_text(job)
    role_track = classify_role(job)
    keywords = ROLE_KEYWORDS.get(role_track, [])
    matched = [keyword for keyword in keywords if _contains_keyword(text, keyword)]
    missing = [keyword for keyword in keywords if not _contains_keyword(text, keyword)][:5]

    title_role = _title_role_override(job)
    explicit_title_match = (
        role_track != "Other"
        and title_role == role_track
    )
    base = (
        52
        if explicit_title_match
        else 40
        if role_track != "Other"
        else 20
    )
    score = min(95, base + len(matched) * 12)
    reasons = [f"Matched {keyword}" for keyword in matched[:5]]
    if role_track != "Other":
        reasons.insert(0, f"Classified as {role_track}")
    if explicit_title_match:
        reasons.insert(
            1,
            f"Title explicitly matches {role_track}",
        )

    return FitScore(
        score=score,
        role_track=role_track,
        reasons=reasons,
        matched_skills=matched,
        missing_keywords=missing,
        recommendation="prepare" if score >= 70 else "review",
        explanation="; ".join(reasons) if reasons else "Insufficient role-specific evidence.",
    )
