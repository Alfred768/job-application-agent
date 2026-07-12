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


def propose_resume_edit_plan(
    jd_text: str,
    resume_track: str | None = None,
    evidence_text: str | None = None,
) -> ResumeEditPlan:
    analysis = parse_jd(jd_text)
    target_track = resume_track or analysis.role_track
    supported = [
        skill
        for skill in analysis.required_skills
        if skill in SUPPORTED_KEYWORDS
        and (evidence_text is None or skill.lower() in evidence_text.lower())
    ]
    unsupported = [skill for skill in analysis.required_skills if skill not in supported]
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


def render_tailored_resume_draft(base_resume_text: str, plan: ResumeEditPlan) -> str:
    supported_keywords = []
    for keyword in plan.summary_keywords + plan.skill_order + plan.bullet_keywords:
        if keyword and keyword not in supported_keywords:
            supported_keywords.append(keyword)

    lines = [
        "# Tailored Resume Draft",
        "",
        "> Review this draft manually before submitting. It preserves the base resume text and only inserts supported JD keywords.",
        "",
        "## Targeted Summary",
        "",
        f"Target track: {plan.target_track}",
    ]
    if supported_keywords:
        lines.append(f"Supported keyword emphasis: {', '.join(supported_keywords)}")
    else:
        lines.append("Supported keyword emphasis: None")

    lines.extend(
        [
            "",
            "## Keyword Alignment",
            "",
        ]
    )
    if supported_keywords:
        lines.extend([f"- {keyword}" for keyword in supported_keywords])
    else:
        lines.append("- No supported JD keywords detected.")

    lines.extend(
        [
            "",
            "## Base Resume",
            "",
            base_resume_text.strip(),
            "",
            "## Review Required",
            "",
        ]
    )
    if plan.unsupported_keywords:
        lines.append(
            f"Unsupported JD keywords not inserted: {', '.join(plan.unsupported_keywords)}"
        )
    else:
        lines.append("Unsupported JD keywords not inserted: None")
    lines.append("No new experience claims were added automatically.")
    return "\n".join(lines).strip() + "\n"


_LLM_RESUME_SYSTEM_PROMPT = (
    "You are a careful resume tailoring assistant for a human job applicant. "
    "Rewrite the candidate's existing resume to target the given job description.\n"
    "Rules:\n"
    "- Emphasize and surface JD keywords that are ALREADY supported by the resume.\n"
    "- Reorder skills so supported JD keywords come first.\n"
    "- Rephrase existing bullets to surface supported JD keywords already backed by real work.\n"
    "- NEVER invent employers, job titles, degrees, schools, dates, publications, "
    "certifications, metrics, or skills that are not in the original resume.\n"
    "- NEVER add unsupported JD keywords as if they were real experience.\n"
    "- Keep every factual claim from the original; only rewrite wording, ordering, and emphasis.\n"
    "- Keep it concise and ATS-readable. Output clean Markdown only, no preamble."
)


def _llm_resume_user_prompt(base_resume_text: str, jd_text: str, plan: ResumeEditPlan) -> str:
    supported = plan.summary_keywords + [
        k for k in plan.skill_order if k not in plan.summary_keywords
    ]
    return (
        f"BASE RESUME (original, must preserve all facts):\n```\n{base_resume_text.strip()}\n```\n\n"
        f"JOB DESCRIPTION:\n```\n{jd_text.strip()}\n```\n\n"
        f"Supported JD keywords already backed by the resume (emphasize these): "
        f"{supported or 'None'}\n"
        f"Unsupported JD keywords NOT in the resume (do NOT add as claims): "
        f"{plan.unsupported_keywords or 'None'}\n\n"
        "Return the full tailored resume as Markdown."
    )


def render_llm_tailored_resume_draft(
    base_resume_text: str,
    jd_text: str,
    llm,
    plan: ResumeEditPlan | None = None,
) -> str:
    """LLM-powered tailored resume draft, grounded in the base resume.

    Uses the configured LLM to rewrite wording/ordering/emphasis and embed
    supported JD keywords, then runs a deterministic truthfulness gate that
    flags any unsupported JD keyword that leaked into the output as a
    potential invented claim. Falls back to the deterministic draft if the
    LLM call fails or returns empty output.
    """
    plan = plan or propose_resume_edit_plan(jd_text)
    try:
        content = llm.invoke(
            [
                {"role": "system", "content": _LLM_RESUME_SYSTEM_PROMPT},
                {"role": "user", "content": _llm_resume_user_prompt(base_resume_text, jd_text, plan)},
            ],
            temperature=0.3,
            max_tokens=1400,
        ) or ""
    except Exception:
        content = ""

    if not content.strip():
        return render_tailored_resume_draft(base_resume_text, plan)

    # Truthfulness gate: flag unsupported JD keywords the LLM may have inserted.
    content_lower = content.lower()
    leaked = [k for k in plan.unsupported_keywords if k and k.lower() in content_lower]

    review_lines = [
        "",
        "---",
        "",
        "## Truthfulness Review (LLM draft)",
        "",
        f"- Target track: {plan.target_track}",
        f"- Unsupported JD keywords detected in LLM output: "
        + (", ".join(leaked) if leaked else "None"),
    ]
    if leaked:
        review_lines.append(
            "- WARNING: the above keywords are NOT in your base resume. Remove them "
            "or back them with real evidence before submitting."
        )
    review_lines.append(
        "- Always review LLM-rewritten bullets against the original resume for invented claims."
    )

    header = (
        "# Tailored Resume Draft (LLM)\n\n"
        "> LLM-rewritten from the base resume to target this JD. "
        "> Verify every claim against the original before submitting.\n"
    )
    return header + content.strip() + "\n" + "\n".join(review_lines) + "\n"
