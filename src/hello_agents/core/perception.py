"""Perception module for converting environment input into observations."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Callable, Mapping

from .contracts import Observation, ToolResult

_MAX_FIELD_LABEL_CHARS = 180
_MAX_OPTION_CHARS = 100
_MAX_OPTIONS = 12
_MAX_SAFE_STRING_CHARS = 2000


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
        normalized_fields = (
            [
                self._compact_ats_field(field)
                for field in fields
                if isinstance(field, Mapping)
            ]
            if isinstance(fields, list)
            else []
        )
        return self.observe(
            "form",
            "ats_page",
            {
                "fields": normalized_fields,
                "field_count": len(normalized_fields),
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
                            self._compact_runtime_event(event)
                            for event in runtime_events
                            if isinstance(event, Mapping)
                        ],
                        "terminal": (
                            self._safe_mapping(terminal)
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
                "output": self._safe_output(result.output),
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

    @classmethod
    def _compact_runtime_event(cls, event: Mapping[str, Any]) -> dict[str, Any]:
        payload = cls._safe_mapping(event)
        fields = event.get("fields")
        if isinstance(fields, Sequence) and not isinstance(
            fields,
            (str, bytes, bytearray),
        ):
            payload["fields"] = [
                cls._compact_ats_field(field)
                for field in fields
                if isinstance(field, Mapping)
            ]
            payload["field_count"] = len(payload["fields"])
        return payload

    @classmethod
    def _compact_ats_field(cls, field: Mapping[str, Any]) -> dict[str, Any]:
        """Reduce raw DOM descriptors to an accessibility-style field summary."""
        options = field.get("options")
        option_labels: list[str] = []
        if isinstance(options, Sequence) and not isinstance(
            options,
            (str, bytes, bytearray),
        ):
            for option in options[:_MAX_OPTIONS]:
                option_labels.append(
                    cls._safe_text(cls._option_text(option), _MAX_OPTION_CHARS)
                )
        return {
            "label": cls._safe_text(
                field.get("label")
                or field.get("ariaLabel")
                or field.get("placeholder")
                or field.get("name")
                or "",
                _MAX_FIELD_LABEL_CHARS,
            ),
            "type": cls._safe_text(field.get("type") or "", 40),
            "role": cls._safe_text(field.get("role") or "", 40),
            "kind": cls._safe_text(field.get("kind") or "", 40),
            "required": bool(field.get("required")),
            "sensitive": bool(field.get("sensitive") or field.get("isSensitive")),
            "options": option_labels,
            "option_count": (
                len(options)
                if isinstance(options, Sequence)
                and not isinstance(options, (str, bytes, bytearray))
                else 0
            ),
        }

    @classmethod
    def _safe_output(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return cls._safe_mapping(value)
        if isinstance(value, str):
            return cls._safe_text(value, _MAX_SAFE_STRING_CHARS)
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return [cls._safe_output(item) for item in value[:_MAX_OPTIONS]]
        return value

    @classmethod
    def _safe_mapping(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        unsafe_keys = {
            "html",
            "raw_html",
            "dom",
            "raw_dom",
            "screenshot",
            "page_text",
            "text",
            "readback",
            "value",
            "profile_json",
            "candidate_facts",
            "credentials",
            "password",
            "token",
        }
        safe: dict[str, Any] = {}
        for key, raw in value.items():
            normalized_key = str(key)
            if normalized_key.lower() in unsafe_keys:
                safe[normalized_key] = cls._redacted_summary(raw)
            elif normalized_key == "fields" and isinstance(raw, Sequence) and not isinstance(
                raw,
                (str, bytes, bytearray),
            ):
                safe[normalized_key] = [
                    cls._compact_ats_field(field)
                    for field in raw
                    if isinstance(field, Mapping)
                ]
            elif isinstance(raw, Mapping):
                safe[normalized_key] = cls._safe_mapping(raw)
            elif isinstance(raw, str):
                safe[normalized_key] = cls._safe_text(raw, _MAX_SAFE_STRING_CHARS)
            elif isinstance(raw, Sequence) and not isinstance(
                raw,
                (str, bytes, bytearray),
            ):
                safe[normalized_key] = [
                    cls._safe_output(item) for item in raw[:_MAX_OPTIONS]
                ]
            else:
                safe[normalized_key] = raw
        return safe

    @staticmethod
    def _option_text(option: Any) -> str:
        if isinstance(option, Mapping):
            return str(option.get("label") or option.get("text") or option.get("value") or "")
        return str(option or "")

    @staticmethod
    def _safe_text(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "..."

    @staticmethod
    def _redacted_summary(value: Any) -> dict[str, Any]:
        if isinstance(value, str):
            return {
                "redacted": True,
                "chars": len(value),
            }
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return {
                "redacted": True,
                "items": len(value),
            }
        return {"redacted": True}
