from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from job_agent.jobs import (
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


def load_jobs_from_source_config(config_path: Path | str) -> list[Job]:
    path = Path(config_path)
    config = json.loads(path.read_text())
    base_dir = path.parent
    jobs: list[Job] = []

    for item in config.get("sources", []):
        source_type = str(item.get("type", "")).lower()
        limit = item.get("limit")
        if source_type == "rss":
            rss_xml = _read_text(base_dir, "rss_file", item, "rss_url")
            jobs.extend(
                parse_rss_jobs(
                    rss_xml,
                    source=item.get("source") or item.get("rss_url") or "rss",
                    limit=limit,
                )
            )
        elif source_type == "greenhouse":
            board_token = item.get("board_token")
            if not board_token:
                raise ValueError("Greenhouse source requires board_token.")
            payload = _read_json(
                base_dir,
                item,
                f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true",
            )
            jobs.extend(parse_greenhouse_jobs(payload, board_token=board_token, limit=limit))
        elif source_type == "lever":
            site = item.get("site")
            if not site:
                raise ValueError("Lever source requires site.")
            payload = _read_json(
                base_dir,
                item,
                f"https://api.lever.co/v0/postings/{site}?mode=json",
            )
            jobs.extend(parse_lever_jobs(payload, site=site, limit=limit))
        elif source_type == "remotive":
            payload = _read_json(base_dir, item, _remotive_url(item))
            jobs.extend(parse_remotive_jobs(payload, limit=limit))
        else:
            raise ValueError(f"Unsupported source type: {source_type}")

    return deduplicate_jobs(jobs)
