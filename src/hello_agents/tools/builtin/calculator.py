"""Safe arithmetic example Tool."""

from __future__ import annotations

import ast
import operator
from typing import Any, Callable, Dict, List

from hello_agents.core.contracts import ToolEffect

from ..base import Tool, ToolParameter


_BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class CalculatorTool(Tool):
    """Evaluate a bounded arithmetic expression without eval."""

    def __init__(self) -> None:
        super().__init__(
            name="calculator",
            description="Evaluate basic arithmetic.",
            effect=ToolEffect.READ,
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="expression",
                type="string",
                description=(
                    "Expression using numbers, parentheses, and "
                    "+ - * / // % **."
                ),
            )
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        expression = str(
            parameters.get("expression")
            or parameters.get("input")
            or ""
        ).strip()
        if not expression:
            return "Error: empty expression"
        if len(expression) > 200:
            return "Error: expression is too long"
        try:
            parsed = ast.parse(expression, mode="eval")
            result = self._evaluate(parsed.body)
        except (SyntaxError, TypeError, ValueError, ZeroDivisionError) as exc:
            return f"Error: {exc}"
        return str(result)

    def _evaluate(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(
                node.value,
                (int, float),
            ):
                raise ValueError("only numeric constants are supported")
            return node.value
        if isinstance(node, ast.UnaryOp):
            function = _UNARY_OPERATORS.get(type(node.op))
            if function is None:
                raise ValueError("unsupported unary operator")
            return function(self._evaluate(node.operand))
        if isinstance(node, ast.BinOp):
            function = _BINARY_OPERATORS.get(type(node.op))
            if function is None:
                raise ValueError("unsupported binary operator")
            left = self._evaluate(node.left)
            right = self._evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent is too large")
            result = function(left, right)
            if isinstance(result, (int, float)) and abs(result) > 1e100:
                raise ValueError("result is too large")
            return result
        raise ValueError("unsupported expression")
