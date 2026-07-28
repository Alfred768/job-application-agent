from __future__ import annotations

from pathlib import Path
from typing import Any

from job_agent.jobs import format_job_as_jd_text
from job_agent.models import Job
from job_agent.profile_vector_store import search_profile_embeddings


NO_AI_APPLICATION_PATTERNS = [
    "do not use llm",
    "do not use llms",
    "do not use ai",
    "without llm assistance",
    "without ai assistance",
    "without using ai",
]


def enrich_profile_for_job(
    profile: dict[str, Any],
    job: Job,
    *,
    llm: Any | None = None,
    use_llm: bool = False,
    profile_vector_db: str | Path | None = None,
) -> dict[str, Any]:
    """Add job-scoped answers that the runtime can use in the real browser.

    The base profile stores stable user facts. This function adds only
    job-scoped narrative answers grounded in those facts. It deliberately does
    not infer legal, employment-history, location, demographic, availability,
    or consent answers: required questions in those categories must be backed
    by an explicit profile answer or approved sensitive-answer record.
    """
    enriched = dict(profile)
    enriched["target_company"] = job.company
    enriched["target_title"] = job.title
    enriched["target_location"] = job.location or ""
    if job.source:
        enriched["job_source"] = job.source
        enriched["application_source_kind"] = job.source
    if job.source_url:
        enriched["job_source_url"] = job.source_url
        enriched["application_source_url"] = job.source_url

    answers = dict(profile.get("answers") or {})
    context = _profile_context(profile_vector_db, job)
    motivation = _motivation_answer(job, profile, context, llm=llm, use_llm=use_llm)

    company = job.company or "the company"
    title = job.title or "this role"
    default_answers = {
        f"Why {company}?": motivation,
        f"Why do you want to work at {company}?": motivation,
        f"What excites you about {company}?": motivation,
        "What excites you about this opportunity?": motivation,
        f"Why are you applying to {company}?": motivation,
        "Why are you interested in this role?": motivation,
        "Why this role?": motivation,
        "Additional Information": _additional_information(profile, title),
    }
    for key, value in default_answers.items():
        answers.setdefault(key, value)

    enriched["answers"] = answers
    if context:
        enriched["profile_vector_context"] = context
    if _contains_no_ai_application_instruction(format_job_as_jd_text(job)):
        enriched["application_requires_user_authored_answers"] = True
    return enriched


def _contains_no_ai_application_instruction(text: str) -> bool:
    normalized = (text or "").lower()
    return any(pattern in normalized for pattern in NO_AI_APPLICATION_PATTERNS)


def _profile_context(profile_vector_db: str | Path | None, job: Job) -> str:
    if not profile_vector_db:
        return ""
    db_path = Path(profile_vector_db)
    if not db_path.is_file():
        return ""
    query = f"{job.company} {job.title}\n{job.raw_jd or ''}"[:4000]
    try:
        matches = search_profile_embeddings(db_path, query=query, top_k=4)
    except Exception:
        return ""
    parts = []
    for match in matches:
        content = str(match.get("content") or "").strip()
        if content:
            parts.append(content[:1200])
    return "\n\n".join(parts)


def _motivation_answer(
    job: Job,
    profile: dict[str, Any],
    context: str,
    *,
    llm: Any | None,
    use_llm: bool,
) -> str:
    if use_llm and llm is not None and getattr(llm, "provider", "deterministic") != "deterministic":
        prompt = (
            "Write a concise, truthful job application answer in first person. "
            "Question: Why are you interested in this company and role?\n"
            "Use only the applicant facts below. Do not invent experience, credentials, "
            "citizenship, clearance, or work authorization. Keep it under 140 words.\n\n"
            f"Company: {job.company}\n"
            f"Role: {job.title}\n"
            f"Location: {job.location or ''}\n"
            f"Job description excerpt:\n{(job.raw_jd or '')[:2500]}\n\n"
            f"Applicant facts:\n{_profile_fact_summary(profile)}\n\n"
            f"Retrieved profile context:\n{context[:3000]}"
        )
        try:
            answer = (llm.invoke([{"role": "user", "content": prompt}], max_tokens=220) or "").strip()
        except Exception:
            answer = ""
        if answer:
            return _trim_answer(answer, 160)
    return _deterministic_motivation(job, profile)


def _profile_fact_summary(profile: dict[str, Any]) -> str:
    skills = ", ".join(str(item) for item in (profile.get("skills") or [])[:18])
    projects = "; ".join(
        str(item.get("title") or "")
        for item in (profile.get("projects") or [])
        if isinstance(item, dict) and item.get("title")
    )
    roles = "; ".join(
        f"{item.get('title')} at {item.get('company')}"
        for item in (profile.get("work_history") or [])
        if isinstance(item, dict)
    )
    return "\n".join(
        part
        for part in [
            f"Name: {profile.get('name')}",
            f"Experience: {roles}",
            f"Skills: {skills}",
            f"Projects/publications: {projects}",
            f"Education: {_education_summary(profile)}",
        ]
        if part and not part.endswith(": ")
    )


def _education_summary(profile: dict[str, Any]) -> str:
    items = []
    for entry in profile.get("education") or []:
        if isinstance(entry, dict):
            items.append(
                " ".join(
                    str(part)
                    for part in [
                        entry.get("degree"),
                        entry.get("field"),
                        entry.get("school"),
                        entry.get("end_date"),
                    ]
                    if part
                )
            )
    return "; ".join(items)


def _deterministic_motivation(job: Job, profile: dict[str, Any]) -> str:
    company = job.company or "your team"
    title = job.title or "this role"
    skills = ", ".join(str(item) for item in (profile.get("skills") or [])[:8])
    return (
        f"I am interested in {company} because the {title} role aligns with my background "
        "building and evaluating reliable AI and machine learning systems. My experience "
        "includes LLM/RAG evaluation, federated model training, production ML workflows, "
        "and agent tooling, and I am looking for work where those skills can support "
        f"impactful products and rigorous engineering. Relevant tools I have used include {skills}."
    )


def _additional_information(profile: dict[str, Any], title: str) -> str:
    publication = next(
        (
            item
            for item in profile.get("projects") or []
            if isinstance(item, dict) and "fingerprinting" in str(item.get("title", "")).lower()
        ),
        None,
    )
    if publication:
        return (
            "I can share more detail on my accepted AAAI 2026 work on reliable LLM "
            "ownership verification and my hands-on AI systems projects if helpful."
        )
    return f"I would be glad to discuss how my AI/ML systems background fits the {title} role."


def _trim_answer(answer: str, max_words: int) -> str:
    words = answer.split()
    if len(words) <= max_words:
        return answer
    return " ".join(words[:max_words]).rstrip(".,;:") + "."
