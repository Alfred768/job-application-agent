from __future__ import annotations

import re
from dataclasses import asdict, replace
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from job_agent.models import Job


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "ref",
    "referrer",
    "source",
}


def _field_from_text(text: str, label: str) -> str | None:
    prefix = f"{label}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix) :].strip() or None
    return None


def import_job_from_text(text: str) -> Job:
    return Job(
        title=_field_from_text(text, "Title") or "Unknown Role",
        company=_field_from_text(text, "Company") or "Unknown Company",
        location=_field_from_text(text, "Location"),
        raw_jd=text,
        source="manual",
    )


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ElementTree.Element, *names: str) -> str | None:
    wanted = {name.lower() for name in names}
    for child in list(element):
        if _strip_namespace(child.tag) in wanted and child.text:
            return child.text.strip()
    return None


def _clean_text(text: str | None) -> str:
    if not text:
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(without_tags)).strip()


def _split_title_and_company(raw_title: str) -> tuple[str, str]:
    title = _clean_text(raw_title)
    if " at " in title:
        role, company = title.rsplit(" at ", 1)
        return role.strip() or "Unknown Role", company.strip() or "Unknown Company"
    if " - " in title:
        role, company = title.rsplit(" - ", 1)
        return role.strip() or "Unknown Role", company.strip() or "Unknown Company"
    return title or "Unknown Role", "Unknown Company"


def parse_rss_jobs(rss_xml: str, source: str = "rss", limit: int | None = None) -> list[Job]:
    root = ElementTree.fromstring(rss_xml)
    items = [element for element in root.iter() if _strip_namespace(element.tag) in {"item", "entry"}]
    jobs: list[Job] = []

    for item in items:
        if limit is not None and len(jobs) >= limit:
            break
        raw_title = _child_text(item, "title") or "Unknown Role"
        title, company = _split_title_and_company(raw_title)
        link = _child_text(item, "link", "id")
        if link is None:
            for child in list(item):
                if _strip_namespace(child.tag) == "link":
                    link = child.attrib.get("href")
                    if link:
                        break
        description = _clean_text(_child_text(item, "description", "summary", "content"))
        location = _clean_text(_child_text(item, "category", "location")) or None
        jobs.append(
            Job(
                title=title,
                company=company,
                location=location,
                raw_jd=description or raw_title,
                source=source,
                source_url=link,
                apply_url=link,
            )
        )

    return jobs


def parse_greenhouse_jobs(payload: dict[str, Any], board_token: str, limit: int | None = None) -> list[Job]:
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        if limit is not None and len(jobs) >= limit:
            break
        location = item.get("location") or {}
        location_name = location.get("name") if isinstance(location, dict) else None
        url = item.get("absolute_url")
        jobs.append(
            Job(
                title=item.get("title") or "Unknown Role",
                company=board_token,
                location=location_name,
                raw_jd=_clean_text(item.get("content")) or item.get("title") or "",
                source=f"greenhouse:{board_token}",
                source_url=url,
                apply_url=url,
            )
        )
    return jobs


def parse_lever_jobs(payload: list[dict[str, Any]], site: str, limit: int | None = None) -> list[Job]:
    jobs: list[Job] = []
    for item in payload:
        if limit is not None and len(jobs) >= limit:
            break
        categories = item.get("categories") or {}
        location = categories.get("location") if isinstance(categories, dict) else None
        url = item.get("hostedUrl") or item.get("applyUrl")
        description = item.get("descriptionPlain") or item.get("description") or item.get("additionalPlain")
        jobs.append(
            Job(
                title=item.get("text") or item.get("title") or "Unknown Role",
                company=site,
                location=location,
                raw_jd=_clean_text(description) or item.get("text") or "",
                source=f"lever:{site}",
                source_url=url,
                apply_url=url,
                remote_policy=item.get("workplaceType"),
            )
        )
    return jobs


def parse_remotive_jobs(payload: dict[str, Any], limit: int | None = None) -> list[Job]:
    jobs: list[Job] = []
    for item in payload.get("jobs", []):
        if limit is not None and len(jobs) >= limit:
            break
        url = item.get("url")
        jobs.append(
            Job(
                title=item.get("title") or "Unknown Role",
                company=item.get("company_name") or "Unknown Company",
                location=item.get("candidate_required_location"),
                raw_jd=_clean_text(item.get("description")) or item.get("title") or "",
                source="remotive",
                source_url=url,
                apply_url=url,
                remote_policy="remote",
            )
        )
    return jobs


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _canonical_job_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value.strip())
    if not parts.netloc:
        return value.strip().rstrip("/")
    query = urlencode(
        sorted(
            (key, item_value)
            for key, item_value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in _TRACKING_QUERY_KEYS
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, query, ""))


def _job_identity(job: Job) -> tuple[str, ...]:
    canonical_url = _canonical_job_url(job.apply_url or job.source_url)
    if canonical_url:
        return ("url", canonical_url)
    return (
        "role",
        _normalized_text(job.company),
        _normalized_text(job.title),
        _normalized_text(job.location),
    )


def _merge_sources(first: str, second: str) -> str:
    sources = []
    for source in [*first.split(" | "), *second.split(" | ")]:
        if source and source not in sources:
            sources.append(source)
    return " | ".join(sources)


def _merge_duplicate_jobs(first: Job, second: Job) -> Job:
    richer_jd = second.raw_jd if len(second.raw_jd.strip()) > len(first.raw_jd.strip()) else first.raw_jd
    return replace(
        first,
        raw_jd=richer_jd,
        location=first.location or second.location,
        source=_merge_sources(first.source, second.source),
        source_url=first.source_url or second.source_url,
        apply_url=first.apply_url or second.apply_url,
        remote_policy=first.remote_policy or second.remote_policy,
    )


def deduplicate_jobs(jobs: list[Job]) -> list[Job]:
    """Collapse jobs found through multiple public sources while preserving order."""
    unique: list[Job] = []
    positions: dict[tuple[str, ...], int] = {}
    for job in jobs:
        identity = _job_identity(job)
        position = positions.get(identity)
        if position is None:
            positions[identity] = len(unique)
            unique.append(job)
            continue
        unique[position] = _merge_duplicate_jobs(unique[position], job)
    return unique


def jobs_to_dicts(jobs: list[Job]) -> list[dict[str, str | None]]:
    return [asdict(job) for job in jobs]


def format_job_as_jd_text(job: Job) -> str:
    fields = [
        f"Company: {job.company}",
        f"Title: {job.title}",
    ]
    if job.location:
        fields.append(f"Location: {job.location}")
    fields.append(f"Source: {job.source}")
    if job.source_url:
        fields.append(f"Source URL: {job.source_url}")
    if job.apply_url:
        fields.append(f"Apply URL: {job.apply_url}")
    fields.append("")
    fields.append(job.raw_jd)
    return "\n".join(fields).strip()
