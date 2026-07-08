"""Compliant job intake tools."""

from __future__ import annotations

from typing import Any

from job_agent.jobs import import_job_from_text
from hello_agents.tools.base import Tool, ToolParameter


class ManualJDImportTool(Tool):
    """Import user-provided JD text into a normalized job posting."""

    def __init__(self):
        super().__init__(
            name="manual_jd_import",
            description="Import user-provided job description text into a normalized job object.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        text = parameters.get("input") or parameters.get("text") or ""
        job = import_job_from_text(text)
        return (
            f"company={job.company}\n"
            f"title={job.title}\n"
            f"location={job.location or 'Unknown'}\n"
            f"source={job.source}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Raw JD text pasted or provided by the user.",
            )
        ]
