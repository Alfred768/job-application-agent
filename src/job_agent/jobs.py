from __future__ import annotations

import re
from dataclasses import asdict
from html import unescape
from typing import Any
from xml.etree import ElementTree

from job_agent.models import Job


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
