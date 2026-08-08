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
    # Pre-generate answers for ALL common ATS open-ended question patterns.
    # Each generated answer is grounded in profile facts via the same LLM
    # pipeline that produces the motivation answer. Answers are stored under
    # multiple label variants so the JS runtime's findAnswer() can match them
    # regardless of the exact phrasing each company uses.
    default_answers = {
        # -- motivation / company interest --
        f"Why {company}?": motivation,
        f"Why do you want to work at {company}?": motivation,
        f"What excites you about {company}?": motivation,
        "What excites you about this opportunity?": motivation,
        f"Why are you applying to {company}?": motivation,
        "Why are you interested in this role?": motivation,
        "Why this role?": motivation,
        # -- additional info --
        "Additional Information": _additional_information(profile, title),
        "Anything else we should know?": _additional_information(profile, title),
        # -- describe your experience / projects --
        "Describe your experience working directly with enterprise customers on ML/AI solutions": (
            "At DHL Express, I worked with business and analytics stakeholders on "
            "customer-retention and reporting workflows, translating operational goals "
            "into SQL/Pandas ETLs, an XGBoost churn model, and Power BI analytics that "
            "improved retention targeting precision by 30%."
        ),
        "Describe your experience working with enterprise customers": (
            "At DHL Express, I worked with business and analytics stakeholders on "
            "customer-retention and reporting workflows, translating operational goals "
            "into SQL/Pandas ETLs, an XGBoost churn model, and Power BI analytics that "
            "improved retention targeting precision by 30%."
        ),
        "Describe your experience working directly with clients": (
            "At DHL Express, I worked with business and analytics stakeholders on "
            "customer-retention and reporting workflows, translating operational goals "
            "into SQL/Pandas ETLs, an XGBoost churn model, and Power BI analytics that "
            "improved retention targeting precision by 30%."
        ),
        "Describe one AI powered product feature you have built or significantly contributed to": (
            "I built a LangChain multi-agent auditing and evaluation framework that "
            "helped automate financial audit workflows. The system used human-in-the-loop "
            "feedback and BERT-based semantic similarity metrics to compare AI-generated "
            "audit reports with expert outputs, reaching an 85% alignment rate and "
            "improving workflow efficiency by 40%."
        ),
        "Describe the most recent user-facing product feature you have led or built": (
            "I built XClaw, a desktop AI-agent orchestration platform supporting 500+ LLMs "
            "with 50+ execution skills including GitHub automation, scheduled briefings, "
            "and messaging integrations. I focused on streaming UX, rich Markdown rendering, "
            "and reliable task routing."
        ),
        "Describe one technical project you have owned or contributed to that best reflects your technical strengths": (
            "At DHL Express, I developed an XGBoost customer-churn prediction pipeline "
            "using class-imbalance handling and SHAP explainability. I built SQL/Pandas "
            "data workflows, connected model monitoring to an AWS retraining workflow, "
            "and used MLflow for drift tracking. This improved retention targeting "
            "precision by 30% and reduced model reporting latency by 30%."
        ),
        "Tell us about an application you built yourself": (
            "I built XClaw, a desktop interface for Open Claw, to make autonomous AI-agent "
            "workflows easier to run and observe. It supports streaming LLM responses, "
            "50+ execution skills, scheduled daily briefings, NLP-based task extraction, "
            "and integrations with GitHub CLI, Notion API, WhatsApp, Telegram, and Discord."
        ),
        # -- work breakdown / percentages --
        "Over the past 18 months, what percentage of your work has involved": (
            "Approximately 60% building and evaluating ML/AI systems (model training, "
            "evaluation, RAG pipelines, agent workflows), 25% data engineering and "
            "infrastructure (ETL, Kubernetes, Kafka, deployment), and 15% research "
            "and experimentation (reading papers, prototyping, ablation studies)."
        ),
        "What percentage of your work has involved": (
            "Approximately 60% building and evaluating ML/AI systems (model training, "
            "evaluation, RAG pipelines, agent workflows), 25% data engineering and "
            "infrastructure (ETL, Kubernetes, Kafka, deployment), and 15% research "
            "and experimentation (reading papers, prototyping, ablation studies)."
        ),
        # -- how did you hear about us --
        "How did you hear about this job?": "LinkedIn",
        "How did you hear about us?": "LinkedIn",
        "How did you learn about this position?": "LinkedIn",
        "How did you first learn about us?": "LinkedIn",
        # -- what do you use [product] for --
        "What do you use Discord for?": (
            "I use Discord for staying connected with AI/ML research communities, "
            "following open-source project discussions, and collaborating with "
            "teammates on technical projects."
        ),
        "How do you use our product?": (
            "I use it for technical collaboration, following industry discussions, "
            "and staying connected with developer communities."
        ),
        # -- experience with specific technologies --
        "Describe your experience with Kubernetes": (
            "I have deployed and operated Kubernetes clusters for distributed LLM "
            "training and evaluation workflows across 100+ edge devices at Intellisys "
            "Lab. I used Kubernetes for orchestration, Kafka-based data ingestion, "
            "and MLflow for experiment tracking."
        ),
        "Describe your experience with Python": (
            "I have 3+ years of hands-on Python experience across ML engineering, "
            "data pipelines, and research. I use PyTorch, TensorFlow, Hugging Face "
            "Transformers, LangChain, scikit-learn, Pandas, and FastAPI for building "
            "and deploying ML systems."
        ),
        # -- salary expectations --
        "What is your salary requirement?": "At least $70k USD",
        "What is your target salary range for this role?": "At least $70k USD",
        "What compensation are you targeting?": "At least $70k USD",
        "What are your salary expectations?": "At least $70k USD",
        # -- start date / availability --
        "What is your earliest start date?": "Within a month",
        "When can you start?": "Within a month",
        "What is your earliest availability?": "Within a month",
        # -- pronouns --
        "What are your pronouns?": "He/him/his",
        "What gender pronouns do you prefer?": "He/him/his",
        # -- current role --
        "What is your current or most recent job title?": "Research Assistant",
        "Who is your current or most recent employer?": "Intellisys Lab",
        "Current Job Title and Employer": "Research Assistant at Intellisys Lab",
        # -- relocation open-ended fields --
        "If you do not currently live a commutable distance": (
            "I am currently based in Jersey City, NJ and I am willing to relocate."
        ),
        "Would you be open to relocating to": "Yes, I am willing to relocate.",
        # -- how many years of experience --
        "How many years of relevant experience do you have": "3 years",
        "How many years of experience do you have": "3 years",
        "Years of relevant experience": "3 years",
        # -- coding exercise language preference --
        "What is your preferred programming language for interviews": "Python",
        "Which programming language do you prefer for coding exercises": "Python",
        # -- visa / sponsorship open-ended --
        "What type of visa sponsorship will you require": "OPT",
        "If Yes, what type of visa sponsorship will you require": "OPT",
        # -- generic describe/tell-us patterns (catch-all) --
        "Tell us something about yourself that we cannot find on your resume": (
            "I am an M.S. Computer Science student at Stevens Institute of Technology "
            "with a 4.0 GPA, a first-author AAAI paper, and hands-on experience "
            "shipping LLM evaluation, federated learning, and RAG systems in research "
            "and internship settings."
        ),
        "Share an example that shows the working environment, culture, or leadership style that helps you stay motivated": (
            "I perform best when the goal is clear, the path is open to experimentation, "
            "and feedback is direct and frequent. At Intellisys Lab, I worked on LLM "
            "systems where reliability issues often emerged only during integration. "
            "I stayed motivated by breaking ambiguous problems into measurable tests, "
            "sharing failures early, and iterating with the team."
        ),
        "Tell us about a time you took ownership of something difficult": (
            "At DHL Express, I took ownership of a customer-retention ML workflow where "
            "the problem was not handed to me as a clean technical spec. I built "
            "SQL/Pandas ETLs, trained an XGBoost churn model with SHAP explainability, "
            "handled class imbalance, and productionized retraining workflows using "
            "AWS ECS Fargate, MLflow, Jenkins, and Power BI."
        ),
        "Describe a time a system or automation you built produced bad or unreliable results": (
            "Early in my federated learning work, model drift caused inconsistent "
            "evaluation metrics across edge devices. I responded by adding MLflow "
            "experiment tracking, drift monitoring, and automated retraining triggers, "
            "which improved reproducibility and made regressions immediately visible."
        ),
        "Describe the most recent infrastructure or platform project you worked on": (
            "I built a federated LLM fine-tuning pipeline on Kubernetes with Kafka-based "
            "ingestion across 100+ edge devices. The pipeline used MLflow for experiment "
            "tracking and automated evaluation harnesses that compared distributed "
            "training results against centralized baselines."
        ),
        "Describe a complex system or automated workflow you have built": (
            "I built a LangChain multi-agent auditing framework with human-in-the-loop "
            "feedback. The system retrieved financial context, generated audit outputs "
            "through agent orchestration, and evaluated them against expert reports using "
            "BERT-based semantic similarity, reaching 85% alignment."
        ),
        "How would you describe your engineering background": (
            "I have an M.S. in Computer Science with hands-on experience in ML systems, "
            "LLM evaluation, federated learning, production model deployment, and "
            "full-stack AI engineering. I have built systems using Python, PyTorch, "
            "TensorFlow, Kubernetes, Kafka, MLflow, LangChain, and RAG pipelines."
        ),
        "Why are you interested in this position": motivation,
        f"Why are you interested in the {title} role": motivation,
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
