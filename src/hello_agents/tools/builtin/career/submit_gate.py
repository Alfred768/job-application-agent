"""Submit safety gate tool."""

from __future__ import annotations

from typing import Any

from hello_agents.tools.base import Tool, ToolParameter


class SubmitGateTool(Tool):
    """Hard gate that prevents ordinary browser applications from auto-submitting."""

    def __init__(self):
        super().__init__(
            name="submit_gate",
            description="Enforce human confirmation before final Submit on browser applications.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        return (
            "Final Submit remains manual unless an allowed source-specific adapter "
            "explicitly permits auto-submit."
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
