"""Compliant job intake tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from job_agent.jobs import (
    import_job_from_text,
    jobs_to_dicts,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_remotive_jobs,
    parse_rss_jobs,
)
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


def _coerce_limit(limit: Any) -> int | None:
    if isinstance(limit, str) and limit.strip():
        return int(limit)
    if isinstance(limit, int):
        return limit
    return None


def _read_json_url(url: str) -> Any:
    with urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


class GreenhouseJobSourceTool(Tool):
    """Import postings from a public Greenhouse Job Board API response."""

    def __init__(self):
        super().__init__(
            name="greenhouse_job_source",
            description="Import public Greenhouse Job Board API jobs into normalized job objects.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        board_token = str(parameters.get("board_token") or "").strip()
        if not board_token:
            raise ValueError("GreenhouseJobSourceTool requires board_token.")
        limit = _coerce_limit(parameters.get("limit"))
        payload_json = parameters.get("payload_json")
        if payload_json:
            payload = json.loads(str(payload_json))
        else:
            url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
            payload = _read_json_url(url)
        jobs = parse_greenhouse_jobs(payload, board_token=board_token, limit=limit)
        return json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="board_token", type="string", description="Greenhouse public board token."),
            ToolParameter(
                name="payload_json",
                type="string",
                description="Optional raw Greenhouse JSON payload for offline import/testing.",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Optional maximum number of jobs to import.",
                required=False,
            ),
        ]


class LeverJobSourceTool(Tool):
    """Import postings from a public Lever Postings API response."""

    def __init__(self):
        super().__init__(
            name="lever_job_source",
            description="Import public Lever Postings API jobs into normalized job objects.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        site = str(parameters.get("site") or "").strip()
        if not site:
            raise ValueError("LeverJobSourceTool requires site.")
        limit = _coerce_limit(parameters.get("limit"))
        payload_json = parameters.get("payload_json")
        if payload_json:
            payload = json.loads(str(payload_json))
        else:
            url = f"https://api.lever.co/v0/postings/{site}?mode=json"
            payload = _read_json_url(url)
        jobs = parse_lever_jobs(payload, site=site, limit=limit)
        return json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="site", type="string", description="Lever public postings site slug."),
            ToolParameter(
                name="payload_json",
                type="string",
                description="Optional raw Lever JSON payload for offline import/testing.",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Optional maximum number of jobs to import.",
                required=False,
            ),
        ]


class RemotiveJobSourceTool(Tool):
    """Import postings from the public Remotive Remote Jobs API response."""

    def __init__(self):
        super().__init__(
            name="remotive_job_source",
            description="Import public Remotive Remote Jobs API jobs into normalized job objects.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        limit = _coerce_limit(parameters.get("limit"))
        payload_json = parameters.get("payload_json")
        if payload_json:
            payload = json.loads(str(payload_json))
        else:
            query: dict[str, Any] = {}
            for key in ("category", "company_name", "search"):
                if parameters.get(key):
                    query[key] = parameters[key]
            if limit is not None:
                query["limit"] = limit
            suffix = f"?{urlencode(query)}" if query else ""
            payload = _read_json_url(f"https://remotive.com/api/remote-jobs{suffix}")
        jobs = parse_remotive_jobs(payload, limit=limit)
        return json.dumps(jobs_to_dicts(jobs), indent=2, ensure_ascii=True)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="payload_json",
                type="string",
                description="Optional raw Remotive JSON payload for offline import/testing.",
                required=False,
            ),
            ToolParameter(
                name="search",
                type="string",
                description="Optional Remotive search query.",
                required=False,
            ),
            ToolParameter(
                name="category",
                type="string",
                description="Optional Remotive category or slug.",
                required=False,
            ),
            ToolParameter(
                name="company_name",
                type="string",
                description="Optional company-name filter.",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="Optional maximum number of jobs to import.",
                required=False,
            ),
        ]
