from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from job_agent.jobs import (
    parse_ashby_jobs,
    deduplicate_jobs,
    parse_greenhouse_jobs,
    parse_lever_jobs,
    parse_remotive_jobs,
    parse_rss_jobs,
)
from job_agent.models import Job

# Public job APIs (e.g. Remotive) reject the default Python-urllib User-Agent.
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _read_url(url: str, timeout: int = 20):
    return urlopen(Request(url, headers={"User-Agent": _HTTP_USER_AGENT}), timeout=timeout)


def _resolve_path(base_dir: Path, value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


def _read_text(base_dir: Path, file_key: str, item: dict[str, Any], url_key: str) -> str:
    file_path = _resolve_path(base_dir, item.get(file_key))
    if file_path:
        return file_path.read_text()
    url = item.get(url_key)
    if not url:
        raise ValueError(f"Source item requires {file_key} or {url_key}.")
    with _read_url(url) as response:
        return response.read().decode("utf-8")


def _read_json(base_dir: Path, item: dict[str, Any], default_url: str) -> Any:
    payload_file = _resolve_path(base_dir, item.get("payload_file"))
    if payload_file:
        return json.loads(payload_file.read_text())
    with _read_url(default_url) as response:
        return json.loads(response.read().decode("utf-8"))


def _remotive_url(item: dict[str, Any]) -> str:
    query = {
        key: item[key]
        for key in ("search", "category", "company_name", "limit")
        if item.get(key) is not None
    }
    suffix = f"?{urlencode(query)}" if query else ""
    return f"https://remotive.com/api/remote-jobs{suffix}"


def _as_patterns(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    return [str(pattern).strip().lower() for pattern in values if str(pattern).strip()]


def _matches_any(text: str, patterns: list[str]) -> bool:
    haystack = text.lower()
    return any(pattern in haystack for pattern in patterns)


def _job_filter_text(job: Job, scope: str) -> str:
    if scope == "title":
        return job.title or ""
    if scope == "location":
        return " ".join(part for part in [job.location, job.remote_policy] if part)
    return " ".join(
        part
        for part in [
            job.title,
            job.company,
            job.location,
            job.remote_policy,
            job.raw_jd,
        ]
        if part
    )


def _filter_jobs_for_source(jobs: list[Job], item: dict[str, Any]) -> list[Job]:
    """Apply optional source-level filters before per-source limiting.

    Large ATS boards are often sorted by department or region, so applying
    ``limit`` during parsing can discard the relevant early-career/AI roles
    before the scorer ever sees them. These filters let a source config narrow
    a board by title, location, or full text first, then cap the useful subset.
    """
    include_specs = [
        ("title", _as_patterns(item.get("title_include"))),
        ("location", _as_patterns(item.get("location_include"))),
        ("text", _as_patterns(item.get("text_include") or item.get("include_keywords"))),
    ]
    exclude_specs = [
        ("title", _as_patterns(item.get("title_exclude"))),
        ("location", _as_patterns(item.get("location_exclude"))),
        ("text", _as_patterns(item.get("text_exclude") or item.get("exclude_keywords"))),
    ]
    if not any(patterns for _, patterns in include_specs + exclude_specs):
        return jobs

    filtered: list[Job] = []
    for job in jobs:
        if any(
            patterns and not _matches_any(_job_filter_text(job, scope), patterns)
            for scope, patterns in include_specs
        ):
            continue
        if any(
            patterns and _matches_any(_job_filter_text(job, scope), patterns)
            for scope, patterns in exclude_specs
        ):
            continue
        filtered.append(job)
    return filtered


def _source_has_filters(item: dict[str, Any]) -> bool:
    return any(
        item.get(key)
        for key in (
            "title_include",
            "title_exclude",
            "location_include",
            "location_exclude",
            "text_include",
            "text_exclude",
            "include_keywords",
            "exclude_keywords",
        )
    )


def _limit_jobs(jobs: list[Job], limit: Any) -> list[Job]:
    if limit is None:
        return jobs
    try:
        parsed_limit = int(limit)
    except (TypeError, ValueError):
        return jobs
    if parsed_limit < 0:
        return jobs
    return jobs[:parsed_limit]


def load_jobs_from_source_config(config_path: Path | str) -> list[Job]:
    path = Path(config_path)
    config = json.loads(path.read_text())
    base_dir = path.parent
    jobs: list[Job] = []
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}

    for raw_item in config.get("sources", []):
        item = {**defaults, **raw_item}
        source_type = str(item.get("type", "")).lower()
        limit = item.get("limit")
        parse_limit = None if _source_has_filters(item) else limit
        source_jobs: list[Job]
        if source_type not in {"rss", "greenhouse", "lever", "ashby", "remotive"}:
            raise ValueError(f"Unsupported source type: {source_type}")
        if source_type == "rss" and not (item.get("rss_file") or item.get("rss_url")):
            raise ValueError("Source item requires rss_file or rss_url.")
        if source_type == "greenhouse" and not item.get("board_token"):
            raise ValueError("Greenhouse source requires board_token.")
        if source_type == "lever" and not item.get("site"):
            raise ValueError("Lever source requires site.")
        if source_type == "ashby" and not (
            item.get("organization") or item.get("org") or item.get("board")
        ):
            raise ValueError("Ashby source requires organization.")

        local_payload = bool(
            item.get("rss_file")
            if source_type == "rss"
            else item.get("payload_file")
        )
        source_id = str(
            item.get("board_token")
            or item.get("site")
            or item.get("organization")
            or item.get("org")
            or item.get("board")
            or item.get("source")
            or item.get("rss_url")
            or source_type
        )
        try:
            if source_type == "rss":
                rss_xml = _read_text(base_dir, "rss_file", item, "rss_url")
                source_jobs = parse_rss_jobs(
                    rss_xml,
                    source=item.get("source") or item.get("rss_url") or "rss",
                    limit=parse_limit,
                )
            elif source_type == "greenhouse":
                board_token = item["board_token"]
                payload = _read_json(
                    base_dir,
                    item,
                    f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true",
                )
                source_jobs = parse_greenhouse_jobs(
                    payload, board_token=board_token, limit=parse_limit
                )
            elif source_type == "lever":
                site = item["site"]
                payload = _read_json(
                    base_dir,
                    item,
                    f"https://api.lever.co/v0/postings/{site}?mode=json",
                )
                source_jobs = parse_lever_jobs(
                    payload, site=site, limit=parse_limit
                )
            elif source_type == "ashby":
                organization = (
                    item.get("organization") or item.get("org") or item.get("board")
                )
                payload = _read_json(
                    base_dir,
                    item,
                    f"https://api.ashbyhq.com/posting-api/job-board/{organization}",
                )
                source_jobs = parse_ashby_jobs(
                    payload,
                    organization=str(organization),
                    limit=parse_limit,
                )
            else:
                payload = _read_json(base_dir, item, _remotive_url(item))
                source_jobs = parse_remotive_jobs(payload, limit=parse_limit)
        except Exception as exc:
            if local_payload:
                raise
            warnings.warn(
                f"Skipping {source_type} source {source_id}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        jobs.extend(_limit_jobs(_filter_jobs_for_source(source_jobs, item), limit))

    return deduplicate_jobs(jobs)
