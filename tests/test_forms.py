from job_agent.forms import (
    FieldPlan,
    FormFillPlan,
    build_form_fill_plan,
    inspect_form_snapshot,
    render_playwright_form_snapshot_script,
    render_playwright_fill_script,
)


def test_form_plan_requires_manual_submit():
    plan = FormFillPlan(
        fields=[FieldPlan(label="Email", value="user@example.com", sensitive=False)]
    )

    assert plan.can_auto_submit is False
    assert "manual" in plan.submit_gate_reason.lower()


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
    assert ".click(" not in script
    assert ".press(" not in script


def test_render_playwright_fill_script_uploads_approved_resume_file():
    fields = inspect_form_snapshot('[{"label": "Resume", "type": "file", "required": true}]')
    plan = build_form_fill_plan(fields, {"resume_file": "/tmp/tailored-resume.pdf"})

    script = render_playwright_fill_script(plan, application_url="https://jobs.example.com/apply")

    assert 'await page.getByLabel("Resume").setInputFiles("/tmp/tailored-resume.pdf");' in script
    assert ".click(" not in script


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


def test_render_playwright_form_snapshot_script_only_inspects_fields():
    script = render_playwright_form_snapshot_script(
        application_url="https://jobs.example.com/apply",
        output_path="form-snapshot.json",
    )

    assert 'await page.goto("https://jobs.example.com/apply");' in script
    assert 'fs.writeFileSync("form-snapshot.json"' in script
    assert "querySelectorAll" in script
    assert "input, textarea, select" in script
    assert ".fill(" not in script
    assert ".setInputFiles(" not in script
    assert ".click(" not in script
    assert ".press(" not in script
