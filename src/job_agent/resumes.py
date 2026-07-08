from __future__ import annotations

from pathlib import Path

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


def index_resume_templates(source_dir: str | Path) -> list[ResumeTemplate]:
    source = Path(source_dir).expanduser()
    grouped: dict[str, dict[str, Path]] = {}

    for path in sorted(source.glob("GAOYI_WU_*")):
        if path.suffix.lower() not in {".docx", ".pdf"}:
            continue
        grouped.setdefault(path.stem, {})[path.suffix.lower()] = path

    templates = []
    for stem, paths in grouped.items():
        templates.append(
            ResumeTemplate(
                track=infer_track_from_filename(stem),
                docx_path=paths.get(".docx"),
                pdf_path=paths.get(".pdf"),
            )
        )
    return templates
