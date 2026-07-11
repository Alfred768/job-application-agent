"""Rich application profile - mirrors how Simplify builds a profile once and
reuses it to autofill every application.

A Simplify-style profile is not a flat contact card: it carries structured
work history, education, links, demographics/EEO answers, and a screening
answer bank. This module also parses an existing resume's text into that
structured profile (Simplify imports your resume at sign-up), so the form
filler can populate multi-entry work/education sections.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WorkEntry:
    title: str = ""
    company: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    current: bool = False
    description: str = ""


@dataclass
class EducationEntry:
    school: str = ""
    degree: str = ""
    field: str = ""
    start_date: str = ""
    end_date: str = ""
    gpa: str = ""


@dataclass
class RichProfile:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    website: str = ""
    cover_letter: str = ""
    skills: list[str] = field(default_factory=list)
    work_history: list[WorkEntry] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)
    demographics: dict[str, str] = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)
    sensitive_answers: dict[str, Any] = field(default_factory=dict)
    resume_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _split_sections(text: str) -> dict[str, list[str]]:
    """Split resume text into sections keyed by common headings."""
    section_keys = {
        "summary": "summary",
        "objective": "summary",
        "skills": "skills",
        "technical skills": "skills",
        "experience": "experience",
        "work experience": "experience",
        "employment": "experience",
        "education": "education",
        "projects": "projects",
    }
    sections: dict[str, list[str]] = {}
    current = "header"
    sections[current] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = section_keys.get(line.lower())
        if key:
            current = key
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _parse_contact(header_lines: list[str]) -> dict[str, str]:
    contact = {"name": "", "email": "", "phone": "", "location": "", "linkedin": "", "github": ""}
    if not header_lines:
        return contact
    contact["name"] = header_lines[0].strip()
    if len(header_lines) > 1:
        joined = " ".join(header_lines[1:3])
        email = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", joined)
        if email:
            contact["email"] = email.group(0)
        phone = re.search(r"\+?\d[\d \-()]{7,}\d", joined)
        if phone:
            contact["phone"] = phone.group(0).strip()
        # split on | or bullet only, so "City, ST" stays intact
        for token in re.split(r"[|•]", joined):
            t = token.strip()
            if not t:
                continue
            tl = t.lower()
            if "linkedin" in tl:
                contact["linkedin"] = t
            elif "github" in tl:
                contact["github"] = t
            elif re.search(r"[A-Z][a-z]+,\s*[A-Z]{2}", t) and not contact["location"]:
                contact["location"] = t
    return contact


def _parse_title_company(line: str) -> tuple[str, str]:
    for sep in [" — ", " – ", " at ", " - ", " | "]:
        if sep in line:
            role, company = line.split(sep, 1)
            return role.strip(), company.strip()
    return line.strip(), ""


def _parse_work_experience(lines: list[str]) -> list[WorkEntry]:
    entries: list[WorkEntry] = []
    current: WorkEntry | None = None
    for line in lines:
        has_sep = any(sep in line for sep in [" — ", " – ", " at ", " - ", " | "])
        looks_sentence = line.endswith(".") or line.startswith(("-", "•", "*"))
        # a description line belongs to the current entry (no separator, looks
        # like a sentence/bullet or is long)
        if current and not has_sep and (looks_sentence or len(line) > 60):
            current.description = (current.description + "\n" + line).strip() if current.description else line
            continue
        title, company = _parse_title_company(line)
        current = WorkEntry(title=title, company=company)
        entries.append(current)
    return entries


def _parse_education(lines: list[str]) -> list[EducationEntry]:
    entries: list[EducationEntry] = []
    for line in lines:
        degree, school = _parse_title_company(line)
        if degree or school:
            entries.append(EducationEntry(degree=degree, school=school))
    return entries


def parse_resume_to_profile(resume_text: str) -> RichProfile:
    """Heuristically parse resume text into a structured RichProfile."""
    sections = _split_sections(resume_text)
    contact = _parse_contact(sections.get("header", []))

    skills: list[str] = []
    for line in sections.get("skills", []):
        skills.extend(s.strip() for s in re.split(r"[,;•]", line) if s.strip())

    work = _parse_work_experience(sections.get("experience", []))
    edu = _parse_education(sections.get("education", []))

    return RichProfile(
        name=contact["name"],
        email=contact["email"],
        phone=contact["phone"],
        location=contact["location"],
        linkedin=contact["linkedin"],
        github=contact["github"],
        skills=skills,
        work_history=work,
        education=edu,
    )


def render_profile_template() -> dict[str, Any]:
    """A fill-in rich profile template (Simplify-style)."""
    return {
        "name": "",
        "email": "",
        "phone": "",
        "location": "",
        "linkedin": "",
        "github": "",
        "portfolio": "",
        "website": "",
        "cover_letter": "",
        "skills": [],
        "work_history": [
            {"title": "", "company": "", "location": "", "start_date": "", "end_date": "", "current": False, "description": ""}
        ],
        "education": [
            {"school": "", "degree": "", "field": "", "start_date": "", "end_date": "", "gpa": ""}
        ],
        "demographics": {"gender": "Prefer not to say", "race": "Prefer not to say", "disability": "Prefer not to say", "veteran": "Prefer not to say"},
        "answers": {},
        "sensitive_answers": {},
    }


def load_profile(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
