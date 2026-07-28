"""Resume index and selection tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.jd_analysis import parse_jd
from job_agent.jobs import import_job_from_text
from job_agent.resumes import index_resume_templates, select_best_resume_template
from job_agent.scoring import classify_role


class ResumeIndexerTool(Tool):
    """Index role-specific resume templates from a local directory."""

    def __init__(self):
        super().__init__(
            name="resume_indexer",
            description="Index local PDF resume templates eligible for ATS upload.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        source_dir = parameters.get("source_dir") or parameters.get("input") or ""
        templates = index_resume_templates(Path(source_dir))
        if not templates:
            return "No resume templates found."
        lines = []
        for template in templates:
            lines.append(
                f"track={template.track}; pdf={template.pdf_path or 'None'}"
            )
        return "\n".join(lines)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="source_dir",
                type="string",
                description="Directory containing role-specific resume PDF files.",
            )
        ]


class ResumeSelectorTool(Tool):
    """Select the best resume template for a JD."""

    def __init__(self):
        super().__init__(
            name="resume_selector",
            description="Select the closest resume template based on the JD role track.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        source_dir = parameters.get("source_dir") or ""
        jd_text = parameters.get("jd_text") or parameters.get("input") or ""
        job = import_job_from_text(jd_text)
        selected_track = classify_role(job)
        templates = index_resume_templates(Path(source_dir))
        selected = select_best_resume_template(
            templates,
            target_track=selected_track,
            required_skills=parse_jd(jd_text).required_skills,
        )
        if selected is None:
            return f"selected_track={selected_track}\nselected_template=None"
        return (
            f"selected_track={selected_track}\n"
            f"selected_pdf={selected.upload_path or 'None'}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="source_dir",
                type="string",
                description="Directory containing role-specific resume PDF files.",
            ),
            ToolParameter(
                name="jd_text",
                type="string",
                description="Raw JD text used to classify the target resume track.",
            ),
        ]
