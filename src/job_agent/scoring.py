from __future__ import annotations

from job_agent.models import FitScore, Job


ROLE_KEYWORDS: dict[str, list[str]] = {
    "Agent Engineer": ["agent", "langchain", "tool", "rag", "llm", "workflow"],
    "ML Infra": ["kubernetes", "kafka", "mlflow", "infrastructure", "serving", "docker"],
    "MLE": ["machine learning", "model", "xgboost", "pytorch", "feature", "inference"],
    "SDE": ["backend", "api", "distributed", "postgres", "redis", "typescript"],
    "Data Scientist": ["analysis", "experiment", "statistics", "sql", "dashboard", "shap"],
    "AI Algorithm Engineer": ["algorithm", "fine-tuning", "lora", "adversarial", "evaluation"],
    "Unity ML Infrastructure": ["unity", "ray", "training dataset", "simulation"],
}


def _job_text(job: Job) -> str:
    return f"{job.title}\n{job.raw_jd}".lower()


def classify_role(job: Job) -> str:
    text = _job_text(job)
    scores = {
        role: sum(1 for keyword in keywords if keyword in text)
        for role, keywords in ROLE_KEYWORDS.items()
    }
    best_role, best_score = max(scores.items(), key=lambda item: item[1])
    return best_role if best_score else "Other"


def score_fit(job: Job) -> FitScore:
    text = _job_text(job)
    role_track = classify_role(job)
    keywords = ROLE_KEYWORDS.get(role_track, [])
    matched = [keyword for keyword in keywords if keyword in text]
    missing = [keyword for keyword in keywords if keyword not in text][:5]

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
