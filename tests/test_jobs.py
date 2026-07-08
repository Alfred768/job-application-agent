from job_agent.jobs import import_job_from_text


def test_import_job_from_text_extracts_basic_fields():
    jd = (
        "Company: Acme AI\n"
        "Title: Agent Engineer\n"
        "Location: Remote\n\n"
        "Build LLM agents with Python and FastAPI."
    )

    job = import_job_from_text(jd)

    assert job.company == "Acme AI"
    assert job.title == "Agent Engineer"
    assert job.location == "Remote"
    assert "FastAPI" in job.raw_jd


def test_import_job_from_text_uses_safe_fallbacks():
    job = import_job_from_text("Build internal tools with Python.")

    assert job.company == "Unknown Company"
    assert job.title == "Unknown Role"
