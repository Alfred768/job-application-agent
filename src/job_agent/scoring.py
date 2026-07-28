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
    text = _job_text(job)
    role_track = classify_role(job)
    keywords = ROLE_KEYWORDS.get(role_track, [])
    matched = [keyword for keyword in keywords if _contains_keyword(text, keyword)]
    missing = [keyword for keyword in keywords if not _contains_keyword(text, keyword)][:5]

    base = 40 if role_track != "Other" else 20
    score = min(95, base + len(matched) * 12)
    reasons = [f"Matched {keyword}" for keyword in matched[:5]]
    if role_track != "Other":
        reasons.insert(0, f"Classified as {role_track}")

    return FitScore(
        score=score,
        role_track=role_track,
        reasons=reasons,
        matched_skills=matched,
        missing_keywords=missing,
        recommendation="prepare" if score >= 70 else "review",
        explanation="; ".join(reasons) if reasons else "Insufficient role-specific evidence.",
    )
