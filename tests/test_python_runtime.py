import json
import inspect

import pytest

from job_agent import python_runtime


class _NonInteractiveStdin:
    def isatty(self):
        return False


def test_self_heal_passes_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("JOB_AGENT_SELF_HEAL_PASSES", raising=False)
    assert python_runtime._self_heal_passes() == 3

    monkeypatch.setenv("JOB_AGENT_SELF_HEAL_PASSES", "20")
    assert python_runtime._self_heal_passes() == 5

    monkeypatch.setenv("JOB_AGENT_SELF_HEAL_PASSES", "invalid")
    assert python_runtime._self_heal_passes() == 3


def test_watchdog_process_discovery_finds_playwright_driver(monkeypatch):
    child = _FakeProcess(4242, "/x/playwright/driver/package/cli.js run-driver")
    python_runtime._direct_child_pids = lambda pid: [4242]
    python_runtime._process_command_line = lambda pid: child.command
    assert python_runtime._playwright_driver_pids() == [4242]


def test_normalized_field_signature_is_stable_across_rerender_churn():
    first = [
        {"kind": "fill", "tag": "input", "type": "text", "id": "a", "name": "a", "label": "First Name", "required": True},
        {"kind": "combobox", "tag": "select", "type": None, "id": "b", "name": "b", "label": "Country", "required": True},
    ]
    rerendered = [
        {"kind": "combobox", "tag": "select", "type": None, "id": "b2", "name": "b2", "label": "Country", "required": True},
        {"kind": "fill", "tag": "input", "type": "text", "id": "a2", "name": "a2", "label": "First Name", "required": True},
    ]
    assert python_runtime._normalized_field_signature(first) == (
        python_runtime._normalized_field_signature(rerendered)
    )
    advanced = [
        {"kind": "fill", "tag": "input", "type": "text", "id": "c", "name": "c", "label": "Veteran Status", "required": True}
    ]
    assert python_runtime._normalized_field_signature(advanced) != (
        python_runtime._normalized_field_signature(first)
    )


def test_normalized_page_url_strips_fragment_and_volatile_tracking_params():
    raw = (
        "https://careers.example.com/jobs/1234?gh_jid=5678&gh_src=referral"
        "&utm_source=feed&q=engineer#section"
    )
    normalized = python_runtime._normalized_page_url(raw)
    assert "#section" not in normalized
    assert "gh_jid" not in normalized
    assert "gh_src" not in normalized
    assert "utm_source" not in normalized
    assert "q=engineer" in normalized
    assert normalized == python_runtime._normalized_page_url(raw)


class _FakeProcess:
    def __init__(self, pid, command):
        self.pid = pid
        self.command = command


def test_captcha_result_blocks_submission_for_unsupported_or_error():
    assert python_runtime._captcha_result_blocks_submission({"status": "unsupported", "detail": "hcaptcha"})
    assert python_runtime._captcha_result_blocks_submission({"status": "error", "detail": "provider error"})
    assert not python_runtime._captcha_result_blocks_submission({"status": "solved", "detail": "ok"})
    assert not python_runtime._captcha_result_blocks_submission({"status": "none", "detail": "not found"})
    assert not python_runtime._captcha_result_blocks_submission({"status": "skipped", "detail": "disabled"})


def test_invalid_captcha_discovery_result_is_ignored(monkeypatch):
    monkeypatch.setattr(python_runtime, "_discover_captcha", lambda _page: [])

    assert python_runtime._solve_captcha_if_configured(object()) == {
        "status": "none",
        "detail": "no supported CAPTCHA detected",
    }


def test_captcha_retry_is_bounded_to_one_recovery_attempt():
    source = inspect.getsource(python_runtime.run_runtime_payload)

    assert "range(1, CAPTCHA_RECOVERY_ATTEMPTS + 1)" in source
    assert python_runtime.CAPTCHA_RECOVERY_ATTEMPTS == 1
    assert "range(1, _self_heal_passes() + 1)" not in source


def test_recaptcha_retry_restores_api_and_requests_a_fresh_solver_token():
    source = inspect.getsource(python_runtime.run_runtime_payload)
    retry_start = source.index("# Native reCAPTCHA v3 token fallback on retry")
    retry_end = source.index("retry_submit = _find_button", retry_start)
    retry_source = source[retry_start:retry_end]

    assert "_restore_native_recaptcha(page, retry_challenge)" in retry_source
    assert "retry_captcha = _solve_captcha_if_configured(page)" in retry_source


def test_blocking_review_does_not_promote_captcha_presence_to_processing_error():
    source = inspect.getsource(python_runtime.run_runtime_payload)
    start = source.index(
        'captcha_result = {"status": "skipped", "detail": "blocking review fields present"}'
    )
    end = source.index("if blocking_review and not review_artifact:", start)
    blocking_review_decision = source[start:end]

    assert "_detect_submission_processing_error(page)" not in blocking_review_decision
    assert 'print(f"CapMonster CAPTCHA: {captcha_result[' in blocking_review_decision


def test_email_verification_polling_is_guarded_by_detected_prompt():
    source = inspect.getsource(python_runtime.run_runtime_payload)

    assert "if verification\n                    else None" in source


def test_ambient_captcha_retry_refills_only_when_form_reset_is_detected():
    source = inspect.getsource(python_runtime.run_runtime_payload)

    assert "if not _captcha_retry_should_refill(page, processing_error)" in source
    assert 'retry_refill = {"filled": [], "review": []}' in source


def test_browser_context_and_submit_delay_settings(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_BROWSER_USER_AGENT", "Mozilla/5.0 Custom")
    monkeypatch.setenv("JOB_AGENT_BROWSER_VIEWPORT", "1440x920")
    monkeypatch.setenv("JOB_AGENT_BROWSER_LOCALE", "en-US")
    monkeypatch.setenv("JOB_AGENT_BROWSER_TIMEZONE", "America/New_York")
    monkeypatch.setenv("JOB_AGENT_SUBMIT_HUMAN_DELAY_SECONDS", "0")

    options = python_runtime._browser_context_options()

    assert options["user_agent"] == "Mozilla/5.0 Custom"
    assert options["viewport"] == {"width": 1440, "height": 920}
    assert options["locale"] == "en-US"
    assert options["timezone_id"] == "America/New_York"
    assert options["extra_http_headers"]["Accept-Language"] == "en-US,en;q=0.9"
    assert python_runtime._human_submit_delay_ms() == 0


class _PureAmbientCaptchaPage:
    def evaluate(self, script):
        if 'data-automation-id="errorMessage"' in script:
            return []
        return {
            "url": "https://job-boards.greenhouse.io/embed/job_app",
            "requiredErrorText": False,
            "emptyKeyRequired": 0,
            "missingRequiredResume": False,
        }


class _GreenhouseResetCaptchaPage:
    def evaluate(self, script):
        if 'data-automation-id="errorMessage"' in script:
            return []
        return {
            "url": "https://job-boards.greenhouse.io/embed/job_app",
            "requiredErrorText": True,
            "emptyKeyRequired": 5,
            "missingRequiredResume": True,
        }


def test_captcha_retry_does_not_refill_for_pure_ambient_captcha():
    assert (
        python_runtime._captcha_retry_should_refill(
            _PureAmbientCaptchaPage(),
            "captcha present at https://job-boards.greenhouse.io/embed/job_app",
        )
        is False
    )


def test_captcha_retry_refills_when_greenhouse_form_reset_after_captcha():
    assert (
        python_runtime._captcha_retry_should_refill(
            _GreenhouseResetCaptchaPage(),
            "captcha present at https://job-boards.greenhouse.io/embed/job_app",
        )
        is True
    )


def test_combobox_available_options_diagnostic_is_scoped_to_open_popup():
    source = inspect.getsource(python_runtime._apply_fill)

    assert '[data-automation-id="menuItem"], [role="option"], [data-automation-id="radioBtn"], li' not in source
    assert '[role="listbox"], [role="menu"], [data-automation-id="activeListContainer"]' in source
    assert "controlled.length ? controlled : globalPopups" in source


def test_combobox_dynamic_fallback_handles_previous_employment_and_single_job_code():
    source = inspect.getsource(python_runtime._dynamic_combobox_fallback_choice)

    assert '"never worked" in _norm(text)' in source
    assert "_looks_like_job_code_option(text)" in source


def test_dynamic_combobox_fallback_uses_grounded_binary_polarity_and_live_job_code():
    sponsorship = {
        "label": "Will you require sponsorship for work authorization in the future?*"
    }
    job_code = {
        "label": "Please indicate the job code number in the job posting here.*"
    }

    assert python_runtime._dynamic_combobox_fallback_choice(
        sponsorship,
        ["Yes", "No"],
        "I require/will require employer sponsorship to obtain work authorization.",
    ) == "Yes"
    assert python_runtime._dynamic_combobox_fallback_choice(
        job_code,
        ["SWEI4AMPI4"],
        "7960680",
    ) == "SWEI4AMPI4"


def test_dynamic_combobox_fallback_generates_from_live_options_for_non_sensitive_question(
    monkeypatch,
):
    observed = {}

    class Resolver:
        def answer_for_field(self, field, profile, *, label=None):
            observed["options"] = field["options"]
            observed["label"] = label
            observed["profile"] = profile
            return "ML Infrastructure"

    monkeypatch.setattr(python_runtime, "llm_answers_enabled", lambda: True)
    monkeypatch.setattr(
        python_runtime,
        "get_llm_answer_resolver",
        lambda: Resolver(),
    )
    profile = {"skills": ["Kubernetes", "PyTorch"], "projects": []}
    field = {
        "label": "Which team best matches your background?",
        "role": "combobox",
    }

    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["Product Design", "ML Infrastructure", "Sales Engineering"],
        "No direct saved answer",
        profile,
    ) == "ML Infrastructure"
    assert observed == {
        "options": ["Product Design", "ML Infrastructure", "Sales Engineering"],
        "label": "Which team best matches your background?",
        "profile": profile,
    }


def test_dynamic_combobox_fallback_can_generate_without_a_preplanned_answer(monkeypatch):
    class Resolver:
        def answer_for_field(self, field, profile, *, label=None):
            assert field["options"] == ["Product Design", "ML Infrastructure"]
            return "ML Infrastructure"

    monkeypatch.setattr(
        python_runtime,
        "get_llm_answer_resolver",
        lambda: Resolver(),
    )

    assert python_runtime._dynamic_combobox_fallback_choice(
        {
            "label": "Which team best matches your background?",
            "role": "combobox",
        },
        ["Product Design", "ML Infrastructure"],
        "",
        {"skills": ["PyTorch"]},
    ) == "ML Infrastructure"


def test_unknown_dynamic_combobox_defers_until_live_options_are_visible(monkeypatch):
    class Resolver:
        def answer_for_field(self, field, profile, *, label=None):
            raise AssertionError("planning must wait for the live option set")

    monkeypatch.setattr(
        python_runtime,
        "get_llm_answer_resolver",
        lambda: Resolver(),
    )
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Which team best matches your background?",
        "required": True,
        "options": [],
    }

    assert python_runtime._plan_field(
        field,
        {"skills": ["PyTorch"]},
        None,
    ) == {
        "action": "combobox",
        "value": "",
        "defer_live_options": True,
    }


def test_explicit_dynamic_combobox_answer_does_not_generate_a_conflicting_guess(
    monkeypatch,
):
    class Resolver:
        def answer_for_field(self, field, profile, *, label=None):
            raise AssertionError("an approved answer must not be regenerated")

    monkeypatch.setattr(
        python_runtime,
        "get_llm_answer_resolver",
        lambda: Resolver(),
    )
    question = "What intern season are you interested in?*"

    assert python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": question,
            "required": True,
            "options": [],
        },
        {"answers": {question: "Fall 2026"}},
        None,
    ) == {"action": "combobox", "value": "Fall 2026"}


def test_dynamic_combobox_fallback_does_not_generate_candidate_facts(monkeypatch):
    monkeypatch.setattr(python_runtime, "llm_answers_enabled", lambda: True)

    class Resolver:
        def answer_for_field(self, field, profile, *, label=None):
            raise AssertionError("candidate facts must not reach option generation")

    monkeypatch.setattr(
        python_runtime,
        "get_llm_answer_resolver",
        lambda: Resolver(),
    )

    assert python_runtime._dynamic_combobox_fallback_choice(
        {"label": "What is your nationality?", "role": "combobox"},
        ["China", "United States", "Canada"],
        "No direct saved answer",
        {"location": "Jersey City, NJ"},
    ) is None


def test_regular_commute_question_uses_explicit_approved_screening_rule():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Will you be able to regularly commute and work in an office "
            "in job posting location?"
        ),
        "required": True,
        "options": [],
    }
    profile = {
        "location": "Jersey City, NJ, USA",
        "screening_answer_rules": [
            {
                "patterns": ["regularly commute and work in an office"],
                "answer": "Yes",
            }
        ],
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_source_checkbox_group_uses_section_context_and_saved_source():
    field = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "Please check all that apply:",
        "section": "How did you hear about Hive?",
        "required": True,
        "options": [
            "Friend",
            "Recruiter/current employee",
            "LinkedIn",
            "Other",
        ],
    }
    profile = {"answers": {"How did you hear about us?": "LinkedIn"}}

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "checkmany",
        "options": ["LinkedIn"],
    }


def test_option_style_sensitive_checkbox_uses_question_section():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Yes",
        "section": "Are you currently authorized to work in the United States? *",
        "required": True,
    }
    profile = {
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["authorized to work in the united states"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "check"
    }

    opposite = dict(field, label="No", value="No")
    assert python_runtime._plan_field(opposite, profile, None) == {
        "action": "skip",
        "reason": "option polarity does not match approved answer",
        "sensitive": True,
        "blocking": False,
    }


def test_export_control_not_applicable_selects_page_none_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "EXPORT CONTROLS - eligibility under U.S. export controls.*",
        "required": True,
        "options": [
            "A United States citizen or national",
            "A person lawfully admitted for permanent residence",
            "None of the above",
        ],
    }
    profile = {
        "sensitive_answers": {
            "us_export_control_status": {
                "patterns": ["export control"],
                "answer": "Not applicable",
                "approved": True,
            }
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "None of the above",
    }


def test_highest_education_saved_master_degree_matches_plural_option():
    field = {
        "label": "Please indicate your highest level of education*",
        "role": "combobox",
        "required": True,
        "options": [
            {"label": option, "value": option}
            for option in (
                "None",
                "High School",
                "Associates",
                "Bachelors",
                "Masters",
                "Doctorate",
            )
        ],
    }

    assert python_runtime._matching_options(field, "Master's degree") == [
        {"label": "Masters", "value": "Masters"}
    ]


def test_coinbase_runtime_url_uses_greenhouse_embed_to_avoid_cloudflare():
    assert python_runtime._runtime_application_url(
        "https://www.coinbase.com/careers/positions/8020892?gh_jid=8020892"
    ) == "https://job-boards.greenhouse.io/embed/job_app?for=coinbase&token=8020892"


def test_c3_runtime_url_uses_greenhouse_embed_to_avoid_queryless_404():
    assert python_runtime._runtime_application_url(
        "https://c3.ai/job-description/8581327002?gh_jid=8581327002"
    ) == "https://job-boards.greenhouse.io/embed/job_app?for=c3iot&token=8581327002"


def test_samsara_runtime_url_uses_greenhouse_embed_when_company_page_has_no_form():
    assert python_runtime._runtime_application_url(
        "https://www.samsara.com/company/careers/roles/8036387?gh_jid=8036387"
    ) == "https://job-boards.greenhouse.io/embed/job_app?for=samsara&token=8036387"


def test_pinterest_runtime_url_uses_greenhouse_embed_to_avoid_cloudflare_company_page():
    assert python_runtime._runtime_application_url(
        "https://www.pinterestcareers.com/jobs/?gh_jid=6816337"
    ) == "https://job-boards.greenhouse.io/embed/job_app?for=pinterest&token=6816337"


def test_runtime_url_does_not_rewrite_other_custom_greenhouse_hosts():
    url = "https://careers.airbnb.com/positions/8031901?gh_jid=8031901"

    assert python_runtime._runtime_application_url(url) == url


def test_python_runtime_rejects_non_pdf_resume_before_playwright_import(tmp_path):
    resume_path = tmp_path / "tailored-resume.docx"
    resume_path.write_text("fake docx")

    with pytest.raises(ValueError, match="resume upload must be an existing PDF"):
        python_runtime._verify_runtime_resume_file({"resumeFile": str(resume_path)})


def test_python_runtime_requires_resume_file_when_source_dir_is_declared(tmp_path):
    source_dir = tmp_path / "resumes"
    source_dir.mkdir()

    with pytest.raises(ValueError, match="missing required PDF resume upload path"):
        python_runtime._verify_runtime_resume_file({"resumeSourceDir": str(source_dir)})


def test_python_runtime_resolves_relative_pdf_resume_from_source_dir(tmp_path):
    package_dir = tmp_path / "package"
    source_dir = tmp_path / "resumes"
    package_dir.mkdir()
    source_dir.mkdir()
    resume_path = source_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nsource resume")
    payload = {
        "resumeFile": "../resumes/resume.pdf",
        "resumeSourceDir": str(source_dir),
        "_runtimeScriptDir": str(package_dir),
    }

    python_runtime._verify_runtime_resume_file(payload)

    assert payload["resumeFile"] == str(resume_path.resolve())


def test_python_runtime_rejects_package_local_resume_pdf(tmp_path, monkeypatch):
    monkeypatch.delenv("RESUME_SOURCE_DIR", raising=False)
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    resume_path = package_dir / "resume.pdf"
    resume_path.write_bytes(b"%PDF-1.4\npackage generated resume")
    payload = {"resumeFile": "resume.pdf", "_runtimeScriptDir": str(package_dir)}

    with pytest.raises(ValueError, match="not package-local"):
        python_runtime._verify_runtime_resume_file(payload)


def test_extend_unique_filled_deduplicates_retry_results():
    target = [{"label": "Email", "action": "fill"}]

    python_runtime._extend_unique_filled(
        target,
        [
            {"label": "Email", "action": "fill"},
            {"label": "Resume", "action": "upload"},
        ],
    )

    assert target == [
        {"label": "Email", "action": "fill"},
        {"label": "Resume", "action": "upload"},
    ]


def test_self_heal_skips_unresolved_manual_application_answers():
    assert python_runtime._has_retryable_blocking_review(
        [
            {
                "label": "How did you hear about us?",
                "reason": "combobox needs saved answer / manual selection",
                "blocking": True,
            }
        ]
    ) is False


def test_plan_field_blocks_external_application_portal_instruction():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please note that you will not be considered unless you complete the Constellation application form. *",
        "required": True,
    }

    assert python_runtime._plan_field(
        field,
        {
            "answers": {},
            "personal_us_company_employment_history": "Never worked for a United States company.",
        },
        None,
    ) == {
        "action": "skip",
        "reason": "external application portal required",
        "sensitive": False,
        "blocking": True,
    }


def test_self_heal_retries_transient_fill_failures():
    assert python_runtime._has_retryable_blocking_review(
        [
            {
                "label": "Email",
                "reason": "fill error: fill readback empty after setting non-empty value",
                "blocking": True,
            }
        ]
    ) is True


def test_self_heal_retries_transient_failure_alongside_manual_blocker():
    assert python_runtime._has_retryable_blocking_review(
        [
            {
                "label": "How did you hear about us?",
                "reason": "combobox needs saved answer / manual selection",
                "blocking": True,
            },
            {
                "label": "Email",
                "reason": "fill error: fill readback empty after setting non-empty value",
                "blocking": True,
            },
        ]
    ) is True


def test_self_heal_does_not_repeat_a_combobox_no_progress_breaker():
    assert python_runtime._has_retryable_blocking_review(
        [
            {
                "label": "Departments",
                "reason": "fill error: combobox made no progress before field repair deadline",
                "blocking": True,
            }
        ]
    ) is False


def test_field_repair_identity_prefers_a_unique_stable_id_over_scrape_marker():
    first = {
        "kind": "single",
        "tag": "input",
        "id": "first_name",
        "name": "first_name",
        "label": "First Name",
        "autofillId": "2",
    }
    rescanned = {**first, "autofillId": "17"}

    assert python_runtime._field_repair_identity(first, [first]) == (
        "id",
        "single",
        "first_name",
    )
    assert python_runtime._field_repair_identity(rescanned, [rescanned]) == (
        "id",
        "single",
        "first_name",
    )


def test_combobox_progress_deadline_is_bounded_and_raises_a_specific_error(
    monkeypatch,
):
    monkeypatch.setenv("JOB_AGENT_COMBOBOX_NO_PROGRESS_SECONDS", "5")
    monotonic_values = iter([100.0, 106.0])
    monkeypatch.setattr(
        python_runtime.time,
        "monotonic",
        lambda: next(monotonic_values),
    )

    deadline = python_runtime._new_combobox_progress_deadline()

    with pytest.raises(
        python_runtime.ComboboxNoProgressError,
        match="combobox made no progress",
    ):
        python_runtime._check_combobox_progress_deadline(
            deadline,
            {"label": "Departments"},
        )


def test_fill_page_enforces_runtime_wall_deadline(monkeypatch):
    class Page:
        def wait_for_timeout(self, milliseconds):
            pass

    def raise_deadline():
        raise RuntimeError("autofill runtime wall-clock deadline exceeded")

    monkeypatch.setattr(
        python_runtime,
        "_ensure_application_fields_ready",
        lambda _page: [],
    )
    monkeypatch.setattr(python_runtime, "_scrape_fields", lambda _page: [])
    monkeypatch.setattr(python_runtime, "_audit_required_fields", lambda _page: [])
    monkeypatch.setattr(
        python_runtime,
        "_check_runtime_wall_deadline",
        raise_deadline,
    )

    with pytest.raises(RuntimeError, match="wall-clock"):
        python_runtime._fill_page(Page(), {}, None)


def test_fill_page_blocks_when_fill_time_budget_is_exhausted(monkeypatch):
    class Page:
        def wait_for_timeout(self, milliseconds):
            pass

    monkeypatch.setattr(
        python_runtime,
        "_ensure_application_fields_ready",
        lambda _page: [],
    )
    monkeypatch.setattr(python_runtime, "_scrape_fields", lambda _page: [])
    monkeypatch.setattr(python_runtime, "_audit_required_fields", lambda _page: [])
    monkeypatch.setattr(
        python_runtime,
        "_check_runtime_wall_deadline",
        lambda: None,
    )
    previous = python_runtime._FILL_TIME_BUDGET_SECONDS[0]
    python_runtime._FILL_TIME_BUDGET_SECONDS[0] = -1.0
    try:
        result = python_runtime._fill_page(Page(), {}, None)
    finally:
        python_runtime._FILL_TIME_BUDGET_SECONDS[0] = previous

    assert result["filled"] == []
    assert any(
        item.get("blocking") and "budget" in str(item.get("reason") or "")
        for item in result["review"]
    )


def test_option_matching_expands_location_state_and_country_abbreviations():
    assert python_runtime._option_matches(
        "Jersey City, New Jersey, United States", "Jersey City, NJ, USA"
    )


def test_best_option_match_prefers_years_range_containing_answer():
    options = [
        "0-1 years",
        "1-3 years",
        "3-5 years",
        "5-7 years",
        "7+ years",
    ]

    assert python_runtime._best_option_match(options, "4 years") == "3-5 years"
    assert python_runtime._best_option_match(options, "5 years") == "3-5 years"
    assert python_runtime._best_option_match(options, "8 years") == "7+ years"


def test_best_option_match_rejects_low_confidence_overlap():
    assert python_runtime._best_option_match(
        ["New York office", "San Francisco office"],
        "office",
    ) is None


def test_candidate_commitments_require_exact_approved_answers():
    onsite_label = "Are you willing to work onsite from our New York City office 5 days/week?*"
    familiarity_label = "How familiar were you with our company before applying?*"
    season_label = "What intern season are you interested in?*"
    product_label = "Have you used our product before?*"
    sms_label = (
        "We would like to contact you via SMS or WhatsApp to provide updates. "
        "Mark yes if you agree.*"
    )
    relocation_only = {
        "answers": {"Are you open to relocation?": "Yes"},
        "sensitive_answers": {
            "relocation": {
                "approved": True,
                "answer": "Yes",
                "patterns": ["relocation"],
            }
        },
    }

    assert python_runtime._priority_auto_answer(onsite_label, relocation_only) is None
    assert python_runtime._auto_answer(familiarity_label, relocation_only) is None
    assert python_runtime._auto_answer(season_label, relocation_only) is None

    approved = {
        **relocation_only,
        "answers": {
            **relocation_only["answers"],
            onsite_label: "Yes",
            familiarity_label: "Somewhat familiar",
            season_label: "Fall 2026",
        },
    }
    assert python_runtime._priority_auto_answer(onsite_label, approved) == "Yes"
    assert python_runtime._auto_answer(familiarity_label, approved) == "Somewhat familiar"
    assert python_runtime._auto_answer(season_label, approved) == "Fall 2026"

    approved_rules = {
        "answers": {
            "Would you like to receive communications via SMS and/or WhatsApp": "No",
            "Which area interests you most?": "AI & Machine Learning",
        },
        "screening_answer_rules": [
            {"patterns": ["onsite", "in office"], "answer": "Yes"},
            {"patterns": ["have you used", "used our product"], "answer": "No"},
            {"patterns": ["sms"], "answer": "Yes"},
        ],
    }
    assert python_runtime._priority_auto_answer(onsite_label, approved_rules) == "Yes"
    assert python_runtime._auto_answer(product_label, approved_rules) == "No"
    # The semantically identical exact answer is more specific than the broad
    # SMS standing rule, so it must win.
    assert python_runtime._auto_answer(sms_label, approved_rules) == "No"
    # Unrelated fuzzy overlap must not become an internship preference.
    assert python_runtime._auto_answer(season_label, approved_rules) is None


def test_candidate_fact_plan_never_uses_fuzzy_answer_overlap():
    field = {
        "kind": "radiogroup",
        "label": "Do you have a minimum of 3 years of experience, not including internships?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    profile = {
        "answers": {
            "Do you have over 2 years of industry work experience (non-internship experience)?": "Yes"
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "skip",
        "reason": "candidate fact needs explicit approved answer",
        "sensitive": False,
        "blocking": True,
    }


def test_nationality_and_english_level_require_explicit_profile_facts():
    for label in ("What is your nationality?", "What is your English level?*"):
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
            "options": ["C1", "C2", "United States", "China"],
        }
        assert python_runtime._plan_field(field, {"country": "United States"}, None) == {
            "action": "skip",
            "reason": "candidate fact needs explicit approved answer",
            "sensitive": False,
            "blocking": True,
        }


def test_exceptional_ability_prompt_can_use_grounded_generation_when_allowed():
    label = (
        "Please provide us with 3-4 examples highlighting your exceptional "
        "ability. This is your moment to WOW us! First example:*"
    )

    assert not python_runtime._requires_user_authored_answer(label, {})
    assert python_runtime._auto_answer(label, {}) is None

    approved_profile = {
        "screening_answer_rules": [
            {
                "patterns": ["examples highlighting your exceptional ability"],
                "answer": "1. Built an approved, measured project example.",
            }
        ]
    }
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": label,
        "required": True,
    }
    assert python_runtime._plan_field(field, approved_profile, None) == {
        "action": "fill",
        "value": "1. Built an approved, measured project example.",
    }


def test_numeric_range_bounds_parses_dash_plus_and_single_values():
    assert python_runtime._numeric_range_bounds("3-5 years") == (3.0, 5.0)
    assert python_runtime._numeric_range_bounds("7+ years") == (7.0, float("inf"))
    assert python_runtime._numeric_range_bounds("5 years") == (5.0, 5.0)
    assert python_runtime._numeric_range_bounds("United States") is None


def test_selector_for_prefers_unique_marker_over_shared_name():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "name": "experience[]",
        "autofillId": "17",
    }

    assert python_runtime._selector_for(field) == '[data-job-agent-autofill-index="17"]'


def test_check_with_fallback_commits_aria_choice_state():
    class Locator:
        selected = False

        def get_attribute(self, name):
            if name == "role":
                return "radio"
            if name == "aria-checked":
                return "true" if self.selected else "false"
            return ""

        def click(self, timeout=None):
            self.selected = True

    locator = Locator()

    assert python_runtime._check_with_fallback(locator) is True
    assert locator.selected is True


def test_fill_page_discovers_conditional_field_revealed_by_prior_answer(monkeypatch):
    class Page:
        def __init__(self):
            self.revealed = False
            self.waits: list[int] = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    trigger = {
        "kind": "single", "tag": "input", "type": "text", "id": "trigger",
        "label": "First name", "required": True,
    }
    conditional = {
        "kind": "single", "tag": "input", "type": "text", "id": "conditional",
        "label": "Current city", "required": True,
    }
    page = Page()

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda current_page: [trigger, conditional] if current_page.revealed else [trigger],
    )
    monkeypatch.setattr(
        python_runtime,
        "_plan_field",
        lambda field, *_args: {"action": "fill", "value": field["id"]},
    )

    def apply_fill(current_page, field, _plan, _profile=None):
        if field["id"] == "trigger":
            current_page.revealed = True
        return field["id"]

    monkeypatch.setattr(python_runtime, "_apply_fill", apply_fill)
    monkeypatch.setattr(python_runtime, "_audit_required_fields", lambda _page: [])

    result = python_runtime._fill_page(page, {}, None)

    assert [item["label"] for item in result["filled"]] == ["First name", "Current city"]
    assert result["review"] == []


def test_fill_page_defers_disabled_conditional_select_until_dependency_is_ready(monkeypatch):
    class Page:
        def __init__(self):
            self.revealed = False
            self.waits: list[int] = []

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    trigger = {
        "kind": "single", "tag": "input", "type": "text", "id": "trigger",
        "label": "Education level", "required": True,
    }
    education = {
        "kind": "single", "tag": "select", "type": "select", "id": "school",
        "label": "School", "required": True, "disabled": True,
    }
    page = Page()
    applied: list[str] = []

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda current_page: [
            trigger,
            {**education, "disabled": not current_page.revealed},
        ],
    )
    monkeypatch.setattr(
        python_runtime,
        "_plan_field",
        lambda field, *_args: {"action": "select" if field["id"] == "school" else "fill", "value": field["id"]},
    )

    def apply_fill(current_page, field, _plan, _profile=None):
        applied.append(field["id"])
        if field["id"] == "trigger":
            current_page.revealed = True
        return field["id"]

    monkeypatch.setattr(python_runtime, "_apply_fill", apply_fill)
    monkeypatch.setattr(python_runtime, "_audit_required_fields", lambda _page: [])

    result = python_runtime._fill_page(page, {}, None)

    assert applied == ["trigger", "school"]
    assert [item["label"] for item in result["filled"]] == [
        "Education level",
        "School",
    ]
    assert result["review"] == []


def test_fill_page_uses_unique_marker_when_required_selects_share_an_id(monkeypatch):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    monkeypatch.setattr(
        python_runtime,
        "_plan_field",
        lambda *_args: {"action": "select", "value": "No"},
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <label for="duplicate-eeo">Veteran Status*</label>
              <select id="duplicate-eeo" required>
                <option value="">Select...</option>
                <option value="No">No</option>
              </select>
              <label for="duplicate-eeo">Veteran Status*</label>
              <select id="duplicate-eeo" required>
                <option value="">Select...</option>
                <option value="No">No</option>
              </select>
            </form>
            """
        )

        result = python_runtime._fill_page(page, {}, None)
        values = page.locator("select").evaluate_all(
            "(nodes) => nodes.map((node) => node.value)"
        )
        browser.close()

    assert values == ["No", "No"]
    assert len(result["filled"]) == 2
    assert result["review"] == []


def test_post_submit_blockers_are_emitted_and_stop_captcha_retry(
    monkeypatch,
    capsys,
):
    review_item = {
        "label": "Your Location",
        "reason": "required field remains empty after fill",
        "sensitive": False,
        "blocking": True,
    }
    all_review = []
    monkeypatch.setattr(
        python_runtime,
        "_write_review_evidence",
        lambda *_args: "/tmp/review-required.txt",
    )

    artifact = python_runtime._finalize_post_submit_blockers(
        object(),
        {},
        all_review,
        [review_item],
        [{"label": "Email", "action": "fill"}],
    )

    output = capsys.readouterr().out
    assert artifact == "/tmp/review-required.txt"
    assert all_review == [review_item]
    assert '"label": "Your Location"' in output
    assert "Autofill stats: filled=1 review=1" in output
    assert "Submit gate: STOPPED before final Submit" in output
    assert "_finalize_post_submit_blockers(" in inspect.getsource(
        python_runtime.run_runtime_payload
    )


def test_open_application_form_navigates_into_embedded_greenhouse_iframe(monkeypatch):
    class Page:
        def __init__(self):
            self.url = "https://www.quantifind.com/open-positions/?gh_jid=7587260"
            self.gotos: list[tuple[str, str | None, int | None]] = []
            self.load_states: list[str] = []
            self.waits: list[int] = []

        def evaluate(self, _script):
            return "https://boards.greenhouse.io/embed/job_app?for=quantifind&token=7587260"

        def goto(self, url, wait_until=None, timeout=None):
            self.gotos.append((url, wait_until, timeout))
            self.url = url

        def wait_for_load_state(self, state, timeout=None):
            self.load_states.append(state)

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda current_page: [{"label": "Full Name", "type": "text"}]
        if "embed/job_app" in current_page.url
        else [{"label": "Username / Email", "type": "text"}],
    )
    monkeypatch.setattr(python_runtime, "_find_application_entry", lambda _page: None)
    monkeypatch.setattr(python_runtime, "_is_workday_apply_gate", lambda _page, _fields=None: False)
    monkeypatch.setattr(python_runtime, "_is_job_page_apply_button", lambda _page, _entry, _fields=None: False)

    assert python_runtime._open_application_form_if_needed(page) is True
    assert page.url == "https://boards.greenhouse.io/embed/job_app?for=quantifind&token=7587260"
    assert page.gotos == [
        ("https://boards.greenhouse.io/embed/job_app?for=quantifind&token=7587260", "domcontentloaded", 30000)
    ]
    assert page.load_states == ["domcontentloaded", "networkidle"]
    assert page.waits == [1500]


def test_open_application_form_does_not_stop_on_career_search_filters(monkeypatch):
    class Page:
        def __init__(self):
            self.clicked = False
            self.load_states = []
            self.waits = []

        def wait_for_load_state(self, state, timeout=None):
            self.load_states.append(state)

        def wait_for_timeout(self, milliseconds):
            self.waits.append(milliseconds)

    page = Page()

    monkeypatch.setattr(python_runtime, "_open_embedded_application_iframe_if_needed", lambda _page: False)
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda current_page: (
            [{"label": "First Name", "type": "text"}, {"label": "Resume", "type": "file"}]
            if current_page.clicked
            else [{"label": "Search", "type": "text"}, {"label": "Department", "role": "combobox"}]
        ),
    )
    monkeypatch.setattr(
        python_runtime,
        "_find_application_entry",
        lambda current_page: None if current_page.clicked else {"text": "Apply", "tag": "button"},
    )
    monkeypatch.setattr(python_runtime, "_is_workday_apply_gate", lambda _page, _fields=None: False)
    monkeypatch.setattr(python_runtime, "_is_job_page_apply_button", lambda _page, _entry, _fields=None: False)

    def fake_click(current_page, _entry):
        current_page.clicked = True

    monkeypatch.setattr(python_runtime, "_click_button", fake_click)

    assert python_runtime._open_application_form_if_needed(page) is True
    assert page.clicked is True


class _EofStdin:
    def isatty(self):
        return True

    def readline(self, *args, **kwargs):
        return ""


class _VerificationPage:
    def __init__(self):
        self.filled = None

    def evaluate(self, script, arg=None):
        body = str(script)
        if "window.location.href" in body:
            return {
                "url": "https://job-boards.greenhouse.io/acme/jobs/1",
                "title": "Application security code",
                "text": "Enter the security code sent to your email, then resubmit your application.",
            }
        if "querySelectorAll(\"input, textarea\")" in body:
            self.filled = arg
            return True
        return None


class _ApplicationEntryPage:
    def evaluate(self, script, arg=None):
        return [
            {"text": "Privacy Notice", "id": "", "tag": "a", "href": "https://example.com/privacy"},
            {"text": "I'm interested", "id": "apply", "tag": "button", "href": ""},
        ]


class _WorkdayApplyEntryPage:
    def evaluate(self, script, arg=None):
        return [{"text": "Apply", "id": "", "tag": "button", "href": ""}]


class _WorkdayApplyManuallyGatePage:
    url = "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply"

    def __init__(self):
        self.opened = False

    def evaluate(self, script, arg=None):
        return [
            {
                "text": "Apply Manually",
                "id": "",
                "tag": "a",
                "href": "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually",
            }
        ]

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, _milliseconds):
        pass


class _WaitingPage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class _WorkdayRejectedSignInPage:
    url = "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually"


class _WorkdayVerificationLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.text == "Resend Account Verification" else 0

    def is_visible(self):
        return self.count() > 0

    def click(self):
        self.page.clicked_texts.append(self.text)


class _WorkdayAccountVerificationPage:
    url = "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually"

    def __init__(self):
        self.clicked_texts = []
        self.gotos = []
        self.waits = []

    def get_by_text(self, text, exact=False):
        return _WorkdayVerificationLocator(self, text)

    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class _WorkdayCandidateHomeNoApplicationsPage:
    url = "https://company.wd5.myworkdayjobs.com/en-US/careers/userHome"

    def __init__(self):
        self.gotos = []
        self.waits = []

    def evaluate(self, script, arg=None):
        body = str(script)
        if "document.body && document.body.innerText" in body:
            return "Candidate Home Welcome to Candidate Home My Applications You have no applications. Search for Jobs"
        return []

    def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class _NoFormFieldsPage:
    def evaluate(self, script, arg=None):
        return []


class _VisibleLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def click(self, force=False):
        self.page.clicked.append((self.selector, force))


class _MissingLocator:
    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def count(self):
        return 0

    def is_visible(self):
        return False


class _WorkdayCreateAccountPage:
    def __init__(self):
        self.clicked = []
        self.waited = []

    def locator(self, selector):
        return _VisibleLocator(self, selector)

    def wait_for_timeout(self, milliseconds):
        self.waited.append(milliseconds)


class _WorkdayEmailSignInGatePage:
    url = "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually"

    def __init__(self):
        self.clicked = []
        self.waited = []

    def evaluate(self, script, arg=None):
        return [
            {
                "text": "Sign in with Apple",
                "id": "apple",
                "tag": "button",
                "href": "",
                "automationId": "",
                "autofillId": "0",
            },
            {
                "text": "Sign in with email",
                "id": "email",
                "tag": "button",
                "href": "",
                "automationId": "",
                "autofillId": "1",
            },
        ]

    def locator(self, selector):
        if "data-automation-id" in selector:
            return _MissingLocator()
        return _VisibleLocator(self, selector)

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, milliseconds):
        self.waited.append(milliseconds)


class _WorkdayRejectedSignInWithCreateAccountPage(_WorkdayEmailSignInGatePage):
    def evaluate(self, script, arg=None):
        return [
            {
                "text": "Sign In",
                "id": "signInSubmitButton",
                "tag": "button",
                "href": "",
                "automationId": "signInSubmitButton",
                "autofillId": "0",
            },
            {
                "text": "Create Account",
                "id": "create",
                "tag": "button",
                "href": "",
                "automationId": "",
                "autofillId": "1",
            },
        ]

    def locator(self, selector):
        return _VisibleLocator(self, selector)


class _CandidateHomeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    def count(self):
        if "signInSubmitButton" in self.selector and self.page.signed_in:
            return 0
        return 1

    def is_visible(self):
        if "utilityButtonSignIn" in self.selector:
            return not self.page.login_open
        if "signInSubmitButton" in self.selector:
            return self.page.login_open and not self.page.signed_in
        return True

    def click(self, force=False):
        if "utilityButtonSignIn" in self.selector:
            self.page.login_open = True
        if "click_filter" in self.selector or "signInSubmitButton" in self.selector:
            self.page.signed_in = True

    def fill(self, value):
        self.page.filled[self.selector] = value

    def press(self, key):
        self.page.pressed.append((self.selector, key))


class _CandidateHomePage:
    def __init__(self):
        self.login_open = False
        self.signed_in = False
        self.filled = {}
        self.pressed = []

    def locator(self, selector):
        return _CandidateHomeLocator(self, selector)

    def wait_for_timeout(self, milliseconds):
        pass


class _WorkdaySignInButtonsPage:
    def evaluate(self, script, arg=None):
        return [
            {
                "text": "Sign In",
                "id": "",
                "className": "",
                "title": "",
                "ariaLabel": "",
                "automationId": "utilityButtonSignIn",
                "tag": "button",
                "type": "button",
                "href": "",
                "inForm": False,
                "inDatepicker": False,
                "y": 10,
                "autofillId": "0",
            },
            {
                "text": "Sign In",
                "id": "",
                "className": "",
                "title": "",
                "ariaLabel": "",
                "automationId": "signInSubmitButton",
                "tag": "button",
                "type": "submit",
                "href": "",
                "inForm": True,
                "inDatepicker": False,
                "y": 500,
                "autofillId": "1",
            },
        ]


class _WorkdaySaveAndContinuePage:
    def evaluate(self, script, arg=None):
        return [
            {
                "text": "Save and Continue",
                "id": "",
                "className": "",
                "title": "",
                "ariaLabel": "",
                "automationId": "bottom-navigation-next-button",
                "tag": "button",
                "type": "button",
                "href": "",
                "inForm": False,
                "inDatepicker": False,
                "y": 900,
                "autofillId": "0",
            }
        ]


class _WorkdayDateSectionLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.value = ""

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def click(self):
        self.page.actions.append((self.selector, "click"))

    def press(self, key):
        self.page.actions.append((self.selector, "press", key))
        if key == "Backspace":
            self.value = ""

    def press_sequentially(self, value, delay):
        self.page.actions.append((self.selector, "type", value, delay))
        self.value = value

    def input_value(self):
        for part in ("Month", "Day", "Year"):
            if f"dateSection{part}-input" in self.selector:
                return self.page.date_values.get(part, self.value)
        return self.value

    def is_visible(self):
        return True


class _WorkdayDatePickerLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def click(self):
        self.page.actions.append((self.selector, "click"))
        if "datePicker" in self.selector:
            self.page.date_values.update({"Month": "7", "Day": "14", "Year": "2026"})


class _WorkdayDateSectionPage:
    def __init__(self):
        self.actions = []
        self.locators = {}
        self.waited = []
        self.date_values = {}

    def locator(self, selector):
        if "dateIcon" in selector or "datePicker" in selector:
            return _WorkdayDatePickerLocator(self, selector)
        if selector not in self.locators:
            self.locators[selector] = _WorkdayDateSectionLocator(self, selector)
        return self.locators[selector]

    def wait_for_timeout(self, milliseconds):
        self.waited.append(milliseconds)


def test_workday_date_sections_use_calendar_and_verify_readback(monkeypatch):
    class _Today:
        month = 7
        day = 14
        year = 2026

    monkeypatch.setattr(python_runtime, "date", type("Date", (), {"today": staticmethod(lambda: _Today())}))
    page = _WorkdayDateSectionPage()

    result = python_runtime._fill_workday_date_sections(
        page,
        "selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input",
    )

    assert result == "07/14/2026"
    assert [action for action in page.actions if action[1] == "click"] == [
        ('[data-fkit-id="selfIdentifiedDisabilityData--dateSignedOn"] [data-automation-id="dateIcon"]', "click"),
        (
            '[data-automation-id="datePicker"] '
            '[data-uxi-datepicker-year="2026"]'
            '[data-uxi-datepicker-month="7"]'
            '[data-uxi-datepicker-mmdd="0714"]',
            "click",
        ),
    ]
    assert not [action for action in page.actions if action[1] == "type"]
    assert page.waited == [250, 700]


def test_application_date_field_uses_todays_date_in_mm_dd_yy(monkeypatch):
    class _Today:
        month = 8
        day = 11
        year = 2026

    monkeypatch.setattr(
        python_runtime,
        "date",
        type("Date", (), {"today": staticmethod(lambda: _Today())}),
    )
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Today's Date of Application (MM/DD/YY Format)*",
        "required": True,
    }

    plan = python_runtime._plan_field(field, {}, None)

    assert plan == {"action": "fill", "value": "08/11/26"}


def test_workday_history_date_section_uses_profile_value_without_sensitive_gate():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Month",
        "id": "workExperience-startDate-dateSectionMonth-input",
        "section": "work",
        "required": True,
    }
    profile = {"work_history": [{"current": True, "start_date": "2022-01-04"}]}
    page = _WorkdayDateSectionPage()

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {"action": "fill", "value": "January"}
    assert python_runtime._fill_workday_date_section(page, field["id"], plan["value"]) == "01"
    assert (f'[id="{field["id"]}"]', "type", "01", 100) in page.actions


def test_workday_nested_source_selection_uses_actual_field_and_commits_radio():
    class _Locator:
        def __init__(self, page, selector, text=""):
            self.page = page
            self.selector = selector
            self.text = text

        @property
        def first(self):
            return self

        @property
        def last(self):
            return self

        def filter(self, has_text):
            return _Locator(self.page, self.selector, str(has_text or ""))

        def count(self):
            if self.selector == '[id="different-source-id"]':
                return 1
            if 'menuItem' in self.selector:
                return int(self.page.menu_open and self.text == "Website")
            if 'radioBtn' in self.selector:
                return int(self.page.nested_open and self.text in {"Company website", "Company Website"})
            return 0

        def is_visible(self):
            return bool(self.count())

        def wait_for(self, state, timeout):
            if not self.is_visible():
                raise RuntimeError("not visible")

        def click(self):
            if self.selector == '[id="different-source-id"]':
                self.page.menu_open = True
            elif 'menuItem' in self.selector and self.text == "Website":
                self.page.nested_open = True
            elif 'radioBtn' in self.selector and self.count():
                self.page.radio_clicked = True
                self.page.selected = "Company Website"

        def input_value(self):
            return self.page.selected if self.selector == '[id="different-source-id"]' else ""

    class _Page:
        def __init__(self):
            self.menu_open = True
            self.nested_open = False
            self.radio_clicked = False
            self.selected = ""
            self.selection_context = None

        def locator(self, selector):
            return _Locator(self, selector)

        def wait_for_timeout(self, milliseconds):
            pass

        def evaluate(self, script, arg=None):
            if "const visibleText" in str(script):
                self.selection_context = arg
                return {"values": [self.selected] if self.selected else [], "expanded": False}
            return None

    page = _Page()
    selected = python_runtime._select_workday_nested_prompt_option(
        page,
        "Company website",
        {"id": "different-source-id"},
    )

    assert selected == "Company Website"
    assert page.radio_clicked is True
    assert page.selection_context == {
        "id": "different-source-id",
        "name": "",
        "autofillId": "",
        "strictCommittedSelection": False,
    }


def test_custom_dropdown_rejects_open_menu_without_committed_selection(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_control_selection_readback",
        lambda page, field: ([], True),
    )

    with pytest.raises(RuntimeError, match="dropdown remained open without a committed selection"):
        python_runtime._verify_control_selection(None, {"id": "source-control"}, "Company website")


def test_control_selection_readback_ignores_raw_combobox_query_without_committed_value():
    class Locator:
        @property
        def first(self):
            return self

        def input_value(self):
            return "United States"

    class Page:
        def locator(self, _selector):
            return Locator()

        def evaluate(self, script, arg=None):
            if "const visibleText" in str(script):
                return {"values": [], "expanded": False}
            return None

    values, expanded = python_runtime._control_selection_readback(
        Page(),
        {"id": "country", "role": "combobox", "tag": "input"},
    )

    assert values == []
    assert expanded is False


def test_location_combobox_readback_accepts_closed_autocomplete_input_value():
    class Locator:
        @property
        def first(self):
            return self

        def input_value(self):
            return "Jersey City, New Jersey, United States"

    class Page:
        def locator(self, _selector):
            return Locator()

        def evaluate(self, script, arg=None):
            if "const visibleText" in str(script):
                return {"values": [], "expanded": False}
            return None

    values, expanded = python_runtime._control_selection_readback(
        Page(),
        {"label": "Location", "role": "combobox", "tag": "input", "autofillId": "6"},
    )

    assert values == ["Jersey City, New Jersey, United States"]
    assert expanded is False
    assert python_runtime._verify_control_selection(
        Page(),
        {"label": "Location", "role": "combobox", "tag": "input", "autofillId": "6"},
        "Jersey City, NJ, USA",
    ) == "Jersey City, New Jersey, United States"


def test_lever_current_location_commit_sets_real_location_quietly():
    class Page:
        url = "https://jobs.lever.co/palantir/example/apply"

        def evaluate(self, script, arg=None):
            assert "setQuietValue" in str(script)
            assert arg["value"] == "Jersey City, New Jersey, United States"
            return {
                "inputValue": arg["value"],
                "hiddenValue": arg["value"],
                "inputValid": True,
            }

    field = {"label": "Current location", "id": "location-input", "name": "location"}

    assert python_runtime._commit_lever_current_location(
        Page(),
        field,
        "Jersey City, New Jersey, United States",
    ) == "Jersey City, New Jersey, United States"


def test_lever_current_location_commit_is_scoped_to_lever_only():
    class Page:
        url = "https://example.com/apply"

        def evaluate(self, script, arg=None):
            raise AssertionError("non-Lever pages must not use Lever location fallback")

    assert python_runtime._commit_lever_current_location(
        Page(),
        {"label": "Current location", "id": "location-input"},
        "Jersey City, New Jersey, United States",
    ) is None


def test_lever_location_candidates_expand_state_and_country_abbreviations():
    assert python_runtime._lever_location_candidates("Jersey City, NJ, USA")[:2] == [
        "Jersey City, NJ, USA",
        "Jersey City, New Jersey, United States",
    ]


def test_control_selection_readback_keeps_raw_text_for_non_combobox_fallback():
    class Locator:
        @property
        def first(self):
            return self

        def input_value(self):
            return "Mobile"

    class Page:
        def locator(self, _selector):
            return Locator()

        def evaluate(self, script, arg=None):
            if "const visibleText" in str(script):
                return {"values": [], "expanded": False}
            return None

    values, expanded = python_runtime._control_selection_readback(
        Page(),
        {"id": "phone-type", "role": "", "tag": "input"},
    )

    assert values == ["Mobile"]
    assert expanded is False


def test_control_selection_readback_script_ignores_expanded_popup_highlight_as_committed_selection():
    class Locator:
        @property
        def first(self):
            return self

        def input_value(self):
            return ""

    class Page:
        def locator(self, _selector):
            return Locator()

        def evaluate(self, script, arg=None):
            text = str(script)
            assert 'candidateRoot.querySelectorAll(\'[data-automation-id="selectedItem"]\')' in text
            assert "if (!expanded)" in text
            assert '[aria-selected="true"], [aria-checked="true"], [data-state="selected"], [data-state="checked"], [data-state="on"]' in text
            return {"values": [], "expanded": True}

    values, expanded = python_runtime._control_selection_readback(
        Page(),
        {"id": "address--countryRegion", "role": "combobox", "tag": "button"},
    )

    assert values == []
    assert expanded is True


def test_required_field_audit_script_treats_selected_presentation_as_committed_for_non_role_input():
    class Page:
        def evaluate(self, script):
            text = str(script)
            assert 'const committed = selectedPresentation(control);' in text
            assert 'control.getAttribute("aria-haspopup") || committed' in text
            assert 'control.getAttribute("aria-describedby")' in text
            return [
                {
                    "label": "State*",
                    "reason": "required field remains empty after fill",
                }
            ]

    assert python_runtime._audit_required_fields(Page()) == [
        {
            "label": "State*",
            "reason": "required field remains empty after fill",
        }
    ]


def test_required_field_audit_uses_group_labels_for_unanswered_radio_controls():
    class Page:
        def evaluate(self, script):
            text = str(script)
            assert "const groupLabelFor = (control) =>" in text
            assert '? groupLabelFor(control) : labelFor(control);' in text
            return [
                {
                    "label": "Have you previously worked or are currently employed in any capacity with Siemens Healthineers?*",
                    "reason": "required field remains empty after fill",
                }
            ]

    assert python_runtime._audit_required_fields(Page()) == [
        {
            "label": "Have you previously worked or are currently employed in any capacity with Siemens Healthineers?*",
            "reason": "required field remains empty after fill",
        }
    ]


def test_form_field_signature_detects_workday_same_title_subpage_change():
    disclosures = [
        {
            "kind": "buttongroup",
            "tag": "button",
            "type": "button",
            "id": "primaryQuestionnaire--race",
            "name": "",
            "label": "Race",
            "required": True,
        }
    ]
    terms = [
        {
            "kind": "single",
            "tag": "input",
            "type": "checkbox",
            "id": "terms-and-conditions",
            "name": "",
            "label": "Yes, I have read and consent to the terms and conditions.*",
            "required": True,
        }
    ]

    assert python_runtime._form_field_signature(disclosures) != python_runtime._form_field_signature(terms)


class _DataDomeCaptchaPage:
    def evaluate(self, script, arg=None):
        return {
            "url": "https://jobs.smartrecruiters.com/oneclick-ui/company/acme/publication/123",
            "title": "smartrecruiters.com",
            "text": "",
            "recaptcha": True,
        }


class _CloudflareTurnstilePage:
    def evaluate(self, script, arg=None):
        return {
            "url": "https://apply.workable.com/acme/j/123/apply/",
            "title": "Application",
            "text": "Verify you are human Submitting...",
            "recaptcha": True,
        }


class _RecaptchaResubmitPage:
    def evaluate(self, script, arg=None):
        return {
            "url": "https://job-boards.greenhouse.io/waymark/jobs/1",
            "title": "Application",
            "text": "Please complete the reCAPTCHA and resubmit your application.",
            "recaptcha": True,
        }


class _GreenhouseEmbedTokenPage:
    def evaluate(self, script, arg=None):
        return {
            "url": "https://job-boards.greenhouse.io/embed/job_app",
            "title": "Application",
            "text": "Please complete the reCAPTCHA and resubmit your application.",
            "recaptcha": True,
        }


class _ButtonLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def click(self):
        self.page.clicked.append(self.selector)


class _ButtonPage:
    def __init__(self):
        self.clicked = []

    def evaluate(self, script, arg=None):
        if "querySelectorAll(\"button, input[type='button'], input[type='submit'], a\")" in str(script):
            return [
                {
                    "text": "Next",
                    "id": "",
                    "className": "ui-datepicker-next",
                    "title": "Next",
                    "ariaLabel": "",
                    "tag": "a",
                    "type": "",
                    "href": "https://jobs.example.com/apply#",
                    "inForm": False,
                    "inDatepicker": True,
                    "y": 10,
                    "autofillId": "0",
                },
                {
                    "text": "Submit Application",
                    "id": "resumator-submit-resume",
                    "className": "",
                    "title": "",
                    "ariaLabel": "",
                    "tag": "a",
                    "type": "",
                    "href": "https://jobs.example.com/apply#",
                    "inForm": True,
                    "inDatepicker": False,
                    "y": 500,
                    "autofillId": "1",
                },
            ]
        return False

    def locator(self, selector):
        return _ButtonLocator(self, selector)

    def get_by_text(self, text, exact=False):
        return _ButtonLocator(self, f"text={text}")


def test_detects_and_fills_email_verification_code():
    page = _VerificationPage()

    assert "security code" in python_runtime._detect_email_verification_request(page)
    assert python_runtime._fill_email_verification_code(page, "8jmDVPeT") is True
    assert page.filled == "8jmDVPeT"


def test_email_verification_code_rejects_a_stale_wait_file(tmp_path, monkeypatch):
    code_file = tmp_path / "greenhouse-code.txt"
    code_file.write_text("NRjGB1c5\n")
    monkeypatch.delenv("JOB_AGENT_EMAIL_VERIFICATION_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_GREENHOUSE_SECURITY_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_SECURITY_CODE", raising=False)
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE", str(code_file))
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS", "0")

    assert python_runtime._email_verification_code() is None


def test_email_verification_code_accepts_a_file_written_after_request(tmp_path, monkeypatch):
    code_file = tmp_path / "greenhouse-code.txt"
    code_file.write_text("NRjGB1c5\n")
    monkeypatch.delenv("JOB_AGENT_EMAIL_VERIFICATION_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_GREENHOUSE_SECURITY_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_SECURITY_CODE", raising=False)
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE", str(code_file))
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS", "0")

    assert python_runtime._email_verification_code(requested_after_ns=1) == "NRjGB1c5"


def test_email_verification_code_file_takes_precedence_over_gmail(tmp_path, monkeypatch):
    code_file = tmp_path / "greenhouse-code.txt"
    code_file.write_text("NRjGB1c5\n")
    monkeypatch.delenv("JOB_AGENT_EMAIL_VERIFICATION_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_GREENHOUSE_SECURITY_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_SECURITY_CODE", raising=False)
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE", str(code_file))
    monkeypatch.setenv("JOB_AGENT_GMAIL_TOKEN_FILE", "configured-token.json")
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS", "0")
    monkeypatch.setattr(
        python_runtime,
        "wait_for_verification_code",
        lambda *_args, **_kwargs: "gmail-code",
    )

    assert python_runtime._email_verification_code(requested_after_ns=1) == "NRjGB1c5"


def test_email_verification_gmail_query_uses_no_stale_grace_by_default(monkeypatch):
    captured = {}

    monkeypatch.delenv("JOB_AGENT_EMAIL_VERIFICATION_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_GREENHOUSE_SECURITY_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_SECURITY_CODE", raising=False)
    monkeypatch.delenv("JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE", raising=False)
    monkeypatch.delenv("JOB_AGENT_GMAIL_VERIFICATION_GRACE_SECONDS", raising=False)
    monkeypatch.setenv("JOB_AGENT_GMAIL_TOKEN_FILE", "configured-token.json")
    monkeypatch.setenv("JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS", "0")

    def fake_wait(*_args, **kwargs):
        captured.update(kwargs)
        return "gmail-code"

    monkeypatch.setattr(python_runtime, "wait_for_verification_code", fake_wait)

    assert python_runtime._email_verification_code(requested_after_ns=1_234_000_000) == "gmail-code"
    assert captured["requested_after_ms"] == 1234


def test_find_button_ignores_datepicker_next_and_clicks_hash_anchor_by_element():
    page = _ButtonPage()

    assert python_runtime._find_button(page, "next") is None
    submit = python_runtime._find_button(page, "submit")

    assert submit["id"] == "resumator-submit-resume"
    python_runtime._click_button(page, submit)
    assert page.clicked == ['[data-job-agent-button-index="1"]']


def test_find_button_detects_translated_submit_label():
    class Page:
        def evaluate(self, _script):
            return [
                {
                    "text": "\u63d0\u4ea4\u7533\u8bf7",
                    "id": "",
                    "className": "",
                    "title": "",
                    "ariaLabel": "",
                    "automationId": "",
                    "tag": "button",
                    "type": "button",
                    "href": "",
                    "inForm": True,
                    "inDatepicker": False,
                    "y": 900,
                    "autofillId": "0",
                }
            ]

    submit = python_runtime._find_button(Page(), "submit")

    assert submit is not None
    assert submit["text"] == "\u63d0\u4ea4\u7533\u8bf7"


def test_citizenship_employment_eligibility_selects_non_citizen_work_authorized():
    field = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "What's your citizenship / employment eligibility?*",
        "options": [
            "No answer",
            "I am a U.S. Citizen/Permanent Resident",
            "Non-citizen allowed to work for any employer",
            "Non-citizen seeking work authorization",
            "Other",
        ],
    }
    profile = {"answers": {}, "sensitive_answers": {"citizenship": {"approved": True, "answer": "No", "patterns": ["citizenship"]}}}

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "skip"
    assert plan["reason"] == "non-required unmapped field"


def test_optional_ai_screening_consent_is_left_unchecked():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I agree to proceed with AI Screening for this application.",
        "required": False,
    }

    plan = python_runtime._plan_field(
        field,
        {"answers": {"AI Policy for Application": "Yes"}, "sensitive_answers": {}},
        None,
    )

    assert plan == {"action": "skip", "reason": "non-required unmapped field", "blocking": False}


def test_plan_field_skips_if_yes_clearance_followup_when_gate_is_no():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "If Yes, Which level of Security Clearance Do You Hold?*",
        "required": True,
        "options": [],
    }
    profile = {
        "sensitive_answers": {
            "active_security_clearance": {
                "patterns": ["active security clearance", "hold a security clearance"],
                "answer": "No",
                "approved": True,
            }
        },
        "answers": {},
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "skip"
    assert plan["blocking"] is False
    assert "not applicable" in plan["reason"]


def test_plan_field_keeps_if_yes_clearance_followup_blocked_when_gate_is_yes():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "If Yes, Which level of Security Clearance Do You Hold?*",
        "required": True,
        "options": [],
    }
    profile = {
        "sensitive_answers": {
            "active_security_clearance": {
                "patterns": ["active security clearance"],
                "answer": "Yes",
                "approved": True,
            }
        },
        "answers": {},
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert "not applicable" not in plan["reason"]


def test_required_terms_and_conditions_consent_is_checked():
    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "checkbox",
            "label": "Yes, I have read and consent to the terms and conditions.*",
            "required": True,
        },
        {"sensitive_answers": {"legal_attestation": {"approved": True, "answer": "Yes", "patterns": ["terms and conditions"]}}},
        None,
    )

    assert plan == {"action": "check"}


def test_workday_terms_conditions_use_approved_legal_attestation_without_exact_pattern():
    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "checkbox",
            "label": "Yes, I have read and consent to the terms and conditions*",
            "required": True,
        },
        {
            "sensitive_answers": {
                "legal_attestation": {
                    "approved": True,
                    "answer": "Yes",
                    "patterns": ["i certify", "true and accurate", "arbitration agreement"],
                }
            }
        },
        None,
    )

    assert plan == {"action": "check"}


def test_workday_by_clicking_terms_conditions_uses_terms_consent():
    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "checkbox",
            "label": 'By clicking the "Save and Continue" button, I agree to the terms and conditions.*',
            "required": True,
        },
        {
            "sensitive_answers": {
                "terms_consent": {
                    "approved": True,
                    "answer": "Yes",
                    "patterns": ["terms and conditions"],
                }
            }
        },
        None,
    )

    assert plan == {"action": "check"}


def test_candidate_account_creation_consent_checkbox_uses_approved_privacy_answer():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Agree",
        "automationId": "createAccountCheckbox",
        "required": False,
    }
    profile = {
        "sensitive_answers": {
            "privacy_consent": {
                "patterns": ["process your personal data"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }

    plan = python_runtime._plan_field(
        field,
        profile,
        None,
        runtime_context={"candidate_account_creation": True},
    )

    assert plan["action"] == "check"
    assert plan["sensitive"] is True


def test_candidate_account_creation_consent_checkbox_blocks_without_approved_answer():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Agree",
        "automationId": "createAccountCheckbox",
        "required": False,
    }

    plan = python_runtime._plan_field(
        field,
        {"sensitive_answers": {}},
        None,
        runtime_context={"candidate_account_creation": True},
    )

    assert plan == {
        "action": "skip",
        "reason": "candidate account creation privacy consent needs approved answer",
        "sensitive": True,
        "blocking": True,
    }


def test_at_least_three_years_select_uses_profile_experience_truthfully():
    field = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Do you have at least 3 years of professional experience in Data Science or Machine Learning Engineering?*",
        "options": ["-- No answer --", "Yes", "No"],
    }

    plan = python_runtime._plan_field(field, {"years_experience": "1-2", "answers": {}, "sensitive_answers": {}}, None)

    assert plan == {"action": "select", "value": "No"}


def test_1099_without_sponsorship_or_paperwork_selects_no():
    field = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Are you legally authorized to work in the U.S. as a 1099 independent contractor without requiring Fusemachines to provide sponsorship or complete any paperwork on your behalf?*",
        "options": ["-- No answer --", "Yes", "No"],
    }
    profile = {
        "years_experience": "1-2",
        "answers": {},
        "sensitive_answers": {
            "work_authorization_us": {
                "approved": True,
                "answer": "Yes",
                "patterns": ["legally authorized to work in the united states"],
            },
            "sponsorship": {
                "approved": True,
                "answer": "Yes",
                "patterns": ["sponsorship"],
            },
        },
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {"action": "select", "value": "No"}


def test_map_text_value_uses_explicit_work_and_education_entries():
    profile = {
        "work_history": [
            {"title": "Engineer", "company": "OldCo", "current": False},
            {"title": "Staff Engineer", "company": "Acme AI", "current": True},
        ],
        "education": [
            {"school": "State University", "degree": "BS", "field": "Computer Science"}
        ],
        "years_experience": "4",
        "name_pronunciation": "YOW-ee Woo",
    }

    assert python_runtime._map_text_value("Current company", profile) == "Acme AI"
    assert python_runtime._map_text_value("Current role", profile) == "Staff Engineer"
    assert python_runtime._map_text_value("How many years of relevant experience do you have?", profile) == "4"
    assert python_runtime._map_text_value("Name Pronunciation | How do you pronounce your name?", profile) == "YOW-ee Woo"
    assert python_runtime._map_text_value("Which university did you last attend?", profile) == "State University"
    assert python_runtime._map_text_value("Degree", profile) == "BS"
    assert python_runtime._map_text_value("Major", profile) == "Computer Science"


def test_greenhouse_education_date_components_are_planned_from_iso_date():
    profile = {
        "education": [
            {
                "start_date": "2024-09",
                "end_date": "2026-05",
            }
        ]
    }
    end_month = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "End date month*",
        "id": "end-month--0",
        "role": "combobox",
        "section": "education",
        "required": True,
        "value": "",
    }
    end_year = {
        "kind": "single",
        "tag": "input",
        "type": "number",
        "label": "End date year*",
        "id": "end-year--0",
        "section": "education",
        "required": True,
        "value": "",
    }
    start_month = {**end_month, "label": "Start date month*", "id": "start-month--0"}

    assert python_runtime._education_date_part(profile, "end", "month") == "May"
    assert python_runtime._education_date_part(profile, "end", "year") == "2026"
    assert python_runtime._plan_field(end_month, profile, None) == {"action": "combobox", "value": "May"}
    assert python_runtime._plan_field(end_year, profile, None) == {"action": "fill", "value": "2026"}
    assert python_runtime._plan_field(start_month, profile, None) == {"action": "combobox", "value": "September"}


def test_plan_field_uses_autocomplete_metadata_when_label_is_missing():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "email",
        "label": "",
        "id": "candidate-email-42",
        "autocomplete": "email",
        "ariaLabel": "Applicant contact email",
        "required": True,
    }

    assert python_runtime._plan_field(field, {"email": "candidate@example.com"}, None) == {
        "action": "fill",
        "value": "candidate@example.com",
    }


def test_required_field_audit_reports_browser_state_that_did_not_persist():
    class Page:
        def evaluate(self, script):
            assert "required field remains empty after fill" in script
            return [
                {
                    "label": "End date year",
                    "reason": "browser reports field as invalid",
                }
            ]

    assert python_runtime._audit_required_fields(Page()) == [
        {
            "label": "End date year",
            "reason": "browser reports field as invalid",
        }
    ]


def test_email_verification_field_is_nonblocking_until_after_submit():
    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "label": "Security code",
            "required": True,
        },
        {},
        None,
    )

    assert plan == {
        "action": "skip",
        "reason": "email verification handled after submit",
        "blocking": False,
    }


def test_final_required_audit_discards_only_stale_combobox_failure():
    review = [
        {
            "label": "Gender",
            "reason": "fill error: no combobox option matches saved answer",
            "blocking": True,
        },
        {
            "label": "Phone",
            "reason": "browser reports field as invalid",
            "blocking": True,
        },
    ]

    assert python_runtime._retain_unresolved_control_reviews(
        review,
        [{"label": "Phone", "reason": "browser reports field as invalid"}],
    ) == [review[1]]


def test_final_required_audit_discards_stale_combobox_no_progress_failure():
    review = [
        {
            "label": "Country",
            "reason": (
                "fill error: combobox made no progress before field repair "
                "deadline: Country"
            ),
            "blocking": True,
        }
    ]

    assert python_runtime._retain_unresolved_control_reviews(review, []) == []
    assert python_runtime._retain_unresolved_control_reviews(
        review,
        [{"label": "Country", "reason": "required field remains empty after fill"}],
    ) == review


def test_required_audit_ignores_email_verification_prompt():
    review: list[dict] = []

    python_runtime._append_required_audit(
        review,
        [
            {
                "label": "A verification code was sent to candidate@example.com. Enter the 8-character code.",
                "reason": "required field remains empty after fill",
            }
        ],
    )

    assert review == []


def test_required_audit_ignores_successful_fill_readback_for_same_label():
    review: list[dict] = []

    python_runtime._append_required_audit(
        review,
        [
            {
                "label": "Degree Type",
                "reason": "required field remains empty after fill",
            }
        ],
        filled=[
            {
                "label": "Degree Type",
                "action": "checkmany",
                "readback": "selected: Master's Degree",
            }
        ],
    )

    assert review == []


def test_required_audit_ignores_low_risk_invalid_when_readback_succeeded():
    review: list[dict] = []

    python_runtime._append_required_audit(
        review,
        [
            {
                "label": "Are you looking for a full-time or internship job?✱Full-timeInternshipBoth",
                "reason": "browser reports field as invalid",
            },
            {
                "label": "When will you graduate? (month & year)✱",
                "reason": "browser reports field as invalid",
            },
        ],
        filled=[
            {
                "label": "Are you looking for a full-time or internship job?",
                "action": "check",
                "readback": "selected: Full-time",
            },
            {
                "label": "When will you graduate? (month & year)",
                "action": "fill",
                "readback": "May 2026",
            },
        ],
    )

    assert review == []


def test_successful_readback_filter_removes_stale_low_risk_invalid_review():
    review = [
        {
            "label": "Email✱",
            "reason": "browser reports field as invalid",
            "blocking": True,
        },
        {
            "label": "Do you now or will you require immigration sponsorship?",
            "reason": "browser reports field as invalid",
            "blocking": True,
        },
    ]
    filled = [
        {
            "label": "Email",
            "action": "fill",
            "readback": "criswu20010728@gmail.com",
        },
        {
            "label": "Do you now or will you require immigration sponsorship?",
            "action": "check",
            "readback": "selected: Yes",
        },
    ]

    assert python_runtime._filter_successful_readback_reviews(review, filled) == []


def test_successful_readback_filter_covers_citizen_and_authorization_groups():
    review = [
        {
            "label": "Are you a U.S. citizen?✱YesNo",
            "reason": "browser reports field as invalid",
            "blocking": True,
        },
        {
            "label": "Are you legally authorized to work in the United States?✱",
            "reason": "browser reports field as invalid",
            "blocking": True,
        },
    ]
    filled = [
        {
            "label": "Are you a U.S. citizen?",
            "action": "check",
            "readback": "selected: No",
        },
        {
            "label": "Are you legally authorized to work in the United States?",
            "action": "check",
            "readback": "selected: Yes",
        },
    ]

    assert python_runtime._filter_successful_readback_reviews(review, filled) == []


def test_required_audit_keeps_invalid_without_successful_readback():
    review: list[dict] = []

    python_runtime._append_required_audit(
        review,
        [
            {
                "label": "Are you a U.S. citizen?",
                "reason": "browser reports field as invalid",
            }
        ],
    )

    assert len(review) == 1


def test_map_text_value_does_not_guess_missing_work_or_education():
    assert python_runtime._map_text_value("Current company", {}) is None
    assert python_runtime._map_text_value("How many years of relevant experience do you have?", {}) is None
    assert python_runtime._map_text_value("Which university did you last attend?", {}) is None


def test_map_text_value_handles_ashby_contact_and_current_location_labels():
    profile = {
        "name": "Gaoyi Wu",
        "phone": "+1 (201) 283-4980",
        "location": "Jersey City, NJ, USA",
    }

    assert python_runtime._map_text_value("Name", profile) == "Gaoyi Wu"
    assert python_runtime._map_text_value("Contact number", profile) == "+1 (201) 283-4980"
    assert python_runtime._map_text_value("Where are you currently based?", profile) == "Jersey City, NJ, USA"


def test_map_text_value_does_not_use_name_for_pronunciation():
    profile = {"name": "Your Name"}

    assert python_runtime._map_text_value("Name Pronunciation | How do you pronounce your name?", profile) is None


def test_map_text_value_does_not_treat_addressing_as_address():
    profile = {"address_line1": "132 New York Avenue", "location": "Jersey City, NJ"}

    assert python_runtime._map_text_value("What pronouns should we use when addressing you?", profile) is None


def test_map_text_value_does_not_treat_schoolwork_essay_as_school_field():
    profile = {"education": [{"school": "State University"}]}

    assert python_runtime._map_text_value(
        "Tell us about a time outside of your schoolwork where you took ownership.",
        profile,
    ) is None


def test_auto_answer_handles_perpay_new_grad_screening_fields():
    onsite_label = "I understand this is an in-person role in Philadelphia, PA.*"
    career_fair_label = "Did you attend a 2026 spring career fair?*"
    profile = {
        "target_company": "Perpay",
        "answers": {
            "Are you open to relocation?": "Yes",
            onsite_label: "Yes",
            career_fair_label: "No",
        },
        "sensitive_answers": {
            "relocation": {
                "patterns": ["relocation", "relocate"],
                "answer": "Yes",
                "approved": True,
            }
        },
        "projects": [
            {
                "title": "XClaw: Desktop Interface for Open Claw",
                "url": "https://github.com/Alfred768/xclaw",
            }
        ],
    }

    assert python_runtime._auto_answer(
        onsite_label,
        profile,
    ) == "Yes"
    assert python_runtime._auto_answer(career_fair_label, profile) == "No"
    assert python_runtime._auto_answer(
        "Do you have a personal project you're proud of that you'd like to share?*",
        profile,
    ) == "Yes"
    project_answer = python_runtime._auto_answer(
        "If you have a personal project, share the link and tell us about it!",
        profile,
    )
    assert "XClaw" in project_answer
    assert "https://github.com/Alfred768/xclaw" in project_answer
    assert "mission-driven product work" in python_runtime._auto_answer(
        "What excites you about this opportunity?*",
        profile,
    )
    assert "Intellisys Lab" in python_runtime._auto_answer(
        "Describe a situation and your response to it that shows how you demonstrate a high level of grit.*",
        profile,
    )
    assert "DHL Express" in python_runtime._auto_answer(
        "Tell us about a time you took full ownership of a challenging moment (outside of your schoolwork), and saw it through to the end.*",
        profile,
    )
    built_app_answer = python_runtime._auto_answer(
        "Tell us about an application you built yourself. What was the problem you were solving, "
        "how did you build it, how did you measure success of the application?*",
        profile,
    )
    assert "XClaw" in built_app_answer
    assert "50+ execution skills" in built_app_answer
    built_ownership_answer = python_runtime._auto_answer(
        "This role doesn't require professional experience -- it requires the right instincts. "
        "Tell us about a time you took ownership of something difficult, figured it out without "
        "a playbook, and saw it through. Could be academic, a side project, a job, anything.*",
        profile,
    )
    assert "DHL Express" in built_ownership_answer
    assert "reduced model reporting latency by 30%" in built_ownership_answer


def test_school_combobox_detection_excludes_schoolwork_essay():
    assert python_runtime._is_school_combobox_field({"label": "School"}) is True
    assert python_runtime._is_school_combobox_field({"label": "Which university did you attend?"}) is True
    assert (
        python_runtime._is_school_combobox_field(
            {"label": "Tell us about a time outside of your schoolwork where you took ownership."}
        )
        is False
    )


def test_map_text_value_does_not_treat_long_work_country_question_as_address_country():
    question = (
        "Will you now or at any time in the future require employer sponsorship "
        "to obtain or maintain employment authorization to work in the country "
        "where this role is based?"
    )

    assert python_runtime._map_text_value(question, {"country": "United States"}) is None


def test_map_text_value_does_not_treat_ethnicity_as_city():
    assert python_runtime._map_text_value(
        "Race/Ethnicity",
        {"city": "New York", "location": "New York, NY"},
    ) is None


def test_workday_address_fields_require_explicit_address_values():
    profile = {
        "location": "New York, NY",
        "phone": "+1 555 0100",
        "address_line1": "123 Main St",
        "postal_code": "10001",
    }

    assert python_runtime._map_text_value("Address Line 1", profile) == "123 Main St"
    assert python_runtime._map_text_value("Address", profile) == "123 Main St"
    assert python_runtime._map_text_value("Address Line 2", profile) is None
    assert python_runtime._map_text_value("City", profile) == "New York"
    assert python_runtime._map_text_value("Postal Code", profile) == "10001"
    assert python_runtime._map_text_value("Country Phone Code*", profile) == "United States of America (+1)"


def test_workday_address_fields_fall_back_to_answer_bank_values():
    profile = {
        "location": "Jersey City, NJ, USA",
        "phone": "+1 (201) 283-4980",
        "answers": {
            "Address": "132 New York Avenue",
            "Address 2": "",
            "Postal Code": "07307",
        },
    }

    assert python_runtime._map_text_value("Address Line 1*", profile) == "132 New York Avenue"
    assert python_runtime._map_text_value("Postal Code", profile) == "07307"


def test_option_matches_united_states_variants_and_us_phone_code():
    assert python_runtime._option_matches("United States of America", "United States")
    assert python_runtime._option_matches("United States of America (+1)", "+1")


def test_greenhouse_uses_local_phone_digits_and_street_address():
    profile = {
        "_application_url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "phone": "+1 (201) 283-4980",
        "phone_country_code": "+1",
        "address_line1": "132 New York Avenue",
        "location": "Jersey City, NJ, USA",
    }
    phone = {"id": "phone", "label": "Phone*", "tag": "input", "type": "text"}

    assert python_runtime._map_text_value(phone, profile) == "2012834980"
    assert python_runtime._map_text_value(
        "What is the address from which you plan on working?*", profile
    ) == "132 New York Avenue"


def test_standalone_country_combobox_does_not_fuzzily_match_screening_answers():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Country*",
        "id": "country",
        "required": True,
    }
    profile = {
        "country": "United States",
        "answers": {
            "Are you authorized to work in the country for which you are applying?": "Yes"
        },
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "United States",
    }


def test_lyft_work_authorization_combobox_uses_sponsorship_specific_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Work Authorization*",
        "required": True,
    }
    profile = {
        "_application_url": "https://app.careerpuck.com/job-board/lyft/job/8402813002?gh_jid=8402813002",
        "sensitive_answers": {
            "work_authorization_current_country": {
                "patterns": ["authorized to work in the country"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {
                "patterns": ["sponsorship"],
                "answer": "Yes",
                "approved": True,
            },
        },
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "combobox"
    assert plan["value"].startswith("I require/will require Lyft's sponsorship")


def test_lyft_greenhouse_screening_comboboxes_use_profile_approved_answers():
    profile = {
        "_application_url": "https://app.careerpuck.com/job-board/lyft/job/8402813002?gh_jid=8402813002",
        "work_history": [{"company": "Current Lab", "current": True}],
        "sensitive_answers": {
            "relocation": {"patterns": ["relocation", "relocate"], "answer": "Yes", "approved": True},
            "privacy_consent": {"patterns": ["privacy policy"], "answer": "Yes", "approved": True},
        },
        "answers": {},
    }

    cases = [
        ("Please enter your relevant employment and military service above using the + Add Another Employment link.*", "Thank you"),
        ("May we contact your current employer?*", "No"),
        ("Can you perform these essential functions of the job with reasonable accommodation?*", "Yes"),
        (
            "Please review the linked document:*",
            "I acknowledge that I have read and understood the terms of the Lyft Candidate Privacy Notice.",
        ),
        (
            "This position is based in the United States. Do you currently reside in commutable proximity to a Lyft Office located in San Francisco or are you open to relocating?*",
            "I am willing to relocate before starting employment.",
        ),
        (
            "Have you been employed by Lyft, or any subsidiary, affiliate, or business unit of Lyft, in the past (whether on a full-time or part-time basis)?*",
            "No",
        ),
    ]
    for label, value in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
        }

        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": value,
        }


def test_airbnb_greenhouse_privacy_policy_uses_exact_acknowledgement():
    profile = {
        "target_company": "airbnb",
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["authorized to work"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {"patterns": ["sponsorship"], "answer": "Yes", "approved": True},
            "privacy_consent": {"patterns": ["privacy policy"], "answer": "Yes", "approved": True},
        },
        "answers": {},
        "screening_answer_rules": [
            {"patterns": ["non-compete", "non-solicitation"], "answer": "No"},
            {"patterns": ["ever worked for Airbnb"], "answer": "No"},
        ],
    }
    privacy = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Airbnb Candidate Privacy Policy*",
        "required": True,
    }

    assert python_runtime._plan_field(privacy, profile, None) == {
        "action": "combobox",
        "value": "I acknowledge that I have read and understood the Airbnb Candidate Privacy Policy.",
    }


def test_airbnb_greenhouse_screening_fields_use_truthful_specific_answers():
    profile = {
        "target_company": "airbnb",
        "screening_answer_rules": [
            {"patterns": ["non-compete or non-solicitation"], "answer": "No"},
            {"patterns": ["ever worked for Airbnb"], "answer": "No"},
        ],
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["authorized to work"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {"patterns": ["sponsorship"], "answer": "Yes", "approved": True},
        },
        "answers": {},
    }

    cases = [
        (
            "Are you legally authorized to work in the country where the job is located? *",
            "Yes, I am currently legally authorized to work in the country where the job is located.",
        ),
        (
            "Will you now or in the future require company sponsorship to retain or extend your work authorization in the country where the job is located?*",
            "Yes, I will require immigration sponsorship in the future to legally work in the country where the job is located.",
        ),
        (
            "Are you currently subject to any non-compete or non-solicitation agreement that would impact your ability to work at Airbnb or prevent you from accepting a job offer from Airbnb? *",
            "No",
        ),
        (
            "Are you currently or have you ever worked for Airbnb in any capacity? This could include, but is not limited to, a full-time employee, intern, apprentice, or contingent worker.*",
            "No",
        ),
    ]
    for label, value in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": value,
        }

    community_support = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Do you have previous experience in the Community Support domain? Briefly describe in 2-4 bullet points/sentences. *",
        "required": True,
    }
    plan = python_runtime._plan_field(community_support, profile, None)
    assert plan["action"] == "fill"
    assert "I do not have direct Community Support domain experience" in plan["value"]


def test_xai_greenhouse_screening_fields_use_truthful_specific_answers():
    profile = {
        "target_company": "xAI",
        "answers": {"How did you hear about us?": "Company website"},
        "screening_answer_rules": [
            {
                "patterns": ["SpaceXAI employment history"],
                "answer": "I have never worked for SpaceX or SpaceXAI",
            }
        ],
    }
    employment = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please indicate your SpaceXAI employment history.*",
        "required": True,
    }
    exceptional = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What exceptional work have you done?*",
        "required": True,
    }
    source = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "How did you hear about us?*",
        "required": True,
    }

    assert python_runtime._plan_field(employment, profile, None) == {
        "action": "combobox",
        "value": "I have never worked for SpaceX or SpaceXAI",
    }
    exceptional_plan = python_runtime._plan_field(exceptional, profile, None)
    assert exceptional_plan["action"] == "fill"
    assert "Intellisys Lab" in exceptional_plan["value"]
    assert python_runtime._plan_field(source, profile, None) == {
        "action": "combobox",
        "value": "Company careers page / website",
    }


def test_contractor_consultant_prior_engagement_screening_defaults_to_no():
    label = (
        "Have you previously or are you currently engaged with BMS as a contractor, "
        "consultant, former employee, or any other role that required/requires you "
        "to have access to BMS systems? If yes, please answer the questions below. "
        "If not, please continue to the next item.*"
    )
    field = {
        "kind": "radiogroup",
        "tag": "div",
        "label": label,
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(
        field,
        {
            "answers": {},
            "personal_us_company_employment_history": "Never worked for a United States company.",
        },
        None,
    ) == {
        "action": "check",
        "option": "No",
    }


def test_company_specific_contract_work_screening_defaults_to_no():
    field = {
        "kind": "single",
        "tag": "select",
        "label": (
            "Have you ever provided any contract work for Block, Inc. or any "
            "of its subsidiaries or affiliates?*"
        ),
        "required": True,
        "options": ["Select...", "Yes", "No"],
    }
    assert python_runtime._plan_field(
        field,
        {
            "answers": {},
            "personal_us_company_employment_history": "Never worked for a United States company.",
        },
        None,
    ) == {
        "action": "select",
        "value": "No",
    }


def test_company_specific_employment_combobox_defaults_to_no():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Have you been employed with The Trade Desk?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    assert python_runtime._plan_field(
        field,
        {
            "answers": {},
            "personal_us_company_employment_history": "Never worked for a United States company.",
        },
        None,
    ) == {
        "action": "combobox",
        "value": "No",
    }


def test_workday_compensation_factor_and_authorization_select_one_use_yes_no():
    profile = {
        "answers": {},
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["authorized to work"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }
    compensation = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "In the event an offer of employment is made, are there any factors BMS "
            "should consider when creating a compensation offer? Please note that "
            "applicants are not required to disclose salary or compensation history.*Select One"
        ),
        "required": True,
    }
    authorized = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you legally authorized to work in the United States for any employer?*Select One",
        "required": True,
    }

    assert python_runtime._plan_field(compensation, profile, None) == {
        "action": "combobox",
        "value": "No",
    }
    assert python_runtime._plan_field(authorized, profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_bms_workday_compliance_select_one_fields_use_deterministic_answers():
    profile = {
        "answers": {},
        "screening_answer_rules": [
            {"patterns": ["relatives", "romantic partners"], "answer": "No"},
            {"patterns": ["willing to commute"], "answer": "Yes"},
            {"patterns": ["List of Excluded"], "answer": "No"},
            {"patterns": ["debarred", "debarment"], "answer": "No"},
            {"patterns": ["licensed physician"], "answer": "No"},
            {"patterns": ["investigated", "investigational drugs"], "answer": "No"},
            {"patterns": ["pending inquiry", "administrative action"], "answer": "No"},
        ],
    }
    labels_and_answers = [
        (
            "BMS seeks to avoid conflicts of interest. Do you have relatives "
            "(including parents), romantic partners, people with whom you share "
            "a dwelling or have a business relationship with who work in any capacity at BMS?* Select One",
            "No",
        ),
        (
            "If applicable, are you willing to commute to the area where this position is located?*Select One",
            "Yes",
        ),
        (
            "Are you or have you ever appeared on the HHS/OIG List of Excluded Individuals/Entities "
            "(available through the Internet at http://www.oig.hhs.gov)?*Select One",
            "No",
        ),
        (
            "Are you or have you ever appeared on the General Services Administration's List of Parties "
            "Excluded from Federal Programs (available through the Internet at http://www.epls.gov)?*Select One",
            "No",
        ),
        ("Are you debarred under the Generic Drug Enforcement Act of 1992?*Select One", "No"),
        ("Are debarment proceedings pending or to your knowledge threatened?*Select One", "No"),
        ("Are you a US licensed physician?*Select One", "No"),
        (
            "Have you ever been investigated for or disqualified or restricted by the FDA or HHS "
            "from receiving investigational drugs?*Select One",
            "No",
        ),
        (
            "Are you currently the subject of any pending inquiry by any governmental entity or "
            "licensing association (whether or not any action has yet been imposed) or has "
            "administrative action been imposed upon you by any governmental entity or licensing association?*Select One",
            "No",
        ),
    ]

    for label, answer in labels_and_answers:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": False,
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": answer,
        }


def test_workday_sponsorship_explanation_does_not_fill_plain_yes():
    profile = {
        "answers": {},
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["employment authorization", "authorized to work"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {"patterns": ["sponsorship"], "answer": "Yes", "approved": True},
        },
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": (
            "If you answered yes, please provide a brief explanation of what will be required "
            "for you to obtain or to maintain your employment authorization?*"
        ),
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "fill"
    assert plan["value"] != "Yes"
    assert "employer sponsorship" in plan["value"]


def test_current_work_end_date_uses_present_month_and_year(monkeypatch):
    class _Today:
        year = 2026
        month = 7

    monkeypatch.setattr(python_runtime, "date", type("Date", (), {"today": staticmethod(lambda: _Today())}))
    profile = {"work_history": [{"company": "Current Lab", "current": True, "end_month": "", "end_year": ""}]}

    assert python_runtime._current_work_value(profile, "end_month") == "July"
    assert python_runtime._current_work_value(profile, "end_year") == "2026"


def test_legal_signature_field_uses_name_and_date(monkeypatch):
    class _Today:
        year = 2026
        month = 7
        day = 18

    monkeypatch.setattr(python_runtime, "date", type("Date", (), {"today": staticmethod(lambda: _Today())}))
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "I certify that the facts set forth in this Application for Employment are true and complete.*",
        "ariaDescription": "Please enter your full name and today's date to signify your electronic signature for this statement.",
        "required": True,
    }
    profile = {
        "name": "Gaoyi Wu",
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["i certify", "true and complete"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "fill",
        "value": "Gaoyi Wu 07/18/2026",
        "sensitive": True,
    }


def test_pronouns_do_not_use_eeo_gender_sensitive_answer():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please share your gender pronouns.",
        "required": False,
    }
    profile = {
        "answers": {"Pronouns": "He/him/his"},
        "sensitive_answers": {
            "eeo_gender": {
                "patterns": ["gender"],
                "answer": "Male",
                "approved": True,
            }
        },
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "He / Him",
    }


def test_pronoun_alias_matches_slash_separated_greenhouse_option():
    assert python_runtime._option_matches("He / Him", "He/him/his")


def test_city_text_field_prefers_structured_profile_over_fuzzy_answer_bank():
    profile = {
        "city": "Jersey City",
        "answers": {"What is your ethnicity?": "East Asian"},
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "City",
        "required": True,
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "fill",
        "value": "Jersey City",
    }


def test_legal_attestation_checkbox_with_true_accurate_context_is_checked():
    profile = {
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["true and accurate", "false or misleading"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": (
            "I confirm that all information I have provided is true and accurate. "
            "I understand that if any information is found to be false or misleading, "
            "this application may be withdrawn or terminated."
        ),
        "required": True,
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check"}


def test_statement_acknowledgement_checkbox_uses_approved_legal_answer():
    profile = {
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["statements"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I have carefully read the above information and I understand and agree to all of the statements.*",
        "required": True,
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check"}


def test_background_check_attestation_uses_approved_legal_answer():
    profile = {
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["background check"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Offer of employment is contingent upon a background check. Do you acknowledge and agree?",
        "required": True,
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "fill", "value": "Yes"}


def test_plan_field_skips_honeypot_robot_fields_without_blocking():
    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "label": "Enter website. This input is for robots only, do not enter if you're human.",
            "name": "website",
            "required": False,
        },
        {"website": "https://example.com"},
        None,
    )

    assert plan == {"action": "skip", "reason": "honeypot field", "blocking": False}


def test_plan_field_blocks_candidate_account_password_creation():
    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "password",
            "label": "Password*",
            "required": False,
        },
        {},
        None,
    )

    assert plan == {"action": "skip", "reason": "candidate account creation required", "blocking": True}


def test_password_fields_use_configured_candidate_account_password(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD", "Generated-Strong-Password-42!")

    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "password",
            "label": "Verify New Password*",
            "required": True,
        },
        {},
        None,
    )

    assert plan == {
        "action": "fill",
        "value": "Generated-Strong-Password-42!",
        "sensitive": True,
    }


def test_password_fields_auto_manage_candidate_account_password_store(tmp_path, monkeypatch):
    store_path = tmp_path / "candidate-passwords.json"
    monkeypatch.delenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD", raising=False)
    monkeypatch.delenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE", str(store_path))

    profile = {
        "email": "candidate@example.com",
        "_application_url": "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply",
    }
    runtime_context = {"candidate_account_creation": True}

    first = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "password",
            "label": "Password*",
            "required": True,
        },
        profile,
        None,
        None,
        runtime_context,
    )
    second = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "password",
            "label": "Verify New Password*",
            "required": True,
        },
        profile,
        None,
        None,
        runtime_context,
    )

    assert first["action"] == "fill"
    assert second["action"] == "fill"
    assert first["value"] == second["value"]
    assert len(first["value"]) >= 16

    store = json.loads(store_path.read_text())
    assert store["accounts"]


def test_workday_password_field_auto_creates_host_password_without_creation_context(tmp_path, monkeypatch):
    store_path = tmp_path / "candidate-passwords.json"
    monkeypatch.delenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD", raising=False)
    monkeypatch.delenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE", str(store_path))

    plan = python_runtime._plan_field(
        {
            "kind": "single",
            "tag": "input",
            "type": "password",
            "label": "Password*",
            "required": True,
        },
        {
            "email": "candidate@example.com",
            "_application_url": "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply",
        },
        None,
        None,
        {},
    )

    assert plan["action"] == "fill"
    assert plan["sensitive"] is True
    assert len(plan["value"]) >= 16
    assert json.loads(store_path.read_text())["accounts"]


def test_workday_candidate_home_is_signed_in_before_application(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD", "Generated-Strong-Password-42!")
    page = _CandidateHomePage()

    assert python_runtime._sign_in_to_candidate_home_if_available(
        page,
        {"email": "candidate@example.com"},
    ) is True
    assert page.signed_in is True
    assert page.filled['[data-automation-id="email"]'] == "candidate@example.com"
    assert '[data-automation-id="password"]' in page.filled


def test_workday_candidate_home_uses_auto_managed_password_store(tmp_path, monkeypatch):
    store_path = tmp_path / "candidate-passwords.json"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    "company.wd5.myworkdayjobs.com\u0000candidate@example.com": {
                        "host": "company.wd5.myworkdayjobs.com",
                        "email": "candidate@example.com",
                        "password": "Stored-Strong-Password-42!",
                    }
                },
            }
        )
    )
    monkeypatch.delenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD", raising=False)
    monkeypatch.delenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE", str(store_path))
    page = _CandidateHomePage()

    assert python_runtime._sign_in_to_candidate_home_if_available(
        page,
        {
            "email": "candidate@example.com",
            "_application_url": "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply",
        },
    ) is True
    assert page.signed_in is True
    assert page.filled['[data-automation-id="password"]'] == "Stored-Strong-Password-42!"


def test_smartrecruiters_im_interested_button_is_application_entry():
    entry = python_runtime._find_application_entry(_ApplicationEntryPage())

    assert entry["text"] == "I'm interested"


def test_application_entry_ignores_accessibility_skip_link():
    class Page:
        def evaluate(self, _script):
            return [
                {
                    "text": "Skip to main content",
                    "id": "accessibilitySkipToMainContent",
                    "tag": "a",
                    "href": "https://example.com/apply",
                    "accessibilitySkip": True,
                },
                {
                    "text": "Apply Manually",
                    "id": "",
                    "tag": "a",
                    "href": "https://example.com/apply/applyManually",
                    "accessibilitySkip": False,
                },
            ]

    entry = python_runtime._find_application_entry(Page())

    assert entry["text"] == "Apply Manually"


def test_workday_apply_button_without_href_is_application_entry():
    entry = python_runtime._find_application_entry(_WorkdayApplyEntryPage())

    assert entry["text"] == "Apply"


def test_application_navigation_guard_installs_privacy_link_blocker():
    class Page:
        def __init__(self):
            self.calls = []

        def evaluate(self, script, arg=None):
            self.calls.append((str(script), arg))
            return True

    page = Page()

    assert python_runtime._install_application_navigation_guard(
        page,
        "https://job-boards.greenhouse.io/boxinc/jobs/7909448",
    )
    script, arg = page.calls[-1]
    assert "addEventListener" in script
    assert "privacy|notice|policy|terms|arbitration|personnel|candidate|pdf" in script
    assert "job-boards.greenhouse.io" in arg["hosts"]


def test_external_privacy_navigation_restores_original_application(monkeypatch):
    class Page:
        def __init__(self):
            self.url = "https://cloud.app.box.com/v/BoxPersonnelPrivacyNotice"
            self.went_back = False

        def go_back(self, **_kwargs):
            self.went_back = True
            self.url = "https://job-boards.greenhouse.io/boxinc/jobs/7909448"

        def wait_for_load_state(self, *_args, **_kwargs):
            return None

    page = Page()
    monkeypatch.setattr(python_runtime, "_open_application_form_if_needed", lambda _page: False)
    monkeypatch.setattr(
        python_runtime,
        "_wait_for_application_form_context",
        lambda candidate, attempts=5, delay_ms=1000: "greenhouse.io" in candidate.url,
    )

    assert python_runtime._restore_application_context_if_external(
        page,
        "https://job-boards.greenhouse.io/boxinc/jobs/7909448",
    )
    assert page.went_back is True
    assert page.url == "https://job-boards.greenhouse.io/boxinc/jobs/7909448"


def test_greenhouse_combobox_aliases_cover_box_options():
    assert python_runtime._selection_matches_answer("United States +1", "United States")
    assert python_runtime._selection_matches_answer("Man", "Male")
    assert python_runtime._selection_matches_answer("I Acknowledge", "Confirmed")
    assert python_runtime._selection_matches_answer("I Agree", "Yes")
    assert python_runtime._selection_matches_answer("Already graduated", "May 2026")


def test_box_greenhouse_remaining_required_comboboxes_plan_exact_options():
    profile = {
        "demographics": {"gender": "Male"},
        "sensitive_answers": {
            "privacy_consent": {"patterns": ["privacy notice"], "answer": "Yes", "approved": True},
            "terms_consent": {"patterns": ["consent"], "answer": "Yes", "approved": True},
        },
        "answers": {},
    }
    cases = {
        "Privacy Notice Acknowledgement*": "I Acknowledge",
        "Consent To Process*": "I Agree",
        "I identify my gender as:*": "Man",
        "I identify my sexual orientation as:*": "I don't wish to answer",
    }
    for label, expected in cases.items():
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
            "options": [],
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": expected,
        }


def test_field_scraper_excludes_controls_hidden_by_computed_visibility():
    source = inspect.getsource(python_runtime._scrape_fields)

    assert 'style.visibility === "hidden"' in source
    assert 'style.visibility === "collapse"' in source
    assert source.index('style.visibility === "hidden"') < source.index("if (node.offsetParent)")


def test_required_audit_treats_aria_selected_custom_choices_as_selected():
    source = inspect.getsource(python_runtime._audit_required_fields)

    assert 'candidate.getAttribute("aria-selected") === "true"' in source
    assert "choiceSelected(candidate)" in source
    assert "!choiceSelected(control)" in source


def test_application_iframe_discovery_accepts_non_greenhouse_application_frames():
    source = inspect.getsource(python_runtime._embedded_application_frame_url)

    assert "application|job_app|apply" in source
    assert "privacy|notice|policy|terms|candidate" in source


def test_required_audit_treats_same_name_checkbox_group_as_satisfied_when_any_checked():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <fieldset>
                <legend>Which AI concepts have you worked with? *</legend>
                <label><input type="checkbox" name="question_1[]" required checked> OpenAI</label>
                <label><input type="checkbox" name="question_1[]" required> None</label>
              </fieldset>
            </form>
            """
        )

        assert python_runtime._audit_required_fields(page) == []
        browser.close()


def test_scraper_preserves_question_context_for_single_yes_checkbox():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <div data-question="onsite-commitment">
                <div data-field-label>
                  I understand that this internship requires working on-site 5 days/week. *
                </div>
                <label><input type="checkbox" name="onsite" required value="Yes"> Yes</label>
              </div>
            </form>
            """
        )

        checkbox = next(
            field
            for field in python_runtime._scrape_fields(page)
            if field.get("type") == "checkbox"
        )

        assert checkbox["label"] == "Yes"
        assert "requires working on-site 5 days/week" in checkbox["section"]
        assert checkbox["required"] is True
        browser.close()


def test_ashby_education_date_selects_infer_subfield_from_entry_context():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <div data-field-path="_systemfield_education_history">
              <div class="ashby-application-form-field-entry">
                <div>Education History</div>
                <div>Start Date</div>
                <select placeholder="Month...">
                  <option value="">Month...</option>
                  <option value="September">September</option>
                </select>
                <select placeholder="Year...">
                  <option value="">Year...</option>
                  <option value="2024">2024</option>
                </select>
                <div>End Date</div>
                <select placeholder="Month...">
                  <option value="">Month...</option>
                  <option value="May">May</option>
                </select>
                <select placeholder="Year...">
                  <option value="">Year...</option>
                  <option value="2026">2026</option>
                </select>
                <label><input type="checkbox"> Still Student?</label>
              </div>
            </div>
            """
        )

        fields = python_runtime._scrape_fields(page)
        by_subfield = {
            field.get("ashbyEduSubfield"): field
            for field in fields
            if field.get("ashbyEduSubfield")
        }

        assert by_subfield["start_month"]["placeholder"] == "Month..."
        assert by_subfield["start_year"]["placeholder"] == "Year..."
        assert by_subfield["end_month"]["placeholder"] == "Month..."
        assert by_subfield["end_year"]["placeholder"] == "Year..."
        browser.close()


def test_ashby_education_dates_detect_placeholder_option_and_enrich_labels():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <div data-field-path="_systemfield_education_history">
              <label class="ashby-application-form-question-title" for="_systemfield_education_history">Education History</label>
              <label class="ashby-application-form-question-title" for="_systemfield_education_history-school">School</label>
              <input placeholder="Search schools..." role="combobox">
              <label class="ashby-application-form-question-title" for="_systemfield_education_history-degree">Degree</label>
              <input id="_systemfield_education_history-degree" placeholder="e.g. Bachelor of Science">
              <label class="ashby-application-form-question-title" for="_systemfield_education_history-major">Field of Study</label>
              <input id="_systemfield_education_history-major" placeholder="e.g. Computer Science">
              <label class="ashby-application-form-question-title" for="_systemfield_education_history-startDate">Start Date</label>
              <div id="_systemfield_education_history-startDate">
                <select><option disabled hidden value="">Month...</option><option value="9">September</option></select>
                <select><option disabled hidden value="">Year...</option><option value="2024">2024</option></select>
              </div>
              <label class="ashby-application-form-question-title" for="_systemfield_education_history-endDate">End Date</label>
              <div id="_systemfield_education_history-endDate">
                <select><option disabled hidden value="">Month...</option><option value="5">May</option></select>
                <select><option disabled hidden value="">Year...</option><option value="2026">2026</option></select>
              </div>
              <label><input type="checkbox"> Still Student?</label>
            </div>
            """
        )

        fields = python_runtime._scrape_fields(page)
        by_subfield = {
            field.get("ashbyEduSubfield"): field
            for field in fields
            if field.get("ashbyEduSubfield")
        }

        assert by_subfield["start_month"]["label"] == "Education History Start Date Month"
        assert by_subfield["start_year"]["label"] == "Education History Start Date Year"
        assert by_subfield["end_month"]["label"] == "Education History End Date Month"
        assert by_subfield["end_year"]["label"] == "Education History End Date Year"
        assert by_subfield["school"]["label"] == "Education History School"
        assert by_subfield["degree"]["label"] == "Education History Degree"
        assert by_subfield["field"]["label"] == "Education History Field of Study"
        assert by_subfield["still_student"]["label"] == "Still Student?"

        profile = {
            "education": [
                {
                    "school": "Stevens Institute of Technology",
                    "degree": "Master's",
                    "field": "Computer Science",
                    "start_date": "2024-09",
                    "end_date": "2026-05",
                }
            ]
        }
        assert python_runtime._plan_field(by_subfield["start_month"], profile, None) == {
            "action": "select",
            "value": "September",
        }
        assert python_runtime._plan_field(by_subfield["start_year"], profile, None) == {
            "action": "select",
            "value": "2024",
        }
        assert python_runtime._plan_field(by_subfield["end_month"], profile, None) == {
            "action": "select",
            "value": "May",
        }
        assert python_runtime._plan_field(by_subfield["end_year"], profile, None) == {
            "action": "select",
            "value": "2026",
        }
        browser.close()


def test_dynamic_combobox_generates_from_live_options_and_commits_with_readback(
    monkeypatch,
):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    class Resolver:
        def answer_for_field(self, field, profile, *, label=None):
            assert field["options"] == ["Product Design", "ML Infrastructure"]
            return "ML Infrastructure"

    monkeypatch.setattr(
        python_runtime,
        "get_llm_answer_resolver",
        lambda: Resolver(),
    )
    profile = {"skills": ["PyTorch", "Kubernetes"]}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <span id="team-question">Which team best matches your background? *</span>
              <button id="team" type="button" role="combobox"
                aria-labelledby="team-question" aria-controls="team-options"
                aria-expanded="false"
                onclick="document.getElementById('team-options').style.display='block'; this.setAttribute('aria-expanded','true')">
                Select one
              </button>
              <div id="team-options" role="listbox" style="display:none">
                <button type="button" role="option"
                  onclick="document.getElementById('team').textContent='Product Design'; document.getElementById('team').setAttribute('aria-expanded','false'); this.parentElement.style.display='none'">
                  Product Design
                </button>
                <button type="button" role="option"
                  onclick="document.getElementById('team').textContent='ML Infrastructure'; document.getElementById('team').setAttribute('aria-expanded','false'); this.parentElement.style.display='none'">
                  ML Infrastructure
                </button>
              </div>
            </form>
            """
        )
        field = next(
            field
            for field in python_runtime._scrape_fields(page)
            if field.get("id") == "team"
        )
        plan = python_runtime._plan_field(field, profile, None)

        assert plan["defer_live_options"] is True
        readback = python_runtime._apply_fill(page, field, plan, profile)
        assert readback == "ML Infrastructure"
        assert page.locator("#team").inner_text().strip() == "ML Infrastructure"
        browser.close()


def test_apply_fill_ashby_button_group_clicks_visible_button_without_hidden_fallback():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <div class="ashby-application-form-field-entry">
              <label class="ashby-application-form-question-title">
                Will you require sponsorship to work in the United States now or in the future?
              </label>
              <div>
                <button class="_container_pjyt6_1 _option_1svni_32"
                  onclick="document.body.dataset.buttonClicks = String(Number(document.body.dataset.buttonClicks || '0') + 1); this.className = this.className.includes('_active_') ? this.className : this.className + ' _active_1svni_57'; this.parentElement.querySelector('input').checked = true;">
                  Yes
                </button>
                <button class="_container_pjyt6_1 _option_1svni_32">No</button>
                <input type="checkbox" class="_input_1svni_78"
                  onclick="document.body.dataset.hiddenClicks = String(Number(document.body.dataset.hiddenClicks || '0') + 1)">
              </div>
            </div>
            """
        )
        field = {
            "kind": "buttongroup",
            "type": "button",
            "label": "Will you require sponsorship to work in the United States now or in the future?",
            "options": [{"label": "Yes", "value": "Yes", "autofillId": "unused"}],
        }
        plan = {"action": "buttonclick", "option": field["options"][0]}

        readback = python_runtime._apply_fill(page, field, plan)
        second_readback = python_runtime._apply_fill(page, field, plan)

        assert readback == "selected: Yes"
        assert second_readback == "selected: Yes"
        assert page.locator("input").is_checked()
        assert "_active_" in page.locator("button").first.get_attribute("class")
        assert page.evaluate("document.body.dataset.buttonClicks || '0'") == "1"
        assert page.evaluate("document.body.dataset.hiddenClicks || '0'") == "0"
        browser.close()


def test_ashby_education_end_date_selects_are_enabled_by_still_student_uncheck():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <div data-field-path="_systemfield_education_history" id="education-history">
              <section class="entry">
                <button type="button">Delete</button>
                <input type="text" id="school-0" value="Stevens Institute of Technology">
                <select id="end-month-0" data-job-agent-autofill-index="end-month-0" disabled>
                  <option value="">Month...</option>
                  <option value="May">May</option>
                  <option value="June">June</option>
                </select>
                <select id="end-year-0" data-job-agent-autofill-index="end-year-0" disabled>
                  <option value="">Year...</option>
                  <option value="2026">2026</option>
                </select>
                <label><input type="checkbox" id="still-student-0" data-job-agent-autofill-index="still-student-0" checked onchange="document.getElementById('end-month-0').disabled = this.checked; document.getElementById('end-year-0').disabled = this.checked;"> Still Student?</label>
              </section>
              <section class="entry">
                <button type="button">Delete</button>
                <input type="text" id="school-1" value="Shenzhen University">
                <select id="end-month-1" data-job-agent-autofill-index="end-month-1" disabled>
                  <option value="">Month...</option>
                  <option value="July">July</option>
                </select>
                <select id="end-year-1" data-job-agent-autofill-index="end-year-1" disabled>
                  <option value="">Year...</option>
                  <option value="2023">2023</option>
                </select>
                <label><input type="checkbox" id="still-student-1" data-job-agent-autofill-index="still-student-1" checked onchange="document.getElementById('end-month-1').disabled = this.checked; document.getElementById('end-year-1').disabled = this.checked;"> Still Student?</label>
              </section>
            </div>
            """
        )
        profile = {
            "education": [
                {
                    "school": "Stevens Institute of Technology",
                    "degree": "Master's",
                    "field": "Computer Science",
                    "start_date": "2024-09",
                    "end_date": "2026-05",
                },
                {
                    "school": "Shenzhen University",
                    "degree": "Bachelor's",
                    "field": "Logistics Management",
                    "start_date": "2019-09",
                    "end_date": "2023-07",
                },
            ]
        }

        checkboxes = [
            {
                "kind": "single",
                "tag": "input",
                "type": "checkbox",
                "label": "Still Student?",
                "id": "still-student-0",
                "autofillId": "still-student-0",
                "section": "education",
                "ashbyEduEntryIndex": 0,
                "ashbyEduSubfield": "still_student",
            },
            {
                "kind": "single",
                "tag": "input",
                "type": "checkbox",
                "label": "Still Student?",
                "id": "still-student-1",
                "autofillId": "still-student-1",
                "section": "education",
                "ashbyEduEntryIndex": 1,
                "ashbyEduSubfield": "still_student",
            },
        ]
        for checkbox in checkboxes:
            plan = python_runtime._plan_field(checkbox, profile, None)
            assert plan == {"action": "uncheck"}
            assert python_runtime._apply_fill(page, checkbox, plan) == "unchecked"

        assert page.locator("#still-student-0").is_checked() is False
        assert page.locator("#still-student-1").is_checked() is False
        assert page.locator("#end-month-0").is_enabled() is True
        assert page.locator("#end-year-0").is_enabled() is True
        assert page.locator("#end-month-1").is_enabled() is True
        assert page.locator("#end-year-1").is_enabled() is True

        end_month = {
            "kind": "single",
            "tag": "select",
            "type": "select",
            "label": "End Date Month",
            "id": "end-month-0",
            "autofillId": "end-month-0",
            "section": "education",
            "ashbyEduEntryIndex": 0,
            "ashbyEduSubfield": "end_month",
            "required": True,
            "options": ["May", "June"],
        }
        end_year = {
            "kind": "single",
            "tag": "select",
            "type": "select",
            "label": "End Date Year",
            "id": "end-year-0",
            "autofillId": "end-year-0",
            "section": "education",
            "ashbyEduEntryIndex": 0,
            "ashbyEduSubfield": "end_year",
            "required": True,
            "options": ["2026"],
        }
        assert python_runtime._plan_field(end_month, profile, None) == {
            "action": "select",
            "value": "May",
        }
        assert python_runtime._plan_field(end_year, profile, None) == {
            "action": "select",
            "value": "2026",
        }
        assert python_runtime._apply_fill(page, end_month, {"action": "select", "value": "May"}) == "May"
        assert python_runtime._apply_fill(page, end_year, {"action": "select", "value": "2026"}) == "2026"
        assert page.locator("#end-month-0").input_value() == "May"
        assert page.locator("#end-year-0").input_value() == "2026"
        browser.close()


def test_application_form_context_rejects_a_generic_newsletter_email(monkeypatch):
    class Page:
        url = "https://www.example.com/careers"

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [{"label": "Enter email*", "type": "email"}],
    )

    assert python_runtime._has_application_form_context(Page()) is False


def test_application_form_context_accepts_an_official_ats_page_with_initial_email_field(monkeypatch):
    class Page:
        url = "https://job-boards.greenhouse.io/acme/jobs/123"

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [{"label": "Email", "type": "email"}],
    )

    assert python_runtime._has_application_form_context(Page()) is True


def test_application_form_context_rejects_greenhouse_job_board_filters(monkeypatch):
    class Page:
        url = "https://job-boards.greenhouse.io/embed/job_board?for=nuro"

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [{"label": "Search", "type": "text"}, {"label": "Department", "role": "combobox"}],
    )

    assert python_runtime._has_application_form_context(Page()) is False


def test_application_form_context_rejects_workday_sign_in_gate_with_apply_manually(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {"label": "Email Address*", "type": "text"},
            {"label": "Password*", "type": "password"},
            {
                "label": "Enter website. This input is for robots only, do not enter if you're human.",
                "type": "text",
            },
        ],
    )

    assert python_runtime._has_application_form_context(_WorkdayApplyManuallyGatePage()) is False


def test_application_form_context_rejects_signed_in_workday_job_page_with_settings_field(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [{"label": "Settings", "type": "text"}],
    )

    assert python_runtime._has_application_form_context(_WorkdayApplyManuallyGatePage()) is False


def test_application_form_context_rejects_workday_loading_shell_with_only_settings(monkeypatch):
    class Page:
        url = "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually"

    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [{"label": "Settings", "type": "text"}],
    )

    assert python_runtime._has_application_form_context(Page()) is False


def test_application_form_context_rejects_workday_utility_menu_button_field(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {
                "kind": "single",
                "tag": "button",
                "type": "button",
                "label": "Settings",
                "id": "settingsSelectorButton",
                "automationId": "utilityMenuButton",
                "role": "combobox",
                "required": False,
            }
        ],
    )

    assert python_runtime._has_application_form_context(_WorkdayApplyManuallyGatePage()) is False


def test_open_application_form_prefers_apply_manually_over_workday_sign_in(monkeypatch):
    page = _WorkdayApplyManuallyGatePage()

    def fake_scrape(_page):
        if not page.opened:
            return [
                {"label": "Email Address*", "type": "text"},
                {"label": "Password*", "type": "password"},
            ]
        return [{"label": "First Name*", "type": "text"}]

    def fake_click(_page, _button):
        page.opened = True

    monkeypatch.setattr(python_runtime, "_scrape_fields", fake_scrape)
    monkeypatch.setattr(python_runtime, "_click_button", fake_click)

    assert python_runtime._open_application_form_if_needed(page) is True
    assert page.opened is True


def test_open_application_form_clicks_apply_manually_from_signed_in_workday_job_page(monkeypatch):
    page = _WorkdayApplyManuallyGatePage()

    def fake_scrape(_page):
        if not page.opened:
            return [{"label": "Settings", "type": "text"}]
        return [{"label": "First Name*", "type": "text"}]

    def fake_click(_page, _button):
        page.opened = True

    monkeypatch.setattr(python_runtime, "_scrape_fields", fake_scrape)
    monkeypatch.setattr(python_runtime, "_click_button", fake_click)

    assert python_runtime._open_application_form_if_needed(page) is True
    assert page.opened is True


def test_wait_for_application_form_context_retries_slow_workday_hydration(monkeypatch):
    page = _WaitingPage()
    checks = iter([False, False, True])
    opened = []
    dismissed = []

    monkeypatch.setattr(
        python_runtime,
        "_has_application_form_context",
        lambda _page: next(checks),
    )
    monkeypatch.setattr(
        python_runtime,
        "_open_application_form_if_needed",
        lambda _page: opened.append(True),
    )
    monkeypatch.setattr(
        python_runtime,
        "_dismiss_cookie_banner",
        lambda _page: dismissed.append(True),
    )

    assert python_runtime._wait_for_application_form_context(page, attempts=3, delay_ms=250) is True
    assert page.waits == [250, 250]
    assert len(opened) == 2
    assert len(dismissed) == 2


def test_dismiss_cookie_banner_targets_waymo_consent_modal():
    class _RecordingPage:
        def __init__(self):
            self.script = None

        def evaluate(self, script, arg=None):
            self.script = script
            return False

        def wait_for_timeout(self, _ms):
            pass

    page = _RecordingPage()
    python_runtime._dismiss_cookie_banner(page)
    assert page.script is not None
    assert '.consent-modal' in page.script
    assert '[aria-label="Cookie consent"]' in page.script
    assert 'i do not accept' in page.script.lower()
    assert 'i accept' in page.script.lower()
    assert 'save and continue' in page.script.lower()


def test_ensure_application_fields_ready_waits_past_nonmeaningful_workday_shell(monkeypatch):
    page = _WaitingPage()
    calls = []
    snapshots = [
        [{"label": "Settings", "type": "text", "id": "settingsSelectorButton"}],
        [{"label": "Settings", "type": "text", "id": "settingsSelectorButton"}],
        [{"label": "First Name*", "type": "text", "id": "firstName"}],
    ]

    def fake_scrape(_page):
        calls.append(True)
        return snapshots[min(len(calls) - 1, len(snapshots) - 1)]

    monkeypatch.setattr(python_runtime, "_scrape_fields", fake_scrape)
    monkeypatch.setattr(python_runtime, "_wait_for_application_form_context", lambda *_args, **_kwargs: False)

    assert python_runtime._ensure_application_fields_ready(page, attempts=3, delay_ms=250) == [
        {"label": "First Name*", "type": "text", "id": "firstName"}
    ]
    assert page.waits == [250, 250]


def test_workday_sign_in_failure_reason_detects_rejected_credentials(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {"label": "Email Address*", "type": "text"},
            {"label": "Password*", "type": "password"},
        ],
    )
    monkeypatch.setattr(
        python_runtime,
        "_current_page_text",
        lambda _page: "Sign In You may have entered the wrong email address or password or your account might be locked.",
    )

    assert (
        python_runtime._workday_sign_in_failure_reason(_WorkdayRejectedSignInPage())
        == "candidate account sign-in rejected by Workday: wrong email address or password"
    )


def test_workday_sign_in_failure_reason_can_skip_generic_unattempted_login(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {"label": "Email Address*", "type": "text"},
            {"label": "Password*", "type": "password"},
        ],
    )
    monkeypatch.setattr(
        python_runtime,
        "_current_page_text",
        lambda _page: "Sign In Email Address Password",
    )

    assert (
        python_runtime._workday_sign_in_failure_reason(
            _WorkdayRejectedSignInPage(),
            allow_generic=False,
        )
        is None
    )
    assert (
        python_runtime._workday_sign_in_failure_reason(_WorkdayRejectedSignInPage())
        == "candidate account sign-in rejected by Workday"
    )


def test_workday_candidate_home_no_applications_restores_original_apply_url(monkeypatch):
    page = _WorkdayCandidateHomeNoApplicationsPage()
    opened = []

    monkeypatch.setattr(
        python_runtime,
        "_open_application_form_if_needed",
        lambda _page: opened.append(True) or True,
    )

    assert python_runtime._restore_workday_application_from_candidate_home(
        page,
        "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply",
    )
    assert page.gotos == ["https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply"]
    assert opened == [True]


def test_workday_account_verification_reason_takes_precedence_over_sign_in_failure(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {"label": "Email Address*", "type": "text"},
            {"label": "Password*", "type": "password"},
        ],
    )
    monkeypatch.setattr(
        python_runtime,
        "_current_page_text",
        lambda _page: "Sign In Verify your account before you sign in or request a verification email. Resend Account Verification",
    )

    assert (
        python_runtime._workday_account_verification_reason(_WorkdayRejectedSignInPage())
        == "candidate account verification required by Workday"
    )
    assert (
        python_runtime._workday_sign_in_failure_reason(
            _WorkdayRejectedSignInPage(),
            allow_generic=False,
        )
        is None
    )


def test_workday_candidate_account_verification_opens_gmail_activation_link(monkeypatch):
    page = _WorkdayAccountVerificationPage()
    link = "https://company.wd5.myworkdayjobs.com/External_Careers/activate/secret-token"
    captured = {}

    monkeypatch.setattr(
        python_runtime,
        "_current_page_text",
        lambda _page: "Sign In Verify your account before you sign in. Resend Account Verification",
    )

    def fake_email_verification_link(**kwargs):
        captured.update(kwargs)
        return link

    monkeypatch.setattr(python_runtime, "_email_verification_link", fake_email_verification_link)

    assert python_runtime._verify_workday_candidate_account_if_configured(
        page,
        requested_after_ns=123_456,
        payload=None,
    )
    assert page.clicked_texts == ["Resend Account Verification"]
    assert page.gotos == [link]
    assert captured["requested_after_ns"] == 123_456
    assert "Workday" in captured["query"]
    assert captured["url_pattern"] == r"workday|myworkdayjobs"


def test_workday_account_verification_evidence_redacts_activation_link_and_email():
    assert (
        python_runtime._safe_evidence_url(
            "https://resmed.wd3.myworkdayjobs.com/ResMed_External_Careers/activate/secret-token?x=1"
        )
        == "https://resmed.wd3.myworkdayjobs.com/<workday-activation-link-redacted>"
    )

    redacted = python_runtime._redact_evidence_text(
        "Click https://resmed.wd3.myworkdayjobs.com/ResMed_External_Careers/activate/secret-token "
        "for criswu20010728+resmedjr052316v5@gmail.com"
    )

    assert "secret-token" not in redacted
    assert "criswu20010728" not in redacted
    assert "<workday-activation-link-redacted>" in redacted
    assert "<email-redacted>" in redacted


def test_workday_sign_in_fill_signature_detects_only_email_password_fields():
    page = _WorkdayRejectedSignInPage()

    assert (
        python_runtime._workday_sign_in_fill_signature(
            page,
            [
                {"label": "Email Address*", "action": "fill"},
                {"label": "Password*", "action": "fill"},
                {"label": "Email Address*", "action": "fill"},
                {"label": "Password*", "action": "fill"},
            ],
        )
        == "email|password"
    )
    assert (
        python_runtime._workday_sign_in_fill_signature(
            page,
            [
                {"label": "Email Address*", "action": "fill"},
                {"label": "Password*", "action": "fill"},
                {"label": "First Name*", "action": "fill"},
            ],
        )
        == ""
    )
    assert (
        python_runtime._workday_sign_in_fill_signature(
            page,
            [
                {"label": "Email Address*", "action": "fill"},
                {"label": "Password*", "action": "fill"},
                {"label": "Verify New Password*", "action": "fill"},
            ],
        )
        == ""
    )


def test_workday_create_account_failure_reason_detects_missing_consent_checkbox(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {"label": "Email Address*", "type": "text"},
            {"label": "Password*", "type": "password"},
            {"label": "Verify New Password*", "type": "password"},
        ],
    )
    monkeypatch.setattr(
        python_runtime,
        "_current_page_text",
        lambda _page: "Create Account Agree Error: Please check the box to continue",
    )

    assert (
        python_runtime._workday_create_account_failure_reason(_WorkdayRejectedSignInPage())
        == "candidate account creation blocked by required privacy consent checkbox"
    )


def test_page_did_not_advance_when_step_is_missing_but_fields_repeat():
    assert python_runtime._page_did_not_advance("", "", ("email", "password"), ("email", "password")) is True
    assert python_runtime._page_did_not_advance("", "", ("email",), ("first_name",)) is False


def test_job_page_apply_button_is_not_treated_as_final_submit():
    assert python_runtime._is_job_page_apply_button(
        _NoFormFieldsPage(),
        {"text": "Apply"},
    ) is True
    assert python_runtime._is_job_page_apply_button(
        _NoFormFieldsPage(),
        {"text": "Apply Manually"},
    ) is True
    assert python_runtime._is_job_page_apply_button(
        _NoFormFieldsPage(),
        {"text": "Apply Now"},
    ) is True


def test_job_page_apply_button_ignores_settings_field(monkeypatch):
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [{"label": "Settings", "type": "text"}],
    )

    assert python_runtime._is_job_page_apply_button(
        _WorkdayApplyManuallyGatePage(),
        {"text": "Apply Manually"},
    ) is True


def test_job_match_resume_upload_is_not_meaningful_application_field():
    fields = [
        {"label": "Find out how well you match with this job Upload your resume", "type": "file"},
        {"label": "Upload your resume", "type": "file"},
    ]

    assert python_runtime._meaningful_application_fields(fields) == []


def test_unlabeled_resume_probe_does_not_make_job_page_apply_a_form():
    fields = [{"label": "", "type": "file", "name": "", "id": "", "placeholder": ""}]

    assert python_runtime._is_job_page_apply_button(
        _NoFormFieldsPage(),
        {"text": "Apply Now", "tag": "button", "inForm": False},
        fields,
    ) is True


def test_find_submit_button_ignores_apply_manually_entry_link():
    class Page:
        def evaluate(self, script, arg=None):
            return [
                {
                    "text": "Apply Manually",
                    "id": "",
                    "className": "",
                    "title": "",
                    "ariaLabel": "",
                    "automationId": "",
                    "tag": "a",
                    "type": "",
                    "href": "https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually",
                    "inForm": False,
                    "inDatepicker": False,
                    "y": 400,
                    "autofillId": "0",
                }
            ]

    assert python_runtime._find_button(Page(), "submit") is None


def test_workday_create_account_form_switches_to_existing_account_sign_in():
    page = _WorkdayCreateAccountPage()

    assert python_runtime._switch_to_candidate_sign_in_if_needed(page) is True
    assert page.clicked == [('[data-automation-id="signInLink"]', True)]
    assert page.waited == [500]


def test_workday_email_sign_in_gate_clicks_email_entry_not_social_buttons():
    page = _WorkdayEmailSignInGatePage()

    assert python_runtime._open_workday_email_sign_in_if_needed(page) is True
    assert page.clicked == [('[data-job-agent-button-index="1"]', False)]
    assert page.waited[-1] == 1500


def test_workday_rejected_sign_in_can_switch_to_create_account(monkeypatch):
    page = _WorkdayRejectedSignInWithCreateAccountPage()
    clicked = []
    monkeypatch.setattr(
        python_runtime,
        "_scrape_fields",
        lambda _page: [
            {"label": "Email Address*", "type": "text"},
            {"label": "Password*", "type": "password"},
        ],
    )
    monkeypatch.setattr(
        python_runtime,
        "_current_page_text",
        lambda _page: "Sign In You may have entered the wrong email address or password or your account might be locked.",
    )
    monkeypatch.setattr(
        python_runtime,
        "_click_button",
        lambda _page, entry: clicked.append((entry["text"], entry["autofillId"])),
    )

    assert python_runtime._open_workday_create_account_from_sign_in_if_available(page) is True
    assert clicked == [("Create Account", "1")]
    assert page.waited[-1] == 1500


def test_workday_form_sign_in_button_is_preferred_over_header_sign_in():
    button = python_runtime._find_button(_WorkdaySignInButtonsPage(), "next")

    assert button["automationId"] == "signInSubmitButton"
    assert button["autofillId"] == "1"


def test_workday_save_and_continue_button_advances_application():
    button = python_runtime._find_button(_WorkdaySaveAndContinuePage(), "next")

    assert button["text"] == "Save and Continue"


def test_detect_submission_processing_error_handles_datadome_captcha():
    assert python_runtime._detect_submission_processing_error(_DataDomeCaptchaPage()).startswith(
        "captcha present at https://jobs.smartrecruiters.com/oneclick-ui/company/acme/publication/123"
    )


def test_detect_submission_processing_error_handles_cloudflare_turnstile():
    assert python_runtime._detect_submission_processing_error(_CloudflareTurnstilePage()).startswith(
        "matched 'verify you are human' at https://apply.workable.com/acme/j/123/apply/"
    )


def test_recaptcha_resubmit_message_is_not_email_verification():
    page = _RecaptchaResubmitPage()

    assert python_runtime._detect_email_verification_request(page) is None
    assert python_runtime._is_retryable_captcha_error(
        python_runtime._detect_submission_processing_error(page)
    ) is True


def test_submission_processing_error_redacts_greenhouse_embed_tokens():
    error = python_runtime._detect_submission_processing_error(_GreenhouseEmbedTokenPage())

    assert error == (
        "matched 'please complete the recaptcha' at "
        "https://job-boards.greenhouse.io/embed/job_app with recaptcha present"
    )
    assert "validityToken" not in error
    assert "secret" not in error


def test_possible_spam_with_captcha_is_immediately_terminal():
    assert python_runtime._is_retryable_captcha_error(
        "matched 'flagged as possible spam' with recaptcha present"
    ) is False


def test_possible_spam_without_captcha_is_not_retried_through_solver():
    assert python_runtime._is_retryable_captcha_error(
        "matched 'flagged as possible spam' at current page"
    ) is False


@pytest.mark.parametrize(
    "error",
    [
        "matched 'too many requests' with recaptcha present",
        "matched 'rate limit' with captcha present",
        "matched 'http 429' with recaptcha present",
    ],
)
def test_server_rate_limit_with_captcha_is_immediately_terminal(error):
    assert python_runtime._is_retryable_captcha_error(error) is False


def test_captcha_recovery_failure_does_not_create_anti_spam_marker():
    error = python_runtime._captcha_recovery_failure(
        {"status": "unsupported", "detail": "challenge type"}
    )

    assert error == "captcha recovery failed: unsupported (challenge type)"
    assert "spam" not in error


def test_wait_for_submit_settle_ignores_ambient_captcha_until_confirmation(monkeypatch):
    class Page:
        waits = 0

        def wait_for_timeout(self, _milliseconds):
            self.waits += 1

    page = Page()
    monkeypatch.setattr(python_runtime, "_current_page_text", lambda _page: "application")
    monkeypatch.setattr(
        python_runtime,
        "_detect_submission_confirmation",
        lambda _page: "confirmed" if page.waits >= 2 else None,
    )
    monkeypatch.setattr(python_runtime, "_detect_email_verification_request", lambda _page: None)
    monkeypatch.setattr(
        python_runtime,
        "_detect_submission_processing_error",
        lambda _page: "captcha present at https://jobs.ashbyhq.com/example/application",
    )

    python_runtime._wait_for_submit_settle(page, timeout_ms=3000)

    assert page.waits == 2


def test_capmonster_task_for_extended_captcha_types(monkeypatch):
    monkeypatch.setenv("CAPMONSTER_RECAPTCHA_MIN_SCORE", "0.7")

    assert python_runtime._capmonster_task_for(
        {
            "kind": "recaptchaV2",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
            "invisible": True,
            "userAgent": "Mozilla/5.0",
            "cookies": "session=abc",
            "recaptchaDataSValue": "data-s-token",
        }
    ) == {
        "type": "RecaptchaV2Task",
        "websiteURL": "https://jobs.example.com/apply",
        "websiteKey": "site-key",
        "isInvisible": True,
        "userAgent": "Mozilla/5.0",
        "cookies": "session=abc",
        "recaptchaDataSValue": "data-s-token",
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "turnstile",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "turnstile-key",
            "userAgent": "Mozilla/5.0",
            "pageAction": "apply",
            "data": "cdata-token",
        }
    ) == {
        "type": "TurnstileTask",
        "websiteURL": "https://jobs.example.com/apply",
        "websiteKey": "turnstile-key",
        "userAgent": "Mozilla/5.0",
        "pageAction": "apply",
        "data": "cdata-token",
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "turnstile",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "turnstile-key",
            "userAgent": "Mozilla/5.0",
            "pageAction": "managed",
            "data": "cdata-token",
            "cloudflareTaskType": "cf_clearance",
            "pageData": "chl-page-data",
            "htmlPageBase64": "PGh0bWw+",
            "apiJsUrl": "https://challenges.cloudflare.com/turnstile/v0/api.js",
        }
    ) == {
        "type": "TurnstileTask",
        "websiteURL": "https://jobs.example.com/apply",
        "websiteKey": "turnstile-key",
        "userAgent": "Mozilla/5.0",
        "pageAction": "managed",
        "data": "cdata-token",
        "cloudflareTaskType": "cf_clearance",
        "pageData": "chl-page-data",
        "htmlPageBase64": "PGh0bWw+",
        "apiJsUrl": "https://challenges.cloudflare.com/turnstile/v0/api.js",
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "hcaptcha",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "hcaptcha-key",
        }
    ) == {
        "type": "HCaptchaTaskProxyless",
        "websiteURL": "https://jobs.example.com/apply",
        "websiteKey": "hcaptcha-key",
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "recaptchaV3Enterprise",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
            "pageAction": "apply",
        }
    ) == {
        "type": "RecaptchaV3EnterpriseTask",
        "websiteURL": "https://jobs.example.com/apply",
        "websiteKey": "site-key",
        "pageAction": "apply",
        "minScore": 0.7,
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "recaptchaV3",
            "websiteURL": "https://jobs.ashbyhq.com/acme/application",
            "websiteKey": "ashby-key",
            "pageAction": "job_apply",
            "minScore": 0.7,
        }
    ) == {
        "type": "RecaptchaV3TaskProxyless",
        "websiteURL": "https://jobs.ashbyhq.com/acme/application",
        "websiteKey": "ashby-key",
        "pageAction": "job_apply",
        "minScore": 0.7,
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "recaptchaV3Enterprise",
            "websiteURL": "https://job-boards.greenhouse.io/waymark/jobs/4711827005",
            "websiteKey": "greenhouse-enterprise-key",
            "pageAction": "apply_to_job",
        }
    ) == {
        "type": "RecaptchaV3EnterpriseTask",
        "websiteURL": "https://job-boards.greenhouse.io/waymark/jobs/4711827005",
        "websiteKey": "greenhouse-enterprise-key",
        "pageAction": "apply_to_job",
        "minScore": 0.7,
    }
    assert python_runtime._capmonster_task_for(
        {
            "kind": "funcaptcha",
            "websiteURL": "https://jobs.example.com/apply",
            "websitePublicKey": "public-key",
            "funcaptchaApiJSSubdomain": "client-api.arkoselabs.com",
            "data": '{"blob":"abc"}',
        }
    ) == {
        "type": "FunCaptchaTask",
        "websiteURL": "https://jobs.example.com/apply",
        "websitePublicKey": "public-key",
        "funcaptchaApiJSSubdomain": "client-api.arkoselabs.com",
        "data": '{"blob":"abc"}',
    }


def test_recaptcha_v3_enterprise_solution_intercepts_execute_call():
    class CapturePage:
        def __init__(self):
            self.script = ""
            self.arg = None

        def evaluate(self, script, arg):
            self.script = str(script)
            self.arg = arg
            return True

    page = CapturePage()
    challenge = {
        "kind": "recaptchaV3Enterprise",
        "websiteURL": "https://job-boards.greenhouse.io/waymark/jobs/4711827005",
        "websiteKey": "site-key",
        "pageAction": "apply_to_job",
    }

    assert python_runtime._inject_captcha_solution(
        page,
        challenge,
        {"gRecaptchaResponse": "solved-token"},
    ) is True
    assert "window.grecaptcha.enterprise" in page.script
    assert 'patchProperty(target, "execute", solved)' in page.script
    assert 'patchProperty(target, "getResponse", response)' in page.script
    assert 'patchProperty(target, "reset", reset)' in page.script
    assert "__jobAgentRecaptchaGuardInterval" in page.script
    assert "setInterval(installGuard, 50)" in page.script
    assert page.arg == {"challenge": challenge, "token": "solved-token"}


def test_cloudflare_clearance_solution_sets_cookie_and_reloads():
    class CaptureContext:
        def __init__(self):
            self.cookies = []

        def add_cookies(self, cookies):
            self.cookies.extend(cookies)

    class CapturePage:
        def __init__(self):
            self.context = CaptureContext()
            self.reloaded = False
            self.waited = False

        def reload(self, wait_until=None, timeout=None):
            self.reloaded = True
            self.wait_until = wait_until
            self.timeout = timeout

        def wait_for_timeout(self, timeout):
            self.waited = timeout

    page = CapturePage()

    assert python_runtime._inject_captcha_solution(
        page,
        {
            "kind": "turnstile",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
            "cloudflareTaskType": "cf_clearance",
        },
        {"cf_clearance": "clearance-cookie"},
    ) is True
    assert page.context.cookies == [
        {
            "name": "cf_clearance",
            "value": "clearance-cookie",
            "url": "https://jobs.example.com",
            "path": "/",
        }
    ]
    assert page.reloaded is True
    assert page.wait_until == "domcontentloaded"
    assert page.timeout == 60000
    assert page.waited == 2000


def test_browser_init_script_captures_turnstile_render_parameters():
    class CaptureContext:
        def __init__(self):
            self.scripts = []

        def add_init_script(self, script):
            self.scripts.append(script)

    context = CaptureContext()

    python_runtime._install_browser_fingerprint_mitigation(context)

    script = "\n".join(context.scripts)
    assert "__jobAgentTurnstileCapture" in script
    assert "api.render = function(container, params)" in script
    assert "options.chlPageData" in script
    assert "options.cData" in script


def test_wait_for_captcha_api_ready_waits_for_greenhouse_enterprise_execute():
    class CapturePage:
        def __init__(self):
            self.script = ""
            self.arg = None
            self.timeout = None

        def wait_for_function(self, script, arg=None, timeout=None):
            self.script = str(script)
            self.arg = arg
            self.timeout = timeout
            return object()

    page = CapturePage()

    assert python_runtime._wait_for_captcha_api_ready(
        page,
        {"kind": "recaptchaV3Enterprise"},
    ) is True
    assert "window.grecaptcha.enterprise.execute" in page.script
    assert page.arg == "recaptchaV3Enterprise"
    assert page.timeout == 12000


def test_solve_captcha_waits_for_recaptcha_api_before_capmonster(monkeypatch):
    events = []

    class CapturePage:
        pass

    class FakeClient:
        def __init__(self, api_key):
            events.append(("client", api_key))

        def solve_task(self, task, timeout_seconds, poll_interval_seconds):
            events.append(("solve", task["type"]))
            return {"gRecaptchaResponse": "solved-token"}

    page = CapturePage()
    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.setattr(
        python_runtime,
        "_discover_captcha",
        lambda _page: {
            "kind": "recaptchaV3Enterprise",
            "websiteURL": "https://job-boards.greenhouse.io/acme/jobs/1",
            "websiteKey": "site-key",
            "pageAction": "apply_to_job",
        },
    )
    monkeypatch.setattr(
        python_runtime,
        "_wait_for_captcha_api_ready",
        lambda _page, _challenge: events.append(("wait", _challenge["kind"])) or True,
    )
    monkeypatch.setattr(python_runtime, "CapMonsterClient", FakeClient)
    monkeypatch.setattr(
        python_runtime,
        "_inject_captcha_solution",
        lambda _page, _challenge, _solution: events.append(("inject", _solution["gRecaptchaResponse"])) or True,
    )

    assert python_runtime._solve_captcha_if_configured(page) == {
        "status": "solved",
        "detail": "recaptchaV3Enterprise at https://job-boards.greenhouse.io/acme/jobs/1",
    }
    assert events == [
        ("wait", "recaptchaV3Enterprise"),
        ("client", "cap-key"),
        ("solve", "RecaptchaV3EnterpriseTask"),
        ("inject", "solved-token"),
    ]


def test_captcha_solution_detail_redacts_greenhouse_embed_tokens():
    detail = python_runtime._captcha_solution_detail(
        {
            "kind": "recaptchaV3Enterprise",
            "websiteURL": "https://job-boards.greenhouse.io/embed/job_app",
        }
    )

    assert detail == "recaptchaV3Enterprise at https://job-boards.greenhouse.io/embed/job_app"
    assert "validityToken" not in detail
    assert "secret" not in detail



def test_restore_native_recaptcha_clears_patched_api():
    class CapturePage:
        def __init__(self):
            self.eval_scripts = []

        def evaluate(self, script):
            self.eval_scripts.append(str(script))
            return True

    page = CapturePage()
    assert python_runtime._restore_native_recaptcha(page, {"kind": "recaptchaV3Enterprise"}) is True
    assert len(page.eval_scripts) == 1
    assert "__jobAgentRestoreRecaptchaApi" in page.eval_scripts[0]


def test_restore_native_recaptcha_skips_non_recaptcha():
    class CapturePage:
        def __init__(self):
            self.eval_scripts = []

        def evaluate(self, script):
            self.eval_scripts.append(str(script))
            return True

    page = CapturePage()
    assert python_runtime._restore_native_recaptcha(page, {"kind": "hcaptcha"}) is False
    assert len(page.eval_scripts) == 0


def test_restore_native_recaptcha_passes_sitekey_and_action():
    class CapturePage:
        def __init__(self):
            self.eval_scripts = []

        def evaluate(self, script):
            self.eval_scripts.append(str(script))
            return True

    page = CapturePage()
    assert python_runtime._restore_native_recaptcha(page, {"kind": "recaptchaV3"}) is True
    assert len(page.eval_scripts) == 1
    assert "__jobAgentRestoreRecaptchaApi" in page.eval_scripts[0]

def test_safe_evidence_url_drops_query_and_redacts_workday_activation():
    assert (
        python_runtime._safe_evidence_url(
            "https://job-boards.greenhouse.io/embed/job_app"
        )
        == "https://job-boards.greenhouse.io/embed/job_app"
    )
    assert (
        python_runtime._safe_evidence_url(
            "https://resmed.wd3.myworkdayjobs.com/ResMed_External_Careers/activate/secret-token?x=1"
        )
        == "https://resmed.wd3.myworkdayjobs.com/<workday-activation-link-redacted>"
    )


def test_parse_vision_clicks_accepts_multi_target_and_rejects_out_of_bounds():
    assert python_runtime._parse_vision_clicks(
        '{"clicks":[{"x":20,"y":30},{"x":999,"y":10},{"x":50,"y":60}]}',
        100,
        100,
    ) == [{"x": 20.0, "y": 30.0}, {"x": 50.0, "y": 60.0}]


def test_parse_complex_image_clicks_accepts_coordinates_and_grid_answers():
    assert python_runtime._parse_complex_image_clicks(
        {"coordinates": [{"x": 20, "y": 30}, [50, 60], {"x": 999, "y": 10}]},
        100,
        100,
    ) == [{"x": 20.0, "y": 30.0}, {"x": 50.0, "y": 60.0}]
    assert python_runtime._parse_complex_image_clicks(
        {"answers": [True, False, False, False], "metadata": {"Grid": "2x2"}},
        200,
        100,
    ) == [{"x": 50.0, "y": 25.0}]


def test_parse_vision_drag_accepts_source_and_target():
    assert python_runtime._parse_vision_drag(
        '{"source":{"x":24,"y":52},"target":[180,92],"confidence":0.8}',
        240,
        120,
    ) == {
        "source": {"x": 24.0, "y": 52.0},
        "target": {"x": 180.0, "y": 92.0},
    }


def test_parse_vision_drag_candidates_accepts_ranked_targets_without_source():
    assert python_runtime._parse_vision_drag_candidates(
        '{"targets":[{"x":180,"y":92},{"x":210,"y":80}]}',
        240,
        120,
        require_source=False,
    ) == {
        "source": None,
        "targets": [{"x": 180.0, "y": 92.0}, {"x": 210.0, "y": 80.0}],
    }


def test_parse_vision_drag_rejects_out_of_bounds():
    with pytest.raises(ValueError, match="out of bounds"):
        python_runtime._parse_vision_drag(
            '{"source":{"x":24,"y":52},"target":{"x":280,"y":92}}',
            240,
            120,
        )


def test_together_modal_mercor_live_screening_fields_are_auto_answered():
    profile = {
        "location": "Jersey City, NJ, USA",
        "graduation_date": "May 2026",
        "target_levels": ["Entry Level & New Grad"],
        "skills": ["Python", "PyTorch", "LangChain"],
        "answers": {
            "Are you open to relocation?": "Yes",
            "Are you open to working in-person in one of our offices 25% of the time?": "Yes",
            "Are you willing to travel?": "Yes",
        },
        "sensitive_answers": {
            "start_date": {"answer": "Within a month", "approved": True},
            "relocation": {"answer": "Yes", "approved": True},
        },
        "screening_answer_rules": [
            {"patterns": ["willing to travel"], "answer": "Yes"},
            {"patterns": ["four days per week", "work from our NYC"], "answer": "Yes"},
        ],
        "education": [{"degree": "Master's Degree", "field": "Computer Science", "end_date": "2026-05"}],
        "work_history": [{"description": "Built ML systems with Python and PyTorch."}],
    }

    assert python_runtime._auto_answer(
        "Are you willing to work four days per week in our San Francisco office?*",
        profile,
    ) == "Yes"
    assert python_runtime._auto_answer(
        "Are you excited and able to work from our NYC, SF, or Stockholm office?",
        profile,
    ) == "Yes"
    assert python_runtime._auto_answer(
        "Do you have hands-on engineering experience with Python and ML frameworks (e.g., PyTorch)?",
        profile,
    ) == "Yes"
    assert python_runtime._auto_answer(
        "Are you comfortable in a customer-facing role, embedding with enterprise customers (including occasional travel)?",
        profile,
    ) == "Yes"
    assert python_runtime._auto_answer("Are you a Student or New Grad?", profile) == "Yes"
    assert (
        python_runtime._auto_answer("If yes, when is your earliest start date?", profile, sensitive=True)
        == "Immediately/next few months, full-time"
    )
    assert python_runtime._option_matches("Immediately/next few months, full-time", "Within a month")
    assert python_runtime._auto_answer("What is your expected graduation month & year?*", profile) == "May 2026"
    assert python_runtime._auto_answer("Where have you published your work?*", profile) == "N/A"


def test_highest_education_answer_uses_profile_facts():
    assert python_runtime._auto_answer(
        "What is the highest level of education you have completed, and from which institution?",
        {
            "education": [
                {
                    "degree": "Master's Degree",
                    "field": "Computer Science",
                    "school": "Stevens Institute of Technology",
                }
            ]
        },
    ) == "Master's Degree in Computer Science from Stevens Institute of Technology"


def test_required_transcript_file_field_uses_configured_transcript(tmp_path):
    transcript = tmp_path / "transcript.pdf"
    transcript.write_bytes(b"%PDF-1.4\ntranscript")

    assert python_runtime._plan_field(
        {
            "type": "file",
            "label": "Please include a copy of your college or university transcript. An unofficial transcript is fine.",
            "required": True,
        },
        {"_transcript_file": str(transcript)},
        str(tmp_path / "resume.pdf"),
    ) == {"action": "upload", "value": str(transcript)}


def test_required_transcript_file_field_blocks_without_transcript():
    assert python_runtime._plan_field(
        {
            "type": "file",
            "label": "Please include a copy of your college or university transcript. An unofficial transcript is fine.",
            "required": True,
        },
        {},
        "/tmp/resume.pdf",
    ) == {"action": "skip", "reason": "transcript file required but no transcript configured"}


def test_capmonster_hcaptcha_unsupported_uses_vision_fallback(monkeypatch):
    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.setenv("CAPTCHA_VISION_FALLBACK", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "vision-key")
    monkeypatch.setattr(
        python_runtime,
        "_discover_captcha",
        lambda page: {
            "kind": "hcaptcha",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
        },
    )
    monkeypatch.setattr(
        python_runtime.CapMonsterClient,
        "solve_task",
        lambda self, task, timeout_seconds, poll_interval_seconds: (_ for _ in ()).throw(
            python_runtime.CapMonsterError("ERROR_TASK_NOT_SUPPORTED")
        ),
    )
    monkeypatch.setattr(
        python_runtime,
        "_solve_hcaptcha_with_vision",
        lambda page: {"status": "solved", "detail": "hcaptcha vision fallback in 2 rounds"},
    )

    assert python_runtime._solve_captcha_if_configured(object()) == {
        "status": "solved",
        "detail": "hcaptcha vision fallback in 2 rounds",
    }


def test_capmonster_hcaptcha_token_task_is_attempted_without_vision(monkeypatch):
    events = []

    class FakeClient:
        def __init__(self, api_key):
            events.append(("client", api_key))

        def solve_task(self, task, timeout_seconds, poll_interval_seconds):
            events.append(("solve", task["type"]))
            return {"gRecaptchaResponse": "hcaptcha-token"}

    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.delenv("CAPTCHA_VISION_FALLBACK", raising=False)
    monkeypatch.delenv("CAPMONSTER_HCAPTCHA_TASK_TYPE", raising=False)
    monkeypatch.setattr(
        python_runtime,
        "_discover_captcha",
        lambda page: {
            "kind": "hcaptcha",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
        },
    )
    monkeypatch.setattr(python_runtime, "CapMonsterClient", FakeClient)

    result = python_runtime._solve_captcha_if_configured(object())

    assert result["status"] == "solution_not_injected"
    assert "hcaptcha at https://jobs.example.com/apply" in result["detail"]
    assert events == [("client", "cap-key"), ("solve", "HCaptchaTaskProxyless")]


def test_capmonster_recaptcha_v2_task_type_error_retries_legacy_alias(monkeypatch):
    events = []

    class FakeClient:
        def __init__(self, api_key):
            events.append(("client", api_key))

        def solve_task(self, task, timeout_seconds, poll_interval_seconds):
            events.append(("solve", task["type"]))
            if task["type"] == "RecaptchaV2Task":
                raise python_runtime.CapMonsterError("ERROR_TASK_NOT_SUPPORTED")
            return {"gRecaptchaResponse": "recaptcha-token"}

    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.setattr(
        python_runtime,
        "_discover_captcha",
        lambda page: {
            "kind": "recaptchaV2",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
        },
    )
    monkeypatch.setattr(python_runtime, "CapMonsterClient", FakeClient)
    monkeypatch.setattr(
        python_runtime,
        "_inject_captcha_solution",
        lambda _page, _challenge, _solution: events.append(("inject", _solution["gRecaptchaResponse"])) or True,
    )

    assert python_runtime._solve_captcha_if_configured(object()) == {
        "status": "solved",
        "detail": "recaptchaV2 at https://jobs.example.com/apply",
    }
    assert events == [
        ("client", "cap-key"),
        ("solve", "RecaptchaV2Task"),
        ("solve", "NoCaptchaTaskProxyless"),
        ("inject", "recaptcha-token"),
    ]


def test_capmonster_turnstile_task_type_error_retries_legacy_alias(monkeypatch):
    events = []

    class FakeClient:
        def __init__(self, api_key):
            events.append(("client", api_key))

        def solve_task(self, task, timeout_seconds, poll_interval_seconds):
            events.append(("solve", task["type"]))
            if task["type"] == "TurnstileTask":
                raise python_runtime.CapMonsterError("ERROR_TASK_NOT_SUPPORTED")
            return {"token": "turnstile-token"}

    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.setattr(
        python_runtime,
        "_discover_captcha",
        lambda page: {
            "kind": "turnstile",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "turnstile-key",
        },
    )
    monkeypatch.setattr(python_runtime, "CapMonsterClient", FakeClient)
    monkeypatch.setattr(
        python_runtime,
        "_inject_captcha_solution",
        lambda _page, _challenge, _solution: events.append(("inject", _solution["token"])) or True,
    )

    assert python_runtime._solve_captcha_if_configured(object()) == {
        "status": "solved",
        "detail": "turnstile at https://jobs.example.com/apply",
    }
    assert events == [
        ("client", "cap-key"),
        ("solve", "TurnstileTask"),
        ("solve", "TurnstileTaskProxyless"),
        ("inject", "turnstile-token"),
    ]


def test_capmonster_hcaptcha_tries_token_tasks_before_vision_fallback(monkeypatch):
    attempts = []

    class FakeClient:
        def __init__(self, api_key):
            pass

        def solve_task(self, task, timeout_seconds, poll_interval_seconds):
            attempts.append(task["type"])
            raise python_runtime.CapMonsterError("ERROR_TASK_NOT_SUPPORTED")

    monkeypatch.setenv("CAPMONSTER_API_KEY", "cap-key")
    monkeypatch.setenv("CAPMONSTER_SOLVE_CAPTCHA", "true")
    monkeypatch.setenv("CAPTCHA_VISION_FALLBACK", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "vision-key")
    monkeypatch.delenv("CAPMONSTER_HCAPTCHA_TASK_TYPE", raising=False)
    monkeypatch.setattr(
        python_runtime,
        "_discover_captcha",
        lambda page: {
            "kind": "hcaptcha",
            "websiteURL": "https://jobs.example.com/apply",
            "websiteKey": "site-key",
        },
    )
    monkeypatch.setattr(python_runtime, "CapMonsterClient", FakeClient)
    monkeypatch.setattr(
        python_runtime,
        "_solve_hcaptcha_with_vision",
        lambda page: {"status": "unsupported", "detail": "vision failed"},
    )

    result = python_runtime._solve_captcha_if_configured(object())

    assert attempts == ["HCaptchaTaskProxyless", "HCaptchaTask"]
    assert result["status"] == "unsupported"
    assert "HCaptchaTaskProxyless: ERROR_TASK_NOT_SUPPORTED" in result["detail"]
    assert "vision fallback: vision failed" in result["detail"]


def test_capmonster_task_for_datadome_requires_proxy(monkeypatch):
    challenge = {
        "kind": "datadome",
        "websiteURL": "https://jobs.example.com/apply",
        "captchaUrl": "https://geo.captcha-delivery.com/interstitial/?initialCid=abc",
        "datadomeCookie": "datadome=cookie",
    }

    assert python_runtime._capmonster_task_for(challenge) is None

    monkeypatch.setenv("CAPMONSTER_PROXY_TYPE", "http")
    monkeypatch.setenv("CAPMONSTER_PROXY_ADDRESS", "proxy.example.com")
    monkeypatch.setenv("CAPMONSTER_PROXY_PORT", "8080")

    task = python_runtime._capmonster_task_for(challenge)

    assert task["type"] == "CustomTask"
    assert task["class"] == "DataDome"
    assert task["metadata"]["datadomeCookie"] == "datadome=cookie"
    assert task["proxyAddress"] == "proxy.example.com"


def test_demographics_fill_only_eeo_sensitive_fields():
    profile = {
        "demographics": {
            "gender": "Prefer not to say",
            "ethnicity": "Prefer not to say",
            "race": "Prefer not to say",
            "disability": "Prefer not to say",
            "veteran": "Prefer not to say",
        }
    }

    assert python_runtime._match_sensitive("Gender", profile) == "Prefer not to say"
    assert python_runtime._match_sensitive("Are you Hispanic/Latino?", profile) == "Prefer not to say"
    assert python_runtime._match_sensitive("Veteran Status", profile) == "Prefer not to say"
    assert python_runtime._match_sensitive("Disability Status", profile) == "Prefer not to say"
    assert python_runtime._match_sensitive("Are you eligible to obtain the security clearance?", profile) is None


def test_prefer_not_to_say_matches_real_ats_decline_options():
    assert python_runtime._option_matches("Decline To Self Identify", "Prefer not to say")
    assert python_runtime._option_matches("I don't wish to answer", "Prefer not to say")
    assert python_runtime._option_matches("I do not want to answer", "Prefer not to say")
    assert python_runtime._option_matches("Decline To Self Identify", "I don't wish to answer")


def test_optional_sensitive_combobox_without_approval_is_not_submission_blocking():
    field = {
        "kind": "single",
        "role": "combobox",
        "label": "Disability Status",
        "required": False,
        "value": "",
    }

    plan = python_runtime._plan_field(field, {"sensitive_answers": {}}, None)

    assert plan == {
        "action": "skip",
        "reason": "optional demographic left unselected",
        "sensitive": True,
        "blocking": False,
    }


def test_optional_demographic_decline_is_left_unselected():
    field = {
        "kind": "single",
        "role": "combobox",
        "label": "Gender",
        "required": False,
        "value": "",
    }
    profile = {
        "sensitive_answers": {
            "eeo_gender": {
                "patterns": ["gender"],
                "answer": "I don't wish to answer",
                "approved": True,
            }
        }
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["reason"] == "optional demographic left unselected"
    assert plan["blocking"] is False


def test_east_asian_race_does_not_match_hispanic_option():
    assert not python_runtime._option_matches("Hispanic or Latino", "East Asian")
    assert python_runtime._option_matches("Asian (Not Hispanic or Latino)", "East Asian")
    assert python_runtime._option_matches("East Asian (inclusive of Chinese, Japanese, Korean, Mongolian, Tibetan, and Taiwanese)", "East Asian")
    assert python_runtime._option_matches("Asian", "East Asian")
    assert python_runtime._option_matches("East Asian", "East Asian")
    assert not python_runtime._option_matches("South Asian", "East Asian")
    assert not python_runtime._option_matches("Southeast Asian", "East Asian")
    assert not python_runtime._option_matches("White or Caucasian", "East Asian")


def test_work_authorization_dropdown_label_noise_resolves_to_sponsorship_answer():
    profile = {
        "target_company": "waymo",
        "work_authorization_by_country": {"requires_sponsorship": "Yes"},
        "sensitive_answers": {
            "work_authorization_current_country": {
                "patterns": ["authorized to work in the country for which you are applying"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }
    answer = python_runtime._work_authorization_dropdown_answer(
        "Work Authorization\n    (required)\n  \n  ed0fa5ad",
        profile,
    )
    assert "require" in answer and "waymo" in answer
    option = (
        "I require, or in the future will require, Waymo's sponsorship to obtain "
        "work authorization in the country in which this position is based (e.g. H-1B, TN, etc.)"
    )
    assert python_runtime._option_matches(option, answer)


def test_option_token_containment_never_matches_negated_statement():
    answer = (
        "I require/will require Waymo's sponsorship to obtain work authorization "
        "in the country in which this position is based"
    )
    assert python_runtime._option_matches(
        "I do not require Waymo's sponsorship to obtain work authorization "
        "in the country in which this position is based",
        answer,
    ) is False
    assert python_runtime._option_matches(
        "I require, or in the future will require, Waymo's sponsorship to obtain "
        "work authorization in the country in which this position is based (e.g. H-1B, TN, etc.)",
        answer,
    ) is True


def test_last_time_wrote_code_professionally_uses_bounded_combobox_answer():
    profile = {"answers": {}, "demographics": {}, "sensitive_answers": {}}
    assert python_runtime._priority_auto_answer(
        "When was the last time you wrote code professionally?",
        profile,
    ) == "Within the last 6 months"
    assert python_runtime._priority_auto_answer(
        "In your current/recent role, do you regularly read and understand code written by other engineers?",
        profile,
    ) == "Yes"


def test_single_radio_field_uses_check_instead_of_text_fill():
    profile = {"answers": {"Graduation Date or Anticipated Graduation Date": "May 2026"}}
    field = {
        "kind": "single",
        "tag": "input",
        "type": "radio",
        "label": "Graduation Date or Anticipated Graduation Date",
        "value": "May 2026",
        "required": True,
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check"}


def test_required_additional_information_is_not_skipped_as_optional():
    profile = {
        "answers": {"Additional Information*": "Here is a short additional note."},
        "sensitive_answers": {},
    }
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Additional Information*",
        "required": True,
        "options": [],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {"action": "fill", "value": "Here is a short additional note."}


def test_recover_text_fill_locator_rebinds_radio_marker_to_editable_control():
    class Locator:
        def __init__(self, payload):
            self.payload = payload

        def evaluate(self, _script):
            return self.payload

    class LocatorResult:
        def __init__(self, recovered):
            self.first = recovered

    class Page:
        def __init__(self):
            self.recovered = Locator({"tag": "input", "type": "text", "role": "", "editable": False})
            self.selector = None
            self.payload = None

        def evaluate(self, _script, payload):
            self.payload = payload
            return "marker-1"

        def locator(self, selector):
            self.selector = selector
            return LocatorResult(self.recovered)

    page = Page()
    original = Locator({"tag": "input", "type": "radio", "role": "", "editable": False})

    recovered = python_runtime._recover_text_fill_locator(
        page,
        {"label": "Graduation Date or Anticipated Graduation Date"},
        original,
    )

    assert recovered is page.recovered
    assert page.payload == {"label": "Graduation Date or Anticipated Graduation Date"}
    assert page.selector == '[data-job-agent-fill-target="marker-1"]'


def test_hispanic_latino_yes_no_field_maps_east_asian_to_no():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you Hispanic or Latino?",
        "required": True,
        "options": ["Select One", "Yes", "No"],
    }
    profile = {"demographics": {"ethnicity": "East Asian", "race": "East Asian"}}

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_hispanic_latino_yes_no_field_beats_broad_eeo_ethnicity_kb():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you Hispanic or Latino?",
        "required": True,
        "options": ["Select One", "Yes", "No"],
    }
    profile = {
        "demographics": {"ethnicity": "East Asian", "race": "East Asian"},
        "sensitive_answers": {
            "eeo_ethnicity": {
                "patterns": ["ethnicity", "race", "hispanic", "latino"],
                "answer": "East Asian",
                "approved": True,
            }
        },
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_workday_self_identify_language_does_not_use_programming_language():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Language*",
        "required": True,
        "options": ["Select One", "English", "Spanish"],
    }
    profile = {
        "preferred_programming_language": "Python",
        "answers": {"What is your preferred programming language for your interviews?": "Python"},
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "English",
    }


def test_coinbase_greenhouse_screening_fields_use_truthful_exact_options(monkeypatch):
    class _Today:
        year = 2026
        month = 7
        day = 18

    monkeypatch.setattr(python_runtime, "date", type("Date", (), {"today": staticmethod(lambda: _Today())}))
    profile = {
        "birthday": "2001-12-18",
        "answers": {},
        "skills": ["AI agents", "workflow automation"],
        "screening_answer_rules": [
            {
                "patterns": ["current government official"],
                "answer": "No, I am not a current or former Government Official",
            },
            {
                "patterns": ["close relative of a government official"],
                "answer": "No, I am not a relative of a government official.",
            },
            {"patterns": ["referred to this position"], "answer": "No"},
        ],
        "sensitive_answers": {
            "privacy_consent": {"approved": True, "answer": "Yes", "patterns": ["privacy notice"]},
            "legal_attestation": {"approved": True, "answer": "Yes", "patterns": ["arbitration agreement"]},
        },
    }
    cases = [
        ("Are you at least 18 years of age?*", "Yes"),
        (
            "Please confirm receipt of the above linked Global Data Privacy Notice and US Arbitration Agreement.*",
            "Confirmed",
        ),
        (
            "I understand that Coinbase may use AI tools to assist in the application and interview process.",
            "Yes",
        ),
        (
            "Which of the following best describes how you use AI tools today?*",
            "I design or automate workflows with AI tools (e.g., building agents, integrating AI into team processes).",
        ),
        (
            "Are you a current government official or were you a government official in the last five years?",
            "No, I am not a current or former Government Official",
        ),
        (
            "Are you a close relative of a government official (i.e., child/step-child, spouse/partner)?*",
            "No, I am not a relative of a government official.",
        ),
        (
            "To your knowledge, were you referred to this position by a senior leader or decision-maker at a current or prospective institutional client, business partner, or vendor of Coinbase?*",
            "No",
        ),
    ]

    for label, value in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": value,
        }


def test_roblox_greenhouse_screening_fields_use_profile_facts():
    profile = {
        "birthday": "2001-07-28",
        "answers": {
            "When can you start?": "Within a month",
            "Are you open to relocation?": "Yes",
        },
        "education": [
            {"school": "Stevens Institute of Technology", "degree": "Master's", "field": "Computer Science"},
            {"school": "Shenzhen University", "degree": "Bachelor's", "field": "Logistics Management"},
        ],
        "demographics": {"race": "East Asian", "ethnicity": "East Asian"},
    }
    cases = [
        (
            "At the time of application, are you 18+ years of age?*",
            {"action": "combobox", "value": "Yes"},
        ),
        (
            "When will you be available to work as a full-time, permanent employee? "
            "Full-time means working 40 hours per week while being based in our San Mateo, CA headquarters.*",
            {"action": "combobox", "value": "Immediately/next few months, full-time"},
        ),
        (
            "Are you pursuing or have you completed a PhD?*",
            {"action": "combobox", "value": "No"},
        ),
        (
            "How would you describe your racial/ethnic background? (mark all that apply)*",
            {"action": "combobox", "value": "Asian"},
        ),
    ]

    for label, expected in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
            "options": [],
            "value": "",
        }
        assert python_runtime._plan_field(field, profile, None) == expected


def test_verbose_work_authorization_answer_matches_yes_option():
    assert python_runtime._option_matches(
        "Yes",
        "Yes, I am currently legally authorized to work in the country where the job is located.",
    )


def test_coinbase_work_authorization_combobox_uses_exact_yes_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you legally authorized to work in the country where this position is located?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    profile = {
        "sensitive_answers": {
            "work_authorization_current_country": {
                "patterns": ["authorized to work in the country where this position is located"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_production_experience_screening_comboboxes_use_profile_evidence_conservatively():
    profile = {
        "answers": {
            "Are you open to relocation?": "Yes",
        },
        "screening_answer_rules": [
            {
                "patterns": ["able to work onsite", "5 days in-office"],
                "answer": "Yes",
            }
        ],
        "sensitive_answers": {
            "relocation": {
                "patterns": ["relocation", "relocate", "willing to relocate"],
                "answer": "Yes",
                "approved": True,
            }
        },
        "skills": ["RAG", "LangChain", "Kubernetes", "Kafka"],
        "projects": [
            {"description": "Built a LangChain agent and RAG evaluation framework."},
        ],
        "work_history": [
            {
                "description": (
                    "Deployed federated LLM workflows on Kubernetes with Kafka and MLflow. "
                    "Dockerized a Transformer REST microservice, productionizing retraining "
                    "with drift detection and improving customer-retention targeting precision."
                )
            }
        ],
    }
    cases = {
        "Are you able to work onsite in our Mountain View, CA office?(5 days in-office required)*": "Yes",
        "Have you built and deployed a production system using LLMs (e.g., tool use, RAG, or agent-based system)?*": "Yes",
        "Have you built any system that automatically optimizes decisions based on feedback signals (e.g., CTR, ROAS, CPA, conversions)?*": "Yes",
        "Have you worked on advertising systems, recommendation systems, or ranking/optimization systems in production?*": "No",
        "Have you built production backend services (not prototypes) involving APIs, async systems, or distributed components?*": "Yes",
        "Have you shipped ML/AI models or systems that were used in real production traffic?*": "Yes",
        "Have you worked on systems that directly impacted business metrics such as revenue, conversion rate, or advertiser spend?*": "Yes",
    }

    for label, expected in cases.items():
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
            "options": ["Yes", "No"],
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": expected,
        }


def test_sponsorship_compound_authorization_question_returns_no_when_future_sponsorship_required():
    profile = {
        "sensitive_answers": {
            "sponsorship": {
                "patterns": ["sponsorship"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Are you legally authorized to work in the United States without requiring visa sponsorship now or in the future?*",
        "required": True,
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "fill",
        "value": "No",
    }


def test_currently_living_in_bay_area_combobox_uses_profile_location():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you currently living in the San Francisco Bay Area?*",
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(field, {"location": "Jersey City, NJ, USA"}, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_botauto_work_authorization_and_sponsorship_comboboxes_use_exact_yes_options():
    profile = {
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["authorized to work"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {
                "patterns": ["sponsorship", "require immigration sponsorship"],
                "answer": "Yes",
                "approved": True,
            },
        }
    }
    cases = [
        "Are you legally authorized to work in the US?*",
        "Do you now, or will you in the future, require immigration sponsorship for work authorization (for example, H-1B status)?*",
    ]

    for label in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
            "options": ["Yes", "No"],
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": "Yes",
        }


def test_vercel_privacy_notice_acknowledge_combobox_uses_approved_privacy_consent():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "By submitting my application, I acknowledge that I have read and understand "
            "Vercel's Job Applicant Privacy Notice - https://vercel.com/legal/job-applicant-privacy-notice *"
        ),
        "required": True,
        "options": ["Acknowledge/Confirm"],
    }
    profile = {
        "sensitive_answers": {
            "privacy_consent": {"patterns": ["privacy notice"], "answer": "Yes", "approved": True}
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Acknowledge/Confirm",
    }


def test_vercel_work_authorization_detail_combobox_uses_sponsorship_specific_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Your authorization to work in the country where you live. "
            "Please choose the option that describes your work authorization. *"
        ),
        "required": True,
        "options": [
            "I am authorized to work in the country due to my nationality",
            "I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa",
            "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
            "I am not authorized to work in the country and need visa support",
            "Other",
        ],
    }
    profile = {
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["authorized to work in the united states"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {
                "patterns": ["sponsorship"],
                "answer": "Yes",
                "approved": True,
            },
        },
        "work_authorization_by_country": {"us": "Yes", "requires_sponsorship": "Yes"},
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "I am authorized to work in the country based on a valid work permit which needs to be sponsored by the company I work for",
    }


def test_vercel_accuracy_confirmation_combobox_uses_approved_legal_attestation():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Please double-check all the information provided above. Ensuring accuracy is crucial, "
            "as any errors or omissions may impact the review of your application.*"
        ),
        "required": True,
        "options": ["Acknowledge/Confirm"],
    }
    profile = {
        "sensitive_answers": {
            "legal_attestation": {"patterns": ["true and accurate"], "answer": "Yes", "approved": True}
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Acknowledge/Confirm",
    }


def test_vercel_accuracy_confirmation_combobox_matches_reviewed_confirmed_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Please double-check all the information provided above. Ensuring accuracy is crucial, "
            "as any errors or omissions may impact the review of your application.*"
        ),
        "required": True,
        "options": [
            "I have reviewed and confirmed that all the information provided is accurate and complete."
        ],
    }
    profile = {
        "sensitive_answers": {
            "legal_attestation": {"patterns": ["true and accurate"], "answer": "Yes", "approved": True}
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "I have reviewed and confirmed that all the information provided is accurate and complete.",
    }


def test_state_list_residency_combobox_uses_profile_state():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Do you live in one of the following states? Alabama, Alaska, Delaware, Kansas, "
            "Maine, Mississippi, Montana, Nebraska, New Mexico, North Dakota, South Dakota, "
            "West Virginia, or Wyoming.*"
        ),
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(field, {"state": "NJ", "location": "Jersey City, NJ, USA"}, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_chime_hybrid_role_acknowledgement_uses_relocation_approval():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Do you acknowledge that this is a hybrid role based in San Francisco, New York "
            "and/or Chicago and you will be required to come into the office four days a week?*"
        ),
        "required": True,
        "options": ["Yes", "No"],
    }
    profile = {
        "sensitive_answers": {
            "relocation": {"patterns": ["relocation", "relocate"], "answer": "Yes", "approved": True}
        },
        "screening_answer_rules": [
            {"patterns": ["hybrid role", "office four days a week"], "answer": "Yes"}
        ],
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_ambiguous_required_demographic_identity_uses_decline_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "I identify as:*",
        "required": True,
        "options": ["Cisgender", "Transgender", "I don't wish to answer"],
    }

    assert python_runtime._plan_field(field, {"demographics": {"gender": "Male"}}, None) == {
        "action": "combobox",
        "value": "I don't wish to answer",
    }


def test_robinhood_previous_employment_matches_never_worked_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Have you ever worked for Robinhood as an employee, intern or contractor? "
            "Note that providing false or misleading information may result in disqualification from the hiring process.*"
        ),
        "required": True,
        "options": [
            "I currently work at Robinhood as a full-time employee or intern",
            "I have previously worked at Robinhood as a full-time employee or intern (Hoodie Alumni)",
            "I currently work at Robinhood in a contractor role",
            "I have previously worked at Robinhood in a contractor role",
            "I have never worked at Robinhood",
        ],
    }

    profile = {
        "screening_answer_rules": [
            {
                "patterns": ["ever worked for Robinhood"],
                "answer": "I have never worked at Robinhood",
            }
        ]
    }
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "I have never worked at Robinhood",
    }


def test_robinhood_office_willingness_uses_relocation_approval():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you willing to work from the office(s) listed on the job description?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    profile = {
        "sensitive_answers": {
            "relocation": {"patterns": ["relocation", "relocate"], "answer": "Yes", "approved": True}
        },
        "screening_answer_rules": [
            {"patterns": ["willing to work from the office"], "answer": "Yes"}
        ],
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "combobox", "value": "Yes"}


def test_robinhood_conflict_disclosure_defaults_to_no_without_saved_relationships():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "Do you have: a) any Personal/Familial Relationships; b) any Outside Business Activities "
            "that you wish to continue; c) any investment in a private company that is a current competitor; "
            "or e) any Intellectual Property Ownership that you wish to retain?*"
        ),
        "required": True,
        "options": ["Yes", "No"],
    }

    profile = {
        "screening_answer_rules": [
            {"patterns": ["Personal/Familial Relationships"], "answer": "No"}
        ]
    }
    assert python_runtime._plan_field(field, profile, None) == {"action": "combobox", "value": "No"}


def test_robinhood_single_job_code_option_is_selected_over_url_id():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please indicate the job code number in the job posting here.*",
        "required": True,
        "options": ["10035097"],
    }

    assert python_runtime._plan_field(field, {"_application_url": "https://boards.greenhouse.io/robinhood/jobs/7960680"}, None) == {
        "action": "combobox",
        "value": "10035097",
    }


def test_robinhood_alphanumeric_job_code_option_is_selected_over_url_id():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please indicate the job code number in the job posting here.*",
        "required": True,
        "options": ["SWEB4BMPI4"],
    }

    assert python_runtime._plan_field(field, {"_application_url": "https://boards.greenhouse.io/robinhood/jobs/7975507"}, None) == {
        "action": "combobox",
        "value": "SWEB4BMPI4",
    }


def test_robinhood_job_code_option_is_selected_for_dynamic_radiogroup():
    field = {
        "kind": "radiogroup",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please indicate the job code number in the job posting here.*",
        "required": True,
        "options": ["SWEI4AMPI4", "Yes", "No", "Select..."],
    }

    assert python_runtime._plan_field(field, {}, None) == {
        "action": "combobox",
        "value": "SWEI4AMPI4",
    }


def test_negative_employment_history_answer_matches_never_worked_option():
    assert python_runtime._option_matches(
        "I have never worked at Robinhood",
        "No, I have never worked at Robinhood",
    )


def test_samsara_learned_about_source_combobox_matches_linkedin_jobs():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Where have you learned about Samsara? Select all that apply.*",
        "required": True,
        "options": ["LinkedIn Jobs", "Samsara Blog", "Other"],
    }
    profile = {"answers": {"How did you hear about us?": "LinkedIn"}}

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "LinkedIn Jobs",
    }


def test_pinterest_current_us_state_combobox_uses_profile_location_state():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "What U.S State do you currently reside in? *",
        "required": True,
        "options": ["California", "New Jersey", "New York"],
    }
    profile = {"location": "Jersey City, NJ, USA"}

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "New Jersey",
    }


def test_continental_us_residency_combobox_uses_yes_not_profile_state():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Do you currently reside within the continental United States?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    profile = {
        "location": "Jersey City, NJ, USA",
        "country": "United States",
        "answers": {},
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_military_status_no_matches_never_served_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "What is your military status?*",
        "required": True,
        "options": [
            "I am on active duty",
            "I am part of the national guard or on reserve",
            "I have never served in the military",
            "I identify as a protected veteran",
            "I don't wish to answer",
        ],
    }
    profile = {"demographics": {"veteran": "No"}}

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "I have never served in the military",
    }


def test_demographic_survey_consent_checkbox_uses_approved_privacy_consent():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": (
            "By checking this box, I consent to Robinhood collecting, storing, and processing "
            "my responses to the demographic data surveys above.*"
        ),
        "required": True,
    }
    profile = {
        "sensitive_answers": {
            "privacy_consent": {"patterns": ["demographic data surveys"], "answer": "Yes", "approved": True}
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check", "sensitive": True}


def test_twilio_acknowledge_checkbox_uses_approved_privacy_consent():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Acknowledge",
        "required": True,
    }
    profile = {
        "target_company": "twilio",
        "sensitive_answers": {
            "privacy_consent": {"patterns": ["privacy policy"], "answer": "Yes", "approved": True}
        },
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check"}


def test_candidate_ai_responsible_use_policy_checkbox_uses_legal_attestation():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": (
            "By checking this box, I confirm I have read, reviewed and understood the guidelines "
            "outlined in the Candidate AI Responsible Use Policy. I affirm that all the information "
            "and materials I submit throughout my application and candidacy will reflect my own work "
            "and experience. *Acknowledge"
        ),
        "required": True,
    }
    profile = {
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["own work and experience", "candidate ai responsible use policy"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check"}


def test_nuro_work_authorization_country_applying_combobox_uses_exact_yes_option():
    profile = {
        "target_location": "Mountain View, California (HQ)",
        "sensitive_answers": {
            "work_authorization_current_country": {
                "patterns": [
                    "authorized to work in the country for which you are applying",
                    "authorized to work in the country where this position is located",
                ],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you authorized to work in the country in which you are applying?*",
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_canada_work_authorization_context_overrides_generic_applied_country_yes():
    profile = {
        "target_company": "Diligent",
        "target_title": "AI Agent Engineer – Commercial AI Transformation",
        "target_location": "Vancouver, British Columbia, Canada",
        "sensitive_answers": {
            "work_authorization_canada": {
                "patterns": ["authorized to work in canada", "work in canada"],
                "answer": "No",
                "approved": True,
            },
            "work_authorization_current_country": {
                "patterns": [
                    "authorized to work in the country for which you are applying",
                    "authorized to work in the country where this position is located",
                ],
                "answer": "Yes",
                "approved": True,
            },
            "work_authorization_us": {
                "patterns": ["authorized to work in the united states"],
                "answer": "Yes",
                "approved": True,
            },
        },
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you authorized to work in the country in which you are applying?*",
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_uk_work_authorization_context_overrides_generic_applied_country_yes():
    profile = {
        "target_company": "Diligent",
        "target_title": "Forward Deployed Engineer - Agentic AI",
        "target_location": "London, England, United Kingdom",
        "sensitive_answers": {
            "work_authorization_uk": {
                "patterns": ["authorized to work in the united kingdom", "work in the united kingdom"],
                "answer": "No",
                "approved": True,
            },
            "work_authorization_current_country": {
                "patterns": ["authorized to work in the country where this position is located"],
                "answer": "Yes",
                "approved": True,
            },
        },
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you legally authorized to work in the country where this position is located?*",
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_welbehealth_style_ai_screening_yes_no_fields_use_profile_evidence():
    profile = {
        "skills": ["Python", "LangChain", "RAG"],
        "projects": [
            {
                "title": "LLM Project",
                "description": "Built a LangChain RAG workflow using OpenAI APIs and Python scripts.",
            }
        ],
        "sensitive_answers": {
            "sponsorship": {"patterns": ["sponsorship"], "answer": "Yes", "approved": True}
        },
    }
    cases = [
        "Have you worked with or completed academic projects, internships, personal projects, or professional work using Large Language Models (LLMs) such as OpenAI, Anthropic Claude, or Google Gemini?*",
        "Do you have working proficiency in Python, including writing and debugging scripts, interacting with APIs, and working with common data structures?*",
        "Will you now or at any point in the future require employer-sponsored work authorization or visa sponsorship to legally work in the United States, including but not limited to H-1B, TN, E-3, O-1, F-1 OPT/STEM OPT, or any other employment-based visa status?*",
    ]

    for label in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": label,
            "required": True,
            "options": ["Yes", "No"],
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": "Yes",
        }


def test_welbehealth_style_ai_concepts_checkbox_group_uses_profile_evidence():
    profile = {
        "skills": ["Python", "LangChain", "RAG", "BERT", "PyTorch"],
        "projects": [
            {
                "title": "LLM Audit Agent",
                "description": (
                    "Built a LangChain multi-agent workflow with RAG, BERT embedding "
                    "semantic similarity evaluation, LoRA fine-tuning, and retrieval."
                ),
            }
        ],
    }
    field = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "Which AI concepts have you worked with? (Select all that apply.) *",
        "required": True,
        "options": [
            {"label": "Prompt Engineering"},
            {"label": "Embeddings"},
            {"label": "Vector Search"},
            {"label": "Retrieval-Augmented Generation (RAG)"},
            {"label": "Semantic Search"},
            {"label": "Fine-Tuning Models"},
            {"label": "AI Agents / Agentic Workflows"},
            {"label": "None of the above"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "checkmany"
    assert [option["label"] for option in plan["options"]] == [
        "Prompt Engineering",
        "Embeddings",
        "Vector Search",
        "Retrieval-Augmented Generation (RAG)",
        "Semantic Search",
        "Fine-Tuning Models",
        "AI Agents / Agentic Workflows",
    ]


def test_welbehealth_style_llm_cloud_and_rag_checkbox_groups_use_truthful_options():
    profile = {
        "skills": [
            "AWS",
            "Microsoft Azure",
            "Hugging Face Transformers",
            "LangChain",
            "RAG",
        ],
        "answers": {
            "Have you ever interviewed at Anthropic before?": "No",
            "Do you have experience with public clouds AWS or GCP and integrating with their APIs?": "No",
        },
        "projects": [
            {
                "title": "XClaw",
                "description": "Integrated OpenAI image generation into an autonomous LLM agent platform.",
            },
            {
                "title": "RAG Evaluation",
                "description": "Designed custom retrieval and RAG evaluation harnesses.",
            },
        ],
    }
    llm = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "Which Large Language Model (LLM) platforms or APIs have you used? (Select all that apply.) *",
        "options": [
            {"label": "OpenAI"},
            {"label": "Anthropic Claude"},
            {"label": "Google Gemini"},
            {"label": "Azure OpenAI Service"},
            {"label": "Hugging Face"},
            {"label": "Ollama"},
            {"label": "Other"},
            {"label": "None"},
        ],
    }
    cloud = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "Which cloud platforms have you worked with? (Select all that apply.) *",
        "options": [
            {"label": "Microsoft Azure"},
            {"label": "Amazon Web Services (AWS)"},
            {"label": "Google Cloud Platform (GCP)"},
            {"label": "None"},
        ],
    }
    rag = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "Do you have experience with Retrieval-Augmented Generation (RAG) architectures or vector databases? (Select all that apply.) *",
        "options": [
            {"label": "Pinecone"},
            {"label": "Weaviate"},
            {"label": "Chroma"},
            {"label": "Azure AI Search"},
            {"label": "FAISS"},
            {"label": "Milvus"},
            {"label": "Other"},
            {"label": "None"},
        ],
    }

    assert [option["label"] for option in python_runtime._plan_field(llm, profile, None)["options"]] == [
        "OpenAI",
        "Hugging Face",
    ]
    assert [option["label"] for option in python_runtime._plan_field(cloud, profile, None)["options"]] == [
        "Microsoft Azure",
        "Amazon Web Services (AWS)",
    ]
    assert [option["label"] for option in python_runtime._plan_field(rag, profile, None)["options"]] == [
        "Other"
    ]


def test_welbehealth_style_single_checkbox_option_can_use_group_context():
    profile = {"skills": ["LangChain", "RAG"]}
    prompt = {
        "kind": "single",
        "type": "checkbox",
        "label": "Prompt Engineering",
        "section": "Which AI concepts have you worked with? (Select all that apply.) *",
        "required": True,
    }
    none = {
        "kind": "single",
        "type": "checkbox",
        "label": "None of the above",
        "section": "Which AI concepts have you worked with? (Select all that apply.) *",
        "required": True,
    }

    assert python_runtime._plan_field(prompt, profile, None) == {"action": "check"}
    assert python_runtime._plan_field(none, profile, None) == {
        "action": "skip",
        "reason": "technical screening negative option not selected",
        "sensitive": False,
        "blocking": False,
    }


def test_databricks_export_control_checkbox_group_selects_none_of_the_above():
    field = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": (
            "Please confirm whether any of the below applies to you. Select all that apply. "
            "Note: This information will only be used to ensure compliance with U.S. sanctions "
            "and export controls. *"
        ),
        "required": True,
        "options": [
            {"label": "Citizen or permanent resident of Cuba, Iran, North Korea, or Syria"},
            {
                "label": (
                    "Ordinarily a resident of Cuba, Iran, North Korea, Syria or the Crimea, "
                    "Donetsk, Luhansk, Zaporizhzhia, or Kherson regions of Ukraine"
                )
            },
            {"label": "Ordinarily a resident of Russia or Belarus and not willing to relocate"},
            {"label": "None of the above"},
        ],
    }

    plan = python_runtime._plan_field(field, {"country": "United States"}, None)

    assert plan["action"] == "checkmany"
    assert [option["label"] for option in plan["options"]] == ["None of the above"]


def test_databricks_export_control_followup_selects_not_applicable_after_none():
    field = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": (
            "If you selected a response to the prior question other than “none of the above,” "
            "please confirm whether any of the following also applies to you. Select all that apply. *"
        ),
        "required": True,
        "options": [
            {"label": "U.S. citizen"},
            {"label": "U.S. permanent resident (Green Card holder)"},
            {"label": "Individual granted citizenship in a country other than Cuba, Iran, North Korea, or Syria"},
            {"label": "None of these apply to me"},
            {"label": "Not applicable (i.e., I selected “none of the above” for the prior question)"},
        ],
    }

    plan = python_runtime._plan_field(field, {"country": "United States"}, None)

    assert plan["action"] == "checkmany"
    assert [option["label"] for option in plan["options"]] == [
        "Not applicable (i.e., I selected “none of the above” for the prior question)"
    ]


def test_databricks_production_genai_prompt_does_not_map_model_name_to_candidate_name():
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": (
            "Describe a production GenAI application you designed, built, and maintained. "
            "Include business use case, models/frameworks, architecture, and productionization "
            "and scaling challenges.* To verify this is an authentic application, please do not "
            "write a long essay. Limit your response to a maximum of 4 short bullet points and "
            "start your answer with the phrase \"The primary model used was [MODEL NAME]\""
        ),
        "required": True,
    }
    profile = {
        "name": "Gaoyi Wu",
        "work_history": [
            {
                "title": "Research Assistant",
                "company": "Intellisys Lab",
                "description": (
                    "Deployed scalable LLM fine-tuning pipelines on Kubernetes, used TensorFlow "
                    "Federated, Kafka, MLflow, and custom RAG evaluation harnesses across 100+ edge devices."
                ),
            }
        ],
        "skills": ["Python", "Kubernetes", "Kafka", "MLflow", "RAG", "Hugging Face Transformers"],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "fill"
    assert plan["value"].startswith("The primary model used was ")
    assert plan["value"] != "Gaoyi Wu"
    assert len([line for line in plan["value"].splitlines() if line.strip().startswith("- ")]) == 4


def test_c3_screening_comboboxes_use_priority_answers_not_fuzzy_answer_bank():
    onsite_label = "Are you willing to work onsite from our New York City office 5 days/week?*"
    profile = {
        "location": "Jersey City, NJ, USA",
        "years_experience": "1-2",
        "answers": {
            "What is your highest level of education?": (
                "I am completing a Master of Science in Computer Science at Stevens Institute of Technology, "
                "with an anticipated graduation date of May 2026."
            ),
            "What languages do you speak?": "English",
            "Where are you currently located?": "Jersey City, NJ, USA",
            "Are you open to relocation?": "Yes",
            onsite_label: "Yes",
        },
        "education": [{"degree": "Master's", "field": "Computer Science"}],
        "work_history": [
            {
                "title": "AI/ML Engineer Intern",
                "start_date": "2024-05",
                "end_date": "2024-08",
            },
            {
                "title": "Research Assistant",
                "start_date": "2024-09",
                "end_date": "2026-07",
            },
        ],
        "sensitive_answers": {
            "relocation": {"approved": True, "answer": "Yes", "patterns": ["relocation"]}
        },
    }
    highest = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "What is highest level of education you have completed?*",
        "required": True,
        "options": ["Associates Degree", "Bachelor's Degree", "Master's Degree", "Doctorate", "Not Applicable"],
    }
    oop_years = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "How many years of professional software development experience do you have with object-oriented programming languages?*",
        "required": True,
        "options": ["Less than 2 years", "2-5 years", "5+ years"],
    }
    onsite = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": onsite_label,
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(highest, profile, None) == {
        "action": "combobox",
        "value": "Master's Degree",
    }
    assert python_runtime._plan_field(oop_years, profile, None) == {
        "action": "combobox",
        "value": "2-5 years",
    }
    assert python_runtime._plan_field(onsite, profile, None) == {"action": "combobox", "value": "Yes"}


def test_figma_full_stack_screening_fields_are_auto_answered_truthfully():
    profile = {
        "years_experience": "1-2",
        "preferred_programming_language": "Python",
        "work_history": [
            {"title": "Research Assistant", "employment_type": "Internship"},
            {"title": "AI/ML Engineer Intern", "employment_type": "Internship"},
        ],
    }
    years = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "How many years of professional experience do you have in this type of role (excluding internships)?*",
        "required": True,
        "options": ["0-2 years", "3-4 years", "5-10 years", "10+ years"],
    }
    full_time = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Have you worked as a full-time software engineer in a professional setting (excluding internships)?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    languages = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Which programming languages do you regularly use in a professional setting?",
        "required": True,
        "options": ["JavaScript/TypeScript", "Python", "Go", "C++", "Java", "Ruby", "Swift", "Kotlin", "Other"],
    }

    assert python_runtime._plan_field(years, profile, None) == {
        "action": "combobox",
        "value": "0-2 years",
    }
    assert python_runtime._plan_field(full_time, profile, None) == {
        "action": "combobox",
        "value": "No",
    }
    assert python_runtime._plan_field(languages, profile, None) == {
        "action": "combobox",
        "value": "Python",
    }


def test_exa_ashby_application_questions_use_profile_facts(monkeypatch):
    class FixedDate(python_runtime.date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 22)

    monkeypatch.setattr(python_runtime, "date", FixedDate)
    profile = {
        "location": "Jersey City, NJ, USA",
        "answers": {
            "What is your earliest availability?": "Within a month",
            "Are you open to relocation?": "Yes",
        },
        "sensitive_answers": {"relocation": {"answer": "Yes", "approved": True}},
        "projects": [
            {
                "title": "XClaw: Desktop Interface for Open Claw",
                "description": "AI agent orchestration platform with execution skills.",
            }
        ],
    }
    earliest_month = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Earliest month you'd be able to join",
        "required": True,
    }
    proud = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What's something you worked on that you were proud of?",
        "required": True,
    }
    motivation = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What motivates you?",
        "required": True,
    }
    relocation = {
        "kind": "radiogroup",
        "label": "Are you based in San Francisco or open to relocating?",
        "required": True,
        "options": [{"label": "San Francisco based"}, {"label": "Open to relocating"}],
    }

    assert python_runtime._plan_field(earliest_month, profile, None) == {
        "action": "fill",
        "value": "August 2026",
    }
    assert "XClaw" in python_runtime._plan_field(proud, profile, None)["value"]
    assert "advanced AI useful" in python_runtime._plan_field(motivation, profile, None)["value"]
    assert python_runtime._plan_field(relocation, profile, None)["option"]["label"] == "Open to relocating"


def test_c3_i_accept_checkbox_uses_approved_privacy_consent():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I Accept",
        "section": (
            "By clicking the “I Accept” button, you give your consents as described below: "
            "C3 AI collects your personal data for recruitment related activities."
        ),
        "required": True,
    }
    profile = {
        "sensitive_answers": {
            "privacy_consent": {
                "approved": True,
                "answer": "Yes",
                "patterns": ["personal data"],
            }
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "check", "sensitive": True}


def test_company_relative_question_defaults_to_na():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Do any of your relatives currently work for or at WelbeHealth? If so, who?*",
        "required": True,
    }

    profile = {
        "screening_answer_rules": [
            {"patterns": ["relatives currently work"], "answer": "N/A"}
        ]
    }
    assert python_runtime._plan_field(field, profile, None) == {"action": "fill", "value": "N/A"}


def test_welbehealth_referral_and_employee_status_fields_default_truthfully():
    profile = {
        "target_company": "WelbeHealth",
        "first_name": "Gaoyi",
        "last_name": "Wu",
        "answers": {"Last Name": "Wu"},
        "screening_answer_rules": [
            {"patterns": ["referred by a current"], "answer": "No"},
            {"patterns": ["current employee of WelbeHealth"], "answer": "No"},
            {"patterns": ["ever worked for WelbeHealth"], "answer": "No"},
            {"patterns": ["referring individual's"], "answer": "N/A"},
        ],
    }
    fields = [
        (
            {
                "kind": "single",
                "tag": "input",
                "type": "text",
                "role": "combobox",
                "label": "Have you been referred by a current WelbeHealth employee?*",
                "required": True,
            },
            {"action": "combobox", "value": "No"},
        ),
        (
            {
                "kind": "single",
                "tag": "input",
                "type": "text",
                "role": "combobox",
                "label": "Are you a current employee of WelbeHealth?*",
                "required": True,
            },
            {"action": "combobox", "value": "No"},
        ),
        (
            {
                "kind": "single",
                "tag": "input",
                "type": "text",
                "role": "combobox",
                "label": "Have you ever worked for WelbeHealth?*",
                "required": True,
            },
            {"action": "combobox", "value": "No"},
        ),
        (
            {
                "kind": "single",
                "tag": "input",
                "type": "text",
                "label": "If you were referred, please specify the referring individual's first and last name. Otherwise, enter “N/A.\"*",
                "required": True,
            },
            {"action": "fill", "value": "N/A"},
        ),
    ]

    for field, expected in fields:
        assert python_runtime._plan_field(field, profile, None) == expected


def test_eeo_race_combobox_normalizes_east_asian_to_greenhouse_option():
    profile = {"demographics": {"race": "East Asian"}}
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please identify your race",
        "required": False,
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Asian",
    }


def test_wonderschool_applied_ai_screening_fields_use_profile_evidence():
    profile = {
        "education": [
            {"school": "Stevens Institute of Technology", "degree": "Master's", "field": "Computer Science"},
            {"school": "Shenzhen University", "degree": "Bachelor's", "field": "Logistics Management"},
        ],
        "sensitive_answers": {
            "relocation": {"patterns": ["relocation"], "answer": "Yes", "approved": True}
        },
        "screening_answer_rules": [
            {"patterns": ["comfortable coming in 5 days"], "answer": "Yes"}
        ],
    }
    cases = {
        "Are you comfortable coming in 5 days a week to the office?*": "Yes",
        "Where did you attend undergad?*": "Shenzhen University",
        "What did you get your undergrad degree in?*": "Logistics Management",
    }

    for label, expected in cases.items():
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox" if expected == "Yes" else "",
            "label": label,
            "required": True,
        }
        plan = python_runtime._plan_field(field, profile, None)
        assert plan["value"] == expected

    long_text_labels = [
        "Are you willing to spend a significant portion of your time (up to 50%) working directly with customers through support tickets and calls to inform the AI systems you build, and can you share specific examples where you’ve worked outside traditional engineering responsibilities or why this type of work appeals to you?*",
        "What experience do you have using tools like Claude Code, OpenClaw, or similar AI-assisted development environments?*",
        "What startup or founder experience do you have?*",
        "Describe a system you've built, either online or offline. Or even a video game. *",
    ]

    for label in long_text_labels:
        plan = python_runtime._plan_field(
            {"kind": "single", "tag": "textarea", "type": "textarea", "label": label, "required": True},
            profile,
            None,
        )
        assert plan["action"] == "fill"
        assert len(plan["value"]) > 80


def test_current_work_status_and_desired_compensation_use_profile_answers():
    profile = {
        "job_search_status": "Actively looking",
        "sensitive_answers": {
            "salary": {
                "patterns": ["salary", "compensation"],
                "answer": "At least $70k USD",
                "approved": True,
            }
        },
    }
    current_status = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Current Work Status *",
        "required": True,
    }
    desired_compensation = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Desired compensation range?*",
        "required": True,
    }

    assert python_runtime._plan_field(current_status, profile, None) == {
        "action": "fill",
        "value": "Actively looking",
    }
    assert python_runtime._plan_field(desired_compensation, profile, None) == {
        "action": "fill",
        "value": "At least $70k USD",
    }


def test_internship_hourly_pay_range_uses_approved_salary_floor():
    profile = {
        "sensitive_answers": {
            "salary": {
                "patterns": ["salary", "compensation", "pay expectation"],
                "answer": "At least $70k USD",
                "approved": True,
            }
        },
    }
    hourly = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "What is your expected hourly pay range for this internship?*",
        "required": True,
    }

    assert python_runtime._plan_field(hourly, profile, None) == {
        "action": "fill",
        "value": "At least $35/hour",
    }


def test_uare_gender_identity_and_transgender_demographics_use_available_options():
    profile = {"demographics": {"gender": "Male"}}
    cases = [
        (
            {
                "label": "How would you describe your gender identity?",
                "options": ["Man", "Non-binary", "Woman", "I prefer to self-describe", "I don't wish to answer"],
            },
            "Man",
        ),
        (
            {
                "label": "Do you identify as transgender?",
                "options": ["Yes", "No", "I prefer to self-describe", "I don't wish to answer"],
            },
            "I don't wish to answer",
        ),
    ]

    for data, expected in cases:
        field = {
            "kind": "single",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": data["label"],
            "required": True,
            "options": data["options"],
        }
        assert python_runtime._plan_field(field, profile, None) == {
            "action": "combobox",
            "value": expected,
        }

    assert python_runtime._option_matches("Man", "Man")
    assert python_runtime._option_matches("Cisgender man", "Man")
    assert not python_runtime._option_matches("Woman", "Man")


def test_aircall_voluntary_demographics_use_available_options(monkeypatch):
    class _Today:
        year = 2026
        month = 7
        day = 25

    monkeypatch.setattr(python_runtime, "date", type("Date", (), {"today": staticmethod(lambda: _Today())}))
    profile = {
        "birthday": "2001-07-28",
        "demographics": {"gender": "Male", "lgbtq": "No"},
    }
    sexual_orientation = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "I identify my sexual orientation as:",
        "required": True,
        "options": [
            {"label": "Asexual"},
            {"label": "Bisexual, pansexual, and/or queer"},
            {"label": "Gay and/or Lesbian"},
            {"label": "Heterosexual"},
            {"label": "I prefer to self-describe"},
            {"label": "I prefer not to say"},
        ],
    }
    age = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Age:",
        "required": True,
        "options": [
            {"label": "Prefer not to say"},
            {"label": "18 - 24"},
            {"label": "25 - 34"},
            {"label": "35 - 44"},
            {"label": "45 - 54"},
            {"label": "55 - 64"},
            {"label": "65 and over"},
        ],
    }

    assert python_runtime._plan_field(sexual_orientation, profile, None)["option"]["label"] == "I prefer not to say"
    assert python_runtime._plan_field(age, profile, None)["option"]["label"] == "18 - 24"


def test_single_radio_yes_answer_does_not_check_no_sponsorship_option():
    profile = {
        "sensitive_answers": {
            "sponsorship": {
                "patterns": ["sponsorship", "require sponsorship"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    yes_option = {
        "kind": "single",
        "type": "radio",
        "role": "radio",
        "label": "Yes - I do/will require sponsorship",
        "required": True,
    }
    no_option = {
        "kind": "single",
        "type": "radio",
        "role": "radio",
        "label": "No - I do not/will not require sponsorship, and I am authorized to work for any employer in the US",
        "required": True,
    }

    assert python_runtime._plan_field(yes_option, profile, None) == {"action": "check", "sensitive": True}
    assert python_runtime._plan_field(no_option, profile, None)["action"] == "skip"


def test_exact_date_field_uses_today_for_workday_self_identify(monkeypatch):
    class _Today:
        year = 2026
        month = 7
        day = 18

    monkeypatch.setattr(python_runtime, "date", type("Date", (), {"today": staticmethod(lambda: _Today())}))
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Date*",
        "required": True,
    }

    assert python_runtime._plan_field(field, {"birthday": "2001-12-18"}, None) == {
        "action": "fill",
        "value": "07/18/2026",
    }


def test_phone_communication_consent_uses_option_semantics():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Phone",
        "name": "communicationConsent",
        "required": False,
        "options": [
            {"value": "given", "label": "Yes - I consent to receiving text messages"},
            {"value": "notGiven", "label": "No - I do not consent to receiving text messages"},
        ],
    }
    profile = {"phone": "+1 (201) 283-4980", "answers": {"Phone Number": "+1 (201) 283-4980"}}

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {
        "action": "skip",
        "reason": "non-required unmapped field",
        "sensitive": True,
        "blocking": False,
    }


def test_detect_submission_confirmation_accepts_ashby_success_copy():
    class Page:
        def evaluate(self, _script):
            return {
                "url": "https://jobs.ashbyhq.com/quora/example/application",
                "title": "Machine Learning Engineer @ Quora",
                "text": "Application Success Thanks so much for applying to join us at Quora!",
            }

    confirmation = python_runtime._detect_submission_confirmation(Page())

    assert confirmation is not None
    assert "thanks so much for applying" in confirmation


def test_detect_submission_confirmation_accepts_translated_success_copy():
    class Page:
        def evaluate(self, _script):
            return {
                "url": "https://raftelis.breezy.hr/p/example/apply/submitted",
                "title": "Application",
                "text": "\u7533\u8bf7\u5df2\u63d0\u4ea4 \u60a8\u7684\u7533\u8bf7\u5df2\u6210\u529f\u63d0\u4ea4\u3002",
            }

    confirmation = python_runtime._detect_submission_confirmation(Page())

    assert confirmation is not None
    assert "localized submission confirmation" in confirmation


def test_detect_submission_confirmation_accepts_nuro_success_copy():
    class Page:
        def evaluate(self, _script):
            return {
                "url": "https://www.nuro.ai/careersitem?gh_jid=7351066",
                "title": "Work at Nuro | Nuro",
                "text": "Submitted, thanks!",
            }

    confirmation = python_runtime._detect_submission_confirmation(Page())

    assert confirmation is not None
    assert "submitted thanks" in confirmation


def test_short_no_does_not_match_inside_unrelated_words():
    assert not python_runtime._option_matches("One-North, Singapore", "No")
    assert python_runtime._option_matches("I'm not open to other locations", "No")


def test_no_matches_verbose_never_employed_option_phrases():
    assert python_runtime._option_matches(
        "I have not previously been employed at Affirm", "No"
    )
    assert python_runtime._option_matches(
        "I have never been employed at Affirm", "No"
    )
    assert not python_runtime._option_matches(
        "I have been employed at Affirm as a full-time employee", "No"
    )


def test_binary_yes_answer_matches_single_affirmative_sentence_option():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": (
            "Please confirm you are able to be in-person in San Francisco, CA "
            "or New York City, NY."
        ),
        "required": True,
        "options": [
            {
                "label": "I am able to be in-person in San Francisco or New York City",
                "autofillId": "yes",
            },
            {
                "label": "I am not able to be in-person in San Francisco or New York City",
                "autofillId": "no",
            },
        ],
    }
    profile = {"answers": {field["label"]: "Yes"}}

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "check"
    assert plan["option"]["autofillId"] == "yes"


def test_binary_yes_answer_does_not_guess_between_relocation_timelines():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you currently based in the SF Bay Area, or ready to relocate?",
        "required": True,
        "options": [
            {"label": "Yes, I'm in SF / Bay Area and willing to work on-site 5-days per week"},
            {"label": "Yes - I can relocate within 30 days (if offered the role)"},
            {"label": "Yes - I can relocate within 1-2 months (if offered the role)"},
            {"label": "No - I am not in SF, and will not be able to relocate anytime soon"},
        ],
    }
    profile = {
        "sensitive_answers": {
            "relocation": {
                "patterns": ["ready to relocate"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "skip",
        "reason": "no option matches saved answer",
        "sensitive": True,
    }


def test_binary_no_answer_deduplicates_duplicate_options():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Do you currently hold an active US security clearance?",
        "required": True,
        "options": [
            {"label": "Yes", "autofillId": "yes"},
            {"label": "No", "autofillId": "no1"},
            {"label": "No", "autofillId": "no2"},
        ],
    }

    plan = python_runtime._plan_field(field, {"sensitive_answers": {"active_security_clearance": {"patterns": ["active security clearance"], "answer": "No", "approved": True}}}, None)

    assert plan["action"] == "check"
    assert plan["option"]["autofillId"] == "no1"


def test_negative_answer_matches_none_of_the_above_and_never_held_option():
    field = {
        "kind": "combobox",
        "type": "select",
        "label": "If you have held a U.S. security clearance in the past, what clearance level have you held?",
        "required": True,
        "options": [
            {"label": "N/A - have never held U.S. security clearance"},
            {"label": "Confidential"},
            {"label": "Secret"},
            {"label": "Top Secret"},
        ],
    }

    matches = python_runtime._matching_options(field, "No")
    assert [o["label"] for o in matches] == ["N/A - have never held U.S. security clearance"]


def test_currently_based_in_us_question_uses_profile_location_not_country_name():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you currently based in the United States?",
        "required": True,
        "options": [
            {"label": "Yes", "autofillId": "yes"},
            {"label": "No", "autofillId": "no"},
        ],
    }

    plan = python_runtime._plan_field(
        field,
        {"location": "Jersey City, NJ, USA", "country": "United States"},
        None,
    )

    assert plan["action"] == "check"
    assert plan["option"]["autofillId"] == "yes"


def test_no_answer_matches_real_veteran_and_disability_decline_options():
    assert python_runtime._option_matches("I am not a veteran", "No")
    assert python_runtime._option_matches("I am not a protected veteran", "No")
    assert not python_runtime._option_matches("I identify as a veteran, just not a protected veteran", "No")
    assert python_runtime._option_matches("No, I do not have a disability", "No")


def test_real_consent_labels_are_sensitive_and_require_approved_answers():
    assert python_runtime._is_sensitive("Applicant Arbitration Agreement Acknowledgement")
    assert python_runtime._is_sensitive("I hereby certify that the answers given by me are true and correct")
    assert python_runtime._is_sensitive("Palantir will process your personal data")
    assert python_runtime._is_sensitive("AI notetakers to transcribe conversations")
    assert python_runtime._is_sensitive("EXPORT CONTROLS - this role may require access to export controlled items")
    assert python_runtime._is_sensitive("Are you any of the following protected individual(s) under U.S. law?")
    assert python_runtime._is_sensitive(
        "Have you signed an employment contract or non-compete agreement?"
    )
    assert python_runtime._is_sensitive(
        "Have you held H-1B status within the preceding 6 years?"
    )
    assert python_runtime._is_sensitive(
        "For the sole purpose of determining export licensing requirements, "
        "provide your country of citizenship."
    )

    assert python_runtime._match_sensitive("Applicant Arbitration Agreement Acknowledgement", {}) is None
    assert python_runtime._match_sensitive("Palantir will process your personal data", {}) is None
    assert python_runtime._match_sensitive("EXPORT CONTROLS - protected individual status", {}) is None


def test_real_consent_labels_use_only_approved_sensitive_answers():
    profile = {
        "sensitive_answers": {
            "legal_attestation": {
                "patterns": ["arbitration agreement", "i hereby certify", "true and correct"],
                "answer": "Yes",
                "approved": True,
            },
            "privacy_consent": {
                "patterns": ["personal data", "ai notetakers", "transcribe conversations"],
                "answer": "Yes",
                "approved": True,
            },
        }
    }

    assert python_runtime._match_sensitive("Applicant Arbitration Agreement Acknowledgement", profile) == "Yes"
    assert python_runtime._match_sensitive("I hereby certify that the answers given by me are true and correct", profile) == "Yes"
    assert python_runtime._match_sensitive("Palantir will process your personal data", profile) == "Yes"
    assert python_runtime._match_sensitive("AI notetakers to transcribe conversations", profile) == "Yes"


def test_ai_notetaker_no_takes_priority_over_general_privacy_yes():
    profile = {
        "sensitive_answers": {
            "privacy_consent": {
                "patterns": ["personal data", "ai notetakers", "transcribe conversations"],
                "answer": "Yes",
                "approved": True,
            },
            "ai_notetaker_consent": {
                "patterns": ["ai notetakers", "transcribe conversations"],
                "answer": "No",
                "approved": True,
            },
        }
    }

    assert python_runtime._match_sensitive("AI notetakers to transcribe conversations", profile) == "No"
    assert python_runtime._match_sensitive("We process your personal data", profile) == "Yes"


def test_sensitive_radio_no_answer_selects_no_option_and_reports_value():
    profile = {
        "sensitive_answers": {
            "clearance_eligibility": {
                "patterns": ["eligible to obtain the security clearance"],
                "answer": "No",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you eligible to obtain the security clearance specified in the job description?",
        "name": "clearance",
        "options": [
            {"id": "clearance_yes", "value": "yes", "label": "Yes", "autofillId": "1"},
            {"id": "clearance_no", "value": "no", "label": "No", "autofillId": "2"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "check"
    assert plan["option"]["label"] == "No"
    assert python_runtime._readback_status("selected: No") == "selected: No"


def test_citizenship_and_ts_sci_no_do_not_check_affirmative_checkbox():
    profile = {
        "sensitive_answers": {
            "security_clearance_interest": {
                "patterns": ["roles that require top security clearance", "top security clearance"],
                "answer": "Yes",
                "approved": True,
            },
            "citizenship": {
                "patterns": ["citizen", "only us citizens will be considered"],
                "answer": "No",
                "approved": True,
            },
            "security_clearance_eligibility": {
                "patterns": ["apply for and maintain a ts/sci security clearance", "ts/sci security clearance"],
                "answer": "No",
                "approved": True,
            },
        }
    }

    citizenship = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Due to contractual requirements, only US Citizens will be considered for this position.",
        "id": "citizen",
        "name": "",
    }
    clearance = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I am willing and able to apply for and maintain a TS/SCI security clearance.",
        "id": "clearance",
        "name": "",
    }

    assert python_runtime._match_sensitive(citizenship["label"], profile) == "No"
    assert python_runtime._match_sensitive(clearance["label"], profile) == "No"
    assert python_runtime._plan_field(citizenship, profile, None)["action"] == "skip"
    assert python_runtime._plan_field(clearance, profile, None)["action"] == "skip"


def test_negative_non_required_sensitive_checkbox_is_not_blocking_review():
    profile = {
        "sensitive_answers": {
            "ai_notetaker_consent": {
                "patterns": ["ai notetaker", "transcribe conversations"],
                "answer": "No",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I consent to AI notetakers to transcribe conversations.",
        "required": False,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "skip"
    assert plan["blocking"] is False


def test_required_sensitive_checkbox_conflicting_with_no_blocks_submit():
    profile = {
        "sensitive_answers": {
            "ai_notetaker_consent": {
                "patterns": ["ai notetaker", "transcribe conversations"],
                "answer": "No",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I consent to AI notetakers to transcribe conversations.",
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "skip"
    assert plan["blocking"] is True


def test_notetaker_checkbox_negative_value_is_not_checked_when_approved_yes():
    profile = {
        "sensitive_answers": {
            "ai_notetaker_consent": {
                "patterns": ["ai notetaker", "transcribe conversations"],
                "answer": "Yes",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "As part of our interview process, we may use AI notetakers to transcribe conversations.",
        "value": "No, I do not consent",
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "skip"
    assert plan["blocking"] is True


def test_notetaker_checkbox_negative_value_is_checked_when_approved_no():
    profile = {
        "sensitive_answers": {
            "ai_notetaker_consent": {
                "patterns": ["ai notetaker", "transcribe conversations"],
                "answer": "No",
                "approved": True,
            }
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "As part of our interview process, we may use AI notetakers to transcribe conversations.",
        "value": "No, I do not consent",
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "check"


def test_job_scoped_open_questions_are_auto_answered_from_enriched_profile(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "0")
    profile = {
        "target_company": "Anthropic",
        "target_title": "Research Engineer",
        "skills": ["Python", "PyTorch", "RAG"],
        "answers": {
            "Why Anthropic?": "Anthropic matches my AI safety and model evaluation background."
        },
    }

    why_field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Why Anthropic?",
        "required": True,
    }
    office_field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you open to working in-person in one of our offices 25% of the time?",
        "options": [{"label": "Yes"}, {"label": "No"}],
        "required": True,
    }
    timeline_field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Do you have any deadlines or timeline considerations we should be aware of?",
        "required": True,
    }

    assert python_runtime._plan_field(why_field, profile, None)["value"].startswith("Anthropic matches")
    assert python_runtime._plan_field(office_field, profile, None)["action"] == "skip"
    assert python_runtime._plan_field(timeline_field, profile, None)["action"] == "skip"


def test_no_ai_question_blocks_auto_generated_written_answer():
    profile = {"application_requires_user_authored_answers": True}
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Why are you interested in this role?",
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "skip"
    assert "no AI assistance" in plan["reason"]


def test_ashby_company_excitement_question_uses_generated_motivation_answer():
    profile = {
        "target_company": "Replit",
        "answers": {
            "Why Replit?": "Replit matches my interest in practical developer tools and AI systems."
        },
    }
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What excites you about Replit?",
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {
        "action": "fill",
        "value": "Replit matches my interest in practical developer tools and AI systems.",
    }


def test_ashby_team_motivation_questions_reuse_company_motivation_with_team_context():
    profile = {
        "target_company": "Browserbase",
        "answers": {
            "Why Browserbase?": "Browserbase matches my interest in reliable applied AI products."
        },
    }
    agent_platform = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Why are you applying to the Agent Platform team?",
        "required": True,
    }
    distributed_systems = {
        **agent_platform,
        "label": "Why are you applying to the Distributed Systems team?",
    }

    agent_plan = python_runtime._plan_field(agent_platform, profile, None)
    distributed_plan = python_runtime._plan_field(distributed_systems, profile, None)

    assert agent_plan["action"] == "fill"
    assert "Browserbase matches" in agent_plan["value"]
    assert "LangChain multi-agent" in agent_plan["value"]
    assert distributed_plan["action"] == "fill"
    assert "Browserbase matches" in distributed_plan["value"]
    assert "Kubernetes, Kafka, MLflow" in distributed_plan["value"]


def test_ashby_role_interest_question_uses_generated_motivation_answer():
    profile = {
        "target_company": "Baseten",
        "answers": {
            "Why Baseten?": "Baseten matches my interest in practical ML infrastructure and developer platforms."
        },
    }
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What about Baseten and this role interests you?",
        "required": True,
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {
        "action": "fill",
        "value": "Baseten matches my interest in practical ML infrastructure and developer platforms.",
    }


def test_ashby_developer_facing_products_question_uses_profile_evidence():
    profile = {
        "projects": [
            {
                "name": "XClaw",
                "description": "AI agent orchestration platform with GitHub CLI, Notion API, and tool integrations.",
            }
        ],
        "skills": ["Python", "FastAPI", "REST APIs", "React.js"],
    }
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Have you built developer-facing products such as APIs, SDKs, or CLI tools?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "buttonclick"
    assert plan["option"]["label"] == "Yes"


def test_ashby_replit_foster_city_office_requirement_uses_relocation_approval():
    profile = {
        "sensitive_answers": {
            "relocation": {
                "patterns": ["relocation", "relocate", "willing to relocate"],
                "answer": "Yes",
                "approved": True,
            },
        },
        "screening_answer_rules": [
            {"patterns": ["Foster City", "3 days per week"], "answer": "Yes"}
        ],
    }
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Are you able to work from our Foster City, CA HQ 3 days per week?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "buttonclick"
    assert plan["option"]["label"] == "Yes"


def test_ashby_replit_relevant_professional_experience_selects_one_to_two_years():
    profile = {"years_experience": "1-2"}
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "How many years of relevant professional experience do you have?",
        "required": True,
        "options": [
            {"label": "1-2 years"},
            {"label": "3-5 years"},
            {"label": "6-8 years"},
            {"label": "8+"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "check"
    assert plan["option"]["label"] == "1-2 years"


def test_ashby_replit_relevant_professional_experience_clicks_by_text_when_options_missing():
    profile = {"years_experience": "1-2"}
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "How many years of relevant professional experience do you have?",
        "required": True,
        "options": [],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {
        "action": "buttonclick",
        "option": {"label": "1-2 years", "value": "1-2 years"},
    }


def test_palantir_relevant_post_college_experience_selects_numeric_lower_bound():
    profile = {"years_experience": "1-2"}
    field = {
        "kind": "single",
        "tag": "select",
        "type": "select-one",
        "label": "How many years of relevant, post college work experience do you have?",
        "required": True,
        "options": ["Select...", "0", "1", "2", "3", "4", "5"],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {"action": "select", "value": "1"}


def test_palantir_relevant_post_college_experience_selects_profile_number():
    profile = {"years_experience": "4"}
    field = {
        "kind": "single",
        "tag": "select",
        "type": "select-one",
        "label": "How many years of relevant, post college work experience do you have?",
        "required": True,
        "options": ["Select...", "0", "1", "2", "3", "4", "5"],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan == {"action": "select", "value": "4"}


def test_unrestricted_work_authorization_with_future_sponsorship_selects_no():
    profile = {
        "sensitive_answers": {
            "work_authorization_us": {
                "patterns": ["work authorization", "authorized to work in the united states"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship": {
                "patterns": ["sponsorship", "future require sponsorship"],
                "answer": "Yes",
                "approved": True,
            },
        },
    }
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Do you currently have unrestricted work authorization in the United States?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "buttonclick"
    assert plan["option"]["label"] == "No"


def test_ashby_education_and_country_radio_fields_use_structured_profile():
    profile = {
        "country": "United States",
        "preferred_programming_language": "Python",
        "education": [
            {
                "school": "Stevens Institute of Technology",
                "degree": "Master's",
                "field": "Computer Science",
                "end_date": "2026-05",
            }
        ],
    }

    assert python_runtime._map_text_value("Graduation Date or Anticipated Graduation Date", profile) == "May 2026"

    degree = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Degree",
        "name": "degree",
        "options": [
            {"id": "bs", "value": "on", "label": "Bachelor's Degree"},
            {"id": "ms", "value": "on", "label": "Master's Degree"},
        ],
    }
    language = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "What is your preferred programming language for your interviews?",
        "name": "language",
        "options": [
            {"id": "cpp", "value": "on", "label": "C++"},
            {"id": "py", "value": "on", "label": "Python"},
        ],
    }
    country = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "In which of the following employment eligible countries are you seeking to work, if hired?",
        "name": "country",
        "options": [
            {"id": "ca", "value": "on", "label": "Canada"},
            {"id": "us", "value": "on", "label": "United States"},
        ],
    }

    assert python_runtime._plan_field(degree, profile, None)["option"]["label"] == "Master's Degree"
    assert python_runtime._plan_field(language, profile, None)["option"]["label"] == "Python"
    assert python_runtime._plan_field(country, profile, None)["option"]["label"] == "United States"


def test_plan_field_maps_ashby_education_entries_to_their_own_profile_facts():
    profile = {
        "education": [
            {
                "school": "Stevens Institute of Technology",
                "degree": "Master's",
                "field": "Computer Science",
                "start_date": "2024-09",
                "end_date": "2026-05",
            },
            {
                "school": "Shenzhen University",
                "degree": "Bachelor's",
                "field": "Logistics Management",
                "start_date": "2019-09",
                "end_date": "2023-07",
            },
        ]
    }
    school_one = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Education History",
        "id": "",
        "section": "education",
        "role": "combobox",
        "placeholder": "Search schools...",
        "required": True,
        "ashbyEduEntryIndex": 0,
        "ashbyEduSubfield": "school",
        "options": [],
    }
    school_two = {**school_one, "ashbyEduEntryIndex": 1}

    assert python_runtime._plan_field(school_one, profile, None) == {
        "action": "combobox",
        "value": "Stevens Institute of Technology",
    }
    assert python_runtime._plan_field(school_two, profile, None) == {
        "action": "combobox",
        "value": "Shenzhen University",
    }

    degree_two = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Degree",
        "id": "_systemfield_education_history-degree",
        "section": "education",
        "ashbyEduEntryIndex": 1,
        "ashbyEduSubfield": "degree",
    }
    assert python_runtime._plan_field(degree_two, profile, None) == {
        "action": "fill",
        "value": "Bachelor's",
    }

    end_year_two = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "End Date",
        "id": "_systemfield_education_history-end-year",
        "section": "education",
        "ashbyEduEntryIndex": 1,
        "ashbyEduSubfield": "end_year",
        "required": True,
        "options": ["2023", "2024", "2025", "2026"],
    }
    assert python_runtime._plan_field(end_year_two, profile, None) == {
        "action": "select",
        "value": "2023",
    }

    end_month_two = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "End Date",
        "id": "_systemfield_education_history-end-month",
        "section": "education",
        "ashbyEduEntryIndex": 1,
        "ashbyEduSubfield": "end_month",
        "required": True,
        "options": ["January", "February", "March", "April", "May", "June", "July"],
    }
    assert python_runtime._plan_field(end_month_two, profile, None) == {
        "action": "select",
        "value": "July",
    }

    still_student_two = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Still Student?",
        "id": "_systemfield_education_history-still-student",
        "section": "education",
        "ashbyEduEntryIndex": 1,
        "ashbyEduSubfield": "still_student",
    }
    assert python_runtime._plan_field(still_student_two, profile, None) == {
        "action": "uncheck",
    }

    ongoing = {
        "education": [
            {
                "school": "Stevens Institute of Technology",
                "degree": "Master's",
                "field": "Computer Science",
                "start_date": "2024-09",
                "current": True,
            }
        ]
    }
    assert python_runtime._plan_field(
        {**still_student_two, "ashbyEduEntryIndex": 0}, ongoing, None
    ) == {"action": "check"}
    assert python_runtime._is_school_combobox_field(
        {**school_one, "label": "Education History"}
    ) is True


def test_sponsorship_type_selects_opt_for_ashby_option_style_question():
    profile = {
        "sensitive_answers": {
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
    }
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Will you now or at any time in the future require sponsorship for employment visa status (e.g. H1B, OPT)?",
        "name": "sponsorship",
        "options": [
            {"id": "opt", "value": "on", "label": "OPT"},
            {"id": "h1b", "value": "on", "label": "H1B"},
            {"id": "none", "value": "on", "label": "None"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "check"
    assert plan["option"]["label"] == "OPT"


def test_sponsorship_question_is_not_confused_with_clearance_eligibility():
    profile = {
        "sensitive_answers": {
            "security_clearance_eligibility": {
                "patterns": [
                    "willing and able to apply for and maintain",
                    "apply for and maintain a ts/sci security clearance",
                ],
                "answer": "No",
                "approved": True,
            },
            "sponsorship": {
                "patterns": [
                    "sponsorship",
                    "require sponsorship",
                    "visa sponsorship",
                    "employment visa status",
                    "future require sponsorship",
                ],
                "answer": "Yes",
                "approved": True,
            },
        }
    }
    field = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": (
            "Will you now or will you in the future require employment visa sponsorship "
            "to work in the country in which the job you're applying for is located?*"
        ),
        "options": ["Select...", "Yes", "No"],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "select"
    assert plan["value"] == "Yes"


def test_waymark_sponsorship_textarea_uses_approved_yes_not_citizenship_no():
    profile = {
        "sensitive_answers": {
            "sponsorship": {
                "patterns": ["sponsorship", "future require sponsorship"],
                "answer": "Yes",
                "approved": True,
            },
            "citizenship": {
                "patterns": ["u s citizen", "u.s. citizen"],
                "answer": "No",
                "approved": True,
            },
        }
    }
    field = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Will you now or in the future require employer sponsorship to work in the U.S.? *",
        "required": True,
        "value": "",
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "fill", "value": "Yes"}


def test_ashby_common_screening_fields_are_auto_answered():
    profile = {
        "target_location": "San Francisco, California",
        "pronouns": "He/Him",
        "answers": {
            "How did you hear about us?": "Company website",
            "Are you open to working in-person in one of our offices 25% of the time?": "Yes",
        },
        "screening_answer_rules": [
            {"patterns": ["Anchor Days"], "answer": "Yes"}
        ],
        "work_history": [
            {"title": "Research Assistant", "employment_type": "Internship"},
            {"title": "AI/ML Engineer Intern", "employment_type": "Internship"},
        ],
        "education": [{"degree": "Master's"}],
    }

    heard = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "How did you hear about this opportunity? (select all that apply)",
        "options": [{"label": "Company website"}, {"label": "LinkedIn"}],
    }
    degree = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "Degree Type",
        "options": [{"label": "Bachelor's Degree"}, {"label": "Master's Degree"}],
    }
    office = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Are you able to commit to working from one of our offices on Anchor Days each week?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    internships = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "How many prior internships have you had?",
        "options": [{"label": "0"}, {"label": "1"}, {"label": "2"}, {"label": "3+"}],
    }
    pronouns = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "What pronouns would you like our team to use when addressing you?",
        "options": [{"label": "He/Him"}, {"label": "Prefer not to say"}],
    }

    assert python_runtime._plan_field(heard, profile, None)["options"][0]["label"] == "Company website"
    assert python_runtime._plan_field(degree, profile, None)["options"][0]["label"] == "Master's Degree"
    office_plan = python_runtime._plan_field(office, profile, None)
    assert office_plan["action"] == "buttonclick"
    assert office_plan["option"]["label"] == "Yes"
    assert python_runtime._plan_field(internships, profile, None)["option"]["label"] == "2"
    assert python_runtime._plan_field(pronouns, profile, None)["option"]["label"] == "He/Him"


def test_ashby_listed_location_hybrid_field_uses_relocation_answer():
    profile = {
        "location": "Jersey City, NJ, USA",
        "target_location": "New York, NY",
        "desired_locations": ["New York City"],
        "answers": {
            "Are you open to relocation?": "Yes",
        },
        "screening_answer_rules": [
            {
                "patterns": ["currently based in the listed location"],
                "answer": "No, I’m not based in this location but willing to relocate",
            }
        ],
    }
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": (
            "This role is tied to the office location listed in the job posting. "
            "Team members are expected to work from the office 3 days per week as part of Harvey’s "
            "hybrid work model. Are you currently based in the listed location and able to work "
            "in person 3 days per week?"
        ),
        "required": True,
        "options": [
            {"label": "Yes, I’m based in this location and able to work from the office 3 days per week"},
            {"label": "No, I’m not based in this location but willing to relocate"},
            {"label": "No, I’m only able to work remotely"},
            {"label": "Other (optional context)"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "check"
    assert plan["option"]["label"] == "No, I’m not based in this location but willing to relocate"


def test_ashby_confido_relocation_chooses_relocation_not_nyc_claim():
    profile = {
        "location": "Jersey City, NJ, USA",
        "answers": {"Are you open to relocation?": "Yes"},
        "sensitive_answers": {
            "relocation": {"answer": "Yes", "approved": True},
        },
    }
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "We work 5 days on-site in NYC. If you're not local, are you willing to relocate?",
        "required": True,
        "options": [
            {"label": "I am in NYC and happy to work in office"},
            {"label": "I will relocate and am happy to work in office"},
            {"label": "I do not want to work in office"},
        ],
    }

    assert not python_runtime._profile_location_matches_question(
        field["label"],
        "I am in NYC and happy to work in office",
        profile,
    )
    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "buttonclick"
    assert plan["option"]["label"] == "I will relocate and am happy to work in office"


def test_ashby_realm_sponsorship_chooses_new_sponsorship_option():
    profile = {
        "sensitive_answers": {
            "sponsorship": {"answer": "Yes", "approved": True},
            "sponsorship_type": {"answer": "OPT", "approved": True},
        },
    }
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Do you now or in the future require sponsorship for employment visa status to work in the United States?",
        "required": True,
        "options": [
            {"label": "No, I do not require sponsorship now or in the future"},
            {"label": "No, I do not require sponsorship now, but may require it in the future"},
            {"label": "Yes, I currently hold a visa status that would require a transfer to a new employer"},
            {"label": "Yes, I would require new sponsorship"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "buttonclick"
    assert plan["option"]["label"] == "Yes, I would require new sponsorship"
    assert python_runtime._matching_options(field, "No", profile) == []
    assert "None" in python_runtime._answer_aliases("N/A - have never held U.S. security clearance")


def test_dynamic_combobox_fallback_chooses_new_sponsorship():
    profile = {
        "sensitive_answers": {
            "sponsorship": {"answer": "Yes", "approved": True},
            "sponsorship_type": {"answer": "OPT", "approved": True},
        },
    }
    field = {
        "role": "combobox",
        "label": "Do you now or in the future require sponsorship for employment visa status to work in the United States?",
    }
    available = [
        "No, I do not require sponsorship now or in the future",
        "No, I do not require sponsorship now, but may require it in the future",
        "Yes, I currently hold a visa status that would require a transfer to a new employer",
        "Yes, I would require new sponsorship",
    ]

    assert (
        python_runtime._dynamic_combobox_fallback_choice(field, available, "Yes", profile)
        == "Yes, I would require new sponsorship"
    )


def test_location_checkbox_group_selects_approved_relocation_offices():
    profile = {
        "answers": {
            "Select all locations you would be open to being placed": (
                "New York, NY; San Francisco, CA; Seattle, WA; Los Angeles, CA; Sunnyvale, CA"
            )
        }
    }
    field = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "What office(s) would you be willing to relocate to? (Select all that apply)",
        "required": True,
        "options": [
            "San Diego, California",
            "Dallas, Texas",
            "Washington, DC",
            "Boston, MA",
            "International",
            "San Francisco, California",
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "checkmany"
    assert [python_runtime._option_text(option) for option in plan["options"]] == [
        "San Francisco, California"
    ]


def test_current_security_clearance_level_uses_none():
    profile = {
        "sensitive_answers": {
            "active_security_clearance": {"answer": "No", "approved": True},
        },
    }
    field = {
        "role": "combobox",
        "label": "What is your current security clearance level?*",
        "required": True,
        "options": [
            {"label": "None"},
            {"label": "Public Trust"},
            {"label": "Secret"},
            {"label": "Top Secret"},
            {"label": "TS/SCI"},
        ],
    }

    plan = python_runtime._plan_field(field, profile, None)

    assert plan["action"] == "combobox"
    assert plan["value"] == "None"


def test_english_level_and_bachelor_year_use_profile_answers():
    profile = {
        "answers": {"What is your English level?": "Fluent"},
        "education": [{"degree": "Bachelor's", "end_year": "2023"}],
    }
    english = {
        "role": "combobox",
        "label": "What is your English level?*",
        "required": True,
        "options": [{"label": "A1"}, {"label": "A2"}, {"label": "B1"}, {"label": "B2"}, {"label": "C1"}],
    }
    bachelor_year = {
        "role": "combobox",
        "label": "What year did you graduate with a Bachelor's degree?*",
        "required": True,
        "options": [
            {"label": "Earlier than 2020"},
            {"label": "2020"},
            {"label": "2021"},
            {"label": "2022"},
            {"label": "2023"},
            {"label": "Not yet graduated"},
        ],
    }

    assert python_runtime._plan_field(english, profile, None)["value"] == "C1"
    assert python_runtime._plan_field(bachelor_year, profile, None)["value"] == "2023"


def test_fireworks_hybrid_location_and_compensation_fields_are_auto_answered():
    profile = {
        "desired_locations": ["New York City"],
        "minimum_expected_salary": "At least $70k USD",
        "answers": {
            "Are you open to working in-person in one of our offices 25% of the time?": "Yes",
            "Where would you like to work?": "New York City",
        },
    }
    hybrid = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Are you open to a hybrid schedule with in-office days on Monday, Wednesday, and Friday?*",
        "options": ["Yes", "No"],
        "required": True,
    }
    hybrid_policy = {
        **hybrid,
        "label": "Are you willing and able to commit to the hybrid policy if hired?*",
    }
    new_york = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "New York, NY",
        "required": True,
    }
    remote = {
        **new_york,
        "label": "Remote, US",
    }
    redwood = {
        **new_york,
        "label": "Redwood City, CA",
    }
    compensation = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Do you have any initial compensation expectations? *",
        "required": True,
    }

    assert python_runtime._plan_field(hybrid, profile, None) == {"action": "select", "value": "Yes"}
    assert python_runtime._plan_field(hybrid_policy, profile, None) == {"action": "select", "value": "Yes"}
    assert python_runtime._plan_field(new_york, profile, None) == {"action": "check"}
    assert python_runtime._plan_field(remote, profile, None) == {"action": "check"}
    assert python_runtime._plan_field(redwood, profile, None) == {
        "action": "skip",
        "reason": "office location option not selected from candidate preferences",
        "blocking": False,
    }
    assert python_runtime._plan_field(compensation, profile, None) == {
        "action": "fill",
        "value": "At least $70k USD",
    }


def test_native_number_inputs_receive_numeric_salary_and_percentage_values():
    salary_field = {
        "kind": "single",
        "tag": "input",
        "type": "number",
        "label": "Salary Expectation",
    }
    coding_field = {
        "kind": "single",
        "tag": "input",
        "type": "number",
        "label": "What percentage of time do you generally enjoy spending coding?",
    }

    assert (
        python_runtime._normalize_number_input_value(
            "At least $70k USD",
            salary_field,
            input_type="number",
        )
        == "70000"
    )
    assert (
        python_runtime._normalize_number_input_value(
            "I generally enjoy spending around 70% of my time coding.",
            coding_field,
            input_type="number",
        )
        == "70"
    )
    assert (
        python_runtime._normalize_number_input_value(
            "At least $70k USD",
            salary_field,
            input_type="text",
        )
        == "At least $70k USD"
    )


def test_native_number_experience_input_uses_conservative_range_bound():
    experience_field = {
        "kind": "single",
        "tag": "input",
        "type": "number",
        "label": "How many years of fulltime experience do you have owning projects end-to-end?",
    }

    assert (
        python_runtime._normalize_number_input_value(
            "3-5",
            experience_field,
            input_type="number",
        )
        == "3"
    )


def test_native_number_input_never_writes_nan_for_non_numeric_answer():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "number",
        "label": "What was (or currently is) your PPA?",
    }

    assert (
        python_runtime._normalize_number_input_value(
            "I do not have a PPA.",
            field,
            input_type="number",
        )
        == ""
    )
    assert (
        python_runtime._normalize_number_input_value(
            "10",
            field,
            input_type="number",
        )
        == "10"
    )


def test_netic_technical_domain_prefers_infrastructure_option():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "What technical domain do you prefer to work in and have most expertise with?",
        "required": True,
        "options": [
            "Front End",
            "Full Stack",
            "Back End",
            "Infrastructure",
            "Database",
            "Operations / SRE",
            "Low-Level Systems Development",
            "Distributed Systems",
        ],
    }

    assert python_runtime._plan_field(field, {}, None) == {
        "action": "combobox",
        "value": "Infrastructure",
    }


def test_nuro_hybrid_four_day_office_requirement_uses_relocation_evidence():
    profile = {
        "answers": {
            "Are you open to relocation?": "Yes",
            "Are you open to working in-person in one of our offices 25% of the time?": "Yes",
            "This role requires that you are willing to relocate to San Francisco, CA, USA. Please confirm that you are willing to relocate for this role?": "Yes",
        },
        "sensitive_answers": {
            "relocation": {
                "patterns": ["relocation", "relocate", "willing to relocate"],
                "answer": "Yes",
                "approved": True,
            }
        },
        "screening_answer_rules": [
            {"patterns": ["requires 4 days a week in office"], "answer": "Yes"},
        ],
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": (
            "This position is hybrid and requires 4 days a week in office, including Thursdays in our "
            "Mountain View, CA headquarters and the remaining 2 days in either Mountain View or our "
            "San Francisco, CA office. Are you able to meet this requirement?*"
        ),
        "required": True,
        "options": ["Yes", "No"],
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "combobox", "value": "Yes"}


def test_required_audit_accepts_checked_office_location_children():
    review = []

    python_runtime._append_required_audit(
        review,
        [
            {
                "label": "Which office location(s) are you interested in? *",
                "reason": "browser reports field as invalid",
            }
        ],
        filled=[
            {
                "label": "New York, NY",
                "action": "check",
                "readback": "checked",
            }
        ],
    )

    assert review == []


def test_glean_agent_screening_fields_are_auto_answered():
    profile = {
        "skills": ["Python", "LangChain", "RAG", "AI agents"],
        "projects": [
            {
                "name": "XClaw",
                "description": "AI agent orchestration desktop platform with execution skills.",
            }
        ],
        "answers": {},
    }
    connection = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Do you know anyone currently at Glean?*",
        "required": True,
    }
    agents = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Have you built AI agents? If so, please elaborate. *",
        "required": True,
    }
    tools = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What AI tools are you currently using today and how are you using them?*",
        "required": True,
    }

    assert python_runtime._plan_field(connection, profile, None) == {"action": "fill", "value": "No"}
    agent_plan = python_runtime._plan_field(agents, profile, None)
    assert agent_plan["action"] == "combobox"
    assert "XClaw" in agent_plan["value"]
    tools_plan = python_runtime._plan_field(tools, profile, None)
    assert tools_plan["action"] == "fill"
    assert "LangChain" in tools_plan["value"]


def test_start_availability_question_uses_existing_when_can_you_start_answer():
    profile = {
        "answers": {
            "When can you start?": "Within a month",
        }
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "What is the soonest date you would be available to start?*",
        "required": True,
        "value": "",
    }

    assert python_runtime._plan_field(field, profile, None) == {"action": "fill", "value": "Within a month"}


def test_pronouns_question_falls_back_to_privacy_preserving_option():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "What pronouns would you like our team to use when addressing you?",
        "options": [{"label": "He/Him"}, {"label": "Prefer not to say"}],
    }

    plan = python_runtime._plan_field(field, {"answers": {}}, None)

    assert plan["action"] == "check"
    assert plan["option"]["label"] == "Prefer not to say"


def test_text_message_application_consent_is_auto_answered_no():
    field = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Do you give your consent to have Quadric text you about your application?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    plan = python_runtime._plan_field(field, {}, None)

    assert plan["action"] == "skip"


def test_common_location_and_work_authorization_screening_fields_are_auto_answered():
    profile = {
        "location": "Jersey City, NJ, USA",
        "sensitive_answers": {
            "relocation": {
                "patterns": ["relocation", "relocate", "willing to relocate"],
                "answer": "Yes",
                "approved": True,
            },
            "work_authorization_current_country": {
                "patterns": ["right to work", "work in the country"],
                "answer": "Yes",
                "approved": True,
            },
            "sponsorship_type": {
                "patterns": ["sponsorship", "employment authorization"],
                "answer": "OPT",
                "approved": True,
            },
        },
    }
    relocate = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Do you live in the San Francisco, Bay Area, or would you be willing to relocate here?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    right_to_work = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Do you have unrestricted right to work in the country in which this position is based?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    sponsorship_yes_no = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Will you now or in the future require sponsorship for employment authorization?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    currently_bay_area = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you currently based in the San Francisco/Bay Area?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    assert python_runtime._plan_field(relocate, profile, None)["option"]["label"] == "Yes"
    assert python_runtime._plan_field(right_to_work, profile, None)["option"]["label"] == "Yes"
    assert python_runtime._plan_field(sponsorship_yes_no, profile, None)["option"]["label"] == "Yes"
    assert python_runtime._plan_field(currently_bay_area, profile, None)["option"]["label"] == "No"


def test_workday_identity_state_and_previous_employer_fields_are_auto_answered(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_LLM_ANSWERS", "0")
    profile = {
        "phone": "+1(201)-283-4980",
        "state": "NJ",
        "region": "New Jersey",
        "target_company": "Warner Bros. Discovery",
        "application_source": "Warner Bros. Discovery Careers Website",
    }
    state = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "label": "State*",
        "id": "address--countryRegion",
        "name": "countryRegion",
        "ariaLabel": "State Select One Required",
        "required": False,
        "value": "Select One",
    }
    previous_worker = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you currently employed or have you been employed at Warner Bros. Discovery or any affiliate of Warner Bros. Discovery?*",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    preferred_name = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "I have a preferred name",
        "required": False,
    }
    phone_number = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Phone Number*",
        "required": True,
    }
    source = {
        "kind": "single",
        "tag": "input",
        "type": "input",
        "label": "How Did You Hear About Us?*",
        "required": True,
    }
    phone_type = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "label": "Phone Device Type*",
        "name": "phoneType",
        "value": "Select One",
    }
    fox_previous_worker = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Have you ever been employed by FOX Corporation or any of its subsidiaries?*",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    salary_range = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "label": "What is your desired annual base salary range:*Select One",
        "value": "Select One",
    }
    highest_education = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "label": "Please select the highest level of education completed*",
        "value": "Select One",
    }
    fox_profile = {**profile, "target_company": "FOX Corporation"}
    fox_profile["minimum_expected_salary"] = "At least $70k USD"

    assert python_runtime._map_text_value(state, profile) == "New Jersey"
    assert python_runtime._plan_field(state, profile, None)["value"] == "New Jersey"
    assert python_runtime._plan_field(previous_worker, profile, None)["action"] == "skip"
    preferred_plan = python_runtime._plan_field(preferred_name, profile, None)
    assert preferred_plan["action"] == "skip"
    assert preferred_plan["blocking"] is False
    assert python_runtime._plan_field(phone_number, profile, None)["value"] == "2012834980"
    assert python_runtime._plan_field(source, profile, None)["value"] == "Warner Bros. Discovery Careers Website"
    assert python_runtime._plan_field({**source, "value": "FOXCareers.com"}, fox_profile, None) == {
        "action": "skip",
        "reason": "field already selected",
    }
    assert python_runtime._option_matches("Warner Bros. Discovery Careers Website", "Careers Website")
    assert python_runtime._plan_field(phone_type, fox_profile, None)["value"] == "Mobile"
    assert python_runtime._plan_field(fox_previous_worker, fox_profile, None)["action"] == "skip"
    assert python_runtime._plan_field(salary_range, fox_profile, None)["value"] == "75,001 to 100,000"
    assert python_runtime._plan_field(highest_education, fox_profile, None)["value"] == "Master's Degree"
    assert python_runtime._option_matches("Master Degree", "Master's Degree")
    assert python_runtime._option_matches("Asian (Not Hispanic or Latino)", "East Asian")


def test_workday_official_careers_source_maps_company_website_to_company_job_board():
    profile = {
        "target_company": "Siemens Healthineers",
        "job_source": "siemens-healthineers:official-careers",
        "job_source_url": "https://careers.siemens-healthineers.com/global/en/search-results?keywords=Data%20Engineer",
        "answers": {"How did you hear about us?": "Company website"},
    }
    source = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "How Did You Hear About Us?*",
        "required": True,
        "value": "",
    }
    top_level_options = {
        "label": "How Did You Hear About Us?*",
        "options": [{"label": "Agency"}, {"label": "Job Board"}, {"label": "Other"}],
    }

    assert python_runtime._auto_answer(source["label"], profile) == "Job Board > Siemens Healthineers Job Board"
    assert python_runtime._plan_field(source, profile, None) == {
        "action": "combobox",
        "value": "Job Board > Siemens Healthineers Job Board",
    }
    assert python_runtime._matching_options(top_level_options, "Job Board > Siemens Healthineers Job Board")[0]["label"] == "Job Board"


def test_workday_source_combobox_prefers_career_website_over_saved_linkedin():
    profile = {
        "target_company": "WellSky",
        "job_source": "workday",
        "job_source_url": "https://wellsky.wd1.myworkdayjobs.com/en-US/WellSkyCareers/job/Associate-Software-Engineer---Onsite_JR4907",
        "answers": {"How did you hear about us?": "LinkedIn", "Degree": "Master's Degree"},
    }
    source = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "How Did You Hear About Us?*",
        "required": True,
        "options": [
            {"label": "Campus Campaign"},
            {"label": "Career Website"},
            {"label": "Job Board"},
        ],
    }

    assert python_runtime._auto_answer(source["label"], profile) == "Career Website"
    assert python_runtime._option_matches("Corporate Website", "Career Website")
    assert python_runtime._option_matches("Career Site", "Career Website")
    assert python_runtime._plan_field(source, profile, None) == {
        "action": "combobox",
        "value": "Career Website",
    }


def test_currently_based_country_list_maps_profile_location_to_country():
    profile = {"location": "New York, NY"}
    field = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "role": "combobox",
        "label": "Are you currently based in any of these countries? Please note these are the only countries where we are accepting applications*",
        "required": True,
        "options": [
            {"label": "United States"},
            {"label": "Germany"},
            {"label": "United Kingdom"},
            {"label": "Other"},
        ],
    }

    assert python_runtime._auto_answer(field["label"], profile) == "United States"
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "United States",
    }


def test_workday_source_combobox_skips_already_selected_careers_webpage():
    profile = {
        "target_company": "Hewlett Packard Enterprise",
        "job_source": "workday",
        "job_source_url": "https://hpe.wd5.myworkdayjobs.com/en-US/Jobsathpe/job/Software-Engineer-I_1202393-2",
        "answers": {"How did you hear about us?": "LinkedIn"},
    }
    source = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "How Did You Hear About Us?*",
        "required": True,
        "value": "1 item selected, HPE Careers webpage HPE Careers webpage",
    }

    assert python_runtime._auto_answer(source["label"], profile) == "Career Website"
    assert python_runtime._option_matches(source["value"], "Career Website")
    assert python_runtime._plan_field(source, profile, None) == {
        "action": "skip",
        "reason": "combobox already selected",
    }


def test_workday_phone_device_type_uses_primary_option():
    profile = {
        "_application_url": "https://wellsky.wd1.myworkdayjobs.com/en-US/WellSkyCareers/job/JR4907/apply",
        "phone_type": "Mobile",
    }
    field = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "role": "combobox",
        "label": "Phone Device Type*",
        "automationId": "phone-device-type",
        "required": True,
        "options": [{"label": "Select One"}, {"label": "Primary"}],
        "value": "Select One",
    }

    assert python_runtime._map_text_value(field, profile) == "Primary"
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Primary",
    }
    assert python_runtime._option_matches("Cell", "Primary")


def test_optional_workday_identity_fields_do_not_fall_back_to_full_name():
    profile = {
        "_application_url": "https://ms.wd5.myworkdayjobs.com/en-US/External/job/JR/apply",
        "name": "Gaoyi Wu",
        "address_line1": "132 New York Avenue",
        "city": "Jersey City",
        "postal_code": "07307",
    }

    for label in ["Middle Name", "Suffix", "Address Line 2", "County"]:
        field = {"kind": "single", "tag": "input", "type": "text", "label": label, "required": False}
        assert python_runtime._map_text_value(field, profile) is None
        assert python_runtime._plan_field(field, profile, None)["action"] == "skip"


def test_morgan_stanley_voluntary_disclosure_dropdowns_use_demographics():
    profile = {
        "_application_url": "https://ms.wd5.myworkdayjobs.com/en-US/External/job/JR025652/apply",
        "demographics": {"gender": "Male", "veteran": "No"},
        "answers": {},
        "sensitive_answers": {},
    }
    gender = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "label": "Please select your gender:*",
        "required": True,
        "value": "Select One",
    }
    veteran = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "label": "Please confirm your veteran status:*",
        "required": True,
        "value": "Select One",
    }

    assert python_runtime._plan_field(gender, profile, None) == {
        "action": "customselect",
        "value": "Man",
    }
    assert python_runtime._plan_field(veteran, profile, None) == {
        "action": "customselect",
        "value": "No",
    }


def test_workday_field_of_study_uses_combobox_selection():
    profile = {
        "_application_url": "https://wellsky.wd1.myworkdayjobs.com/en-US/WellSkyCareers/job/JR4907/apply",
        "education": [{"field": "Computer Science"}],
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Field of Study",
        "required": False,
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Computer Science",
    }


def test_relative_at_company_question_uses_approved_profile_rule():
    field = {
        "kind": "single",
        "tag": "button",
        "type": "button",
        "role": "combobox",
        "label": "Do you have any relatives that currently work for WellSky?* Select One",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }

    profile = {
        "answers": {},
        "screening_answer_rules": [
            {"patterns": ["relatives that currently work"], "answer": "No"},
        ],
    }

    assert python_runtime._auto_answer(field["label"], profile) == "No"
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "No",
    }


def test_greenhouse_location_combobox_uses_full_location_to_disambiguate_city():
    profile = {
        "location": "Jersey City, NJ, USA",
        "city": "Jersey City",
        "answers": {},
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Location (City)*",
        "required": True,
        "value": "",
    }

    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Jersey City, NJ, USA",
    }
    assert python_runtime._option_match_score(
        "Jersey City, New Jersey, United States",
        "Jersey City, NJ, USA",
    ) > python_runtime._option_match_score(
        "Jersey City, Wisconsin, United States",
        "Jersey City, NJ, USA",
    )


def test_lever_new_grad_common_fields_are_auto_answered():
    profile = {
        "education": [{"end_date": "2026-05"}],
        "answers": {"What type of roles are you looking for?": "Full-Time"},
    }
    graduation = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "When will you graduate? (month & year)",
        "required": True,
    }
    role_type = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Are you looking for a full-time or internship job?",
        "options": [{"label": "Full-time"}, {"label": "Internship"}, {"label": "Both"}],
    }
    other_countries = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "If there are no suitable positions in U.S, are you open to positions in other countries?",
        "options": [{"label": "Guangzhou, China"}, {"label": "I'm not open to other locations"}],
    }
    marketing = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "name": "consent[marketing]",
        "autofillId": "7",
    }

    assert python_runtime._plan_field(graduation, profile, None)["value"] == "May 2026"
    assert python_runtime._plan_field(role_type, profile, None)["option"]["label"] == "Full-time"
    other_countries_plan = python_runtime._plan_field(other_countries, profile, None)
    assert other_countries_plan["action"] == "checkmany"
    assert other_countries_plan["options"][0]["label"] == "I'm not open to other locations"
    assert python_runtime._selector_for(marketing) == '[data-job-agent-autofill-index="7"]'


def test_relative_availability_is_normalized_for_date_picker_only():
    today = python_runtime.date(2026, 7, 14)

    assert python_runtime._normalize_date_input_value(
        "Within a month", "Pick date...", today=today
    ) == "08/14/2026"
    assert python_runtime._normalize_date_input_value(
        "Within two weeks", "Pick date...", today=today
    ) == "07/28/2026"
    assert python_runtime._normalize_date_input_value(
        "Within a month", "Type here...", today=today
    ) == "Within a month"


def test_relative_availability_uses_iso_format_for_native_date_input():
    today = python_runtime.date(2026, 7, 14)

    assert python_runtime._normalize_date_input_value(
        "Within a month", input_type="date", today=today
    ) == "2026-08-14"
    assert python_runtime._normalize_date_input_value(
        "2026-07-28", input_type="date", today=today
    ) == "2026-07-28"


def test_breezy_company_site_and_degree_answers_use_truthful_fallbacks():
    profile = {
        "name": "Gaoyi Wu",
        "address_line1": "132 New York Avenue",
        "answers": {"How did you hear about us?": "Company website"},
        "education": [
            {"degree": "Master's", "field": "Computer Science"},
            {"degree": "Bachelor's", "field": "Logistics Management"},
        ],
        "demographics": {"disability": "No"},
        "sensitive_answers": {
            "sponsorship": {
                "approved": True,
                "answer": "Yes",
                "patterns": ["sponsorship", "require sponsorship"],
            }
        },
    }
    sponsorship = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Do you now or will you require sponsorship for employment (e.g. H-1B visa status)?",
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    bachelor = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Which field is your Bachelor's degree in?",
        "options": ["Accounting", "Engineering", "Other"],
    }
    master = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "If applicable, which field is your Master's degree in (or will be in)?",
        "options": ["Accounting", "Engineering", "Other", "N/A"],
    }
    source = {
        "kind": "checkboxgroup",
        "type": "checkbox",
        "label": "How did you hear about this position?",
        "options": [
            {"label": "LinkedIn"},
            {"label": "Other job board site (Monster, Indeed, etc.)"},
            {"label": "Other"},
        ],
    }
    disability = {
        "kind": "radiogroup",
        "type": "radio",
        "label": "Disability status",
        "options": [
            {"label": "YES, I HAVE A DISABILITY (or previously had a disability)"},
            {"label": "NO, I DON'T HAVE A DISABILITY"},
            {"label": "I do not want to answer"},
        ],
    }

    assert python_runtime._map_text_value("全名 cName", profile) == "Gaoyi Wu"
    assert python_runtime._map_text_value("地址 cAddress", profile) == "132 New York Avenue"
    assert python_runtime._plan_field(sponsorship, profile, None)["option"]["label"] == "Yes"
    assert python_runtime._plan_field(bachelor, profile, None) == {"action": "select", "value": "Other"}
    assert python_runtime._plan_field(master, profile, None) == {"action": "select", "value": "Other"}
    assert python_runtime._plan_field(source, profile, None)["options"][0]["label"] == "Other"
    assert python_runtime._plan_field(disability, profile, None)["option"]["label"] == "NO, I DON'T HAVE A DISABILITY"


def test_access_control_experience_combobox_uses_profile_evidence():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Do you have experience implementing access control models (like OAuth)*",
        "required": True,
        "options": ["Yes", "No"],
    }

    no_evidence_profile = {
        "skills": ["Python", "FastAPI"],
        "projects": [{"description": "Built a backend API service."}],
    }
    evidence_profile = {
        "skills": ["Python", "OAuth", "RBAC"],
        "projects": [{"description": "Implemented authorization middleware for API access control."}],
    }

    assert python_runtime._plan_field(field, no_evidence_profile, None) == {
        "action": "combobox",
        "value": "No",
    }
    assert python_runtime._plan_field(field, evidence_profile, None) == {
        "action": "combobox",
        "value": "Yes",
    }


def test_greenhouse_source_question_defaults_to_company_website_without_saved_answer():
    profile = {
        "target_company": "Vercel",
        "job_source": "greenhouse:vercel",
        "job_source_url": "https://job-boards.greenhouse.io/vercel/jobs/5430088004",
        "answers": {},
    }
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Where did you first hear about this role?*",
        "required": True,
        "options": ["Company Website", "LinkedIn", "Other"],
    }

    assert python_runtime._auto_answer(field["label"], profile) == "Company website"
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "Company Website",
    }


def test_renaissance_referral_and_race_comboboxes_use_approved_facts():
    profile = {
        "answers": {},
        "screening_answer_rules": [
            {"patterns": ["referred by a current employee"], "answer": "No"},
        ],
        "demographics": {"race": "East Asian", "ethnicity": "East Asian"},
    }
    referral = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Were you referred by a current employee of the company?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    race = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Please identify your race",
        "required": True,
        "options": [
            "American Indian or Alaskan Native",
            "Asian",
            "Black or African American",
            "White",
            "Native Hawaiian or Other Pacific Islander",
            "Two or More Races",
            "Decline To Self Identify",
        ],
    }

    assert python_runtime._plan_field(referral, profile, None) == {"action": "combobox", "value": "No"}
    assert python_runtime._plan_field(race, profile, None) == {"action": "combobox", "value": "Asian"}


def test_brex_referral_relocation_and_capital_one_fields_use_approved_facts():
    profile = {
        "location": "Jersey City, NJ, USA",
        "answers": {
            "How did you hear about us?": "LinkedIn",
            "Are you open to relocation?": "Yes",
            "If you currently work, or have previously worked, at Capital One or a company acquired by Capital One, please provide your Employee ID (EID).": "N/A",
        },
        "sensitive_answers": {
            "relocation": {"answer": "Yes", "approved": True},
            "privacy_consent": {"answer": "Yes", "approved": True},
        },
        "screening_answer_rules": [
            {
                "patterns": ["requires in-office work three days per week"],
                "answer": "Yes, I’d relocate prior to the start of the role",
            },
            {
                "patterns": ["plan to relocate to, the specified location"],
                "answer": "Yes, I plan to relocate",
            },
            {
                "patterns": ["previously, worked at Capital One"],
                "answer": "No",
            },
        ],
    }
    referral_name = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "If you heard about us through a referral, please state the Brex employee's name.",
        "required": False,
    }
    office_ack = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "This role requires in-office work three days per week (Mon, Wed, Thurs). Do you acknowledge and agree to this requirement?*",
        "required": True,
        "options": [
            "Yes, I’m currently located here",
            "Yes, I’d relocate prior to the start of the role",
            "No, I’m not located nearby",
        ],
    }
    location_plan = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Do you currently live in, or plan to relocate to, the specified location to meet this in-office requirement?*",
        "required": True,
        "options": ["Yes, I live here", "Yes, I plan to relocate", "No"],
    }
    capital_one_history = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Do you currently, or have you previously, worked at Capital One or a company acquired by Capital One as an employee, contractor, consultant, or temp?",
        "required": True,
        "options": ["Yes", "No"],
    }
    capital_one_eid = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "If you currently work, or have previously worked, at Capital One or a company acquired by Capital One, please provide your Employee ID (EID).",
        "required": True,
    }
    privacy_consent = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Do you consent to Brex processing your personal information for the purpose of assessing your candidacy for this position in accordance with Brex’s Applicant Privacy Policy?*",
        "required": True,
        "options": ["Consent"],
    }

    assert python_runtime._plan_field(referral_name, profile, None)["action"] == "skip"
    assert python_runtime._plan_field(office_ack, profile, None) == {
        "action": "combobox",
        "value": "Yes, I’d relocate prior to the start of the role",
    }
    assert python_runtime._plan_field(location_plan, profile, None) == {
        "action": "combobox",
        "value": "Yes, I plan to relocate",
    }
    assert python_runtime._plan_field(capital_one_history, profile, None) == {
        "action": "check",
        "option": "No",
    }
    assert python_runtime._plan_field(capital_one_eid, profile, None) == {"action": "fill", "value": "N/A"}
    assert python_runtime._plan_field(privacy_consent, profile, None) == {"action": "combobox", "value": "Consent"}


def test_wonderschool_applied_ai_screening_fields_are_auto_answered_truthfully():
    profile = {
        "target_company": "wonderschool",
        "location": "Jersey City, NJ, USA",
        "answers": {
            "Are you open to relocation?": "Yes",
            "Are you open to working in-person in one of our offices 25% of the time?": "Yes",
            "Why wonderschool?": "This should never be used for a School field.",
        },
        "screening_answer_rules": [
            {"patterns": ["coming to the office 3 days a week"], "answer": "Yes"},
        ],
        "education": [
            {
                "school": "Stevens Institute of Technology",
                "degree": "Master's",
                "field": "Computer Science",
            }
        ],
    }
    cs_degree = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Do you have a degree in Computer Science?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    cs_level = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "If you have a degree in Computer Science, what level (Bachelor’s, Master’s, or PhD) and from which school(s)?*",
        "required": True,
        "options": ["Bachelors", "Masters", "PhD", "Other"],
    }
    bay_area = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Please share which part of the Bay Area you’re based in?*",
        "required": True,
    }
    agents = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Describe a time you've managed AI Agents, the number and what you've had them do.*",
        "required": True,
    }
    system = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Describe a system you've built before.*",
        "required": True,
    }
    office = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you comfortable coming to the office 3 days a week (Tuesday, Wednesday, Thursday)?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    school = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "School*",
        "required": True,
        "options": [],
    }

    assert python_runtime._plan_field(school, profile, None) == {
        "action": "combobox",
        "value": "Stevens Institute of Technology",
    }
    assert python_runtime._plan_field(cs_degree, profile, None) == {"action": "combobox", "value": "Yes"}
    assert python_runtime._plan_field(cs_level, profile, None) == {"action": "combobox", "value": "Masters"}
    assert "not in the Bay Area" in python_runtime._plan_field(bay_area, profile, None)["value"]
    assert "XClaw" in python_runtime._plan_field(agents, profile, None)["value"]
    assert "LangChain multi-agent" in python_runtime._plan_field(system, profile, None)["value"]
    assert python_runtime._plan_field(office, profile, None) == {"action": "combobox", "value": "Yes"}


def test_palantir_new_grad_early_talent_fields_are_auto_answered_truthfully():
    profile = {
        "target_company": "Palantir Technologies",
        "target_title": "Forward Deployed Software Engineer, New Grad",
        "target_location": "New York, NY",
        "location": "Jersey City, NJ, USA",
        "answers": {
            "How did you hear about us?": "LinkedIn",
            "Do you have any, or anticipate any upcoming offer deadlines?": "No",
            "What are your preferred office location(s) to work from in addition to the location you are applying to? Select 1-3 from below.": "New York, NY",
            "Which office location would you prefer?": "New York",
            "What are your preferred Palantir product(s)? Please pick 1-2. Foundry Gotham Foundations Apollo": "Foundry; Apollo",
        },
        "education": [
            {
                "school": "Stevens Institute of Technology",
                "degree": "Master's",
                "field": "Computer Science",
                "end_date": "2026-05",
            }
        ],
    }
    graduation_year = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Please include your intended graduation year for the degree or relevant learning program that you are currently pursuing or have completed.",
        "required": True,
        "options": ["2025", "2026", "2027"],
    }
    source = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Please tell us how you heard about this opportunity.",
        "required": True,
        "options": ["LinkedIn", "Company Website", "Other"],
    }
    offer_deadline = {
        "kind": "radiogroup",
        "label": "Do you have any, or anticipate any upcoming offer deadlines?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    offer_dates = {
        "kind": "checkboxgroup",
        "label": "If so, what are the dates? August 1 - August 15 August 16 - August 31",
        "required": True,
        "options": [{"label": "August 1 - August 15"}],
    }
    office_locations = {
        "kind": "checkboxgroup",
        "label": "What are your preferred office location(s) to work from in addition to the location you are applying to? Select 1-3 from below.",
        "required": True,
        "options": [{"label": "New York, NY"}, {"label": "Palo Alto, CA"}],
    }
    office_location_combobox = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Which office location would you prefer?",
        "id": "question_6416158009[]",
        "required": True,
        "options": [{"label": "New York"}, {"label": "San Francisco"}],
    }
    office_location_combobox_without_options = {
        **office_location_combobox,
        "options": [],
    }
    role_confirmation = {
        "kind": "checkboxgroup",
        "label": "The FDSE and SWE roles have different applications. Please confirm that your answer to the above question matches the role you are applying for.",
        "required": True,
        "options": [{"label": "Software Engineer"}, {"label": "Forward Deployed Software Engineer"}],
    }
    role_confirmation_yes_only = {
        "kind": "checkboxgroup",
        "label": "The FDSE and SWE roles have different applications. Please confirm that your answer to the above question matches the role you are applying for.",
        "required": True,
        "options": [{"label": "Yes"}],
    }
    role_confirmation_without_options = {
        "kind": "checkboxgroup",
        "label": "The FDSE and SWE roles have different applications. Please confirm that your answer to the above question matches the role you are applying for.",
        "required": True,
        "options": [],
    }
    products = {
        "kind": "checkboxgroup",
        "label": "What are your preferred Palantir product(s)? Please pick 1-2. Foundry Gotham Foundations Apollo",
        "required": True,
        "options": [{"label": "Foundry"}, {"label": "Gotham"}, {"label": "Apollo"}],
    }
    summer_2026 = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Where are you spending summer 2026?",
        "required": True,
        "options": ["New York City or somewhere nearby", "Palo Alto or somewhere nearby"],
    }
    partner_sharing = {
        "kind": "radiogroup",
        "label": "Please share my resume and contact information with external Palantir partners for job seeking purposes.",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    california_resident = {
        "kind": "radiogroup",
        "label": "Are you a resident of California?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    technical_challenge = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What is the hardest technical challenge you've faced as part of work experience or a personal project? (Approx. 200 words)",
        "required": True,
    }
    alternative_work = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "If Palantir didn't exist, what kind of company or work would you be most excited and interested in working at/on? (Approx. 200 words)",
        "required": True,
    }
    delta_vs_dev = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Delta vs. Dev",
        "required": True,
    }
    changed_mind = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Tell us about a time you changed your mind about something. 150 words max",
        "required": True,
    }
    current_location = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "Current location",
        "required": True,
    }
    role_confirmation_checkbox = {
        "kind": "single",
        "tag": "input",
        "type": "checkbox",
        "label": "Yes",
        "section": "The FDSE and SWE roles have different applications. Please confirm that your answer to the above question matches the role you are applying for.",
        "required": True,
    }

    assert python_runtime._plan_field(graduation_year, profile, None) == {
        "action": "select",
        "value": "2026",
    }
    assert python_runtime._plan_field(source, profile, None) == {
        "action": "select",
        "value": "LinkedIn",
    }
    assert python_runtime._plan_field(summer_2026, profile, None) == {
        "action": "select",
        "value": "New York City or somewhere nearby",
    }
    assert python_runtime._plan_field(partner_sharing, profile, None)["option"]["label"] == "No"
    assert python_runtime._plan_field(california_resident, profile, None)["option"]["label"] == "No"
    assert python_runtime._plan_field(offer_deadline, profile, None)["option"]["label"] == "No"
    assert python_runtime._plan_field(offer_dates, profile, None) == {
        "action": "skip",
        "reason": "no offer deadlines reported",
        "sensitive": False,
        "blocking": False,
    }
    assert [o["label"] for o in python_runtime._plan_field(office_locations, profile, None)["options"]] == ["New York, NY"]
    assert python_runtime._plan_field(office_location_combobox, profile, None) == {
        "action": "combobox",
        "value": "New York",
    }
    assert python_runtime._plan_field(office_location_combobox_without_options, profile, None) == {
        "action": "combobox",
        "value": "New York",
    }
    assert (
        python_runtime._office_location_combobox_fallback_choice(
            office_location_combobox,
            ["New York, San Francisco"],
            "New York",
        )
        == "New York"
    )
    assert [o["label"] for o in python_runtime._plan_field(role_confirmation, profile, None)["options"]] == ["Forward Deployed Software Engineer"]
    assert [o["label"] for o in python_runtime._plan_field(role_confirmation_yes_only, profile, None)["options"]] == ["Yes"]
    assert python_runtime._plan_field(role_confirmation_without_options, profile, None) == {
        "action": "check",
    }
    assert python_runtime._plan_field(role_confirmation_checkbox, profile, None) == {
        "action": "check",
    }
    assert [o["label"] for o in python_runtime._plan_field(products, profile, None)["options"]] == ["Foundry", "Apollo"]
    profile["location"] = "Jersey City, NJ, USA"
    assert python_runtime._plan_field(current_location, profile, None) == {
        "action": "combobox",
        "value": "Jersey City, New Jersey, United States",
    }
    assert "multi-agent financial-audit workflow" in python_runtime._plan_field(
        technical_challenge, profile, None
    )["value"]
    assert "Forward Deployed Software Engineer" in python_runtime._plan_field(
        delta_vs_dev, profile, None
    )["value"]
    assert "changed my mind" in python_runtime._plan_field(changed_mind, profile, None)["value"]
    assert "applied AI infrastructure" in python_runtime._plan_field(
        alternative_work, profile, None
    )["value"]


def test_high_school_fields_do_not_use_university_without_explicit_profile_fact():
    profile = {
        "education": [
            {
                "school": "Stevens Institute of Technology",
                "degree": "Master's",
                "field": "Computer Science",
                "end_date": "2026-05",
            }
        ],
    }
    high_school_name = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "label": "High School Name",
        "required": True,
    }
    high_school_year = {
        "kind": "single",
        "tag": "select",
        "type": "select",
        "label": "Year of High School Graduation",
        "required": True,
        "options": ["2020", "2021", "Other"],
    }

    assert python_runtime._map_text_value(high_school_name, profile) is None
    assert python_runtime._plan_field(high_school_name, profile, None) == {
        "action": "skip",
        "reason": "candidate fact needs explicit approved answer",
        "sensitive": False,
        "blocking": True,
    }
    assert python_runtime._plan_field(high_school_year, profile, None) == {
        "action": "skip",
        "reason": "candidate fact needs explicit approved answer",
        "sensitive": False,
        "blocking": True,
    }

    profile["high_school"] = {
        "school": "Shenzhen Experimental High School",
        "end_year": "2019",
    }
    high_school_year["options"] = ["2018", "2019", "2020", "Other"]

    assert python_runtime._plan_field(high_school_name, profile, None) == {
        "action": "fill",
        "value": "Shenzhen Experimental High School",
    }
    assert python_runtime._plan_field(high_school_year, profile, None) == {
        "action": "select",
        "value": "2019",
    }


def test_lyra_forward_deployment_ai_fields_are_auto_answered_truthfully():
    profile = {
        "location": "Jersey City, NJ, USA",
        "answers": {},
    }
    geography = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Are you currently based in one of the following geographies?\nDenver | St. Louis | Indianapolis*",
        "required": True,
        "options": ["Yes", "No"],
    }
    frameworks = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "Which AI frameworks have you used hands-on (LangChain, LangGraph, AutoGen, etc.)? Give one concrete example of what you built with each.*",
        "required": True,
    }
    clients = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What experience do you have working directly with clients or in a consulting capacity?*",
        "required": True,
    }

    assert python_runtime._plan_field(geography, profile, None) == {"action": "combobox", "value": "No"}
    assert "LangChain" in python_runtime._plan_field(frameworks, profile, None)["value"]
    assert "stakeholders" in python_runtime._plan_field(clients, profile, None)["value"]


def test_newsbreak_llm_post_training_fields_are_auto_answered_truthfully():
    profile = {
        "target_company": "newsbreak",
        "target_title": "Machine Learning Engineer, LLM Post-Training",
        "skills": ["LLM", "RAG", "Kubernetes", "Kafka", "MLflow"],
        "work_history": [
            {
                "description": (
                    "Boosted LLM accuracy by deploying scalable fine-tuning pipelines on Kubernetes, "
                    "automating edge-data ingestion and scheduled retraining."
                )
            }
        ],
        "answers": {
            "Use this final response to make your case for why we should prioritize interviewing you. You may include anything you think is most relevant or differentiating": "Final strong fit answer."
        },
    }
    multi_gpu = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Have you trained an LLM on a multi-GPU cluster (8+ GPUs)?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    post_training_data = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Have you built data pipelines for LLM post-training (preference pairs, reward signals, etc.*",
        "required": True,
        "options": ["Yes", "No"],
    }
    rl_training = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Have you personally run RL training on an LLM (PPO, GRPO, DPO, or similar)?*",
        "required": True,
        "options": ["Yes", "No"],
    }
    strong_fit = {
        "kind": "single",
        "tag": "textarea",
        "type": "textarea",
        "label": "What makes you a strong fit for this role? *",
        "required": True,
    }

    assert python_runtime._plan_field(multi_gpu, profile, None) == {"action": "combobox", "value": "No"}
    assert python_runtime._plan_field(post_training_data, profile, None) == {"action": "combobox", "value": "Yes"}
    assert python_runtime._plan_field(rl_training, profile, None) == {"action": "combobox", "value": "No"}
    assert python_runtime._plan_field(strong_fit, profile, None) == {
        "action": "fill",
        "value": "Final strong fit answer.",
    }


def test_greenhouse_fast_path_does_not_report_an_uncommitted_click(monkeypatch):
    class Locator:
        @property
        def first(self):
            return self

        def scroll_into_view_if_needed(self):
            return None

        def click(self, timeout=None):
            return None

    class Keyboard:
        def press(self, _key):
            return None

    class Page:
        url = "https://job-boards.greenhouse.io/embed/job_app"
        keyboard = Keyboard()

        def get_by_role(self, _role, name=None, exact=None):
            return Locator()

        def get_by_text(self, _text, exact=None):
            return Locator()

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, _script, _arg=None):
            return False

    monkeypatch.setattr(python_runtime, "_control_readback", lambda _locator, _field: "")
    monkeypatch.setattr(
        python_runtime,
        "_verify_control_selection",
        lambda _page, _field, _answer: None,
    )

    assert (
        python_runtime._select_greenhouse_react_combobox_option(
            Page(),
            Locator(),
            {
                "id": "question_sponsorship",
                "role": "combobox",
                "label": "Will you require visa sponsorship?*",
            },
            "Yes - I will require visa sponsorship.",
        )
        is None
    )


def test_greenhouse_react_combobox_fuzzy_matches_country_with_dialing_code(monkeypatch):
    """Country options on Greenhouse render as 'United States +1' while the
    profile stores the plain country name. The selector should pick the closest
    visible option and return the verified selection."""

    class Locator:
        @property
        def first(self):
            return self

        def scroll_into_view_if_needed(self):
            return None

        def click(self, timeout=None):
            return None

    class Keyboard:
        def press(self, _key):
            return None

    class Page:
        url = "https://job-boards.greenhouse.io/embed/job_app"
        keyboard = Keyboard()

        def get_by_role(self, _role, name=None, exact=None):
            return Locator()

        def get_by_text(self, _text, exact=None):
            return Locator()

        def wait_for_timeout(self, _milliseconds):
            return None

        def evaluate(self, script, _arg=None):
            # First evaluate call opens the dropdown and is ignored by the
            # existing exact-match fallbacks. The fuzzy fallback then collects
            # visible option texts and clicks the matched one.
            if "const texts = []" in script:
                return ["United States +1", "Canada +1", "China +86"]
            if "return texts" not in script and "opt.click" in script:
                return True
            return False

    monkeypatch.setattr(python_runtime, "_control_readback", lambda _locator, _field: "")
    monkeypatch.setattr(
        python_runtime,
        "_verify_control_selection",
        lambda _page, _field, _answer: "United States +1",
    )

    assert (
        python_runtime._select_greenhouse_react_combobox_option(
            Page(),
            Locator(),
            {"id": "country", "role": "combobox", "label": "Country*"},
            "United States",
        )
        == "United States +1"
    )


def test_combobox_scorer_prefers_east_asian_over_central_asian_substring():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(
                """
                <input id="race" role="combobox" aria-controls="race-list"
                       data-job-agent-autofill-index="race">
                <div id="race-list" role="listbox">
                  <div role="option">Central Asian (inclusive of the peoples of Kazakhstan, Kyrgyzstan, Tajikistan, Turkmenistan, or Uzbekistan)</div>
                  <div role="option">East Asian (inclusive of Chinese, Japanese, Korean, Mongolian, Tibetan, and Taiwanese)</div>
                  <div role="option">South Asian (inclusive of the peoples of Afghanistan, Bangladesh, Bhutan, India, Maldives, Nepal, Pakistan, and Sri Lanka)</div>
                  <div role="option">Southeast Asian (inclusive of Burmese, Cambodian, Filipino, Hmong, Indonesian, Laotian, Malaysian, Mien, Singaporean, Thai, and Vietnamese)</div>
                </div>
                """
            )
            field = {
                "kind": "single",
                "tag": "input",
                "role": "combobox",
                "id": "race",
                "autofillId": "race",
                "label": "Race/Ethnicity",
                "required": True,
                "options": [],
            }
            clicked = python_runtime._click_visible_option_with_playwright(
                page,
                "Asian",
                field,
                aliases=python_runtime._answer_aliases("Asian"),
            )
        finally:
            browser.close()

    assert clicked and "East Asian" in clicked
    assert "Central Asian" not in clicked


def test_intl_tel_input_country_selector_sets_country_via_api():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <div class="iti iti--allow-dropdown">
              <input type="tel" class="iti__tel-input" value="" />
              <ul class="iti__country-list">
                <li class="iti__country" data-country-code="us">
                  <span class="iti__country-name">United States</span>
                  <span class="iti__dial-code">+1</span>
                </li>
              </ul>
            </div>
            <script>
              const input = document.querySelector('input');
              input.iti = {
                _selected: null,
                setCountry(iso) { this._selected = iso; },
                getSelectedCountryData() { return { name: this._selected === 'us' ? 'United States' : '' }; },
              };
            </script>
            """
        )
        locator = page.locator("input").first
        assert (
            python_runtime._select_intl_tel_input_country(page, locator, "United States")
            == "United States"
        )
        browser.close()


def test_intl_tel_input_country_selector_clicks_matching_item_when_api_missing():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        clicked = []
        page.set_content(
            """
            <div class="iti">
              <button class="iti__selected-country">Select</button>
              <ul class="iti__country-list">
                <li class="iti__country" data-country-code="us">
                  <span class="iti__country-name">United States</span>
                  <span class="iti__dial-code">+1</span>
                </li>
                <li class="iti__country" data-country-code="ca">
                  <span class="iti__country-name">Canada</span>
                  <span class="iti__dial-code">+1</span>
                </li>
              </ul>
            </div>
            """
        )
        page.expose_function("logClick", lambda name: clicked.append(name))
        page.evaluate("""() => {
          document.querySelectorAll('.iti__country').forEach(el => {
            el.addEventListener('click', () => {
              window.logClick(el.querySelector('.iti__country-name').textContent);
            });
          });
        }""")
        locator = page.locator("button").first
        assert (
            python_runtime._select_intl_tel_input_country(page, locator, "United States")
            == "United States"
        )
        assert clicked == ["United States"]
        browser.close()


def test_greenhouse_selected_presentation_is_committed_despite_empty_native_input():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <label for="sponsorship">Will you require visa sponsorship?*</label>
            <div class="select__control">
              <div class="select__value-container">
                <div class="select__single-value">Yes - I will require visa sponsorship.</div>
                <input
                  id="sponsorship"
                  data-job-agent-autofill-index="sponsorship"
                  role="combobox"
                  aria-expanded="false"
                  aria-invalid="true"
                  required
                  value=""
                >
              </div>
            </div>
            """
        )
        field = {
            "id": "sponsorship",
            "autofillId": "sponsorship",
            "tag": "input",
            "role": "combobox",
            "label": "Will you require visa sponsorship?*",
        }

        values, expanded = python_runtime._control_selection_readback(page, field)

        assert values == ["Yes - I will require visa sponsorship."]
        assert expanded is False
        assert python_runtime._audit_required_fields(page) == []
        browser.close()


def test_greenhouse_select_shell_committed_text_is_read_back_and_audited():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <label for="residency">Do you currently reside within the continental United States?*</label>
            <div class="select-shell">
              <span class="selected-option">option Yes, selected.</span>
              <input
                id="residency"
                data-job-agent-autofill-index="residency"
                role="combobox"
                aria-expanded="false"
                required
                value=""
              >
            </div>
            """
        )
        field = {
            "id": "residency",
            "autofillId": "residency",
            "tag": "input",
            "role": "combobox",
            "label": "Do you currently reside within the continental United States?*",
        }

        values, expanded = python_runtime._control_selection_readback(page, field)

        assert values == ["Yes"]
        assert expanded is False
        assert python_runtime._audit_required_fields(page) == []
        browser.close()


def test_required_audit_uses_greenhouse_label_star_when_native_required_is_missing():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <label for="source">How did you hear about this job?*</label>
            <div class="select__control">
              <div class="select__value-container">
                <div class="select__placeholder">Select...</div>
                <input id="source" role="combobox" aria-expanded="false" value="">
              </div>
            </div>
            """
        )

        assert python_runtime._audit_required_fields(page) == [
            {
                "label": "How did you hear about this job?*",
                "reason": "required field remains empty after fill",
            }
        ]
        browser.close()


def test_greenhouse_phone_country_uses_trusted_exact_option_and_never_first_option():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <label for="country">Phone Country*</label>
            <div class="select__control">
              <div class="select__value-container" id="country-values">
                <div class="select__placeholder">Select...</div>
                <input
                  id="country"
                  data-job-agent-autofill-index="country"
                  role="combobox"
                  aria-expanded="false"
                  aria-controls="country-options"
                  value=""
                >
              </div>
            </div>
            <div id="country-options" role="listbox" hidden>
              <div id="country-afghanistan" role="option">Afghanistan +93</div>
              <div id="country-us" role="option">United States +1</div>
            </div>
            <script>
              const input = document.getElementById("country");
              const listbox = document.getElementById("country-options");
              const values = document.getElementById("country-values");
              input.addEventListener("click", () => {
                input.setAttribute("aria-expanded", "true");
                listbox.hidden = false;
              });
              for (const option of listbox.querySelectorAll('[role="option"]')) {
                option.addEventListener("click", (event) => {
                  if (!event.isTrusted) return;
                  values.querySelector(".select__placeholder")?.remove();
                  const selected = document.createElement("div");
                  selected.className = "select__single-value";
                  selected.textContent = option.textContent;
                  values.prepend(selected);
                  input.setAttribute("aria-expanded", "false");
                  listbox.hidden = true;
                  document.body.dataset.selectedCountry = option.textContent;
                });
              }
            </script>
            """
        )
        field = {
            "id": "country",
            "autofillId": "country",
            "tag": "input",
            "type": "text",
            "role": "combobox",
            "label": "Phone Country*",
            "required": True,
        }

        readback = python_runtime._apply_fill(
            page,
            field,
            {"action": "combobox", "value": "+1"},
        )

        assert readback == "United States +1"
        assert page.locator("body").get_attribute("data-selected-country") == "United States +1"
        browser.close()


def test_phone_country_readback_accepts_committed_dial_code_next_to_phone_field():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <div class="phone-row">
              <div class="country-control">
                <label for="country">Country</label>
                <button
                  id="country"
                  data-job-agent-autofill-index="country"
                  role="combobox"
                  aria-expanded="false"
                  type="button"
                ><span aria-label="United States flag"></span><span>+1</span></button>
              </div>
              <div class="phone-control">
                <label for="phone">Phone</label>
                <input id="phone" type="tel" value="2015550100">
              </div>
            </div>
            """
        )
        field = {
            "id": "country",
            "autofillId": "country",
            "tag": "button",
            "role": "combobox",
            "label": "Country",
            "required": True,
        }

        assert (
            python_runtime._verify_control_selection(
                page,
                field,
                "United States",
            )
            == "+1"
        )
        browser.close()


def test_phone_country_readback_accepts_adjacent_form_phone_field():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <div class="greenhouse-field">
                <label for="country">Country</label>
                <button
                  id="country"
                  data-job-agent-autofill-index="country"
                  role="combobox"
                  aria-expanded="false"
                  type="button"
                >+1</button>
              </div>
              <div class="greenhouse-field">
                <label for="phone">Phone</label>
                <input id="phone" type="tel" value="2015550100">
              </div>
            </form>
            """
        )
        field = {
            "id": "country",
            "autofillId": "country",
            "tag": "button",
            "role": "combobox",
            "label": "Country",
            "required": True,
        }

        assert (
            python_runtime._verify_control_selection(
                page,
                field,
                "United States",
            )
            == "+1"
        )
        browser.close()


def test_greenhouse_phone_country_accepts_saved_us_when_committed_value_is_dial_code():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <form>
              <div class="greenhouse-field">
                <label for="country">Country*</label>
                <button
                  id="country"
                  data-job-agent-autofill-index="country"
                  role="combobox"
                  aria-expanded="false"
                  type="button"
                >+1</button>
              </div>
              <div class="greenhouse-field">
                <label for="phone">Phone*</label>
                <input id="phone" type="tel" value="2015550100">
              </div>
            </form>
            """
        )
        field = {
            "id": "country",
            "autofillId": "country",
            "tag": "button",
            "role": "combobox",
            "label": "Country*",
            "required": True,
        }
        plan = python_runtime._plan_field(field, {"country": "US"}, None)

        assert plan == {"action": "combobox", "value": "US"}
        assert python_runtime._apply_fill(page, field, plan) == "+1"
        assert page.locator("#country").get_attribute("aria-expanded") == "false"
        browser.close()


def test_combobox_fill_accepts_existing_committed_selection_before_reselecting(
    monkeypatch,
):
    class Locator:
        @property
        def first(self):
            return self

    class Page:
        def locator(self, _selector):
            return Locator()

    monkeypatch.setattr(
        python_runtime,
        "_verify_control_selection",
        lambda _page, _field, _answer: "+1",
    )
    monkeypatch.setattr(
        python_runtime,
        "_select_intl_tel_input_country",
        lambda *_args, **_kwargs: pytest.fail(
            "a committed selection must not be reselected"
        ),
    )

    assert (
        python_runtime._apply_fill(
            Page(),
            {
                "id": "country",
                "autofillId": "country",
                "tag": "button",
                "role": "combobox",
                "label": "Country",
                "required": True,
            },
            {"action": "combobox", "value": "United States"},
        )
        == "+1"
    )


def test_plain_country_field_does_not_accept_a_dial_code_without_phone_context():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(
            """
            <label for="country">Country</label>
            <button
              id="country"
              data-job-agent-autofill-index="country"
              role="combobox"
              aria-expanded="false"
              type="button"
            >+1</button>
            """
        )
        field = {
            "id": "country",
            "autofillId": "country",
            "tag": "button",
            "role": "combobox",
            "label": "Country",
            "required": True,
        }

        with pytest.raises(
            RuntimeError,
            match="does not match requested answer",
        ):
            python_runtime._verify_control_selection(
                page,
                field,
                "United States",
            )
        browser.close()


def test_ashby_not_found_application_route_falls_back_to_job_page(monkeypatch):
    class Page:
        url = "https://jobs.ashbyhq.com/cursor/job-id/application"

        def __init__(self):
            self.gotos = []

        def goto(self, url, wait_until=None, timeout=None):
            self.gotos.append(url)
            self.url = url

        def wait_for_load_state(self, _state, timeout=None):
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

    page = Page()
    opened = []
    monkeypatch.setattr(
        python_runtime,
        "_open_application_form_if_needed",
        lambda current_page: opened.append(current_page.url),
    )
    monkeypatch.setattr(
        python_runtime,
        "_wait_for_application_form_context",
        lambda _page, attempts=8, delay_ms=1000: True,
    )

    assert python_runtime._recover_application_form_from_job_page(
        page,
        "https://jobs.ashbyhq.com/cursor/job-id/application",
    )
    assert page.gotos == ["https://jobs.ashbyhq.com/cursor/job-id"]
    assert opened == ["https://jobs.ashbyhq.com/cursor/job-id"]


def test_retired_cursor_ashby_route_falls_back_to_current_official_career_page(
    monkeypatch,
):
    class Page:
        url = "https://jobs.ashbyhq.com/cursor/retired-id/application"

        def __init__(self):
            self.gotos = []

        def goto(self, url, wait_until=None, timeout=None):
            self.gotos.append(url)
            self.url = url

        def wait_for_load_state(self, _state, timeout=None):
            return None

        def wait_for_timeout(self, _milliseconds):
            return None

    page = Page()
    monkeypatch.setattr(
        python_runtime,
        "_open_application_form_if_needed",
        lambda _page: False,
    )
    monkeypatch.setattr(
        python_runtime,
        "_wait_for_application_form_context",
        lambda current_page, attempts=8, delay_ms=1000: (
            current_page.url
            == "https://cursor.com/careers/software-engineer-ml-infrastructure"
        ),
    )

    assert python_runtime._recover_application_form_from_job_page(
        page,
        "https://jobs.ashbyhq.com/cursor/retired-id/application",
        {
            "target_company": "cursor",
            "target_title": "Software Engineer, ML Infrastructure",
        },
    )
    assert page.gotos == [
        "https://jobs.ashbyhq.com/cursor/retired-id",
        "https://cursor.com/careers/software-engineer-ml-infrastructure",
    ]


def test_best_option_match_binary_yes_no_from_long_answers():
    assert python_runtime._best_option_match(["Yes", "No"], "Yes, I will require sponsorship") == "Yes"
    assert python_runtime._best_option_match(["Yes", "No"], "No, I do not require sponsorship") == "No"
    assert python_runtime._best_option_match(["Yes", "No"], "Yes, I am based in San Francisco") == "Yes"
    # Negative answer should not map to Yes.
    assert python_runtime._best_option_match(["Yes", "No"], "No, I am not willing") == "No"
    assert python_runtime._best_option_match(
        ["Yes", "No"],
        "I require/will require employer sponsorship to maintain work authorization.",
    ) == "Yes"


def test_visible_option_picker_adds_only_leading_binary_polarity_alias():
    source = inspect.getsource(python_runtime._click_visible_option_with_playwright)

    assert 'wants.push("no")' in source
    assert 'wants.push("yes")' in source
    assert "explicit leading polarity" in source


def test_best_option_match_binary_yes_no_from_grounded_experience_text():
    assert python_runtime._best_option_match(
        ["Yes", "No"],
        "I have deployed and operated Kubernetes clusters for distributed LLM training.",
    ) == "Yes"
    assert python_runtime._best_option_match(
        ["Yes", "No"],
        "I have not used Terraform in a professional setting.",
    ) == "No"


def test_plan_field_maps_grounded_experience_rule_to_binary_option():
    field = {
        "kind": "radiogroup",
        "label": "Kubernetes experience",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    profile = {
        "screening_answer_rules": [
            {
                "patterns": ["kubernetes experience"],
                "answer": (
                    "I have deployed and operated Kubernetes clusters for distributed LLM training."
                ),
            }
        ]
    }
    plan = python_runtime._plan_field(field, profile, None)
    assert plan == {"action": "check", "option": {"label": "Yes"}}


def test_explicit_experience_threshold_supports_plus_years_from_profile_fact():
    field = {
        "kind": "radiogroup",
        "label": "Do you have 3+ years of experience in an industry role?",
        "required": True,
        "options": [{"label": "Yes"}, {"label": "No"}],
    }
    profile = {
        "years_experience": 3,
        "answers": {
            "How many years of relevant experience do you have outside internships/academia?": "3 years",
        },
    }
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "check",
        "option": {"label": "Yes"},
    }


def test_best_option_match_does_not_choose_generic_placeholders():
    assert python_runtime._best_option_match(["Yes", "No", "Other", "Select"], "Maybe") is None
    assert python_runtime._best_option_match(["Yes", "No", "Other"], "Not sure") is None
    assert python_runtime._best_option_match(["Yes", "No", "Other"], "N/A") is None


def test_best_option_match_falls_back_to_token_overlap():
    assert python_runtime._best_option_match(
        ["Fall 2026 (September - December)", "Spring 2027"],
        "Fall 2026",
    ) == "Fall 2026 (September - December)"


def test_best_option_match_usage_negation():
    assert python_runtime._best_option_match(
        [
            "Yes, on my personal devices.",
            "Yes, at work.",
            "Yes, both personally and at work.",
            "I haven't used it, but I'm excited to learn more!",
        ],
        "No, I have not used Tailscale",
    ) == "I haven't used it, but I'm excited to learn more!"


def test_best_option_match_negation_polarity_respected():
    # A clearly affirmative answer should not match a negative option.
    assert python_runtime._best_option_match(
        ["I have used it", "I haven't used it"],
        "Yes, I have used it",
    ) == "I have used it"
    # A clearly negative answer should not match a non-negative option.
    assert python_runtime._best_option_match(
        ["I have used it", "I haven't used it"],
        "No, I have not used it",
    ) == "I haven't used it"


def test_best_option_match_country_region_location_question():
    options = [
        "United States/Canada",
        "Latin America",
        "Western Europe",
        "Eastern Europe",
        "India",
        "East Asia",
        "Other",
    ]
    assert (
        python_runtime._best_option_match(
            options,
            "Jersey City, New Jersey, United States",
        )
        == "United States/Canada"
    )
    assert (
        python_runtime._best_option_match(
            options,
            "I am currently based in the Hoboken, NJ area, United States",
        )
        == "United States/Canada"
    )
    assert (
        python_runtime._best_option_match(options, "Hoboken, NJ")
        == "United States/Canada"
    )


def test_plan_field_uses_country_region_closest_match_for_location_combobox():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Where are you located?*",
        "required": True,
        "options": [
            {"label": "United States/Canada"},
            {"label": "Latin America"},
            {"label": "Western Europe"},
            {"label": "Other"},
        ],
    }
    plan = python_runtime._plan_field(
        field,
        {"location": "Jersey City, New Jersey, United States"},
        None,
    )
    assert plan == {"action": "combobox", "value": "United States/Canada"}


def test_structured_geography_answer_avoids_verbose_llm_text():
    profile = {"location": "Jersey City, NJ, USA"}
    assert python_runtime._is_geography_question("Where are you located?*")
    answer = python_runtime._structured_geography_answer(
        "Where are you located?*",
        profile,
    )
    assert answer
    assert "United States" in answer
    assert "Jersey City" in answer


def test_source_question_does_not_map_to_school_when_label_contains_source_options():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "From which job site did you see this posting? Indeed LinkedIN University Glassdoor Zip Recruiter Referral Search Engine Other",
        "required": True,
        "options": [
            {"label": "Indeed", "value": "Indeed"},
            {"label": "LinkedIN", "value": "LinkedIN"},
            {"label": "University", "value": "University"},
            {"label": "Glassdoor", "value": "Glassdoor"},
            {"label": "Zip Recruiter", "value": "Zip Recruiter"},
            {"label": "Referral", "value": "Referral"},
            {"label": "Search Engine", "value": "Search Engine"},
            {"label": "Other", "value": "Other"},
        ],
    }
    profile = {
        "location": "Jersey City, NJ",
        "answers": {"How did you hear about us?": "LinkedIn"},
    }
    plan = python_runtime._plan_field(field, profile, None)
    assert plan == {"action": "combobox", "value": "LinkedIN"}


def test_na_score_option_maps_to_did_not_take():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "SAT Score*",
        "required": True,
        "options": [
            {"label": "Did not take", "value": "Did not take"},
            {"label": "1600 out of 1600", "value": "1600 out of 1600"},
        ],
    }
    profile = {"answers": {"SAT Score*": "N/A"}}
    plan = python_runtime._plan_field(field, profile, None)
    assert plan == {"action": "combobox", "value": "Did not take"}


def test_dynamic_combobox_fallback_maps_generated_na_score_to_did_not_take():
    field = {
        "role": "combobox",
        "label": "SAT Score*",
        "required": True,
    }
    profile = {
        "screening_answer_rules": [
            {"patterns": ["sat score", "act score", "gre score", "standardized test score"], "answer": "N/A"}
        ]
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["Did not take/Do not recall", "1600 out of 1600"],
        "",
        profile,
    ) == "Did not take/Do not recall"


def test_best_option_match_maps_na_to_did_not_take():
    assert python_runtime._best_option_match(
        ["Did not take/Do not recall", "1600 out of 1600"],
        "N/A",
    ) == "Did not take/Do not recall"


def test_negative_answer_matches_never_held_clearance_option():
    field = {
        "kind": "combobox",
        "type": "select",
        "label": "Active Security Clearance(s)*",
        "required": True,
        "options": [
            {"label": "Top Secret"},
            {"label": "Secret"},
            {"label": "Never held a clearance"},
        ],
    }
    matches = python_runtime._matching_options(field, "No")
    assert [o["label"] for o in matches] == ["Never held a clearance"]


def test_office_commitment_question_uses_approved_standing_rule():
    field = {
        "kind": "buttongroup",
        "type": "button",
        "label": "Can you work out of the San Francisco office 5 days a week?",
        "required": True,
        "options": [
            {"label": "Yes", "value": "yes"},
            {"label": "No", "value": "no"},
        ],
    }
    profile = {
        "screening_answer_rules": [
            {"patterns": ["work from our local office", "onsite monday-friday", "monday-friday"], "answer": "Yes"}
        ]
    }
    plan = python_runtime._plan_field(field, profile, None)
    assert plan["action"] == "buttonclick"
    assert plan["option"]["label"] == "Yes"


def test_dynamic_combobox_fallback_office_commitment_uses_approved_rule():
    field = {
        "role": "combobox",
        "label": "Please note the expectation for this role is to work in our Chicago office - please confirm this works for you.",
        "required": True,
    }
    profile = {
        "screening_answer_rules": [
            {"patterns": ["work from our local office", "monday-friday"], "answer": "Yes"}
        ]
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["Yes", "No"],
        "",
        profile,
    ) == "Yes"


def test_dynamic_combobox_fallback_age_bucket_prefers_derived_range():
    field = {
        "role": "combobox",
        "label": "What is your current age?",
        "required": True,
    }
    profile = {
        "birthday": "2002-06-15",
        "sensitive_answers": {
            "age_bucket": {
                "patterns": ["what is your current age", "current age", "age range", "age bracket"],
                "answer": "Under 30",
                "approved": True,
            }
        },
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["18-24", "25-34", "35-44"],
        "",
        profile,
    ) == "18-24"


def test_dynamic_combobox_fallback_age_under_30_style_option():
    field = {
        "role": "combobox",
        "label": "What is your current age?",
        "required": True,
    }
    profile = {"birthday": "2001-07-28"}
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["Under 30", "30-39", "40-49", "50-59", "60 or older", "I prefer not to answer"],
        "",
        profile,
    ) == "Under 30"


def test_dynamic_combobox_fallback_work_authorization_prefers_screening_rule():
    field = {
        "role": "combobox",
        "label": "U.S. Work Authorization",
        "required": True,
    }
    profile = {
        "screening_answer_rules": [
            {
                "patterns": ["work authorization", "authorized to work in the united states"],
                "answer": "Seeking work authorization",
            }
        ],
        "sensitive_answers": {
            "work_authorization_us": {
                "label": "US Work Authorization",
                "patterns": ["authorized to work in the us"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        [
            "Can work for any employer",
            "Can work for current employer",
            "Seeking work authorization",
            "NA, applying to non-US location",
        ],
        "",
        profile,
    ) == "Seeking work authorization"


def test_dynamic_combobox_fallback_ethnicity_maps_to_not_hispanic():
    field = {
        "role": "combobox",
        "label": "Indicate Ethnic group",
        "required": True,
    }
    profile = {"demographics": {"ethnicity": "East Asian"}}
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["Hispanic or Latino", "Not Hispanic or Latino", "I don't wish to answer"],
        "",
        profile,
    ) == "Not Hispanic or Latino"


def test_cc305_disability_disclosure_uses_approved_disability_fact():
    label = "Please review Form CC-305 at the link above before checking one of the boxes below."
    field = {
        "role": "combobox",
        "label": label,
        "required": True,
    }
    profile = {
        "demographics": {"disability": "No"},
        "sensitive_answers": {
            "disability": {
                "patterns": ["disability", "disabled"],
                "answer": "No",
                "approved": True,
            }
        },
    }
    assert python_runtime._is_sensitive(label) is True
    assert python_runtime._match_sensitive(label, profile) == "No"
    assert python_runtime._plan_field(field, profile, None) == {
        "action": "combobox",
        "value": "No",
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        [
            "Yes, I have a disability, or have had one in the past.",
            "No, I do not have a disability and have not had one in the past.",
            "I don't wish to answer",
        ],
        "",
        profile,
    ) == "No, I do not have a disability and have not had one in the past."


def test_based_in_metro_answer_uses_generic_city_extraction():
    assert python_runtime._based_in_metro_question_answer(
        "Are you local to Fort Worth, TX?",
        {"location": "Jersey City, NJ, USA"},
    ) == "No"
    assert python_runtime._based_in_metro_question_answer(
        "Are you local to Jersey City, NJ?",
        {"location": "Jersey City, NJ, USA"},
    ) == "Yes"
    assert python_runtime._based_in_metro_question_answer(
        "Are you a resident of California?",
        {"location": "Jersey City, NJ, USA"},
    ) == "No"
    assert python_runtime._based_in_metro_question_answer(
        "Are you a resident of New Jersey?",
        {"location": "Jersey City, NJ, USA"},
    ) == "Yes"
    assert python_runtime._based_in_metro_question_answer(
        "Do you currently reside in the Austin, TX metro area?",
        {"location": "Jersey City, NJ, USA"},
    ) == "No"
    assert python_runtime._based_in_metro_question_answer(
        "Do you live in New York or California?",
        {"location": "Jersey City, NJ, USA"},
    ) == "No"


def test_dynamic_combobox_fallback_local_question_prefers_willing_relocation_option():
    field = {
        "role": "combobox",
        "label": "Are you local to Fort Worth, TX?",
        "required": True,
    }
    profile = {
        "location": "Jersey City, NJ, USA",
        "sensitive_answers": {
            "relocation": {"patterns": ["relocation"], "answer": "Yes", "approved": True}
        },
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["Yes", "No", "No, but I'm willing to relocate"],
        "",
        profile,
    ) == "No, but I'm willing to relocate"


def test_negative_answer_matches_not_currently_us_person_option():
    field = {
        "kind": "combobox",
        "type": "select",
        "label": "Are you currently a U.S. Person as described above, or otherwise eligible to obtain the required authorization?",
        "required": True,
        "options": [
            {"label": "U.S. citizen or national of the United States"},
            {"label": "U.S. lawful permanent resident (Green Card holder)"},
            {"label": "Refugee admitted under 8 U.S.C. 1157"},
            {"label": "Asylee granted asylum under 8 U.S.C. 1158"},
            {"label": "Not currently a U.S. Person / Other status"},
        ],
    }
    matches = python_runtime._matching_options(field, "No")
    assert [o["label"] for o in matches] == ["Not currently a U.S. Person / Other status"]


def test_best_option_match_maps_negative_answer_to_not_worked_option():
    assert python_runtime._best_option_match(
        ["I am a previous employee", "I have not worked at DoorDash"],
        "No",
    ) == "I have not worked at DoorDash"


def test_dynamic_combobox_fallback_maps_negative_screening_rule_to_not_worked_option():
    field = {
        "role": "combobox",
        "label": "Have you worked at DoorDash?",
        "required": True,
    }
    profile = {
        "screening_answer_rules": [
            {"patterns": ["worked at"], "answer": "No"}
        ]
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        ["I am a previous employee", "I have not worked at DoorDash"],
        "",
        profile,
    ) == "I have not worked at DoorDash"


def test_where_do_you_currently_live_uses_profile_location():
    field = {
        "kind": "single",
        "tag": "input",
        "type": "text",
        "role": "combobox",
        "label": "Where do you currently live?",
        "required": True,
        "options": [],
    }
    profile = {"location": "Jersey City, NJ, USA", "country": "United States"}
    plan = python_runtime._plan_field(field, profile, None)
    assert plan == {"action": "combobox", "value": "Jersey City, NJ, USA"}


def test_source_answer_linkedin_matches_social_media_option():
    assert python_runtime._option_matches("Social Media", "LinkedIn")
    assert python_runtime._option_matches("Third Party Job Board", "LinkedIn")
    assert python_runtime._best_option_match(
        ["Events", "General Awareness", "Company Website", "Social Media", "Third Party Job Board", "Press", "Other"],
        "LinkedIn",
    ) == "Social Media"


def test_how_did_you_hear_combobox_uses_social_media_option():
    field = {
        "kind": "combobox",
        "role": "combobox",
        "tag": "select",
        "label": "How did you hear about The Trade Desk?",
        "required": True,
        "options": [
            {"label": "Events"},
            {"label": "General Awareness"},
            {"label": "Company Website"},
            {"label": "Social Media"},
            {"label": "Third Party Job Board"},
            {"label": "Press"},
            {"label": "Other"},
        ],
    }
    profile = {"answers": {"How did you hear about us?": "LinkedIn"}}
    plan = python_runtime._plan_field(field, profile, None)
    assert plan["action"] == "combobox"
    assert plan["value"] == "Social Media"


def test_citizenship_status_uses_other_when_not_a_us_citizen():
    field = {
        "kind": "combobox",
        "role": "combobox",
        "tag": "select",
        "label": "Citizenship Status",
        "required": True,
        "options": [
            {"label": "1) U.S. citizen or national of the United States"},
            {"label": "2) U.S. lawful permanent resident (green card holder)"},
            {"label": "3) Refugee under 8 U.S.C 1157"},
            {"label": "4) Asylee under 8 U.S.C 1158"},
            {"label": "5) Authorized to work in the United States under the Deferred Action For Childhood Arrivals (DACA Program)"},
            {"label": "6) Other (please explain)"},
        ],
    }
    profile = {
        "sensitive_answers": {
            "citizenship": {
                "label": "Citizenship",
                "patterns": ["citizenship", "citizen", "us citizen"],
                "answer": "No",
                "approved": True,
            }
        }
    }
    assert python_runtime._citizenship_status_option(field, profile)["label"] == "6) Other (please explain)"
    plan = python_runtime._plan_field(field, profile, None)
    assert plan == {"action": "combobox", "value": "6) Other (please explain)"}


def test_what_city_do_you_live_in_returns_profile_city():
    profile = {"location": "Jersey City, NJ, USA", "city": "Jersey City"}
    assert python_runtime._structured_geography_answer("What city do you live in?", profile) == "Jersey City"


def test_radiogroup_without_plan_options_defers_to_live_choices():
    field = {
        "kind": "radiogroup",
        "label": "We work 5 days on-site in NYC. If you're not local, are you willing to relocate?",
        "required": True,
        "options": [],
    }
    profile = {
        "answers": {"Are you open to relocation?": "Yes"},
        "sensitive_answers": {
            "relocation": {
                "label": "Relocation",
                "patterns": ["relocation", "willing to relocate", "open to relocation"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }
    plan = python_runtime._plan_field(field, profile, None)
    assert plan["action"] == "check"
    assert plan.get("defer_live_options") is True


def test_dynamic_radiogroup_fallback_prefers_willing_relocation_statement():
    field = {
        "kind": "radiogroup",
        "label": "We work 5 days on-site in NYC. If you're not local, are you willing to relocate?",
        "required": True,
    }
    profile = {
        "answers": {"Are you open to relocation?": "Yes"},
        "sensitive_answers": {
            "relocation": {
                "label": "Relocation",
                "patterns": ["relocation", "willing to relocate", "open to relocation"],
                "answer": "Yes",
                "approved": True,
            }
        },
    }
    assert python_runtime._dynamic_combobox_fallback_choice(
        field,
        [
            "I am in NYC and happy to work in office",
            "I will relocate and am happy to work in office",
            "I do not want to work in office",
        ],
        "",
        profile,
    ) == "I will relocate and am happy to work in office"
