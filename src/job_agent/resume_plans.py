from __future__ import annotations

from dataclasses import asdict, dataclass, field

from job_agent.jd_analysis import parse_jd


SUPPORTED_KEYWORDS = {
    "LangChain",
    "RAG",
    "FastAPI",
    "Kafka",
    "Kubernetes",
    "MLflow",
    "Docker",
    "Python",
    "TypeScript",
    "Postgres",
    "Redis",
    "PyTorch",
    "XGBoost",
    "SHAP",
    "SQL",
    "AWS",
    "LoRA",
    "BERT",
}


@dataclass(frozen=True)
class ResumeEditPlan:
    target_track: str
    summary_keywords: list[str] = field(default_factory=list)
    skill_order: list[str] = field(default_factory=list)
    bullet_keywords: list[str] = field(default_factory=list)
    unsupported_keywords: list[str] = field(default_factory=list)
    allowed_edits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def propose_resume_edit_plan(jd_text: str, resume_track: str | None = None) -> ResumeEditPlan:
    analysis = parse_jd(jd_text)
    target_track = resume_track or analysis.role_track
    supported = [skill for skill in analysis.required_skills if skill in SUPPORTED_KEYWORDS]
    unsupported = [skill for skill in analysis.required_skills if skill not in SUPPORTED_KEYWORDS]
    return ResumeEditPlan(
        target_track=target_track,
        summary_keywords=supported[:4],
        skill_order=supported,
        bullet_keywords=supported[:6],
        unsupported_keywords=unsupported,
        allowed_edits=[
            "Rewrite summary toward the target role.",
            "Reorder skills using supported JD keywords.",
            "Rephrase existing bullets without inventing new facts.",
        ],
    )
