from __future__ import annotations

from dataclasses import dataclass, field
import json


SENSITIVE_FIELD_KEYWORDS = [
    "sponsor",
    "sponsorship",
    "visa",
    "authorization",
    "authorized",
    "disability",
    "veteran",
    "gender",
    "ethnicity",
    "race",
    "salary",
    "relocation",
    "start date",
    "legal",
    "attestation",
]


@dataclass(frozen=True)
class FieldPlan:
    label: str
    value: str
    sensitive: bool = False
    confidence: float = 1.0


@dataclass(frozen=True)
class FormFillPlan:
    fields: list[FieldPlan] = field(default_factory=list)
    can_auto_submit: bool = False
    submit_gate_reason: str = (
        "Final Submit remains manual for browser-based applications unless a "
        "source-specific adapter explicitly permits auto-submit."
    )

    @property
    def review_required_fields(self) -> list[str]:
        return [field.label for field in self.fields if field.sensitive or field.confidence < 0.9]


@dataclass(frozen=True)
class FormField:
    label: str
    field_type: str = "text"
    required: bool = False
    options: list[str] = field(default_factory=list)


def inspect_form_snapshot(snapshot_json: str) -> list[FormField]:
    raw_fields = json.loads(snapshot_json or "[]")
    fields = []
    for raw in raw_fields:
        fields.append(
            FormField(
                label=str(raw.get("label", "")).strip(),
                field_type=str(raw.get("type", "text")).strip() or "text",
                required=bool(raw.get("required", False)),
                options=list(raw.get("options", [])),
            )
        )
    return fields


def is_sensitive_field(label: str) -> bool:
    normalized = label.lower()
    return any(keyword in normalized for keyword in SENSITIVE_FIELD_KEYWORDS)


def detect_sensitive_fields(fields: list[FormField]) -> list[str]:
    return [field.label for field in fields if is_sensitive_field(field.label)]


def build_form_fill_plan(fields: list[FormField], profile: dict[str, str]) -> FormFillPlan:
    plans = []
    for field_item in fields:
        label_lower = field_item.label.lower()
        value = ""
        confidence = 0.0
        if "email" in label_lower:
            value = profile.get("email", "")
            confidence = 1.0 if value else 0.0
        elif "name" in label_lower:
            value = profile.get("name", "")
            confidence = 1.0 if value else 0.0
        elif "phone" in label_lower:
            value = profile.get("phone", "")
            confidence = 1.0 if value else 0.0
        elif "linkedin" in label_lower:
            value = profile.get("linkedin", "")
            confidence = 1.0 if value else 0.0
        elif "github" in label_lower:
            value = profile.get("github", "")
            confidence = 1.0 if value else 0.0
        elif "sponsor" in label_lower or "visa" in label_lower:
            value = profile.get("sponsorship", "")
            confidence = 0.5 if value else 0.0
        plans.append(
            FieldPlan(
                label=field_item.label,
                value=value,
                sensitive=is_sensitive_field(field_item.label),
                confidence=confidence,
            )
        )
    return FormFillPlan(fields=plans)


def render_playwright_fill_script(plan: FormFillPlan, application_url: str | None = None) -> str:
    lines = [
        'const { chromium } = require("playwright");',
        "",
        "async function main() {",
        "  const browser = await chromium.launch({ headless: false });",
        "  const page = await browser.newPage();",
    ]
    if application_url:
        lines.append(f"  await page.goto({json.dumps(application_url)});")

    for field_item in plan.fields:
        if field_item.sensitive or field_item.confidence < 0.9 or not field_item.value:
            continue
        lines.append(
            f"  await page.getByLabel({json.dumps(field_item.label)}).fill({json.dumps(field_item.value)});"
        )

    lines.extend(
        [
            f"  console.log('Review required fields:', {json.dumps(plan.review_required_fields)});",
            f"  console.log('Submit gate:', {json.dumps(plan.submit_gate_reason)});",
            "  console.log('Review the page manually before any final submission.');",
            "}",
            "",
            "main().catch((error) => {",
            "  console.error(error);",
            "  process.exit(1);",
            "});",
        ]
    )
    return "\n".join(lines) + "\n"
