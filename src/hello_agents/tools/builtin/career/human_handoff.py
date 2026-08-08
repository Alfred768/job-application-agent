"""Human-in-the-loop handoff tools for unresolved candidate facts."""

from __future__ import annotations

from typing import Any, Mapping

from hello_agents.core.contracts import ToolEffect
from hello_agents.tools.base import Tool, ToolParameter


class EscalateToHumanTool(Tool):
    """Serialize a candidate-fact blocker without inventing an answer."""

    def __init__(self):
        super().__init__(
            name="escalate_to_human",
            description=(
                "Record unresolved required application fields for candidate "
                "approval before any resume, browser, or submit retry."
            ),
            effect=ToolEffect.WRITE,
        )

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        labels = parameters.get("field_labels") or []
        if not isinstance(labels, list):
            labels = [labels]
        checkpoint = parameters.get("checkpoint")
        return {
            "status": "waiting_for_user",
            "reason": str(
                parameters.get("reason")
                or "required_candidate_fact_missing"
            ),
            "field_labels": [
                str(label) for label in labels if str(label or "").strip()
            ],
            "checkpoint": (
                self._safe_checkpoint(checkpoint)
                if isinstance(checkpoint, Mapping)
                else {}
            ),
            "retry_scope": "single_application",
        }

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="field_labels",
                type="array",
                description="Required field labels that need approved candidate facts.",
            ),
            ToolParameter(
                name="reason",
                type="string",
                description="Privacy-safe reason for escalating the application.",
                required=False,
            ),
            ToolParameter(
                name="checkpoint",
                type="object",
                description="Privacy-safe resume point for the current application.",
                required=False,
            ),
        ]

    @staticmethod
    def _safe_checkpoint(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "application_id",
            "agent_runtime_id",
            "company",
            "title",
            "phase",
            "observation_id",
            "status",
        }
        return {
            str(key): value
            for key, value in checkpoint.items()
            if str(key) in allowed
            and isinstance(value, (str, int, float, bool, type(None)))
        }
