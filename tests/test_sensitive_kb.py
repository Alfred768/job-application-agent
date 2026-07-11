from job_agent.forms import build_form_fill_plan, inspect_form_snapshot, render_playwright_fill_script
from job_agent.sensitive_kb import (
    match_sensitive_answer,
    render_sensitive_kb_template,
    resolve_sensitive_answer,
)


def test_kb_template_covers_standard_sensitive_fields():
    kb = render_sensitive_kb_template()

    assert "work_authorization" in kb
    assert "sponsorship" in kb
    assert "salary" in kb
    assert "relocation" in kb
    # entries default to unapproved with empty answer
    assert kb["salary"]["approved"] is False
    assert kb["salary"]["answer"] == ""
    assert kb["salary"]["patterns"]


def test_match_sensitive_answer_only_uses_approved_entries():
    kb = render_sensitive_kb_template()
    # unapproved -> no answer
    assert match_sensitive_answer("Desired Salary", kb) is None

    kb["salary"]["answer"] = "120000"
    kb["salary"]["approved"] = True
    assert match_sensitive_answer("Desired Salary", kb) == "120000"
    assert match_sensitive_answer("What is your salary expectation?", kb) == "120000"
    # unrelated label -> None
    assert match_sensitive_answer("First Name", kb) is None


def test_resolve_sensitive_answer_uses_legacy_real_values_not_placeholders():
    profile = {
        "salary": "120000",  # real legacy value -> approved
        "work_authorization": "Needs review",  # placeholder -> ignored
    }
    assert resolve_sensitive_answer("Desired Salary", profile) == "120000"
    assert resolve_sensitive_answer("Are you authorized to work?", profile) is None


def test_sensitive_field_auto_fills_from_approved_kb():
    fields = inspect_form_snapshot('[{"label": "Desired Salary"}]')
    profile = {
        "sensitive_answers": {
            "salary": {"patterns": ["salary"], "answer": "120000", "approved": True},
        }
    }

    plan = build_form_fill_plan(fields, profile)
    field = plan.fields[0]

    assert field.sensitive is True
    assert field.approved is True
    assert field.confidence == 1.0
    assert field.value == "120000"
    assert "Desired Salary" not in plan.review_required_fields
    # the fill script actually fills it
    script = render_playwright_fill_script(plan)
    assert 'await page.getByLabel("Desired Salary").fill("120000");' in script


def test_sensitive_field_without_approved_kb_stays_review_required():
    fields = inspect_form_snapshot('[{"label": "Desired Salary"}]')
    profile = {"salary": "Needs review"}  # placeholder, not approved

    plan = build_form_fill_plan(fields, profile)
    field = plan.fields[0]

    assert field.sensitive is True
    assert field.approved is False
    assert field.confidence < 0.9
    assert "Desired Salary" in plan.review_required_fields
    script = render_playwright_fill_script(plan)
    assert "120000" not in script
    assert ".fill(" not in script.split("Review required")[0]
