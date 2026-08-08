"""Fit scoring tool."""

from __future__ import annotations

import json
from typing import Any

from job_agent.jobs import import_job_from_text
from job_agent.scoring import score_fit
from hello_agents.tools.base import Tool, ToolParameter


class EvaluateFitTool(Tool):
    """Evaluate JD fit without building an application package."""

    def __init__(self, *, name: str = "evaluate_fit"):
        super().__init__(
            name=name,
            description=(
                "Read a JD and return fit score, matched skills, and missing "
                "signals only."
            ),
        )

    def run(self, parameters: dict[str, Any]) -> str:
        text = parameters.get("input") or parameters.get("text") or ""
        job = import_job_from_text(text)
        score = score_fit(job)
        return json.dumps(
            {
                "score": score.score,
                "role_track": score.role_track,
                "recommendation": score.recommendation,
                "matched_skills": score.matched_skills,
                "missing_keywords": score.missing_keywords,
                "reasons": score.reasons,
            },
            indent=2,
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Raw JD text to classify and score.",
            )
        ]


class FitScorerTool(EvaluateFitTool):
    """Backward-compatible text output for existing callers."""

    def __init__(self):
        super().__init__(name="fit_scorer")

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
