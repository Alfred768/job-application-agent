"""Calculator tool - basic arithmetic built-in tool for the HelloAgents base."""

from __future__ import annotations

from typing import Any, Dict, List

from ..base import Tool, ToolParameter


class CalculatorTool(Tool):
    """Evaluate a basic arithmetic expression safely."""

    def __init__(self):
        super().__init__(
            name="calculator",
            description="Evaluate a basic arithmetic expression, e.g. '2 + 3 * 4'.",
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="expression",
                type="string",
                description="Arithmetic expression using + - * / ** % and parentheses.",
                required=True,
            )
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        expr = str(parameters.get("expression") or parameters.get("input") or "").strip()
        if not expr:
            return "Error: empty expression"
        # Restrict to a safe character set; no names, no builtins.
        allowed = set("0123456789+-*/.%() ")
        if not set(expr) <= allowed:
            return "Error: expression contains unsupported characters"
        try:
            # eval with no builtins on a restricted node set
            result = eval(expr, {"__builtins__": {}}, {})
        except Exception as exc:  # noqa: BLE001
            return f"Error: {exc}"
        return str(result)
