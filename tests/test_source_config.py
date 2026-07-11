import json

from job_agent.source_config import load_jobs_from_source_config, _read_url


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


def test_load_jobs_from_source_config_deduplicates_overlapping_sources(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Agent Engineer at Acme</title>
        <link>https://jobs.example.com/acme-agent?utm_source=feed</link>
        <description>Build agents.</description>
        </item></channel></rss>"""
    )
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        '{"jobs": [{"title": "Agent Engineer", "absolute_url": "https://jobs.example.com/acme-agent/", "location": {"name": "Remote"}, "content": "Build production agents with Python."}]}'
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {"type": "rss", "source": "company-rss", "rss_file": str(rss_path)},
                    {"type": "greenhouse", "board_token": "acme", "payload_file": str(greenhouse_path)},
                ]
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert len(jobs) == 1
    assert jobs[0].source == "company-rss | greenhouse:acme"
    assert "Python" in jobs[0].raw_jd


def test_read_url_sends_browser_user_agent_not_python_urllib(monkeypatch):
    """Public job APIs (e.g. Remotive) 403 the default Python-urllib UA.

    Regression guard: the agent's autonomous source fetcher must send a
    browser-like User-Agent so live job fetching keeps working.
    """
    import job_agent.source_config as source_config

    captured = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"jobs": []}'

    def fake_urlopen(request, timeout=20):
        captured["user_agent"] = request.get_header("User-agent")
        return _FakeResponse()

    monkeypatch.setattr(source_config, "urlopen", fake_urlopen)
    _read_url("https://remotive.com/api/remote-jobs")

    assert captured["user_agent"]
    assert "Python-urllib" not in captured["user_agent"]
    assert "Mozilla" in captured["user_agent"]
