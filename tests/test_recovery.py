from __future__ import annotations

from hello_agents.career.recovery import (
    JobApplicationRecoveryPlanner,
    attach_recovery_plan,
    classify_processing_failure,
    recovery_plan_to_dict,
)


def test_recovery_planner_maps_anti_spam_to_tenant_cooldown():
    plan = JobApplicationRecoveryPlanner(
        {"JOB_AGENT_ANTI_SPAM_COOLDOWN_HOURS": "6"}
    )("submission_blocked_by_anti_spam", {})

    assert plan is not None
    assert plan.strategy == "tenant_cooldown_then_scoped_resume"
    assert plan.retry_after_seconds == 6 * 3600
    assert plan.retry_scope == "single_application"
    assert "duplicate_check" in plan.evidence_required


def test_recovery_planner_uses_supported_captcha_solver_when_configured():
    planner = JobApplicationRecoveryPlanner(
        {
            "CAPMONSTER_SOLVE_CAPTCHA": "true",
            "CAPMONSTER_API_KEY": "configured-secret",
        }
    )

    plan = planner(
        "submission_processing_error",
        {"processing_error_kind": "captcha_failed"},
    )

    assert plan is not None
    assert plan.strategy == "captcha_resolution"
    assert [action.action for action in plan.actions] == [
        "validate_captcha_challenge",
        "solve_supported_captcha_once",
        "resume_after_challenge",
    ]
    assert all(action.requires_user is False for action in plan.actions)
    assert "configured-secret" not in str(recovery_plan_to_dict(plan))


def test_recovery_planner_routes_unsupported_captcha_to_candidate():
    plan = JobApplicationRecoveryPlanner({})(
        "submission_processing_error",
        {"processing_error_kind": "captcha_unsupported"},
    )

    assert plan is not None
    assert plan.actions[1].action == "complete_captcha_interactively"
    assert plan.actions[1].requires_user is True


def test_recovery_planner_automates_configured_email_and_account_steps(
    tmp_path,
):
    token = tmp_path / "gmail-token.json"
    password_file = tmp_path / "account-password"
    token.write_text("{}")
    password_file.write_text("not-read-by-planner")
    planner = JobApplicationRecoveryPlanner(
        {
            "JOB_AGENT_GMAIL_TOKEN_FILE": str(token),
            "JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE": str(password_file),
        }
    )

    email_plan = planner("email_verification_required", {})
    account_plan = planner("candidate_account_required", {})

    assert email_plan is not None
    assert account_plan is not None
    assert all(action.automatic for action in email_plan.actions)
    assert all(action.automatic for action in account_plan.actions)
    serialized = str(recovery_plan_to_dict(account_plan))
    assert "not-read-by-planner" not in serialized


def test_recovery_planner_requests_missing_candidate_facts():
    plan = JobApplicationRecoveryPlanner({})(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": "Work authorization",
                    "reason": "profile has no approved answer",
                    "sensitive": True,
                    "blocking": True,
                }
            ]
        },
    )

    assert plan is not None
    assert plan.strategy == "candidate_fact_resolution"
    assert plan.actions[0].requires_user is True
    assert plan.actions[0].parameters == {
        "field_labels": ["Work authorization"]
    }


def test_recovery_planner_routes_explicit_candidate_fact_gate_to_human():
    label = "Are you excited to work in-office five days a week?"
    plan = JobApplicationRecoveryPlanner({})(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": label,
                    "reason": "candidate fact needs explicit approved answer",
                    "sensitive": False,
                    "blocking": True,
                }
            ]
        },
    )

    assert plan is not None
    assert plan.strategy == "candidate_fact_resolution"
    assert plan.actions[0].requires_user is True
    assert plan.actions[0].parameters == {"field_labels": [label]}


def test_recovery_planner_routes_saved_answer_option_mismatch_to_candidate_fact():
    label = "Are you currently located in the San Francisco, Bay Area?"
    plan = JobApplicationRecoveryPlanner({})(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": label,
                    "reason": "no option matches saved answer",
                    "sensitive": False,
                    "blocking": True,
                }
            ]
        },
    )

    assert plan is not None
    assert plan.strategy == "candidate_fact_resolution"
    assert plan.actions[0].parameters == {"field_labels": [label]}


def test_recovery_planner_routes_user_authored_reading_answer_to_candidate():
    label = (
        "What's the most interesting paper, blog post, or documentation "
        "you've read in the past month?"
    )
    plan = JobApplicationRecoveryPlanner({})(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": label,
                    "reason": "unmapped field",
                    "sensitive": False,
                    "blocking": True,
                }
            ]
        },
    )

    assert plan is not None
    assert plan.strategy == "candidate_fact_resolution"
    assert plan.actions[0].requires_user is True
    assert plan.actions[0].parameters == {"field_labels": [label]}
    assert "not code or an LLM" in plan.reason


def test_recovery_planner_routes_high_school_history_to_candidate():
    labels = ["High School Name", "Year of High School Graduation"]
    plan = JobApplicationRecoveryPlanner({})(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": labels[0],
                    "reason": "unmapped field",
                    "sensitive": False,
                    "blocking": True,
                },
                {
                    "label": labels[1],
                    "reason": "no matching option / answer",
                    "sensitive": False,
                    "blocking": True,
                },
            ]
        },
    )

    assert plan is not None
    assert plan.strategy == "candidate_fact_resolution"
    assert plan.actions[0].parameters == {"field_labels": labels}


def test_recovery_planner_routes_candidate_preferences_to_candidate():
    label = "What are your preferred Palantir product(s)?"
    plan = JobApplicationRecoveryPlanner({})(
        "autofill_completed_blocked",
        {
            "review_items": [
                {
                    "label": label,
                    "reason": "unmapped field",
                    "sensitive": False,
                    "blocking": True,
                }
            ]
        },
    )

    assert plan is not None
    assert plan.strategy == "candidate_fact_resolution"
    assert plan.actions[0].parameters == {"field_labels": [label]}


def test_recovery_planner_reconciles_unconfirmed_click_without_retry():
    plan = JobApplicationRecoveryPlanner({})(
        "submit_clicked_unconfirmed",
        {},
    )

    assert plan is not None
    assert plan.strategy == "confirmation_reconciliation"
    assert plan.retry_allowed is False
    assert all(action.automatic for action in plan.actions)


def test_attach_recovery_plan_handles_interrupted_unknown_outcome():
    record = {
        "status": "autofill_failed",
        "error": "execution_interrupted_unconfirmed",
    }

    assert attach_recovery_plan(record) is record
    assert (
        record["recovery_plan"]["strategy"]
        == "confirmation_reconciliation"
    )
    assert record["recovery_plan"]["retry_allowed"] is False


def test_processing_failure_classifier_is_privacy_safe():
    assert (
        classify_processing_failure(
            "CapMonster CAPTCHA: unsupported (ERROR_TASK_NOT_SUPPORTED)"
        )
        == "captcha_unsupported"
    )
    assert classify_processing_failure("HTTP 429 Too Many Requests") == "rate_limited"
    assert classify_processing_failure("unknown site message") == "site_processing_error"
