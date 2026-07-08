from job_agent.forms import FieldPlan, FormFillPlan


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
