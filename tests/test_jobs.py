from job_agent.jobs import deduplicate_jobs, format_job_as_jd_text, import_job_from_text, parse_rss_jobs
from job_agent.jobs import parse_greenhouse_jobs, parse_lever_jobs, parse_remotive_jobs
from job_agent.models import Job


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


def test_parse_greenhouse_jobs_normalizes_public_board_response():
    payload = {
        "jobs": [
            {
                "id": 123,
                "title": "Agent Engineer",
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
                "location": {"name": "Remote"},
                "content": "<p>Build LLM agents with Python and FastAPI.</p>",
            }
        ]
    }

    jobs = parse_greenhouse_jobs(payload, board_token="acme")

    assert len(jobs) == 1
    assert jobs[0].title == "Agent Engineer"
    assert jobs[0].company == "acme"
    assert jobs[0].location == "Remote"
    assert jobs[0].source == "greenhouse:acme"
    assert jobs[0].apply_url == "https://boards.greenhouse.io/acme/jobs/123"
    assert "FastAPI" in jobs[0].raw_jd


def test_parse_lever_jobs_normalizes_public_postings_response():
    payload = [
        {
            "id": "abc",
            "text": "ML Platform Engineer",
            "hostedUrl": "https://jobs.lever.co/acme/abc",
            "categories": {"location": "New York, NY", "team": "Engineering"},
            "descriptionPlain": "Own ML infrastructure and orchestration.",
            "workplaceType": "hybrid",
        }
    ]

    jobs = parse_lever_jobs(payload, site="acme")

    assert len(jobs) == 1
    assert jobs[0].title == "ML Platform Engineer"
    assert jobs[0].company == "acme"
    assert jobs[0].location == "New York, NY"
    assert jobs[0].remote_policy == "hybrid"
    assert jobs[0].source == "lever:acme"
    assert jobs[0].apply_url == "https://jobs.lever.co/acme/abc"
    assert "orchestration" in jobs[0].raw_jd


def test_parse_remotive_jobs_normalizes_public_api_response():
    payload = {
        "job-count": 1,
        "jobs": [
            {
                "id": 456,
                "title": "Backend Engineer",
                "company_name": "RemoteCo",
                "url": "https://remotive.com/remote-jobs/software-dev/backend-engineer-456",
                "candidate_required_location": "Worldwide",
                "description": "<p>Build APIs and distributed systems.</p>",
            }
        ],
    }

    jobs = parse_remotive_jobs(payload)

    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].company == "RemoteCo"
    assert jobs[0].location == "Worldwide"
    assert jobs[0].source == "remotive"
    assert jobs[0].apply_url == "https://remotive.com/remote-jobs/software-dev/backend-engineer-456"
    assert "distributed systems" in jobs[0].raw_jd


def test_deduplicate_jobs_collapses_tracking_variants_and_merges_provenance():
    jobs = [
        Job(
            title="Agent Engineer",
            company="Acme AI",
            location="Remote",
            raw_jd="Build agents.",
            source="company-rss",
            source_url="https://jobs.acme.example/roles/123?utm_source=rss",
            apply_url="https://jobs.acme.example/roles/123?utm_source=rss",
        ),
        Job(
            title="Agent Engineer",
            company="Acme AI",
            location="Remote",
            raw_jd="Build production LLM agents with Python, RAG, and FastAPI.",
            source="greenhouse:acme",
            source_url="https://jobs.acme.example/roles/123/",
            apply_url="https://jobs.acme.example/roles/123/",
        ),
    ]

    unique = deduplicate_jobs(jobs)

    assert len(unique) == 1
    assert unique[0].source == "company-rss | greenhouse:acme"
    assert "FastAPI" in unique[0].raw_jd
    assert unique[0].apply_url == "https://jobs.acme.example/roles/123?utm_source=rss"


def test_deduplicate_jobs_uses_role_identity_when_urls_are_missing():
    jobs = [
        Job(title="ML Engineer", company="Data Forge", location="New York, NY", raw_jd="First", source="rss-a"),
        Job(title=" ml engineer ", company="DATA  FORGE", location="new york, ny", raw_jd="More complete JD", source="rss-b"),
        Job(title="ML Engineer", company="Data Forge", location="Remote", raw_jd="Remote role", source="rss-c"),
    ]

    unique = deduplicate_jobs(jobs)

    assert len(unique) == 2
    assert unique[0].source == "rss-a | rss-b"
    assert unique[0].raw_jd == "More complete JD"
