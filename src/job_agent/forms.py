from __future__ import annotations

from dataclasses import dataclass, field
import json

from job_agent.sensitive_kb import resolve_sensitive_answer


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
    approved: bool = False


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
        return [
            field.label
            for field in self.fields
            if field.confidence < 0.9 or (field.sensitive and not field.approved)
        ]


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


def _approved_answers(profile: dict) -> dict[str, str]:
    raw_answers = profile.get("answers", {})
    if not isinstance(raw_answers, dict):
        return {}
    return {str(key).strip().lower(): str(value) for key, value in raw_answers.items()}


def build_form_fill_plan(fields: list[FormField], profile: dict) -> FormFillPlan:
    approved_answers = _approved_answers(profile)
    plans = []
    for field_item in fields:
        label_lower = field_item.label.lower()
        ftype = field_item.field_type.lower()
        value = ""
        confidence = 0.0
        action = "fill"
        approved = False
        sensitive = is_sensitive_field(field_item.label)
        exact_answer = approved_answers.get(label_lower)
        if ftype == "file":
            action = "upload"
            if "resume" in label_lower or "cv" in label_lower:
                value = profile.get("resume_file", "")
                confidence = 1.0 if value else 0.0
                approved = True if value else False
        elif sensitive:
            # Sensitive fields auto-fill ONLY from the approved knowledge base
            # (sensitive_answers KB with approved=true, or legacy profile fields
            # with a real, non-placeholder value). An answers-bank entry is
            # stored but kept for review (confidence 0.5) so "Needs review" and
            # other placeholders are never auto-submitted.
            kb_answer = resolve_sensitive_answer(field_item.label, profile)
            if kb_answer:
                value = kb_answer
                confidence = 1.0
                approved = True
                action = "select" if ftype == "select" else "fill"
            elif exact_answer is not None:
                value = exact_answer
                confidence = 0.5
                action = "select" if ftype == "select" else "fill"
        elif exact_answer is not None:
            action = "select" if ftype == "select" else "fill"
            value = exact_answer
            confidence = 1.0
            approved = True
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
        elif "portfolio" in label_lower:
            value = profile.get("portfolio", "") or profile.get("website", "")
            confidence = 1.0 if value else 0.0
        elif "website" in label_lower or "personal site" in label_lower:
            value = profile.get("website", "") or profile.get("portfolio", "")
            confidence = 1.0 if value else 0.0
        elif "location" in label_lower or "city" in label_lower:
            value = profile.get("location", "") or profile.get("city", "")
            confidence = 1.0 if value else 0.0
        elif "cover letter" in label_lower:
            value = profile.get("cover_letter", "")
            confidence = 1.0 if value else 0.0
        plans.append(
            FieldPlan(
                label=field_item.label,
                value=value,
                sensitive=sensitive,
                confidence=confidence,
                action=action,
                approved=approved,
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
        # Fill any field with a confident approved value. Sensitive fields are
        # filled only when explicitly approved (knowledge-base answer); all
        # other sensitive/low-confidence fields stay for manual review.
        if not field_item.value or field_item.confidence < 0.9 or (
            field_item.sensitive and not field_item.approved
        ):
            continue
        if field_item.action == "upload":
            lines.append(
                f"  await page.getByLabel({json.dumps(field_item.label)}).setInputFiles({json.dumps(field_item.value)});"
            )
        elif field_item.action == "select":
            lines.append(
                f"  await page.getByLabel({json.dumps(field_item.label)}).selectOption({{ label: {json.dumps(field_item.value)} }});"
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
