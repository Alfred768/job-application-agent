from job_agent.application_answers import enrich_profile_for_job
from job_agent.models import Job


def test_enrich_profile_for_job_adds_job_scoped_answers_without_mutating_source():
    profile = {
        "name": "Gaoyi Wu",
        "skills": ["Python", "PyTorch", "RAG"],
        "answers": {"How did you hear about us?": "Company website"},
    }
    job = Job(
        title="Research Engineer",
        company="Anthropic",
        raw_jd="Build model evaluation systems.",
        location="New York, NY",
        source="anthropic:official-careers",
        source_url="https://jobs.anthropic.com/search?keywords=research%20engineer",
    )

    enriched = enrich_profile_for_job(profile, job, profile_vector_db=None)

    assert profile["answers"] == {"How did you hear about us?": "Company website"}
    assert enriched["target_company"] == "Anthropic"
    assert enriched["job_source"] == "anthropic:official-careers"
    assert enriched["job_source_url"] == "https://jobs.anthropic.com/search?keywords=research%20engineer"
    assert enriched["application_source_kind"] == "anthropic:official-careers"
    assert enriched["application_source_url"] == "https://jobs.anthropic.com/search?keywords=research%20engineer"
    assert "Why Anthropic?" in enriched["answers"]
    assert enriched["answers"]["What excites you about Anthropic?"] == enriched["answers"]["Why Anthropic?"]
    assert enriched["answers"]["What excites you about this opportunity?"] == enriched["answers"]["Why Anthropic?"]
    assert enriched["answers"]["Why are you applying to Anthropic?"] == enriched["answers"]["Why Anthropic?"]
    assert "model" in enriched["answers"]["Why Anthropic?"].lower() or "ai" in enriched["answers"]["Why Anthropic?"].lower()
    assert "Have you ever interviewed at Anthropic before?" not in enriched["answers"]
    assert "Are you open to working in-person in one of our offices 25% of the time?" not in enriched["answers"]


def test_enrich_profile_for_job_marks_no_ai_application_instruction():
    profile = {"answers": {}}
    job = Job(
        title="Researcher",
        company="Epoch",
        raw_jd="Please answer the application questions without LLM assistance.",
    )

    enriched = enrich_profile_for_job(profile, job, profile_vector_db=None)

    assert enriched["application_requires_user_authored_answers"] is True
