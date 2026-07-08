"""Form inspection and guarded fill-plan tools."""

from __future__ import annotations

import json
from typing import Any

from hello_agents.tools.base import Tool, ToolParameter
from job_agent.forms import (
    build_form_fill_plan,
    detect_sensitive_fields,
    inspect_form_snapshot,
    render_playwright_fill_script,
)


class FormInspectorTool(Tool):
    """Normalize an ATS form field snapshot."""

    def __init__(self):
        super().__init__(
            name="form_inspector",
            description="Normalize form field labels, types, required flags, and options.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        snapshot = parameters.get("form_snapshot_json") or parameters.get("input") or "[]"
        fields = inspect_form_snapshot(snapshot)
        return json.dumps([field.__dict__ for field in fields], indent=2)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="form_snapshot_json",
                type="string",
                description="JSON array of form fields captured from an ATS page.",
            )
        ]


class SensitiveFieldDetectorTool(Tool):
    """Detect sensitive fields that require user review."""

    def __init__(self):
        super().__init__(
            name="sensitive_field_detector",
            description="Detect sponsorship, work authorization, demographic, salary, relocation, and legal fields.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        snapshot = parameters.get("form_snapshot_json") or parameters.get("input") or "[]"
        sensitive = detect_sensitive_fields(inspect_form_snapshot(snapshot))
        if not sensitive:
            return "sensitive_fields=None"
        return f"sensitive_fields={'; '.join(sensitive)}"

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="form_snapshot_json",
                type="string",
                description="JSON array of form fields captured from an ATS page.",
            )
        ]


class FormFillerTool(Tool):
    """Create a guarded form fill plan without submitting the application."""

    def __init__(self):
        super().__init__(
            name="form_filler",
            description="Map profile facts to form fields and identify fields requiring review.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        snapshot = parameters.get("form_snapshot_json") or parameters.get("input") or "[]"
        profile_json = parameters.get("profile_json") or "{}"
        profile = json.loads(profile_json)
        plan = build_form_fill_plan(inspect_form_snapshot(snapshot), profile)
        lines = [f"can_auto_submit={plan.can_auto_submit}"]
        for field in plan.fields:
            lines.append(
                f"{field.label}={field.value}; sensitive={field.sensitive}; confidence={field.confidence}"
            )
        review_required = plan.review_required_fields
        lines.append(
            "review_required="
            + ("; ".join(review_required) if review_required else "None")
        )
        return "\n".join(lines)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="form_snapshot_json",
                type="string",
                description="JSON array of form fields captured from an ATS page.",
            ),
            ToolParameter(
                name="profile_json",
                type="string",
                description="JSON object containing approved profile facts.",
            ),
        ]


class FormFillScriptTool(Tool):
    """Generate a guarded Playwright script for low-risk form filling."""

    def __init__(self):
        super().__init__(
            name="form_fill_script",
            description="Generate a Playwright form-fill script that avoids sensitive fields and never submits.",
        )

    def run(self, parameters: dict[str, Any]) -> str:
        snapshot = parameters.get("form_snapshot_json") or parameters.get("input") or "[]"
        profile_json = parameters.get("profile_json") or "{}"
        application_url = parameters.get("application_url")
        resume_file = parameters.get("resume_file")
        profile = json.loads(profile_json)
        if resume_file:
            profile["resume_file"] = str(resume_file)
        plan = build_form_fill_plan(inspect_form_snapshot(snapshot), profile)
        return render_playwright_fill_script(plan, application_url=application_url)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="form_snapshot_json",
                type="string",
                description="JSON array of form fields captured from an ATS page.",
            ),
            ToolParameter(
                name="profile_json",
                type="string",
                description="JSON object containing approved profile facts.",
            ),
            ToolParameter(
                name="application_url",
                type="string",
                description="Optional application page URL to open before filling fields.",
                required=False,
            ),
            ToolParameter(
                name="resume_file",
                type="string",
                description="Optional approved resume file path for Resume/CV upload fields.",
                required=False,
            ),
        ]
