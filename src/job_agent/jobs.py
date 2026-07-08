from __future__ import annotations

import re
from dataclasses import asdict
from html import unescape
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
