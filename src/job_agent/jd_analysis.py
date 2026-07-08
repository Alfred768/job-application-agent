from __future__ import annotations

from dataclasses import asdict, dataclass, field

from job_agent.jobs import import_job_from_text
from job_agent.scoring import classify_role


KNOWN_SKILLS = [
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
    "Rust",
]


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
    job = import_job_from_text(text)
    lower_text = text.lower()
    skills = [skill for skill in KNOWN_SKILLS if skill.lower() in lower_text]
    responsibilities = [
        line.strip("-• ").strip()
        for line in text.splitlines()
        if line.strip() and not line.lower().startswith(("company:", "title:", "location:"))
    ][:5]
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
