"""Application review packet tool."""

from __future__ import annotations

from typing import Any

from job_agent.jobs import import_job_from_text
from job_agent.reports import render_markdown_review
from job_agent.scoring import score_fit
from hello_agents.tools.base import Tool, ToolParameter


class ReviewPacketTool(Tool):
    """Generate the human-readable application review packet."""

    def __init__(self):
        super().__init__(
            name="review_packet",
            description="Generate a Markdown review packet with fit score and safety gates.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        text = parameters.get("input") or parameters.get("text") or ""
        job = import_job_from_text(text)
        return render_markdown_review(job, score_fit(job))

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Raw JD text to turn into an application review packet.",
            )
        ]
