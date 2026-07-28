from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

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
    "sex",
    "salary",
    "compensation",
    "pay expectation",
    "relocation",
    "start date",
    "eeo",
    "demographic",
    "clearance",
    "citizen",
    "citizenship",
    "legal",
    "attestation",
    "certify",
    "arbitration",
    "acknowledgement",
    "acknowledgment",
    "consent",
    "privacy",
    "personal data",
    "notetaker",
    "notetakers",
    "transcribe",
]


def _label_tokens(label: str) -> set[str]:
    return {
        token
        for token in "".join(ch.lower() if ch.isalnum() else " " for ch in label or "").split()
        if token
    }


def _is_low_risk_identity_field(label: str) -> bool:
    tokens = _label_tokens(label)
    if "legal" not in tokens:
        return False
    has_name_token = bool(tokens & {"name", "first", "last", "middle", "given", "family"})
    if not has_name_token:
        return False
    sensitive_context = {
        "attestation",
        "attest",
        "authorization",
        "authorized",
        "background",
        "certify",
        "sponsor",
        "sponsorship",
        "visa",
        "salary",
    }
    return not bool(tokens & sensitive_context)


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
    can_auto_submit: bool = True
    submit_gate_reason: str = (
        "Automatic final submission is enabled when no blocking review fields remain."
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
    field_id: str = ""
    name: str = ""


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
                field_id=str(raw.get("id", "")).strip(),
                name=str(raw.get("name", "")).strip(),
            )
        )
    return fields


def is_sensitive_field(label: str) -> bool:
    if _is_low_risk_identity_field(label):
        return False
    normalized = label.lower()
    return any(keyword in normalized for keyword in SENSITIVE_FIELD_KEYWORDS)


def detect_sensitive_fields(fields: list[FormField]) -> list[str]:
    return [field.label for field in fields if is_sensitive_field(field.label)]


def _approved_answers(profile: dict) -> dict[str, str]:
    raw_answers = profile.get("answers", {})
    if not isinstance(raw_answers, dict):
        return {}
    return {str(key).strip().lower(): str(value) for key, value in raw_answers.items()}


def _normalize_option(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else " " for ch in text or "").strip()


def _matching_option_label(options: list[str], answer: str) -> str:
    want = _normalize_option(answer)
    if not want:
        return answer
    want_tokens = [token for token in want.split() if token]
    for option in options:
        opt = _normalize_option(option)
        opt_tokens = set(opt.split())
        if (
            opt == want
            or (want and want in opt)
            or (opt and opt in want)
            or (want_tokens and all(token in opt_tokens for token in want_tokens))
        ):
            return str(option)
    return answer


def build_form_fill_plan(fields: list[FormField], profile: dict) -> FormFillPlan:
    approved_answers = _approved_answers(profile)
    plans = []
    for field_item in fields:
        label_lower = field_item.label.lower()
        field_key = " ".join(
            part
            for part in [
                field_item.label.lower(),
                field_item.field_id.lower(),
                field_item.name.lower(),
            ]
            if part
        )
        ftype = field_item.field_type.lower()
        value = ""
        confidence = 0.0
        action = "fill"
        approved = False
        sensitive = is_sensitive_field(field_item.label)
        exact_answer = approved_answers.get(label_lower)
        if ftype == "file":
            action = "upload"
            if "resume" in field_key or "cv" in field_key:
                value = profile.get("resume_file", "")
                is_pdf = bool(value) and Path(str(value)).expanduser().suffix.lower() == ".pdf"
                confidence = 1.0 if is_pdf else 0.0
                approved = is_pdf
        elif sensitive:
            # Sensitive fields auto-fill ONLY from the approved knowledge base
            # (sensitive_answers KB with approved=true, or legacy profile fields
            # with a real, non-placeholder value). An answers-bank entry is
            # stored but kept for review (confidence 0.5) so "Needs review" and
            # other placeholders are never auto-submitted.
            kb_answer = resolve_sensitive_answer(field_item.label, profile)
            if kb_answer:
                value = _matching_option_label(field_item.options, kb_answer) if ftype == "select" else kb_answer
                confidence = 1.0
                approved = True
                action = "select" if ftype == "select" else "fill"
            elif exact_answer is not None:
                value = _matching_option_label(field_item.options, exact_answer) if ftype == "select" else exact_answer
                confidence = 0.5
                action = "select" if ftype == "select" else "fill"
        elif exact_answer is not None:
            action = "select" if ftype == "select" else "fill"
            value = _matching_option_label(field_item.options, exact_answer) if ftype == "select" else exact_answer
            confidence = 1.0
            approved = True
        elif "email" in label_lower:
            value = profile.get("email", "")
            confidence = 1.0 if value else 0.0
        elif "first name" in label_lower or "given name" in label_lower:
            value = profile.get("first_name") or (profile.get("name", "").split(" ")[0] if profile.get("name") else "")
            confidence = 1.0 if value else 0.0
        elif "last name" in label_lower or "family name" in label_lower:
            value = profile.get("last_name") or (
                " ".join(profile.get("name", "").split(" ")[1:]) if profile.get("name") else ""
            )
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
    can_auto_submit = not any(
        field.confidence < 0.9 or (field.sensitive and not field.approved)
        for field in plans
    )
    return FormFillPlan(fields=plans, can_auto_submit=can_auto_submit)


def render_playwright_fill_script(plan: FormFillPlan, application_url: str | None = None) -> str:
    lines = [
        'const { chromium } = require("playwright");',
        "",
        "async function main() {",
        "  let browser = null;",
        "  try {",
        "    browser = await chromium.launch({ headless: false });",
        "    const page = await browser.newPage();",
    ]
    if application_url:
        lines.append(f"    await page.goto({json.dumps(application_url)});")

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
                f"    await page.getByLabel({json.dumps(field_item.label)}).setInputFiles({json.dumps(field_item.value)});"
            )
        elif field_item.action == "select":
            lines.append(
                f"    await page.getByLabel({json.dumps(field_item.label)}).selectOption({{ label: {json.dumps(field_item.value)} }});"
            )
        else:
            lines.append(
                f"    await page.getByLabel({json.dumps(field_item.label)}).fill({json.dumps(field_item.value)});"
            )

    lines.extend(
        [
            f"    console.log('Review required fields:', {json.dumps(plan.review_required_fields)});",
            f"    console.log('Submit gate:', {json.dumps(plan.submit_gate_reason)});",
            "    console.log('Snapshot fill complete; live runtime controls automatic submission.');",
            "  } finally {",
            "    if (browser) {",
            "      await browser.close();",
            "    }",
            "  }",
            "}",
            "",
            "main().catch((error) => {",
            "  console.error('Form fill failed:', error && error.message ? error.message : error);",
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
        "  let browser = null;",
        "  try {",
        "    browser = await chromium.launch({ headless: false });",
        "    const page = await browser.newPage();",
    ]
    if application_url:
        lines.append(f"    await page.goto({json.dumps(application_url)});")
    lines.extend(
        [
            "    const fields = await page.evaluate(() => {",
            '    const controls = Array.from(document.querySelectorAll("input, textarea, select"));',
            "    const textForIds = (ids) =>",
            "      (ids || '')",
            "        .split(/\\s+/)",
            "        .map((id) => id && document.getElementById ? document.getElementById(id) : null)",
            "        .filter((node) => node && node.textContent)",
            "        .map((node) => node.textContent.trim())",
            "        .filter(Boolean)",
            "        .join(' ');",
            "    const labelFor = (control) => {",
            "      if (control.id) {",
            "        const explicit = Array.from(document.querySelectorAll('label')).find((label) =>",
            "          label.htmlFor === control.id || label.getAttribute('for') === control.id",
            "        );",
            "        if (explicit && explicit.textContent) return explicit.textContent.trim();",
            "      }",
            "      const wrapping = control.closest('label');",
            "      if (wrapping && wrapping.textContent) return wrapping.textContent.trim();",
            "      const labelledBy = textForIds(control.getAttribute('aria-labelledby'));",
            "      if (labelledBy) return labelledBy;",
            "      const describedBy = textForIds(control.getAttribute('aria-describedby'));",
            "      return control.getAttribute('aria-label') || control.getAttribute('placeholder') || describedBy || control.name || '';",
            "    };",
            "    return controls.map((control) => ({",
            "      label: labelFor(control),",
            "      type: control.getAttribute('type') || control.tagName.toLowerCase(),",
            "      id: control.id || '',",
            "      name: control.name || '',",
            "      required: Boolean(control.required),",
            "      options: control.tagName.toLowerCase() === 'select'",
            "        ? Array.from(control.options).map((option) => option.textContent.trim()).filter(Boolean)",
            "        : [],",
            "    })).filter((field) => field.label);",
            "    });",
            f"    fs.writeFileSync({json.dumps(output_path)}, JSON.stringify(fields, null, 2));",
            f"    console.log('Wrote form snapshot to {output_path}');",
            "    console.log('Review the snapshot before using it for guarded form filling.');",
            "  } finally {",
            "    if (browser) {",
            "      await browser.close();",
            "    }",
            "  }",
            "}",
            "",
            "main().catch((error) => {",
            "  console.error('Form snapshot failed:', error && error.message ? error.message : error);",
            "  process.exit(1);",
            "});",
        ]
    )
    return "\n".join(lines) + "\n"
