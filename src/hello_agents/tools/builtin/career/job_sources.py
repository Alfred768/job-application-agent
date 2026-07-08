"""Compliant job intake tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import urlopen

from job_agent.jobs import import_job_from_text, jobs_to_dicts, parse_rss_jobs
from hello_agents.tools.base import Tool, ToolParameter


class ManualJDImportTool(Tool):
    """Import user-provided JD text into a normalized job posting."""

    def __init__(self):
        super().__init__(
            name="manual_jd_import",
            description="Import user-provided job description text into a normalized job object.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        text = parameters.get("input") or parameters.get("text") or ""
        job = import_job_from_text(text)
        return (
            f"company={job.company}\n"
            f"title={job.title}\n"
            f"location={job.location or 'Unknown'}\n"
            f"source={job.source}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Raw JD text pasted or provided by the user.",
            )
        ]


class RSSJobSourceTool(Tool):
    """Import postings from a public RSS or Atom feed into normalized job objects."""

    def __init__(self):
        super().__init__(
            name="rss_job_source",
            description="Import public RSS/Atom job feed XML into normalized job objects.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        rss_xml = parameters.get("rss_xml") or parameters.get("input")
        rss_url = parameters.get("rss_url")
        source = parameters.get("source") or rss_url or "rss"
        limit = parameters.get("limit")
        if isinstance(limit, str) and limit.strip():
            limit = int(limit)
        elif not isinstance(limit, int):
            limit = None

        if not rss_xml and rss_url:
            with urlopen(rss_url, timeout=20) as response:
                rss_xml = response.read().decode("utf-8")
        if not rss_xml:
            raise ValueError("RSSJobSourceTool requires rss_xml/input or rss_url.")

        jobs = parse_rss_jobs(str(rss_xml), source=str(source), limit=limit)
        return json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="rss_xml",
                type="string",
                description="Raw RSS or Atom XML from a public job feed.",
                required=False,
            ),
            ToolParameter(
                name="rss_url",
                type="string",
                description="Optional public RSS or Atom URL to fetch.",
                required=False,
            ),
            ToolParameter(
                name="source",
                type="string",
                description="Human-readable source label for provenance.",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Optional maximum number of jobs to import.",
                required=False,
            ),
        ]
