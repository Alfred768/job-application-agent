"""Automatic submission policy tool."""

from __future__ import annotations

from typing import Any

from hello_agents.tools.base import Tool, ToolParameter


class SubmitGateTool(Tool):
    """Describe when an application may be submitted automatically."""

    def __init__(self):
        super().__init__(
            name="submit_gate",
            description="Allow final Submit when all required answers are resolved truthfully.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        return (
            "Automatic final submission is enabled when all required fields have "
            "truthful answers and no blocking review fields remain."
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Optional form or application state summary.",
                required=False,
                default="",
            )
        ]
