from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from job_agent.jd_analysis import parse_jd
from job_agent.jobs import import_job_from_text
from job_agent.reports import render_markdown_review
from job_agent.scoring import score_fit


@dataclass(frozen=True)
class ApplicationPackage:
    package_dir: Path
    review_path: Path
    jd_analysis_path: Path
    submit_gate_path: Path


def export_application_package(jd_text: str, output_dir: str | Path) -> ApplicationPackage:
    package_dir = Path(output_dir)
    package_dir.mkdir(parents=True, exist_ok=True)

    job = import_job_from_text(jd_text)
    review = render_markdown_review(job, score_fit(job))
    jd_analysis = parse_jd(jd_text).to_dict()
    submit_gate = (
        "Automatic final submission is enabled when all required fields have "
        "truthful answers and no blocking review fields remain."
    )

    review_path = package_dir / "review.md"
    jd_analysis_path = package_dir / "jd-analysis.json"
    submit_gate_path = package_dir / "submit-gate.txt"

    review_path.write_text(review)
    jd_analysis_path.write_text(json.dumps(jd_analysis, indent=2))
    submit_gate_path.write_text(submit_gate)

    return ApplicationPackage(
        package_dir=package_dir,
        review_path=review_path,
        jd_analysis_path=jd_analysis_path,
        submit_gate_path=submit_gate_path,
    )
