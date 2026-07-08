from job_agent.jobs import format_job_as_jd_text, import_job_from_text, parse_rss_jobs


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


def test_parse_rss_jobs_normalizes_public_feed_items():
    rss = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Example Jobs</title>
        <item>
          <title>Agent Engineer at Acme AI</title>
          <link>https://jobs.example.com/acme-agent</link>
          <description>Build LLM agents with Python, RAG, and FastAPI.</description>
          <category>Remote</category>
        </item>
        <item>
          <title>ML Platform Engineer - DataForge</title>
          <link>https://jobs.example.com/dataforge-ml</link>
          <description>Own ML infrastructure and orchestration.</description>
        </item>
      </channel>
    </rss>
    """

    jobs = parse_rss_jobs(rss, source="example-rss")

    assert len(jobs) == 2
    assert jobs[0].title == "Agent Engineer"
    assert jobs[0].company == "Acme AI"
    assert jobs[0].source == "example-rss"
    assert jobs[0].source_url == "https://jobs.example.com/acme-agent"
    assert jobs[0].apply_url == "https://jobs.example.com/acme-agent"
    assert jobs[0].location == "Remote"
    assert "FastAPI" in jobs[0].raw_jd
    assert jobs[1].title == "ML Platform Engineer"
    assert jobs[1].company == "DataForge"


def test_format_job_as_jd_text_preserves_provenance_for_agent_review():
    job = parse_rss_jobs(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        <category>Remote</category>
        </item></channel></rss>""",
        source="example-rss",
    )[0]

    text = format_job_as_jd_text(job)

    assert "Company: Acme AI" in text
    assert "Title: Agent Engineer" in text
    assert "Location: Remote" in text
    assert "Source: example-rss" in text
    assert "Apply URL: https://jobs.example.com/acme-agent" in text
    assert "Build LLM agents" in text
