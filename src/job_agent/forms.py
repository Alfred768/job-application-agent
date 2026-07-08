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
    action: str = "fill"


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
        action = "fill"
        if field_item.field_type.lower() == "file":
            action = "upload"
            if "resume" in label_lower or "cv" in label_lower:
                value = profile.get("resume_file", "")
                confidence = 1.0 if value else 0.0
        elif "email" in label_lower:
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
                action=action,
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
        if field_item.action == "upload":
            lines.append(
                f"  await page.getByLabel({json.dumps(field_item.label)}).setInputFiles({json.dumps(field_item.value)});"
            )
        else:
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


def render_playwright_form_snapshot_script(
    application_url: str | None = None,
    output_path: str = "form-snapshot.json",
) -> str:
    lines = [
        'const { chromium } = require("playwright");',
        'const fs = require("fs");',
        "",
        "async function main() {",
        "  const browser = await chromium.launch({ headless: false });",
        "  const page = await browser.newPage();",
    ]
    if application_url:
        lines.append(f"  await page.goto({json.dumps(application_url)});")
    lines.extend(
        [
            "  const fields = await page.evaluate(() => {",
            '    const controls = Array.from(document.querySelectorAll("input, textarea, select"));',
            "    const labelFor = (control) => {",
            "      if (control.id) {",
            '        const explicit = document.querySelector(`label[for="${control.id}"]`);',
            "        if (explicit && explicit.textContent) return explicit.textContent.trim();",
            "      }",
            "      const wrapping = control.closest('label');",
            "      if (wrapping && wrapping.textContent) return wrapping.textContent.trim();",
            "      return control.getAttribute('aria-label') || control.getAttribute('placeholder') || control.name || '';",
            "    };",
            "    return controls.map((control) => ({",
            "      label: labelFor(control),",
            "      type: control.getAttribute('type') || control.tagName.toLowerCase(),",
            "      required: Boolean(control.required),",
            "      options: control.tagName.toLowerCase() === 'select'",
            "        ? Array.from(control.options).map((option) => option.textContent.trim()).filter(Boolean)",
            "        : [],",
            "    })).filter((field) => field.label);",
            "  });",
            f"  fs.writeFileSync({json.dumps(output_path)}, JSON.stringify(fields, null, 2));",
            f"  console.log('Wrote form snapshot to {output_path}');",
            "  console.log('Review the snapshot before using it for guarded form filling.');",
            "}",
            "",
            "main().catch((error) => {",
            "  console.error(error);",
            "  process.exit(1);",
            "});",
        ]
    )
    return "\n".join(lines) + "\n"
