"""Fit scoring tool."""

from __future__ import annotations

from typing import Any

from job_agent.jobs import import_job_from_text
from job_agent.scoring import score_fit
from hello_agents.tools.base import Tool, ToolParameter


class FitScorerTool(Tool):
    """Score a JD against the user's current role tracks."""

    def __init__(self):
        super().__init__(
            name="fit_scorer",
            description="Classify role track and compute explainable fit score for JD text.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        text = parameters.get("input") or parameters.get("text") or ""
        job = import_job_from_text(text)
        score = score_fit(job)
        return (
            f"score={score.score}\n"
            f"role_track={score.role_track}\n"
            f"recommendation={score.recommendation}\n"
            f"reasons={'; '.join(score.reasons)}"
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Raw JD text to classify and score.",
            )
        ]
