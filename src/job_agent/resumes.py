from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from job_agent.models import ResumeTemplate


TRACK_BY_TOKEN = {
    "Agent_Engineer": "Agent Engineer",
    "SDE": "SDE",
    "MLE": "MLE",
    "ML_Infra": "ML Infra",
    "AI_Algorithm_Engineer": "AI Algorithm Engineer",
    "Data_Scientist": "Data Scientist",
    "Unity_ML_Infrastructure": "Unity ML Infrastructure",
}


def infer_track_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    for token, track in TRACK_BY_TOKEN.items():
        if token in stem:
            return track
    return "Other"


def _normalize_text(parts: list[str]) -> str:
    return "\n".join(part.strip() for part in parts if part and part.strip()).strip()


def _extract_docx_text(path: Path) -> str | None:
    try:
        with ZipFile(path) as docx:
            document_xml = docx.read("word/document.xml")
    except (BadZipFile, KeyError, OSError):
        return None

    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError:
        return None

    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in root.findall(".//w:p", namespace):
        texts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
        if texts:
            paragraphs.append("".join(texts))
    return _normalize_text(paragraphs) or None


def _extract_pdf_text(path: Path) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None

    try:
        reader = PdfReader(str(path))
        return _normalize_text([page.extract_text() or "" for page in reader.pages]) or None
    except Exception:
        return None


def extract_resume_text(path: str | Path) -> str | None:
    resume_path = Path(path)
    suffix = resume_path.suffix.lower()
    if suffix == ".docx":
        return _extract_docx_text(resume_path)
    if suffix == ".pdf":
        return _extract_pdf_text(resume_path)
    return None


def index_resume_templates(source_dir: str | Path) -> list[ResumeTemplate]:
    source = Path(source_dir).expanduser()
    grouped: dict[str, dict[str, Path]] = {}

    for path in sorted(source.glob("GAOYI_WU_*")):
        if path.suffix.lower() not in {".docx", ".pdf"}:
            continue
        grouped.setdefault(path.stem, {})[path.suffix.lower()] = path

    templates = []
    for stem, paths in grouped.items():
        parsed_text = None
        if paths.get(".docx"):
            parsed_text = extract_resume_text(paths[".docx"])
        if parsed_text is None and paths.get(".pdf"):
            parsed_text = extract_resume_text(paths[".pdf"])
        templates.append(
            ResumeTemplate(
                track=infer_track_from_filename(stem),
                docx_path=paths.get(".docx"),
                pdf_path=paths.get(".pdf"),
                parsed_text=parsed_text,
            )
        )
    return templates
