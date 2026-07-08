import json

from job_agent.source_config import load_jobs_from_source_config


def test_load_jobs_from_source_config_combines_public_sources(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme AI</title>
        <link>https://jobs.example.com/acme-agent</link>
        <description>Build LLM agents with FastAPI.</description>
        <category>Remote</category>
        </item></channel></rss>"""
    )
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        '{"jobs": [{"title": "ML Platform Engineer", "absolute_url": "https://boards.greenhouse.io/dataforge/jobs/1", "location": {"name": "Remote"}, "content": "Build ML platforms."}]}'
    )
    remotive_path = tmp_path / "remotive.json"
    remotive_path.write_text(
        '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs."}]}'
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {"type": "rss", "source": "example-rss", "rss_file": str(rss_path)},
                    {"type": "greenhouse", "board_token": "dataforge", "payload_file": str(greenhouse_path)},
                    {"type": "remotive", "payload_file": str(remotive_path)},
                ]
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.title for job in jobs] == [
        "Agent Engineer",
        "ML Platform Engineer",
        "Backend Engineer",
    ]
    assert [job.source for job in jobs] == [
        "example-rss",
        "greenhouse:dataforge",
        "remotive",
    ]
