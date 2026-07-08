from job_agent.forms import FieldPlan, FormFillPlan, render_playwright_fill_script


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
