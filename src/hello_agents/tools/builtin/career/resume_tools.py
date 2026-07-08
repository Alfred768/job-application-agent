"""Resume index and selection tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.jobs import import_job_from_text
from job_agent.resumes import index_resume_templates
from job_agent.scoring import classify_role


class ResumeIndexerTool(Tool):
    """Index role-specific resume templates from a local directory."""

    def __init__(self):
        super().__init__(
            name="resume_indexer",
            description="Index DOCX/PDF resume templates from RESUME_SOURCE_DIR or a provided directory.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        source_dir = parameters.get("source_dir") or parameters.get("input") or ""
        templates = index_resume_templates(Path(source_dir))
        if not templates:
            return "No resume templates found."
        lines = []
        for template in templates:
            lines.append(
                f"track={template.track}; "
                f"docx={template.docx_path or 'None'}; "
                f"pdf={template.pdf_path or 'None'}"
            )
        return "\n".join(lines)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="source_dir",
                type="string",
                description="Directory containing role-specific resume DOCX/PDF files.",
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
        selected = next((item for item in templates if item.track == selected_track), None)
        if selected is None:
            return f"selected_track={selected_track}\nselected_template=None"
        return (
            f"selected_track={selected_track}\n"
            f"selected_docx={selected.docx_path or 'None'}\n"
            f"selected_pdf={selected.pdf_path or 'None'}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="source_dir",
                type="string",
                description="Directory containing role-specific resume DOCX/PDF files.",
            ),
            ToolParameter(
                name="jd_text",
                type="string",
                description="Raw JD text used to classify the target resume track.",
            ),
        ]
