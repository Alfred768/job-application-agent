"""Structured JD parser tool."""

from __future__ import annotations

import json
from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.jd_analysis import parse_jd


class JDParserTool(Tool):
    """Parse JD text into structured role and skill analysis."""

    def __init__(self):
        super().__init__(
            name="jd_parser",
            description="Parse JD text into title, company, role track, skills, responsibilities, and risks.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        jd_text = parameters.get("jd_text") or parameters.get("input") or ""
        return json.dumps(parse_jd(jd_text).to_dict(), indent=2)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="jd_text",
                type="string",
                description="Raw JD text to parse.",
            )
        ]
