from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from job_agent.models import ResumeTemplate
from job_agent.scoring import ROLE_KEYWORDS


class ResumePathError(ValueError):
    """Raised when an ATS upload resume path is not an approved source PDF."""


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
    stem = Path(filename).stem.lower()
    for token, track in TRACK_BY_TOKEN.items():
        if token.lower() in stem:
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
        try:
            return path.read_bytes().decode("utf-8", errors="ignore") or None
        except OSError:
            return None


def extract_resume_text(path: str | Path) -> str | None:
    resume_path = Path(path)
    suffix = resume_path.suffix.lower()
    if suffix == ".docx":
        return _extract_docx_text(resume_path)
    if suffix == ".pdf":
        return _extract_pdf_text(resume_path)
    return None


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_original_resume_pdf(
    resume_path: str | Path,
    *,
    source_dir: str | Path | None = None,
    package_dir: str | Path | None = None,
    required_pdf: str | Path | None = None,
) -> Path:
    """Resolve and validate a resume path that may be uploaded to an ATS.

    The uploadable resume must be an existing PDF. When a source directory is
    supplied, the resolved PDF must live inside that directory. When a package
    directory is supplied, package-local generated PDFs are rejected.
    """

    candidate = Path(resume_path).expanduser()
    if candidate.suffix.lower() != ".pdf":
        raise ResumePathError(f"resume upload must be an existing PDF: {candidate}")
    if not candidate.is_file():
        raise ResumePathError(f"resume upload PDF does not exist: {candidate}")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ResumePathError(f"resume upload PDF cannot be resolved: {candidate}") from exc

    if required_pdf is not None:
        required = Path(required_pdf).expanduser()
        if required.suffix.lower() != ".pdf" or not required.is_file():
            raise ResumePathError(f"required resume PDF is not an existing PDF: {required}")
        try:
            required_resolved = required.resolve()
        except OSError as exc:
            raise ResumePathError(f"required resume PDF cannot be resolved: {required}") from exc
        if resolved != required_resolved:
            raise ResumePathError(
                f"resume upload PDF does not match required path: {candidate}; expected: {required}"
            )

    if source_dir is not None:
        source = Path(source_dir).expanduser()
        if not source.is_dir():
            raise ResumePathError(f"required resume source dir does not exist: {source}")
        try:
            source_resolved = source.resolve()
        except OSError as exc:
            raise ResumePathError(f"required resume source dir cannot be resolved: {source}") from exc
        if not _is_relative_to(resolved, source_resolved):
            raise ResumePathError(
                "resume upload PDF must come from required resume source dir: "
                f"{candidate}; expected under: {source}"
            )

    if package_dir is not None:
        package = Path(package_dir).expanduser()
        try:
            package_resolved = package.resolve()
        except OSError as exc:
            raise ResumePathError(f"application package dir cannot be resolved: {package}") from exc
        if _is_relative_to(resolved, package_resolved):
            raise ResumePathError(
                f"resume upload PDF must be an original external path, not package-local: {candidate}"
            )

    return resolved


def index_resume_templates(source_dir: str | Path) -> list[ResumeTemplate]:
    source = Path(source_dir).expanduser()
    if not source.is_dir():
        raise FileNotFoundError(f"resume source directory does not exist: {source}")
    grouped: dict[str, dict[str, Path]] = {}

    for path in sorted(source.glob("*.pdf")):
        try:
            path.resolve().relative_to(source.resolve())
        except (OSError, ValueError):
            continue
        if not path.is_file():
            continue
        grouped.setdefault(path.stem, {})[".pdf"] = path

    templates = []
    for stem, paths in grouped.items():
        parsed_text = None
        if paths.get(".pdf"):
            parsed_text = extract_resume_text(paths[".pdf"])
        templates.append(
            ResumeTemplate(
                track=infer_track_from_filename(stem),
                docx_path=None,
                pdf_path=paths.get(".pdf"),
                parsed_text=parsed_text,
            )
        )
    return templates


def select_best_resume_template(
    templates: list[ResumeTemplate],
    *,
    target_track: str,
    required_skills: list[str],
) -> ResumeTemplate | None:
    """Choose the template with the strongest evidence for this specific JD.

    Filename-derived tracks are useful hints, but they are not proof that the
    corresponding resume actually covers a role's required skills. Prefer the
    template that substantiates the most required skills, then use the target
    track as a deterministic tie-breaker.
    """
    uploadable_templates = [template for template in templates if template.upload_path]
    if not uploadable_templates:
        return None

    normalized_skills = [skill.lower().strip() for skill in required_skills if skill.strip()]
    target_keywords = [
        keyword.lower().strip()
        for keyword in ROLE_KEYWORDS.get(target_track, [])
        if keyword.strip()
    ]

    def rank(template: ResumeTemplate) -> tuple[int, int, int, int, int]:
        evidence = (template.parsed_text or "").lower()
        coverage = sum(skill in evidence for skill in normalized_skills)
        track_coverage = sum(keyword in evidence for keyword in target_keywords)
        track_match = int(template.track == target_track)
        score = coverage * 3 + track_coverage + track_match * 2
        return (
            score,
            track_coverage,
            coverage,
            track_match,
            int(bool(evidence)),
        )

    # Always select the strongest available PDF, even without an exact
    # filename-derived track match.
    return max(uploadable_templates, key=rank)
