"""Perception module for converting environment input into observations."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .contracts import Observation, ToolResult


class StructuredPerception:
    """Normalize raw environment and tool data at the system boundary."""

    def observe(
        self,
        kind: str,
        source: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Observation:
        return Observation(kind=kind, source=source, payload=dict(payload or {}))

    def observe_job(
        self,
        jd_text: str,
        *,
        parser: Callable[[str], Any] | None = None,
    ) -> Observation:
        parsed = parser(jd_text) if parser is not None else None
        payload: dict[str, Any] = {"raw_text": jd_text}
        if parsed is not None:
            payload["job"] = self._structured_value(parsed)
        return self.observe("job", "job_description", payload)

    def observe_form(self, snapshot_json: str) -> Observation:
        try:
            fields = json.loads(snapshot_json or "[]")
        except json.JSONDecodeError as exc:
            return self.observe(
                "form",
                "ats_page",
                {
                    "fields": [],
                    "valid": False,
                    "error": f"invalid_form_snapshot:{exc.msg}",
                },
            )
        return self.observe(
            "form",
            "ats_page",
            {
                "fields": fields if isinstance(fields, list) else [],
                "valid": isinstance(fields, list),
            },
        )

    def observe_tool_result(self, result: ToolResult) -> Observation:
        if isinstance(result.output, Mapping):
            runtime_events = result.output.get("runtime_observations")
            terminal = result.output.get("record")
            if isinstance(runtime_events, list) or isinstance(
                runtime_events,
                tuple,
            ):
                return self.observe(
                    "ats_runtime",
                    result.tool_name,
                    {
                        "call_id": result.call_id,
                        "ok": result.ok,
                        "effect": result.effect.value,
                        "events": [
                            dict(event)
                            for event in runtime_events
                            if isinstance(event, Mapping)
                        ],
                        "terminal": (
                            dict(terminal)
                            if isinstance(terminal, Mapping)
                            else {}
                        ),
                        "error": result.error,
                        "policy_code": (
                            result.policy_decision.code
                            if result.policy_decision is not None
                            else None
                        ),
                    },
                )
        return self.observe(
            "tool_result",
            result.tool_name,
            {
                "call_id": result.call_id,
                "ok": result.ok,
                "effect": result.effect.value,
                "output": result.output,
                "error": result.error,
                "policy_code": (
                    result.policy_decision.code
                    if result.policy_decision is not None
                    else None
                ),
            },
        )

    @staticmethod
    def _structured_value(value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return {
                name: getattr(value, name)
                for name in value.__dataclass_fields__
            }
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if isinstance(value, Mapping):
            return dict(value)
        return value
