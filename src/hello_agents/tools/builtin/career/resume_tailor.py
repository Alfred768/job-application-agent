"""Resume tailoring plan and truthfulness tools."""

from __future__ import annotations

import json
from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.resume_plans import propose_resume_edit_plan


class ResumeTailorTool(Tool):
    """Generate a grounded resume edit plan without modifying source files."""

    def __init__(self):
        super().__init__(
            name="resume_tailor",
            description="Generate an auditable resume edit plan grounded in supported user evidence.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        jd_text = parameters.get("jd_text") or parameters.get("input") or ""
        resume_track = parameters.get("resume_track")
        plan = propose_resume_edit_plan(jd_text, resume_track)
        return json.dumps(plan.to_dict(), indent=2)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="jd_text",
                type="string",
                description="Raw JD text used to propose resume edits.",
            ),
            ToolParameter(
                name="resume_track",
                type="string",
                description="Selected resume track.",
                required=False,
                default="",
            ),
        ]


class TruthfulnessCheckTool(Tool):
    """Check whether a resume edit plan contains unsupported claims."""

    def __init__(self):
        super().__init__(
            name="truthfulness_check",
            description="Check a resume edit plan for unsupported keywords requiring user review.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        plan_json = parameters.get("plan_json") or parameters.get("input") or "{}"
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError:
            return "truthfulness_status=needs_review\nreason=invalid_plan_json"
        unsupported = plan.get("unsupported_keywords") or []
        if unsupported:
            return (
                "truthfulness_status=needs_review\n"
                f"unsupported_keywords={', '.join(unsupported)}"
            )
        return "truthfulness_status=passed"

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="plan_json",
                type="string",
                description="Resume edit plan JSON produced by resume_tailor.",
            )
        ]
