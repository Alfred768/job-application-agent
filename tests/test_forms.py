import json
import shutil
import subprocess

import pytest

from job_agent.forms import (
    FieldPlan,
    FormFillPlan,
    build_form_fill_plan,
    inspect_form_snapshot,
    render_playwright_form_snapshot_script,
    render_playwright_fill_script,
)


def test_form_plan_enables_automatic_submit():
    plan = FormFillPlan(
        fields=[FieldPlan(label="Email", value="user@example.com", sensitive=False)]
    )

    assert plan.can_auto_submit is True
    assert "automatic" in plan.submit_gate_reason.lower()


def test_sensitive_fields_are_marked_for_review():
    plan = FormFillPlan(
        fields=[FieldPlan(label="Sponsorship", value="Needs review", sensitive=True)]
    )

    assert plan.review_required_fields == ["Sponsorship"]


def test_render_playwright_fill_script_only_fills_safe_fields():
    plan = FormFillPlan(
        fields=[
            FieldPlan(label="Email", value="gaoyi@example.com", sensitive=False),
            FieldPlan(label="Do you require visa sponsorship?", value="Needs review", sensitive=True),
        ]
    )

    script = render_playwright_fill_script(plan, application_url="https://jobs.example.com/apply")

    assert 'await page.goto("https://jobs.example.com/apply");' in script
    assert 'await page.getByLabel("Email").fill("gaoyi@example.com");' in script
    assert "Do you require visa sponsorship?" in script
    assert "waitForManualReview" not in script
    assert "await browser.close();" in script
    assert ".click(" not in script
    assert ".press(" not in script


def test_render_playwright_fill_script_uploads_approved_resume_file():
    fields = inspect_form_snapshot('[{"label": "Resume", "type": "file", "required": true}]')
    plan = build_form_fill_plan(fields, {"resume_file": "/tmp/tailored-resume.pdf"})

    script = render_playwright_fill_script(plan, application_url="https://jobs.example.com/apply")

    assert 'await page.getByLabel("Resume").setInputFiles("/tmp/tailored-resume.pdf");' in script
    assert ".click(" not in script


def test_form_plan_uploads_greenhouse_resume_field_when_visible_label_is_attach():
    fields = inspect_form_snapshot(
        '[{"label": "Attach", "type": "file", "id": "resume", "required": true}]'
    )

    plan = build_form_fill_plan(fields, {"resume_file": "/tmp/source-resume.pdf"})
    field = plan.fields[0]

    assert field.action == "upload"
    assert field.value == "/tmp/source-resume.pdf"
    assert field.approved is True
    assert field.confidence == 1.0


def test_form_plan_rejects_non_pdf_resume_upload_path():
    fields = inspect_form_snapshot('[{"label": "Resume", "type": "file", "required": true}]')

    plan = build_form_fill_plan(fields, {"resume_file": "/tmp/tailored-resume.docx"})
    field = plan.fields[0]

    assert field.action == "upload"
    assert field.approved is False
    assert field.confidence == 0.0
    assert plan.review_required_fields == ["Resume"]


def test_render_playwright_fill_script_executes_and_waits_for_manual_review(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for fill script execution test")

    plan = FormFillPlan(
        fields=[
            FieldPlan(label="Email", value="gaoyi@example.com", sensitive=False),
            FieldPlan(label="Do you require visa sponsorship?", value="Needs review", sensitive=True),
        ]
    )
    script_path = tmp_path / "fill-form.js"
    script_path.write_text(render_playwright_fill_script(plan, application_url="https://jobs.example.com/apply"))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function control(label) {
  return {
    async fill(value) {
      values[label] = value;
      console.log('filled ' + label + '=' + value);
    },
    async selectOption(option) {
      values[label] = option.label;
      console.log('selected ' + label + '=' + option.label);
    },
    async setInputFiles(value) {
      values[label] = value;
      console.log('uploaded ' + label + '=' + value);
    },
  };
}
module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake launch headless=' + options.headless);
      return {
        async newPage() {
          return {
            async goto(url) { console.log('goto ' + url); },
            getByLabel(label) { return control(label); },
          };
        },
        async close() { console.log('fake browser closed after manual review'); },
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "fake launch headless=false" in result.stdout
    assert "goto https://jobs.example.com/apply" in result.stdout
    assert "filled Email=gaoyi@example.com" in result.stdout
    assert "Needs review" not in result.stdout
    assert "Submit gate:" in result.stdout
    assert "Browser remains open for manual review" not in result.stdout
    assert "fake browser closed after manual review" in result.stdout


def test_render_playwright_fill_script_closes_browser_on_failure(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for fill script execution test")

    plan = FormFillPlan(
        fields=[
            FieldPlan(label="Email", value="gaoyi@example.com", sensitive=False),
        ]
    )
    script_path = tmp_path / "fill-form.js"
    script_path.write_text(render_playwright_fill_script(plan, application_url="https://jobs.example.com/apply"))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake launch headless=' + options.headless);
      return {
        async newPage() {
          return {
            async goto(url) { console.log('goto ' + url); },
            getByLabel(label) {
              return {
                async fill(value) {
                  console.log('fill attempted ' + label + '=' + value);
                  throw new Error('field is detached');
                },
              };
            },
          };
        },
        async close() { console.log('fake browser closed after failure'); },
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "fill attempted Email=gaoyi@example.com" in result.stdout
    assert "fake browser closed after failure" in result.stdout
    assert "Submit gate:" not in result.stdout
    assert "Form fill failed: field is detached" in result.stderr


def test_file_field_requires_review_when_resume_file_missing():
    fields = inspect_form_snapshot('[{"label": "Resume", "type": "file", "required": true}]')
    plan = build_form_fill_plan(fields, {})

    assert plan.review_required_fields == ["Resume"]


def test_form_plan_fills_common_low_risk_profile_fields():
    fields = inspect_form_snapshot(
        """[
          {"label": "Portfolio URL"},
          {"label": "Personal Website"},
          {"label": "Current Location"},
          {"label": "Cover Letter"},
          {"label": "Desired Salary"},
          {"label": "Are you authorized to work in the United States?"}
        ]"""
    )

    plan = build_form_fill_plan(
        fields,
        {
            "portfolio": "https://gaoyi.example.com",
            "website": "https://gaoyi.example.com",
            "location": "New York, NY",
            "cover_letter": "I am excited to apply because my agent work matches this role.",
            "salary": "Needs review",
            "work_authorization": "Needs review",
        },
    )

    by_label = {field.label: field for field in plan.fields}
    assert by_label["Portfolio URL"].value == "https://gaoyi.example.com"
    assert by_label["Personal Website"].value == "https://gaoyi.example.com"
    assert by_label["Current Location"].value == "New York, NY"
    assert by_label["Cover Letter"].value.startswith("I am excited")
    assert by_label["Desired Salary"].sensitive is True
    assert by_label["Desired Salary"].confidence < 0.9
    assert by_label["Are you authorized to work in the United States?"].sensitive is True


def test_form_plan_treats_legal_name_as_low_risk_identity_field():
    fields = inspect_form_snapshot(
        """[
          {"label": "Legal First Name"},
          {"label": "Legal Last Name"},
          {"label": "Legal Attestation"}
        ]"""
    )
    profile = {
        "name": "Gaoyi Wu",
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["legal attestation"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }

    plan = build_form_fill_plan(fields, profile)
    by_label = {field.label: field for field in plan.fields}

    assert by_label["Legal First Name"].sensitive is False
    assert by_label["Legal First Name"].value == "Gaoyi"
    assert by_label["Legal Last Name"].sensitive is False
    assert by_label["Legal Last Name"].value == "Wu"
    assert by_label["Legal Attestation"].sensitive is True
    assert by_label["Legal Attestation"].value == "Yes"


def test_form_plan_keeps_citizenship_and_clearance_review_required_without_approved_kb():
    fields = inspect_form_snapshot(
        """[
          {"label": "Are you a U.S. citizen?"},
          {"label": "Do you have security clearance?"}
        ]"""
    )
    profile = {
        "answers": {
            "Are you a U.S. citizen?": "Yes",
            "Do you have security clearance?": "Yes",
        }
    }

    plan = build_form_fill_plan(fields, profile)
    by_label = {field.label: field for field in plan.fields}

    assert by_label["Are you a U.S. citizen?"].sensitive is True
    assert by_label["Are you a U.S. citizen?"].confidence < 0.9
    assert by_label["Do you have security clearance?"].sensitive is True
    assert by_label["Do you have security clearance?"].confidence < 0.9
    assert plan.review_required_fields == [
        "Are you a U.S. citizen?",
        "Do you have security clearance?",
    ]
    script = render_playwright_fill_script(plan)
    assert 'getByLabel("Are you a U.S. citizen?").fill("Yes")' not in script
    assert 'getByLabel("Do you have security clearance?").fill("Yes")' not in script


def test_form_plan_fills_citizenship_and_clearance_from_approved_kb():
    fields = inspect_form_snapshot(
        """[
          {"label": "Are you a U.S. citizen?"},
          {"label": "Do you have security clearance?"}
        ]"""
    )
    profile = {
        "sensitive_answers": {
            "citizenship": {"patterns": ["citizen", "citizenship"], "answer": "No", "approved": True},
            "security_clearance": {"patterns": ["security clearance", "clearance"], "answer": "No active clearance", "approved": True},
        }
    }

    plan = build_form_fill_plan(fields, profile)
    by_label = {field.label: field for field in plan.fields}

    assert by_label["Are you a U.S. citizen?"].value == "No"
    assert by_label["Are you a U.S. citizen?"].approved is True
    assert by_label["Do you have security clearance?"].value == "No active clearance"
    assert by_label["Do you have security clearance?"].approved is True
    assert plan.review_required_fields == []


def test_form_plan_uses_approved_exact_label_answers_without_bypassing_sensitive_fields():
    fields = inspect_form_snapshot(
        """[
          {"label": "Have you built production AI agents?"},
          {"label": "How did you hear about us?", "type": "select", "options": ["LinkedIn", "Company website"]},
          {"label": "Desired Salary"}
        ]"""
    )

    plan = build_form_fill_plan(
        fields,
        {
            "answers": {
                "Have you built production AI agents?": "Yes, I built agent workflows with guarded tools.",
                "How did you hear about us?": "Company website",
                "Desired Salary": "Needs review",
            }
        },
    )

    by_label = {field.label: field for field in plan.fields}
    assert by_label["Have you built production AI agents?"].value.startswith("Yes")
    assert by_label["Have you built production AI agents?"].confidence == 1.0
    assert by_label["How did you hear about us?"].action == "select"
    assert by_label["How did you hear about us?"].value == "Company website"
    assert by_label["Desired Salary"].value == "Needs review"
    assert by_label["Desired Salary"].sensitive is True
    assert by_label["Desired Salary"].confidence < 0.9

    script = render_playwright_fill_script(plan)
    assert 'await page.getByLabel("Have you built production AI agents?").fill("Yes, I built agent workflows with guarded tools.");' in script
    assert 'await page.getByLabel("How did you hear about us?").selectOption({ label: "Company website" });' in script
    assert "Desired Salary" in script
    assert "Needs review" not in script


def test_form_plan_selects_verbose_option_label_from_short_approved_answer():
    fields = inspect_form_snapshot(
        json.dumps(
            [
                {
                    "label": "Are you authorized to work in the United States?",
                    "type": "select",
                    "options": [
                        "Yes, I am legally authorized to work in the United States",
                        "No, I require sponsorship",
                    ],
                },
                {
                    "label": "How did you hear about us?",
                    "type": "select",
                    "options": ["I found this role on the company website", "Referral"],
                },
            ]
        )
    )
    profile = {
        "answers": {"How did you hear about us?": "Company website"},
        "sensitive_answers": {
            "work_authorization": {
                "patterns": ["authorized to work"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }

    plan = build_form_fill_plan(fields, profile)
    by_label = {field.label: field for field in plan.fields}

    assert (
        by_label["Are you authorized to work in the United States?"].value
        == "Yes, I am legally authorized to work in the United States"
    )
    assert (
        by_label["How did you hear about us?"].value
        == "I found this role on the company website"
    )


def test_render_playwright_form_snapshot_script_only_inspects_fields():
    script = render_playwright_form_snapshot_script(
        application_url="https://jobs.example.com/apply",
        output_path="form-snapshot.json",
    )

    assert 'await page.goto("https://jobs.example.com/apply");' in script
    assert 'fs.writeFileSync("form-snapshot.json"' in script
    assert "querySelectorAll" in script
    assert "input, textarea, select" in script
    assert "aria-labelledby" in script
    assert "aria-describedby" in script
    assert "querySelectorAll('label')" in script
    assert "label[for=" not in script
    assert ".fill(" not in script
    assert ".setInputFiles(" not in script
    assert ".click(" not in script
    assert ".press(" not in script
    assert "await browser.close();" in script


def test_render_playwright_form_snapshot_script_executes_and_closes_browser(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for snapshot script execution test")

    snapshot_path = tmp_path / "form-snapshot.json"
    script_path = tmp_path / "capture-form-snapshot.js"
    script_path.write_text(
        render_playwright_form_snapshot_script(
            application_url="https://jobs.example.com/apply",
            output_path=str(snapshot_path),
        )
    )

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const controls = [
  {
    id: 'email:field"1',
    tagName: 'INPUT',
    required: true,
    options: [],
    getAttribute(name) {
      if (name === 'type') return 'email';
      if (name === 'aria-label') return '';
      if (name === 'placeholder') return '';
      return '';
    },
    closest() { return null; },
    name: 'email',
  },
  {
    id: '',
    tagName: 'SELECT',
    required: false,
    options: [{ textContent: 'Company website' }],
    getAttribute(name) {
      if (name === 'aria-label') return 'How did you hear about us?';
      return '';
    },
    closest() { return null; },
    name: 'source',
  },
  {
    id: '',
    tagName: 'INPUT',
    required: false,
    options: [],
    getAttribute(name) {
      if (name === 'type') return 'url';
      if (name === 'aria-labelledby') return 'portfolio-label helper-text';
      if (name === 'aria-label') return '';
      if (name === 'placeholder') return '';
      return '';
    },
    closest() { return null; },
    name: 'portfolio',
  },
  {
    id: '',
    tagName: 'INPUT',
    required: false,
    options: [],
    getAttribute(name) {
      if (name === 'type') return 'email';
      if (name === 'aria-labelledby') return '';
      if (name === 'aria-describedby') return 'alternate-email-help';
      if (name === 'aria-label') return '';
      if (name === 'placeholder') return '';
      return '';
    },
    closest() { return null; },
    name: 'field_123',
  },
];

global.document = {
  querySelector(selector) {
    return null;
  },
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return controls;
    if (selector === 'label') {
      return [
        {
          htmlFor: 'email:field"1',
          textContent: 'Email',
          getAttribute(name) {
            if (name === 'for') return 'email:field"1';
            return '';
          },
        },
      ];
    }
    return [];
  },
  getElementById(id) {
    if (id === 'portfolio-label') return { textContent: 'Portfolio URL' };
    if (id === 'helper-text') return { textContent: 'optional' };
    if (id === 'alternate-email-help') return { textContent: 'Alternate email' };
    return null;
  },
};

module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake snapshot launch headless=' + options.headless);
      return {
        async newPage() {
          return {
            async goto(url) { console.log('snapshot goto ' + url); },
            async evaluate(fn) { return fn(); },
          };
        },
        async close() { console.log('fake snapshot browser closed'); },
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "snapshot goto https://jobs.example.com/apply" in result.stdout
    assert "fake snapshot browser closed" in result.stdout
    assert "Wrote form snapshot" in result.stdout
    snapshot = __import__("json").loads(snapshot_path.read_text())
    assert snapshot == [
        {
            "label": "Email",
            "type": "email",
            "id": 'email:field"1',
            "name": "email",
            "required": True,
            "options": [],
        },
        {
            "label": "How did you hear about us?",
            "type": "select",
            "id": "",
            "name": "source",
            "required": False,
            "options": ["Company website"],
        },
        {
            "label": "Portfolio URL optional",
            "type": "url",
            "id": "",
            "name": "portfolio",
            "required": False,
            "options": [],
        },
        {
            "label": "Alternate email",
            "type": "email",
            "id": "",
            "name": "field_123",
            "required": False,
            "options": [],
        },
    ]


def test_render_playwright_form_snapshot_script_closes_browser_on_failure(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for snapshot script execution test")

    snapshot_path = tmp_path / "form-snapshot.json"
    script_path = tmp_path / "capture-form-snapshot.js"
    script_path.write_text(
        render_playwright_form_snapshot_script(
            application_url="https://jobs.example.com/apply",
            output_path=str(snapshot_path),
        )
    )

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake snapshot launch headless=' + options.headless);
      return {
        async newPage() {
          return {
            async goto(url) { console.log('snapshot goto ' + url); },
            async evaluate(fn) { throw new Error('page crashed during inspect'); },
          };
        },
        async close() { console.log('fake snapshot browser closed after failure'); },
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "snapshot goto https://jobs.example.com/apply" in result.stdout
    assert "fake snapshot browser closed after failure" in result.stdout
    assert "Wrote form snapshot" not in result.stdout
    assert not snapshot_path.exists()
    assert "Form snapshot failed: page crashed during inspect" in result.stderr
