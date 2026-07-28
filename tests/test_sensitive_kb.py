import json
from importlib import resources
from pathlib import Path

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
    assert "citizenship" in kb
    assert "security_clearance" in kb
    # entries default to unapproved with empty answer
    assert kb["salary"]["approved"] is False
    assert kb["salary"]["answer"] == ""
    assert kb["salary"]["patterns"]
    assert kb["citizenship"]["approved"] is False
    assert kb["security_clearance"]["approved"] is False


def test_examples_sensitive_answers_matches_safe_template_defaults():
    example = json.loads(Path("examples/sensitive-answers.json").read_text())
    packaged = json.loads(
        resources.files("job_agent.example_data").joinpath("sensitive-answers.json").read_text()
    )
    template = render_sensitive_kb_template()

    assert packaged == example
    assert set(example) == set(template)
    for key, entry in example.items():
        assert entry["label"] == template[key]["label"]
        assert entry["patterns"] == template[key]["patterns"]
        assert entry["answer"] == ""
        assert entry["approved"] is False


def test_packaged_examples_match_top_level_fixtures():
    resource_root = resources.files("job_agent.example_data")
    for filename in [
        "offline-sources.json",
        "offline-jobs.xml",
        "sample-resume.md",
        "profile.json",
        "form-snapshot.json",
        "sensitive-answers.json",
    ]:
        packaged = resource_root.joinpath(filename).read_text()
        top_level = Path("examples", filename).read_text()
        if filename.endswith(".json"):
            assert json.loads(packaged) == json.loads(top_level)
        else:
            assert packaged == top_level


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


def test_resolve_sensitive_answer_reuses_explicit_profile_facts_without_duplicate_approval():
    profile = {
        "work_authorization": "Yes",
        "relocation": "No",
        "start_date": "2026-09-01",
        "citizenship": "No",
        "sensitive_answers": {
            "relocation": {
                "patterns": ["relocate"],
                "answer": "Yes",
                "approved": False,
            }
        },
    }

    assert resolve_sensitive_answer("Are you authorized to work?", profile) == "Yes"
    assert resolve_sensitive_answer("Are you willing to relocate?", profile) == "No"
    assert resolve_sensitive_answer("What is your earliest start date?", profile) == "2026-09-01"
    assert resolve_sensitive_answer("Are you a U.S. citizen?", profile) == "No"


def test_resolve_sensitive_answer_rejects_placeholder_profile_facts():
    profile = {
        "work_authorization": "Needs review",
        "relocation": "TBD",
        "citizenship": "",
    }

    assert resolve_sensitive_answer("Are you authorized to work?", profile) is None
    assert resolve_sensitive_answer("Are you willing to relocate?", profile) is None
    assert resolve_sensitive_answer("Are you a U.S. citizen?", profile) is None


def test_match_sensitive_answer_handles_stemmed_and_reordered_patterns():
    kb = {
        "work_authorization": {
            "patterns": ["authorized to work"],
            "answer": "Yes",
            "approved": True,
        },
        "sponsorship": {
            "patterns": ["require sponsorship"],
            "answer": "No",
            "approved": True,
        },
    }

    assert match_sensitive_answer("Work Authorization", kb) == "Yes"
    assert match_sensitive_answer("Authorization Status", kb) == "Yes"
    assert match_sensitive_answer("Visa Sponsorship", kb) == "No"
    assert match_sensitive_answer("First Name", kb) is None


def test_match_sensitive_answer_does_not_overmatch_legal_name_or_work_location():
    kb = {
        "legal_attestation": {
            "patterns": ["legal attestation", "i attest"],
            "answer": "Yes",
            "approved": True,
        },
        "work_authorization": {
            "patterns": ["authorized to work"],
            "answer": "Yes",
            "approved": True,
        },
    }

    assert match_sensitive_answer("Legal First Name", kb) is None
    assert match_sensitive_answer("Legal Last Name", kb) is None
    assert match_sensitive_answer("Work Location", kb) is None
    assert match_sensitive_answer("Legal Attestation", kb) == "Yes"
    assert match_sensitive_answer("Authorization Status", kb) == "Yes"


def test_background_check_pattern_does_not_match_demographic_background():
    kb = {
        "legal_attestation": {
            "patterns": ["background check"],
            "answer": "Yes",
            "approved": True,
        },
        "eeo_ethnicity": {
            "patterns": ["ethnicity", "race"],
            "answer": "Asian",
            "approved": True,
        },
    }

    assert (
        match_sensitive_answer(
            "How would you describe your racial/ethnic background? (mark all that apply)*",
            kb,
        )
        == "Asian"
    )
    assert match_sensitive_answer("I authorize the company to run a background check", kb) == "Yes"


def test_work_authorization_does_not_overmatch_security_clearance_eligibility():
    kb = {
        "work_authorization": {
            "patterns": ["authorized to work", "eligible to work"],
            "answer": "Yes",
            "approved": True,
        }
    }

    assert match_sensitive_answer("Are you legally authorized to work in the United States?", kb) == "Yes"
    assert match_sensitive_answer("Are you eligible to obtain the security clearance specified in the job description?", kb) is None


def test_country_specific_work_authorization_does_not_cross_match():
    kb = {
        "work_authorization_us": {
            "patterns": ["authorized to work in the United States"],
            "answer": "Yes",
            "approved": True,
        },
        "work_authorization_canada": {
            "patterns": ["authorized to work in Canada"],
            "answer": "No",
            "approved": True,
        },
        "work_authorization_uk": {
            "patterns": ["authorized to work in the United Kingdom"],
            "answer": "No",
            "approved": True,
        },
    }

    assert match_sensitive_answer("Are you authorized to work in the US?", kb) == "Yes"
    assert match_sensitive_answer("Are you authorized to work in Canada?", kb) == "No"
    assert match_sensitive_answer("Are you authorized to work in the United Kingdom?", kb) == "No"
    assert match_sensitive_answer("Are you authorized to work in the country for which you are applying?", kb) is None


def test_match_sensitive_answer_supports_citizenship_and_clearance_families():
    kb = render_sensitive_kb_template()
    kb["citizenship"]["answer"] = "Needs review"
    kb["citizenship"]["approved"] = False
    kb["security_clearance"]["answer"] = "No active clearance"
    kb["security_clearance"]["approved"] = True

    assert match_sensitive_answer("Are you a U.S. citizen?", kb) is None

    kb["citizenship"]["answer"] = "No"
    kb["citizenship"]["approved"] = True

    assert match_sensitive_answer("Are you a U.S. citizen?", kb) == "No"
    assert match_sensitive_answer("Citizenship status", kb) == "No"
    assert match_sensitive_answer("Do you have security clearance?", kb) == "No active clearance"


def test_clearance_eligibility_no_takes_priority_over_clearance_role_interest_yes():
    kb = {
        "security_clearance_interest": {
            "patterns": [
                "roles that require top security clearance",
                "top security clearance",
                "security clearance roles",
            ],
            "answer": "Yes",
            "approved": True,
        },
        "security_clearance_eligibility": {
            "patterns": [
                "eligible to obtain the security clearance",
                "security clearance specified",
                "apply for and maintain a ts/sci security clearance",
                "ts/sci security clearance",
            ],
            "answer": "No",
            "approved": True,
        },
        "citizenship": {
            "patterns": ["citizen", "us citizen", "only us citizens will be considered"],
            "answer": "No",
            "approved": True,
        },
    }

    assert match_sensitive_answer("I am willing and able to apply for and maintain a TS/SCI security clearance.", kb) == "No"
    assert match_sensitive_answer("Due to contractual requirements, only US Citizens will be considered for this position.", kb) == "No"


def test_sponsorship_type_opt_takes_priority_only_for_option_style_question():
    kb = {
        "sponsorship": {
            "patterns": ["sponsorship", "employment visa status", "future require sponsorship"],
            "answer": "Yes",
            "approved": True,
        },
        "sponsorship_type": {
            "patterns": ["h1b opt", "e.g. h1b, opt", "employment visa status e g h1b opt"],
            "answer": "OPT",
            "approved": True,
        },
    }

    assert match_sensitive_answer("Will you now or in the future require sponsorship for employment visa status?", kb) == "Yes"
    assert (
        match_sensitive_answer(
            "Will you now or at any time in the future require sponsorship for employment visa status (e.g. H1B, OPT)?",
            kb,
        )
        == "OPT"
    )


def test_sponsorship_yes_no_question_is_not_overridden_by_visa_type_pattern():
    kb = {
        "sponsorship": {
            "patterns": ["sponsorship", "future require sponsorship"],
            "answer": "Yes",
            "approved": True,
        },
        "sponsorship_type": {
            "patterns": ["visa type", "status (e.g. h1b, opt)"],
            "answer": "OPT",
            "approved": True,
        },
    }

    assert (
        match_sensitive_answer(
            "Will you need employment visa sponsorship now or in the future to work in your current location?",
            kb,
        )
        == "Yes"
    )


def test_us_sponsorship_question_is_not_overridden_by_citizenship_rule():
    kb = {
        "sponsorship": {
            "patterns": ["sponsorship", "future require sponsorship"],
            "answer": "Yes",
            "approved": True,
        },
        "citizenship": {
            "patterns": ["u s citizen", "u.s. citizen", "only u.s. citizens"],
            "answer": "No",
            "approved": True,
        },
    }

    assert match_sensitive_answer(
        "Will you now or in the future require employer sponsorship to work in the U.S.?",
        kb,
    ) == "Yes"


def test_generic_legal_words_do_not_override_race_answer():
    kb = {
        "legal_attestation": {
            "patterns": ["i confirm that all information"],
            "answer": "Yes",
            "approved": True,
        },
        "eeo_ethnicity": {
            "patterns": ["ethnicity", "race"],
            "answer": "East Asian",
            "approved": True,
        },
    }

    assert (
        match_sensitive_answer(
            "What race and/or ethnic identities do you identify with? (please select all that apply)",
            kb,
        )
        == "East Asian"
    )


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
