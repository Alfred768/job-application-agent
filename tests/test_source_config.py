import json
from urllib.error import URLError

import pytest

from job_agent.source_config import (
    _read_url,
    _source_timeout_seconds,
    load_jobs_from_source_config,
)
from job_agent.source_config import _filter_jobs_for_source
from job_agent.models import Job


def test_source_timeout_seconds_uses_env_override(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_SOURCE_TIMEOUT_SECONDS", "45")
    assert _source_timeout_seconds() == 45


def test_source_timeout_seconds_clamps_below_five(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_SOURCE_TIMEOUT_SECONDS", "1")
    assert _source_timeout_seconds() == 5


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
    ashby_path = tmp_path / "ashby.json"
    ashby_path.write_text(
        '{"jobs": [{"title": "AI Product Engineer", "jobUrl": "https://jobs.ashbyhq.com/brainco/1", "applyUrl": "https://jobs.ashbyhq.com/brainco/1/application", "location": "San Francisco", "descriptionHtml": "Build AI products."}]}'
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {"type": "rss", "source": "example-rss", "rss_file": str(rss_path)},
                    {"type": "greenhouse", "board_token": "dataforge", "payload_file": str(greenhouse_path)},
                    {"type": "ashby", "organization": "brainco", "payload_file": str(ashby_path)},
                    {"type": "remotive", "payload_file": str(remotive_path)},
                ]
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.title for job in jobs] == [
        "Agent Engineer",
        "ML Platform Engineer",
        "AI Product Engineer",
        "Backend Engineer",
    ]
    assert [job.source for job in jobs] == [
        "example-rss",
        "greenhouse:dataforge",
        "ashby:brainco",
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


def test_source_filters_apply_before_limit(tmp_path):
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "title": "Account Executive",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        "location": {"name": "New York, NY"},
                        "content": "Sell software.",
                    },
                    {
                        "title": "Senior Finance Manager",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                        "location": {"name": "London, UK"},
                        "content": "Manage finance.",
                    },
                    {
                        "title": "2026 Early Career Software Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
                        "location": {"name": "Seattle, WA"},
                        "content": "Build backend systems.",
                    },
                    {
                        "title": "Machine Learning Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/4",
                        "location": {"name": "San Francisco, CA"},
                        "content": "Build ML systems.",
                    },
                ]
            }
        )
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "type": "greenhouse",
                        "board_token": "acme",
                        "payload_file": str(greenhouse_path),
                        "title_include": ["software engineer", "machine learning"],
                        "location_include": ["seattle", "san francisco"],
                        "limit": 1,
                    }
                ]
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.title for job in jobs] == ["2026 Early Career Software Engineer"]


def test_source_filter_drops_non_direct_application_urls():
    jobs = [
        Job(
            title="Machine Learning Engineer",
            company="Example",
            raw_jd="",
            location="Remote",
            apply_url="https://job-boards.greenhouse.io/coinbase/jobs/123",
        ),
        Job(
            title="Software Engineer",
            company="Example",
            raw_jd="",
            location="Remote",
            apply_url="https://boards.greenhouse.io/coinbase/jobs/456",
        ),
        Job(
            title="ML Engineer",
            company="Example",
            raw_jd="",
            location="Remote",
            apply_url="https://boards.greenhouse.io/acme/jobs/789",
        ),
    ]

    assert [job.title for job in _filter_jobs_for_source(jobs, {})] == [
        "ML Engineer",
    ]


def test_source_config_defaults_are_merged_into_sources(tmp_path):
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "title": "Account Executive",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        "location": {"name": "New York, NY"},
                        "content": "Sell software.",
                    },
                    {
                        "title": "Machine Learning Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                        "location": {"name": "San Francisco, CA"},
                        "content": "Build ML systems.",
                    },
                ]
            }
        )
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {"title_include": ["machine learning"]},
                "sources": [
                    {
                        "type": "greenhouse",
                        "board_token": "acme",
                        "payload_file": str(greenhouse_path),
                    }
                ],
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.title for job in jobs] == ["Machine Learning Engineer"]


def test_us_only_source_filter_rejects_explicit_non_us_remote_jobs(
    tmp_path,
):
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/us",
                        "location": {"name": "Remote - United States"},
                        "content": "Build distributed Python services.",
                    },
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/india",
                        "location": {"name": "Remote - India"},
                        "content": "Build distributed Python services.",
                    },
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/worldwide",
                        "location": {"name": "Remote - Worldwide"},
                        "content": "Build distributed Python services.",
                    },
                ]
            }
        )
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {
                    "title_include": ["software engineer"],
                    "location_include": ["remote"],
                    "us_only": True,
                },
                "sources": [
                    {
                        "type": "greenhouse",
                        "board_token": "acme",
                        "payload_file": str(greenhouse_path),
                    }
                ],
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.apply_url for job in jobs] == [
        "https://jobs.example.com/us",
        "https://jobs.example.com/worldwide",
    ]


def test_us_only_source_filter_works_without_keyword_filters(tmp_path):
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/us",
                        "location": {"name": "New York, NY"},
                        "content": "Build distributed Python services.",
                    },
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/canada",
                        "location": {"name": "Remote - Canada"},
                        "content": "Build distributed Python services.",
                    },
                ]
            }
        )
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {"us_only": True},
                "sources": [
                    {
                        "type": "greenhouse",
                        "board_token": "acme",
                        "payload_file": str(greenhouse_path),
                    }
                ],
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.apply_url for job in jobs] == ["https://jobs.example.com/us"]


def test_us_only_keeps_generic_us_locations_without_source_location_include(tmp_path):
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/hybrid",
                        "location": {"name": "Hybrid"},
                        "content": "Build distributed Python services.",
                    },
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/in-office",
                        "location": {"name": "In-Office"},
                        "content": "Build distributed Python services.",
                    },
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/india",
                        "location": {"name": "Remote - India"},
                        "content": "Build distributed Python services.",
                    },
                ]
            }
        )
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {
                    "title_include": ["software engineer"],
                    "location_include": ["new york", "remote"],
                    "us_only": True,
                },
                "sources": [
                    {
                        "type": "greenhouse",
                        "board_token": "acme",
                        "payload_file": str(greenhouse_path),
                    }
                ],
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.apply_url for job in jobs] == [
        "https://jobs.example.com/hybrid",
        "https://jobs.example.com/in-office",
    ]


def test_us_only_source_location_include_does_not_drop_other_us_roles(tmp_path):
    greenhouse_path = tmp_path / "greenhouse.json"
    greenhouse_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/austin",
                        "location": {"name": "Austin, TX"},
                        "content": "Build distributed Python services.",
                    },
                    {
                        "title": "Software Engineer",
                        "absolute_url": "https://jobs.example.com/nyc",
                        "location": {"name": "New York, NY"},
                        "content": "Build distributed Python services.",
                    },
                ]
            }
        )
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {
                    "title_include": ["software engineer"],
                    "location_include": ["new york", "remote"],
                    "us_only": True,
                },
                "sources": [
                    {
                        "type": "greenhouse",
                        "board_token": "acme",
                        "payload_file": str(greenhouse_path),
                        "location_include": ["new york"],
                    }
                ],
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.apply_url for job in jobs] == [
        "https://jobs.example.com/austin",
        "https://jobs.example.com/nyc",
    ]


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


def test_source_failure_is_isolated_and_other_sources_still_import(
    tmp_path,
    monkeypatch,
):
    import job_agent.source_config as source_config

    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {"type": "greenhouse", "board_token": "unavailable"},
                    {"type": "greenhouse", "board_token": "healthy"},
                ]
            }
        )
    )

    def fake_read_json(_base_dir, item, _default_url):
        if item["board_token"] == "unavailable":
            raise URLError("timed out")
        return {
            "jobs": [
                {
                    "title": "Software Engineer I",
                    "absolute_url": "https://boards.greenhouse.io/healthy/jobs/1",
                    "location": {"name": "New York, NY"},
                    "content": "Build software.",
                }
            ]
        }

    monkeypatch.setattr(source_config, "_read_json", fake_read_json)

    with pytest.warns(RuntimeWarning, match="Skipping greenhouse source unavailable"):
        jobs = load_jobs_from_source_config(config_path)

    assert [job.title for job in jobs] == ["Software Engineer I"]


def test_aggregator_links_without_direct_application_forms_are_excluded(tmp_path):
    rss_path = tmp_path / "jobs.xml"
    rss_path.write_text(
        """<rss><channel><item>
        <title>Acme (YC S20) is hiring a software engineer</title>
        <link>https://www.ycombinator.com/companies/acme/jobs/1</link>
        <description>Build agents.</description>
        </item><item>
        <title>Acme at OpenBoard</title>
        <link>https://boards.example.com/acme/jobs/2</link>
        <description>Build agents.</description>
        </item></channel></rss>"""
    )
    config_path = tmp_path / "sources.json"
    config_path.write_text(
        json.dumps(
            {
                "sources": [
                    {"type": "rss", "source": "example-rss", "rss_file": str(rss_path)}
                ]
            }
        )
    )

    jobs = load_jobs_from_source_config(config_path)

    assert [job.apply_url for job in jobs] == ["https://boards.example.com/acme/jobs/2"]
