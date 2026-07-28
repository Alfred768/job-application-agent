from __future__ import annotations

import base64
import calendar
import json
import os
import re
import secrets
import string
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from job_agent.ats_adapters import adapter_profile_key_for_field, detect_ats_from_url
from job_agent.capmonster import (
    CapMonsterClient,
    CapMonsterConfig,
    CapMonsterError,
    build_complex_image_task,
    build_datadome_task,
    build_funcaptcha_task,
    build_geetest_task,
    build_hcaptcha_task,
    build_recaptcha_v2_task,
    build_recaptcha_v2_enterprise_task,
    build_recaptcha_v3_task,
    build_turnstile_task,
    proxy_settings_from_env,
)
from job_agent.field_semantics import classify_field, value_for_semantic
from job_agent.gmail_verification import (
    GREENHOUSE_SECURITY_CODE_QUERY,
    GmailVerificationError,
    wait_for_verification_code,
    wait_for_verification_link,
)
from job_agent.llm_answer_resolver import get_llm_answer_resolver, match_screening_rule
from job_agent.resumes import ResumePathError, resolve_original_resume_pdf
from job_agent.sensitive_kb import resolve_sensitive_answer
from hello_agents.core.llm import HelloAgentsLLM


SUBMIT_GATE_LINE = (
    "Submit gate: automatic submission not performed because blocking review fields "
    "remain or the final Submit control is unavailable."
)
SUBMITTED_LINE_PREFIX = "Submission confirmed:"
SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX = "Submit clicked but confirmation not detected:"
EMAIL_VERIFICATION_REQUIRED_LINE_PREFIX = "Email verification required:"
SUBMISSION_PROCESSING_ERROR_LINE_PREFIX = "Submission processing error:"
CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX = "Candidate account required:"
WORKDAY_ACCOUNT_VERIFIED_LINE_PREFIX = "Workday account verification handled:"
APPLICATION_FORM_UNAVAILABLE_LINE_PREFIX = "Application form unavailable:"
REVIEW_ITEM_LINE_PREFIX = "Review item:"
CAPTCHA_RECOVERY_ATTEMPTS = 1
_CANDIDATE_ACCOUNT_PASSWORD_STORE_FILENAME = ".job-agent-candidate-passwords.json"
_CANDIDATE_ACCOUNT_PASSWORD_LENGTH = 20
_CANDIDATE_ACCOUNT_PASSWORD_SPECIALS = "!@#$%^*_-"
_DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


class ComboboxNoProgressError(RuntimeError):
    """Raised when one field exhausts its bounded repair budget."""


class RuntimeActionDenied(RuntimeError):
    """Raised when Agent Core denies one live browser action."""


RuntimeActionRunner = Callable[
    [str, str, Mapping[str, Any], Callable[[], Any]],
    Any,
]


def _runtime_agent_action(
    runner: RuntimeActionRunner | None,
    name: str,
    effect: str,
    context: Mapping[str, Any],
    action: Callable[[], Any],
) -> Any:
    if runner is None:
        return action()
    return runner(name, effect, context, action)


SENSITIVE = [
    "sponsor",
    "sponsorship",
    "visa",
    "authorization",
    "authorized",
    "right to work",
    "disability",
    "veteran",
    "military",
    "gender",
    "ethnicity",
    "ethnic",
    "hispanic",
    "latino",
    "race",
    "racial",
    "salary",
    "compensation",
    "pay range",
    "pay expectation",
    "expected pay",
    "expected hourly",
    "hourly pay",
    "hourly rate",
    "base pay",
    "relocation",
    "relocate",
    "start date",
    "legal",
    "attestation",
    "eeo",
    "demographic",
    "clearance",
    "export control",
    "export licens",
    "protected individual",
    "citizen",
    "non compete",
    "non competition",
    "non solicitation",
    "employment contract",
    "employment agreement",
    "h 1b",
    "certify",
    "arbitration",
    "acknowledgement",
    "acknowledgment",
    "consent",
    "privacy",
    "personal data",
    "terms and conditions",
    "background check",
    "confirm the statement",
    "true and accurate",
    "false or misleading",
    "notetaker",
    "notetakers",
    "transcribe",
]
PLACEHOLDER_ANSWERS = {"needs review", "n/a", "tbd", "na", ""}


def load_runtime_payload(script_path: str | Path) -> dict[str, Any]:
    """Extract the embedded CFG payload from a generated JS runtime script."""
    text = Path(script_path).read_text()
    match = re.search(r"^const CFG = (?P<payload>\{.*\});$", text, re.MULTILINE)
    if not match:
        raise ValueError("runtime payload not found in generated autofill script")
    return json.loads(match.group("payload"))


def _runtime_application_url(application_url: Any) -> str:
    """Return the URL the runtime should actually open.

    Some custom careers pages wrap Greenhouse postings but are unreliable for
    automation.  Coinbase hosts the form behind Cloudflare, and C3 AI's
    ``/job-description`` page redirects to a queryless 404 when opened by the
    runtime.  In those narrow cases, use Greenhouse's public embedded
    application endpoint.  Keep this intentionally narrow so working custom
    Greenhouse hosts continue to use their native application pages.
    """

    raw = str(application_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host.endswith("coinbase.com") and "/careers/positions/" in parsed.path:
        token_match = re.search(r"(?:^|[?&])gh_jid=(\d+)(?:&|$)", parsed.query)
        if token_match is None:
            token_match = re.search(r"/positions/(\d+)", parsed.path)
        if token_match is not None:
            return f"https://job-boards.greenhouse.io/embed/job_app?for=coinbase&token={token_match.group(1)}"
    if host.endswith("c3.ai") and "/job-description/" in parsed.path:
        token_match = re.search(r"(?:^|[?&])gh_jid=(\d+)(?:&|$)", parsed.query)
        if token_match is None:
            token_match = re.search(r"/job-description/(\d+)", parsed.path)
        if token_match is not None:
            return f"https://job-boards.greenhouse.io/embed/job_app?for=c3iot&token={token_match.group(1)}"
    if host.endswith("samsara.com") and "/company/careers/roles/" in parsed.path:
        token_match = re.search(r"(?:^|[?&])gh_jid=(\d+)(?:&|$)", parsed.query)
        if token_match is None:
            token_match = re.search(r"/roles/(\d+)", parsed.path)
        if token_match is not None:
            return f"https://job-boards.greenhouse.io/embed/job_app?for=samsara&token={token_match.group(1)}"
    if host.endswith("pinterestcareers.com") and parsed.path.rstrip("/") == "/jobs":
        token_match = re.search(r"(?:^|[?&])gh_jid=(\d+)(?:&|$)", parsed.query)
        if token_match is not None:
            return f"https://job-boards.greenhouse.io/embed/job_app?for=pinterest&token={token_match.group(1)}"
    return raw


def _verify_runtime_resume_file(payload: dict[str, Any]) -> None:
    raw_resume = payload.get("resumeFile")
    source_dir_raw = str(
        payload.get("resumeSourceDir")
        or os.getenv("JOB_AGENT_RUNTIME_RESUME_SOURCE_DIR")
        or ""
    ).strip()
    required_pdf_raw = str(
        payload.get("requiredResumePdf")
        or os.getenv("JOB_AGENT_RUNTIME_REQUIRED_RESUME_PDF")
        or ""
    ).strip()
    if not raw_resume:
        if source_dir_raw or required_pdf_raw:
            raise ValueError("missing required PDF resume upload path")
        return
    resume_path = Path(str(raw_resume)).expanduser()
    if not resume_path.is_absolute():
        runtime_dir = Path(str(payload.get("_runtimeScriptDir") or ".")).expanduser()
        resume_path = runtime_dir / resume_path
    package_dir = payload.get("_runtimePackageDir") or payload.get("_runtimeScriptDir")
    try:
        resolved = resolve_original_resume_pdf(
            resume_path,
            source_dir=source_dir_raw or None,
            package_dir=package_dir,
            required_pdf=required_pdf_raw or None,
        )
    except ResumePathError as exc:
        raise ValueError(str(exc)) from exc
    payload["resumeFile"] = str(resolved)


def run_runtime_payload(
    payload: dict[str, Any],
    *,
    action_runner: RuntimeActionRunner | None = None,
) -> int:
    """Run the generic autofill runtime with Python Playwright.

    This mirrors the generated Node runtime's submission contract: by default,
    it clicks final Submit only if there are no review-required fields. Set
    JOB_AGENT_SUBMIT_COMPLETE=0 to stop before Submit.
    """
    _verify_runtime_resume_file(payload)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("Python Playwright is not installed") from exc

    browser = None
    try:
        with sync_playwright() as playwright:
            headless = bool(payload.get("headless", True))
            headless_override = os.getenv("BROWSER_HEADLESS")
            if headless_override is not None:
                headless = _norm(headless_override) not in {"0", "false", "no", "off"}
            launch_options: dict[str, Any] = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            browser_channel = str(os.getenv("BROWSER_CHANNEL") or "").strip()
            if browser_channel:
                launch_options["channel"] = browser_channel
            browser = playwright.chromium.launch(**launch_options)
            context = browser.new_context(**_browser_context_options())
            _install_browser_fingerprint_mitigation(context)
            page = context.new_page()
            page.set_default_timeout(10000)
            # Some ATSs dispatch the email challenge as soon as their final
            # form validation succeeds, before the explicit Submit click.
            verification_window_started_at_ns = time.time_ns()
            profile = payload.get("profile") or {}
            transcript_file = str(payload.get("transcriptFile") or os.getenv("JOB_AGENT_TRANSCRIPT_FILE") or "").strip()
            if transcript_file and not profile.get("_transcript_file"):
                profile["_transcript_file"] = transcript_file
            application_url = _runtime_application_url(payload.get("applicationUrl"))
            if application_url and not profile.get("_application_url"):
                profile["_application_url"] = application_url
            review_artifact: str | None = None
            review_artifact_reported = False
            if application_url:
                page.goto(application_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                _dismiss_cookie_banner(page)
                if _candidate_account_password(profile):
                    for _ in range(2):
                        if _sign_in_to_candidate_home_if_available(page, profile):
                            break
                        page.wait_for_timeout(1000)
                _open_application_form_if_needed(page)
                if _candidate_account_password(profile):
                    _switch_to_candidate_sign_in_if_needed(page)
                application_form_ready = _wait_for_application_form_context(page)
                if not application_form_ready:
                    application_form_ready = _recover_application_form_from_job_page(
                        page,
                        application_url,
                    )
                if not application_form_ready:
                    review = [
                        {
                            "label": "Application form",
                            "reason": "no visible job-application form was found",
                            "sensitive": False,
                            "blocking": True,
                        }
                    ]
                    artifact = _write_review_evidence(page, payload, review)
                    print(
                        f"{APPLICATION_FORM_UNAVAILABLE_LINE_PREFIX} "
                        "no visible job-application form was found"
                    )
                    _emit_review_items(review)
                    if artifact:
                        print(f"Review evidence: {artifact}")
                    print("Autofill stats: filled=0 review=1")
                    print(SUBMIT_GATE_LINE)
                    return 0
                _install_application_navigation_guard(page, application_url)

            all_filled: list[dict[str, Any]] = []
            all_review: list[dict[str, Any]] = []
            pages = 0
            max_pages = int(payload.get("maxPages") or 12)
            repeated_workday_sign_in_pages = 0
            last_workday_sign_in_signature = ""
            repeated_workday_sign_in_fill_pages = 0
            last_workday_sign_in_fill_signature = ""
            workday_account_verification_attempted = False
            workday_error_refreshes = 0
            while pages < max_pages:
                if _candidate_account_password(profile) and _has_workday_login_controls(page):
                    _sign_in_to_candidate_home_if_available(page, profile)
                    _open_application_form_if_needed(page)
                _restore_application_context_if_external(page, application_url)
                _restore_workday_application_from_candidate_home(page, application_url)
                _install_application_navigation_guard(page, application_url)
                pages += 1
                _open_application_form_if_needed(page)
                current_fields = _runtime_agent_action(
                    action_runner,
                    "ats_observe_page",
                    "observe",
                    {
                        "phase": "page_observation",
                        "page_index": pages,
                        "application_url": application_url,
                    },
                    lambda: _ensure_application_fields_ready(page),
                )
                if (
                    "myworkdayjobs.com" in str(page.url or "").lower()
                    and workday_error_refreshes < 2
                    and _workday_transient_error_page(page)
                ):
                    workday_error_refreshes += 1
                    print("Workday transient error page detected; refreshing and retrying.")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        try:
                            page.reload(timeout=30000)
                        except Exception:
                            pass
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(2500)
                    pages -= 1
                    continue
                if (
                    pages > 1
                    and last_workday_sign_in_fill_signature
                    and "myworkdayjobs.com" in str(page.url or "").lower()
                    and not _meaningful_application_fields(current_fields)
                ):
                    if _restore_workday_application_from_candidate_home(page, application_url):
                        pages -= 1
                        continue
                    all_review.append(
                        {
                            "label": "Candidate account sign-in",
                            "reason": _workday_sign_in_failure_reason(page)
                            or "candidate account sign-in did not advance to the Workday application form",
                            "sensitive": False,
                            "blocking": True,
                        }
                    )
                    break
                account_verification_reason = _workday_account_verification_reason(page)
                if account_verification_reason:
                    if not workday_account_verification_attempted:
                        workday_account_verification_attempted = True
                        if _verify_workday_candidate_account_if_configured(
                            page,
                            requested_after_ns=time.time_ns(),
                            payload=payload,
                        ):
                            continue
                    all_review.append(
                        {
                            "label": "Candidate account verification",
                            "reason": account_verification_reason,
                            "sensitive": False,
                            "blocking": True,
                        }
                    )
                    break
                if _workday_sign_in_field_set(current_fields):
                    sign_in_signature = _form_field_signature(current_fields)
                    if sign_in_signature and sign_in_signature == last_workday_sign_in_signature:
                        repeated_workday_sign_in_pages += 1
                    else:
                        repeated_workday_sign_in_pages = 1
                        last_workday_sign_in_signature = sign_in_signature
                    explicit_sign_in_reason = _workday_sign_in_failure_reason(
                        page,
                        allow_generic=False,
                    )
                    if explicit_sign_in_reason or repeated_workday_sign_in_pages > 1:
                        if _open_workday_create_account_from_sign_in_if_available(
                            page,
                            require_failure=False,
                        ):
                            repeated_workday_sign_in_pages = 0
                            last_workday_sign_in_signature = ""
                            continue
                        all_review.append(
                            {
                                "label": "Candidate account sign-in",
                                "reason": explicit_sign_in_reason
                                or _workday_sign_in_failure_reason(page)
                                or "candidate account sign-in rejected by Workday",
                                "sensitive": False,
                                "blocking": True,
                            }
                        )
                        break
                else:
                    repeated_workday_sign_in_pages = 0
                    last_workday_sign_in_signature = ""
                step_before = _current_application_step(page)
                print(f"Autofill progress: page {pages} ({step_before or 'application entry'})")
                result = _runtime_agent_action(
                    action_runner,
                    "ats_fill_fields",
                    "write",
                    {
                        "phase": "field_fill",
                        "page_index": pages,
                        "application_url": application_url,
                    },
                    lambda: _fill_page(
                        page,
                        profile,
                        payload.get("resumeFile"),
                        payload.get("coverLetterFile"),
                    ),
                )
                if _restore_application_context_if_external(page, application_url):
                    _install_application_navigation_guard(page, application_url)
                    result = _runtime_agent_action(
                        action_runner,
                        "ats_fill_fields",
                        "write",
                        {
                            "phase": "field_refill",
                            "page_index": pages,
                            "application_url": application_url,
                        },
                        lambda: _fill_page(
                            page,
                            profile,
                            payload.get("resumeFile"),
                            payload.get("coverLetterFile"),
                        ),
                    )
                fields_before_next = _form_field_signature(_ensure_application_fields_ready(page))
                page_filled = list(result["filled"])
                for _attempt in range(1, _self_heal_passes()):
                    previous_blocking = sum(item.get("blocking", True) for item in result["review"])
                    if not _has_retryable_blocking_review(result["review"]):
                        break
                    page.wait_for_timeout(750)
                    _restore_application_context_if_external(page, application_url)
                    _install_application_navigation_guard(page, application_url)
                    retry = _runtime_agent_action(
                        action_runner,
                        "ats_fill_fields",
                        "write",
                        {
                            "phase": "bounded_self_heal",
                            "page_index": pages,
                            "application_url": application_url,
                        },
                        lambda: _fill_page(
                            page,
                            profile,
                            payload.get("resumeFile"),
                            payload.get("coverLetterFile"),
                        ),
                    )
                    _extend_unique_filled(page_filled, retry["filled"])
                    next_blocking = sum(item.get("blocking", True) for item in retry["review"])
                    result = retry
                    if not retry["filled"] and next_blocking >= previous_blocking:
                        break
                sign_in_fill_signature = _workday_sign_in_fill_signature(page, page_filled)
                if sign_in_fill_signature:
                    if sign_in_fill_signature == last_workday_sign_in_fill_signature:
                        repeated_workday_sign_in_fill_pages += 1
                    else:
                        repeated_workday_sign_in_fill_pages = 1
                        last_workday_sign_in_fill_signature = sign_in_fill_signature
                    if repeated_workday_sign_in_fill_pages > 1:
                        sign_in_reason = _workday_sign_in_failure_reason(page)
                        if _open_workday_create_account_from_sign_in_if_available(
                            page,
                            require_failure=False,
                        ):
                            repeated_workday_sign_in_fill_pages = 0
                            last_workday_sign_in_fill_signature = ""
                            continue
                        all_review.append(
                            {
                                "label": "Candidate account sign-in",
                                "reason": sign_in_reason
                                or "candidate account sign-in did not advance after filling email and password",
                                "sensitive": False,
                                "blocking": True,
                            }
                        )
                        break
                else:
                    repeated_workday_sign_in_fill_pages = 0
                    last_workday_sign_in_fill_signature = ""
                if "myworkdayjobs.com" in str(page.url or "").lower():
                    _close_workday_open_menus(page)
                    fields_before_next = _form_field_signature(_ensure_application_fields_ready(page))
                final_required_findings = _audit_required_fields(page)
                result["review"] = _retain_unresolved_control_reviews(
                    result["review"], final_required_findings
                )
                _append_required_audit(
                    result["review"], final_required_findings, filled=page_filled
                )
                result["review"] = _filter_successful_readback_reviews(
                    result["review"], page_filled
                )
                if (
                    "myworkdayjobs.com" in str(page.url or "").lower()
                    and any(item.get("blocking", True) for item in result["review"])
                    and _close_workday_open_menus(page)
                ):
                    retry = _runtime_agent_action(
                        action_runner,
                        "ats_fill_fields",
                        "write",
                        {
                            "phase": "workday_menu_recovery",
                            "page_index": pages,
                            "application_url": application_url,
                        },
                        lambda: _fill_page(
                            page,
                            profile,
                            payload.get("resumeFile"),
                            payload.get("coverLetterFile"),
                        ),
                    )
                    _extend_unique_filled(page_filled, retry["filled"])
                    final_required_findings = _audit_required_fields(page)
                    result["review"] = _retain_unresolved_control_reviews(
                        retry["review"], final_required_findings
                    )
                    _append_required_audit(
                        result["review"], final_required_findings, filled=page_filled
                    )
                    result["review"] = _filter_successful_readback_reviews(
                        result["review"], page_filled
                    )
                _extend_unique_filled(all_filled, page_filled)
                all_review.extend(result["review"])
                if any(item.get("blocking", True) for item in result["review"]):
                    review_artifact = _write_review_evidence(page, payload, result["review"])
                    if review_artifact:
                        print(f"Review evidence: {review_artifact}")
                        review_artifact_reported = True
                    break

                next_button = _find_button(page, kind="next")
                if not next_button:
                    break
                try:
                    _runtime_agent_action(
                        action_runner,
                        "ats_advance_page",
                        "write",
                        {
                            "phase": "page_navigation",
                            "page_index": pages,
                            "application_url": application_url,
                            "blocking_review_count": 0,
                        },
                        lambda: _click_button(page, next_button),
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(2500)
                    _restore_application_context_if_external(page, application_url)
                    _install_application_navigation_guard(page, application_url)
                    account_verification_reason = _workday_account_verification_reason(page)
                    if account_verification_reason:
                        if not workday_account_verification_attempted:
                            workday_account_verification_attempted = True
                            if _verify_workday_candidate_account_if_configured(
                                page,
                                requested_after_ns=time.time_ns(),
                                payload=payload,
                            ):
                                continue
                        all_review.append(
                            {
                                "label": "Candidate account verification",
                                "reason": account_verification_reason,
                                "sensitive": False,
                                "blocking": True,
                            }
                        )
                        break
                    sign_in_reason = _workday_sign_in_failure_reason(
                        page,
                        allow_generic=False,
                    )
                    if sign_in_reason:
                        if _open_workday_create_account_from_sign_in_if_available(
                            page,
                            require_failure=False,
                        ):
                            continue
                        all_review.append(
                            {
                                "label": "Candidate account sign-in",
                                "reason": sign_in_reason,
                                "sensitive": False,
                                "blocking": True,
                            }
                        )
                        break
                    validation_errors = _visible_form_validation_errors(page)
                    if validation_errors:
                        recovered_filled, recovered_review, validation_errors = _recover_from_validation_errors(
                            page,
                            profile,
                            payload.get("resumeFile"),
                            payload.get("coverLetterFile"),
                            action_runner=action_runner,
                            application_url=application_url,
                            page_index=pages,
                        )
                        _extend_unique_filled(page_filled, recovered_filled)
                        all_review.extend(recovered_review)
                        if any(item.get("blocking", True) for item in recovered_review):
                            break
                        if validation_errors:
                            all_review.extend(
                                {
                                    "label": error,
                                    "reason": "page validation error after Save and Continue",
                                    "sensitive": False,
                                    "blocking": True,
                                }
                                for error in validation_errors
                            )
                            break
                    step_after = _current_application_step(page)
                    fields_after_next_list = _scrape_fields(page)
                    fields_after_next = _form_field_signature(fields_after_next_list)
                    if _page_did_not_advance(
                        step_before,
                        step_after,
                        fields_before_next,
                        fields_after_next,
                    ):
                        try:
                            selector = _attr_selector(
                                "data-job-agent-button-index",
                                str(next_button.get("autofillId") or ""),
                            )
                            _runtime_agent_action(
                                action_runner,
                                "ats_advance_page",
                                "write",
                                {
                                    "phase": "page_navigation_fallback",
                                    "page_index": pages,
                                    "application_url": application_url,
                                    "blocking_review_count": 0,
                                },
                                lambda: page.locator(selector).first.evaluate(
                                    "(node) => node.click()"
                                ),
                            )
                            page.wait_for_timeout(2500)
                        except Exception:
                            pass
                        validation_errors = _visible_form_validation_errors(page)
                        step_after = _current_application_step(page)
                        fields_after_next_list = _scrape_fields(page)
                        fields_after_next = _form_field_signature(fields_after_next_list)
                        if validation_errors:
                            recovered_filled, recovered_review, validation_errors = _recover_from_validation_errors(
                                page,
                                profile,
                                payload.get("resumeFile"),
                                payload.get("coverLetterFile"),
                                action_runner=action_runner,
                                application_url=application_url,
                                page_index=pages,
                            )
                            _extend_unique_filled(page_filled, recovered_filled)
                            all_review.extend(recovered_review)
                            if any(item.get("blocking", True) for item in recovered_review):
                                break
                            if validation_errors:
                                all_review.extend(
                                    {
                                        "label": error,
                                        "reason": "page validation error after Save and Continue",
                                        "sensitive": False,
                                        "blocking": True,
                                    }
                                    for error in validation_errors
                                )
                                break
                        if _page_did_not_advance(
                            step_before,
                            step_after,
                            fields_before_next,
                            fields_after_next,
                        ):
                            sign_in_reason = _workday_sign_in_failure_reason(
                                page,
                                allow_generic=False,
                            )
                            create_account_reason = _workday_create_account_failure_reason(page)
                            if (
                                not sign_in_reason
                                and not create_account_reason
                                and _workday_sign_in_field_set(fields_after_next_list)
                            ):
                                continue
                            reason = sign_in_reason or create_account_reason or (
                                f"Save and Continue did not advance the Workday page: {step_before}"
                                if step_before
                                else "Save and Continue did not advance the Workday page"
                            )
                            artifact = _write_submission_evidence(
                                page,
                                payload,
                                None,
                                None,
                                reason,
                            )
                            if artifact:
                                print(f"Application page evidence: {artifact}")
                            all_review.append(
                                {
                                    "label": (
                                        "Candidate account creation"
                                        if reason.startswith("candidate account creation")
                                        else (step_before or "Candidate account sign-in")
                                    ),
                                    "reason": reason,
                                    "sensitive": False,
                                    "blocking": True,
                                }
                            )
                            break
                except Exception as exc:
                    print(f"Could not advance to next page: {exc}")
                    break

            for blocker in profile.get("submission_blockers") or []:
                all_review.append(
                    {
                        "label": str(blocker),
                        "reason": "package truthfulness gate",
                        "sensitive": False,
                        "blocking": True,
                    }
                )
            _restore_application_context_if_external(page, application_url)
            _install_application_navigation_guard(page, application_url)
            all_review = _filter_successful_readback_reviews(all_review, all_filled)
            blocking_review = [item for item in all_review if item.get("blocking", True)]
            captcha_result = {"status": "skipped", "detail": "blocking review fields present"}
            if not blocking_review:
                captcha_result = _solve_captcha_if_configured(page)
            submit = _find_button(page, kind="submit")
            if _is_job_page_apply_button(page, submit):
                submit = None
            print("=== Simplify-style autofill report ===")
            print(f"Detected ATS: {_detect_ats(payload.get('applicationUrl'))}")
            print(f"Pages filled: {pages}")
            print(f"Filled fields ({len(all_filled)}):")
            for field in all_filled:
                suffix = " -> file selected" if field.get("action") == "upload" else ""
                print(
                    f"  - [{field.get('action')}] {_display_text(field.get('label'))}{suffix} "
                    f"| readback={_readback_status(field.get('readback'))}"
                )
            print(f"Review-required ({len(all_review)}):")
            for item in all_review:
                print(f"  - {_display_text(item.get('label'))} ({_display_text(item.get('reason'))})")
            _emit_review_items(all_review)
            if any(
                item.get("reason") == "candidate account creation required"
                for item in blocking_review
            ):
                print(f"{CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX} configured candidate account password is missing")
            if any(
                str(item.get("reason") or "").startswith("candidate account sign-in rejected by Workday")
                for item in blocking_review
            ):
                print(
                    f"{CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX} "
                    "configured candidate account credentials were rejected by Workday"
                )
            if any(
                str(item.get("reason") or "").startswith("candidate account verification required by Workday")
                for item in blocking_review
            ):
                print(
                    f"{CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX} "
                    "candidate account verification is required by Workday"
                )
            print(f"CapMonster CAPTCHA: {captcha_result['status']} ({captcha_result['detail']})")
            print(f"Final submit button present: {submit['text'] if submit else 'none'}")
            print(f"Autofill stats: filled={len(all_filled)} review={len(all_review)}")
            if blocking_review and not review_artifact:
                review_artifact = _write_review_evidence(page, payload, blocking_review)
            if review_artifact and not review_artifact_reported:
                print(f"Review evidence: {review_artifact}")
            if (
                _submit_complete_enabled()
                and not blocking_review
                and submit
                and _captcha_result_blocks_submission(captcha_result)
            ):
                processing_error = (
                    "captcha blocked automatic submission: "
                    f"{captcha_result['status']} ({captcha_result['detail']})"
                )
                artifact = _write_submission_evidence(page, payload, None, None, processing_error)
                print(f"{SUBMISSION_PROCESSING_ERROR_LINE_PREFIX} {processing_error}")
                if artifact:
                    print(f"Submission evidence: {artifact}")
                return 0
            if not submit and not all_filled and not all_review:
                processing_error = _detect_submission_processing_error(page)
                artifact = _write_submission_evidence(page, payload, None, None, processing_error)
                if processing_error:
                    print(f"{SUBMISSION_PROCESSING_ERROR_LINE_PREFIX} {processing_error}")
                    if artifact:
                        print(f"Submission evidence: {artifact}")
                    return 0
            if _submit_complete_enabled() and not blocking_review and submit:
                verification_requested_at_ns = verification_window_started_at_ns
                try:
                    _wait_before_submit(page, skip_delay=(captcha_result.get('status') == 'solved'))
                    _runtime_agent_action(
                        action_runner,
                        "ats_submit_application",
                        "submit",
                        _runtime_submit_policy_context(
                            payload,
                            profile,
                            blocking_review,
                            application_url,
                        ),
                        lambda: _click_button(page, submit),
                    )
                except RuntimeActionDenied:
                    print(SUBMIT_GATE_LINE)
                    return 0
                except Exception as exc:
                    artifact = _write_submission_evidence(page, payload, None, None, f"click failed: {exc}")
                    print(f"{SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX} click failed: {exc}")
                    if artifact:
                        print(f"Submission evidence: {artifact}")
                    return 0
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                _wait_for_submit_settle(page)
                confirmation = _detect_submission_confirmation(page)
                if confirmation:
                    artifact = _write_submission_evidence(page, payload, confirmation)
                    print(f"{SUBMITTED_LINE_PREFIX} {confirmation}")
                    if artifact:
                        print(f"Submission evidence: {artifact}")
                    return 0
                verification = _detect_email_verification_request(page)
                processing_error = _detect_submission_processing_error(page) if not verification else None
                for retry_number in range(1, CAPTCHA_RECOVERY_ATTEMPTS + 1):
                    if not _is_retryable_captcha_error(processing_error):
                        break
                    if not _captcha_retry_should_refill(page, processing_error):
                        retry_refill = {"filled": [], "review": []}
                    else:
                        retry_refill = _runtime_agent_action(
                            action_runner,
                            "ats_fill_fields",
                            "write",
                            {
                                "phase": "captcha_refill",
                                "page_index": pages,
                                "application_url": application_url,
                            },
                            lambda: _fill_page(
                                page,
                                profile,
                                payload.get("resumeFile"),
                                payload.get("coverLetterFile"),
                            ),
                        )
                    _extend_unique_filled(all_filled, retry_refill["filled"])
                    retry_refill_blockers = [
                        item for item in retry_refill["review"] if item.get("blocking", True)
                    ]
                    if retry_refill_blockers:
                        _finalize_post_submit_blockers(
                            page,
                            payload,
                            all_review,
                            retry_refill["review"],
                            all_filled,
                        )
                        return 0
                    if retry_refill["filled"]:
                        print(
                            f"Autofill CAPTCHA retry {retry_number}: "
                            f"rechecked/refilled {len(retry_refill['filled'])} field(s)"
                        )
                    retry_captcha = _solve_captcha_if_configured(page)
                    print(
                        f"CapMonster CAPTCHA retry {retry_number}: "
                        f"{retry_captcha['status']} ({retry_captcha['detail']})"
                    )
                    if retry_captcha["status"] != "solved":
                        processing_error = _captcha_recovery_failure(retry_captcha)
                        break
                    retry_submit = _find_button(page, kind="submit")
                    if not retry_submit:
                        break
                    _wait_before_submit(page, skip_delay=True)
                    _runtime_agent_action(
                        action_runner,
                        "ats_submit_application",
                        "submit",
                        _runtime_submit_policy_context(
                            payload,
                            profile,
                            [],
                            application_url,
                        ),
                        lambda: _click_button(page, retry_submit),
                    )
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeoutError:
                        pass
                    _wait_for_submit_settle(page)
                    confirmation = _detect_submission_confirmation(page)
                    if confirmation:
                        artifact = _write_submission_evidence(page, payload, confirmation)
                        print(f"{SUBMITTED_LINE_PREFIX} {confirmation}")
                        if artifact:
                            print(f"Submission evidence: {artifact}")
                        return 0
                    verification = _detect_email_verification_request(page)
                    processing_error = _detect_submission_processing_error(page) if not verification else None
                code = (
                    _email_verification_code(requested_after_ns=verification_requested_at_ns)
                    if verification
                    else None
                )
                if code and _fill_email_verification_code(page, code):
                    verification = verification or "code field found on page"
                    print(f"Email verification code entered: {verification}")
                    verification_captcha = _solve_captcha_if_configured(page)
                    print(
                        "CapMonster CAPTCHA for verification submit: "
                        f"{verification_captcha['status']} ({verification_captcha['detail']})"
                    )
                    resubmit = _find_button(page, kind="submit")
                    if resubmit and verification_captcha["status"] in {"solved", "none", "skipped"}:
                        _wait_before_submit(page)
                        _runtime_agent_action(
                            action_runner,
                            "ats_submit_application",
                            "submit",
                            _runtime_submit_policy_context(
                                payload,
                                profile,
                                [],
                                application_url,
                            ),
                            lambda: _click_button(page, resubmit),
                        )
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeoutError:
                            pass
                        _wait_for_submit_settle(page)
                        confirmation = _detect_submission_confirmation(page)
                        if confirmation:
                            artifact = _write_submission_evidence(page, payload, confirmation, verification)
                            print(f"{SUBMITTED_LINE_PREFIX} {confirmation}")
                            if artifact:
                                print(f"Submission evidence: {artifact}")
                            return 0
                        processing_error = _detect_submission_processing_error(page)
                        artifact = _write_submission_evidence(page, payload, None, verification, processing_error)
                        if processing_error:
                            print(f"{SUBMISSION_PROCESSING_ERROR_LINE_PREFIX} {processing_error}")
                            if artifact:
                                print(f"Submission evidence: {artifact}")
                            return 0
                        print(f"{SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX} verification code submitted")
                        if artifact:
                            print(f"Submission evidence: {artifact}")
                        return 0
                if verification:
                    artifact = _write_submission_evidence(page, payload, None, verification)
                    print(f"{EMAIL_VERIFICATION_REQUIRED_LINE_PREFIX} {verification}")
                    if artifact:
                        print(f"Submission evidence: {artifact}")
                    return 0
                processing_error = processing_error or _detect_submission_processing_error(page)
                artifact = _write_submission_evidence(page, payload, None, None, processing_error)
                if processing_error:
                    print(f"{SUBMISSION_PROCESSING_ERROR_LINE_PREFIX} {processing_error}")
                    if artifact:
                        print(f"Submission evidence: {artifact}")
                    return 0
                print(f"{SUBMIT_CLICKED_UNCONFIRMED_LINE_PREFIX} clicked {submit['text']}")
                if artifact:
                    print(f"Submission evidence: {artifact}")
                return 0
            print(SUBMIT_GATE_LINE)
            return 0
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _runtime_submit_policy_context(
    payload: Mapping[str, Any],
    profile: Mapping[str, Any],
    blocking_review: list[dict[str, Any]],
    application_url: str,
) -> dict[str, Any]:
    """Build the live submit gate from current page and approved package state."""
    required_resume = bool(
        payload.get("requiredResumePdf")
        or payload.get("resumeSourceDir")
    )
    resume_verified = bool(payload.get("resumeFile")) or not required_resume
    return {
        "phase": "final_submission",
        "application_url": application_url,
        "submit_complete": _submit_complete_enabled(),
        "facts_verified": bool(profile),
        "blocking_review_items": [
            {
                "label": str(item.get("label") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for item in blocking_review
            if item.get("blocking", True)
        ],
        "unapproved_sensitive_fields": [
            str(item.get("label") or "")
            for item in blocking_review
            if item.get("sensitive")
        ],
        "resume_verified": resume_verified,
        "confirmation_required": True,
    }


def run_runtime_script(script_path: str | Path) -> int:
    payload = load_runtime_payload(script_path)
    payload["_runtimeScriptDir"] = str(Path(script_path).parent)
    return run_runtime_payload(payload)


def _submit_complete_enabled() -> bool:
    raw = str(os.getenv("JOB_AGENT_SUBMIT_COMPLETE") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _parse_viewport(raw: str | None) -> dict[str, int]:
    text = str(raw or "").strip().lower()
    match = re.match(r"^(\d{3,5})\s*[x,]\s*(\d{3,5})$", text)
    if not match:
        return {"width": 1365, "height": 900}
    width = min(3840, max(800, int(match.group(1))))
    height = min(2160, max(600, int(match.group(2))))
    return {"width": width, "height": height}


def _browser_context_options() -> dict[str, Any]:
    locale = str(os.getenv("JOB_AGENT_BROWSER_LOCALE") or "en-US").strip() or "en-US"
    timezone_id = str(os.getenv("JOB_AGENT_BROWSER_TIMEZONE") or "America/New_York").strip() or "America/New_York"
    user_agent = str(os.getenv("JOB_AGENT_BROWSER_USER_AGENT") or _DEFAULT_BROWSER_USER_AGENT).strip()
    return {
        "user_agent": user_agent,
        "locale": locale,
        "timezone_id": timezone_id,
        "viewport": _parse_viewport(os.getenv("JOB_AGENT_BROWSER_VIEWPORT")),
        "device_scale_factor": 1,
        "color_scheme": "light",
        "extra_http_headers": {"Accept-Language": f"{locale},en;q=0.9"},
    }


def _install_browser_fingerprint_mitigation(context) -> None:
    try:
        context.add_init_script(
            """(() => {
              try {
                Object.defineProperty(Navigator.prototype, "webdriver", {
                  configurable: true,
                  get: () => undefined,
                });
              } catch (e) {}
              try {
                Object.defineProperty(navigator, "languages", {
                  configurable: true,
                  get: () => ["en-US", "en"],
                });
              } catch (e) {}
              try {
                Object.defineProperty(navigator, "plugins", {
                  configurable: true,
                  get: () => [1, 2, 3, 4, 5],
                });
              } catch (e) {}
              try {
                window.chrome = window.chrome || {};
                window.chrome.runtime = window.chrome.runtime || {};
              } catch (e) {}
              try {
                const capture = window.__jobAgentTurnstileCapture = window.__jobAgentTurnstileCapture || {};
                const install = () => {
                  const api = window.turnstile;
                  if (!api || typeof api.render !== "function" || api.__jobAgentWrapped) return;
                  const originalRender = api.render.bind(api);
                  api.render = function(container, params) {
                    try {
                      const options = params || {};
                      capture.websiteKey = options.sitekey || capture.websiteKey || "";
                      capture.pageAction = options.action || capture.pageAction || "";
                      capture.data = options.cData || options.cdata || capture.data || "";
                      capture.pageData = options.chlPageData || options.pageData || capture.pageData || "";
                      capture.apiJsUrl = document.currentScript && document.currentScript.src || capture.apiJsUrl || "";
                      capture.capturedAt = Date.now();
                    } catch (e) {}
                    return originalRender(container, params);
                  };
                  api.__jobAgentWrapped = true;
                };
                install();
                clearInterval(window.__jobAgentTurnstileCaptureInterval);
                window.__jobAgentTurnstileCaptureInterval = setInterval(install, 25);
                setTimeout(() => clearInterval(window.__jobAgentTurnstileCaptureInterval), 30000);
              } catch (e) {}
            })();"""
        )
    except Exception:
        pass


def _human_submit_delay_ms() -> int:
    raw = str(os.getenv("JOB_AGENT_SUBMIT_HUMAN_DELAY_SECONDS") or "1.5-4.0").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return 0
    match = re.match(r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)$", raw)
    if match:
        low = float(match.group(1))
        high = float(match.group(2))
        if high < low:
            low, high = high, low
    else:
        try:
            low = high = float(raw)
        except ValueError:
            low, high = 1.5, 4.0
    low_ms = max(0, int(low * 1000))
    high_ms = max(low_ms, int(high * 1000))
    if high_ms == low_ms:
        return low_ms
    return low_ms + secrets.randbelow(high_ms - low_ms + 1)


def _wait_before_submit(page, skip_delay: bool = False) -> None:
    delay_ms = 0 if skip_delay else _human_submit_delay_ms()
    if delay_ms <= 0:
        return
    try:
        page.mouse.move(120 + secrets.randbelow(180), 160 + secrets.randbelow(220))
    except Exception:
        pass
    try:
        page.wait_for_timeout(delay_ms)
    except Exception:
        pass


def _captcha_result_blocks_submission(captcha_result: dict[str, str]) -> bool:
    status = str(captcha_result.get("status") or "").strip().lower()
    return bool(status) and status not in {"none", "skipped", "solved"}


def _self_heal_passes() -> int:
    try:
        return min(5, max(1, int(os.getenv("JOB_AGENT_SELF_HEAL_PASSES") or "3")))
    except ValueError:
        return 3


def _new_combobox_progress_deadline() -> float:
    try:
        seconds = float(
            os.getenv("JOB_AGENT_COMBOBOX_NO_PROGRESS_SECONDS") or "20"
        )
    except ValueError:
        seconds = 20.0
    return time.monotonic() + min(120.0, max(5.0, seconds))


def _check_combobox_progress_deadline(
    deadline: float,
    field: dict[str, Any],
) -> None:
    if time.monotonic() < deadline:
        return
    label = str(
        field.get("label")
        or field.get("id")
        or field.get("name")
        or "unlabeled field"
    )
    raise ComboboxNoProgressError(
        f"combobox made no progress before field repair deadline: {label}"
    )


def _extend_unique_filled(target: list[dict[str, Any]], items: list[dict[str, Any]]) -> None:
    seen = {(item.get("label"), item.get("action")) for item in target}
    for item in items:
        key = (item.get("label"), item.get("action"))
        if key not in seen:
            target.append(item)
            seen.add(key)


def _finalize_post_submit_blockers(
    page,
    payload: dict[str, Any],
    all_review: list[dict[str, Any]],
    new_review: list[dict[str, Any]],
    all_filled: list[dict[str, Any]],
) -> str | None:
    seen = {
        (
            item.get("label"),
            item.get("reason"),
            bool(item.get("sensitive", False)),
            bool(item.get("blocking", True)),
        )
        for item in all_review
    }
    for item in new_review:
        key = (
            item.get("label"),
            item.get("reason"),
            bool(item.get("sensitive", False)),
            bool(item.get("blocking", True)),
        )
        if key not in seen:
            all_review.append(item)
            seen.add(key)
    blocking_review = [item for item in all_review if item.get("blocking", True)]
    artifact = _write_review_evidence(page, payload, blocking_review)
    _emit_review_items(blocking_review)
    print(f"Autofill stats: filled={len(all_filled)} review={len(all_review)}")
    print("Submit gate: STOPPED before final Submit because blocking review fields remain.")
    if artifact:
        print(f"Review evidence: {artifact}")
    return artifact


def _has_retryable_blocking_review(review: list[dict[str, Any]]) -> bool:
    """Retry when any blocker is transient without retrying protected answers."""
    blocking = [item for item in review if item.get("blocking", True)]
    if not blocking:
        return False
    non_retryable_markers = (
        "needs saved answer",
        "manual selection",
        "combobox made no progress",
        "no combobox option matches saved answer",
        "no button dropdown option matches saved answer",
        "profile has no approved",
        "user-authored",
        "truthfulness gate",
    )
    return any(
        not any(marker in _norm(item.get("reason") or "") for marker in non_retryable_markers)
        and (
            not item.get("sensitive")
            or _norm(item.get("reason") or "").startswith("fill error")
        )
        for item in blocking
    )


def _close_workday_open_menus(page) -> bool:
    if "myworkdayjobs.com" not in str(getattr(page, "url", "") or "").lower():
        return False
    closed = False
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(150)
        closed = True
    except Exception:
        pass
    try:
        viewport = page.viewport_size or {}
        width = int(viewport.get("width") or 1280)
        height = int(viewport.get("height") or 900)
        page.mouse.click(max(5, width - 20), max(5, min(height - 20, height // 2)))
        page.wait_for_timeout(250)
        closed = True
    except Exception:
        pass
    try:
        expanded = page.evaluate(
            """() => Array.from(document.querySelectorAll('[aria-expanded="true"]'))
              .filter((node) => node.offsetParent || node.getClientRects().length)
              .length"""
        )
        if expanded:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            closed = True
    except Exception:
        pass
    return closed


def _detect_submission_confirmation(page) -> str | None:
    try:
        state = page.evaluate(
            """() => ({
              url: window.location.href,
              title: document.title || "",
              text: (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 60000),
            })"""
        ) or {"url": "", "title": "", "text": ""}
    except Exception:
        return None
    raw_text = " ".join(str(state.get(key) or "") for key in ["url", "title", "text"]).lower()
    localized_patterns = [
        "\u7533\u8bf7\u5df2\u63d0\u4ea4",
        "\u60a8\u7684\u7533\u8bf7\u5df2\u6210\u529f\u63d0\u4ea4",
        "\u63d0\u4ea4\u6210\u529f",
    ]
    for pattern in localized_patterns:
        if pattern in raw_text:
            url = _safe_evidence_url(str(state.get("url") or ""))
            return f"matched localized submission confirmation at {url or 'current page'}"
    text = _norm(raw_text)
    patterns = [
        "thank you for applying",
        "thanks for applying",
        "thanks so much for applying",
        "application success",
        "application submitted",
        "application has been submitted",
        "successfully submitted",
        "submitted thanks",
        "application received",
        "we have received your application",
        "received your application",
        "your application has been received",
        "we ll be in touch",
        "we will be in touch",
    ]
    for pattern in patterns:
        if pattern in text:
            url = _safe_evidence_url(str(state.get("url") or ""))
            return f"matched '{pattern}' at {url or 'current page'}"
    return None


def _detect_email_verification_request(page) -> str | None:
    try:
        state = page.evaluate(
            """() => ({
              url: window.location.href,
              title: document.title || "",
              text: (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 60000),
            })"""
        )
    except Exception:
        return None
    text = _norm(" ".join(str(state.get(key) or "") for key in ["url", "title", "text"]))
    patterns = [
        "security code",
        "verification code",
        "enter the code",
        "copy and paste this code",
        "email verification",
        "verify your email",
        "one time code",
        "one time password",
    ]
    for pattern in patterns:
        if pattern in text:
            url = _safe_evidence_url(str(state.get("url") or ""))
            return f"matched '{pattern}' at {url or 'current page'}"
    return None


def _detect_submission_processing_error(page) -> str | None:
    try:
        state = page.evaluate(
            """() => ({
              url: window.location.href,
              title: document.title || "",
              text: (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 60000),
              recaptcha: Boolean(document.querySelector([
                'iframe[src*="recaptcha"]',
                'iframe[src*="captcha"]',
                'iframe[src*="hcaptcha"]',
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="arkoselabs"]',
                'iframe[src*="funcaptcha"]',
                'iframe[title*="cloudflare" i]',
                'iframe[title*="challenge" i]',
                'iframe[title*="verification" i]',
                'iframe[title*="captcha" i]',
                '.g-recaptcha',
                '.cf-turnstile',
                '[name="cf-turnstile-response"]',
                '[class*="turnstile" i]',
                '[id*="turnstile" i]',
                '[class*="recaptcha" i]',
                '[class*="captcha" i]',
                '[id*="captcha" i]',
              ].join(","))),
            })"""
        )
    except Exception:
        return None
    text = _norm(" ".join(str(state.get(key) or "") for key in ["url", "title", "text"]))
    patterns = [
        "flagged as possible spam",
        "application was flagged as spam",
        "too many requests",
        "rate limited",
        "rate limit",
        "http 429",
        "status 429",
        "your form needs corrections",
        "missing entry for required field",
        "there was an error processing your application",
        "error processing your application",
        "please try again",
        "please complete the recaptcha",
        "captcha verification failed",
        "captcha token expired",
        "invalid captcha",
        "verify you are human",
        "cf turnstile",
    ]
    for pattern in patterns:
        if pattern in text:
            suffix = " with recaptcha present" if state.get("recaptcha") else ""
            url = _safe_evidence_url(str(state.get("url") or ""))
            return f"matched '{pattern}' at {url or 'current page'}{suffix}"
    if state.get("recaptcha"):
        url = _safe_evidence_url(str(state.get("url") or ""))
        return f"captcha present at {url or 'current page'}"
    return None


def _is_retryable_captcha_error(error: str | None) -> bool:
    normalized = _norm(error)
    if not normalized:
        return False
    if any(
        marker in normalized
        for marker in (
            "possible spam",
            "flagged as spam",
            "too many requests",
            "rate limit",
            "rate-limit",
            "rate limited",
            "rate-limited",
            "http 429",
            "status 429",
        )
    ):
        return False
    return any(
        marker in normalized
        for marker in (
            "captcha present at ",
            "please complete the recaptcha",
            "captcha verification failed",
            "captcha token expired",
            "invalid captcha",
            "verify you are human",
            "cf turnstile",
        )
    )


def _captcha_recovery_failure(captcha_result: dict[str, str]) -> str:
    status = str(captcha_result.get("status") or "unknown")
    detail = str(captcha_result.get("detail") or "no solver detail")
    return f"captcha recovery failed: {status} ({detail})"


def _is_ambient_captcha_presence(error: str | None) -> bool:
    """Return whether a page merely contains a CAPTCHA container, not an error."""
    return _norm(error).startswith("captcha present at ")


def _captcha_retry_should_refill(page, processing_error: str | None) -> bool:
    """Return whether a CAPTCHA retry should re-fill the current form.

    Some ATS pages, notably Greenhouse embeds, can reject a CAPTCHA token and
    re-render the application with empty required fields while the only
    detectable processing signal remains an ambient CAPTCHA iframe.  In that
    state retrying only the token repeatedly resubmits an empty form.  Keep the
    old no-refill path for pure ambient CAPTCHA presence, but re-fill when the
    live page shows required-field errors or key required controls are empty.
    """
    if not _is_ambient_captcha_presence(processing_error):
        return True
    if _visible_form_validation_errors(page):
        return True
    try:
        state = page.evaluate(
            """() => {
              const visible = (node) => {
                if (!node || node.getAttribute("aria-hidden") === "true") return false;
                const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
                if (style && (style.display === "none" || style.visibility === "hidden")) return false;
                if (node.offsetParent) return true;
                const rects = node.getClientRects ? Array.from(node.getClientRects()) : [];
                return rects.some((rect) => rect.width > 0 && rect.height > 0);
              };
              const norm = (text) => String(text || "").toLowerCase().replace(/\\s+/g, " ").trim();
              const labelFor = (control) => {
                const explicit = control.id
                  ? Array.from(document.querySelectorAll("label")).find((label) =>
                      label.htmlFor === control.id || label.getAttribute("for") === control.id
                    )
                  : null;
                const wrapping = control.closest && control.closest("label");
                const container = control.closest && control.closest("div, li, fieldset, section");
                return norm(
                  control.getAttribute("aria-label")
                  || control.getAttribute("placeholder")
                  || (explicit && explicit.textContent)
                  || (wrapping && wrapping.textContent)
                  || (container && container.textContent)
                  || control.name
                  || control.id
                );
              };
              const valueFor = (control) => {
                if ((control.type || "").toLowerCase() === "file") return control.files && control.files.length ? "file" : "";
                if (control.tagName === "SELECT") return control.value || "";
                return control.value || control.getAttribute("value") || "";
              };
              const requiredControls = Array.from(document.querySelectorAll(
                "input, textarea, select, [role='combobox'], [aria-required='true']"
              )).filter((node) => visible(node) && (
                node.required
                || node.getAttribute("aria-required") === "true"
                || /\\*/.test(labelFor(node))
              ));
              let emptyKeyRequired = 0;
              let missingRequiredResume = false;
              for (const control of requiredControls) {
                const label = labelFor(control);
                const value = valueFor(control);
                const isEmpty = !String(value || "").trim();
                if (isEmpty && /(first name|last name|email|phone|location|city)/.test(label)) emptyKeyRequired += 1;
                if (isEmpty && /(resume|cv|curriculum vitae)/.test(label)) missingRequiredResume = true;
              }
              const text = norm(document.body && document.body.innerText || "");
              const requiredErrorText = /(is required|this field is required|please enter your location|resume\\/cv is required|required field)/.test(text);
              return {
                url: window.location.href,
                requiredErrorText,
                emptyKeyRequired,
                missingRequiredResume,
              };
            }"""
        )
    except Exception:
        return False
    if not isinstance(state, dict):
        return False
    if state.get("missingRequiredResume"):
        return True
    if state.get("requiredErrorText") and int(state.get("emptyKeyRequired") or 0) > 0:
        return True
    return False


def _email_verification_code(*, requested_after_ns: int | None = None) -> str | None:
    """Return an explicit code or a file value created for this request.

    A shared code file can retain a code from an earlier ATS session. Explicit
    environment values are intentional operator overrides; file values must be
    written after the current verification request was observed.
    """
    for key in [
        "JOB_AGENT_EMAIL_VERIFICATION_CODE",
        "JOB_AGENT_GREENHOUSE_SECURITY_CODE",
        "JOB_AGENT_SECURITY_CODE",
    ]:
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    try:
        wait_seconds = max(0.0, float(os.getenv("JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS") or "120"))
    except ValueError:
        wait_seconds = 120.0
    code_file = str(os.getenv("JOB_AGENT_EMAIL_VERIFICATION_CODE_FILE") or "").strip()
    if code_file:
        minimum_mtime_ns = requested_after_ns if requested_after_ns is not None else time.time_ns()
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                path = Path(code_file)
                value = path.read_text().strip()
                if path.stat().st_mtime_ns < minimum_mtime_ns:
                    value = ""
            except OSError:
                value = ""
            if value:
                return value
            if time.monotonic() >= deadline:
                return None
            time.sleep(2)

    gmail_token_file = str(os.getenv("JOB_AGENT_GMAIL_TOKEN_FILE") or "").strip()
    default_token_path = Path(".job-agent-secrets") / "gmail-token.json"
    if not gmail_token_file and default_token_path.is_file():
        gmail_token_file = str(default_token_path)
    if gmail_token_file:
        try:
            try:
                gmail_grace_seconds = max(
                    0.0,
                    float(os.getenv("JOB_AGENT_GMAIL_VERIFICATION_GRACE_SECONDS") or "0"),
                )
            except ValueError:
                gmail_grace_seconds = 0.0
            requested_after_ms = max(
                0,
                ((requested_after_ns or time.time_ns()) // 1_000_000) - int(gmail_grace_seconds * 1000),
            )
            return wait_for_verification_code(
                gmail_token_file,
                requested_after_ms=requested_after_ms,
                wait_seconds=wait_seconds,
                query=str(
                    os.getenv("JOB_AGENT_GMAIL_VERIFICATION_QUERY")
                    or GREENHOUSE_SECURITY_CODE_QUERY
                ),
            )
        except GmailVerificationError as exc:
            print(f"Email verification inbox error: {exc}")
    return None


def _email_verification_link(
    *,
    requested_after_ns: int | None = None,
    query: str,
    url_pattern: str,
) -> str | None:
    try:
        wait_seconds = max(0.0, float(os.getenv("JOB_AGENT_EMAIL_VERIFICATION_WAIT_SECONDS") or "120"))
    except ValueError:
        wait_seconds = 120.0
    gmail_token_file = str(os.getenv("JOB_AGENT_GMAIL_TOKEN_FILE") or "").strip()
    default_token_path = Path(".job-agent-secrets") / "gmail-token.json"
    if not gmail_token_file and default_token_path.is_file():
        gmail_token_file = str(default_token_path)
    if not gmail_token_file:
        return None
    try:
        requested_after_ms = max(
            0,
            ((requested_after_ns or time.time_ns()) // 1_000_000) - 60_000,
        )
        return wait_for_verification_link(
            gmail_token_file,
            requested_after_ms=requested_after_ms,
            wait_seconds=wait_seconds,
            query=query,
            url_pattern=url_pattern,
        )
    except GmailVerificationError as exc:
        print(f"Email verification inbox error: {exc}")
    return None


def _fill_email_verification_code(page, code: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{3,20}", code.strip()):
        return False
    characters = list(code.strip())
    # OTP widgets are often React-controlled, so assigning ``input.value``
    # (even with synthetic events) can leave the form submit button disabled.
    # Prefer Playwright's native per-cell fill path when the page exposes it.
    try:
        cells = page.locator('input[id^="security-input-"]')
        if cells.count() >= len(characters):
            cells.first.click()
            for index, character in enumerate(characters):
                cells.nth(index).fill(character)
            try:
                cells.nth(len(characters) - 1).press("Tab")
            except Exception:
                pass
            if all(cells.nth(index).input_value() == character for index, character in enumerate(characters)):
                return True
    except Exception:
        pass
    return bool(
        page.evaluate(
            """(code) => {
              const visible = (node) => {
                if (!node) return false;
                if (node.offsetParent) return true;
                const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
                return rects && rects.length > 0;
              };
              const labelFor = (control) => {
                const parts = [
                  control.id || "",
                  control.name || "",
                  control.getAttribute("aria-label") || "",
                  control.getAttribute("placeholder") || "",
                  control.getAttribute("autocomplete") || "",
                ];
                if (control.id) {
                  document.querySelectorAll("label").forEach((label) => {
                    if (label.htmlFor === control.id || label.getAttribute("for") === control.id) {
                      parts.push(label.textContent || "");
                    }
                  });
                }
                const wrapper = control.closest("label,[data-automation-id^='formField-'],.field,.form-field,.application-question");
                if (wrapper) parts.push(wrapper.textContent || "");
                return parts.join(" ").replace(/\\s+/g, " ").trim().toLowerCase();
              };
              const candidates = Array.from(document.querySelectorAll("input, textarea"))
                .filter((node) => visible(node))
                .filter((node) => !["hidden", "submit", "button", "file", "checkbox", "radio"].includes((node.getAttribute("type") || "").toLowerCase()))
                .map((node) => {
                  const text = labelFor(node);
                  let score = 0;
                  if (/security\\s+code|verification\\s+code|confirmation\\s+code|one[-\\s]?time\\s+(code|password)|email\\s+code/.test(text)) score += 100;
                  if (/\\bcode\\b/.test(text)) score += 25;
                  if ((node.getAttribute("autocomplete") || "").toLowerCase() === "one-time-code") score += 100;
                  return { node, score };
                })
                .filter((item) => item.score > 0)
                .sort((left, right) => right.score - left.score);
              const setValue = (target, value) => {
                const proto = target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                if (descriptor && descriptor.set) descriptor.set.call(target, value);
                else target.value = value;
                target.dispatchEvent(new Event("input", { bubbles: true }));
                target.dispatchEvent(new Event("change", { bubbles: true }));
              };
              const singleCharacterInputs = Array.from(document.querySelectorAll("input"))
                .filter((node) => visible(node))
                .filter((node) => node.maxLength === 1 || /security-input-\\d+/i.test(node.id || ""));
              singleCharacterInputs.sort((left, right) => {
                const leftIndex = Number((String(left.id || "").match(/(\\d+)$/) || [])[1]);
                const rightIndex = Number((String(right.id || "").match(/(\\d+)$/) || [])[1]);
                return leftIndex - rightIndex;
              });
              if (singleCharacterInputs.length >= code.length) {
                Array.from(code).forEach((character, index) => setValue(singleCharacterInputs[index], character));
                return Array.from(code).every((character, index) => singleCharacterInputs[index].value === character);
              }
              const target = candidates[0] && candidates[0].node;
              if (!target) return false;
              setValue(target, code);
              return true;
            }""",
            code.strip(),
        )
    )


def _write_submission_evidence(
    page,
    payload: dict[str, Any],
    confirmation: str | None,
    verification: str | None = None,
    processing_error: str | None = None,
) -> str | None:
    directory = payload.get("_runtimeScriptDir")
    if not directory:
        return None
    if confirmation:
        filename = "submission-confirmation.txt"
    elif processing_error:
        filename = "submission-processing-error.txt"
    elif verification:
        filename = "email-verification-required.txt"
    else:
        filename = "submission-click-unconfirmed.txt"
    out = Path(directory) / filename
    try:
        state = page.evaluate(
            """() => ({
              url: window.location.href,
              title: document.title || "",
              text: (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 60000),
            })"""
        )
        screenshot = None
        try:
            screenshot_path = Path(directory) / filename.replace(".txt", ".png")
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot = str(screenshot_path)
        except Exception:
            screenshot = None
        text = _redact_evidence_text(str(state.get("text") or ""))
        url = _safe_evidence_url(str(state.get("url") or ""))
        snippets = _evidence_snippets(
            text,
            [
                "thank you",
                "thanks for applying",
                "application submitted",
                "application received",
                "\u7533\u8bf7\u5df2\u63d0\u4ea4",
                "\u63d0\u4ea4\u6210\u529f",
                "security code",
                "verification code",
                "resubmit your application",
                "error",
                "invalid",
                "expired",
                "required",
            ],
        )
        out.write_text(
            "\n".join(
                [
                    f"confirmation: {_redact_evidence_text(confirmation) if confirmation else 'not detected'}",
                    f"email_verification: {_redact_evidence_text(verification) if verification else 'not detected'}",
                    f"processing_error: {_redact_evidence_text(processing_error) if processing_error else 'not detected'}",
                    f"url: {url}",
                    f"title: {state.get('title') or ''}",
                    f"screenshot: {screenshot or 'not captured'}",
                    "",
                    "signal_snippets:",
                    snippets or "not detected",
                    "",
                    "page_text_head:",
                    text[:8000],
                    "",
                    "page_text_tail:",
                    text[-8000:],
                ]
            )
        )
        return str(out)
    except Exception:
        return None


def _write_review_evidence(
    page,
    payload: dict[str, Any],
    review_items: list[dict[str, Any]],
) -> str | None:
    directory = payload.get("_runtimeScriptDir")
    if not directory:
        return None
    out = Path(directory) / "review-required.txt"
    try:
        state = page.evaluate(
            """() => ({
              url: window.location.href,
              title: document.title || "",
              text: (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 60000),
            })"""
        )
        screenshot = None
        try:
            screenshot_path = Path(directory) / "review-required.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot = str(screenshot_path)
        except Exception:
            screenshot = None
        text = _redact_evidence_text(str(state.get("text") or ""))
        url = _safe_evidence_url(str(state.get("url") or ""))
        serialized = [
            json.dumps(
                {
                    "label": item.get("label") or "",
                    "reason": item.get("reason") or "",
                    "sensitive": bool(item.get("sensitive", False)),
                    "blocking": bool(item.get("blocking", True)),
                },
                ensure_ascii=True,
            )
            for item in review_items
        ]
        out.write_text(
            "\n".join(
                [
                    f"review_count: {len(review_items)}",
                    f"url: {url}",
                    f"title: {state.get('title') or ''}",
                    f"screenshot: {screenshot or 'not captured'}",
                    "",
                    "review_items:",
                    *serialized,
                    "",
                    "page_text_head:",
                    text[:8000],
                    "",
                    "page_text_tail:",
                    text[-8000:],
                ]
            )
        )
        return str(out)
    except Exception:
        return None


def _emit_review_items(review_items: list[dict[str, Any]]) -> None:
    for item in review_items:
        print(
            f"{REVIEW_ITEM_LINE_PREFIX} "
            + json.dumps(
                {
                    "label": item.get("label") or "",
                    "reason": item.get("reason") or "",
                    "sensitive": bool(item.get("sensitive", False)),
                    "blocking": bool(item.get("blocking", True)),
                },
                ensure_ascii=True,
            )
        )


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())).strip()


def _has_phrase(text: str, phrase: str) -> bool:
    normalized_phrase = _norm(phrase)
    return bool(normalized_phrase) and f" {normalized_phrase} " in f" {_norm(text)} "


def _evidence_snippets(text: str, terms: list[str], window: int = 700) -> str:
    lowered = text.lower()
    snippets = []
    for term in terms:
        idx = lowered.find(term)
        if idx < 0:
            continue
        start = max(0, idx - window)
        end = min(len(text), idx + len(term) + window)
        snippet = text[start:end].strip()
        if snippet and snippet not in snippets:
            snippets.append(f"[{term}] {snippet}")
    return "\n\n".join(snippets[:8])


def _is_sensitive(label: str) -> bool:
    normalized = _norm(label)
    if "legal" in normalized and any(token in normalized.split() for token in ["name", "first", "last", "middle"]):
        return False
    return any(_norm(keyword) in normalized for keyword in SENSITIVE)


def _is_email_verification_field(label: str) -> bool:
    normalized = _norm(label)
    return any(
        phrase in normalized
        for phrase in (
            "security code",
            "verification code",
            "one time code",
            "one time password",
            "8 character code",
            "8 character security code",
        )
    )


def _same_required_field(left: str, right: str) -> bool:
    left_normalized = _norm(left)
    right_normalized = _norm(right)
    return bool(left_normalized and right_normalized) and (
        left_normalized == right_normalized
        or left_normalized in right_normalized
        or right_normalized in left_normalized
    )


def _retain_unresolved_control_reviews(
    review: list[dict[str, Any]], findings: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Drop only stale combobox failures superseded by a final DOM audit."""
    retained: list[dict[str, Any]] = []
    for item in review:
        reason = str(item.get("reason") or "")
        stale_combobox_failure = (
            reason == "fill error: no combobox option matches saved answer"
            or reason.startswith(
                "fill error: combobox made no progress before field repair deadline"
            )
        )
        if (
            item.get("blocking", True)
            and stale_combobox_failure
            and not any(
                _same_required_field(str(item.get("label") or ""), str(finding.get("label") or ""))
                for finding in findings
            )
        ):
            continue
        retained.append(item)
    return retained

def _has_successful_fill_readback(label: str, filled: list[dict[str, Any]] | None) -> bool:
    for item in filled or []:
        if not _same_required_field(label, str(item.get("label") or "")):
            continue
        readback = item.get("readback")
        if readback in {None, False, "", "readback-error"}:
            continue
        if isinstance(readback, str) and readback.startswith("selected: "):
            return True
        if readback == "file-selected":
            return True
        if str(readback).strip():
            return True
    return False


def _invalid_finding_can_use_successful_readback(label: str) -> bool:
    normalized = _norm(label)
    return (
        "email" in normalized
        or ("full time" in normalized and "internship" in normalized)
        or "when will you graduate" in normalized
    )


def _filter_successful_readback_reviews(
    review: list[dict[str, Any]], filled: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    stale_reasons = {
        "browser reports field as invalid",
        "required field remains empty after fill",
    }
    return [
        item
        for item in review
        if not (
            item.get("blocking", True)
            and str(item.get("reason") or "") in stale_reasons
            and _invalid_finding_can_use_successful_readback(str(item.get("label") or ""))
            and _has_successful_fill_readback(str(item.get("label") or ""), filled)
        )
    ]


def _is_office_location_group_label(label: str) -> bool:
    normalized = _norm(label)
    return (
        "which office location" in normalized
        or "office locations" in normalized
        or "location s are you interested" in normalized
    )


def _has_checked_office_location(filled: list[dict[str, Any]] | None) -> bool:
    for item in filled or []:
        label = str(item.get("label") or "")
        if not _looks_like_location_checkbox_option(label):
            continue
        readback = str(item.get("readback") or "").strip().lower()
        action = str(item.get("action") or "").strip().lower()
        if action in {"check", "checkmany"} and readback in {"checked", "selected", "true"}:
            return True
        if action in {"check", "checkmany"} and readback.startswith("selected:"):
            return True
    return False


def _append_required_audit(
    review: list[dict[str, Any]],
    findings: list[dict[str, str]],
    *,
    filled: list[dict[str, Any]] | None = None,
) -> None:
    for finding in findings:
        label = str(finding.get("label") or "required field")
        if _is_email_verification_field(label):
            continue
        if (
            (
                str(finding.get("reason") or "") == "required field remains empty after fill"
                or (
                    str(finding.get("reason") or "") == "browser reports field as invalid"
                    and _invalid_finding_can_use_successful_readback(label)
                )
            )
            and _has_successful_fill_readback(label, filled)
        ):
            continue
        if _is_office_location_group_label(label) and _has_checked_office_location(filled):
            continue
        if any(_same_required_field(label, str(item.get("label") or "")) for item in review):
            continue
        review.append(
            {
                "label": label,
                "reason": finding.get("reason") or "required field remains empty after fill",
                "sensitive": _is_sensitive(label),
                "blocking": True,
            }
        )


def _find_answer(label: str, answers: dict[str, Any]) -> Any | None:
    label_norm = _norm(label)
    best: Any | None = None
    best_score = 0.0
    for key, value in (answers or {}).items():
        key_norm = _norm(key)
        if not label_norm or not key_norm or _norm(value) in PLACEHOLDER_ANSWERS:
            continue
        if label_norm == key_norm:
            score = 1.0
        elif label_norm in key_norm or key_norm in label_norm:
            score = 0.8
        else:
            left = set(label_norm.split())
            right = set(key_norm.split())
            common = len(left & right)
            score = min(common / max(1, len(left)), common / max(1, len(right)))
        if score > best_score:
            best_score = score
            best = value
    return best if best_score >= 0.6 else None


def _requires_user_authored_answer(label: str, profile: dict[str, Any]) -> bool:
    combined = " ".join(
        str(part or "")
        for part in [
            label,
            profile.get("target_company"),
            profile.get("target_title"),
        ]
    )
    normalized = _norm(combined)
    if any(
        phrase in normalized
        for phrase in [
            "do not use llm",
            "do not use llms",
            "do not use ai",
            "without llm assistance",
            "without ai assistance",
            "without using ai",
        ]
    ):
        return True
    return bool(profile.get("application_requires_user_authored_answers")) and any(
        token in normalized for token in ["why", "essay", "written", "answer", "question"]
    )


def _source_url_looks_like_company_careers_site(source_url: Any, company: Any) -> bool:
    raw_url = str(source_url or "").strip()
    raw_company = str(company or "").strip()
    if not raw_url or not raw_company:
        return False
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return False
    domain = _norm(parsed.netloc)
    if not domain or ("career" not in domain and "job" not in domain):
        return False
    tokens = [token for token in _norm(raw_company).split() if len(token) >= 4]
    if not tokens:
        return False
    required_hits = 1 if len(tokens) == 1 else 2
    return sum(1 for token in tokens if token in domain) >= required_hits


def _preferred_source_answer(
    label: str,
    profile: dict[str, Any],
    answers: dict[str, Any],
) -> str | None:
    saved_source = (
        profile.get("application_source")
        or profile.get("source_of_application")
        or _find_answer(label, answers)
        or answers.get("How did you hear about this opportunity?")
        or answers.get("How did you hear about us?")
    )
    company = str(profile.get("target_company") or "").strip()
    source_kind = _norm(
        profile.get("application_source_kind")
        or profile.get("job_source")
        or profile.get("source")
    )
    source_url = (
        profile.get("application_source_url")
        or profile.get("job_source_url")
        or profile.get("source_url")
    )
    source_url_lower = str(source_url or "").lower()
    if not saved_source:
        if (
            "greenhouse" in source_kind
            or "lever" in source_kind
            or "ashby" in source_kind
            or "job-boards.greenhouse.io" in source_url_lower
            or "boards.greenhouse.io" in source_url_lower
            or "jobs.lever.co" in source_url_lower
            or "jobs.ashbyhq.com" in source_url_lower
            or _source_url_looks_like_company_careers_site(source_url, company)
        ):
            return "Company website"
        return None
    answer = str(saved_source)
    if ">" in answer:
        return answer
    if _norm(company) in {"xai", "spacexai"} and _is_company_website_answer(answer):
        return "Company careers page / website"
    if (
        company
        and _is_company_website_answer(answer)
        and (
            "official careers" in source_kind
            or _source_url_looks_like_company_careers_site(source_url, company)
        )
    ):
        return f"Job Board > {company} Job Board"
    # When the application originates from a company-hosted Workday careers
    # URL, "Career Website" is more accurate than the generic "Job Board".
    # Some Workday source prompts treat "Job Board" as a parent category and
    # force an arbitrary third-party leaf source such as Career Builder.
    if company and ("myworkdayjobs.com" in source_url_lower or "workdayjobs.com" in source_url_lower):
        return "Career Website"
    return answer


def _is_workday_application_url(profile: dict[str, Any]) -> bool:
    application_url = str(profile.get("_application_url") or "").lower()
    source_url = str(
        profile.get("application_source_url")
        or profile.get("job_source_url")
        or profile.get("source_url")
        or ""
    ).lower()
    source_kind = _norm(
        profile.get("application_source_kind")
        or profile.get("job_source")
        or profile.get("source")
    )
    return (
        "myworkdayjobs.com" in application_url
        or "workdayjobs.com" in application_url
        or "myworkdayjobs.com" in source_url
        or "workdayjobs.com" in source_url
        or source_kind == "workday"
    )


def _target_application_country(profile: dict[str, Any]) -> str | None:
    target_country = str(profile.get("target_country") or "").strip()
    if target_country:
        return target_country
    target_location = _norm(profile.get("target_location") or "")
    if any(token in target_location for token in ("united states", "usa", "u s", "u s a")):
        return "United States"
    if "canada" in target_location:
        return "Canada"
    if "united kingdom" in target_location or "uk" in target_location:
        return "United Kingdom"
    return _infer_country(profile)


def _sponsorship_countries_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        "require sponsorship" in normalized
        and ("locations or countries" in normalized or "all locations" in normalized or "all countries" in normalized)
        and ("separate each response" in normalized or "comma" in normalized or "not applicable" in normalized)
    ):
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    if saved is not None:
        return str(saved)
    sponsorship = (
        _approved_sensitive_entry_answer(profile, "sponsorship")
        or str((profile.get("work_authorization_by_country") or {}).get("requires_sponsorship") or "")
    )
    if _truthy_answer(sponsorship):
        return _target_application_country(profile) or "United States"
    return "N/A"


def _current_based_country_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if (
        "currently based in the united states" in normalized
        or "currently based in the u s" in normalized
        or "currently based in us" in normalized
    ):
        candidate_country = _norm(_infer_country(profile) or "")
        if candidate_country in {
            "united states",
            "united states of america",
            "usa",
            "us",
        }:
            return "Yes"
        if str(profile.get("country") or "").strip():
            return "No"
        return None
    if not (
        "currently based in any of these countries" in normalized
        or ("countries where we are accepting applications" in normalized and "currently based" in normalized)
    ):
        return None
    return _target_application_country(profile) or _infer_country(profile)


def _other_countries_location_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        "no suitable positions" in normalized
        and ("u s" in normalized or "us" in normalized or "united states" in normalized)
        and "open to positions in other countries" in normalized
    ):
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    if saved is not None:
        return str(saved)
    return "No"


def _conflict_of_interest_screening_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not ("conflict of interest" in normalized or "conflicts of interest" in normalized):
        return None
    if not any(
        token in normalized
        for token in (
            "outside employment",
            "financial interest",
            "customer",
            "business partner",
            "supplier",
            "competitor",
            "family member",
            "close personal relationship",
            "government official",
            "regulatory authority",
            "retain hpe business",
        )
    ):
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    return str(saved) if saved is not None else "No"


def _government_public_employment_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        ("government" in normalized or "public institution" in normalized or "public sector" in normalized)
        and ("employment experience" in normalized or "employee" in normalized or "contractor" in normalized or "consultant" in normalized)
        and ("federal" in normalized or "state" in normalized or "local" in normalized or "public institution" in normalized)
    ):
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    return str(saved) if saved is not None else "No"


def _is_listed_country_status_question(label: str) -> bool:
    normalized = _norm(label)
    return (
        ("countries listed" in normalized or "countries listed below" in normalized)
        and ("citizen" in normalized or "passport" in normalized or "dual citizenship" in normalized)
        and (
            "permanent resident" in normalized
            or "refugee" in normalized
            or "asylum" in normalized
            or "citizenship status" in normalized
        )
    )


def _listed_country_status_answer(label: str, profile: dict[str, Any]) -> str | None:
    if not _is_listed_country_status_question(label):
        return None
    answers = profile.get("answers") or {}
    saved = _find_answer(label, answers)
    if saved is not None:
        return str(saved)
    for key in (
        "listed_country_status",
        "restricted_country_status",
        "restricted_country_citizenship",
        "export_control_country_status",
    ):
        approved = _approved_sensitive_entry_answer(profile, key)
        if approved is not None:
            return str(approved)

    listed_countries = {
        "armenia",
        "azerbaijan",
        "belarus",
        "burma",
        "myanmar",
        "cambodia",
        "china",
        "china prc",
        "prc",
        "cuba",
        "georgia",
        "hong kong",
        "iran",
        "iraq",
        "kazakhstan",
        "kyrgyzstan",
        "laos",
        "libya",
        "macao",
        "macau",
        "moldova",
        "mongolia",
        "nicaragua",
        "north korea",
        "dprk",
        "russia",
        "sudan",
        "syria",
        "tajikistan",
        "turkmenistan",
        "ukraine",
        "crimea",
        "uzbekistan",
        "venezuela",
        "vietnam",
        "yemen",
    }
    raw_values: list[Any] = []
    for key in ("country_of_citizenship", "citizenship_country", "nationality", "nationalities", "citizenships"):
        value = profile.get(key)
        if isinstance(value, list):
            raw_values.extend(value)
        elif value:
            raw_values.append(value)
    citizenship_entry = (profile.get("sensitive_answers") or {}).get("citizenship")
    if isinstance(citizenship_entry, dict) and citizenship_entry.get("approved"):
        answer = str(citizenship_entry.get("answer") or "").strip()
        if answer and _norm(answer) not in {"yes", "no", "true", "false", "1", "0"}:
            raw_values.append(answer)
    normalized_values = {_norm(value) for value in raw_values if _norm(value)}
    if not normalized_values:
        return None
    if any(any(country == value or country in value or value in country for country in listed_countries) for value in normalized_values):
        return "Yes"
    return "No"


def _is_phone_device_type_field(field_or_label: str | dict[str, Any]) -> bool:
    if isinstance(field_or_label, dict):
        text = " ".join(
            str(field_or_label.get(key) or "")
            for key in ("label", "id", "name", "ariaLabel", "automationId")
        )
    else:
        text = str(field_or_label or "")
    normalized = _norm(text.replace("-", " "))
    return (
        "phone device type" in normalized
        or "phone type" in normalized
        or "phone device" in normalized
    )


def _workday_phone_device_type_answer(
    field_or_label: str | dict[str, Any],
    profile: dict[str, Any],
) -> str | None:
    if not _is_workday_application_url(profile) or not _is_phone_device_type_field(field_or_label):
        return None
    if isinstance(field_or_label, dict):
        option_labels = [_norm(_option_text(option)) for option in field_or_label.get("options") or []]
        if "primary" in option_labels:
            return "Primary"
    # Workday candidate contact sections frequently expose the single phone
    # device option as "Primary" instead of "Mobile".
    return "Primary"


def _profile_evidence_text(profile: dict[str, Any]) -> str:
    return _norm(
        json.dumps(
            {
                "summary": profile.get("summary"),
                "skills": profile.get("skills"),
                "projects": profile.get("projects"),
                "work_history": profile.get("work_history"),
                "answers": profile.get("answers"),
            },
            ensure_ascii=False,
            default=str,
        )
    )


def _profile_technical_evidence_text(profile: dict[str, Any]) -> str:
    """Profile evidence appropriate for skill/platform checkbox claims.

    The general evidence text includes historical application answers. Those
    answers can contain negative or unrelated mentions such as "Have you ever
    interviewed at Anthropic before? No" and must not become evidence that the
    candidate has used Anthropic/Claude or GCP. Technical checkbox claims should
    be grounded in resume-like evidence only.
    """
    return _norm(
        json.dumps(
            {
                "summary": profile.get("summary"),
                "skills": profile.get("skills"),
                "projects": profile.get("projects"),
                "work_history": profile.get("work_history"),
                "profile_vector_context": profile.get("profile_vector_context"),
                "specializations": profile.get("specializations"),
            },
            ensure_ascii=False,
            default=str,
        )
    )


def _has_profile_evidence(profile_text: str, *needles: str) -> bool:
    return any(_norm(needle) in profile_text for needle in needles)


def _production_screening_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        "able to work onsite" in normalized
        or "built and deployed" in normalized
        or "automatically optimizes decisions" in normalized
        or "advertising systems" in normalized
        or "multi gpu cluster" in normalized
        or "data pipelines for llm post training" in normalized
        or "rl training on an llm" in normalized
        or "production backend services" in normalized
        or "shipped ml ai models" in normalized
        or "directly impacted business metrics" in normalized
    ):
        return None
    answers = profile.get("answers") or {}
    saved = _find_answer(label, answers)
    if saved is not None:
        return str(saved)
    profile_text = _profile_evidence_text(profile)
    if (
        "able to work onsite" in normalized
        and "office" in normalized
        and ("mountain view" in normalized or "bay area" in normalized or "5 days" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or answers.get("Are you open to working in-person in one of our offices 25% of the time?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if (
        "built and deployed" in normalized
        and "production system" in normalized
        and "llm" in normalized
        and ("rag" in normalized or "tool use" in normalized or "agent" in normalized)
    ):
        has_llm_system = _has_profile_evidence(profile_text, "llm", "rag", "langchain", "agent")
        has_deployment = _has_profile_evidence(profile_text, "kubernetes", "deployed", "dockerized", "production")
        return "Yes" if has_llm_system and has_deployment else "No"
    if "automatically optimizes decisions" in normalized and "feedback signals" in normalized:
        return (
            "Yes"
            if _has_profile_evidence(profile_text, "automated retraining", "drift detection", "feedback scoring")
            else "No"
        )
    if (
        "advertising systems" in normalized
        and "recommendation systems" in normalized
        and ("ranking" in normalized or "optimization systems" in normalized)
    ):
        return (
            "Yes"
            if _has_profile_evidence(profile_text, "advertising", "recommendation system", "ranking")
            else "No"
        )
    if "multi gpu cluster" in normalized and "llm" in normalized:
        return (
            "Yes"
            if _has_profile_evidence(profile_text, "multi-gpu", "multi gpu", "8+ gpu", "deepspeed", "fsdp")
            else "No"
        )
    if "data pipelines for llm post training" in normalized:
        return (
            "Yes"
            if _has_profile_evidence(
                profile_text,
                "fine-tuning pipelines",
                "post-training",
                "preference pairs",
                "reward signals",
                "scheduled retraining",
                "edge-data ingestion",
            )
            else "No"
        )
    if "rl training on an llm" in normalized or (
        "personally run" in normalized and "ppo" in normalized and "grpo" in normalized
    ):
        return (
            "Yes"
            if _has_profile_evidence(profile_text, "rlhf", "ppo", "grpo", "dpo", "reinforcement learning")
            else "No"
        )
    if (
        "production backend services" in normalized
        and ("apis" in normalized or "async systems" in normalized or "distributed components" in normalized)
    ):
        has_service = _has_profile_evidence(profile_text, "rest microservice", "api", "fastapi", "distributed", "kafka")
        has_production = _has_profile_evidence(profile_text, "deployed", "dockerized", "production")
        return "Yes" if has_service and has_production else "No"
    if "shipped ml ai models" in normalized and "production traffic" in normalized:
        has_model = _has_profile_evidence(profile_text, "xgboost", "transformer", "model", "ml")
        has_deployment = _has_profile_evidence(profile_text, "deployed", "dockerized", "productionizing", "production")
        return "Yes" if has_model and has_deployment else "No"
    if "directly impacted business metrics" in normalized and (
        "revenue" in normalized or "conversion" in normalized or "advertiser spend" in normalized
    ):
        return (
            "Yes"
            if _has_profile_evidence(
                profile_text,
                "customer retention",
                "retention targeting",
                "workflow efficiency",
                "reporting latency",
                "business analytics",
            )
            else "No"
        )
    return None


def _developer_facing_products_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        "developer facing" in normalized
        and ("product" in normalized or "tool" in normalized)
        and ("api" in normalized or "sdk" in normalized or "cli" in normalized)
    ):
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    if saved is not None:
        return str(saved)
    profile_text = _profile_technical_evidence_text(profile)
    has_developer_tooling = _has_profile_evidence(
        profile_text,
        "developer tools",
        "developer facing",
        "api",
        "apis",
        "rest",
        "fastapi",
        "sdk",
        "cli",
        "github cli",
        "notion api",
        "tool integrations",
    )
    return "Yes" if has_developer_tooling else "No"


def _access_control_experience_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        "access control" in normalized
        or "oauth" in normalized
        or "openid" in normalized
        or "saml" in normalized
        or "sso" in normalized
        or "rbac" in normalized
        or "abac" in normalized
    ):
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    if saved is not None:
        return str(saved)
    profile_text = _profile_technical_evidence_text(profile)
    has_access_control = _has_profile_evidence(
        profile_text,
        "access control",
        "oauth",
        "openid",
        "saml",
        "sso",
        "rbac",
        "abac",
        "rebac",
        "authorization middleware",
        "identity synchronization",
        "identity management",
    )
    return "Yes" if has_access_control else "No"


def _auto_answer(label: str, profile: dict[str, Any], sensitive: bool = False) -> str | None:
    if not label or _requires_user_authored_answer(label, profile):
        return None
    normalized = _norm(label)
    company = str(profile.get("target_company") or "the company")
    title = str(profile.get("target_title") or "this role")
    answers = profile.get("answers") or {}
    profile_text = _profile_technical_evidence_text(profile)
    access_control_answer = _access_control_experience_answer(label, profile)
    if access_control_answer is not None:
        return access_control_answer
    if (
        "describe a production genai application" in normalized
        and "business use case" in normalized
        and ("models frameworks" in normalized or "architecture" in normalized)
    ):
        return (
            "The primary model used was Transformer-based LLMs.\n"
            "- Built a federated LLM fine-tuning and RAG evaluation workflow at Intellisys Lab for privacy-aware model improvement.\n"
            "- Used Python, TensorFlow Federated, Kubernetes, Kafka, MLflow, Hugging Face/Transformer tooling, and custom evaluation harnesses.\n"
            "- Productionization focus: automated edge-data ingestion, scheduled retraining, experiment tracking, and regression monitoring across 100+ edge devices.\n"
            "- Scaling challenges included distributed training coordination, reproducibility, RAG quality measurement, and backdoor/regression detection."
        )
    if _is_palantir_profile(profile):
        palantir_answer = _palantir_auto_answer(label, profile)
        if palantir_answer is not None:
            return palantir_answer
    if "suffix" in normalized:
        return profile.get("suffix") or answers.get("Suffix")
    if "middle name" in normalized:
        return profile.get("middle_name") or answers.get("Middle Name")
    if "address line 2" in normalized:
        return profile.get("address_line2") or answers.get("Address 2")
    if normalized == "county" or normalized.endswith(" county"):
        return profile.get("county") or answers.get("County")
    if "degree in computer science" in normalized:
        cs_entries = [
            item
            for item in profile.get("education") or []
            if isinstance(item, dict) and "computer science" in _norm(item.get("field"))
        ]
        if "what level" in normalized or ("level" in normalized and "school" in normalized):
            degree_text = _norm(cs_entries[0].get("degree") if cs_entries else "")
            if "master" in degree_text:
                return "Masters"
            if "bachelor" in degree_text:
                return "Bachelors"
            if "phd" in degree_text or "doctor" in degree_text:
                return "PhD"
            return "Other" if cs_entries else None
        return "Yes" if cs_entries else "No"
    if "which part of the bay area" in normalized and "based" in normalized:
        location = str(profile.get("location") or "Jersey City, NJ, USA")
        return (
            f"I am currently based in {location}, not in the Bay Area, and I am willing to relocate "
            "to San Francisco for the required in-office schedule."
        )
    if "currently based in one of the following geographies" in normalized:
        location = _norm(profile.get("location") or "")
        target_cities = [
            city
            for city in ("denver", "st louis", "saint louis", "indianapolis")
            if city in normalized
        ]
        return "Yes" if any(city in location for city in target_cities) else "No"
    current_based_country = _current_based_country_answer(label, profile)
    if current_based_country is not None:
        return current_based_country
    if (
        "currently live in" in normalized
        and "plan to relocate" in normalized
        and ("in office" in normalized or "in person" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes, I plan to relocate" if _truthy_answer(relocation) else "No"
    if (
        "requires in office work" in normalized
        and "acknowledge" in normalized
        and "agree" in normalized
        and ("three days" in normalized or "3 days" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes, I’d relocate prior to the start of the role" if _truthy_answer(relocation) else "No"
    if (
        "hybrid role" in normalized
        and "acknowledge" in normalized
        and "office" in normalized
        and ("four days" in normalized or "4 days" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if (
        ("foster city" in normalized or "hq 3 days per week" in normalized)
        and ("3 days" in normalized or "three days" in normalized)
        and ("work from" in normalized or "work at" in normalized or "hq" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if "willing to work from" in normalized and "office" in normalized:
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if (
        ("willing to work" in normalized or "able to work" in normalized or "excited and able" in normalized)
        and ("office" in normalized or "on site" in normalized or "onsite" in normalized)
        and (
            "four days" in normalized
            or "4 days" in normalized
            or "monday friday" in normalized
            or "monday through friday" in normalized
            or "nyc" in normalized
            or "sf" in normalized
            or "san francisco" in normalized
            or "new york" in normalized
            or "stockholm" in normalized
        )
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or answers.get("Are you open to working in-person in one of our offices 25% of the time?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if (
        "hands on engineering experience" in normalized
        and "python" in normalized
        and ("ml framework" in normalized or "pytorch" in normalized)
    ):
        return "Yes" if _has_profile_evidence(profile_text, "python", "pytorch") else "No"
    if (
        "customer facing" in normalized
        and ("enterprise customer" in normalized or "customer" in normalized)
        and ("travel" in normalized or "embedding" in normalized or "embedded" in normalized)
    ):
        travel = answers.get("Are you willing to travel?") or match_screening_rule(label, profile.get("screening_answer_rules"))
        return "Yes" if travel is None or _truthy_answer(travel) else "No"
    if "student or new grad" in normalized:
        levels = _norm(" ".join(str(item) for item in profile.get("target_levels") or []))
        profile_blob = _profile_evidence_text(profile)
        if "new grad" in levels or "new grad" in profile_blob or "student" in profile_blob:
            return "Yes"
        return "No"
    if "earliest month" in normalized and ("join" in normalized or "start" in normalized):
        saved_start_month = _find_answer(label, answers)
        if saved_start_month is not None:
            return str(saved_start_month)
        availability = (
            answers.get("When can you start?")
            or answers.get("What is your earliest availability?")
            or profile.get("earliest_availability")
            or profile.get("availability")
        )
        if "within a month" in _norm(availability):
            next_month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
            return next_month.strftime("%B %Y")
        return str(availability) if availability else None
    if "earliest start date" in normalized:
        start = (
            answers.get("What is your earliest start date?")
            or _approved_sensitive_entry_answer(profile, "start_date")
            or _match_sensitive("start_date", profile)
            or "Within a month"
        )
        return "Immediately/next few months, full-time" if "within a month" in _norm(start) else str(start)
    if "expected graduation" in normalized and ("month" in normalized or "year" in normalized):
        return str(profile.get("graduation_date") or _education_end_date_value(profile) or "May 2026")
    if "where have you published your work" in normalized:
        saved_publications = _find_answer(label, answers)
        if saved_publications is not None:
            return str(saved_publications)
        publication_text = _norm(json.dumps(profile.get("publications") or "", ensure_ascii=False, default=str))
        if publication_text:
            return str(profile.get("publications"))
        return "N/A"
    if "ai frameworks" in normalized and "hands on" in normalized:
        return (
            "I have used LangChain hands-on to build a multi-agent financial-audit workflow with retrieval, "
            "tool-style orchestration, human-in-the-loop feedback, and a BERT-based semantic similarity evaluator. "
            "I have also built RAG and LLM evaluation workflows around Hugging Face Transformers, PyTorch, and "
            "custom retrieval/evaluation harnesses. I have not used AutoGen in production, but I understand the "
            "multi-agent orchestration pattern and have built comparable agent workflows with LangChain."
        )
    if "working directly with clients" in normalized or "consulting capacity" in normalized:
        return (
            "At DHL Express, I worked with business and analytics stakeholders on customer-retention and reporting "
            "workflows, translating operational goals into SQL/Pandas ETLs, an XGBoost churn model, and Power BI "
            "analytics that improved retention targeting precision by 30%. I am comfortable gathering requirements, "
            "explaining tradeoffs to non-engineering stakeholders, and turning business pain points into deployed "
            "automation or ML workflows."
        )
    if "managed ai agents" in normalized:
        return (
            "I built XClaw, an AI agent orchestration desktop platform that supports 500+ LLMs and routes work "
            "through 50+ execution skills such as GitHub automation, scheduled briefings, task extraction, and "
            "tool-driven workflows. I also built a LangChain multi-agent audit workflow where agents retrieved "
            "financial context, generated audit outputs, and were evaluated against expert reports with a "
            "BERT-based semantic similarity benchmark."
        )
    if "system you" in normalized and "built before" in normalized:
        return (
            "I built a LangChain multi-agent auditing and evaluation system for financial audit workflows. "
            "The system combined retrieval, agent orchestration, human-in-the-loop feedback, and a BERT-based "
            "semantic similarity evaluator to compare AI-generated audit reports with expert outputs. It reached "
            "an 85% alignment rate with human experts and improved audit workflow efficiency by 40%."
        )
    if (
        "comfortable" in normalized
        and ("coming to the office" in normalized or "work from the office" in normalized or "in office" in normalized)
        and ("3 days" in normalized or "three days" in normalized or "tuesday" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or answers.get("Are you open to working in-person in one of our offices 25% of the time?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if (
        ("hybrid" in normalized or "in office" in normalized or "in-office" in normalized)
        and "office" in normalized
        and ("day" in normalized or "days" in normalized)
        and (
            "are you able" in normalized
            or "able to meet" in normalized
            or "able to commit" in normalized
            or "can you meet" in normalized
            or "requires" in normalized
        )
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or answers.get("This role requires that you are willing to relocate to San Francisco, CA, USA. Please confirm that you are willing to relocate for this role?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        office_answer = (
            answers.get("Are you open to working in-person in one of our offices 25% of the time?")
            or answers.get("Are you able to commit to working from one of our offices on Anchor Days each week?")
            or answers.get("Are you open to a hybrid schedule with in-office days on Monday, Wednesday, and Friday?")
        )
        if _truthy_answer(relocation) and (office_answer is None or _truthy_answer(office_answer)):
            return "Yes"
    if (
        "currently based" in normalized
        and "listed location" in normalized
        and ("work in person" in normalized or "office" in normalized)
        and ("3 days" in normalized or "three days" in normalized or "hybrid" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        if _truthy_answer(relocation):
            return "No, I’m not based in this location but willing to relocate"
    if "bachelor" in normalized and "degree" in normalized and (
        "computer science" in normalized
        or "data science" in normalized
        or "software engineering" in normalized
        or "closely related" in normalized
        or "related field" in normalized
    ):
        education_text = _norm(" ".join(
            " ".join(str(item.get(key) or "") for key in ("degree", "field", "school"))
            for item in profile.get("education") or []
            if isinstance(item, dict)
        ))
        return "Yes" if any(
            term in education_text
            for term in ("computer science", "data science", "software engineering")
        ) else "No"
    llm_label = (
        "large language model" in normalized
        or "llm" in normalized
        or "openai" in normalized
        or "anthropic claude" in normalized
        or "google gemini" in normalized
    )
    if llm_label and (
        "worked with" in normalized
        or "completed academic projects" in normalized
        or "professional work" in normalized
        or "personal projects" in normalized
    ):
        return "Yes" if any(term in profile_text for term in ("llm", "large language", "openai", "langchain", "rag")) else "No"
    if "highest level of education" in normalized and (
        "institution" in normalized or "from which" in normalized
    ):
        education = next(
            (item for item in profile.get("education") or [] if isinstance(item, dict)),
            {},
        )
        degree = str(education.get("degree") or "Master's Degree")
        field = str(education.get("field") or "Computer Science")
        school = str(education.get("school") or "Stevens Institute of Technology")
        return f"{degree} in {field} from {school}"
    if "highest level of education" in normalized and "completed" in normalized:
        degree = str(_current_education_value(profile, "degree") or "Master's Degree")
        degree_normalized = _norm(degree)
        if "master" in degree_normalized:
            return "Master's Degree"
        if "bachelor" in degree_normalized:
            return "Bachelor's Degree"
        if "doctor" in degree_normalized or "phd" in degree_normalized:
            return "Doctoral Degree"
        return degree
    if _is_source_question(normalized):
        return _preferred_source_answer(label, profile, answers)
    if "employee id" in normalized and (
        "currently" in normalized
        or "previously" in normalized
        or "if you" in normalized
    ):
        return "N/A"
    if "relative" in normalized and (
        "work for" in normalized
        or "currently work" in normalized
        or "employed" in normalized
    ) and "if so" not in normalized and "who" not in normalized:
        return "No"
    if (
        (
            "anchor days" in normalized
            or "working from one of our offices" in normalized
            or ("hybrid schedule" in normalized and "in office" in normalized)
            or ("hybrid schedule" in normalized and "in-office" in normalized)
            or "hybrid policy" in normalized
        )
        and ("office" in normalized or "in person" in normalized or "hybrid policy" in normalized)
    ):
        office_answer = (
            answers.get("Are you open to working in-person in one of our offices 25% of the time?")
            or answers.get("Are you able to commit to working from one of our offices on Anchor Days each week?")
            or answers.get("Are you open to a hybrid schedule with in-office days on Monday, Wednesday, and Friday?")
            or _find_answer(
                "Are you open to working in-person in one of our offices 25% of the time?",
                answers,
            )
        )
        if office_answer is not None:
            return str(office_answer)
    if (
        "comfortable" in normalized
        and ("coming in" in normalized or "come in" in normalized or "in person" in normalized or "in office" in normalized)
        and ("5 days" in normalized or "five days" in normalized or "5 6 days" in normalized or "5 days a week" in normalized)
        and "office" in normalized
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if "1099" in normalized and (
        "without requiring" in normalized
        or "without sponsorship" in normalized
        or "complete any paperwork" in normalized
    ):
        # This compound question is false when an approved profile requires
        # sponsorship, even if the candidate can work today.
        for key, value in (profile.get("sensitive_answers") or {}).items():
            if "sponsor" not in _norm(key) or not isinstance(value, dict) or not value.get("approved"):
                continue
            if _norm(value.get("answer")) in {"yes", "true", "1"}:
                return "No"
    if (
        ("legally authorized" in normalized or "authorized to work" in normalized)
        and ("without requiring" in normalized or "without sponsorship" in normalized)
        and ("sponsorship" in normalized or "visa" in normalized)
    ):
        sponsorship = (
            _approved_sensitive_entry_answer(profile, "sponsorship")
            or str((profile.get("work_authorization_by_country") or {}).get("requires_sponsorship") or "")
        )
        if sponsorship is not None:
            return "No" if _truthy_answer(sponsorship) else "Yes"
    sponsorship_countries = _sponsorship_countries_answer(label, profile)
    if sponsorship_countries is not None:
        return sponsorship_countries
    conflict_answer = _conflict_of_interest_screening_answer(label, profile)
    if conflict_answer is not None:
        return conflict_answer
    government_public_answer = _government_public_employment_answer(label, profile)
    if government_public_answer is not None:
        return government_public_answer
    if "compensation offer" in normalized and "factors" in normalized:
        saved_compensation_factor = _find_answer(label, answers)
        return str(saved_compensation_factor) if saved_compensation_factor is not None else "No"
    if _is_hourly_pay_question(label):
        saved_hourly = _find_answer(label, answers)
        if saved_hourly is not None:
            return str(saved_hourly)
        hourly = _hourly_pay_expectation_value(profile)
        if hourly is not None:
            return hourly
    if (
        "desired compensation" in normalized
        or "compensation range" in normalized
        or "compensation expectation" in normalized
        or "compensation expectations" in normalized
        or "desired pay" in normalized
        or "pay range" in normalized
        or "pay expectation" in normalized
        or "expected pay" in normalized
        or "salary expectation" in normalized
    ):
        saved_compensation = _find_answer(label, answers)
        if saved_compensation is not None:
            return str(saved_compensation)
        approved_salary = _approved_sensitive_entry_answer(profile, "salary")
        if approved_salary is not None:
            return str(approved_salary)
        return str(profile.get("minimum_expected_salary") or "") or None
    if "current work status" in normalized:
        saved_status = _find_answer(label, answers)
        if saved_status is not None:
            return str(saved_status)
        return str(profile.get("job_search_status") or "") or None
    if (
        "18 years of age" in normalized
        or "18 years old" in normalized
        or "18 or older" in normalized
    ):
        saved_age = _find_answer(label, answers)
        if saved_age is not None:
            return str(saved_age)
        birthday = _date_target(profile.get("birthday"))
        if birthday is not None:
            today = date.today()
            age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
            return "Yes" if age >= 18 else "No"
    if (
        "available to work" in normalized
        and "full time" in normalized
        and "permanent employee" in normalized
    ):
        saved_availability = _find_answer(label, answers)
        if saved_availability is not None:
            return str(saved_availability)
        start = (
            answers.get("When can you start?")
            or answers.get("What is your earliest start date?")
            or _approved_sensitive_entry_answer(profile, "start_date")
            or _match_sensitive("start_date", profile)
            or profile.get("earliest_availability")
            or profile.get("availability")
            or "Within a month"
        )
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        if ("san mateo" in normalized or "headquarters" in normalized or "hq" in normalized) and not _truthy_answer(relocation):
            return None
        return "Immediately/next few months, full-time" if "within a month" in _norm(start) else str(start)
    if "phd" in normalized and ("pursuing" in normalized or "completed" in normalized):
        saved_phd = _find_answer(label, answers)
        if saved_phd is not None:
            return str(saved_phd)
        education_text = _norm(
            " ".join(
                " ".join(str(item.get(key) or "") for key in ("degree", "field", "school"))
                for item in profile.get("education") or []
                if isinstance(item, dict)
            )
        )
        return "Yes" if ("phd" in education_text or "doctor" in education_text) else "No"
    if (
        "confirm receipt" in normalized
        and ("privacy notice" in normalized or "arbitration agreement" in normalized)
    ):
        saved_notice = _find_answer(label, answers)
        if saved_notice is not None:
            return str(saved_notice)
        privacy = _approved_sensitive_entry_answer(profile, "privacy_consent")
        legal = _approved_sensitive_entry_answer(profile, "legal_attestation")
        terms = _approved_sensitive_entry_answer(profile, "terms_consent")
        if any(_truthy_answer(value) for value in (privacy, legal, terms)):
            return "Confirmed"
    if "privacy notice" in normalized and (
        "acknowledge" in normalized
        or "acknowledgement" in normalized
        or "acknowledgment" in normalized
    ):
        saved_notice = _find_answer(label, answers)
        if saved_notice is not None:
            return str(saved_notice)
        privacy = _approved_sensitive_entry_answer(profile, "privacy_consent")
        legal = _approved_sensitive_entry_answer(profile, "legal_attestation")
        terms = _approved_sensitive_entry_answer(profile, "terms_consent")
        if any(_truthy_answer(value) for value in (privacy, legal, terms)):
            return "I Acknowledge"
    if "consent to process" in normalized or (
        "process" in normalized and "personal data" in normalized and "consent" in normalized
    ):
        saved_consent = _find_answer(label, answers)
        if saved_consent is not None:
            return str(saved_consent)
        privacy = _approved_sensitive_entry_answer(profile, "privacy_consent")
        terms = _approved_sensitive_entry_answer(profile, "terms_consent")
        if any(_truthy_answer(value) for value in (privacy, terms)):
            return "I Agree"
    legal_terms_consent = _legal_terms_consent_answer(label, profile)
    if legal_terms_consent is not None:
        return legal_terms_consent
    if "may use ai tools" in normalized and "application and interview process" in normalized:
        saved_ai_ack = _find_answer(label, answers)
        if saved_ai_ack is not None:
            return str(saved_ai_ack)
        legal = _approved_sensitive_entry_answer(profile, "legal_attestation")
        return "Yes" if legal is None or _truthy_answer(legal) else None
    if "best describes how you use ai tools today" in normalized:
        saved_ai_use = _find_answer(label, answers)
        if saved_ai_use is not None:
            return str(saved_ai_use)
        profile_text = _profile_evidence_text(profile)
        if "agent" in profile_text or "ai tool" in profile_text or "automation" in profile_text:
            return "I design or automate workflows with AI tools (e.g., building agents, integrating AI into team processes)."
        return "I have experimented with AI tools (professionally and/or personally)."
    if "know anyone" in normalized and ("currently at" in normalized or "currently work" in normalized):
        saved_connection = _find_answer(label, answers)
        if saved_connection is not None:
            return str(saved_connection)
        return "No"
    if "built ai agents" in normalized or ("built" in normalized and "ai agents" in normalized):
        saved_agents = _find_answer(label, answers)
        if saved_agents is not None:
            return str(saved_agents)
        return (
            "Yes. I built XClaw, an AI agent orchestration desktop platform that routes work across LLMs, "
            "tools, and execution skills, and I built a LangChain multi-agent audit workflow with retrieval, "
            "human-in-the-loop feedback, and BERT-based evaluation against expert audit reports."
        )
    if "what ai tools" in normalized and ("currently using" in normalized or "using today" in normalized):
        saved_tools = _find_answer(label, answers)
        if saved_tools is not None:
            return str(saved_tools)
        return (
            "I use ChatGPT/Codex for coding assistance, debugging, and workflow automation; LangChain for "
            "agent/RAG prototypes; OpenAI and Anthropic APIs for LLM workflows; and Python, PyTorch, MLflow, "
            "Kafka, Docker, and Kubernetes for model training, evaluation, deployment, and monitoring."
        )
    if (
        "large language models" in normalized
        and ("worked with" in normalized or "completed academic projects" in normalized)
    ):
        saved_llm_exposure = _find_answer(label, answers)
        if saved_llm_exposure is not None:
            return str(saved_llm_exposure)
        return "Yes" if any(term in profile_text for term in ("llm", "large language", "openai", "anthropic", "langchain", "rag")) else "No"
    if (
        "working proficiency in python" in normalized
        and ("scripts" in normalized or "apis" in normalized or "data structures" in normalized)
    ):
        saved_python = _find_answer(label, answers)
        if saved_python is not None:
            return str(saved_python)
        skills = {_norm(skill) for skill in profile.get("skills") or []}
        profile_text = _profile_evidence_text(profile)
        return "Yes" if "python" in skills or "python" in profile_text else "No"
    if "describe your experience with python" in normalized:
        return (
            "I use Python for ML engineering, data pipelines, APIs, automation, and evaluation workflows. "
            "At DHL Express, I built SQL/Pandas ETLs, an XGBoost churn-prediction pipeline, and a Dockerized "
            "Transformer sentiment-analysis REST microservice. At Intellisys Lab, I used Python with "
            "TensorFlow Federated, Kafka, Kubernetes, and MLflow to run distributed LLM fine-tuning, "
            "scheduled retraining, experiment tracking, and regression monitoring."
        )
    if "describe your experience with artificial intelligence or machine learning" in normalized:
        return (
            "My AI/ML experience spans coursework, research, internships, and personal projects. I have built "
            "LLM/RAG evaluation workflows, federated LLM fine-tuning pipelines, anomaly detection for Transformer "
            "models, an XGBoost customer-churn system, and a Transformer sentiment service. I work with Python, "
            "PyTorch, TensorFlow, Hugging Face Transformers, LangChain, RAG patterns, MLflow, Kubernetes, Kafka, "
            "and cloud deployment workflows."
        )
    if "project where you used an llm" in normalized or (
        "used an llm to solve a problem" in normalized and "outcome" in normalized
    ):
        return (
            "I built a LangChain multi-agent auditing and evaluation framework to help automate financial audit "
            "workflows. My role was to design the agent workflow, implement retrieval/evaluation logic, and build "
            "a BERT-based semantic similarity benchmark comparing AI-generated audit reports with expert outputs. "
            "The system used LLM orchestration, human-in-the-loop feedback, Python, LangChain, and PyTorch, reached "
            "an 85% alignment rate with human experts, and improved audit workflow efficiency by 40%."
        )
    if "why are you interested" in normalized and ("welbehealth" in normalized or "healthcare" in normalized):
        return (
            "I am interested in WelbeHealth because the role applies LLMs, RAG, and agentic workflows to operational "
            "and participant-care challenges where reliable AI can create direct human impact. My background in "
            "Python, RAG evaluation, LLM systems, ML pipelines, and cloud deployment fits the AI Engineer I scope. "
            "I would bring hands-on experience building measurable AI systems while learning healthcare-specific "
            "privacy, security, and responsible AI practices from the team."
        )
    if "support tickets" in normalized and "customer" in normalized and ("50" in normalized or "calls" in normalized):
        return (
            "Yes. I am comfortable spending a substantial part of the role directly with customers through support "
            "tickets and calls if that improves the AI systems we build. At DHL Express, I worked from business and "
            "customer-retention problems back into SQL/Pandas data pipelines, an XGBoost churn model, SHAP analysis, "
            "and Power BI reporting used by non-engineering stakeholders. I like this feedback loop because support "
            "work exposes real edge cases, unclear workflows, and failure modes that are easy to miss from a purely "
            "engineering-only view, and those details make applied AI systems more useful."
        )
    if (
        ("claude code" in normalized or "openclaw" in normalized or "open claw" in normalized)
        and ("ai assisted" in normalized or "development environment" in normalized or "coding" in normalized)
    ):
        return (
            "I have hands-on experience using AI-assisted development environments and building adjacent tooling. "
            "I built XClaw, a desktop interface for Open Claw, as an autonomous AI agent orchestration platform with "
            "real-time streaming responses, rich Markdown rendering, scheduled AI briefings, task extraction, and "
            "integrations across GitHub CLI, Notion API, and messaging tools. I also use Codex-style AI coding tools "
            "for debugging, implementation planning, and repetitive development workflows, and I am interested in "
            "building systems that make these capabilities usable by non-engineers."
        )
    if "where did you attend" in normalized and ("undergrad" in normalized or "undergad" in normalized):
        undergrad = next(
            (
                item
                for item in profile.get("education") or []
                if isinstance(item, dict) and "bachelor" in _norm(item.get("degree"))
            ),
            None,
        )
        return str((undergrad or {}).get("school") or "") or None
    if "undergrad degree" in normalized or "undergraduate degree" in normalized:
        undergrad = next(
            (
                item
                for item in profile.get("education") or []
                if isinstance(item, dict) and "bachelor" in _norm(item.get("degree"))
            ),
            None,
        )
        return str((undergrad or {}).get("field") or "") or None
    if "startup or founder experience" in normalized:
        return (
            "I do not have formal founder experience, but I have built founder-style independent projects end to end. "
            "For example, I built XClaw, a desktop interface for Open Claw, from product idea through implementation: "
            "an AI agent orchestration platform with streaming LLM responses, 50+ execution skills, daily AI briefings, "
            "NLP task extraction, and messaging integrations. I am comfortable taking ambiguous problems, validating "
            "the workflow through users or stakeholders, and shipping practical iterations quickly."
        )
    if "describe a system" in normalized and "built" in normalized:
        return (
            "I built XClaw, an AI agent orchestration desktop system for Open Claw. The system coordinates LLM-driven "
            "workflows across tools, supports real-time streaming responses and rich Markdown rendering, and integrates "
            "execution skills such as GitHub automation, scheduled daily briefings, NLP-based task extraction, and "
            "messaging integrations for WhatsApp, Telegram, and Discord. I designed the workflow layer, implemented the "
            "desktop interface, and focused on making agent actions observable and usable for practical automation."
        )
    if "relatives currently work" in normalized or "relatives currently employed" in normalized:
        saved_relative = _find_answer(label, answers)
        return str(saved_relative) if saved_relative is not None else "N/A"
    if ("referred" in normalized or "referral" in normalized) and (
        "full name" in normalized
        or "employee name" in normalized
        or ("employee" in normalized and "name" in normalized)
        or "referring individual" in normalized
    ):
        saved_referral_name = answers.get(label)
        return str(saved_referral_name) if saved_referral_name is not None else "N/A"
    if (
        (
            "were you referred" in normalized
            or "are you referred" in normalized
            or "have you been referred" in normalized
            or "employee referral" in normalized
        )
        and (
            "current employee" in normalized
            or "employee" in normalized
            or "employee of the company" in normalized
            or "company employee" in normalized
            or "by an employee" in normalized
        )
    ):
        saved_referral = _find_answer(label, answers)
        return str(saved_referral) if saved_referral is not None else "No"
    if (
        "current employee" in normalized
        and (
            "are you" in normalized
            or "currently an employee" in normalized
            or "of the company" in normalized
        )
    ):
        saved_current_employee = _find_answer(label, answers)
        return str(saved_current_employee) if saved_current_employee is not None else "No"
    if (
        "ever worked for" in normalized
        or "previously worked for" in normalized
        or ("worked for" in normalized and "previously" in normalized)
        or "previously worked at" in normalized
        or ("worked at" in normalized and ("currently" in normalized or "previously" in normalized))
        or ("worked" in normalized and "company acquired" in normalized)
    ):
        saved_previous_employee = _find_answer(label, answers)
        return str(saved_previous_employee) if saved_previous_employee is not None else "No"
    production_screening = _production_screening_answer(label, profile)
    if production_screening is not None:
        return production_screening
    if "securities industry" in normalized and ("registered" in normalized or "attempted" in normalized):
        saved_securities = _find_answer(label, answers)
        return str(saved_securities) if saved_securities is not None else "No"
    if (
        "government official" in normalized
        or "financial regulator" in normalized
        or ("military" in normalized and "law enforcement" in normalized)
    ) and (
        "currently" in normalized
        or "previously" in normalized
        or "influence" in normalized
        or "post employment" in normalized
        or "post-employment" in normalized
        or "immediate family" in normalized
        or "close associate" in normalized
        or "referred" in normalized
        or "recommended" in normalized
    ):
        saved_government = _find_answer(label, answers)
        return str(saved_government) if saved_government is not None else "No"
    if "current government official" in normalized or "former government official" in normalized:
        saved_government = _find_answer(label, answers)
        return str(saved_government) if saved_government is not None else "No, I am not a current or former Government Official"
    if "close relative of a government official" in normalized:
        saved_relative = _find_answer(label, answers)
        return str(saved_relative) if saved_relative is not None else "No, I am not a relative of a government official."
    if (
        "referred to this position" in normalized
        and ("senior leader" in normalized or "decision maker" in normalized or "decisionmaker" in normalized)
    ):
        saved_referral = _find_answer(label, answers)
        return str(saved_referral) if saved_referral is not None else "No"
    if (
        "if you answered yes" in normalized
        and ("employment authorization" in normalized or "immigration" in normalized or "sponsorship" in normalized)
        and ("explanation" in normalized or "explain" in normalized or "provide" in normalized)
    ):
        saved_explanation = _find_answer(label, answers)
        if saved_explanation is not None:
            return str(saved_explanation)
        sponsorship = (
            _approved_sensitive_entry_answer(profile, "sponsorship")
            or str((profile.get("work_authorization_by_country") or {}).get("requires_sponsorship") or "")
        )
        if _truthy_answer(sponsorship):
            return (
                "I am currently authorized to work in the United States and will require "
                "immigration-related employer sponsorship in the future to maintain employment authorization."
            )
        return None
    biopharma_compliance = _biopharma_compliance_answer(label, profile)
    if biopharma_compliance is not None:
        return biopharma_compliance
    if (
        "how many years of professional experience" in normalized
        and "excluding internships" in normalized
    ):
        return _zero_based_professional_experience_range_answer(profile)
    if (
        ("full time software engineer" in normalized or "full time software engineering" in normalized)
        and "professional setting" in normalized
        and "excluding internships" in normalized
    ):
        return "Yes" if _has_full_time_software_engineering_experience(profile) else "No"
    if (
        "which programming languages" in normalized
        and "regularly use" in normalized
        and "professional setting" in normalized
    ):
        return str(profile.get("preferred_programming_language") or "Python")
    years_match = re.search(r"at least\s+(\d+)\s*(?:\+)?\s+years?", normalized)
    if years_match and "experience" in normalized:
        current_years = _years_experience_value(profile)
        if current_years is not None:
            nums = [int(value) for value in re.findall(r"\d+", str(current_years))]
            if nums:
                return "Yes" if max(nums) >= int(years_match.group(1)) else "No"
    if "pronouns" in normalized:
        value = profile.get("pronouns") or answers.get("Pronouns")
        value_norm = _norm(value)
        if "he" in value_norm and "him" in value_norm:
            return "He / Him"
        if "she" in value_norm and "her" in value_norm:
            return "She / Her"
        if "they" in value_norm and "them" in value_norm:
            return "They / Them"
        return str(value) if value else None
    if "community support domain" in normalized:
        return (
            "I do not have direct Community Support domain experience, but I have worked on "
            "customer-focused ML and analytics problems. At DHL Express, I built churn "
            "prediction, sentiment analysis, SQL/Pandas data workflows, and Power BI reporting "
            "to improve customer retention targeting and operational decision-making."
        )
    if "exceptional work" in normalized:
        return (
            "I built and evaluated production-minded ML systems across research and applied settings. "
            "At Intellisys Lab, I deployed federated LLM fine-tuning and evaluation workflows on "
            "Kubernetes across 100+ edge devices with Kafka ingestion and MLflow tracking, improving "
            "LLM accuracy by 54% over centralized baselines. At DHL Express, I built an XGBoost "
            "customer-churn pipeline, Transformer sentiment service, SQL/Pandas ETLs, and AWS ECS "
            "retraining workflows that improved customer-retention targeting precision by 30%."
        )
    if "spacexai employment history" in normalized or "spacex employment history" in normalized:
        saved_history = _find_answer(label, answers)
        return str(saved_history) if saved_history is not None else "I have never worked for SpaceX or SpaceXAI"
    if "ever worked for" in normalized and any(term in normalized for term in ["employee", "intern", "contractor"]):
        saved_history = _find_answer(label, answers)
        return str(saved_history) if saved_history is not None else "No"
    if (
        ("previously" in normalized or "currently" in normalized or "current" in normalized)
        and (
            "contractor" in normalized
            or "consultant" in normalized
            or "former employee" in normalized
            or "access to" in normalized
            or "engaged with" in normalized
        )
    ):
        saved_engagement = _find_answer(label, answers)
        return str(saved_engagement) if saved_engagement is not None else "No"
    if "non compete" in normalized or "non solicitation" in normalized:
        saved_agreement = _find_answer(label, answers)
        return str(saved_agreement) if saved_agreement is not None else "No"
    if (
        "personal familial relationships" in normalized
        or "outside business activities" in normalized
        or "intellectual property ownership" in normalized
        or ("investment" in normalized and "private company" in normalized and "competitor" in normalized)
    ):
        saved_disclosure = _find_answer(label, answers)
        return str(saved_disclosure) if saved_disclosure is not None else "No"
    if (
        "worked for airbnb" in normalized
        or ("currently" in normalized and "ever worked" in normalized and "airbnb" in normalized)
    ):
        saved_airbnb_history = _find_answer(label, answers)
        return str(saved_airbnb_history) if saved_airbnb_history is not None else "No"
    if "been employed by" in normalized and ("past" in normalized or "subsidiary" in normalized or "affiliate" in normalized):
        saved_employment = _find_answer(label, answers)
        return str(saved_employment) if saved_employment is not None else "No"
    if (
        "contact" in normalized
        and "current employer" in normalized
        and (profile.get("work_history") or [])
    ):
        # Do not authorize unsolicited contact with a current employer unless
        # the user has stored a more specific answer.
        saved_contact = _find_answer(label, answers)
        return str(saved_contact) if saved_contact is not None else "No"
    if "employment and military service" in normalized and "add another employment" in normalized:
        return "Thank you"
    if "essential functions" in normalized and "reasonable accommodation" in normalized:
        saved_ability = _find_answer(label, answers)
        return str(saved_ability) if saved_ability is not None else "Yes"
    if "prior internships" in normalized or "previous internships" in normalized:
        internships = [
            item
            for item in profile.get("work_history") or []
            if isinstance(item, dict)
            and "intern" in _norm(" ".join(str(item.get(key) or "") for key in ["employment_type", "title"]))
        ]
        return "3+" if len(internships) >= 3 else str(len(internships))
    if "discipline" in normalized or "field of study" in normalized or "major" in normalized:
        value = _current_education_value(profile, "field")
        return str(value) if value else None
    if (
        "personal project" in normalized
        and ("proud" in normalized or "share" in normalized)
        and "do you have" not in normalized
    ):
        projects = profile.get("projects") or profile.get("outside_experience") or []
        for project in projects:
            if not isinstance(project, dict):
                continue
            title_text = " ".join(str(project.get(key) or "") for key in ["title", "name"])
            if "xclaw" in _norm(title_text):
                url = project.get("url") or "https://github.com/Alfred768/xclaw"
                return (
                    "XClaw is a desktop interface for Open Claw that I built to orchestrate "
                    "autonomous AI agent workflows across hundreds of LLMs, with streaming "
                    f"Markdown UX, tool integrations, scheduled automation, and messaging integrations. {url}"
                )
        return None
    if (
        ("something you worked on" in normalized and "proud" in normalized)
        or ("project" in normalized and "proud" in normalized and "do you have" not in normalized)
    ):
        return (
            "I am proud of XClaw, a desktop interface I built for orchestrating autonomous AI-agent workflows. "
            "It supports streaming LLM responses, rich Markdown rendering, 50+ execution skills, scheduled daily "
            "briefings, NLP task extraction, and integrations with GitHub CLI, Notion API, WhatsApp, Telegram, "
            "and Discord. The project reflects the product engineering I enjoy: turning complex agent capabilities "
            "into a practical, observable workflow that people can actually use."
        )
    if "what motivates you" in normalized:
        return (
            "I am motivated by building systems that make advanced AI useful in real workflows. I like work where "
            "the product surface, backend reliability, and evaluation loop all matter, because that is where careful "
            "engineering turns model capability into something users can trust. I also enjoy fast feedback from "
            "shipping, measuring behavior, and improving the system based on real usage."
        )
    if (
        "application you built yourself" in normalized
        and "problem you were solving" in normalized
        and "measure success" in normalized
    ):
        return (
            "I built XClaw, a desktop interface for Open Claw, to make autonomous AI-agent workflows easier to run "
            "and observe from one place. The problem was that agent work often spans many models, tools, and channels, "
            "but users need a coherent interface for streaming responses, executing actions, and tracking useful outputs. "
            "I built the application with a desktop UI, real-time LLM streaming, rich Markdown rendering, 50+ execution "
            "skills, scheduled daily briefings, NLP-based task extraction, and integrations with GitHub CLI, Notion API, "
            "WhatsApp, Telegram, and Discord. I measured success by whether the system could reliably route work across "
            "hundreds of LLMs, turn chat messages into actionable tasks, and support repeatable automations such as "
            "daily briefings and tool-driven workflows instead of one-off prompts."
        )
    if "job code" in normalized and "posting" in normalized:
        application_url = str(profile.get("_application_url") or "")
        match = re.search(r"(?:gh_jid=|/jobs/)(\d+)", application_url)
        if match:
            return match.group(1)
    if "technical domain" in normalized and ("prefer" in normalized or "expertise" in normalized):
        return "Infrastructure"
    if "lgbtq" in normalized or "lgbtq+" in normalized:
        demographics = profile.get("demographics") or {}
        value = demographics.get("lgbtq") or profile.get("lgbtq")
        return str(value) if value else None
    if "sexual orientation" in normalized:
        demographics = profile.get("demographics") or {}
        value = demographics.get("sexual_orientation") or profile.get("sexual_orientation")
        return str(value) if value else "I don't wish to answer"
    if normalized == "age":
        return _age_bucket_answer(profile)
    if "which location" in normalized and "applying" in normalized:
        return str(profile.get("target_location") or profile.get("location") or profile.get("city") or "")
    if "full time" in normalized and "internship" in normalized:
        return "Full-Time"
    other_countries = _other_countries_location_answer(label, profile)
    if other_countries is not None:
        return other_countries
    if "spring career fair" in normalized:
        saved_career_fair = _find_answer(label, answers)
        return str(saved_career_fair) if saved_career_fair is not None else "No"
    if (
        ("based in san francisco" in normalized or "san francisco based" in normalized)
        and "open to relocating" in normalized
    ):
        location = _norm(profile.get("location") or "")
        if "san francisco" in location:
            return "San Francisco based"
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Open to relocating" if _truthy_answer(relocation) else None
    if (
        ("i understand" in normalized or "please confirm" in normalized or "confirm" in normalized)
        and ("in person role" in normalized or "in-person role" in normalized)
    ):
        relocation = (
            answers.get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if (
        "do you have" in normalized
        and "personal project" in normalized
        and ("proud" in normalized or "share" in normalized)
    ):
        projects = profile.get("projects") or profile.get("outside_experience") or []
        return "Yes" if any(isinstance(project, dict) for project in projects) else "No"
    if "what excites you about this opportunity" in normalized:
        return (
            f"I am excited by {company}'s mission-driven product work and by the chance to grow in a new grad "
            "software engineering role where I can contribute across customer-facing and internal platforms. "
            "My background includes Python, REST APIs, React, Docker/Kubernetes, data pipelines, and ML-focused "
            "internship projects, and I would value the mentorship, code review, and fast feedback loop described "
            "for this team."
        )
    if "high level of grit" in normalized:
        return (
            "At Intellisys Lab, I worked on federated LLM fine-tuning where reliability problems showed up across "
            "distributed edge-device training, data ingestion, and regression monitoring. I responded by breaking "
            "the problem into measurable checks, adding MLflow experiment tracking, improving Kafka-based data "
            "flows, and iterating on evaluation harnesses instead of treating failures as one-off issues. That "
            "persistence helped improve LLM accuracy by 54% over centralized baselines and made the workflow more "
            "reproducible."
        )
    if "full ownership" in normalized and "challenging moment" in normalized:
        return (
            "During my DHL Express internship, I took ownership of a customer-retention ML workflow that required "
            "turning business goals into usable data and model outputs. I built SQL/Pandas ETLs, trained an XGBoost "
            "churn model with SHAP explainability, handled class imbalance, and helped productionize retraining and "
            "reporting workflows with AWS ECS Fargate, MLflow, Jenkins, and Power BI. The work improved retention "
            "targeting precision by 30% and reduced model reporting latency by 30%."
        )
    if (
        "took ownership" in normalized
        and ("without a playbook" in normalized or "figured it out" in normalized)
        and "saw it through" in normalized
    ):
        return (
            "At DHL Express, I took ownership of a customer-retention ML workflow where the problem was not handed to "
            "me as a clean technical spec. I had to turn a broad business goal into usable data, model behavior, and "
            "reporting outputs. I built SQL/Pandas ETLs, trained an XGBoost churn model with SHAP explainability, handled "
            "class imbalance, and productionized retraining/reporting workflows using AWS ECS Fargate, MLflow, Jenkins, "
            "and Power BI. I kept iterating with business stakeholders until the workflow was measurable and useful; it "
            "improved retention targeting precision by 30% and reduced model reporting latency by 30%."
        )
    if "in person role" in normalized or "in-person role" in normalized or "work from the office" in normalized:
        value = _match_sensitive(label, profile)
        return str(value) if value else None
    if (
        "when can you start" in normalized
        or "soonest date" in normalized
        or "earliest availability" in normalized
        or (("available" in normalized or "availability" in normalized) and ("start" in normalized or "begin" in normalized))
    ):
        availability = (
            answers.get("When can you start?")
            or answers.get("What is your earliest availability?")
            or answers.get("What is the soonest date you would be available to start?")
            or profile.get("earliest_availability")
            or profile.get("availability")
            or profile.get("start_date")
        )
        return str(availability) if availability else None
    if (
        ("currently based" in normalized or "currently living" in normalized)
        and ("san francisco" in normalized or "bay area" in normalized)
    ):
        location = _norm(profile.get("location") or "")
        return "Yes" if ("san francisco" in location or "bay area" in location) else "No"
    if "commutable proximity" in normalized and "relocat" in normalized:
        value = _match_sensitive(label, profile)
        if _truthy_answer(value):
            return "I am willing to relocate before starting employment."
        return str(value) if value else None
    if "relocat" in normalized:
        value = _match_sensitive(label, profile)
        return str(value) if value else None
    if "review" in normalized and "linked document" in normalized and _profile_company_slug(profile) == "lyft":
        value = _match_sensitive("privacy policy", profile)
        if value is not None:
            return "I acknowledge that I have read and understood the terms of the Lyft Candidate Privacy Notice."
    if "candidate privacy policy" in normalized and _profile_company_slug(profile) == "airbnb":
        value = _match_sensitive("privacy policy", profile)
        if value is not None:
            return "I acknowledge that I have read and understood the Airbnb Candidate Privacy Policy."
    if normalized == "language":
        return str(profile.get("language") or profile.get("human_language") or "English")
    saved = _find_answer(label, answers)
    if saved is not None:
        return str(saved)
    developer_facing = _developer_facing_products_answer(label, profile)
    if developer_facing is not None:
        return developer_facing
    if _is_motivation_question(normalized, company):
        generated = _motivation_answer_for_label(label, profile)
        if generated:
            return generated
        return None
    if "strong fit" in normalized and ("role" in normalized or "position" in normalized):
        generated = (
            answers.get("Use this final response to make your case for why we should prioritize interviewing you. You may include anything you think is most relevant or differentiating")
            or answers.get("What makes you a strong fit for this role?")
            or answers.get(f"Why {company}?")
            or answers.get(f"Why do you want to work at {company}?")
        )
        if generated:
            return str(generated)
        title_text = title if title and title != "this role" else "this role"
        return (
            f"I am a strong fit for {title_text} because my background combines LLM/RAG evaluation, "
            "distributed model training workflows, and production ML engineering. At Intellisys Lab, "
            "I built federated LLM fine-tuning and evaluation workflows with Kubernetes, Kafka, MLflow, "
            "TensorFlow Federated, and custom RAG metrics. At DHL Express, I productionized ML retraining, "
            "monitoring, Dockerized model services, and SQL/Pandas data pipelines with measurable business impact."
        )
    if "additional information" in normalized or "anything else" in normalized:
        return str(answers.get("Additional Information") or "")
    if sensitive:
        return _match_sensitive(label, profile)
    return None


def _is_motivation_question(normalized_label: str, company: str | None) -> bool:
    company_norm = _norm(company or "")
    if "what excites you about" in normalized_label:
        return True
    if "this role interests you" in normalized_label:
        return True
    if "what about" in normalized_label and "interests you" in normalized_label:
        return True
    if "why are you applying to" in normalized_label:
        return True
    if "why" in normalized_label and "team" in normalized_label:
        return True
    return "why" in normalized_label and (
        "company" in normalized_label
        or "role" in normalized_label
        or (company_norm and company_norm in normalized_label)
    )


def _motivation_answer_for_label(label: str, profile: dict[str, Any]) -> str | None:
    answers = profile.get("answers") or {}
    company = str(profile.get("target_company") or "the company")
    generated = (
        answers.get(f"Why {company}?")
        or answers.get(f"Why do you want to work at {company}?")
        or answers.get(f"What excites you about {company}?")
        or answers.get("What excites you about this opportunity?")
        or answers.get(f"Why are you applying to {company}?")
        or answers.get("Why are you interested in this role?")
        or answers.get("Why this role?")
    )
    if not generated:
        return None
    answer = str(generated).strip()
    normalized = _norm(label)
    if "agent platform" in normalized and "agent" not in _norm(answer):
        answer += " I am especially interested in agent platform work because my projects include LangChain multi-agent workflows and agent tooling."
    if (
        ("distributed systems" in normalized or "core infrastructure" in normalized or "infrastructure team" in normalized)
        and not any(token in _norm(answer) for token in ["distributed", "infrastructure", "kubernetes", "kafka"])
    ):
        answer += " I am especially interested in this team because my background includes Kubernetes, Kafka, MLflow, and distributed model-training workflows."
    return answer


def _palantir_auto_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    answers = profile.get("answers") or {}
    graduation_year = _education_date_part(profile, "end", "year")
    if (
        "intended graduation year" in normalized
        or (
            "graduation year" in normalized
            and "high school" not in normalized
        )
    ):
        return graduation_year
    if "high school" in normalized:
        if "graduation" in normalized or "year" in normalized:
            return _high_school_value(profile, "end_year")
        if "name" in normalized or "school" in normalized:
            return _high_school_value(profile, "school")
    if _is_source_question(label):
        return _preferred_source_answer(label, profile, answers) or "LinkedIn"
    if "offer deadlines" in normalized:
        return "No"
    if "where are you spending summer 2026" in normalized:
        summer_location = (
            profile.get("summer_2026_location")
            or answers.get("Where are you spending summer 2026?")
            or profile.get("location")
            or profile.get("target_location")
        )
        location_norm = _norm(summer_location)
        target_norm = _norm(profile.get("target_location") or "")
        if "new york" in location_norm or "jersey city" in location_norm or "new york" in target_norm:
            return "New York City or somewhere nearby"
        if "palo alto" in location_norm or "palo alto" in target_norm:
            return "Palo Alto or somewhere nearby"
        return str(summer_location) if summer_location else None
    if "external palantir partners" in normalized and ("share" in normalized or "contact information" in normalized):
        return "No"
    if "resident of california" in normalized:
        state = _profile_us_state_code(profile)
        if state:
            return "Yes" if state == "ca" else "No"
        location = _norm(profile.get("location") or "")
        if location:
            return "Yes" if "california" in location else "No"
        return None
    if "delta vs dev" in normalized or (
        "software engineer roles at palantir" in normalized
        and "forward deployed software engineer" in normalized
    ):
        return (
            "I confirm that I am interested in the Forward Deployed Software Engineer New Grad role."
        )
    if "time you changed your mind" in normalized:
        return (
            "During my DHL Express internship, I initially focused on model accuracy for the churn-prediction "
            "pipeline. After reviewing stakeholder feedback, I changed my mind and prioritized explainability and "
            "operational reporting as first-class outputs. Adding SHAP explanations and Power BI reporting made the "
            "model more useful to business users and helped improve retention targeting precision by 30%."
        )
    saved = _find_answer(label, answers)
    if saved is not None:
        return str(saved)
    if (
        "hardest technical challenge" in normalized
        and ("work experience" in normalized or "personal project" in normalized)
    ):
        return (
            "One of the hardest technical challenges I faced was building a multi-agent financial-audit workflow "
            "that produced useful outputs instead of just plausible text. I had to design retrieval, agent "
            "coordination, human-in-the-loop feedback, and an evaluation layer that compared generated audit "
            "reports with expert reports. The difficult part was making the system measurable: I built a "
            "BERT-based semantic similarity benchmark, iterated on prompting and context selection, and tracked "
            "failure cases where the agent missed important evidence. The final workflow reached 85% alignment "
            "with expert reports and improved audit workflow efficiency by 40%, while teaching me to treat AI "
            "systems as engineered products with feedback loops, observability, and regression checks."
        )
    if (
        "if palantir did not exist" in normalized
        or "if palantir didn't exist" in normalized
        or "if palantir didn t exist" in normalized
    ):
        return (
            "I would be most excited to work on applied AI infrastructure for organizations with complex, "
            "high-stakes operational data. I like roles where engineering work connects directly to real users: "
            "building data pipelines, LLM workflows, evaluation systems, and reliable internal tools that help "
            "teams make better decisions. My strongest motivation is not only model quality in isolation, but "
            "turning models and data into systems people can trust in production. I would look for a team working "
            "at the intersection of software engineering, AI, and operational problem solving, with room for "
            "end-to-end ownership from ambiguous problem definition through deployment and measurement."
        )
    if (
        "fdse and swe roles" in normalized
        and "which of these roles resonates" in normalized
    ):
        return (
            "Software Engineer resonates most with my current search. I am looking for a new-grad engineering role "
            "where I can build reliable product and platform systems, strengthen my software engineering depth, "
            "and contribute to production code across backend, data, and AI workflows. I am also drawn to Palantir's "
            "customer impact, but the SWE path best matches my goal of growing as an engineer through ownership of "
            "scalable systems, clean abstractions, and measurable product outcomes."
        )
    return None


def _high_school_value(profile: dict[str, Any], key: str) -> str | None:
    high_school = profile.get("high_school")
    if isinstance(high_school, dict):
        aliases = {
            "school": ["school", "name", "high_school_name"],
            "end_year": ["end_year", "graduation_year", "year", "high_school_graduation_year"],
        }
        for alias in aliases.get(key, [key]):
            value = str(high_school.get(alias) or "").strip()
            if value:
                return value
    answers = profile.get("answers") or {}
    answer_keys = {
        "school": ["High School Name", "High school name"],
        "end_year": ["Year of High School Graduation", "High School Graduation Year"],
    }
    for answer_key in answer_keys.get(key, []):
        value = answers.get(answer_key)
        if value not in {None, ""}:
            return str(value)
    return None


def _match_sensitive(label: str, profile: dict[str, Any]) -> str | None:
    return resolve_sensitive_answer(label, profile) or _demographic_answer(label, profile)


def _legal_terms_consent_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    is_terms_consent = (
        "terms and conditions" in normalized
        and (
            "read and consent" in normalized
            or "read and agree" in normalized
            or "acceptance" in normalized
            or "i agree" in normalized
            or "agree to the terms" in normalized
            or "by clicking" in normalized
        )
    )
    is_truthfulness_attestation = (
        (
            "true and accurate" in normalized
            or "true and correct" in normalized
            or "false or misleading" in normalized
        )
        and (
            "i confirm" in normalized
            or "i certify" in normalized
            or "i hereby certify" in normalized
            or "i understand" in normalized
            or "i attest" in normalized
        )
    )
    is_accuracy_attestation = (
        ("double check" in normalized or "double-check" in normalized)
        and ("information provided" in normalized or "provided above" in normalized)
        and ("accuracy" in normalized or "accurate" in normalized or "errors" in normalized or "omissions" in normalized)
    )
    is_candidate_ai_responsible_use_ack = (
        "candidate ai responsible use policy" in normalized
        and (
            "read reviewed and understood" in normalized
            or ("read" in normalized and "reviewed" in normalized and "understood" in normalized)
        )
        and (
            "reflect my own work and experience" in normalized
            or "reflect my own work" in normalized
            or "own work and experience" in normalized
        )
    )
    is_privacy_consent = (
        (
            "personal data" in normalized
            or (
                "collecting storing and processing my responses" in normalized
                and "demographic data surveys" in normalized
            )
        )
        and ("consent" in normalized or "consents" in normalized or "agree" in normalized or "accept" in normalized)
        and (
            "by clicking" in normalized
            or "by checking this box" in normalized
            or "i accept" in normalized
            or "i agree" in normalized
        )
    )
    is_statement_ack = (
        "carefully read" in normalized
        and "understand" in normalized
        and "agree" in normalized
        and "statement" in normalized
    )
    if not (
        is_terms_consent
        or is_statement_ack
        or is_privacy_consent
        or is_truthfulness_attestation
        or is_accuracy_attestation
        or is_candidate_ai_responsible_use_ack
    ):
        return None
    if is_privacy_consent:
        approved = (
            _approved_sensitive_entry_answer(profile, "privacy_consent")
            or _approved_sensitive_entry_answer(profile, "terms_consent")
            or _approved_sensitive_entry_answer(profile, "legal_attestation")
        )
    else:
        approved = (
            _approved_sensitive_entry_answer(profile, "terms_consent")
            or _approved_sensitive_entry_answer(profile, "legal_attestation")
        )
    if approved is None:
        return None
    return "Yes" if _truthy_answer(approved) else approved


def _biopharma_compliance_answer(label: str, profile: dict[str, Any]) -> str | None:
    """Deterministic defaults for common pharma compliance Yes/No screening.

    Saved answers still win. These questions are narrow, required Workday-style
    controls that otherwise block because the scraper may miss their required
    state when the label appears as ``?*Select One``.
    """

    normalized = _norm(label)
    default: str | None = None
    if (
        ("conflict of interest" in normalized or "conflicts of interest" in normalized)
        and "relatives" in normalized
        and ("work in any capacity" in normalized or "work at" in normalized)
    ):
        default = "No"
    if "willing to commute" in normalized and "area where this position is located" in normalized:
        default = "Yes"
    if "oig list of excluded individuals entities" in normalized:
        default = "No"
    if "general services administration" in normalized and "excluded" in normalized:
        default = "No"
    if "debarred under the generic drug enforcement act" in normalized:
        default = "No"
    if "debarment proceedings pending" in normalized:
        default = "No"
    if "us licensed physician" in normalized or "u s licensed physician" in normalized:
        default = "No"
    if (
        ("fda" in normalized or "hhs" in normalized)
        and ("investigated" in normalized or "disqualified" in normalized or "restricted" in normalized)
        and "investigational drugs" in normalized
    ):
        default = "No"
    if (
        "pending inquiry by any governmental entity" in normalized
        or ("licensing association" in normalized and "administrative action" in normalized)
    ):
        default = "No"
    if default is None:
        return None
    saved = _find_answer(label, profile.get("answers") or {})
    return str(saved) if saved is not None else default


def _approved_sensitive_entry_answer(profile: dict[str, Any], key: str) -> str | None:
    entry = (profile.get("sensitive_answers") or {}).get(key)
    if isinstance(entry, dict) and entry.get("approved") and entry.get("answer") not in {None, ""}:
        return str(entry.get("answer"))
    return None


def _profile_us_state_code(profile: dict[str, Any]) -> str | None:
    raw_values = [
        profile.get("state"),
        profile.get("region"),
        profile.get("location"),
        profile.get("address_state"),
        profile.get("address_region"),
    ]
    state_by_name = {name: code for code, name in _US_STATE_NAMES.items()}
    for raw in raw_values:
        normalized = _norm(raw)
        if not normalized:
            continue
        tokens = normalized.split()
        for token in tokens:
            if token in _US_STATE_CODES:
                return token
        for name, code in state_by_name.items():
            if name in normalized:
                return code
    return None


def _profile_us_state_name(profile: dict[str, Any]) -> str | None:
    code = _profile_us_state_code(profile)
    return _US_STATE_NAMES.get(code) if code else None


def _listed_state_residency_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if not (
        ("live in one of the following states" in normalized)
        or ("reside in one of the following states" in normalized)
        or ("located in one of the following states" in normalized)
    ):
        return None
    candidate_state = _profile_us_state_code(profile)
    if not candidate_state:
        return None
    listed_codes = {
        code
        for code, name in _US_STATE_NAMES.items()
        if name in normalized or re.search(rf"\b{re.escape(code)}\b", normalized)
    }
    if not listed_codes:
        return None
    return "Yes" if candidate_state in listed_codes else "No"


def _truthy_answer(value: Any) -> bool:
    return _norm(value) in {"yes", "true", "1", "y"}


def _is_hourly_pay_question(label: str) -> bool:
    normalized = _norm(label)
    return (
        "hourly pay" in normalized
        or "hourly rate" in normalized
        or "expected hourly" in normalized
        or ("pay range" in normalized and "internship" in normalized)
    )


def _target_job_location_context(profile: dict[str, Any]) -> str:
    """Return job-location context only.

    Do not include the candidate's own ``location``/``country`` fields here:
    those describe where the applicant is based, not where the role is based.
    """

    parts: list[str] = []
    for key in [
        "target_location",
        "job_location",
        "position_location",
        "role_location",
        "target_country",
        "job_country",
        "position_country",
        "application_country",
        "application_source_url",
        "job_source_url",
        "source_url",
        "_application_url",
    ]:
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " ".join(parts)


def _work_authorization_country_context(label: str, profile: dict[str, Any]) -> str | None:
    """Infer the role country for generic work-authorization questions.

    Many ATS forms ask "authorized to work in the country where this position is
    located" without naming the country in the field label.  In that case use
    the job target location from the prepared package.  This prevents a broad
    "current applied country" answer from incorrectly overriding approved
    country-specific answers such as Canada/UK = No.
    """

    label_context = f" {_norm(label)} "
    job_context = f" {_norm(_target_job_location_context(profile))} "

    def has_any(context: str, phrases: tuple[str, ...]) -> bool:
        return any(f" {_norm(phrase)} " in context for phrase in phrases)

    # Explicit country in the question label is strongest.
    if has_any(label_context, ("canada", "vancouver", "british columbia", "toronto", "ontario", "montreal", "quebec")):
        return "canada"
    if has_any(label_context, ("united kingdom", "uk", "u k", "london", "england", "scotland", "wales", "northern ireland")):
        return "united_kingdom"
    if has_any(label_context, ("united states", "usa", "u s", "u s a", "us", "u s citizen")):
        return "us"

    if has_any(job_context, ("canada", "vancouver", "british columbia", "toronto", "ontario", "montreal", "quebec")):
        return "canada"
    if has_any(job_context, ("united kingdom", "uk", "u k", "london", "england", "scotland", "wales", "northern ireland")):
        return "united_kingdom"
    if has_any(
        job_context,
        (
            "united states",
            "usa",
            "u s",
            "u s a",
            "new york",
            "california",
            "san francisco",
            "mountain view",
            "san diego",
            "jersey city",
            "new jersey",
            "atlanta",
            "texas",
            "washington dc",
            "remote us",
            "remote united states",
        ),
    ):
        return "us"
    if has_any(
        job_context,
        (
            "ireland",
            "galway",
            "hungary",
            "budapest",
            "india",
            "bengaluru",
            "bangalore",
            "mexico",
            "guadalajara",
            "germany",
            "france",
            "netherlands",
            "poland",
            "romania",
            "singapore",
            "australia",
        ),
    ):
        return "other_non_us"
    return None


def _approved_work_authorization_for_country(profile: dict[str, Any], country: str | None) -> str | None:
    if not country:
        return None
    sensitive_key_by_country = {
        "us": "work_authorization_us",
        "canada": "work_authorization_canada",
        "united_kingdom": "work_authorization_uk",
    }
    sensitive_key = sensitive_key_by_country.get(country)
    if sensitive_key:
        approved = _approved_sensitive_entry_answer(profile, sensitive_key)
        if approved is not None:
            return approved
    by_country = profile.get("work_authorization_by_country") or {}
    if not isinstance(by_country, dict):
        return None
    aliases = {
        "us": ("us", "usa", "united_states", "united states"),
        "canada": ("canada", "ca"),
        "united_kingdom": ("united_kingdom", "united kingdom", "uk", "gb", "great_britain"),
    }.get(country, (country,))
    for alias in aliases:
        value = by_country.get(alias)
        if value not in {None, ""}:
            return str(value)
    return None


def _requires_external_application_portal(label: str) -> bool:
    normalized = _norm(label)
    if "constellation application form" in normalized:
        return True
    if "official hiring partner" in normalized and "application form" in normalized:
        return True
    if "do not need to submit" in normalized and "greenhouse application" in normalized:
        return True
    return False


def _profile_company_slug(profile: dict[str, Any]) -> str:
    company = _norm(profile.get("target_company") or "")
    if company:
        return company
    application_url = str(profile.get("_application_url") or "").lower()
    match = re.search(r"/(?:job-board|boards)/([^/?#]+)/", application_url)
    if match:
        return _norm(match.group(1))
    for token in ["lyft", "anthropic", "affirm", "airbnb", "coinbase"]:
        if token in application_url:
            return token
    return ""


def _is_palantir_profile(profile: dict[str, Any]) -> bool:
    return "palantir" in _profile_company_slug(profile)


def _work_authorization_dropdown_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if (
        "work authorization" not in normalized
        and "authorized to work" not in normalized
        and "authorization to work" not in normalized
        and "right to work" not in normalized
    ):
        return None
    target_country = _work_authorization_country_context(label, profile)
    country_authorization = _approved_work_authorization_for_country(profile, target_country)
    sponsorship = (
        _approved_sensitive_entry_answer(profile, "sponsorship")
        or str((profile.get("work_authorization_by_country") or {}).get("requires_sponsorship") or "")
    )
    if country_authorization is not None:
        authorization = country_authorization
    elif target_country and target_country != "us":
        authorization = "No"
    else:
        authorization = (
            _match_sensitive(label, profile)
            or _approved_sensitive_entry_answer(profile, "work_authorization_current_country")
            or _approved_sensitive_entry_answer(profile, "work_authorization_us")
        )
    company = str(profile.get("target_company") or _profile_company_slug(profile).title()).strip()
    company_possessive = f"{company}'s " if company else ""
    sponsorship_field = "sponsor" in normalized or "sponsorship" in normalized
    authorization_field = (
        "work authorization" in normalized
        or "legally authorized" in normalized
        or "authorized to work" in normalized
        or "authorization to work" in normalized
        or "right to work" in normalized
    )
    if "unrestricted" in normalized and authorization_field and not sponsorship_field:
        if _truthy_answer(sponsorship):
            return "No"
        if _truthy_answer(authorization):
            return "Yes"
    if (
        authorization_field
        and not sponsorship_field
        and _truthy_answer(sponsorship)
        and normalized == "work authorization"
    ):
        return (
            f"I require/will require {company_possessive}sponsorship to obtain work authorization "
            "in the country in which this position is based"
        )
    if authorization_field and not sponsorship_field and _truthy_answer(authorization):
        if (
            _truthy_answer(sponsorship)
            and (
                "describes your work authorization" in normalized
                or "describes your authorization" in normalized
                or "authorization to work in the country where you live" in normalized
            )
        ):
            return (
                "I am authorized to work in the country based on a valid work permit "
                "which needs to be sponsored by the company I work for"
            )
        if "for any employer" in normalized or "select one" in normalized:
            return "Yes"
        if (
            "country where this position is located" in normalized
            or "country where the position is located" in normalized
            or "country in which you are applying" in normalized
            or "country which you are applying" in normalized
            or "in the us" in normalized
            or "in the u s" in normalized
            or "in the united states" in normalized
        ):
            return "Yes"
        return "Yes, I am currently legally authorized to work in the country where the job is located."
    if authorization_field and not sponsorship_field and authorization is not None and _is_negative_answer(authorization):
        return "No"
    if (sponsorship_field or not authorization_field) and _truthy_answer(sponsorship):
        if (
            "retain or extend" in normalized
            or "now or in the future" in normalized
            or "will you in the future" in normalized
            or "at any point in the future" in normalized
        ):
            if "do you now" in normalized or "will you in the future" in normalized:
                return "Yes"
            if "at any point in the future" in normalized:
                return "Yes"
            return "Yes, I will require immigration sponsorship in the future to legally work in the country where the job is located."
        return (
            f"I require/will require {company_possessive}sponsorship to obtain work authorization "
            "in the country in which this position is based"
        )
    if _truthy_answer(authorization):
        return "I am authorized to work for any employer in the country in which this position is based."
    if authorization is not None and _is_negative_answer(authorization):
        return "No"
    if authorization is not None:
        return "My status to work in the country in which this position is based is unknown."
    return None


def _legal_signature_value(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if "full name" not in normalized or "date" not in normalized:
        return None
    if "signature" not in normalized and "signify" not in normalized:
        return None
    approved = _match_sensitive(label, profile) or _match_sensitive("i certify true and complete", profile)
    if not _truthy_answer(approved):
        return None
    name = str(profile.get("name") or "").strip()
    if not name:
        return None
    today = date.today()
    return f"{name} {today.month:02d}/{today.day:02d}/{today.year}"


def _demographic_answer(label: str, profile: dict[str, Any]) -> str | None:
    demographics = profile.get("demographics") or {}
    if not isinstance(demographics, dict):
        return None
    normalized = _norm(label)
    if "transgender" in normalized:
        explicit = demographics.get("transgender") or profile.get("transgender")
        if explicit:
            return str(explicit)
        return "I don't wish to answer"
    if "sexual orientation" in normalized:
        explicit = demographics.get("sexual_orientation") or profile.get("sexual_orientation")
        if explicit:
            return str(explicit)
        return "I don't wish to answer"
    if normalized == "age":
        return _age_bucket_answer(profile)
    if "gender" in normalized or "sex" in normalized:
        gender = demographics.get("gender")
        if _norm(gender) == "male":
            return "Man"
        if _norm(gender) == "female":
            return "Woman"
        return gender
    if "racial" in normalized or ("race" in normalized and ("ethnic" in normalized or "ethnicity" in normalized)):
        race = demographics.get("race") or demographics.get("ethnicity")
        race_normalized = _norm(race)
        if race_normalized in {"asian", "east asian", "south asian", "southeast asian", "asian not hispanic or latino"}:
            return "Asian"
        return race
    if "hispanic" in normalized or "latino" in normalized:
        explicit = demographics.get("hispanic_latino") or demographics.get("hispanic") or demographics.get("latino")
        if explicit:
            return explicit
        raw_ethnicity = demographics.get("ethnicity") or demographics.get("race") or ""
        if _is_decline_answer(raw_ethnicity):
            return str(raw_ethnicity)
        ethnicity = _norm(raw_ethnicity)
        if ethnicity in {"asian", "east asian", "south asian", "southeast asian", "asian not hispanic or latino"}:
            return "No"
        if ethnicity in {"hispanic", "latino", "hispanic or latino"}:
            return "Yes"
        return None
    if "ethnicity" in normalized:
        return demographics.get("ethnicity") or demographics.get("hispanic_latino")
    if "race" in normalized:
        race = demographics.get("race")
        race_normalized = _norm(race)
        if race_normalized in {"asian", "east asian", "south asian", "southeast asian", "asian not hispanic or latino"}:
            return "Asian"
        return race
    if "veteran" in normalized or "military" in normalized:
        return demographics.get("veteran")
    if "disability" in normalized or "disabled" in normalized:
        return demographics.get("disability")
    return None


def _age_bucket_answer(profile: dict[str, Any]) -> str | None:
    birthday = _date_target(profile.get("birthday"))
    if birthday is None:
        return None
    today = date.today()
    age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
    if age < 18:
        return None
    if age <= 24:
        return "18 - 24"
    if age <= 34:
        return "25 - 34"
    if age <= 44:
        return "35 - 44"
    if age <= 54:
        return "45 - 54"
    if age <= 64:
        return "55 - 64"
    return "65 and over"


def _is_demographic_label(label: str) -> bool:
    normalized = _norm(label)
    if normalized == "age" or "sexual orientation" in normalized:
        return True
    return any(
        token in normalized
        for token in ("gender", "sex", "ethnicity", "hispanic", "latino", "race", "veteran", "military", "disability")
    )


def _is_decline_answer(answer: Any) -> bool:
    return _norm(answer) in {
        "prefer not to say",
        "prefer not to answer",
        "decline",
        "decline to answer",
        "i don t wish to answer",
        "i do not wish to answer",
        "i do not want to answer",
        "decline to self identify",
        "i decline to self identify",
    }


_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

_US_STATE_NAMES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}


def _expanded_location_text(value: Any) -> str:
    tokens = _norm(value).split()
    expanded: list[str] = []
    for token in tokens:
        if token in {"us", "usa"}:
            expanded.extend(["united", "states"])
        else:
            expanded.extend(_US_STATE_NAMES.get(token, token).split())
    return " ".join(expanded)


def _option_match_score(option: Any, answer: Any) -> int:
    option_text = _norm(option)
    answer_text = _norm(answer)
    if not option_text or not answer_text:
        return 0
    if option_text == answer_text:
        return 100
    expanded_option = _expanded_location_text(option_text)
    expanded_answer = _expanded_location_text(answer_text)
    if expanded_option == expanded_answer:
        return 95
    if expanded_answer in expanded_option:
        return 70
    if expanded_option in expanded_answer:
        return 60
    return 0


def _infer_country(profile: dict[str, Any]) -> str | None:
    country = profile.get("country")
    if country:
        return str(country)
    location = _norm(profile.get("location"))
    if not location:
        return None
    tokens = set(location.split())
    if tokens & _US_STATE_CODES or "united states" in location or "usa" in tokens or "us" in tokens:
        return "United States"
    return None


def _infer_phone_country_code(profile: dict[str, Any]) -> str | None:
    explicit = str(profile.get("phone_country_code") or "").strip()
    if explicit:
        return explicit
    phone = str(profile.get("phone") or "").strip()
    match = re.match(r"^\+(\d{1,3})\b", phone)
    if match:
        return f"+{match.group(1)}"
    country = _norm(_infer_country(profile) or "")
    if country in {"united states", "united states of america", "usa", "us", "canada"}:
        return "+1"
    return None


def _city_from_location(location: Any) -> str | None:
    raw = str(location or "").strip()
    if "," not in raw:
        return None
    city = raw.split(",", 1)[0].strip()
    return city or None


def _expanded_us_location(location: Any) -> str | None:
    raw = str(location or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) < 2:
        return raw
    state_aliases = {
        "NJ": "New Jersey",
        "NY": "New York",
        "CA": "California",
        "WA": "Washington",
        "DC": "District of Columbia",
    }
    country_aliases = {"USA": "United States", "US": "United States", "U.S.": "United States"}
    state = state_aliases.get(parts[1].upper(), parts[1])
    country = country_aliases.get(parts[2].upper(), parts[2]) if len(parts) > 2 else "United States"
    return ", ".join([parts[0], state, country])


def _first_profile_entry(entries: Any) -> dict[str, Any] | None:
    if not isinstance(entries, list):
        return None
    return next((entry for entry in entries if isinstance(entry, dict)), None)


def _current_work_value(profile: dict[str, Any], key: str) -> Any | None:
    entries = profile.get("work_history")
    if not isinstance(entries, list):
        return None
    current = next((entry for entry in entries if isinstance(entry, dict) and entry.get("current")), None)
    entry = current or _first_profile_entry(entries)
    if not entry:
        return None
    value = entry.get(key)
    if value not in {None, ""}:
        return value
    if entry.get("current") and key == "end_month":
        return _MONTH_NAMES[date.today().month]
    if entry.get("current") and key == "end_year":
        return str(date.today().year)
    return value


def _current_education_value(profile: dict[str, Any], key: str) -> Any | None:
    entry = _first_profile_entry(profile.get("education"))
    return entry.get(key) if entry else None


_MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def _graduation_date_aliases(raw: str) -> list[str]:
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d{2}|19\d{2})\b",
        raw,
        re.IGNORECASE,
    )
    if not match:
        return []
    month_lookup = {name.lower(): number for number, name in _MONTH_NAMES.items()}
    month = month_lookup.get(match.group(1).lower())
    year = int(match.group(2))
    if not month:
        return []
    aliases: list[str] = []
    today = date.today()
    if (year, month) < (today.year, today.month):
        aliases.append("Already graduated")
    if 1 <= month <= 4:
        aliases.append(f"Jan - April {year}")
    elif 5 <= month <= 8:
        aliases.extend([f"May - Aug {year}", f"May - August {year}"])
    else:
        aliases.extend([f"Sept - Dec {year}", f"September - December {year}"])
    return aliases


def _education_date_part(profile: dict[str, Any], boundary: str, part: str) -> str | None:
    """Return an education date component, deriving it from YYYY-MM when needed."""
    explicit = _current_education_value(profile, f"{boundary}_{part}")
    if explicit not in {None, ""}:
        value = str(explicit).strip()
    else:
        raw = str(_current_education_value(profile, f"{boundary}_date") or "").strip()
        match = re.fullmatch(r"(\d{4})[-/](\d{1,2})", raw)
        if not match:
            return None
        value = match.group(2 if part == "month" else 1)
    if part == "year":
        return value
    try:
        month = int(value)
    except ValueError:
        return value
    return _MONTH_NAMES.get(month, value)


def _format_education_end_date(profile: dict[str, Any]) -> str | None:
    end_date = _current_education_value(profile, "end_date")
    if not end_date:
        return None
    raw = str(end_date)
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        year, month = raw.split("-")
        return f"{_MONTH_NAMES.get(int(month), month)} {year}"
    return raw


def _years_experience_value(profile: dict[str, Any]) -> Any | None:
    return (
        profile.get("years_experience")
        or profile.get("relevant_years_experience")
        or profile.get("post_college_years_experience")
    )


def _work_history_entry_date(entry: dict[str, Any], prefix: str) -> date | None:
    raw = entry.get(f"{prefix}_date")
    if raw:
        return _date_target(raw)
    year = str(entry.get(f"{prefix}_year") or "").strip()
    month = str(entry.get(f"{prefix}_month") or "").strip()
    if year and month:
        month_number = None
        if month.isdigit():
            month_number = int(month)
        else:
            month_lookup = {name.lower(): number for number, name in _MONTH_NAMES.items()}
            month_number = month_lookup.get(month.lower())
        if month_number and 1 <= month_number <= 12:
            return date(int(year), month_number, 1)
    if year and year.isdigit():
        return date(int(year), 1, 1)
    return None


def _profile_work_experience_years(profile: dict[str, Any], *, today: date | None = None) -> float | None:
    entries = profile.get("work_history")
    if not isinstance(entries, list):
        return None
    end_default = today or date.today()
    intervals: list[tuple[date, date]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start = _work_history_entry_date(entry, "start")
        if start is None:
            continue
        end = _work_history_entry_date(entry, "end")
        if end is None and (entry.get("current") or not str(entry.get("end_date") or "").strip()):
            end = end_default
        if end is None or end < start:
            continue
        intervals.append((start, end))
    if not intervals:
        return None
    intervals.sort()
    merged: list[list[date]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    days = sum((end - start).days for start, end in merged)
    return max(0.0, days / 365.25)


def _professional_software_experience_range_answer(profile: dict[str, Any]) -> str | None:
    years = _profile_work_experience_years(profile)
    if years is None:
        raw_years = _years_experience_value(profile)
        nums = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", str(raw_years or ""))]
        if nums:
            years = max(nums)
    if years is None:
        return None
    if years < 2:
        return "Less than 2 years"
    if years < 5:
        return "2-5 years"
    return "5+ years"


def _zero_based_professional_experience_range_answer(profile: dict[str, Any]) -> str | None:
    raw_years = _years_experience_value(profile)
    nums = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", str(raw_years or ""))]
    if nums:
        years = max(nums)
    else:
        years = _profile_work_experience_years(profile)
    if years is None:
        return None
    if years <= 2:
        return "0-2 years"
    if years <= 4:
        return "3-4 years"
    if years <= 10:
        return "5-10 years"
    return "10+ years"


def _relevant_professional_experience_range_answer(profile: dict[str, Any]) -> str | None:
    raw_years = _years_experience_value(profile)
    nums = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", str(raw_years or ""))]
    if nums:
        years = max(nums)
    else:
        years = _profile_work_experience_years(profile)
    if years is None:
        return None
    if years <= 2:
        return "1-2 years"
    if years <= 5:
        return "3-5 years"
    if years <= 8:
        return "6-8 years"
    return "8+"


def _is_relevant_professional_experience_years_question(label: str) -> bool:
    normalized = _norm(label)
    if "how many years" not in normalized:
        return False
    if "relevant professional experience" in normalized:
        return True
    return (
        "relevant" in normalized
        and "post college" in normalized
        and "work experience" in normalized
    )


def _numeric_relevant_year_option(field: dict[str, Any], profile: dict[str, Any]) -> Any | None:
    if not _is_relevant_professional_experience_years_question(str(field.get("label") or "")):
        return None
    raw_years = _years_experience_value(profile)
    nums = [float(value) for value in re.findall(r"\d+(?:\.\d+)?", str(raw_years or ""))]
    if nums:
        years = min(nums)
    else:
        years = _profile_work_experience_years(profile)
    if years is None:
        return None
    years_text = str(int(years)) if float(years).is_integer() else str(years)
    for option in field.get("options") or []:
        if _norm(_option_text(option)) == years_text:
            return option
    return None


def _has_full_time_software_engineering_experience(profile: dict[str, Any]) -> bool:
    for entry in profile.get("work_history") or []:
        if not isinstance(entry, dict):
            continue
        role_text = _norm(
            " ".join(
                str(entry.get(key) or "")
                for key in ("title", "employment_type", "type")
            )
        )
        if not role_text:
            continue
        if "intern" in role_text:
            continue
        if "full time" not in role_text and "fulltime" not in role_text:
            continue
        if any(
            term in role_text
            for term in (
                "software engineer",
                "software engineering",
                "sde",
                "backend engineer",
                "full stack engineer",
                "fullstack engineer",
            )
        ):
            return True
    return False


def _adapter_profile_value(field: dict[str, Any], profile: dict[str, Any]) -> Any | None:
    key = adapter_profile_key_for_field(field, ats_name=detect_ats_from_url(profile.get("_application_url")))
    if not key:
        return None
    answers = profile.get("answers") or {}
    if key == "full_name":
        return profile.get("name")
    if key == "first_name":
        return profile.get("first_name") or str(profile.get("name") or "").split(" ")[0]
    if key == "last_name":
        return profile.get("last_name") or " ".join(str(profile.get("name") or "").split(" ")[1:])
    if key == "preferred_name":
        return profile.get("preferred_name") or profile.get("first_name") or str(profile.get("name") or "").split(" ")[0]
    if key == "email":
        return profile.get("email")
    if key == "phone":
        phone = profile.get("phone")
        # Greenhouse provides a separate country-code control. Passing the
        # international prefix to the local-number field leaves it invalid.
        if detect_ats_from_url(profile.get("_application_url")) == "greenhouse":
            return _greenhouse_phone_number(phone, _infer_phone_country_code(profile))
        return phone
    if key == "phone_country_code":
        return _infer_phone_country_code(profile)
    if key == "phone_device_type":
        workday_phone_type = _workday_phone_device_type_answer(field, profile)
        if workday_phone_type:
            return workday_phone_type
        return profile.get("phone_type") or "Mobile"
    if key == "linkedin_url":
        return profile.get("linkedin")
    if key == "github_url":
        return profile.get("github")
    if key == "twitter_url":
        return profile.get("twitter")
    if key == "website":
        return profile.get("website") or profile.get("portfolio")
    if key == "location":
        return profile.get("location") or profile.get("city")
    if key == "country":
        return _infer_country(profile)
    if key == "state":
        return profile.get("region") or profile.get("state")
    if key == "city":
        return profile.get("city") or _city_from_location(profile.get("location"))
    if key == "address_line_1":
        return (
            profile.get("address_line1")
            or profile.get("street_address")
            or profile.get("address")
            or answers.get("Address")
        )
    if key == "postal_code":
        return profile.get("postal_code") or profile.get("zip") or answers.get("Postal Code")
    if key == "current_company":
        return _current_work_value(profile, "company")
    if key == "cover_letter":
        return profile.get("cover_letter")
    if key == "resume_text":
        return profile.get("resume_text") or profile.get("resume")
    if key == "additional_info":
        return (profile.get("answers") or {}).get("Additional Information")
    return None


def _greenhouse_phone_number(phone: Any, country_code: Any) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    country_digits = re.sub(r"\D+", "", str(country_code or ""))
    if country_digits and digits.startswith(country_digits) and len(digits) > len(country_digits):
        return digits[len(country_digits):]
    return digits or str(phone or "")


def _desired_salary_range_value(profile: dict[str, Any]) -> str | None:
    raw = str(
        profile.get("minimum_expected_salary")
        or (profile.get("answers") or {}).get("What is your minimum expected salary?")
        or ""
    )
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k)?", raw, re.I)
    if not match:
        return None
    minimum = float(match.group(1).replace(",", ""))
    if match.group(2):
        minimum *= 1000
    ranges = [
        (25_000, "$25,000 to $50,000"),
        (50_001, "50,001 to 75,000"),
        (75_001, "75,001 to 100,000"),
        (100_001, "100,001 to 125,000"),
        (125_001, "125,001 to 150,000"),
        (150_001, "150,001 and above"),
    ]
    for lower_bound, label in ranges:
        if lower_bound >= minimum:
            return label
    return ranges[-1][1]


def _approved_salary_expectation_text(profile: dict[str, Any]) -> str:
    return str(
        _approved_sensitive_entry_answer(profile, "salary")
        or profile.get("minimum_expected_salary")
        or (profile.get("answers") or {}).get("What is your minimum expected salary?")
        or ""
    ).strip()


def _hourly_pay_expectation_value(profile: dict[str, Any]) -> str | None:
    """Convert the approved salary floor into a conservative hourly floor."""

    raw = _approved_salary_expectation_text(profile)
    if not raw:
        return None
    normalized = _norm(raw)
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(k)?", raw, re.I)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    if match.group(2):
        amount *= 1000
    if "hour" in normalized or amount < 1000:
        hourly = amount
    else:
        hourly = amount / 2080
    rounded = int(((hourly + 4) // 5) * 5)
    if rounded <= 0:
        rounded = int(hourly + 0.999)
    return f"At least ${rounded}/hour"


def _prefer_auto_answer_before_identity_mapping(label: str) -> bool:
    normalized = _norm(label)
    return (
        "describe a production genai application" in normalized
        and "business use case" in normalized
        and "model name" in normalized
    )


def _map_text_value(field_or_label: str | dict[str, Any], profile: dict[str, Any]) -> Any | None:
    """Resolve standard applicant fields through the shared semantic layer.

    Keep the legacy fallback rules below for unusual historical field names,
    but use structured field metadata first.  Passing the full descriptor
    allows section and ARIA metadata to disambiguate generic labels such as
    ``Month`` and ``Name``.
    """
    if isinstance(field_or_label, dict):
        label = " ".join(
            str(field_or_label.get(key) or "")
            for key in [
                "label",
                "id",
                "name",
                "section",
                "ariaLabel",
                "ariaDescription",
                "placeholder",
                "autocomplete",
            ]
        )
    else:
        label = field_or_label
    normalized = _norm(label)
    compact = normalized.replace(" ", "")
    today = date.today()
    if ("referred" in normalized or "referral" in normalized) and "employee" in normalized and "name" in normalized:
        return None
    if "employee id" in normalized and (
        "currently" in normalized
        or "previously" in normalized
        or "if you" in normalized
    ):
        return None
    workday_phone_type = _workday_phone_device_type_answer(field_or_label, profile)
    if workday_phone_type:
        return workday_phone_type
    if "suffix" in normalized:
        return profile.get("suffix") or (profile.get("answers") or {}).get("Suffix")
    if "middle name" in normalized:
        return profile.get("middle_name") or (profile.get("answers") or {}).get("Middle Name")
    if "address line 2" in normalized:
        return profile.get("address_line2") or (profile.get("answers") or {}).get("Address 2")
    if normalized == "county" or normalized.endswith(" county"):
        return profile.get("county") or (profile.get("answers") or {}).get("County")
    if any(
        token in normalized
        for token in [
            "salary",
            "compensation",
            "pay range",
            "pay expectation",
            "expected pay",
            "expected hourly",
            "hourly pay",
            "hourly rate",
            "salary expectation",
        ]
    ):
        return None
    if (
        "state" in normalized
        and ("currently reside" in normalized or "current residence" in normalized or "reside in" in normalized)
    ):
        return _profile_us_state_name(profile) or profile.get("region") or profile.get("state")
    if normalized == "state" or "state province" in normalized or "province" in normalized or "countryregion" in compact:
        return profile.get("region") or profile.get("state")
    if "high school" in normalized:
        if "year" in normalized or "graduation" in normalized:
            return profile.get("high_school_graduation_year") or _high_school_value(profile, "end_year")
        if "name" in normalized or "school" in normalized:
            return profile.get("high_school_name") or _high_school_value(profile, "school")
        return None
    semantic = classify_field(field_or_label)
    if semantic:
        mapped = value_for_semantic(semantic, profile, field_text=semantic.text)
        if mapped is not None and mapped != "":
            if (
                semantic.key == "contact.phone"
                and detect_ats_from_url(profile.get("_application_url")) == "greenhouse"
            ):
                return _greenhouse_phone_number(mapped, profile.get("phone_country_code"))
            return mapped
    if isinstance(field_or_label, dict):
        adapter_mapped = _adapter_profile_value(field_or_label, profile)
        if adapter_mapped is not None and adapter_mapped != "":
            return adapter_mapped
    # Breezy's stable field names survive browser translation, while the
    # visible labels may not. Treat these as provider-owned identity fields.
    if "cname" in compact:
        return profile.get("name")
    if "cemail" in compact:
        return profile.get("email")
    if "cphonenumber" in compact:
        return profile.get("phone")
    if "caddress" in compact:
        return profile.get("address_line1") or profile.get("street_address")
    if "ccoverletter" in compact:
        return profile.get("cover_letter")
    if "phone device type" in normalized or "phonetype" in compact:
        return "Mobile"
    if "extension" in normalized:
        return profile.get("phone_extension")
    if "email" in normalized or "e mail" in normalized:
        return profile.get("email")
    if "country phone code" in normalized or "phone country code" in normalized:
        country_code = _infer_phone_country_code(profile)
        if country_code == "+1":
            return "United States of America (+1)"
        return country_code
    if "phone number" in normalized:
        digits = re.sub(r"\D+", "", str(profile.get("phone") or ""))
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        return digits or profile.get("phone")
    if any(token in normalized for token in ["phone", "mobile", "telephone", "contact number"]):
        return profile.get("phone")
    if "linkedin" in normalized:
        return profile.get("linkedin")
    if "github" in normalized:
        return profile.get("github")
    if "portfolio" in normalized:
        return profile.get("portfolio") or profile.get("website")
    if "website" in normalized or "personal site" in normalized or "homepage" in normalized:
        return profile.get("website") or profile.get("portfolio")
    if "cover letter" in normalized:
        return profile.get("cover_letter")
    if _has_phrase(normalized, "country") and len(normalized.split()) <= 8:
        return _infer_country(profile)
    if (
        normalized in {"address", "address 1"}
        or "resumatoraddressvalue" in compact
        or "address line 1" in normalized
        or "street address" in normalized
        or "mailing address" in normalized
    ):
        return (
            profile.get("address_line1")
            or profile.get("street_address")
            or profile.get("address")
            or (profile.get("answers") or {}).get("Address")
        )
    if "address line 2" in normalized:
        return profile.get("address_line2") or (profile.get("answers") or {}).get("Address 2")
    if "postal code" in normalized or "zip code" in normalized or normalized == "zip":
        return profile.get("postal_code") or profile.get("zip") or (profile.get("answers") or {}).get("Postal Code")
    if _has_phrase(normalized, "city"):
        return profile.get("city") or _city_from_location(profile.get("location"))
    if re.search(r"\baddress\b", normalized):
        return profile.get("address_line1") or profile.get("street_address")
    if _has_phrase(normalized, "location"):
        return profile.get("location") or profile.get("city")
    if "first name" in normalized:
        return profile.get("first_name") or str(profile.get("name") or "").split(" ")[0]
    if "last name" in normalized:
        return profile.get("last_name") or " ".join(str(profile.get("name") or "").split(" ")[1:])
    if "preferred name" in normalized:
        return profile.get("preferred_name") or profile.get("first_name") or str(profile.get("name") or "").split(" ")[0]
    if "pronunciation" in normalized or "pronounce" in normalized:
        return profile.get("name_pronunciation") or profile.get("pronunciation")
    if "legal name" in normalized or "full name" in normalized or "your name" in normalized or normalized == "name":
        return profile.get("name")
    if "selfidentifieddisabilitydata" in compact and "name" in normalized:
        return profile.get("name")
    if "date of birth" in normalized or "birthday" in normalized:
        return profile.get("birthday")
    if normalized == "date":
        return f"{today.month:02d}/{today.day:02d}/{today.year}"
    if "datesignedon" in compact and "month" in normalized:
        return f"{today.month:02d}"
    if "datesignedon" in compact and "day" in normalized:
        return f"{today.day:02d}"
    if "datesignedon" in compact and "year" in normalized:
        return str(today.year)
    if "datesignedon" in compact or "date signed" in normalized or "date of signature" in normalized:
        return today.isoformat()
    if (
        "currently based in any of these countries" in normalized
        or ("countries where we are accepting applications" in normalized and "currently based" in normalized)
    ):
        return _target_application_country(profile) or _infer_country(profile)
    if "currently located" in normalized or "current location" in normalized or "currently based" in normalized:
        return profile.get("location") or profile.get("city")
    if "current company" in normalized or "current employer" in normalized or normalized == "company" or "companyname" in compact:
        return _current_work_value(profile, "company")
    if "current title" in normalized or "current role" in normalized or "current position" in normalized or "job title" in normalized or "jobtitle" in compact:
        return _current_work_value(profile, "title")
    if "role description" in normalized or "roledescription" in compact:
        return _current_work_value(profile, "description")
    # Greenhouse renders education end dates as a React month combobox and a
    # separate numeric year input. Its IDs omit an education prefix, so the
    # scraper carries the enclosing section into ``mapping_label``.
    if "education" in compact:
        if "start" in normalized and "month" in normalized:
            return _education_date_part(profile, "start", "month")
        if "start" in normalized and "year" in normalized:
            return _education_date_part(profile, "start", "year")
        if "end" in normalized and "month" in normalized:
            return _education_date_part(profile, "end", "month")
        if "end" in normalized and "year" in normalized:
            return _education_date_part(profile, "end", "year")
    if "startdate" in compact and "month" in normalized:
        return _current_work_value(profile, "start_month")
    if "startdate" in compact and "year" in normalized:
        return _current_work_value(profile, "start_year")
    if "enddate" in compact and "month" in normalized:
        return _current_work_value(profile, "end_month")
    if "enddate" in compact and "year" in normalized:
        return _current_work_value(profile, "end_year")
    if "years" in normalized and "experience" in normalized:
        return _years_experience_value(profile)
    if "graduation date" in normalized or "anticipated graduation" in normalized or "when will you graduate" in normalized:
        return profile.get("graduation_date") or _format_education_end_date(profile)
    if "preferred programming language" in normalized:
        return profile.get("preferred_programming_language") or "Python"
    if (
        len(normalized.split()) <= 14
        and any(_has_phrase(normalized, term) for term in ["university", "school", "college", "institution"])
    ):
        return _current_education_value(profile, "school")
    if "degree" in normalized:
        return _current_education_value(profile, "degree")
    if "field of study" in normalized or "major" in normalized:
        return _current_education_value(profile, "field")
    if "gradeaverage" in compact or "gpa" in normalized:
        return _current_education_value(profile, "gpa")
    if "firstyearattended" in compact:
        return _current_education_value(profile, "start_year")
    if "lastyearattended" in compact:
        return _current_education_value(profile, "end_year")
    return None


def _is_optional_blank_field(label: str) -> bool:
    normalized = _norm(label)
    return (
        "optional" in normalized
        or "middle name" in normalized
        or "suffix" in normalized
        or "address line 2" in normalized
        or normalized == "county"
        or normalized.endswith(" county")
        or "phone extension" in normalized
        or normalized == "extension"
        or "type to add skills" in normalized
        or "employee id" in normalized
        or (
            ("referred" in normalized or "referral" in normalized)
            and "enter n a" not in normalized
            and (
                "employee name" in normalized
                or ("employee" in normalized and "name" in normalized)
                or "referring individual" in normalized
            )
        )
        or "additional information" in normalized
        or "anything else" in normalized
    )


def _is_honeypot_field(label: str) -> bool:
    normalized = _norm(label)
    return normalized.startswith("hp ") or any(
        marker in normalized
        for marker in [
            "robots only",
            "do not enter if you re human",
            "do not fill",
            "leave this field blank",
            "website this input is for robots",
        ]
    )


def _scrape_fields(page) -> list[dict[str, Any]]:
    return page.evaluate(
        """() => {
          const visible = (node) => {
            if (!node) return false;
            if (node.getAttribute && node.getAttribute("aria-hidden") === "true") return false;
            const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
            if (style && (
              style.display === "none"
              || style.visibility === "hidden"
              || style.visibility === "collapse"
            )) return false;
            if (node.offsetParent) return true;
            const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
            if (!rects || rects.length === 0) return false;
            return Array.from(rects).some((rect) => rect.width > 0 && rect.height > 0);
          };
          const hitVisible = (node) => {
            if (!visible(node)) return false;
            if (typeof document.elementFromPoint !== "function" || typeof node.getBoundingClientRect !== "function") return true;
            const rects = Array.from(node.getClientRects ? node.getClientRects() : [])
              .filter((rect) => rect.width > 0 && rect.height > 0);
            const rect = rects[0] || node.getBoundingClientRect();
            if (!rect || rect.width <= 0 || rect.height <= 0) return false;
            if (
              rect.bottom < 0
              || rect.top > window.innerHeight
              || rect.right < 0
              || rect.left > window.innerWidth
            ) return true;
            const points = [
              [rect.left + rect.width / 2, rect.top + rect.height / 2],
              [rect.left + Math.min(rect.width - 1, 4), rect.top + Math.min(rect.height - 1, 4)],
              [rect.right - Math.min(rect.width - 1, 4), rect.bottom - Math.min(rect.height - 1, 4)],
            ];
            const ownWorkdayField = node.closest && node.closest('[data-automation-id^="formField-"]');
            for (const [rawX, rawY] of points) {
              const x = Math.max(0, Math.min(window.innerWidth - 1, rawX));
              const y = Math.max(0, Math.min(window.innerHeight - 1, rawY));
              const top = document.elementFromPoint(x, y);
              if (!top) continue;
              if (top === node || node.contains(top) || top.contains(node)) return true;
              if (
                ownWorkdayField
                && top.closest
                && top.closest('[data-automation-id^="formField-"]') === ownWorkdayField
              ) return true;
            }
            return false;
          };
          const textForIds = (ids) => (ids || "").split(/\\s+/)
            .map((id) => id && document.getElementById(id))
            .filter((node) => node && node.textContent)
            .map((node) => node.textContent.trim())
            .filter(Boolean)
            .join(" ");
          const cleanQuestionText = (text) => {
            const lines = (text || "").split("\\n").map((line) => line.trim()).filter(Boolean);
            const keep = [];
            for (const line of lines) {
              if (line === "✱" || line === "*" || /^select(\\.\\.\\.)?$/i.test(line) || /^(yes|no|upload|attach)/i.test(line)) break;
              keep.push(line);
            }
            return keep.join(" ");
          };
          const optionLabelFor = (control) => {
            if (control.id) {
              const explicit = Array.from(document.querySelectorAll("label")).find((label) =>
                label.htmlFor === control.id || label.getAttribute("for") === control.id
              );
              if (explicit && explicit.textContent) return explicit.textContent.trim();
            }
            const wrapping = control.closest("label");
            if (wrapping && wrapping.textContent) {
              const clone = wrapping.cloneNode(true);
              clone.querySelectorAll("select,input,textarea,button").forEach((node) => node.remove());
              const text = clone.textContent.trim();
              if (text) return text;
            }
            const option = control.closest("li.option");
            if (option && option.textContent) {
              const clone = option.cloneNode(true);
              clone.querySelectorAll("select,input,textarea,button").forEach((node) => node.remove());
              const text = clone.textContent.trim();
              if (text) return text;
            }
            return control.getAttribute("aria-label") || control.getAttribute("data-value") ||
              control.getAttribute("data-option-value") || control.value || (control.textContent || "").trim() || "";
          };
          const workdayButtonLabel = (control) => {
            const aria = (control.getAttribute("aria-label") || "").trim();
            const text = (control.textContent || "").trim();
            let raw = aria || text || control.name || control.id || "";
            raw = raw.replace(/\\b(required|select one|mobile|united states of america|\\(\\+1\\))\\b/gi, " ");
            raw = raw.replace(/\\s+/g, " ").trim();
            return raw || control.name || control.id || "";
          };
          const workdaySelectedText = (control) => {
            const field = control.closest('[data-automation-id^="formField-"]');
            if (!field) return "";
            const selected = Array.from(field.querySelectorAll('[data-automation-id="selectedItem"]'))
              .map((node) => (node.textContent || "").trim())
              .filter(Boolean);
            if (selected.length) return Array.from(new Set(selected)).join(", ");
            const text = field.textContent || "";
            const match = text.match(/1 item selected,?\\s*([^\\n]+?)(?:\\1)?(?:Error:|$)/i);
            return match ? match[1].trim() : "";
          };
          const leverQuestionLabel = (control) => {
            const question = control.closest(".application-question");
            if (!question) return "";
            const direct = Array.from(question.children)
              .map((node) => cleanQuestionText(node.innerText || node.textContent || ""))
              .find((text) => text && !/^(yes|no|select|select\\.\\.\\.|upload|attach)/i.test(text));
            if (direct) return direct;
            return cleanQuestionText(question.innerText || question.textContent || "");
          };
          const breezyQuestionLabel = (control) => {
            const question = control.closest("li.question");
            if (!question) return "";
            const heading = question.querySelector("h1,h2,h3,h4,h5,h6");
            if (!heading) return "";
            const clone = heading.cloneNode(true);
            clone.querySelectorAll(".required,input,textarea,select,button").forEach((node) => node.remove());
            return cleanQuestionText(clone.innerText || clone.textContent || "");
          };
          const fieldEntryLabel = (control) => {
            const entry = control.closest(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]");
            if (!entry) return "";
            const explicit = entry.querySelector("label,.ashby-application-form-question-title");
            if (explicit && explicit.textContent) return explicit.textContent.trim();
            return "";
          };
          const workdayFieldLabel = (control) => {
            const field = control.closest('[data-automation-id^="formField-"]');
            if (!field) return "";
            const explicit = field.querySelector("label,[data-automation-id='formLabel']");
            const explicitText = explicit && explicit.textContent ? explicit.textContent.trim() : "";
            const isGenericCheckboxLabel = (
              (control.type || "").toLowerCase() === "checkbox"
              && /^(agree|accept|yes|i agree)$/i.test(explicitText)
            );
            let text = (explicitText && !isGenericCheckboxLabel) ? explicitText : (field.textContent || "");
            text = text.replace(/\\b\\d+\\s+items?\\s+selected\\b.*$/i, "");
            text = text.replace(/\\bExpanded\\b.*$/i, "");
            text = text.replace(/\\bError:.*$/i, "");
            if ((control.type || "").toLowerCase() === "file") {
              return text.split(/Drop files here|orSelect files|Select files/i)[0].trim();
            }
            return cleanQuestionText(text);
          };
          const workdayQuestionLabel = (control) => {
            const field = control.closest('[data-automation-id^="formField-"]');
            if (!field || !field.textContent) return "";
            const raw = field.textContent.trim();
            if (raw.includes("*")) return (raw.split("*")[0] + "*").trim();
            return workdayFieldLabel(control);
          };
          const workdayContainerSelectLabel = (field) => {
            if (!field || !field.textContent) return "";
            const explicit = field.querySelector("label,[data-automation-id='formLabel']");
            if (explicit && explicit.textContent && explicit.textContent.trim()) {
              return explicit.textContent.trim();
            }
            const text = (field.textContent || "")
              .replace(/\\bError:.*$/i, "")
              .replace(/\\bExpanded\\b.*$/i, "")
              .trim();
            const match = text.match(/(.*?\\*)\\s*Select One/i);
            return match ? cleanQuestionText(match[1]) : "";
          };
          const workdayOptionLabel = (control) => {
            let node = control.parentElement;
            const field = control.closest('[data-automation-id^="formField-"]');
            while (node && node !== field) {
              const text = (node.textContent || "").trim();
              if (text) return text;
              node = node.parentElement;
            }
            return optionLabelFor(control);
          };
          const questionnaireLabel = (control) => {
            const optionText = cleanQuestionText(optionLabelFor(control));
            let node = control.parentElement;
            for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
              const text = cleanQuestionText(node.textContent || "");
              if (!text || text === optionText) continue;
              if (text.length > optionText.length + 10) return text;
            }
            return "";
          };
          const textWithoutControls = (node) => {
            if (!node) return "";
            const clone = typeof node.cloneNode === "function" ? node.cloneNode(true) : node;
            if (clone && typeof clone.querySelectorAll === "function") {
              clone.querySelectorAll("input,textarea,select,button,[role='option'],[role='radio'],[role='checkbox']")
                .forEach((child) => child.remove());
            }
            return cleanQuestionText(clone.innerText || clone.textContent || "");
          };
          const isPromptNode = (node) => {
            if (!node || !node.getAttribute) return false;
            const marker = [
              node.tagName || "", node.id || "", node.className || "",
              node.getAttribute("role") || "", node.getAttribute("data-testid") || "",
              node.getAttribute("data-qa") || "", node.getAttribute("data-test") || "",
              node.getAttribute("data-field-label") || "", node.getAttribute("data-question") || "",
            ].join(" ").toLowerCase();
            return /(^|\\s)(label|legend)(\\s|$)|question|prompt|field.?title|form.?title|heading/.test(marker) ||
              /^H[1-6]$/.test(node.tagName || "") || node.getAttribute("role") === "heading";
          };
          const genericPromptLabel = (control) => {
            let node = control;
            for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
              const parent = node.parentElement;
              if (parent && parent.children) {
                const siblings = Array.from(parent.children);
                const index = siblings.indexOf(node);
                for (const sibling of siblings.slice(0, index).reverse()) {
                  if (!isPromptNode(sibling)) continue;
                  const text = textWithoutControls(sibling);
                  if (text) return text;
                }
              }
              const candidates = typeof node.querySelectorAll === "function"
                ? Array.from(node.querySelectorAll(
                  "label,legend,h1,h2,h3,h4,h5,h6,[role='heading'],[data-field-label],[data-question],[data-question-label],[data-label],[data-testid],[data-qa],[data-test]"
                )).filter((candidate) => candidate !== control && typeof candidate.contains === "function" && !candidate.contains(control) && isPromptNode(candidate))
                : [];
              for (const candidate of candidates) {
                const text = textWithoutControls(candidate);
                if (text) return text;
              }
            }
            return "";
          };
          const labelFor = (control) => {
            const breezyLabel = breezyQuestionLabel(control);
            if (breezyLabel) return breezyLabel;
            const leverLabel = leverQuestionLabel(control);
            if (leverLabel) return leverLabel;
            const fieldLabel = fieldEntryLabel(control);
            if (fieldLabel) return fieldLabel;
            const workdayLabel = workdayFieldLabel(control);
            if (workdayLabel) return workdayLabel;
            const labelledBy = textForIds(control.getAttribute("aria-labelledby"));
            if (labelledBy) return labelledBy;
            if (control.id) {
              const explicit = Array.from(document.querySelectorAll("label")).find((label) =>
                label.htmlFor === control.id || label.getAttribute("for") === control.id
              );
              if (explicit && explicit.textContent) return explicit.textContent.trim();
            }
            const wrapping = control.closest("label");
            if (wrapping && wrapping.textContent) {
              const clone = wrapping.cloneNode(true);
              clone.querySelectorAll("select,input,textarea,button").forEach((n) => n.remove());
              const txt = clone.textContent.trim();
              if (txt) return txt;
            }
            const genericPrompt = genericPromptLabel(control);
            if (genericPrompt) return genericPrompt;
            const describedBy = textForIds(control.getAttribute("aria-describedby"));
            return control.getAttribute("aria-label") || control.getAttribute("placeholder") || describedBy || control.name || "";
          };
          const groupLabelFor = (control) => {
            const breezyLabel = breezyQuestionLabel(control);
            if (breezyLabel) return breezyLabel;
            const leverLabel = leverQuestionLabel(control);
            if (leverLabel) return leverLabel;
            const fieldLabel = fieldEntryLabel(control);
            if (fieldLabel) return fieldLabel;
            const fs = control.closest("fieldset");
            if (fs) {
              const legend = fs.querySelector("legend");
              if (legend && legend.textContent) return legend.textContent.trim();
            }
            const labelledBy = textForIds(control.getAttribute("aria-labelledby"));
            if (labelledBy) return labelledBy;
            const genericPrompt = genericPromptLabel(control);
            if (genericPrompt) return genericPrompt;
            const describedBy = textForIds(control.getAttribute("aria-describedby"));
            return control.getAttribute("aria-label") || describedBy || control.getAttribute("name") || "";
          };
          const sectionFor = (control) => {
            let node = control;
            for (let depth = 0; node && depth < 10; depth++, node = node.parentElement) {
              const marker = [
                node.id || "",
                typeof node.className === "string" ? node.className : "",
                node.getAttribute ? (node.getAttribute("data-automation-id") || "") : "",
              ].join(" ").toLowerCase();
              if (marker.includes("education")) return "education";
              if (marker.includes("employment") || marker.includes("work-history") || marker.includes("work_history") || marker.includes("work-experience") || marker.includes("workexperience") || marker.includes("employment-history") || marker.includes("employmenthistory")) return "work";
            }
            return "";
          };
          const metadataFor = (control) => ({
            ariaLabel: control.getAttribute("aria-label") || "",
            ariaDescription: textForIds(control.getAttribute("aria-describedby")),
            placeholder: control.getAttribute("placeholder") || "",
            autocomplete: control.getAttribute("autocomplete") || "",
            automationId: control.getAttribute("data-automation-id") || "",
            ariaControls: control.getAttribute("aria-controls") || "",
            ariaOwns: control.getAttribute("aria-owns") || "",
            contentEditable: Boolean(control.isContentEditable),
          });
          const ashbyRequired = (control) => {
            const entry = control && control.closest && control.closest(
              '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
            );
            const label = entry && entry.querySelector(
              'label,.ashby-application-form-question-title'
            );
            return Boolean(label && /(^|\\s)[^\\s]*required[^\\s]*(\\s|$)/i.test(String(label.className || "")));
          };
          const out = [];
          const radiosByName = {};
          const checkboxesByName = {};
          let autofillIndex = 0;
          let customGroupIndex = 0;
          document.querySelectorAll("[data-job-agent-autofill-index]").forEach((node) => {
            node.removeAttribute("data-job-agent-autofill-index");
          });
          document.querySelectorAll("input, textarea, select").forEach((control) => {
            const type = (control.type || control.tagName).toLowerCase();
            if (["hidden", "submit", "button", "image"].includes(type)) return;
            const workdayField = control.closest('[data-automation-id^="formField-"]');
            const allowHiddenWorkdayCheckbox = type === "checkbox" && workdayField && hitVisible(workdayField);
            if (type !== "file" && !hitVisible(control) && !allowHiddenWorkdayCheckbox) return;
            const autofillId = String(autofillIndex++);
            control.setAttribute("data-job-agent-autofill-index", autofillId);
            if (type === "radio") {
              const label = groupLabelFor(control);
              const name = control.name || label || control.id || control.value || autofillId;
              if (!radiosByName[name]) {
                radiosByName[name] = { kind: "radiogroup", type: "radio", label, name, required: false, options: [] };
              }
              radiosByName[name].options.push({ id: control.id, value: control.value, label: optionLabelFor(control), autofillId });
              if (control.required || control.getAttribute("aria-required") === "true" || ashbyRequired(control)) radiosByName[name].required = true;
              return;
            }
            if (type === "checkbox") {
              const breezyLabel = breezyQuestionLabel(control);
              const breezyQuestion = control.closest("li.question");
              const breezyBoxes = breezyQuestion
                ? Array.from(breezyQuestion.querySelectorAll('input[type="checkbox"]')).filter(hitVisible)
                : [];
              if (breezyLabel && breezyBoxes.length > 1 && control.name) {
                if (!checkboxesByName[control.name]) {
                  checkboxesByName[control.name] = {
                    kind: "checkboxgroup", type: "checkbox", label: breezyLabel,
                    name: control.name, required: false, options: [],
                  };
                }
                checkboxesByName[control.name].options.push({
                  id: control.id, value: control.value, label: optionLabelFor(control), autofillId
                });
                if (control.required || control.getAttribute("aria-required") === "true") checkboxesByName[control.name].required = true;
                return;
              }
              const questionLabel = questionnaireLabel(control);
              if (questionLabel && /true and accurate|false or misleading|i certify|i confirm/i.test(questionLabel)) {
                out.push({
                  kind: "single", tag: "input", type: "checkbox",
                  label: questionLabel, id: control.id || "", name: control.name || "",
                  role: control.getAttribute("role") || "", autofillId,
                  required: Boolean(control.required || /\\*/.test(questionLabel)), options: [], value: control.checked ? control.value : "",
                });
                return;
              }
              const ashbyLabel = fieldEntryLabel(control);
              if (ashbyLabel) {
                const name = ashbyLabel;
                if (!checkboxesByName[name]) {
                  checkboxesByName[name] = {
                    kind: "checkboxgroup", type: "checkbox", label: ashbyLabel,
                    name, required: false, options: [],
                  };
                }
                checkboxesByName[name].options.push({
                  id: control.id, value: control.value, label: optionLabelFor(control), autofillId
                });
                if (control.required || control.getAttribute("aria-required") === "true") checkboxesByName[name].required = true;
                return;
              }
              const workdayField = control.closest('[data-automation-id^="formField-"]');
              const workdayBoxes = workdayField
                ? Array.from(workdayField.querySelectorAll('input[type="checkbox"]')).filter(hitVisible)
                : [];
              if (workdayBoxes.length > 1) {
                const groupLabel = workdayQuestionLabel(control);
                const name = workdayField.getAttribute("data-automation-id") || groupLabel || autofillId;
                if (!checkboxesByName[name]) {
                  checkboxesByName[name] = {
                    kind: "checkboxgroup", type: "checkbox",
                    label: groupLabel, name, required: false, options: [],
                  };
                }
                checkboxesByName[name].options.push({
                  id: control.id, value: control.value, label: workdayOptionLabel(control), autofillId
                });
                return;
              }
              const groupLabel = leverQuestionLabel(control);
              if (groupLabel && control.name) {
                if (!checkboxesByName[control.name]) {
                  checkboxesByName[control.name] = {
                    kind: "checkboxgroup", type: "checkbox", label: groupLabel,
                    name: control.name, required: false, options: [],
                  };
                }
                checkboxesByName[control.name].options.push({
                  id: control.id, value: control.value, label: optionLabelFor(control), autofillId
                });
                if (control.required || control.getAttribute("aria-required") === "true") checkboxesByName[control.name].required = true;
                return;
              }
              const genericGroupLabel = groupLabelFor(control);
              const genericOptionLabel = optionLabelFor(control);
              const sameNameBoxes = control.name
                ? Array.from(document.querySelectorAll('input[type="checkbox"]')).filter((box) =>
                    box.name === control.name && hitVisible(box)
                  )
                : [];
              if (
                genericGroupLabel &&
                control.name &&
                sameNameBoxes.length > 1 &&
                cleanQuestionText(genericGroupLabel) !== cleanQuestionText(genericOptionLabel)
              ) {
                if (!checkboxesByName[control.name]) {
                  checkboxesByName[control.name] = {
                    kind: "checkboxgroup", type: "checkbox", label: genericGroupLabel,
                    name: control.name, required: false, options: [],
                  };
                }
                checkboxesByName[control.name].options.push({
                  id: control.id, value: control.value, label: genericOptionLabel, autofillId
                });
                if (
                  control.required ||
                  control.getAttribute("aria-required") === "true" ||
                  /\\*/.test(genericGroupLabel)
                ) checkboxesByName[control.name].required = true;
                return;
              }
            }
            const tag = control.tagName.toLowerCase();
            const options = tag === "select"
              ? Array.from(control.options).map((option) => option.textContent.trim()).filter(Boolean)
              : [];
            const label = labelFor(control) || workdayFieldLabel(control);
            if (!label && !control.id && !control.name) return;
            out.push({
              kind: "single", tag, type: (control.getAttribute("type") || tag).toLowerCase(),
              label, id: control.id || "", name: control.name || "", role: control.getAttribute("role") || "",
              section: sectionFor(control), autofillId, ...metadataFor(control),
              required: Boolean(control.required || control.getAttribute("aria-required") === "true" || /\\*/.test(label)),
              options, value: control.value || workdaySelectedText(control),
            });
          });
          // Many company sites use a button or div with ARIA combobox semantics
          // instead of a native input/select.  Discover those controls by
          // capability so the planner does not need an ATS-specific branch.
          document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"]').forEach((control) => {
            if (!hitVisible(control) || control.matches("input,textarea,select")) return;
            if (control.matches('button[type="submit"], button[type="reset"]')) return;
            const label = labelFor(control) || workdayFieldLabel(control) || workdayButtonLabel(control);
            if (!label && !control.id && !control.getAttribute("name")) return;
            const autofillId = String(autofillIndex++);
            control.setAttribute("data-job-agent-autofill-index", autofillId);
            const selected = workdaySelectedText(control)
              || control.getAttribute("aria-valuetext")
              || control.value
              || (control.textContent || "").trim();
            out.push({
              kind: "single", tag: control.tagName.toLowerCase(),
              type: (control.getAttribute("type") || control.tagName).toLowerCase(),
              label, id: control.id || "", name: control.getAttribute("name") || "",
              role: control.getAttribute("role") || "combobox",
              section: sectionFor(control), autofillId, ...metadataFor(control),
              required: Boolean(control.getAttribute("aria-required") === "true" || control.closest('[aria-required="true"]') || /\\*/.test(label)),
              options: [], value: selected,
            });
          });
          // Rich-text inputs and ARIA choice controls are common in bespoke
          // application forms. Treat their semantics as the contract instead
          // of requiring a particular ATS DOM or a native input element.
          document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"]').forEach((control) => {
            if (!hitVisible(control) || control.matches('[role="combobox"], [aria-haspopup]')) return;
            const label = labelFor(control);
            if (!label && !control.id && !control.getAttribute("name")) return;
            const autofillId = String(autofillIndex++);
            control.setAttribute("data-job-agent-autofill-index", autofillId);
            out.push({
              kind: "single", tag: control.tagName.toLowerCase(), type: "contenteditable",
              label, id: control.id || "", name: control.getAttribute("name") || "",
              role: control.getAttribute("role") || "textbox",
              section: sectionFor(control), autofillId, ...metadataFor(control),
              required: Boolean(control.getAttribute("aria-required") === "true" || control.closest('[aria-required="true"]') || /\\*/.test(label)),
              options: [], value: (control.textContent || "").trim(),
            });
          });
          // A few component libraries implement a textbox as a focusable div
          // with ARIA semantics instead of `contenteditable`. It still has a
          // label and an input event contract, so expose it to the same
          // generic planner rather than treating the host site as a special
          // case.
          document.querySelectorAll('[role="textbox"], [role="searchbox"]').forEach((control) => {
            if (!visible(control) || control.matches('input,textarea,select,[contenteditable="true"],[contenteditable="plaintext-only"],[role="combobox"],[aria-haspopup]')) return;
            const label = labelFor(control);
            if (!label && !control.id && !control.getAttribute("name")) return;
            const autofillId = String(autofillIndex++);
            control.setAttribute("data-job-agent-autofill-index", autofillId);
            out.push({
              kind: "single", tag: control.tagName.toLowerCase(), type: "aria-textbox",
              label, id: control.id || "", name: control.getAttribute("name") || "",
              role: control.getAttribute("role") || "textbox",
              section: sectionFor(control), autofillId, ...metadataFor(control),
              required: Boolean(control.getAttribute("aria-required") === "true" || control.closest('[aria-required="true"]')),
              options: [], value: control.getAttribute("aria-valuetext") || (control.textContent || "").trim(),
            });
          });
          const customGroups = {};
          const customGroupFor = (control, kind) => {
            const groupRole = kind === "radio" ? "radiogroup" : "group";
            const root = control.closest(
              `[role="${groupRole}"], fieldset, [role="group"], [data-field], [data-question], [data-field-path]`
            ) || control.parentElement || control;
            let marker = root.getAttribute && root.getAttribute("data-job-agent-group-index");
            if (!marker) {
              marker = String(customGroupIndex++);
              if (root.setAttribute) root.setAttribute("data-job-agent-group-index", marker);
            }
            return { root, key: `${kind}:${marker}` };
          };
          document.querySelectorAll('[role="radio"], [role="checkbox"]').forEach((control) => {
            if (!hitVisible(control) || control.matches("input") || control.closest('[role="listbox"], [role="menu"]')) return;
            const type = control.getAttribute("role") === "radio" ? "radio" : "checkbox";
            const kind = type === "radio" ? "radiogroup" : "checkboxgroup";
            const group = customGroupFor(control, type);
            const groupLabel = groupLabelFor(control) || labelFor(group.root) || "";
            if (!customGroups[group.key]) {
              customGroups[group.key] = {
                kind, type, label: groupLabel, name: group.key,
                required: false, options: [], custom: true,
              };
            }
            const autofillId = String(autofillIndex++);
            control.setAttribute("data-job-agent-autofill-index", autofillId);
            customGroups[group.key].options.push({
              id: control.id || "", value: control.getAttribute("data-value") || control.getAttribute("value") || "",
              label: optionLabelFor(control), autofillId, custom: true,
              tag: control.tagName.toLowerCase(), role: control.getAttribute("role") || "",
              checked: control.getAttribute("aria-checked") === "true",
            });
            if (
              control.getAttribute("aria-required") === "true" ||
              (group.root && group.root.getAttribute && group.root.getAttribute("aria-required") === "true") ||
              (group.root && group.root.closest && group.root.closest('[aria-required="true"]'))
            ) customGroups[group.key].required = true;
          });
          document.querySelectorAll(".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]").forEach((entry) => {
            const labelNode = entry.querySelector("label,.ashby-application-form-question-title");
            const label = labelNode && labelNode.textContent ? labelNode.textContent.trim() : "";
            if (!label) return;
            const buttons = Array.from(entry.querySelectorAll("button"))
              .filter(hitVisible)
              .map((button) => ({ node: button, text: (button.textContent || button.value || "").trim() }))
              .filter((button) => button.text && !/upload|submit application|apply/i.test(button.text));
            if (buttons.length < 2) return;
            const options = [];
            buttons.forEach((button) => {
              const autofillId = String(autofillIndex++);
              button.node.setAttribute("data-job-agent-autofill-index", autofillId);
              options.push({ label: button.text, value: button.text, autofillId });
            });
            out.push({ kind: "buttongroup", type: "button", label, name: label, required: ashbyRequired(entry), options });
          });
          document.querySelectorAll('button[name][id], [data-automation-id^="formField-"] button').forEach((button) => {
            if (!hitVisible(button)) return;
            if (button.getAttribute("role") === "combobox" || button.getAttribute("aria-haspopup")) return;
            const name = button.getAttribute("name") || "";
            const id = button.id || "";
            const formField = button.closest('[data-automation-id^="formField-"]');
            if (!["country", "countryRegion", "phoneType", "degree", "veteranStatus", "gender", "ethnicity"].includes(name) && !id.startsWith("primaryQuestionnaire--") && !formField) return;
            const text = (button.textContent || "").trim();
            if (/upload|select files|remove|back|save and continue/i.test(text)) return;
            const autofillId = String(autofillIndex++);
            button.setAttribute("data-job-agent-autofill-index", autofillId);
            const label = workdayFieldLabel(button) || workdayButtonLabel(button);
            if (!label || out.some((field) => field.tag === "button" && field.label === label)) return;
            const required = Boolean(
              button.getAttribute("aria-required") === "true" ||
              button.closest('[aria-required="true"]') ||
              /(^|\\s)required(\\s|$)/i.test(String(button.getAttribute("aria-label") || "")) ||
              /\\*/.test(label)
            );
            out.push({
              kind: "single", tag: "button", type: "button",
              label, id, name,
              role: button.getAttribute("role") || "", autofillId, required,
              options: [], value: text,
            });
          });
          document.querySelectorAll('[data-automation-id^="formField-"]').forEach((field) => {
            if (!visible(field)) return;
            const rawText = (field.textContent || "").replace(/\\s+/g, " ").trim();
            if (!/\\bSelect One\\b/i.test(rawText)) return;
            const label = workdayContainerSelectLabel(field);
            if (!label || out.some((item) => item.label === label)) return;
            const target = field.querySelector(
              'button, [role="combobox"], [aria-haspopup], input, [tabindex]:not([tabindex="-1"])'
            ) || field;
            const autofillId = String(autofillIndex++);
            target.setAttribute("data-job-agent-autofill-index", autofillId);
            out.push({
              kind: "single",
              tag: target.tagName.toLowerCase(),
              type: (target.getAttribute("type") || target.tagName).toLowerCase(),
              label,
              id: target.id || "",
              name: target.getAttribute("name") || "",
              role: target.getAttribute("role") || "combobox",
              section: sectionFor(target),
              autofillId,
              ...metadataFor(target),
              required: Boolean(
                target.getAttribute("aria-required") === "true" ||
                field.getAttribute("aria-required") === "true" ||
                /\\*/.test(label)
              ),
              options: [],
              value: workdaySelectedText(target) || "Select One",
            });
          });
          Object.values(radiosByName).forEach((group) => out.push(group));
          Object.values(checkboxesByName).forEach((group) => out.push(group));
          Object.values(customGroups).forEach((group) => {
            if (group.type !== "checkbox" || group.options.length !== 1) {
              out.push(group);
              return;
            }
            const option = group.options[0];
            out.push({
              kind: "single", tag: option.tag, type: "checkbox",
              label: group.label || option.label, id: option.id || "", name: group.name,
              role: "checkbox", autofillId: option.autofillId, required: group.required,
              options: [], value: option.checked ? "true" : "",
            });
          });
          return out;
        }"""
    )


def _audit_required_fields(page) -> list[dict[str, str]]:
    """Return visible required controls whose browser state is still invalid.

    Framework-owned fields can discard a successful-looking ``fill`` call.  A
    browser-side audit catches that state before the runtime advances or clicks
    Submit, allowing the normal self-heal pass to retry the exact page.
    """
    try:
        findings = page.evaluate(
            """() => {
              const visible = (node) => {
                if (!node || node.getAttribute("aria-hidden") === "true") return false;
                if (node.offsetParent) return true;
                const rects = node.getClientRects ? node.getClientRects() : [];
                const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
                return Boolean(rects.length) && !(style && (style.display === "none" || style.visibility === "hidden"));
              };
              const textForIds = (ids) => String(ids || "").split(/\\s+/)
                .map((id) => id && document.getElementById(id))
                .filter(Boolean)
                .map((node) => (node.textContent || "").trim())
                .filter(Boolean)
                .join(" ");
              const cleanQuestionText = (text) => {
                const lines = (text || "").split("\\n").map((line) => line.trim()).filter(Boolean);
                const keep = [];
                for (const line of lines) {
                  if (line === "✱" || line === "*" || /^select(\\.\\.\\.)?$/i.test(line) || /^(yes|no|upload|attach)$/i.test(line)) break;
                  keep.push(line);
                }
                return keep.join(" ");
              };
              const optionLabelFor = (control) => {
                if (control.id) {
                  const explicit = Array.from(document.querySelectorAll("label")).find((node) =>
                    node.htmlFor === control.id || node.getAttribute("for") === control.id
                  );
                  if (explicit && explicit.textContent) return explicit.textContent.trim();
                }
                const wrapping = control.closest("label");
                if (wrapping && wrapping.textContent) {
                  const clone = wrapping.cloneNode(true);
                  clone.querySelectorAll("select,input,textarea,button,[role='option'],[role='radio'],[role='checkbox']").forEach((node) => node.remove());
                  const text = clone.textContent.trim();
                  if (text) return text;
                }
                return control.getAttribute("aria-label") || control.getAttribute("data-value") ||
                  control.getAttribute("data-option-value") || control.value || (control.textContent || "").trim() || "";
              };
              const textWithoutControls = (node) => {
                if (!node) return "";
                const clone = typeof node.cloneNode === "function" ? node.cloneNode(true) : node;
                if (clone && typeof clone.querySelectorAll === "function") {
                  clone.querySelectorAll("input,textarea,select,button,[role='option'],[role='radio'],[role='checkbox']")
                    .forEach((child) => child.remove());
                }
                return cleanQuestionText(clone.innerText || clone.textContent || "");
              };
              const isPromptNode = (node) => {
                if (!node || !node.getAttribute) return false;
                const marker = [
                  node.tagName || "", node.id || "", node.className || "",
                  node.getAttribute("role") || "", node.getAttribute("data-testid") || "",
                  node.getAttribute("data-qa") || "", node.getAttribute("data-test") || "",
                  node.getAttribute("data-field-label") || "", node.getAttribute("data-question") || "",
                ].join(" ").toLowerCase();
                return /(^|\\s)(label|legend)(\\s|$)|question|prompt|field.?title|form.?title|heading/.test(marker) ||
                  /^H[1-6]$/.test(node.tagName || "") || node.getAttribute("role") === "heading";
              };
              const genericPromptLabel = (control) => {
                let node = control;
                for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
                  const parent = node.parentElement;
                  if (parent && parent.children) {
                    const siblings = Array.from(parent.children);
                    const index = siblings.indexOf(node);
                    for (const sibling of siblings.slice(0, index).reverse()) {
                      if (!isPromptNode(sibling)) continue;
                      const text = textWithoutControls(sibling);
                      if (text) return text;
                    }
                  }
                  const candidates = typeof node.querySelectorAll === "function"
                    ? Array.from(node.querySelectorAll(
                      "label,legend,h1,h2,h3,h4,h5,h6,[role='heading'],[data-field-label],[data-question],[data-question-label],[data-label],[data-testid],[data-qa],[data-test]"
                    )).filter((candidate) => candidate !== control && typeof candidate.contains === "function" && !candidate.contains(control) && isPromptNode(candidate))
                    : [];
                  for (const candidate of candidates) {
                    const text = textWithoutControls(candidate);
                    if (text) return text;
                  }
                }
                return "";
              };
              const workdayFieldLabel = (control) => {
                const field = control.closest('[data-automation-id^="formField-"]');
                if (!field) return "";
                const explicit = field.querySelector("label,[data-automation-id='formLabel']");
                const explicitText = explicit && explicit.textContent ? explicit.textContent.trim() : "";
                const isGenericCheckboxLabel = (
                  (control.getAttribute("type") || "").toLowerCase() === "checkbox" &&
                  /^(agree|accept|yes|i agree)$/i.test(explicitText)
                );
                let text = (explicitText && !isGenericCheckboxLabel) ? explicitText : (field.textContent || "");
                text = text.replace(/\\b\\d+\\s+items?\\s+selected\\b.*$/i, "");
                text = text.replace(/\\bExpanded\\b.*$/i, "");
                text = text.replace(/\\bError:.*$/i, "");
                return cleanQuestionText(text);
              };
              const workdayQuestionLabel = (control) => {
                const field = control.closest('[data-automation-id^="formField-"]');
                if (!field || !field.textContent) return "";
                const raw = field.textContent.trim();
                if (raw.includes("*")) return (raw.split("*")[0] + "*").trim();
                return workdayFieldLabel(control);
              };
              const labelFor = (control) => {
                const workdayLabel = workdayFieldLabel(control);
                if (workdayLabel) return workdayLabel;
                const byIds = textForIds(control.getAttribute("aria-labelledby"));
                if (byIds) return cleanQuestionText(byIds);
                if (control.id) {
                  const label = Array.from(document.querySelectorAll("label")).find((node) =>
                    node.htmlFor === control.id || node.getAttribute("for") === control.id
                  );
                  if (label && label.textContent) return cleanQuestionText(label.textContent.trim());
                }
                const wrapping = control.closest("label");
                if (wrapping && wrapping.textContent) {
                  const clone = wrapping.cloneNode(true);
                  clone.querySelectorAll("select,input,textarea,button,[role='option'],[role='radio'],[role='checkbox']").forEach((node) => node.remove());
                  const txt = clone.textContent.trim();
                  if (txt) return cleanQuestionText(txt);
                }
                const fieldset = control.closest("fieldset");
                const legend = fieldset && fieldset.querySelector("legend");
                if (legend && legend.textContent) return cleanQuestionText(legend.textContent);
                const genericPrompt = genericPromptLabel(control);
                if (genericPrompt) return genericPrompt;
                const describedBy = textForIds(control.getAttribute("aria-describedby"));
                return cleanQuestionText(
                  control.getAttribute("aria-label") || control.getAttribute("placeholder") || describedBy || control.name || control.id || "required field"
                );
              };
              const groupLabelFor = (control) => {
                const workdayQuestion = workdayQuestionLabel(control);
                if (workdayQuestion) return workdayQuestion;
                const fieldset = control.closest("fieldset");
                const legend = fieldset && fieldset.querySelector("legend");
                if (legend && legend.textContent) return cleanQuestionText(legend.textContent);
                const labelledBy = textForIds(control.getAttribute("aria-labelledby"));
                if (labelledBy) return cleanQuestionText(labelledBy);
                const genericPrompt = genericPromptLabel(control);
                if (genericPrompt) return genericPrompt;
                const root = control.closest('[role="radiogroup"], fieldset, [role="group"], [data-automation-id^="formField-"]');
                if (root) {
                  const rootText = textWithoutControls(root);
                  if (rootText) return rootText;
                }
                return labelFor(control);
              };
              const isPlaceholder = (value) => /^(select|select one|choose|please select|--.*--)?$/i.test(String(value || "").trim());
              const selectedPresentation = (control) => {
                const expanded = control.getAttribute("aria-expanded") === "true";
                // React Select and similar widgets deliberately keep their
                // editable combobox input empty after a choice is committed.
                // Read the rendered selected chip/value instead of mistaking
                // that implementation detail for a missing required answer.
                const root = control.closest('[class*="select__control"], [class*="select__value-container"]');
                if (root) {
                  const selected = Array.from(root.querySelectorAll(
                    '[class*="select__single-value"], [class*="select__multi-value__label"], [data-automation-id="selectedItem"], [aria-selected="true"]'
                  )).map((node) => String(node.textContent || "").trim()).filter(Boolean);
                  if (selected.length) return selected.join(" ");
                }
                const fieldRoot = control.closest('[data-automation-id^="formField-"], [role="group"], fieldset');
                if (fieldRoot) {
                  const selected = Array.from(fieldRoot.querySelectorAll(
                    '[data-automation-id="selectedItem"]'
                  )).map((node) => String(node.textContent || "").trim()).filter(Boolean);
                  if (selected.length) return selected.join(" ");
                  if (!expanded) {
                    const genericSelected = Array.from(fieldRoot.querySelectorAll(
                      '[aria-selected="true"], [aria-checked="true"], [data-state="selected"], [data-state="checked"], [data-state="on"]'
                    )).map((node) => String(node.textContent || "").trim()).filter(Boolean);
                    if (genericSelected.length) return genericSelected.join(" ");
                  }
                }
                const describedBy = textForIds(control.getAttribute("aria-describedby"));
                if (/^[1-9]\\d*\\s+item(?:s)?\\s+selected\\b/i.test(describedBy)) return describedBy;
                return "";
              };
              const labelAppearsRequired = (label) => /(?:\\*|✱)\\s*$/.test(String(label || "").trim());
              const controls = Array.from(new Set([
                ...document.querySelectorAll("input, textarea, select"),
                ...document.querySelectorAll('[contenteditable="true"], [contenteditable="plaintext-only"]'),
                ...document.querySelectorAll('[role="textbox"], [role="searchbox"]'),
                ...document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="menu"]'),
                ...document.querySelectorAll('[role="radio"], [role="checkbox"], [role="switch"]'),
              ])).filter((control) => visible(control) && !control.disabled);
              const findings = [];
              const seenRadioGroups = new Set();
              const seenCheckboxGroups = new Set();
              const choiceSelected = (candidate) => Boolean(
                candidate.checked ||
                candidate.getAttribute("aria-checked") === "true" ||
                candidate.getAttribute("aria-pressed") === "true" ||
                ["checked", "on", "selected"].includes(candidate.getAttribute("data-state"))
              );
              for (const control of controls) {
              const ashbyRequired = () => {
                const entry = control.closest && control.closest(
                  '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
                );
                const label = entry && entry.querySelector(
                  'label,.ashby-application-form-question-title'
                );
                return Boolean(label && /(^|\\s)[^\\s]*required[^\\s]*(\\s|$)/i.test(String(label.className || "")));
              };
              const required = control.required || control.getAttribute("aria-required") === "true" ||
                  Boolean(control.closest('[aria-required="true"]')) || ashbyRequired() ||
                  labelAppearsRequired(labelFor(control));
                if (!required) continue;
                const type = (control.getAttribute("type") || control.tagName || "").toLowerCase();
                const role = control.getAttribute("role") || "";
                let invalid = control.getAttribute("aria-invalid") === "true";
                let empty = false;
                let groupSatisfied = false;
                let committedSelection = "";
                if (type === "radio" || role === "radio") {
                  const root = control.closest('[role="radiogroup"], fieldset, [role="group"]');
                  const group = root || control.name || control.getAttribute("aria-labelledby") || control.id;
                  if (!group || seenRadioGroups.has(group)) continue;
                  seenRadioGroups.add(group);
                  empty = !controls.some((candidate) => {
                    const candidateRole = candidate.getAttribute("role") || "";
                    const candidateRoot = candidate.closest && candidate.closest('[role="radiogroup"], fieldset, [role="group"]');
                    const sameGroup = root
                      ? candidateRoot === root
                      : (candidate.name || candidate.getAttribute("aria-labelledby") || candidate.id) === group;
                    return candidate !== control && sameGroup &&
                      ((candidate.getAttribute("type") || "").toLowerCase() === "radio" || candidateRole === "radio") &&
                      (candidate.checked || candidate.getAttribute("aria-checked") === "true");
                  }) && !(control.checked || control.getAttribute("aria-checked") === "true");
                } else if (type === "checkbox" || role === "checkbox" || role === "switch") {
                  const checkboxPeers = control.name
                    ? controls.filter((candidate) => {
                        const candidateType = (candidate.getAttribute("type") || candidate.tagName || "").toLowerCase();
                        const candidateRole = candidate.getAttribute("role") || "";
                        return candidate.name === control.name &&
                          (candidateType === "checkbox" || candidateRole === "checkbox" || candidateRole === "switch");
                      })
                    : [];
                  if (checkboxPeers.length > 1) {
                    if (seenCheckboxGroups.has(control.name)) continue;
                    seenCheckboxGroups.add(control.name);
                    empty = !checkboxPeers.some(choiceSelected);
                    groupSatisfied = !empty;
                    if (groupSatisfied) invalid = false;
                  } else {
                    empty = !choiceSelected(control);
                  }
                } else if (type === "file") {
                  empty = !(control.files && control.files.length);
                } else if (control.tagName.toLowerCase() === "select") {
                  const selected = control.options && control.selectedIndex >= 0 ? control.options[control.selectedIndex] : null;
                  empty = !String(control.value || "").trim() || Boolean(selected && (selected.disabled || isPlaceholder(selected.textContent)));
                } else {
                  const committed = selectedPresentation(control);
                  committedSelection = committed;
                  if (role === "combobox" || control.getAttribute("aria-haspopup") || committed) {
                    empty = !committed && isPlaceholder(
                      control.value ||
                      control.getAttribute("aria-valuetext") ||
                      control.getAttribute("data-value") ||
                      control.textContent
                    );
                    // Some React Select wrappers retain aria-invalid until the
                    // next form-level validation even after an onChange has
                    // committed a visible selection. Submission remains the
                    // final validation authority, so do not gate it on stale
                    // wrapper state when the selected value is present.
                    if (committed) invalid = false;
                  } else if (control.isContentEditable || role === "textbox" || role === "searchbox") {
                    empty = !String(control.getAttribute("aria-valuetext") || control.textContent || "").trim();
                  } else {
                    empty = !String(control.value || "").trim();
                  }
                }
                const nativeInvalid = groupSatisfied || committedSelection
                  ? false
                  : (typeof control.checkValidity === "function" ? !control.checkValidity() : false);
                if (!empty && !nativeInvalid) invalid = false;
                else invalid = invalid || nativeInvalid;
                if (empty || invalid) {
                  const auditLabel = (
                    type === "radio" || role === "radio" ||
                    type === "checkbox" || role === "checkbox" || role === "switch"
                  ) ? groupLabelFor(control) : labelFor(control);
                  findings.push({
                    label: String(auditLabel).replace(/\\s+/g, " ").trim(),
                    reason: invalid ? "browser reports field as invalid" : "required field remains empty after fill",
                  });
                }
              }
              return findings.slice(0, 30);
            }"""
        )
    except Exception:
        return []
    return [item for item in (findings or []) if isinstance(item, dict) and item.get("label")]


def _form_field_signature(fields: list[dict[str, Any]]) -> tuple[str, ...]:
    """Return a stable, value-free signature for detecting same-title form subpages."""
    return tuple(
        sorted(
            "|".join(
                str(field.get(key) or "")
                for key in ("kind", "tag", "type", "id", "name", "label", "required")
            )
            for field in fields
        )
    )


def _repair_invalid_text_field_by_label(page, label: str, profile: dict[str, Any]) -> bool:
    value = (
        _auto_answer(label, profile, sensitive=False)
        if _is_source_question(label)
        else _map_text_value(label, profile)
    )
    if not value:
        return False
    try:
        return bool(
            page.evaluate(
                """({ label, value }) => {
                  const norm = (text) => String(text || "")
                    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
                    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
                    .toLowerCase()
                    .replace(/[^a-z0-9\\s]/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                  const visible = (node) => {
                    if (!node || node.disabled || node.getAttribute("aria-hidden") === "true") return false;
                    if (node.offsetParent) return true;
                    const rects = node.getClientRects ? node.getClientRects() : [];
                    const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
                    return Boolean(rects.length) && !(style && (style.display === "none" || style.visibility === "hidden"));
                  };
                  const textForIds = (ids) => String(ids || "").split(/\\s+/)
                    .map((id) => id && document.getElementById(id))
                    .filter(Boolean)
                    .map((node) => (node.textContent || "").trim())
                    .filter(Boolean)
                    .join(" ");
                  const clean = (text) => String(text || "")
                    .replace(/[✱*]/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                  const labelFor = (control) => {
                    const ashbyEntry = control.closest && control.closest(
                      '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
                    );
                    if (ashbyEntry) {
                      const ashbyLabel = ashbyEntry.querySelector(
                        'label,.ashby-application-form-question-title,[data-field-label]'
                      );
                      if (ashbyLabel && ashbyLabel.textContent) return clean(ashbyLabel.textContent);
                    }
                    const byIds = textForIds(control.getAttribute("aria-labelledby"));
                    if (byIds) return clean(byIds);
                    if (control.id) {
                      const explicit = Array.from(document.querySelectorAll("label")).find((node) =>
                        node.htmlFor === control.id || node.getAttribute("for") === control.id
                      );
                      if (explicit && explicit.textContent) return clean(explicit.textContent);
                    }
                    const wrapping = control.closest && control.closest("label");
                    if (wrapping && wrapping.textContent) return clean(wrapping.textContent);
                    const describedBy = textForIds(control.getAttribute("aria-describedby"));
                    return clean(
                      control.getAttribute("aria-label") ||
                      control.getAttribute("placeholder") ||
                      describedBy ||
                      control.name ||
                      control.id
                    );
                  };
                  const wanted = norm(label);
                  if (!wanted) return false;
                  const controls = Array.from(document.querySelectorAll(
                    'input,textarea,[role="textbox"],[role="searchbox"],[contenteditable="true"],[contenteditable="plaintext-only"]'
                  )).filter((control) => {
                    const type = String(control.getAttribute("type") || control.tagName || "").toLowerCase();
                    return visible(control) && !["hidden", "file", "checkbox", "radio", "submit", "button"].includes(type);
                  });
                  const ranked = controls
                    .map((control) => {
                      const actual = norm(labelFor(control));
                      const exact = actual === wanted;
                      const close = exact || actual.includes(wanted) || wanted.includes(actual);
                      const invalid = control.getAttribute("aria-invalid") === "true" ||
                        (typeof control.checkValidity === "function" && !control.checkValidity());
                      const empty = !String(control.value || control.textContent || "").trim();
                      return {
                        control,
                        close,
                        score: (exact ? 8 : 0) + (close ? 4 : 0) + (invalid ? 2 : 0) + (empty ? 1 : 0),
                      };
                    })
                    .filter((item) => item.close && item.score > 0)
                    .sort((left, right) => right.score - left.score);
                  const target = ranked.length ? ranked[0].control : null;
                  if (!target) return false;
                  try { target.focus(); } catch (e) {}
                  if (target.isContentEditable) {
                    target.textContent = value;
                  } else {
                    const proto = target.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                    if (descriptor && descriptor.set) descriptor.set.call(target, value);
                    else target.value = value;
                  }
                  try {
                    target.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value }));
                  } catch (e) {
                    target.dispatchEvent(new Event("input", { bubbles: true }));
                  }
                  target.dispatchEvent(new Event("change", { bubbles: true }));
                  target.dispatchEvent(new Event("blur", { bubbles: true }));
                  return true;
                }""",
                {"label": label, "value": value},
            )
        )
    except Exception:
        return False


def _repair_invalid_source_combobox_by_label(page, label: str, profile: dict[str, Any]) -> bool:
    if not _is_source_question(label):
        return False
    answer = (
        _auto_answer(label, profile, sensitive=False)
        or _find_answer(label, profile.get("answers") or {})
        or (profile.get("answers") or {}).get("How did you hear about us?")
        or "LinkedIn"
    )
    wants = _answer_aliases(answer)
    try:
        return bool(
            page.evaluate(
                """({ label, answer, wants }) => {
                  const norm = (text) => String(text || "")
                    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
                    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
                    .toLowerCase()
                    .replace(/[^a-z0-9\\s]/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                  const visible = (node) => {
                    if (!node || node.disabled || node.getAttribute("aria-hidden") === "true") return false;
                    if (node.offsetParent) return true;
                    const rects = node.getClientRects ? node.getClientRects() : [];
                    const style = window.getComputedStyle ? window.getComputedStyle(node) : null;
                    return Boolean(rects.length) && !(style && (style.display === "none" || style.visibility === "hidden"));
                  };
                  const textForIds = (ids) => String(ids || "").split(/\\s+/)
                    .map((id) => id && document.getElementById(id))
                    .filter(Boolean)
                    .map((node) => (node.textContent || "").trim())
                    .filter(Boolean)
                    .join(" ");
                  const clean = (text) => String(text || "")
                    .replace(/[✱*]/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim();
                  const labelFor = (control) => {
                    const byIds = textForIds(control.getAttribute("aria-labelledby"));
                    if (byIds) return clean(byIds);
                    if (control.id) {
                      const explicit = Array.from(document.querySelectorAll("label")).find((node) =>
                        node.htmlFor === control.id || node.getAttribute("for") === control.id
                      );
                      if (explicit && explicit.textContent) return clean(explicit.textContent);
                    }
                    const fieldRoot = control.closest && control.closest('[role="group"], fieldset, [class*="field"], [class*="question"]');
                    if (fieldRoot) {
                      const labelNode = fieldRoot.querySelector('label,legend,[id*="label"],[class*="label"],[class*="question"]');
                      if (labelNode && labelNode.textContent) return clean(labelNode.textContent);
                    }
                    const describedBy = textForIds(control.getAttribute("aria-describedby"));
                    return clean(
                      control.getAttribute("aria-label") ||
                      control.getAttribute("placeholder") ||
                      describedBy ||
                      control.name ||
                      control.id
                    );
                  };
                  const wantedLabel = norm(label);
                  const candidates = Array.from(document.querySelectorAll(
                    'input,[role="combobox"],[aria-haspopup="listbox"],[aria-haspopup="menu"]'
                  )).filter((control) => {
                    const type = String(control.getAttribute("type") || control.tagName || "").toLowerCase();
                    if (["hidden", "file", "checkbox", "radio", "submit", "button"].includes(type)) return false;
                    if (!visible(control)) return false;
                    const actual = norm(labelFor(control));
                    return actual && (actual === wantedLabel || actual.includes(wantedLabel) || wantedLabel.includes(actual));
                  });
                  if (!candidates.length) return false;
                  const target = candidates.sort((left, right) => {
                    const score = (node) => {
                      const invalid = node.getAttribute("aria-invalid") === "true" ? 2 : 0;
                      const empty = !String(node.value || node.textContent || "").trim() ? 1 : 0;
                      const expanded = node.getAttribute("aria-expanded") === "true" ? 1 : 0;
                      return invalid + empty + expanded;
                    };
                    return score(right) - score(left);
                  })[0];
                  try { target.focus(); } catch (e) {}
                  try { target.click(); } catch (e) {}
                  if ("value" in target) {
                    const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                    if (descriptor && descriptor.set) descriptor.set.call(target, String(answer || ""));
                    else target.value = String(answer || "");
                    try {
                      target.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: String(answer || "") }));
                    } catch (e) {
                      target.dispatchEvent(new Event("input", { bubbles: true }));
                    }
                  }
                  const normalizedWants = Array.from(new Set((wants || []).map(norm).filter(Boolean)));
                  const optionNodes = Array.from(document.querySelectorAll(
                    '[role="option"],[role="menuitem"],[data-automation-id="menuItem"],li,[class*="option"]'
                  )).filter(visible);
                  const optionText = (node) => clean(node.textContent || node.getAttribute("aria-label") || "");
                  let chosen = optionNodes.find((node) => normalizedWants.includes(norm(optionText(node))));
                  if (!chosen) {
                    chosen = optionNodes.find((node) => normalizedWants.some((want) => {
                      const text = norm(optionText(node));
                      return want && text && (text.includes(want) || want.includes(text));
                    }));
                  }
                  if (!chosen) {
                    const useful = optionNodes.filter((node) => {
                      const text = norm(optionText(node));
                      return text && text !== "select one" && !/^\\+?\\d+$/.test(text);
                    });
                    if (useful.length === 1) chosen = useful[0];
                  }
                  if (!chosen) return false;
                  try { chosen.focus(); } catch (e) {}
                  chosen.click();
                  target.dispatchEvent(new Event("change", { bubbles: true }));
                  target.dispatchEvent(new Event("blur", { bubbles: true }));
                  return true;
                }""",
                {"label": label, "answer": str(answer), "wants": wants},
            )
        )
    except Exception:
        return False


def _css_attr_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\a ")


def _attr_selector(attr: str, value: str) -> str:
    return f'[{attr}="{_css_attr_value(value)}"]'


def _selector_for(field: dict[str, Any]) -> str | None:
    # Third-party forms can repeat both ids and names. The scrape marker is
    # unique for the current DOM snapshot.
    if field.get("autofillId"):
        return _attr_selector("data-job-agent-autofill-index", field["autofillId"])
    if field.get("id"):
        return _attr_selector("id", field["id"])
    if field.get("name"):
        return _attr_selector("name", field["name"])
    return None


def _recover_text_fill_locator(page, field: dict[str, Any], locator):
    """Recover when a stale runtime marker points at a non-fillable option."""
    try:
        target = locator.evaluate(
            """(el) => {
              const tag = String((el && el.tagName) || "").toLowerCase();
              const type = String((el && el.getAttribute && el.getAttribute("type")) || "").toLowerCase();
              const role = String((el && el.getAttribute && el.getAttribute("role")) || "").toLowerCase();
              return { tag, type, role, editable: Boolean(el && el.isContentEditable) };
            }"""
        )
    except Exception:
        return locator
    if not isinstance(target, dict):
        return locator
    if target.get("editable") or target.get("tag") == "textarea":
        return locator
    if target.get("tag") == "input" and target.get("type") not in {
        "radio",
        "checkbox",
        "file",
        "submit",
        "button",
        "hidden",
        "image",
    }:
        return locator
    label = str(field.get("label") or "").strip()
    if not label:
        return locator
    try:
        marker = page.evaluate(
            """(payload) => {
              const norm = (value) => String(value || "")
                .toLowerCase()
                .replace(/[^a-z0-9\\s]/g, " ")
                .replace(/\\s+/g, " ")
                .trim();
              const text = (node) => String((node && (node.innerText || node.textContent)) || "")
                .replace(/\\s+/g, " ")
                .trim();
              const labelTextFor = (control) => {
                const parts = [
                  control.getAttribute("aria-label"),
                  control.getAttribute("placeholder"),
                  control.getAttribute("name"),
                  control.id,
                ];
                if (control.id) {
                  const explicit = Array.from(document.querySelectorAll("label")).find((label) =>
                    label.htmlFor === control.id || label.getAttribute("for") === control.id
                  );
                  if (explicit) parts.push(text(explicit));
                }
                const entry = control.closest && control.closest(
                  ".ashby-application-form-field-entry,[data-field-entry-id],[data-field-path],fieldset,[role='group']"
                );
                if (entry) parts.push(text(entry));
                return parts.filter(Boolean).join(" ");
              };
              const wanted = norm(payload.label);
              if (!wanted) return null;
              const wantedTokens = wanted.split(" ").filter((token) =>
                token.length >= 4 && !["most", "recent", "progress", "degree"].includes(token)
              );
              const controls = Array.from(document.querySelectorAll(
                "input:not([type='hidden']), textarea, [contenteditable='true'], [contenteditable='plaintext-only']"
              )).filter((control) => {
                const tag = String(control.tagName || "").toLowerCase();
                const type = String(control.getAttribute("type") || tag).toLowerCase();
                return !["radio", "checkbox", "file", "submit", "button", "image"].includes(type);
              });
              let best = null;
              let bestScore = 0;
              for (const control of controls) {
                const haystack = norm(labelTextFor(control));
                if (!haystack) continue;
                let score = 0;
                if (haystack.includes(wanted)) score = 100;
                const common = wantedTokens.filter((token) => haystack.includes(token)).length;
                score = Math.max(score, common * 20);
                if (
                  wanted.includes("graduation") &&
                  (haystack.includes("graduation") || haystack.includes("anticipated graduation"))
                ) score = Math.max(score, 90);
                if (wanted.includes("date") && norm(control.getAttribute("placeholder")).includes("date")) {
                  score = Math.max(score, 60 + common * 10);
                }
                if (score > bestScore) {
                  best = control;
                  bestScore = score;
                }
              }
              if (!best || bestScore < 50) return null;
              const marker = "job-agent-fill-recovered-" + Date.now() + "-" + Math.floor(Math.random() * 1000000);
              best.setAttribute("data-job-agent-fill-target", marker);
              return marker;
            }""",
            {"label": label},
        )
    except Exception:
        marker = None
    if marker:
        return page.locator(_attr_selector("data-job-agent-fill-target", str(marker))).first
    return locator


def _normalize_date_input_value(
    value: Any,
    placeholder: str = "",
    *,
    input_type: str = "",
    today: date | None = None,
) -> str:
    """Convert approved relative availability into a date-picker-safe value."""
    raw = str(value or "").strip()
    normalized_placeholder = _norm(placeholder)
    native_date = _norm(input_type) == "date"
    date_like_placeholder = "date" in normalized_placeholder or bool(
        re.search(r"\b(?:mm|dd|yyyy)\b", normalized_placeholder)
    )
    if not native_date and not date_like_placeholder:
        return raw
    target = _date_target(value, today=today)
    if not target:
        return raw
    # Playwright only accepts the ISO wire format for native date inputs.
    # Text inputs and framework date pickers usually display the US format.
    return target.isoformat() if native_date else target.strftime("%m/%d/%Y")


def _normalize_number_input_value(value: Any, field: dict[str, Any], *, input_type: str = "") -> str:
    """Convert approved text answers into values accepted by native number inputs."""
    raw = str(value or "").strip()
    if _norm(input_type) != "number":
        return raw
    label = _norm(
        " ".join(
            str(field.get(key) or "")
            for key in ("label", "id", "name", "placeholder", "ariaLabel")
        )
    )
    if "percentage" in label or "percent" in label or "%" in raw:
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", raw) or re.search(r"\b(\d+(?:\.\d+)?)\b", raw)
        if match:
            return match.group(1)
    if any(token in label for token in ("salary", "compensation", "pay", "hourly", "wage")):
        match = re.search(r"(?:\$|usd\s*)?\s*(\d+(?:\.\d+)?)\s*k\b", raw, flags=re.I)
        if match:
            amount = float(match.group(1)) * 1000
            return str(int(amount)) if amount.is_integer() else str(amount)
        match = re.search(r"\b(\d+(?:\.\d+)?)\b", raw)
        if match:
            return match.group(1)
    if "year" in label and "experience" in label:
        match = re.search(r"\b(\d+(?:\.\d+)?)\b", raw)
        if match:
            return match.group(1)
    return raw


def _date_target(value: Any, *, today: date | None = None) -> date | None:
    """Resolve a profile date or relative availability into a concrete date.

    ATS date widgets use a mix of native ISO inputs, masked text inputs and
    calendar-only controls.  Keeping the parsing independent from a widget
    lets every fill path use the same target date and avoids provider-specific
    date heuristics.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = _norm(raw)
    base = today or date.today()
    if normalized in {"within a month", "in one month", "one month"}:
        year = base.year + (1 if base.month == 12 else 0)
        month = 1 if base.month == 12 else base.month + 1
        return date(year, month, min(base.day, calendar.monthrange(year, month)[1]))
    if normalized in {"within two weeks", "in two weeks", "two weeks"}:
        return base + timedelta(days=14)
    if normalized in {"immediately", "as soon as possible", "asap", "now"}:
        return base

    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%d/%m/%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    # Some education and availability controls only expose month/year.  Pick
    # the first day rather than inventing a day from the current date.
    for fmt in ("%Y-%m", "%Y/%m", "%m/%Y", "%B %Y", "%b %Y"):
        try:
            return datetime.strptime(raw, fmt).date().replace(day=1)
        except ValueError:
            continue
    return None


def _is_date_like_field(field: dict[str, Any], locator) -> bool:
    """Identify calendar-backed inputs without relying on a particular ATS."""
    if _norm(field.get("type")) == "date":
        return True
    context = " ".join(
        str(field.get(key) or "")
        for key in ("label", "id", "name", "placeholder", "ariaLabel", "ariaDescription")
    )
    try:
        context = " ".join(
            [
                context,
                str(locator.get_attribute("placeholder") or ""),
                str(locator.get_attribute("autocomplete") or ""),
                str(locator.get_attribute("inputmode") or ""),
            ]
        )
    except Exception:
        pass
    normalized = _norm(context)
    return (
        "date" in normalized
        or bool(re.search(r"\b(?:mm|dd|yyyy)[/ .-]*(?:mm|dd|yyyy)\b", normalized))
    )


def _control_readback(locator, field: dict[str, Any]) -> str:
    """Read native, ARIA and rich-text controls through their public state."""
    try:
        if field.get("contentEditable") or _norm(field.get("type")) == "contenteditable":
            return str(locator.text_content() or "").strip()
        return str(locator.input_value() or "").strip()
    except Exception:
        pass
    for attribute in ("aria-valuetext", "data-value", "data-selected-value", "value"):
        try:
            value = locator.get_attribute(attribute)
        except Exception:
            value = None
        if value:
            return str(value).strip()
    try:
        return str(locator.text_content() or "").strip()
    except Exception:
        return ""


def _readback_matches_date(value: Any, target: date) -> bool:
    parsed = _date_target(value)
    return parsed == target


def _option_matches(option: Any, answer: Any) -> bool:
    option_text = _norm(option)
    wants = [_norm(alias) for alias in _answer_aliases(answer) if _norm(alias)]
    if not option_text or not wants:
        return False
    if _norm(answer) == "no" and "veteran" in option_text:
        return _is_negative_veteran_option(option_text)
    for want in wants:
        expanded_option = _expanded_location_text(option_text)
        expanded_want = _expanded_location_text(want)
        if expanded_option == expanded_want:
            return True
        if want in {"asian", "east asian", "asian not hispanic or latino"}:
            if option_text == want or option_text.startswith(f"{want} "):
                return True
            continue
        if want in {"man", "woman", "male", "female"}:
            # Controlled gender matching: only match when the option text
            # is a recognized gender label (not "manager", "policewoman", etc.).
            if want == "man" or want == "male":
                if option_text in {"man", "male", "cisgender man", "cis man", "cisgender male", "cis male"}:
                    return True
            if want == "woman" or want == "female":
                if option_text in {"woman", "female", "cisgender woman", "cis woman", "cisgender female", "cis female"}:
                    return True
            continue
        # "No" answer to employment history questions should match
        # "I have never worked at X" / "I have never worked for X" options.
        if want == "no" and (
            "never worked" in option_text
            or "have not worked" in option_text
            or "have not been employed" in option_text
            or "do not currently work" in option_text
        ):
            return True
        if option_text == want:
            return True
        if len(want) >= 3 and len(option_text) >= 3 and want in option_text:
            return True
        # Never infer a generic "Other"/placeholder choice from a longer
        # negative answer such as "Not open to other locations".
        if option_text in {"other", "no answer", "select", "select one"}:
            continue
        if len(want) >= 3 and len(option_text) >= 3 and option_text in want:
            if f"not {option_text}" in want:
                continue
            return True
    return False


def _locator_is_ashby_yes_no_option(locator) -> bool:
    try:
        return bool(
            locator.evaluate(
                """(node) => Boolean(
                  node &&
                  node.closest &&
                  node.closest('.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]') &&
                  String(node.className || '').includes('_option_')
                )"""
            )
        )
    except Exception:
        return False


def _click_ashby_button_group(page, field: dict[str, Any], option: dict[str, Any]) -> str | None | bool:
    label = str(field.get("label") or "").strip()
    option_text = str(option.get("label") or option.get("value") or "").strip()
    if not label or not option_text:
        return None
    try:
        marker = page.evaluate(
            """(payload) => {
              const norm = (value) => String(value || "")
                .toLowerCase()
                .replace(/[^a-z0-9\\s]/g, " ")
                .replace(/\\s+/g, " ")
                .trim();
              const labelNorm = norm(payload.label);
              const optionNorm = norm(payload.optionText);
              if (!labelNorm || !optionNorm) return null;
              const entries = Array.from(document.querySelectorAll(
                '.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]'
              ));
              const entry = entries.find((candidate) => {
                const labelNode = candidate.querySelector('label,.ashby-application-form-question-title');
                const text = norm(labelNode && labelNode.textContent ? labelNode.textContent : candidate.textContent);
                return text === labelNorm || text.includes(labelNorm) || labelNorm.includes(text);
              });
              let button = entry ? Array.from(entry.querySelectorAll('button')).find((candidate) =>
                norm(candidate.textContent || candidate.value) === optionNorm
              ) : null;
              if (!button) {
                const exactButtons = Array.from(document.querySelectorAll('button')).filter((candidate) =>
                  norm(candidate.textContent || candidate.value) === optionNorm
                );
                button = exactButtons.find((candidate) => {
                  const container = candidate.closest('.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path],fieldset,section,div');
                  const text = norm(container && container.textContent);
                  return text.includes(labelNorm);
                }) || (exactButtons.length === 1 ? exactButtons[0] : null);
              }
              let target = button;
              if (!target) {
                const root = entry || document;
                const textFor = (node) => {
                  if (!node) return '';
                  if (node.matches && node.matches('input[type="radio"],input[type="checkbox"]')) {
                    const explicit = node.id ? document.querySelector(`label[for="${CSS.escape(node.id)}"]`) : null;
                    const wrapping = node.closest('label');
                    const parent = node.parentElement;
                    return norm(node.getAttribute('aria-label') || node.value || (explicit && explicit.textContent) || (wrapping && wrapping.textContent) || (parent && parent.textContent));
                  }
                  return norm(node.getAttribute && node.getAttribute('aria-label') || node.textContent || node.value);
                };
                const candidates = Array.from(root.querySelectorAll('label,[role="radio"],[role="button"],input[type="radio"],input[type="checkbox"]'));
                target = candidates.find((candidate) => textFor(candidate) === optionNorm)
                  || candidates.find((candidate) => textFor(candidate).includes(optionNorm));
              }
              if (!target) return null;
              const marker = `ashby-${Date.now()}-${Math.random().toString(36).slice(2)}`;
              target.setAttribute('data-job-agent-ashby-click-target', marker);
              return marker;
            }""",
            {"label": label, "optionText": option_text},
        )
        if not marker:
            return None
        locator = page.locator(_attr_selector("data-job-agent-ashby-click-target", str(marker))).first
        locator.scroll_into_view_if_needed(timeout=3000)
        option_norm = _norm(option_text)
        desired_yes = option_norm == "yes"
        yes_no_option = option_norm in {"yes", "no"}
        state = page.evaluate(
            """(marker) => {
              const target = document.querySelector(`[data-job-agent-ashby-click-target="${marker}"]`);
              if (!target) return null;
              const entry = target.closest('.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]');
              const input = target.matches && target.matches('input[type="radio"],input[type="checkbox"]')
                ? target
                : (target.querySelector && target.querySelector('input[type="radio"],input[type="checkbox"]')) ||
                  (entry && entry.querySelector('input[type="radio"],input[type="checkbox"]'));
              const style = window.getComputedStyle(target);
              const active = String(target.className || '').includes('_active_') ||
                target.getAttribute('aria-pressed') === 'true' ||
                target.getAttribute('aria-checked') === 'true' ||
                target.getAttribute('aria-selected') === 'true' ||
                ['checked', 'on', 'selected'].includes(target.getAttribute('data-state')) ||
                (style.color === 'rgb(255, 255, 255)' && style.backgroundColor !== 'rgba(0, 0, 0, 0)');
              return { active, checked: input ? Boolean(input.checked) : null };
            }""",
            str(marker),
        )
        if isinstance(state, dict) and (
            state.get("active")
            or (yes_no_option and state.get("checked") == desired_yes)
            or (not yes_no_option and state.get("checked") is True)
        ):
            return f"selected: {option_text}"
        locator.click(timeout=3000)
        page.wait_for_timeout(200)
        state = page.evaluate(
            """(marker) => {
              const target = document.querySelector(`[data-job-agent-ashby-click-target="${marker}"]`);
              if (!target) return null;
              const entry = target.closest('.ashby-application-form-field-entry,[data-field-entry-id],[data-field-path]');
              const input = target.matches && target.matches('input[type="radio"],input[type="checkbox"]')
                ? target
                : (target.querySelector && target.querySelector('input[type="radio"],input[type="checkbox"]')) ||
                  (entry && entry.querySelector('input[type="radio"],input[type="checkbox"]'));
              const style = window.getComputedStyle(target);
              const active = String(target.className || '').includes('_active_') ||
                target.getAttribute('aria-pressed') === 'true' ||
                target.getAttribute('aria-checked') === 'true' ||
                target.getAttribute('aria-selected') === 'true' ||
                ['checked', 'on', 'selected'].includes(target.getAttribute('data-state')) ||
                (style.color === 'rgb(255, 255, 255)' && style.backgroundColor !== 'rgba(0, 0, 0, 0)');
              return { active, checked: input ? Boolean(input.checked) : null };
            }""",
            str(marker),
        )
    except Exception:
        return None
    if isinstance(state, dict) and (
        state.get("active")
        or (yes_no_option and state.get("checked") == desired_yes)
        or (not yes_no_option and state.get("checked") is True)
    ):
        return f"selected: {option_text}"
    return False


def _desired_location_values(profile: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for raw in profile.get("desired_locations") or []:
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    answers = profile.get("answers") or {}
    for key in ("Where would you like to work?", "Preferred location", "Desired location"):
        raw = answers.get(key)
        if isinstance(raw, str) and raw.strip():
            values.extend(part.strip() for part in re.split(r"[,;/|]", raw) if part.strip())
    return values


def _locations_compatible(option: str, desired: str) -> bool:
    option_text = _expanded_location_text(option)
    desired_text = _expanded_location_text(desired)
    if not option_text or not desired_text:
        return False
    aliases = {
        "new york city": "new york",
        "nyc": "new york",
    }
    option_text = aliases.get(option_text, option_text)
    desired_text = aliases.get(desired_text, desired_text)
    return option_text == desired_text or option_text in desired_text or desired_text in option_text


def _looks_like_location_checkbox_option(label: str) -> bool:
    normalized = _norm(label)
    if not normalized:
        return False
    if "remote" in normalized and ("us" in normalized or "usa" in normalized or "united states" in normalized):
        return True
    tokens = normalized.split()
    if len(tokens) > 8:
        return False
    if any(token in _US_STATE_CODES for token in tokens):
        return True
    return any(state in normalized for state in _US_STATE_NAMES.values())


def _office_location_checkbox_plan(field: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any] | None:
    label = str(field.get("label") or "")
    combined = _norm(
        " ".join(
            str(field.get(key) or "")
            for key in ["label", "section", "ariaLabel", "ariaDescription", "name", "id"]
        )
    )
    if not (
        "which office location" in combined
        or "office locations" in combined
        or "location s are you interested" in combined
        or _looks_like_location_checkbox_option(label)
    ):
        return None
    option = _norm(label)
    if not option:
        return None
    if "remote" in option and ("us" in option or "united states" in option or "usa" in option):
        return {"action": "check"}
    if any(_locations_compatible(label, desired) for desired in _desired_location_values(profile)):
        return {"action": "check"}
    return {
        "action": "skip",
        "reason": "office location option not selected from candidate preferences",
        "blocking": False,
    }


def _preferred_office_location_option(
    field: dict[str, Any],
    profile: dict[str, Any],
) -> Any | None:
    label = _norm(field.get("label") or "")
    if not (
        ("which office location" in label or "preferred office location" in label)
        and ("prefer" in label or "would you" in label)
    ):
        return None
    options = field.get("options") or []
    if not options:
        return None
    answers = profile.get("answers") or {}
    preference_text = " ".join(
        str(value or "")
        for value in [
            profile.get("target_location"),
            " ".join(str(item) for item in profile.get("desired_locations") or []),
            answers.get("Where would you like to work?"),
            answers.get(
                "Please indicate all of the locations that you would be interested in relocating to for this position."
            ),
            profile.get("location"),
        ]
    )
    preference = _norm(preference_text)
    if any(token in preference for token in ["new york", "nyc", "jersey city", "new jersey"]):
        office_keywords = ["new york", "ny"]
    elif any(token in preference for token in ["san francisco", "bay area", "sf"]):
        office_keywords = ["san francisco", "sf"]
    else:
        office_keywords = ["new york"]
    for option in options:
        option_label = _norm(_option_text(option))
        if any(keyword in option_label for keyword in office_keywords):
            return option
    return None


def _preferred_office_location_answer(
    field: dict[str, Any],
    profile: dict[str, Any],
) -> str | None:
    label = _norm(field.get("label") or "")
    if not (
        ("which office location" in label or "preferred office location" in label)
        and ("prefer" in label or "would you" in label)
    ):
        return None
    answers = profile.get("answers") or {}
    primary_preference_text = " ".join(
        str(value or "")
        for value in [
            " ".join(str(item) for item in profile.get("desired_locations") or []),
            answers.get("Where would you like to work?"),
            answers.get(
                "Please indicate all of the locations that you would be interested in relocating to for this position."
            ),
            profile.get("location"),
        ]
    )
    preference = _norm(primary_preference_text)
    if any(token in preference for token in ["new york", "nyc", "jersey city", "new jersey"]):
        return "New York"
    if any(token in preference for token in ["san francisco", "bay area", "sf"]):
        return "San Francisco"
    preference = _norm(profile.get("target_location"))
    if "new york" in preference:
        return "New York"
    if "san francisco" in preference:
        return "San Francisco"
    if any(token in preference for token in ["new york", "nyc", "jersey city", "new jersey"]):
        return "New York"
    return "New York"


def _office_location_combobox_fallback_choice(
    field: dict[str, Any],
    available: list[str],
    answer: str,
) -> str | None:
    if not available:
        return None
    label = _norm(field.get("label") or "")
    if "office location" not in label:
        return None
    preferred_norm = _norm(answer)
    if not preferred_norm:
        return None
    for item in available:
        if _norm(item) == preferred_norm:
            return item
    if "new york" in preferred_norm and any("new york" in _norm(item) for item in available):
        return "New York"
    if "san francisco" in preferred_norm and any("san francisco" in _norm(item) for item in available):
        return "San Francisco"
    return None


def _select_intl_tel_input_country(page, locator, answer: str) -> str | None:
    """Set the country on an intl-tel-input widget when the combobox is its
    country selector. Greenhouse and other sites render the phone country code
    as an intl-tel-input dropdown whose options are `<li class="iti__country">`
    items with a `.iti__country-name` child. The widget also exposes an
    instance on the tel input via `.iti`, so prefer the API and fall back to
    clicking the matching country item."""
    if not answer:
        return None
    country_name = str(answer).strip()
    try:
        in_it_container = bool(locator.evaluate(
            "el => !!(el && el.closest && el.closest('.iti'))"
        ))
    except Exception:
        in_it_container = False
    if not in_it_container:
        return None
    try:
        return page.evaluate(
            """(args) => {
              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
              const countryName = args.countryName;
              const container = document.querySelector('.iti');
              if (!container) return null;
              const input = container.querySelector('input[type="tel"], input.iti__tel-input');
              if (input && input.iti && typeof input.iti.setCountry === 'function') {
                // Find ISO code by country name from dropdown items.
                let iso = null;
                const items = container.querySelectorAll('.iti__country');
                for (const item of items) {
                  const nameEl = item.querySelector('.iti__country-name');
                  const name = (nameEl && nameEl.textContent || '').replace(/\s+/g, ' ').trim();
                  if (name === countryName) {
                    iso = item.getAttribute('data-country-code');
                    break;
                  }
                }
                if (iso) {
                  input.iti.setCountry(iso);
                  return input.iti.getSelectedCountryData().name || iso;
                }
              }
              // Fallback: open dropdown and click matching item.
              const trigger = container.querySelector('.iti__selected-country');
              if (trigger) trigger.click();
              const items = container.querySelectorAll('.iti__country');
              for (const item of items) {
                if (!visible(item)) continue;
                const nameEl = item.querySelector('.iti__country-name');
                const name = (nameEl && nameEl.textContent || '').replace(/\s+/g, ' ').trim();
                if (name === countryName) {
                  item.click();
                  return name;
                }
              }
              return null;
            }""",
            {"countryName": country_name},
        )
    except Exception:
        return None


def _select_greenhouse_react_combobox_option(page, locator, field: dict[str, Any], answer: str) -> str | None:

    if not answer:
        return None
    if "greenhouse.io" not in str(getattr(page, "url", "") or "").lower():

        return None
    # Greenhouse uses the same React select for office, source, sponsorship,
    # work-authorization, and EEO questions, so keep the field coverage broad
    # while containing this commit strategy to the Greenhouse host.
    step = str(answer).split(">")[-1].strip()
    if not step:
        return None
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        locator.click(timeout=3000)
        page.wait_for_timeout(400)

    except Exception as e:

        return None
    option_names = [step]
    fallback = _office_location_combobox_fallback_choice(field, [step], step)
    if fallback and fallback not in option_names:
        option_names.append(fallback)

    def committed_selection() -> str | None:
        try:
            selected = _verify_control_selection(page, field, step)
        except Exception:
            selected = None
        if selected:
            return selected
        try:
            _values, expanded = _control_selection_readback(page, field)
        except Exception:
            expanded = False
        if not expanded:
            return None
        keyboard = getattr(page, "keyboard", None)
        for key in ("Enter", "Tab"):
            try:
                if keyboard is None:
                    break
                keyboard.press(key)
                page.wait_for_timeout(250)
                selected = _verify_control_selection(page, field, step)
                if selected:
                    return selected
            except Exception:
                pass
        return None

    for option_name in option_names:
        try:
            page.get_by_role("option", name=option_name, exact=True).first.click(timeout=3000)
            page.wait_for_timeout(500)
            selected = committed_selection()
            if selected:
                return selected
        except Exception:
            pass
        # Greenhouse React renders options as plain divs without role="option".
        # Try get_by_text as a lighter fallback before the full JS approach.
        try:
            page.get_by_text(option_name, exact=True).first.click(timeout=2000)
            page.wait_for_timeout(500)
            selected = committed_selection()
            if selected:
                return selected
        except Exception:
            pass
    # JS click fallback: Greenhouse React renders dropdown options as
    # plain divs that get_by_role("option") cannot find.
    try:
        clicked = page.evaluate(
            """(text) => {
              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
              const popups = Array.from(document.querySelectorAll(
                '[role="listbox"], [role="menu"], [class*="select__menu"], [class*="-menu"], [class*="-dropdown"], [class*="dropdown-"], [data-popper-placement], [data-radix-popper-content-wrapper], [id*="downshift"], [id*="-menu"], [class*="gph-select"]'
              )).filter(visible);
              for (const popup of popups) {
                const options = Array.from(popup.querySelectorAll(
                  '[role="option"], [role="menuitem"], [class*="select__option"], [class*="-option"], li, div, [id*="-item-"]'
                )).filter(visible).filter(node => !node.querySelector(
                  '[role="option"], [role="menuitem"], [class*="select__option"], [class*="-option"], [id*="-item-"]'
                ));
                for (const opt of options) {
                  if (String(opt.textContent || "").replace(/\s+/g, " ").trim() === text) {
                    opt.click();
                    return true;
                  }
                }
              }
              return false;
            }""",
            step,
        )
        if clicked:
            page.wait_for_timeout(500)
            selected = committed_selection()
            if selected:
                return selected
    except Exception:
        pass
    # Fuzzy fallback: Greenhouse React options sometimes include extra tokens
    # such as dialing codes ("United States +1") that exact matching misses.
    # Re-open the dropdown to ensure options are visible, collect option texts,
    # pick the closest match using the same matching rules as planning, then
    # click the matched option text.
    try:
        try:
            locator.scroll_into_view_if_needed()
        except Exception:
            pass
        try:
            locator.click(timeout=3000)
            page.wait_for_timeout(400)
        except Exception:
            pass
        visible_texts = page.evaluate(
            """() => {
              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
              const popups = Array.from(document.querySelectorAll(
                '[role="listbox"], [role="menu"], [class*="select__menu"], [class*="-menu"], [class*="-dropdown"], [class*="dropdown-"], [data-popper-placement], [data-radix-popper-content-wrapper], [id*="downshift"], [id*="-menu"], [class*="gph-select"]'
              )).filter(visible);
              const texts = [];
              for (const popup of popups) {
                const options = Array.from(popup.querySelectorAll(
                  '[role="option"], [role="menuitem"], [class*="select__option"], [class*="-option"], li, div, [id*="-item-"]'
                )).filter(visible);
                for (const opt of options) {
                  const text = String(opt.textContent || "").replace(/\s+/g, " ").trim();
                  if (text) texts.push(text);
                }
              }
              return texts;
            }"""
        )
        if visible_texts:

            fallback_choice = next(
                (text for text in visible_texts if _option_matches(text, step)),
                None,
            )

            if fallback_choice:
                clicked = page.evaluate(
                    """(text) => {
                      const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                      const popups = Array.from(document.querySelectorAll(
                        '[role="listbox"], [role="menu"], [class*="select__menu"], [class*="-menu"], [class*="-dropdown"], [class*="dropdown-"], [data-popper-placement], [data-radix-popper-content-wrapper], [id*="downshift"], [id*="-menu"], [class*="gph-select"]'
                      )).filter(visible);
                      for (const popup of popups) {
                        const options = Array.from(popup.querySelectorAll(
                          '[role="option"], [role="menuitem"], [class*="select__option"], [class*="-option"], li, div, [id*="-item-"]'
                        )).filter(visible);
                        for (const opt of options) {
                          if (String(opt.textContent || "").replace(/\s+/g, " ").trim() === text) {
                            opt.click();
                            return true;
                          }
                        }
                      }
                      return false;
                    }""",
                    fallback_choice,
                )
                if clicked:
                    page.wait_for_timeout(500)
                    selected = committed_selection()
                    if selected:
                        return selected
    except Exception:
        pass
    # Strategy 5: direct JS event simulation. Click the input to open
    # the dropdown, then dispatch keyboard events directly via JS to
    # filter and select "United States".
    field_label_lower2 = str(field.get('label', '') or '').lower()
    field_id_lower2 = str(field.get('id', '') or '').lower()
    if 'country' in field_label_lower2 or 'country' in field_id_lower2:
        try:
            result = page.evaluate(
                """(payload) => {
                  const input = payload.autofillId
                    ? document.querySelector('[data-job-agent-autofill-index="' + payload.autofillId + '"]')
                    : (payload.id ? document.getElementById(payload.id) : null);
                  if (!input) return null;
                  // Focus and click to open dropdown
                  input.focus();
                  input.click();
                  input.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                  return 'clicked';
                }""",
                {'autofillId': str(field.get('autofillId', '')), 'id': str(field.get('id', ''))},
            )
            if result:
                page.wait_for_timeout(500)
                # Type 'United States' via JS input events
                page.evaluate(
                    """(payload) => {
                      const input = payload.autofillId
                        ? document.querySelector('[data-job-agent-autofill-index="' + payload.autofillId + '"]')
                        : (payload.id ? document.getElementById(payload.id) : null);
                      if (!input) return;
                      const text = 'United States';
                      for (const char of text) {
                        input.dispatchEvent(new KeyboardEvent('keydown', {key: char, bubbles: true}));
                        input.dispatchEvent(new KeyboardEvent('keypress', {key: char, bubbles: true}));
                        if (input.tagName === 'INPUT' || input.isContentEditable) {
                          const val = input.value || input.textContent || '';
                          if (input.value !== undefined) input.value = val + char;
                          else input.textContent = val + char;
                        }
                        input.dispatchEvent(new KeyboardEvent('keyup', {key: char, bubbles: true}));
                        input.dispatchEvent(new Event('input', {bubbles: true}));
                      }
                    }""",
                    {'autofillId': str(field.get('autofillId', '')), 'id': str(field.get('id', ''))},
                )
                page.wait_for_timeout(1000)
                # ArrowDown + Enter via JS
                page.evaluate(
                    """(payload) => {
                      const input = payload.autofillId
                        ? document.querySelector('[data-job-agent-autofill-index="' + payload.autofillId + '"]')
                        : (payload.id ? document.getElementById(payload.id) : null);
                      if (!input) return;
                      input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, bubbles: true}));
                      input.dispatchEvent(new KeyboardEvent('keyup', {key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, bubbles: true}));
                      setTimeout(() => {
                        input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
                        input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true}));
                      }, 200);
                    }""",
                    {'autofillId': str(field.get('autofillId', '')), 'id': str(field.get('id', ''))},
                )
                page.wait_for_timeout(800)
                selected = committed_selection()
                if selected:
                    return selected
        except Exception:
            pass
    # Strategy 4: direct DOM manipulation for Greenhouse React country
    # selects. These are often backed by a hidden <select> element that
    # the React component syncs with. Set its value and dispatch change.
    field_label_lower = str(field.get('label', '') or '').lower()
    field_id_lower = str(field.get('id', '') or '').lower()
    field_label_lower2 = str(field.get('label', '') or '').lower()
    field_id_lower2 = str(field.get('id', '') or '').lower()
    if 'country' in field_label_lower2 or 'country' in field_id_lower2:
        try:
            result = page.evaluate(
                """(payload) => {
                  const input = payload.autofillId
                    ? document.querySelector('[data-job-agent-autofill-index="' + payload.autofillId + '"]')
                    : (payload.id ? document.getElementById(payload.id) : null);
                  if (!input) return null;
                  const form = input.closest('form');
                  if (!form) return null;
                  const selects = Array.from(form.querySelectorAll('select'));
                  for (const s of selects) {
                    if (!(s.name || '').toLowerCase().includes('country')
                        && !(s.id || '').toLowerCase().includes('country'))
                      continue;
                    for (const opt of s.options) {
                      const optText = (opt.textContent || opt.label || '').trim().toLowerCase();
                      if (optText.includes('united states')) {
                        s.value = opt.value;
                        s.dispatchEvent(new Event('change', {bubbles: true}));
                        s.dispatchEvent(new Event('input', {bubbles: true}));
                        return opt.value;
                      }
                    }
                  }
                  return null;
                }""",
                {'autofillId': str(field.get('autofillId', '')), 'id': str(field.get('id', ''))},
            )
            if result:
                page.wait_for_timeout(500)
                selected = committed_selection()
                if selected:
                    return selected
        except Exception:
            pass
    return None


def _is_negative_veteran_option(option_text: str) -> bool:
    normalized = _norm(option_text)
    if not normalized or "veteran" not in normalized:
        return False
    if "identify as a veteran" in normalized or "i am a veteran" in normalized:
        return False
    return (
        normalized == "no"
        or "not a veteran" in normalized
        or "not a protected veteran" in normalized
        or "non veteran" in normalized
    )


def _is_negative_answer(answer: Any) -> bool:
    return _norm(answer) in {"no", "false", "none", "n/a", "na", "do not consent", "i do not consent"}


def _use_structured_auto_answer(label: str) -> bool:
    normalized = _norm(label)
    return any(
        token in normalized
        for token in [
            "anchor days",
            "citizenship",
            "employment eligibility",
            "work eligibility",
            "government or public institution",
            "public institution employment",
            "government employment experience",
            "conflict of interest",
            "conflicts of interest",
            "how did you hear",
            "pronouns",
            "prior internships",
            "previous internships",
            "which location",
            "hybrid",
            "in office",
            "working from one of our offices",
            "office days",
            "currently live in",
            "plan to relocate",
            "highest level of education",
            "professional software development experience",
            "willing to work onsite",
            "open to positions in other countries",
            "previously worked at",
            "worked at",
            "employee id",
        ]
    )


def _priority_auto_answer(label: str, profile: dict[str, Any]) -> str | None:
    normalized = _norm(label)
    if _is_palantir_profile(profile):
        palantir_answer = _palantir_auto_answer(label, profile)
        if palantir_answer is not None:
            return palantir_answer
    sponsorship_countries = _sponsorship_countries_answer(label, profile)
    if sponsorship_countries is not None:
        return sponsorship_countries
    current_based_country = _current_based_country_answer(label, profile)
    if current_based_country is not None:
        return current_based_country
    other_countries = _other_countries_location_answer(label, profile)
    if other_countries is not None:
        return other_countries
    conflict_answer = _conflict_of_interest_screening_answer(label, profile)
    if conflict_answer is not None:
        return conflict_answer
    government_public_answer = _government_public_employment_answer(label, profile)
    if government_public_answer is not None:
        return government_public_answer
    listed_country_status = _listed_country_status_answer(label, profile)
    if listed_country_status is not None:
        return listed_country_status
    listed_state_residency = _listed_state_residency_answer(label, profile)
    if listed_state_residency is not None:
        return listed_state_residency
    if "high school" in normalized:
        if "graduation" in normalized or "year" in normalized:
            return _high_school_value(profile, "end_year")
        if "name" in normalized or "school" in normalized:
            return _high_school_value(profile, "school")
        return None
    if _is_school_combobox_field({"label": label}) and "degree" not in normalized and "level" not in normalized:
        school = _current_education_value(profile, "school")
        return str(school) if school else None
    if (
        "consent" in normalized
        and ("processing" in normalized or "process" in normalized)
        and ("personal information" in normalized or "personal data" in normalized or "privacy policy" in normalized)
    ):
        consent = (
            _approved_sensitive_entry_answer(profile, "privacy_consent")
            or _approved_sensitive_entry_answer(profile, "terms_consent")
            or _approved_sensitive_entry_answer(profile, "legal_attestation")
        )
        return "Consent" if _truthy_answer(consent) else None
    if "currently based in one of the following geographies" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("based in san francisco" in normalized or "san francisco based" in normalized)
        and "open to relocating" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        "currently live in" in normalized
        and "plan to relocate" in normalized
        and ("in office" in normalized or "in person" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        "requires in office work" in normalized
        and "acknowledge" in normalized
        and "agree" in normalized
        and ("three days" in normalized or "3 days" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if "degree in computer science" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "highest level of education" in normalized and "completed" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if (
        "professional software development experience" in normalized
        and ("object oriented" in normalized or "object-oriented" in normalized)
    ):
        return _professional_software_experience_range_answer(profile)
    if _is_relevant_professional_experience_years_question(label):
        return _relevant_professional_experience_range_answer(profile)
    if (
        "how many years of professional experience" in normalized
        and "excluding internships" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("full time software engineer" in normalized or "full time software engineering" in normalized)
        and "professional setting" in normalized
        and "excluding internships" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        "which programming languages" in normalized
        and "regularly use" in normalized
        and "professional setting" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if "willing to work onsite" in normalized and "office" in normalized and ("5 days" in normalized or "five days" in normalized):
        relocation = (
            (profile.get("answers") or {}).get("Are you open to relocation?")
            or _approved_sensitive_entry_answer(profile, "relocation")
            or _match_sensitive("relocation", profile)
        )
        return "Yes" if _truthy_answer(relocation) else None
    if "bachelor" in normalized and "degree" in normalized and (
        "computer science" in normalized
        or "data science" in normalized
        or "software engineering" in normalized
        or "closely related" in normalized
        or "related field" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("large language model" in normalized or "llm" in normalized)
        and ("worked with" in normalized or "completed academic projects" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if _is_source_question(normalized):
        return _auto_answer(label, profile, sensitive=False)
    if "pronouns" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if normalized == "language":
        return _auto_answer(label, profile, sensitive=False)
    if "gender" in normalized or "transgender" in normalized:
        demographic = _demographic_answer(label, profile)
        if demographic is not None:
            if normalized == "gender":
                return str((profile.get("demographics") or {}).get("gender") or demographic)
            return demographic
    if normalized == "i identify as" or ("demographic" in normalized and "i identify as" in normalized):
        return "I don't wish to answer"
    if "sexual orientation" in normalized:
        return _demographic_answer(label, profile)
    if normalized == "age":
        return _demographic_answer(label, profile)
    if "hispanic" in normalized or "latino" in normalized:
        demographic = _demographic_answer(label, profile)
        if demographic is not None:
            return demographic
    if "race" in normalized or "veteran" in normalized or "disability" in normalized or "disabled" in normalized:
        demographic = _demographic_answer(label, profile)
        if demographic is not None:
            return demographic
    if "contact" in normalized and "current employer" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "been employed by" in normalized and ("past" in normalized or "subsidiary" in normalized or "affiliate" in normalized):
        return _auto_answer(label, profile, sensitive=False)
    if "commutable proximity" in normalized and "relocat" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("anchor days" in normalized or "working from one of our offices" in normalized)
        and ("office" in normalized or "in person" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("hybrid" in normalized or "in office" in normalized or "in-office" in normalized)
        and "office" in normalized
        and ("day" in normalized or "days" in normalized)
        and ("able to meet" in normalized or "are you able" in normalized or "able to commit" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("foster city" in normalized or "hq 3 days per week" in normalized)
        and ("3 days" in normalized or "three days" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    developer_facing = _developer_facing_products_answer(label, profile)
    if developer_facing is not None:
        return developer_facing
    if "1099" in normalized and ("without requiring" in normalized or "without sponsorship" in normalized or "complete any paperwork" in normalized):
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("legally authorized" in normalized or "authorized to work" in normalized)
        and ("without requiring" in normalized or "without sponsorship" in normalized)
        and ("sponsorship" in normalized or "visa" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if "compensation offer" in normalized and "factors" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "at least 18 years of age" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "confirm receipt" in normalized and ("privacy notice" in normalized or "arbitration agreement" in normalized):
        return _auto_answer(label, profile, sensitive=False)
    if "privacy notice" in normalized and (
        "acknowledge" in normalized
        or "acknowledgement" in normalized
        or "acknowledgment" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if "consent to process" in normalized or (
        "process" in normalized and "personal data" in normalized and "consent" in normalized
    ):
        return _auto_answer(label, profile, sensitive=False)
    if _legal_terms_consent_answer(label, profile) is not None:
        return _auto_answer(label, profile, sensitive=False)
    if "may use ai tools" in normalized and "application and interview process" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "best describes how you use ai tools today" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "current government official" in normalized or "former government official" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "close relative of a government official" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if (
        "referred to this position" in normalized
        and ("senior leader" in normalized or "decision maker" in normalized or "decisionmaker" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if (
        "if you answered yes" in normalized
        and ("employment authorization" in normalized or "immigration" in normalized or "sponsorship" in normalized)
        and ("explanation" in normalized or "explain" in normalized or "provide" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if _biopharma_compliance_answer(label, profile) is not None:
        return _auto_answer(label, profile, sensitive=False)
    years_match = re.search(r"at least\s+(\d+)\s*(?:\+)?\s+years?", normalized)
    if years_match and "experience" in normalized:
        return _auto_answer(label, profile, sensitive=False)
    if "ai screening" in normalized and ("agree" in normalized or "proceed" in normalized or "consent" in normalized):
        return _auto_answer(label, profile, sensitive=False)
    if "citizenship" in normalized and ("employment eligibility" in normalized or "work eligibility" in normalized):
        return _auto_answer(label, profile, sensitive=False)
    if (
        ("currently based" in normalized or "currently living" in normalized)
        and ("san francisco" in normalized or "bay area" in normalized)
    ):
        return _auto_answer(label, profile, sensitive=False)
    if "candidate privacy policy" in normalized and _profile_company_slug(profile) == "airbnb":
        return _auto_answer(label, profile, sensitive=False)
    return None


def _candidate_account_password_store_path() -> Path:
    override = str(os.getenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_STORE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.cwd() / _CANDIDATE_ACCOUNT_PASSWORD_STORE_FILENAME


def _candidate_account_password_host(application_url: str | None) -> str | None:
    host = urlparse(str(application_url or "").strip()).hostname or ""
    host = host.strip().lower()
    return host or None


def _candidate_account_password_store_key(application_url: str | None, email: str | None) -> str | None:
    host = _candidate_account_password_host(application_url)
    email_value = str(email or "").strip().lower()
    if not host or not email_value:
        return None
    return f"{host}\0{email_value}"


def _load_candidate_account_password_store(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "accounts": {}}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "accounts": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "accounts": {}}
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        payload["accounts"] = {}
    payload.setdefault("version", 1)
    return payload


def _save_candidate_account_password_store(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True))
    tmp_path.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _generate_candidate_account_password(length: int = _CANDIDATE_ACCOUNT_PASSWORD_LENGTH) -> str:
    alphabet = string.ascii_letters + string.digits + _CANDIDATE_ACCOUNT_PASSWORD_SPECIALS
    size = max(16, int(length))
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(size))
        if (
            any(character.islower() for character in password)
            and any(character.isupper() for character in password)
            and any(character.isdigit() for character in password)
            and any(character in _CANDIDATE_ACCOUNT_PASSWORD_SPECIALS for character in password)
        ):
            return password


def _candidate_account_password(
    profile: dict[str, Any] | None = None,
    *,
    application_url: str | None = None,
    create_if_missing: bool = False,
) -> str | None:
    value = str(os.getenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD") or "").strip()
    if value:
        return value
    password_file = str(os.getenv("JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD_FILE") or "").strip()
    if password_file:
        try:
            value = Path(password_file).read_text().strip()
        except OSError:
            value = ""
        if value:
            return value
    profile_data = profile or {}
    resolved_url = str(application_url or profile_data.get("_application_url") or "").strip()
    resolved_email = str(profile_data.get("email") or "").strip().lower()
    key = _candidate_account_password_store_key(resolved_url, resolved_email)
    if not key:
        return None
    store_path = _candidate_account_password_store_path()
    store = _load_candidate_account_password_store(store_path)
    accounts = store.setdefault("accounts", {})
    record = accounts.get(key)
    if isinstance(record, dict):
        value = str(record.get("password") or "").strip()
        if value:
            return value
    elif isinstance(record, str):
        value = str(record).strip()
        if value:
            return value
    if not create_if_missing:
        return None
    host = _candidate_account_password_host(resolved_url)
    if not host:
        return None
    password = _generate_candidate_account_password()
    timestamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    accounts[key] = {
        "host": host,
        "email": resolved_email,
        "password": password,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _save_candidate_account_password_store(store_path, store)
    return password


def _is_candidate_account_creation_field(field: dict[str, Any]) -> bool:
    combined = _norm(
        " ".join(
            str(field.get(key) or "")
            for key in ["label", "id", "name", "section", "ariaLabel", "ariaDescription", "placeholder"]
        )
    )
    return any(
        marker in combined
        for marker in (
            "verify password",
            "verifypassword",
            "verify new password",
            "confirm password",
            "new password",
            "create account",
        )
    )


def _runtime_fill_context(fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidate_account_creation": any(_is_candidate_account_creation_field(field) for field in fields)
    }


def _is_candidate_account_creation_consent_checkbox(
    field: dict[str, Any], runtime_context: dict[str, Any] | None
) -> bool:
    if not runtime_context or not runtime_context.get("candidate_account_creation"):
        return False
    automation_id = _norm(field.get("automationId") or field.get("automation_id") or "")
    label = _norm(field.get("label") or "")
    if automation_id == "createaccountcheckbox":
        return True
    return label in {"agree", "accept", "yes", "i agree"}


def _answer_aliases(answer: Any) -> list[str]:
    raw = str(answer or "")
    aliases = [raw]
    aliases.extend(_graduation_date_aliases(raw))
    if _norm(raw) in {
        "prefer not to say",
        "prefer not to answer",
        "decline",
        "decline to answer",
        "i don t wish to answer",
        "i do not wish to answer",
        "i do not want to answer",
    }:
        aliases.extend(
            [
                "Prefer not to say",
                "I prefer not to say",
                "Decline to self-identify",
                "Decline To Self Identify",
                "I decline to self-identify",
                "I don't wish to answer",
                "I do not wish to answer",
                "I do not want to answer",
            ]
        )
    if _norm(raw) in {"east asian", "asian"}:
        aliases.extend(["Asian", "East Asian", "Asian (Not Hispanic or Latino)"])
    if _norm(raw) == "male":
        aliases.append("Man")
    if _norm(raw) == "man":
        aliases.append("Male")
    if _norm(raw) == "female":
        aliases.append("Woman")
    if _norm(raw) == "linkedin":
        aliases.append("LinkedIn Jobs")
    if _norm(raw) in {"yes", "confirmed", "confirm", "agree", "i agree", "acknowledge", "i acknowledge"}:
        aliases.extend(
            [
                "I Agree",
                "I Acknowledge",
                "Yes, I agree",
                "Yes, I acknowledge",
                "Acknowledge",
                "Confirm",
                "Confirmed",
                "Acknowledge/Confirm",
                "I have reviewed and confirmed that all the information provided is accurate and complete.",
            ]
        )
    if _norm(raw) in {"master s degree", "masters degree", "master degree", "masters", "master"}:
        aliases.extend(["Master's Degree", "Master Degree"])
    if _norm(raw) in {"1 2", "1 2 years", "2", "2 years", "2 5", "2 5 years"}:
        aliases.extend(["2-5 years", "2 - 5 years"])
    if _norm(raw) in {"within a month", "in one month", "one month", "immediately", "as soon as possible", "asap"}:
        aliases.extend(
            [
                "Immediately/next few months, full-time",
                "Immediately / next few months, full-time",
                "Immediately/next few months",
                "Immediately",
            ]
        )
    if _norm(raw) == "computer science":
        aliases.extend(
            [
                "Computer Science",
                "Computer and Information Sciences",
                "Computer and Information Sciences, General",
            ]
        )
    if _norm(raw) in {"he him his", "he him", "he his"}:
        aliases.extend(["He / Him", "He/Him", "He/Him/His"])
    if _norm(raw) in {"united states", "united states of america", "usa", "us", "u s", "u s a"}:
        aliases.extend(
            [
                "United States",
                "United States +1",
                "United States of America",
                "United States of America +1",
                "USA",
                "USA +1",
                "US",
                "US +1",
                "U.S.",
                "U.S.A.",
            ]
        )
    if _norm(raw) in {"+1", "1", "united states of america (+1)", "united states (+1)"}:
        aliases.extend(
            [
                "+1",
                "United States +1",
                "United States (+1)",
                "United States of America (+1)",
                "USA (+1)",
            ]
        )
    if _norm(raw) == "no":
        aliases.extend(
            [
                "I'm not open to other locations",
                "Not open to other locations",
                "I am not a veteran",
                "I am not a protected veteran",
                "Not a veteran",
                "Not a protected veteran",
                "I have never worked at",
                "I have never worked for",
                "I have never served in the military",
                "I am not disabled",
                "I do not have a disability",
                "No, I do not have a disability",
                "No, I don't have a disability",
                "No, I don't have a disability and have not had one in the past",
                "No - I do not consent to receiving text messages",
            ]
        )
    if _norm(raw) in {
        "company website",
        "company site",
        "company careers",
        "company career site",
        "career site",
        "career website",
        "careers website",
        "careers site",
    }:
        aliases.extend(
            [
                "Website",
                "Company Website",
                "Corporate Website",
                "Career Site",
                "Careers Website",
                "Career Website",
                "Career Webpage",
                "Careers Page",
                "Company Careers",
                "Careers Site",
                "Careers Webpage",
                "Company Careers Webpage",
            ]
        )
    if _norm(raw) == "opt":
        aliases.extend(["F1", "F-1", "OPT / F-1", "Yes"])
    if _norm(raw) == "primary":
        aliases.extend(["Cell", "Mobile"])
    if _norm(raw).startswith("yes") and len(_norm(raw)) > 3:
        aliases.append("Yes")
    if _norm(raw).startswith("no") and len(_norm(raw)) > 2:
        aliases.append("No")
    if "require will require" in _norm(raw) and "sponsorship" in _norm(raw):
        aliases.extend(
            [
                "I require/will require sponsorship",
                "I require sponsorship",
                "I will require sponsorship",
            ]
        )
    return aliases


def _option_text(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("label") or option.get("value") or "")
    return str(option or "")


def _is_source_question(label: str) -> bool:
    normalized = _norm(label)
    return (
        "how did you hear" in normalized
        or "tell us how you heard" in normalized
        or "where did you hear" in normalized
        or bool(re.search(r"\bwhere\b.*\b(hear|heard)\b", normalized))
        or bool(re.search(r"\bhow\b.*\b(hear|heard)\b", normalized))
        or "where have you learned about" in normalized
    )


def _is_company_website_answer(answer: Any) -> bool:
    return _norm(answer) in {
        "company website",
        "company site",
        "company careers",
        "company career site",
        "career site",
        "career website",
        "careers website",
        "careers site",
    }


def _matching_options(field: dict[str, Any], answer: Any) -> list[Any]:
    options = field.get("options") or []
    aliases = [alias for alias in _answer_aliases(answer) if _norm(alias)]
    scored_matches: list[tuple[int, int, Any]] = []
    for index, option in enumerate(options):
        option_text = _option_text(option)
        if not _option_matches(option_text, answer):
            continue
        score = max((_option_match_score(option_text, alias) for alias in aliases), default=0)
        scored_matches.append((score, index, option))
    matches = [
        option
        for _score, _index, option in sorted(
            scored_matches,
            key=lambda item: (-item[0], item[1]),
        )
    ]
    if not matches and _norm(answer) in {"yes", "no"} and len(options) == 2:
        negative_markers = (
            "no ",
            "not ",
            "unable",
            "cannot",
            "can t",
            "do not",
            "don t",
            "will not",
            "won t",
            "never",
        )
        negative = [
            option
            for option in options
            if any(
                marker in f"{_norm(_option_text(option))} "
                for marker in negative_markers
            )
        ]
        affirmative = [option for option in options if option not in negative]
        if len(negative) == 1 and len(affirmative) == 1:
            return affirmative if _norm(answer) == "yes" else negative
    if matches or not _is_source_question(str(field.get("label") or "")):
        return matches
    if not _is_company_website_answer(answer):
        return matches
    # A company careers site is truthfully categorized as "Other" when a
    # source question does not expose a more specific company-site option.
    return [option for option in options if _norm(_option_text(option)) == "other"]


def _single_job_code_option(field: dict[str, Any], label: str) -> str | None:
    normalized = _norm(label)
    if "job code" not in normalized or "posting" not in normalized:
        return None
    code_options = [
        _option_text(option).strip()
        for option in field.get("options") or []
        if _looks_like_job_code_option(_option_text(option).strip())
    ]
    return code_options[0] if len(code_options) == 1 else None


def _looks_like_job_code_option(text: str) -> bool:
    value = text.strip()
    if re.fullmatch(r"\d{4,}", value):
        return True
    return bool(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{4,}", value)
        and re.search(r"[A-Za-z]", value)
        and re.search(r"\d", value)
    )


def _technical_screening_checkbox_options(
    field: dict[str, Any],
    profile: dict[str, Any],
) -> list[Any]:
    """Select truthful technical-experience checkbox options from profile evidence.

    Greenhouse and similar ATS forms frequently ask "select all that apply"
    screening questions for AI concepts, LLM APIs, cloud platforms, and vector
    stores. These are not consent checkboxes: they are profile-evidence fields.
    The matcher is intentionally conservative and only selects options whose
    label is supported by explicit profile skills, projects, or vector-context
    text. Negative placeholders such as "None" are never selected when evidence
    supports positive options.
    """
    options = field.get("options") or []
    if not options:
        return []
    context = _norm(
        " ".join(
            str(field.get(key) or "")
            for key in [
                "label",
                "section",
                "name",
                "id",
                "ariaLabel",
                "ariaDescription",
                "placeholder",
            ]
        )
    )
    if not any(
        marker in context
        for marker in (
            "ai concepts",
            "large language model",
            "llm",
            "cloud platforms",
            "rag",
            "retrieval augmented generation",
            "vector database",
            "vector databases",
        )
    ):
        return []
    profile_text = _profile_technical_evidence_text(profile)

    def has(*needles: str) -> bool:
        return _has_profile_evidence(profile_text, *needles)

    matched: list[Any] = []
    concrete_vector_match = False
    for option in options:
        option_norm = _norm(_option_text(option))
        if not option_norm or option_norm in {"none", "none of the above", "not applicable", "n a"}:
            continue
        selected = False
        if "ai concepts" in context:
            selected = (
                ("prompt engineering" in option_norm and has("llm", "large language", "langchain", "agent", "rag", "openai"))
                or ("embedding" in option_norm and has("embedding", "bert", "rag", "retrieval"))
                or ("vector search" in option_norm and has("rag", "retrieval", "vector"))
                or (("retrieval augmented generation" in option_norm or option_norm == "rag") and has("rag", "retrieval augmented generation", "retrieval"))
                or ("semantic search" in option_norm and has("semantic similarity", "bert embedding", "retrieval"))
                or ("fine tuning" in option_norm and has("fine tuning", "fine tuned", "fine-tuned", "lora"))
                or ("agent" in option_norm and has("agent", "multi agent", "langchain", "xclaw"))
            )
        elif "large language model" in context or "llm" in context:
            selected = (
                (option_norm == "openai" and has("openai", "chatgpt", "codex"))
                or (("anthropic" in option_norm or "claude" in option_norm) and has("anthropic", "claude"))
                or ("gemini" in option_norm and has("gemini", "google gemini"))
                or ("azure openai" in option_norm and has("azure openai"))
                or ("hugging face" in option_norm and has("huggingface", "hugging face", "transformers"))
                or ("ollama" in option_norm and has("ollama"))
            )
        elif "cloud platforms" in context:
            selected = (
                (("microsoft azure" in option_norm or option_norm == "azure") and has("microsoft azure", "azure"))
                or (("amazon web services" in option_norm or "aws" in option_norm) and has("aws", "amazon web services"))
                or (("google cloud platform" in option_norm or option_norm == "gcp") and has("google cloud platform", "gcp"))
            )
        elif (
            "rag" in context
            or "retrieval augmented generation" in context
            or "vector database" in context
            or "vector databases" in context
        ):
            selected = (
                ("pinecone" in option_norm and has("pinecone"))
                or ("weaviate" in option_norm and has("weaviate"))
                or ("chroma" in option_norm and has("chroma"))
                or ("azure ai search" in option_norm and has("azure ai search"))
                or ("faiss" in option_norm and has("faiss"))
                or ("milvus" in option_norm and has("milvus"))
            )
            if selected:
                concrete_vector_match = True
        if selected:
            matched.append(option)

    if (
        not concrete_vector_match
        and (
            "rag" in context
            or "retrieval augmented generation" in context
            or "vector database" in context
            or "vector databases" in context
        )
        and has("rag", "retrieval augmented generation", "retrieval", "vector")
    ):
        other = next(
            (option for option in options if _norm(_option_text(option)) == "other"),
            None,
        )
        if other is not None:
            matched.append(other)
    return matched


def _export_control_checkbox_options(field: dict[str, Any]) -> list[Any]:
    """Select truthful default options for sanctions/export-control screens.

    Some Greenhouse forms ask a two-step export-control question:
    first whether the candidate is connected to sanctioned countries/regions,
    then a conditional follow-up that is only applicable when the first answer
    is not "none of the above".  The user's profile is not a citizen or
    resident of the listed sanctioned jurisdictions, so the truthful selections
    are the negative/conditional-not-applicable options.  Keep the matcher
    narrow so ordinary citizenship or legal-status checkbox groups still block
    unless they have an explicit approved answer.
    """
    options = field.get("options") or []
    if not options:
        return []
    label = _norm(field.get("label") or "")
    option_text = _norm(" ".join(_option_text(option) for option in options))
    context = f"{label} {option_text}"

    sanctions_question = (
        ("sanctions" in context or "export controls" in context or "export controlled" in context)
        and any(country in context for country in ["cuba", "iran", "north korea", "syria"])
    )
    if sanctions_question:
        none_option = next(
            (option for option in options if _norm(_option_text(option)) == "none of the above"),
            None,
        )
        if none_option is not None:
            return [none_option]

    conditional_none_followup = (
        "prior question" in label
        and "none of the above" in label
        and any(marker in option_text for marker in ["not applicable", "selected none of the above"])
    )
    if conditional_none_followup:
        not_applicable = next(
            (
                option
                for option in options
                if "not applicable" in _norm(_option_text(option))
                and "none of the above" in _norm(_option_text(option))
            ),
            None,
        )
        if not_applicable is not None:
            return [not_applicable]

    return []


def _palantir_checkbox_group_plan(
    field: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any] | None:
    if not _is_palantir_profile(profile):
        return None
    label = _norm(field.get("label") or "")
    options = field.get("options") or []

    def matching(*needles: str) -> list[Any]:
        selected = []
        for option in options:
            option_norm = _norm(_option_text(option))
            if any(needle in option_norm for needle in needles):
                selected.append(option)
        return selected

    if "if so" in label and "what are the dates" in label:
        return {
            "action": "skip",
            "reason": "no offer deadlines reported",
            "sensitive": False,
            "blocking": False,
        }
    if "preferred office location" in label:
        target_location = _norm(profile.get("target_location") or "")
        if "new york" in target_location or not target_location:
            matches = matching("new york")
        elif "palo alto" in target_location:
            matches = matching("palo alto")
        elif "seattle" in target_location:
            matches = matching("seattle")
        elif "denver" in target_location:
            matches = matching("denver")
        elif "washington" in target_location or "dc" in target_location:
            matches = matching("washington", "dc")
        else:
            matches = []
        if matches:
            return {"action": "checkmany", "options": matches[:3]}
    if "fdse and swe roles" in label and "confirm" in label:
        title = _norm(profile.get("target_title") or "")
        yes_matches = matching("yes")
        if yes_matches and ("software engineer" in title or "forward deployed" in title):
            return {"action": "checkmany", "options": yes_matches[:1]}
        if not options and "software engineer" in title:
            return {"action": "check"}
        if "forward deployed" in title:
            matches = matching("forward deployed", "fdse")
        else:
            matches = matching("software engineer", "swe") if "software engineer" in title else []
        if matches:
            return {"action": "checkmany", "options": matches[:1]}
    if not options:
        return None
    if "preferred palantir product" in label:
        matches = matching("foundry", "apollo")
        if matches:
            return {"action": "checkmany", "options": matches[:2]}
    return None


def _technical_screening_checkbox_plan(
    field: dict[str, Any],
    profile: dict[str, Any],
    mapping_label: str,
) -> dict[str, Any] | None:
    """Plan a single checkbox that is actually an option in a technical group."""
    option_label = _norm(field.get("label") or "")
    if not option_label:
        return None
    context = _norm(mapping_label)
    if option_label in {"none", "none of the above", "not applicable", "n a"} and any(
        marker in context
        for marker in (
            "ai concepts",
            "large language model",
            "llm",
            "cloud platforms",
            "rag",
            "retrieval augmented generation",
            "vector database",
            "vector databases",
        )
    ):
        return {
            "action": "skip",
            "reason": "technical screening negative option not selected",
            "sensitive": False,
            "blocking": False,
        }
    synthetic_group = dict(field)
    synthetic_group["options"] = [field]
    synthetic_group["label"] = mapping_label
    matches = _technical_screening_checkbox_options(synthetic_group, profile)
    if matches:
        return {"action": "check"}
    if any(
        marker in context
        for marker in (
            "ai concepts",
            "large language model",
            "llm",
            "cloud platforms",
            "rag",
            "retrieval augmented generation",
            "vector database",
            "vector databases",
        )
    ) and option_label in {
        "google gemini",
        "azure openai service",
        "ollama",
        "pinecone",
        "weaviate",
        "chroma",
        "azure ai search",
        "faiss",
        "milvus",
    }:
        return {
            "action": "skip",
            "reason": "technical screening option not supported by profile evidence",
            "sensitive": False,
            "blocking": False,
        }
    return None


def _education_field_for_level(profile: dict[str, Any], level: str) -> str | None:
    wanted = _norm(level)
    for entry in profile.get("education") or []:
        if not isinstance(entry, dict):
            continue
        degree = _norm(entry.get("degree"))
        if wanted in degree:
            field = str(entry.get("field") or "").strip()
            if field:
                return field
    return None


def _degree_field_question(label: str) -> str | None:
    normalized = _norm(label)
    if "bachelor" in normalized and ("field" in normalized or "major" in normalized):
        return "bachelor"
    if "master" in normalized and ("field" in normalized or "major" in normalized):
        return "master"
    return None


def _privacy_preserving_pronoun_option(field: dict[str, Any]) -> Any | None:
    if "pronouns" not in _norm(field.get("label") or ""):
        return None
    for preferred in ["use name only", "prefer not to say", "not represented here"]:
        for option in field.get("options") or []:
            if _norm(_option_text(option)) == preferred:
                return option
    return None


def _yes_no_screening_answer(
    field: dict[str, Any],
    profile: dict[str, Any],
    label: str,
) -> dict[str, Any] | None:
    """Use screening rules to answer Yes/No questions rendered as dropdowns.

    Workday sometimes renders conflict-of-interest and similar screening
    questions as custom single-select dropdowns instead of radio groups. The
    generic combobox path can struggle to commit those values, so when the
    label matches a user-approved screening rule and the control exposes Yes/No
    options, we click the option directly like a radio button.
    """
    required = bool(field.get("required") or _field_label_appears_required(label))
    if not required:
        return None
    normalized = _norm(label)
    if not any(
        pattern in normalized
        for pattern in [
            "previously worked",
            "currently employed",
            "currently engaged",
            "conflict of interest",
            "contractor",
            "consultant",
            "former employee",
        ]
    ):
        return None
    options = field.get("options") or []
    yes_option = next(
        (option for option in options if _norm(_option_text(option)) == "yes"),
        None,
    )
    no_option = next(
        (option for option in options if _norm(_option_text(option)) == "no"),
        None,
    )
    if yes_option is None or no_option is None:
        return None
    rule_answer = match_screening_rule(label, profile.get("screening_answer_rules"))
    if rule_answer is None:
        return None
    if _norm(rule_answer) == "yes":
        return {"action": "check", "option": yes_option}
    if _norm(rule_answer) == "no":
        return {"action": "check", "option": no_option}
    return None


def _generalized_screening_answer(
    field: dict[str, Any],
    profile: dict[str, Any],
    label: str,
    *,
    sensitive: bool,
) -> str | list[Any] | None:
    """Last-resort answer for an unknown non-sensitive screening question.

    Resolution order: the user's approved ``screening_answer_rules`` (standing
    answers for whole question families), then the guarded LLM fallback
    grounded in profile facts. Sensitive fields never reach this layer — they
    still require an approved sensitive-KB answer. Option-bearing kinds return
    a list of validated option entries from the field itself; free-text kinds
    return a plain string. ``None`` keeps the original blocking-review path.
    """
    if sensitive or _requires_user_authored_answer(label, profile):
        return None
    rule_answer = match_screening_rule(label, profile.get("screening_answer_rules"))
    if rule_answer is not None:
        return rule_answer
    resolver = get_llm_answer_resolver()
    if resolver is None:
        return None
    return resolver.answer_for_field(field, profile, label=label)


def _field_label_appears_required(label: str | None) -> bool:
    """Detect required asterisks / text that the scraper may have missed.

    Only treat explicit patterns as required: ``* Required``, ``Required *``,
    or ``(*)``. A bare trailing asterisk glued to punctuation (e.g.
    ``eligibility?*``) is intentionally ignored because it is ambiguous and
    existing field schemas already expose a dedicated ``required`` flag.
    """
    norm = " ".join(str(label or "").split())
    return bool(
        re.search(r"\*\s*required", norm, re.IGNORECASE)
        or re.search(r"required\s*\*", norm, re.IGNORECASE)
        or re.search(r"\(\s*\*\s*\)", norm)
    )


def _plan_field(
    field: dict[str, Any],
    profile: dict[str, Any],
    resume_file: str | None,
    cover_letter_file: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label = field.get("label") or ""
    required = bool(field.get("required") or _field_label_appears_required(label))
    semantic = classify_field(field)
    profile_date_semantic = bool(
        semantic
        and semantic.key.startswith(("education.", "work."))
        and semantic.key.endswith((".date", ".month", ".day", ".year"))
    )
    mapping_label = " ".join(
        str(field.get(key) or "")
        for key in [
            "label",
            "id",
            "name",
            "section",
            "ariaLabel",
            "ariaDescription",
            "placeholder",
            "autocomplete",
        ]
    )
    option_labels = " ".join(
        str(option.get("label") or option.get("value") or "")
        if isinstance(option, dict)
        else str(option)
        for option in field.get("options") or []
    )
    answer_label = label
    if (
        "communicationconsent" in _norm(mapping_label).replace(" ", "")
        or "text messages" in _norm(option_labels)
    ):
        answer_label = f"{label} Do you consent to receiving text messages?"
    if _norm(label) == "i identify as" and (
        "demographic" in _norm(mapping_label) or "survey" in _norm(mapping_label)
    ):
        answer_label = f"{mapping_label} {label}"
    yes_no_plan = _yes_no_screening_answer(field, profile, answer_label)
    if yes_no_plan is not None:
        return yes_no_plan
    if _is_honeypot_field(mapping_label):
        return {"action": "skip", "reason": "honeypot field", "blocking": False}
    if _requires_external_application_portal(" ".join([label, mapping_label])):
        return {
            "action": "skip",
            "reason": "external application portal required",
            "sensitive": False,
            "blocking": True,
        }
    if _is_email_verification_field(mapping_label):
        # This is filled only after the first final Submit triggers the ATS
        # verification challenge. Its code comes from the configured inbox,
        # never from the candidate profile or answer bank.
        return {"action": "skip", "reason": "email verification handled after submit", "blocking": False}
    # "Start date" is sensitive when it means availability for the new role,
    # but not when it is a dated entry from the candidate's work/education
    # history.  The shared semantic layer distinguishes those forms.
    sensitive = (not profile_date_semantic) and (_is_sensitive(label) or (
        field.get("kind") in {"radiogroup", "buttongroup", "checkboxgroup"} or field.get("tag") == "button"
    ) and _is_sensitive(mapping_label))
    if not required and _is_demographic_label(label):
        demographic_answer = _match_sensitive(answer_label, profile)
        if demographic_answer is None or _is_decline_answer(demographic_answer):
            return {
                "action": "skip",
                "reason": "optional demographic left unselected",
                "sensitive": True,
                "blocking": False,
            }
    priority_answer = _priority_auto_answer(answer_label, profile)
    if priority_answer is None:
        priority_answer = _work_authorization_dropdown_answer(answer_label, profile)
    if _is_listed_country_status_question(answer_label) and priority_answer is None:
        return {
            "action": "skip",
            "reason": "listed-country citizenship/status question needs explicit approved answer",
            "sensitive": True,
            "blocking": bool(required),
        }
    signature_value = _legal_signature_value(mapping_label, profile)
    if signature_value is not None and field.get("tag") in {"input", "textarea"}:
        return {"action": "fill", "value": signature_value, "sensitive": True}
    if field.get("kind") in {"radiogroup", "buttongroup"}:
        answer = priority_answer
        if answer is None:
            answer = _match_sensitive(answer_label, profile) if sensitive else None
        if answer is None and not sensitive and not _use_structured_auto_answer(label):
            answer = _find_answer(label, profile.get("answers") or {})
        if answer is None and not sensitive and not _use_structured_auto_answer(label):
            answer = _map_text_value(field, profile) or _map_text_value(mapping_label, profile)
        if answer is None:
            answer = _auto_answer(answer_label, profile, sensitive=sensitive)
        if answer is None:
            answer = _match_sensitive(answer_label, profile)
        if answer is None:
            generalized = _generalized_screening_answer(field, profile, answer_label, sensitive=sensitive)
            if isinstance(generalized, dict):
                return {
                    "action": "buttonclick" if field.get("kind") == "buttongroup" else "check",
                    "option": generalized,
                }
            if isinstance(generalized, list) and generalized:
                return {
                    "action": "buttonclick" if field.get("kind") == "buttongroup" else "check",
                    "option": generalized[0],
                }
            if isinstance(generalized, str):
                answer = generalized
        if answer is None:
            privacy_option = _privacy_preserving_pronoun_option(field)
            if privacy_option is not None:
                return {
                    "action": "buttonclick" if field.get("kind") == "buttongroup" else "check",
                    "option": privacy_option,
                }
        if answer is None:
            if _requires_user_authored_answer(label, profile):
                return {"action": "skip", "reason": "question requires user-authored answer / no AI assistance", "sensitive": sensitive, "blocking": True}
            if not required:
                return {"action": "skip", "reason": "non-required unmapped field", "sensitive": sensitive, "blocking": False}
            return {"action": "skip", "reason": "no approved answer for screening question", "sensitive": sensitive}
        matches = _matching_options(field, answer)
        if matches:
            return {
                "action": "buttonclick" if field.get("kind") == "buttongroup" else "check",
                "option": matches[0],
            }
        numeric_year_option = _numeric_relevant_year_option(field, profile)
        if numeric_year_option is not None:
            return {
                "action": "buttonclick" if field.get("kind") == "buttongroup" else "check",
                "option": numeric_year_option,
            }
        if (
            _is_relevant_professional_experience_years_question(answer_label)
            and answer
        ):
            return {
                "action": "buttonclick",
                "option": {"label": str(answer), "value": str(answer)},
            }
        if _is_negative_answer(answer) and not required:
            return {
                "action": "skip",
                "reason": "approved No answer has no matching optional option",
                "sensitive": sensitive,
                "blocking": False,
            }
        return {"action": "skip", "reason": "no option matches saved answer", "sensitive": sensitive}
    if field.get("kind") == "checkboxgroup":
        palantir_plan = _palantir_checkbox_group_plan(field, profile)
        if palantir_plan is not None:
            return palantir_plan
        export_control_options = _export_control_checkbox_options(field)
        if export_control_options:
            return {"action": "checkmany", "options": export_control_options}
        technical_options = _technical_screening_checkbox_options(field, profile)
        if technical_options:
            return {"action": "checkmany", "options": technical_options}
        answer = priority_answer
        if answer is None:
            answer = _match_sensitive(mapping_label, profile) if sensitive else None
        if answer is None and not sensitive and not _use_structured_auto_answer(label):
            answer = _find_answer(label, profile.get("answers") or {})
        if answer is None and not sensitive and not _use_structured_auto_answer(label):
            answer = _map_text_value(field, profile) or _map_text_value(mapping_label, profile)
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=sensitive)
        if answer is None:
            generalized = _generalized_screening_answer(field, profile, label, sensitive=sensitive)
            if isinstance(generalized, list) and generalized:
                return {"action": "checkmany", "options": generalized}
            if isinstance(generalized, dict):
                return {"action": "checkmany", "options": [generalized]}
            if isinstance(generalized, str):
                answer = generalized
        if answer is None:
            return {"action": "skip", "reason": "checkbox group needs saved answer / manual selection", "sensitive": sensitive}
        matches = _matching_options(field, answer)
        if matches:
            return {"action": "checkmany", "options": matches}
        if _is_negative_answer(answer) and not required:
            return {
                "action": "skip",
                "reason": "approved No answer has no matching optional checkbox option",
                "sensitive": sensitive,
                "blocking": False,
            }
        return {"action": "skip", "reason": "no checkbox option matches saved answer", "sensitive": sensitive}
    if field.get("type") == "file":
        combined = _norm(" ".join(str(field.get(key) or "") for key in ["label", "id", "name"]))
        cover_letter_path = cover_letter_file or profile.get("cover_letter_file")
        if cover_letter_path and "cover letter" in combined:
            return {"action": "upload", "value": cover_letter_path}
        transcript_path = str(profile.get("_transcript_file") or profile.get("transcript_file") or "").strip()
        if "transcript" in combined:
            if transcript_path and Path(transcript_path).expanduser().is_file():
                return {"action": "upload", "value": transcript_path}
            if not required:
                return {"action": "skip", "reason": "optional transcript file field without configured transcript", "blocking": False}
            return {"action": "skip", "reason": "transcript file required but no transcript configured"}
        if resume_file and (
            "resume" in combined
            or "cv" in combined
            or ("upload a file" in combined and "cover letter" not in combined)
            or ("attachment" in combined and "cover letter" not in combined)
        ):
            return {"action": "upload", "value": resume_file}
        if not required:
            return {"action": "skip", "reason": "optional non-resume file field", "blocking": False}
        return {"action": "skip", "reason": "file field not resume/cv or no resume configured"}
    if field.get("type") == "password":
        application_url = str(profile.get("_application_url") or "").lower()
        password = _candidate_account_password(
            profile,
            create_if_missing=(
                bool(runtime_context and runtime_context.get("candidate_account_creation"))
                or "myworkdayjobs.com" in application_url
                or "workdayjobs.com" in application_url
            ),
        )
        if password:
            return {"action": "fill", "value": password, "sensitive": True}
        return {"action": "skip", "reason": "candidate account creation required", "blocking": True}
    current_value = _field_selected_text(field)
    if current_value and "country phone code" in _norm(label):
        return {"action": "skip", "reason": "field already selected"}
    if field.get("role") == "combobox":
        is_country_control = (
            _norm(label) in {"country", "country name"}
            or _norm(field.get("id")) in {"country", "country name"}
        )
        preferred_office = _preferred_office_location_option(field, profile)
        answer = _infer_country(profile) if is_country_control else priority_answer
        if answer is None and preferred_office is not None:
            answer = _option_text(preferred_office)
        if answer is None:
            answer = _preferred_office_location_answer(field, profile)
        if answer is None and _is_source_question(answer_label):
            answer = _auto_answer(answer_label, profile, sensitive=sensitive)
        if answer is None:
            answer = _match_sensitive(label, profile) if sensitive else _find_answer(label, profile.get("answers") or {})
        if (
            answer is None
            and not sensitive
            and profile.get("location")
            and "location" in _norm(mapping_label)
            and "city" in _norm(mapping_label)
        ):
            answer = profile["location"]
        if answer is None and not sensitive:
            answer = _map_text_value(field, profile) or _map_text_value(mapping_label, profile)
        if (
            answer is None
            and not sensitive
            and profile.get("location")
            and any(re.search(rf"\b{re.escape(token)}\b", _norm(mapping_label)) for token in ["location", "city"])
        ):
            answer = profile["location"]
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=sensitive)
        if answer is None:
            generalized = _generalized_screening_answer(field, profile, label, sensitive=sensitive)
            if isinstance(generalized, dict):
                answer = _option_text(generalized)
            elif isinstance(generalized, list) and generalized:
                answer = _option_text(generalized[0])
            elif isinstance(generalized, str):
                answer = generalized
        single_job_code = _single_job_code_option(field, answer_label)
        if single_job_code is not None:
            return {"action": "combobox", "value": single_job_code}
        current = _field_selected_text(field)
        if current and current != "expanded":
            if answer is None:
                return {"action": "skip", "reason": "combobox already selected"}
            if _selection_matches_answer(current, answer):
                return {"action": "skip", "reason": "combobox already selected"}
            last = _norm(str(answer).split(">")[-1])
            if last and last in current:
                return {"action": "skip", "reason": "combobox already selected"}
        if answer is not None:
            matches = _matching_options(field, answer)
            if matches:
                return {"action": "combobox", "value": _option_text(matches[0])}
            return {"action": "combobox", "value": str(answer)}
        if not required:
            return {
                "action": "skip",
                "reason": "non-required combobox has no approved answer",
                "sensitive": sensitive,
                "blocking": False,
            }
        return {"action": "skip", "reason": "combobox needs saved answer / manual selection", "sensitive": sensitive}
    if field.get("tag") == "button":
        current = _field_selected_text(field)
        if current and current != "select one":
            return {"action": "skip", "reason": "button dropdown already selected"}
        answer = (
            _desired_salary_range_value(profile)
            if "desired annual base salary range" in _norm(label)
            else priority_answer
        )
        if answer is None:
            answer = _match_sensitive(label, profile) if sensitive else _find_answer(label, profile.get("answers") or {})
        if answer is None and not sensitive:
            answer = _map_text_value(field, profile) or _map_text_value(mapping_label, profile)
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=sensitive)
        if answer is None:
            generalized = _generalized_screening_answer(field, profile, label, sensitive=sensitive)
            if isinstance(generalized, dict):
                answer = _option_text(generalized)
            elif isinstance(generalized, list) and generalized:
                answer = _option_text(generalized[0])
            elif isinstance(generalized, str):
                answer = generalized
        if answer is not None:
            return {"action": "customselect", "value": str(answer)}
        if not required:
            return {"action": "skip", "reason": "non-required unmapped field", "blocking": False}
        return {"action": "skip", "reason": "button dropdown needs saved answer / manual selection", "sensitive": sensitive}
    if field.get("type") == "input" and "how did you hear" in _norm(label):
        if current_value:
            return {"action": "skip", "reason": "field already selected"}
        answer = _auto_answer(label, profile, sensitive=sensitive)
        if answer is None:
            answer = _find_answer(label, profile.get("answers") or {})
        if answer is not None:
            return {"action": "combobox", "value": str(answer)}
        return {"action": "skip", "reason": "combobox needs saved answer / manual selection", "sensitive": sensitive}
    if field.get("tag") == "select":
        answer = priority_answer
        if answer is None:
            answer = _match_sensitive(label, profile) if sensitive else _find_answer(label, profile.get("answers") or {})
        if answer is None and not sensitive:
            answer = _map_text_value(field, profile) or _map_text_value(mapping_label, profile)
        degree_level = _degree_field_question(label)
        if degree_level:
            answer = _education_field_for_level(profile, degree_level) or answer
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=sensitive)
        if answer is None:
            generalized = _generalized_screening_answer(field, profile, label, sensitive=sensitive)
            if isinstance(generalized, dict):
                return {"action": "select", "value": generalized}
            if isinstance(generalized, list) and generalized:
                return {"action": "select", "value": generalized[0]}
            if isinstance(generalized, str):
                answer = generalized
        for option in field.get("options") or []:
            if _option_matches(option, answer):
                return {"action": "select", "value": option}
        numeric_year_option = _numeric_relevant_year_option(field, profile)
        if numeric_year_option is not None:
            return {"action": "select", "value": numeric_year_option}
        if degree_level and answer:
            other = next(
                (option for option in field.get("options") or [] if _norm(option) == "other"),
                None,
            )
            if other:
                return {"action": "select", "value": other}
        source_matches = _matching_options(field, answer)
        if source_matches:
            return {"action": "select", "value": source_matches[0]}
        if not required:
            return {"action": "skip", "reason": "non-required unmapped field", "blocking": False}
        return {"action": "skip", "reason": "no matching option / answer", "sensitive": sensitive}
    if field.get("type") == "checkbox":
        technical_plan = _technical_screening_checkbox_plan(field, profile, mapping_label)
        if technical_plan is not None:
            return technical_plan
        office_location_plan = _office_location_checkbox_plan(field, profile)
        if office_location_plan is not None:
            return office_location_plan
        if (
            _is_palantir_profile(profile)
            and "fdse and swe roles" in _norm(mapping_label)
            and "confirm" in _norm(mapping_label)
            and "software engineer" in _norm(profile.get("target_title") or "")
        ):
            return {"action": "check"}
        if "preferred name" in _norm(mapping_label):
            return {"action": "skip", "reason": "preferred name checkbox not needed", "blocking": False}
        if (
            "currently work" in _norm(mapping_label)
            or "current role" in _norm(mapping_label)
            or "current position" in _norm(mapping_label)
        ) and _current_work_value(profile, "current"):
            return {"action": "check"}
        if _is_candidate_account_creation_consent_checkbox(field, runtime_context):
            answer = _match_sensitive("process your personal data", profile)
            if _norm(answer) in {"yes", "true", "1"}:
                return {"action": "check", "sensitive": True}
            if answer is not None and _norm(answer) in {"no", "false", "0"}:
                return {
                    "action": "skip",
                    "reason": "candidate account creation privacy consent declined",
                    "sensitive": True,
                    "blocking": True,
                }
            return {
                "action": "skip",
                "reason": "candidate account creation privacy consent needs approved answer",
                "sensitive": True,
                "blocking": True,
            }
        legal_context_answer = _legal_terms_consent_answer(mapping_label, profile)
        if (
            legal_context_answer is None
            and required
            and _profile_company_slug(profile) == "twilio"
            and _norm(label) == "acknowledge"
        ):
            legal_context_answer = (
                _approved_sensitive_entry_answer(profile, "privacy_consent")
                or _approved_sensitive_entry_answer(profile, "terms_consent")
                or _approved_sensitive_entry_answer(profile, "legal_attestation")
            )
        if legal_context_answer is None and required and _norm(label) in {"i accept", "accept"}:
            legal_context_answer = (
                _approved_sensitive_entry_answer(profile, "privacy_consent")
                or _approved_sensitive_entry_answer(profile, "terms_consent")
                or _approved_sensitive_entry_answer(profile, "legal_attestation")
            )
        if legal_context_answer is not None:
            if _truthy_answer(legal_context_answer):
                if "personal data" in _norm(mapping_label) or "demographic data surveys" in _norm(mapping_label):
                    return {"action": "check", "sensitive": True}
                return {"action": "check"}
            return {
                "action": "skip",
                "reason": "approved legal/consent answer is negative",
                "sensitive": True,
                "blocking": bool(required),
            }
        answer = priority_answer
        if answer is None:
            answer = _match_sensitive(label, profile) if sensitive else _find_answer(label, profile.get("answers") or {})
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=sensitive)
        if _norm(answer) in {"yes", "true", "1"}:
            return {"action": "check"}
        if answer is not None and _norm(answer) in {"no", "false", "0"}:
            if required:
                return {
                    "action": "skip",
                    "reason": "required checkbox conflicts with approved No answer",
                    "sensitive": sensitive,
                    "blocking": True,
                }
            return {"action": "skip", "reason": "approved No answer leaves checkbox unchecked", "sensitive": sensitive, "blocking": False}
        if not required:
            return {"action": "skip", "reason": "non-required unmapped field", "blocking": False}
        reason = "sensitive checkbox needs approved answer" if sensitive else "consent/checkbox needs explicit review"
        return {"action": "skip", "reason": reason, "sensitive": sensitive}
    if field.get("type") == "radio" or field.get("role") in {"radio", "checkbox", "switch"}:
        is_radio_control = field.get("type") == "radio" or field.get("role") == "radio"
        answer = priority_answer
        if answer is None:
            answer = _match_sensitive(mapping_label, profile) if sensitive else _find_answer(label, profile.get("answers") or {})
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=sensitive)
        if answer is not None:
            answer_norm = _norm(answer)
            selected_text = _norm(" ".join(str(part or "") for part in [label, field.get("value")]))
            selectable_option_values = [
                str(part or "")
                for part in [label, field.get("value")]
                if str(part or "").strip()
            ]
            if (
                (not is_radio_control and answer_norm in {"yes", "true", "1"})
                or (
                    is_radio_control
                    and answer_norm in {"yes", "no"}
                    and selected_text.startswith(answer_norm)
                )
                or any(_option_matches(option_value, answer) for option_value in selectable_option_values)
                or any(alias and alias in selected_text for alias in (_norm(alias) for alias in _answer_aliases(answer)))
            ):
                return {"action": "check", "sensitive": sensitive} if sensitive else {"action": "check"}
            if answer_norm in {"no", "false", "0"}:
                return {
                    "action": "skip",
                    "reason": "single selectable answer is negative",
                    "sensitive": sensitive,
                    "blocking": bool(required),
                }
        if not required:
            return {"action": "skip", "reason": "non-required unmapped field", "sensitive": sensitive, "blocking": False}
        return {
            "action": "skip",
            "reason": "single selectable needs saved answer / manual selection",
            "sensitive": sensitive,
        }
    if sensitive:
        answer = priority_answer
        if answer is None and _is_hourly_pay_question(label):
            answer = _auto_answer(label, profile, sensitive=True)
        if answer is None:
            answer = _match_sensitive(label, profile)
        if answer is None:
            answer = _auto_answer(label, profile, sensitive=True)
        if answer is not None:
            return {"action": "fill", "value": answer}
        return {"action": "skip", "reason": "sensitive field needs review", "sensitive": True}
    employee_id_answer = _auto_answer(label, profile)
    if employee_id_answer is not None and "employee id" in _norm(label):
        return {"action": "fill", "value": str(employee_id_answer)}
    if _is_optional_blank_field(label):
        return {"action": "skip", "reason": "optional empty field", "blocking": False}
    if "phone number" in _norm(mapping_label):
        mapped_phone = _map_text_value(field, profile) or _map_text_value(mapping_label, profile)
        if mapped_phone:
            return {"action": "fill", "value": str(mapped_phone)}
    if (
        _is_workday_application_url(profile)
        and _norm(label) in {"field of study", "major"}
    ):
        field_of_study = _current_education_value(profile, "field")
        if field_of_study:
            return {"action": "combobox", "value": str(field_of_study)}
    if (
        ("referred" in _norm(label) and "referring individual" in _norm(label))
        or "relatives currently work" in _norm(label)
        or "relatives currently employed" in _norm(label)
        or ("where did you attend" in _norm(label) and ("undergrad" in _norm(label) or "undergad" in _norm(label)))
        or "undergrad degree" in _norm(label)
        or "undergraduate degree" in _norm(label)
    ):
        auto = _auto_answer(label, profile)
        if auto:
            return {"action": "fill", "value": str(auto)}
    if _prefer_auto_answer_before_identity_mapping(label):
        auto = _auto_answer(label, profile)
        if auto:
            return {"action": "fill", "value": str(auto)}
    if (
        profile.get("location")
        and "current location" in _norm(mapping_label)
        and (_is_palantir_profile(profile) or _norm(profile.get("target_company")) == "weride")
    ):
        location = _expanded_us_location(profile.get("location")) or str(profile["location"])
        return {"action": "combobox", "value": location}
    mapped = _map_text_value(field, profile) or _map_text_value(label, profile) or _map_text_value(mapping_label, profile)
    if mapped:
        return {"action": "fill", "value": str(mapped)}
    answer = _find_answer(label, profile.get("answers") or {})
    if answer is not None:
        return {"action": "fill", "value": str(answer)}
    auto = _auto_answer(label, profile)
    if auto:
        return {"action": "fill", "value": str(auto)}
    if _requires_user_authored_answer(label, profile):
        return {"action": "skip", "reason": "question requires user-authored answer / no AI assistance", "blocking": True}
    if not required:
        return {"action": "skip", "reason": "non-required unmapped field", "blocking": False}
    generalized = _generalized_screening_answer(field, profile, label, sensitive=False)
    if isinstance(generalized, dict):
        return {"action": "fill", "value": _option_text(generalized)}
    if isinstance(generalized, list) and generalized:
        return {"action": "fill", "value": _option_text(generalized[0])}
    if isinstance(generalized, str):
        return {"action": "fill", "value": generalized}
    return {"action": "skip", "reason": "unmapped field"}


def _check_with_fallback(locator) -> bool:
    """Select a native or ARIA checkbox/radio and verify its committed state."""
    try:
        role = str(locator.get_attribute("role") or "").lower()
    except Exception:
        role = ""
    if role in {"checkbox", "radio", "switch"}:
        def selected() -> bool:
            try:
                state = str(locator.get_attribute("aria-checked") or "").lower()
                pressed = str(locator.get_attribute("aria-pressed") or "").lower()
                data_state = str(locator.get_attribute("data-state") or "").lower()
                return state == "true" or pressed == "true" or data_state in {"checked", "on", "selected"}
            except Exception:
                return False

        if not selected():
            locator.click(timeout=3000)
        if not selected():
            raise RuntimeError("ARIA choice did not retain selected state")
        return True
    try:
        locator.check(timeout=3000)
    except Exception:
        locator.evaluate(
            """(node) => {
              node.checked = true;
              node.dispatchEvent(new Event("input", { bubbles: true }));
              node.dispatchEvent(new Event("change", { bubbles: true }));
            }"""
        )
    try:
        return bool(locator.is_checked())
    except Exception:
        return True


def _select_date_from_calendar(page, field: dict[str, Any], target: date) -> str | None:
    """Choose ``target`` from an accessible calendar popup when direct fill fails.

    Calendar libraries differ in markup but consistently expose a visible popup,
    a month navigation control, and day cells.  The browser-side probe finds
    those semantics at runtime and marks exactly one live element for each
    click.  It is deliberately independent of Workday's segmented picker,
    which has a more reliable native path below.
    """
    selector = _selector_for(field)
    if not selector:
        return None
    locator = page.locator(selector).first
    try:
        locator.click(timeout=3000)
    except Exception:
        try:
            locator.evaluate("(node) => { node.focus(); node.click(); }")
        except Exception:
            return None

    context = {
        "id": str(field.get("id") or ""),
        "name": str(field.get("name") or ""),
        "autofillId": str(field.get("autofillId") or ""),
        "target": target.isoformat(),
    }
    for _ in range(30):
        page.wait_for_timeout(140)
        action = page.evaluate(
            """(payload) => {
              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length) &&
                (!window.getComputedStyle || (window.getComputedStyle(node).display !== "none" && window.getComputedStyle(node).visibility !== "hidden")));
              const clearMarkers = () => document.querySelectorAll('[data-job-agent-calendar-action]').forEach((node) =>
                node.removeAttribute('data-job-agent-calendar-action'));
              const norm = (value) => String(value || "").toLowerCase().replace(/\\s+/g, " ").trim();
              const target = new Date(`${payload.target}T12:00:00`);
              const monthNames = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"];
              const byContext = () => {
                if (payload.autofillId) {
                  const node = document.querySelector(`[data-job-agent-autofill-index="${CSS.escape(payload.autofillId)}"]`);
                  if (node) return node;
                }
                if (payload.id) {
                  const node = document.getElementById(payload.id);
                  if (node) return node;
                }
                if (payload.name) return document.querySelector(`[name="${CSS.escape(payload.name)}"]`);
                return document.activeElement;
              };
              const control = byContext();
              const controlledIds = String((control && (control.getAttribute('aria-controls') || control.getAttribute('aria-owns'))) || "")
                .split(/\\s+/).map((id) => id && document.getElementById(id)).filter(Boolean);
              const selectors = [
                '[role="dialog"]', '[role="grid"]', '[role="application"]',
                '[data-calendar]', '[data-datepicker]', '[class*="datepicker" i]', '[class*="date-picker" i]',
                '[class*="calendar" i]', '[id*="datepicker" i]', '[id*="calendar" i]'
              ];
              const roots = Array.from(new Set([
                ...controlledIds,
                ...selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector))),
              ])).filter((node) => visible(node) && node.querySelector &&
                node.querySelector('[role="gridcell"], [data-date], [data-day], button, td'));
              if (!roots.length) return null;
              const root = roots.sort((left, right) => {
                const score = (node) => {
                  let value = 0;
                  if (controlledIds.includes(node)) value += 100;
                  if (node.getAttribute('role') === 'dialog' || node.getAttribute('role') === 'grid') value += 30;
                  if (node.querySelector('[role="gridcell"], [data-date], [data-day]')) value += 20;
                  const rect = node.getBoundingClientRect();
                  if (control && control.getBoundingClientRect) {
                    const controlRect = control.getBoundingClientRect();
                    value -= Math.abs(rect.top - controlRect.bottom) / 1000;
                  }
                  return value;
                };
                return score(right) - score(left);
              })[0];
              const sourceText = (node) => [
                node.getAttribute('data-date'), node.getAttribute('data-value'), node.getAttribute('data-day'),
                node.getAttribute('aria-label'), node.getAttribute('title'), node.textContent,
              ].filter(Boolean).join(' ');
              const currentMonth = () => {
                const candidates = Array.from(root.querySelectorAll(
                  '[role="heading"], [aria-live], [class*="month" i], [class*="header" i], [class*="title" i]'
                ));
                const texts = [...candidates.map((node) => node.textContent || ''), root.textContent || ''];
                for (const text of texts) {
                  const normalized = norm(text);
                  for (let index = 0; index < monthNames.length; index += 1) {
                    const match = normalized.match(new RegExp(`\\b${monthNames[index]}\\b[^0-9]{0,20}(20\\d{2}|19\\d{2})`));
                    if (match) return { month: index, year: Number(match[1]) };
                  }
                }
                return null;
              };
              const current = currentMonth();
              const isTarget = (node) => {
                const raw = sourceText(node);
                const normalized = norm(raw);
                if (raw.includes(payload.target)) return true;
                const dateMatch = raw.match(/(20\\d{2}|19\\d{2})[-/.](\\d{1,2})[-/.](\\d{1,2})/);
                if (dateMatch) {
                  return Number(dateMatch[1]) === target.getFullYear() && Number(dateMatch[2]) === target.getMonth() + 1 && Number(dateMatch[3]) === target.getDate();
                }
                const dateFromLabel = Date.parse(raw);
                if (!Number.isNaN(dateFromLabel)) {
                  const parsed = new Date(dateFromLabel);
                  if (parsed.getFullYear() === target.getFullYear() && parsed.getMonth() === target.getMonth() && parsed.getDate() === target.getDate()) return true;
                }
                return Boolean(current && current.year === target.getFullYear() && current.month === target.getMonth() &&
                  new RegExp(`^${target.getDate()}$`).test(normalized) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
              };
              const dayNodes = Array.from(root.querySelectorAll('[role="gridcell"], [data-date], [data-day], button, td'))
                .filter((node) => visible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
              const day = dayNodes.find(isTarget);
              clearMarkers();
              if (day) {
                day.setAttribute('data-job-agent-calendar-action', 'day');
                return { action: 'day' };
              }
              if (!current) return null;
              const targetMonth = target.getFullYear() * 12 + target.getMonth();
              const currentMonthIndex = current.year * 12 + current.month;
              const direction = targetMonth > currentMonthIndex ? 'next' : targetMonth < currentMonthIndex ? 'previous' : '';
              if (!direction) return null;
              const navigation = Array.from(root.querySelectorAll('button, [role="button"], a'))
                .filter((node) => visible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
                .find((node) => {
                  const text = norm([node.getAttribute('aria-label'), node.getAttribute('title'), node.getAttribute('data-action'), node.className, node.textContent].join(' '));
                  return direction === 'next'
                    ? /\\b(next|forward|right)\\b/.test(text)
                    : /\\b(previous|prev|back|left)\\b/.test(text);
                });
              if (!navigation) return null;
              navigation.setAttribute('data-job-agent-calendar-action', direction);
              return { action: direction };
            }""",
            context,
        )
        if not action:
            return None
        action_name = str(action.get("action") or "")
        if action_name not in {"day", "next", "previous"}:
            return None
        try:
            page.locator(
                _attr_selector("data-job-agent-calendar-action", action_name)
            ).first.click(force=True, timeout=3000)
        except Exception:
            return None
        if action_name == "day":
            page.wait_for_timeout(300)
            readback = _control_readback(locator, field)
            return readback or target.strftime("%m/%d/%Y")
    return None


def _fill_rich_text_value(locator, value: str) -> str:
    """Set a non-native textbox while dispatching framework input events."""
    locator.evaluate(
        """(node, text) => {
          node.focus();
          if (node.isContentEditable) {
            node.textContent = text;
          } else {
            node.textContent = text;
            node.setAttribute('aria-valuetext', text);
          }
          node.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
          node.dispatchEvent(new Event('change', { bubbles: true }));
          node.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        value,
    )
    return str(locator.text_content() or "").strip()


def _is_school_combobox_field(field: dict[str, Any]) -> bool:
    label = _norm(field.get("label") or "")
    if "schoolwork" in label or "school work" in label:
        return False
    return label in {"school", "university", "institution", "college"} or any(
        _has_phrase(label, term) for term in ["school", "university", "institution", "college"]
    )


def _type_into_combobox_search(locator, page, field: dict[str, Any], query: str) -> bool:
    """Type into the real searchable input behind a custom combobox shell.

    Greenhouse education-school controls can expose a non-input combobox shell
    plus a hidden/inner React input.  If we only click the shell, the visible
    menu stays on its initial alphabetic slice (Aalborg, Aalto, ...), so exact
    option matching never sees schools later in the list.
    """
    if not query:
        return False
    if _is_school_combobox_field(field):
        try:
            locator.click(timeout=3000)
            page.wait_for_timeout(250)
            keyboard = getattr(page, "keyboard", None)
            if keyboard and hasattr(keyboard, "press"):
                keyboard.press("Control+A")
                keyboard.press("Backspace")
            if keyboard and hasattr(keyboard, "insert_text"):
                keyboard.insert_text(query)
            elif keyboard and hasattr(keyboard, "type"):
                keyboard.type(query)
            page.wait_for_timeout(900)
            return True
        except Exception:
            pass
    try:
        did_type = locator.evaluate(
            """(node, text) => {
              const visible = (el) => !!(el && (el.offsetParent || el.getClientRects().length));
              const root = node.closest(
                '[data-field-entry-id], .field, .application--form--field, .select, .select__control, .select-container'
              ) || node.parentElement || node;
              const candidates = Array.from(root.querySelectorAll('input:not([type="hidden"]), textarea'))
                .filter((el) => !el.disabled && !el.readOnly && visible(el));
              const input = candidates.find((el) =>
                String(el.getAttribute('role') || '').toLowerCase() === 'combobox' ||
                String(el.getAttribute('aria-autocomplete') || '').toLowerCase() === 'list' ||
                /react-select|select|school|institution|university|college/i.test(
                  [el.id, el.name, el.getAttribute('aria-label'), el.getAttribute('placeholder')].join(' ')
                )
              ) || candidates[0] || (node.matches && node.matches('input:not([type="hidden"]), textarea') ? node : null);
              if (!input) return false;
              input.focus();
              const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(input, '');
              else input.value = '';
              input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
              if (setter) setter.call(input, text);
              else input.value = text;
              input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }""",
            query,
        )
        if did_type:
            page.wait_for_timeout(250)
            try:
                locator.click(timeout=3000)
                page.wait_for_timeout(150)
                keyboard = getattr(page, "keyboard", None)
                if keyboard and hasattr(keyboard, "press"):
                    keyboard.press("Control+A")
                    keyboard.press("Backspace")
                if keyboard and hasattr(keyboard, "insert_text"):
                    keyboard.insert_text(query)
                elif keyboard and hasattr(keyboard, "type"):
                    keyboard.type(query)
            except Exception:
                pass
            page.wait_for_timeout(650)
            return True
    except Exception:
        pass
    try:
        locator.click(timeout=3000)
        keyboard = getattr(page, "keyboard", None)
        if keyboard and hasattr(keyboard, "press"):
            keyboard.press("Control+A")
            keyboard.press("Backspace")
        if keyboard and hasattr(keyboard, "insert_text"):
            keyboard.insert_text(query)
        elif keyboard and hasattr(keyboard, "type"):
            keyboard.type(query)
        page.wait_for_timeout(650)
        return True
    except Exception:
        return False


def _commit_school_combobox_native_value(locator, value: str) -> None:
    if not value:
        return
    try:
        locator.evaluate(
            """(node, text) => {
              const input = (node.matches && node.matches('input:not([type="hidden"]), textarea'))
                ? node
                : node.querySelector && node.querySelector('input:not([type="hidden"]), textarea');
              if (!input) return false;
              const proto = input.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(input, text);
              else input.value = text;
              input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
              input.dispatchEvent(new Event('change', { bubbles: true }));
              input.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
              return true;
            }""",
            value,
        )
    except Exception:
        pass


def _select_greenhouse_school_combobox(page, locator, field: dict[str, Any], answer: str) -> str | None:
    if not answer or not _is_school_combobox_field(field):
        return None
    if "greenhouse.io" not in str(page.url or "").lower():
        return None
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        locator.click(timeout=3000)
        page.wait_for_timeout(300)
        keyboard = getattr(page, "keyboard", None)
        if keyboard and hasattr(keyboard, "press"):
            keyboard.press("Control+A")
            keyboard.press("Backspace")
        if keyboard and hasattr(keyboard, "insert_text"):
            keyboard.insert_text(answer)
        elif keyboard and hasattr(keyboard, "type"):
            keyboard.type(answer)
        page.wait_for_timeout(1000)
        for option_locator in (
            page.get_by_role("option", name=answer, exact=True).first,
            page.get_by_text(answer, exact=True).last,
        ):
            try:
                option_locator.click(timeout=3000)
                page.wait_for_timeout(500)
                _commit_school_combobox_native_value(locator, answer)
                return answer
            except Exception:
                pass
    except Exception:
        return None
    return None


def _commit_workday_combobox_via_select(page, field: dict[str, Any], answer: Any) -> str | None:
    """Set the underlying <select> or hidden input for a Workday combobox.

    Workday's React-driven dropdowns sometimes expose a native ``<select>``
    that is not nested inside the clickable control.  When the visual
    combobox state does not propagate to form state, directly setting the
    underlying control and firing events can commit the value.
    """
    selector = _selector_for(field)
    result = page.evaluate(
        """(payload) => {
          const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9\\s]/g, " ").replace(/\\s+/g, " ").trim();
          const labelText = norm(payload.label);
          const want = norm(payload.answer);
          let roots = [];
          if (payload.selector) {
            const control = document.querySelector(payload.selector);
            if (control) {
              const field = control.closest('[data-automation-id^="formField-"]');
              if (field) roots.push(field);
            }
          }
          if (!roots.length) {
            roots = Array.from(document.querySelectorAll('[data-automation-id^="formField-"]'));
          }
          for (const root of roots) {
            if (labelText && !norm(root.textContent || "").includes(labelText)) continue;
            for (const select of root.querySelectorAll('select')) {
              let bestOption = null;
              for (const option of select.options) {
                const optText = norm(option.text);
                const optVal = norm(option.value);
                if (optText === want || optVal === want) {
                  bestOption = option;
                  break;
                }
                if (!bestOption && (optText.startsWith(want + " ") || optText.startsWith(want + ","))) {
                  bestOption = option;
                }
                if (!bestOption && want.length > 1 && (optText.includes(want) || optVal.includes(want))) {
                  bestOption = option;
                }
              }
              if (bestOption) {
                select.value = bestOption.value;
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('change', { bubbles: true }));
                select.dispatchEvent(new Event('blur', { bubbles: true }));
                return { success: true, option: bestOption.text };
              }
            }
            const hidden = root.querySelector('input[type="hidden"]');
            if (hidden) {
              hidden.value = payload.answer;
              hidden.dispatchEvent(new Event('input', { bubbles: true }));
              hidden.dispatchEvent(new Event('change', { bubbles: true }));
              return { success: true, option: payload.answer };
            }
          }
          return { success: false };
        }""",
        {"label": field.get("label") or "", "answer": str(answer or ""), "selector": selector},
    )
    if isinstance(result, dict) and result.get("success"):
        return str(result.get("option") or answer)
    return None


def _click_workday_menu_item_js(page, field: dict[str, Any], answer: Any) -> str | None:
    """Open a Workday combobox and click the matching option via JavaScript.

    Some Workday simple dropdowns render their options inside the formField
    container but Playwright's visible-text locators miss them (e.g. because
    the popover is detached, there are duplicate controls, or the click target
    is a child element).  A scoped JavaScript click within the same formField
    avoids cross-field ambiguity.
    """
    selector = _selector_for(field)
    if not selector:
        return None
    wants = [_norm(alias) for alias in _answer_aliases(answer) if _norm(alias)]
    if not wants:
        wants = [_norm(str(answer or ""))]
    # Prefer longer, more specific aliases (e.g. "United States of America")
    # over shorter ones ("United States") so they do not accidentally match
    # sub-regions like "United States Minor Outlying Islands".
    wants = sorted(set(wants), key=lambda w: -len(w))
    result = page.evaluate(
        """async (payload) => {
          const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9\\s]/g, " ").replace(/\\s+/g, " ").trim();
          const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
          const wants = (payload.wants || [norm(payload.answer)]).filter(Boolean);
          const control = document.querySelector(payload.selector);
          if (!control) return { success: false, reason: "control not found" };
          control.focus();
          if (control.getAttribute('aria-expanded') !== 'true') control.click();
          await sleep(150);
          let items = [];
          // Workday renders dropdown popovers as body-level portals linked by
          // aria-controls/aria-owns. Prefer those over the formField container.
          const popoverIds = String(control.getAttribute('aria-controls') || control.getAttribute('aria-owns') || '')
            .split(/\\s+/)
            .filter(Boolean);
          for (const id of popoverIds) {
            const popover = document.getElementById(id);
            if (popover) {
              items.push(...Array.from(popover.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="radioBtn"]')));
            }
          }
          // Fallback to the formField container when no popover is linked.
          if (!items.length) {
            const field = control.closest('[data-automation-id^="formField-"]');
            if (field) {
              items.push(...Array.from(field.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="radioBtn"]')));
            }
          }
          // Final fallback: scan globally and keep items whose text contains the
          // field label, to avoid clicking options belonging to other controls.
          if (!items.length) {
            const fieldLabel = norm(payload.label);
            items = Array.from(document.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="radioBtn"]'))
              .filter((node) => !fieldLabel || norm(node.textContent || "").includes(fieldLabel) || norm(node.getAttribute('aria-label') || "").includes(fieldLabel));
          }
          // Workday often renders the currently open menu as an unlabelled
          // body-level portal. If there is no aria-controls/field-scoped
          // linkage, fall back to visible global menu rows and rely on exact
          // option-text matching below.
          if (!items.length) {
            items = Array.from(document.querySelectorAll('[data-automation-id="menuItem"], [role="option"], [data-automation-id="radioBtn"]'))
              .filter((node) => node.offsetParent || node.getClientRects().length);
          }
          const nodeText = (node) => norm(node.textContent || node.getAttribute('aria-label') || "");
          for (const want of wants) {
            let item = items.find((node) => nodeText(node) === want);
            if (!item) {
              item = items.find((node) => {
                const text = nodeText(node);
                return text.startsWith(want + " ") || text.startsWith(want + ",");
              });
            }
            if (!item && want.length > 1) {
              item = items.find((node) => nodeText(node).includes(want));
            }
            if (item) {
              item.focus();
              item.click();
              await sleep(50);
              if (control.getAttribute('aria-expanded') === 'true') {
                control.click();
                await sleep(100);
              }
              control.dispatchEvent(new Event('change', { bubbles: true }));
              control.dispatchEvent(new Event('blur', { bubbles: true }));
              return { success: true, option: (item.textContent || "").replace(/\\s+/g, " ").trim() };
            }
          }
          const sourceQuestion = /\\b(how|where)\\b.*\\b(hear|heard)\\b/.test(norm(payload.label));
          if (sourceQuestion) {
            const usefulItems = items.filter((node) => {
              const text = nodeText(node);
              if (!text || text === "select one") return false;
              if (/\\+\\d+/.test(text)) return false;
              if (["united states", "united states of america", "canada", "india", "china"].includes(text)) return false;
              return true;
            });
            const unique = [];
            const seen = new Set();
            for (const node of usefulItems) {
              const text = nodeText(node);
              if (seen.has(text)) continue;
              seen.add(text);
              unique.push(node);
            }
            if (unique.length === 1) {
              const item = unique[0];
              item.focus();
              item.click();
              await sleep(50);
              if (control.getAttribute('aria-expanded') === 'true') {
                control.click();
                await sleep(100);
              }
              control.dispatchEvent(new Event('change', { bubbles: true }));
              control.dispatchEvent(new Event('blur', { bubbles: true }));
              return { success: true, option: (item.textContent || "").replace(/\\s+/g, " ").trim(), fallback: "single-source-option" };
            }
          }
          const available = Array.from(new Set(items.map((n) => (n.textContent || "").replace(/\\s+/g, " ").trim()))).filter(Boolean).slice(0, 10);
          return { success: false, reason: "no matching option", want: wants[0], available };
        }""",
        {"selector": selector, "answer": str(answer or ""), "wants": wants, "label": field.get("label") or ""},
    )
    if isinstance(result, dict) and result.get("success"):
        return str(result.get("option") or answer)
    if isinstance(result, dict):
        print(
            f"[_click_workday_menu_item_js] failed for {field.get('label')}: "
            f"{result.get('reason')} answer={answer!r} want={str(result.get('want') or '')!r} available={result.get('available')}",
            file=sys.stderr,
        )
    return None


def _lever_current_location_field(field: dict[str, Any], page_url: str) -> bool:
    if "jobs.lever.co" not in str(page_url or "").lower():
        return False
    label = _norm(field.get("label") or "")
    field_id = _norm(field.get("id") or "")
    field_name = _norm(field.get("name") or "")
    return label == "current location" or field_id == "location input" or field_name == "location"


def _lever_location_candidates(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    candidates = [raw]
    expanded_parts = [
        _expanded_location_text(part).title()
        for part in raw.split(",")
        if _expanded_location_text(part)
    ]
    if expanded_parts:
        candidates.append(", ".join(expanded_parts))
    city = raw.split(",", 1)[0].strip()
    if city:
        candidates.append(city)
    deduped: list[str] = []
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _select_lever_current_location(page, field: dict[str, Any], value: str) -> str | None:
    if not value or not _lever_current_location_field(field, str(page.url or "")):
        return None
    selector = _selector_for(field) or "#location-input, input[name='location']"
    candidates = _lever_location_candidates(value)
    if not candidates:
        return None
    locator = page.locator(selector).first
    for candidate in candidates:
        try:
            locator.click(timeout=3000)
            locator.fill(candidate)
            page.wait_for_timeout(800)
        except Exception:
            continue
        option_texts = [candidate, *candidates]
        for option_text in option_texts:
            try:
                page.get_by_text(option_text, exact=True).last.click(timeout=2500)
                page.wait_for_timeout(400)
                readback = _control_readback(locator, field)
                if readback and "no location found" not in _norm(readback):
                    return readback
            except Exception:
                pass
            try:
                page.locator(
                    "[role='option'], li, .pac-item, .location-search-results li, .autocomplete-result"
                ).filter(has_text=option_text).first.click(timeout=2500)
                page.wait_for_timeout(400)
                readback = _control_readback(locator, field)
                if readback and "no location found" not in _norm(readback):
                    return readback
            except Exception:
                pass
        try:
            locator.press("Enter")
            page.wait_for_timeout(400)
            readback = _control_readback(locator, field)
            if readback and "no location found" not in _norm(readback):
                return readback
        except Exception:
            pass
    return _commit_lever_current_location(page, field, candidates[0])


def _commit_lever_current_location(page, field: dict[str, Any], value: str) -> str | None:
    if not value or not _lever_current_location_field(field, str(page.url or "")):
        return None
    try:
        result = page.evaluate(
            """(payload) => {
              const byAutofill = payload.autofillId
                ? document.querySelector(`[data-job-agent-autofill-index="${payload.autofillId}"]`)
                : null;
              const input = byAutofill || document.querySelector('#location-input, input[name="location"]');
              const hidden = document.querySelector('#selected-location, input[name="selectedLocation"]');
              const setQuietValue = (node, val) => {
                if (!node) return false;
                const proto = node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(node, val);
                else node.value = val;
                node.setAttribute('value', val);
                return true;
              };
              setQuietValue(input, payload.value);
              setQuietValue(hidden, payload.value);
              return {
                inputValue: input ? input.value : '',
                hiddenValue: hidden ? hidden.value : '',
                inputValid: input && typeof input.checkValidity === 'function' ? input.checkValidity() : Boolean(input && input.value),
              };
            }""",
            {
                "autofillId": str(field.get("autofillId") or ""),
                "value": value,
            },
        )
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    input_value = str(result.get("inputValue") or "").strip()
    hidden_value = str(result.get("hiddenValue") or "").strip()
    if result.get("inputValid") and input_value and _norm(input_value) == _norm(value):
        return input_value
    if input_value and hidden_value and _norm(input_value) == _norm(hidden_value) == _norm(value):
        return input_value
    return None


def _apply_fill(page, field: dict[str, Any], plan: dict[str, Any]) -> Any:
    required = bool(field.get("required") or _field_label_appears_required(field.get("label") or ""))
    if plan["action"] == "check" and plan.get("option"):
        option = plan["option"]
        selector = (
            _attr_selector("data-job-agent-autofill-index", option["autofillId"])
            if option.get("autofillId")
            else _attr_selector("id", option["id"])
        )
        locator = page.locator(selector).first
        if _check_with_fallback(locator):
            selected = option.get("label") or option.get("value") or "selected"
            return f"selected: {selected}"
        return False
    if plan["action"] == "buttonclick" and plan.get("option"):
        option = plan["option"]
        ashby_readback = _click_ashby_button_group(page, field, option)
        if ashby_readback is not None:
            return ashby_readback
        if not option.get("autofillId"):
            return False
        selector = _attr_selector("data-job-agent-autofill-index", option["autofillId"])
        locator = page.locator(selector).first
        locator.click()
        if not _locator_is_ashby_yes_no_option(locator):
            hidden = locator.locator("xpath=..").locator("input[type='checkbox']").first
            if hidden.count():
                desired = _norm(option.get("label") or option.get("value")) == "yes"
                if hidden.is_checked() == desired:
                    hidden.dispatch_event("click")
                hidden.dispatch_event("click")
                if hidden.is_checked() != desired:
                    raise RuntimeError("hidden Yes/No input state did not update")
        selected = option.get("label") or option.get("value") or "selected"
        return f"selected: {selected}"
    if plan["action"] == "checkmany":
        checked: list[str] = []
        for option in plan.get("options") or []:
            selector = _attr_selector("data-job-agent-autofill-index", option["autofillId"])
            locator = page.locator(selector).first
            if _check_with_fallback(locator):
                checked.append(str(option.get("label") or option.get("value") or "selected"))
        return "selected: " + ", ".join(checked) if checked else None
    selector = _selector_for(field)
    if not selector:
        raise RuntimeError("no selector")
    locator = page.locator(selector).first
    if plan["action"] == "fill":
        segmented_readback = _fill_workday_date_section(
            page,
            str(field.get("id") or ""),
            plan.get("value"),
        )
        if segmented_readback is not None:
            return segmented_readback
        locator = _recover_text_fill_locator(page, field, locator)
        fill_value = _normalize_date_input_value(
            plan["value"],
            locator.get_attribute("placeholder") or "",
            input_type=str(field.get("type") or ""),
        )
        fill_value = _normalize_number_input_value(
            fill_value,
            field,
            input_type=str(locator.get_attribute("type") or field.get("type") or ""),
        )
        lever_location = _select_lever_current_location(page, field, str(fill_value or ""))
        if lever_location:
            return lever_location
        target_date = _date_target(plan.get("value")) if _is_date_like_field(field, locator) else None
        fill_error: Exception | None = None
        try:
            locator.fill(fill_value)
            try:
                locator.press("Tab")
            except Exception:
                pass
            readback = _control_readback(locator, field)
        except Exception as exc:
            fill_error = exc
            readback = ""
        if readback and (
            target_date is None
            or _readback_matches_date(readback, target_date)
            # A locale-specific date string can still represent a committed
            # value even when the generic parser cannot read it. The browser
            # validation audit remains the final authority in that case.
            or _date_target(readback) is None
        ):
            return readback
        if target_date:
            calendar_readback = _select_date_from_calendar(page, field, target_date)
            if calendar_readback:
                return calendar_readback
        if not readback and fill_value:
            try:
                locator.click()
                locator.press_sequentially(fill_value, delay=10)
                try:
                    locator.press("Tab")
                except Exception:
                    pass
                readback = _control_readback(locator, field)
            except Exception:
                readback = ""
        if not readback and fill_value and (
            field.get("contentEditable")
            or _norm(field.get("type")) == "contenteditable"
            or _norm(field.get("role")) == "textbox"
        ):
            try:
                readback = _fill_rich_text_value(locator, fill_value)
            except Exception:
                readback = ""
        if not readback and fill_value:
            detail = f": {fill_error}" if fill_error else ""
            raise RuntimeError(f"fill readback empty after setting non-empty value{detail}")
        return readback
    if plan["action"] == "select":
        locator.select_option(label=plan["value"])
        return locator.input_value()
    if plan["action"] == "upload":
        locator.set_input_files(plan["value"])
        return "file-selected"
    if plan["action"] == "check":
        return _check_with_fallback(locator)
    if plan["action"] == "combobox":
        progress_deadline = _new_combobox_progress_deadline()

        def _guard_combobox_progress() -> None:
            _check_combobox_progress_deadline(progress_deadline, field)

        answer = str(plan.get("value") or "")
        if (
            _requires_strict_combobox_commit_readback(field)
            or _is_phone_country_control(page, field)
        ):
            try:
                verified = _verify_control_selection(page, field, answer)
            except Exception:
                verified = None
            if verified:
                return verified

        # intl-tel-input country selectors appear as custom comboboxes but are
        # not native selects or React selects; set the country via the widget
        # API or dropdown item before trying generic paths.
        intl_country = _select_intl_tel_input_country(page, locator, answer)
        if intl_country:
            verified = _verify_control_selection(page, field, str(plan.get("value") or ""))
            if verified:
                return verified
            return intl_country
        _guard_combobox_progress()
        # Some forms style a native <select> as a combobox; selecting by label
        # is far more reliable than typing into it.
        try:
            tag_name = str(locator.evaluate("el => el.tagName.toLowerCase()") or "").lower()
        except Exception:
            tag_name = ""
        if tag_name == "select":
            try:
                locator.select_option(label=plan["value"])
                return locator.input_value()
            except Exception:
                pass
        _guard_combobox_progress()
        # Workday/Greenhouse occasionally wrap a real <select> inside a
        # clickable combobox shell. Try the inner select when the shell is not
        # itself a select, including the Workday formField container. Labels on
        # Greenhouse selects often include extra text (e.g. "United States +1"
        # or "No, I do not have a disability..."), so fall back to fuzzy label
        # and value matching.
        def _try_select_option(select_locator) -> bool:
            if not select_locator.count():
                return False
            answer = str(plan.get("value") or "")
            wants = [_norm(alias) for alias in _answer_aliases(answer) if _norm(alias)]
            field_label = _norm(field.get("label") or "")
            try:
                options = select_locator.evaluate(
                    """(sel) => Array.from(sel.options).map((o) => ({
                        label: (o.label || "").trim(),
                        value: (o.value || "").trim(),
                        text: (o.text || "").trim(),
                    }))"""
                )
            except Exception:
                options = []
            best_option = None
            best_score = 0
            for option in options:
                option_label = str(option.get("label") or option.get("text") or "")
                option_value = str(option.get("value") or "")
                option_text = _norm(f"{option_label} {option_value}").strip()
                if not option_text:
                    continue
                for want in wants:
                    if not want:
                        continue
                    score = _option_match_score(option_text, want)
                    if score > best_score:
                        best_score = score
                        best_option = option
            try:
                if best_option and best_score >= 30:
                    label = best_option.get("label") or best_option.get("text")
                    value = best_option.get("value")
                    if value:
                        try:
                            select_locator.select_option(value=value)
                            return True
                        except Exception:
                            pass
                    select_locator.select_option(label=label)
                    return True
            except Exception:
                pass
            try:
                select_locator.select_option(label=answer)
                return True
            except Exception:
                pass
            return False

        inner_select = locator.locator("select").first
        if _try_select_option(inner_select):
            return inner_select.input_value()
        _guard_combobox_progress()
        try:
            form_field_select = locator.locator(
                'xpath=ancestor::*[starts-with(@data-automation-id,"formField-")][1]//select'
            ).first
            if _try_select_option(form_field_select):
                return form_field_select.input_value()
        except Exception:
            pass
        _guard_combobox_progress()
        lever_location = _commit_lever_current_location(page, field, str(plan.get("value") or ""))
        if lever_location:
            return lever_location
        _guard_combobox_progress()
        steps = [step.strip() for step in str(plan.get("value") or "").split(">") if step.strip()]
        supports_text_entry = field.get("tag") in {"input", "textarea"} or bool(field.get("contentEditable"))
        verified_selection: str | None = None
        if len(steps) == 1:
            greenhouse_react_selection = _select_greenhouse_react_combobox_option(
                page,
                locator,
                field,
                steps[0],
            )
            if greenhouse_react_selection:
                return greenhouse_react_selection
            _guard_combobox_progress()
        if len(steps) == 1 and _is_school_combobox_field(field):
            direct_school = _select_greenhouse_school_combobox(page, locator, field, steps[0])
            if direct_school:
                return direct_school
            _guard_combobox_progress()
        for index, step in enumerate(steps):
            _guard_combobox_progress()
            field_label = _norm(field.get("label") or "")
            country_like_field = (
                str(field.get("id") or "").lower() == "country"
                or field_label.startswith("country")
                or "country phone code" in field_label
                or "phone country code" in field_label
            )
            search_step = (
                step.split(",", 1)[0].strip() or step
                if "location" in field_label or "city" in field_label
                else step
            )
            school_like_field = _is_school_combobox_field(field)
            clicked = None
            for attempt in range(3):
                _guard_combobox_progress()
                if clicked:
                    break
                if index == 0 or attempt > 0:
                    try:
                        locator.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    try:
                        locator.click(timeout=3000)
                    except Exception:
                        pass
                    page.wait_for_timeout(150 + attempt * 250)
                    try:
                        clicked = _choose_dropdown_option(page, step, field)
                    except RuntimeError:
                        clicked = None
                    _guard_combobox_progress()
                    if clicked:
                        break
                    if str(field.get("id") or "") == "country" or (
                        _norm(field.get("label") or "").startswith("country") and "+" in step
                    ):
                        try:
                            page.get_by_role("option", name=step, exact=True).first.click(timeout=3000)
                            clicked = step
                        except Exception:
                            clicked = None
                        if clicked:
                            break
                    if _norm(step) in {"yes", "no"}:
                        try:
                            page.get_by_role("option", name=step, exact=True).first.click(timeout=3000)
                            clicked = step
                        except Exception:
                            clicked = None
                        if clicked:
                            break
                    if _norm(step) in {"i don t wish to answer", "i don't wish to answer"}:
                        try:
                            page.get_by_role("option", name="I don't wish to answer", exact=True).first.click(timeout=3000)
                            clicked = "I don't wish to answer"
                        except Exception:
                            clicked = None
                        if clicked:
                            break
                    try:
                        page.get_by_role("option", name=step, exact=True).first.click(timeout=3000)
                        clicked = step
                    except Exception:
                        clicked = None
                    if clicked:
                        break
                    if school_like_field:
                        _type_into_combobox_search(locator, page, field, search_step)
                        try:
                            page.get_by_role("option", name=step, exact=True).first.click(timeout=3000)
                            clicked = step
                        except Exception:
                            clicked = None
                        if clicked:
                            break
                        try:
                            clicked = _choose_dropdown_option(page, step, field)
                        except RuntimeError:
                            clicked = None
                        _guard_combobox_progress()
                        if clicked:
                            break
                        try:
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(450)
                            verified_school = _verify_control_selection(page, field, step)
                            if verified_school:
                                clicked = verified_school
                        except Exception:
                            clicked = None
                        if clicked:
                            break
                    if supports_text_entry:
                        try:
                            locator.fill(search_step)
                        except Exception:
                            pass
                        page.wait_for_timeout(350 + attempt * 350)
                        try:
                            page.get_by_role("option", name=step, exact=True).first.click(timeout=3000)
                            clicked = step
                        except Exception:
                            clicked = None
                        if clicked:
                            break
                        try:
                            clicked = _choose_dropdown_option(page, step, field)
                        except RuntimeError:
                            clicked = None
                        _guard_combobox_progress()
                        if clicked:
                            break
                        # Native autocomplete controls can commit the active
                        # option with Enter. React Select clears an uncommitted
                        # query on Enter, so only use this after a visible
                        # option click was unavailable.
                        if not country_like_field:
                            try:
                                page.keyboard.press("Enter")
                            except Exception:
                                pass
                            page.wait_for_timeout(250)
                            try:
                                verified_after_enter = _verify_control_selection(page, field, step)
                            except Exception:
                                verified_after_enter = None
                            if verified_after_enter:
                                clicked = verified_after_enter
                                break
                    else:
                        try:
                            clicked = _choose_dropdown_option(page, step, field)
                        except RuntimeError:
                            clicked = None
                        _guard_combobox_progress()
                        if clicked:
                            break
                        keyboard = getattr(page, "keyboard", None)
                        try:
                            if keyboard and hasattr(keyboard, "insert_text"):
                                keyboard.insert_text(search_step)
                            elif keyboard and hasattr(keyboard, "type"):
                                keyboard.type(search_step)
                        except Exception:
                            pass
                        page.wait_for_timeout(350 + attempt * 350)
                if not clicked:
                    try:
                        clicked = _choose_dropdown_option(page, step, field)
                    except RuntimeError:
                        clicked = None
                    _guard_combobox_progress()
            if not clicked:
                _guard_combobox_progress()
                if len(steps) == 1:
                    try:
                        current = _control_readback(locator, field)
                    except Exception:
                        current = ""
                    if (
                        "location" not in field_label
                        and "city" not in field_label
                        and _norm(current)
                        and (_norm(current) in _norm(step) or _norm(step) in _norm(current))
                    ):
                        verified = _verify_control_selection(page, field, step)
                        if verified:
                            return verified
                try:
                    locator.click(timeout=3000)
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                available = page.evaluate(
                    """(context) => {
                      const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                      const text = (node) => String((node && node.textContent) || "").replace(/\\s+/g, " ").trim();
                      const control = context.autofillId
                        ? document.querySelector(`[data-job-agent-autofill-index="${context.autofillId}"]`)
                        : (context.id ? document.getElementById(context.id) : document.activeElement);
                      const controlled = [
                        context.ariaControls,
                        context.ariaOwns,
                        control && control.getAttribute("aria-controls"),
                        control && control.getAttribute("aria-owns"),
                      ].filter(Boolean).join(" ")
                        .split(/\\s+/)
                        .map((id) => id && document.getElementById(id))
                        .filter(Boolean);
                      const popups = Array.from(new Set([
                        ...controlled,
                        ...Array.from(document.querySelectorAll('[role="listbox"], [role="menu"], [data-automation-id="activeListContainer"], [data-popper-placement], [data-radix-popper-content-wrapper], [data-headlessui-state~="open"], [class*="select__menu"], [class*="select__menu-list"], [class*="select__value-container"], [class*="-menu"], [class*="-dropdown"], [class*="dropdown-"], [id*="downshift"], [id*="-menu"], [class*="gph-select"]')),
                      ])).filter(visible);
                      const optionSelector = 'div, li, [data-automation-id="menuItem"], [role="option"], [role="menuitem"], [role="menuitemradio"], [data-automation-id="radioBtn"], [data-option-value], [data-value], [class*="select__option"], [class*="-option"], [aria-selected], [id*="-item-"]';
                      // Leaf-node check must not include bare div/li/data-value/aria-selected — those
                      // catch decorative children (e.g. flag icons) and incorrectly exclude option elements.
                      const leafExcludeSelector = '[data-automation-id="menuItem"], [role="option"], [role="menuitem"], [role="menuitemradio"], [data-automation-id="radioBtn"], [data-option-value], [class*="select__option"], [class*="-option"], [id*="-item-"]';
                      const nodes = popups.flatMap((root) => [
                        ...(root.matches && root.matches(optionSelector) ? [root] : []),
                        ...Array.from(root.querySelectorAll(optionSelector)),
                      ]);
                      const leafNodes = nodes.filter(node => !node.querySelector(leafExcludeSelector));
                      return Array.from(new Set(leafNodes.filter(visible).map(text).filter(Boolean))).slice(0, 100);
                    }""",
                    {
                        "id": str(field.get("id") or ""),
                        "autofillId": str(field.get("autofillId") or ""),
                        "ariaControls": str(field.get("ariaControls") or ""),
                        "ariaOwns": str(field.get("ariaOwns") or ""),
                    },
                )
                _guard_combobox_progress()
                fallback_choice = _office_location_combobox_fallback_choice(
                    field,
                    available,
                    step,
                )
                if not fallback_choice:
                    fallback_choice = next((text for text in available if len(text) <= 80 and _option_matches(text, step)), None)
                normalized_field_label = _norm(field.get("label") or "")
                if not fallback_choice and "ever worked for" in normalized_field_label:
                    fallback_choice = next((text for text in available if "never worked" in _norm(text)), None)
                if not fallback_choice and "job code" in normalized_field_label and "posting" in normalized_field_label:
                    code_available = [text for text in available if _looks_like_job_code_option(text)]
                    if len(code_available) == 1:
                        fallback_choice = code_available[0]
                if fallback_choice:
                    for option_locator in (
                        page.get_by_role("option", name=fallback_choice, exact=True).first,
                        page.locator('[data-automation-id="menuItem"]').filter(has_text=fallback_choice).last,
                        page.get_by_text(fallback_choice, exact=True).last,
                        page.locator('[class*="select__option"]').filter(has_text=fallback_choice).last,
                        page.locator('[role="option"]').filter(has_text=fallback_choice).last,
                    ):
                        _guard_combobox_progress()
                        try:
                            option_locator.click(timeout=3000)
                            page.wait_for_timeout(500)
                            if school_like_field:
                                _commit_school_combobox_native_value(locator, fallback_choice)
                            verified_fallback = _verify_control_selection(
                                page,
                                field,
                                step,
                            )
                            if verified_fallback:
                                return verified_fallback
                        except Exception:
                            pass
                if fallback_choice:
                    # Last-resort JS click: some Greenhouse React dropdowns
                    # render options as plain divs without role="option".
                    try:
                        page.evaluate(
                            """(text) => {
                              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                              const popups = Array.from(document.querySelectorAll(
                                '[role="listbox"], [role="menu"], [class*="select__menu"], [class*="-menu"], [class*="-dropdown"], [class*="dropdown-"], [data-popper-placement], [data-radix-popper-content-wrapper], [id*="downshift"], [id*="-menu"], [class*="gph-select"]'
                              )).filter(visible);
                              for (const popup of popups) {
                                const options = Array.from(popup.querySelectorAll(
                                  '[role="option"], [role="menuitem"], [class*="select__option"], [class*="-option"], li, div, [id*="-item-"]'
                                )).filter(visible);
                                for (const opt of options) {
                                  if (String(opt.textContent || "").replace(/\s+/g, " ").trim() === text) {
                                    opt.click();
                                    return true;
                                  }
                                }
                              }
                              return false;
                            }""",
                            fallback_choice,
                        )
                        page.wait_for_timeout(500)
                        if school_like_field:
                            _commit_school_combobox_native_value(locator, fallback_choice)
                        verified_after_js = _verify_control_selection(page, field, step)
                        if verified_after_js:
                            return verified_after_js
                    except Exception:
                        pass
                    _guard_combobox_progress()
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                # Last-resort Workday fallbacks: some React dropdowns only
                # commit when the underlying <select> is set or when the option
                # is clicked via a scoped JavaScript call.
                if "myworkdayjobs" in str(page.url or "").lower():
                    _guard_combobox_progress()
                    try:
                        locator.click(timeout=3000)
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    for fallback_fn in (_click_workday_menu_item_js, _commit_workday_combobox_via_select):
                        _guard_combobox_progress()
                        try:
                            fallback_value = fallback_fn(page, field, step)
                            if fallback_value:
                                try:
                                    box = locator.bounding_box()
                                    if box:
                                        viewport = page.viewport_size or {}
                                        width = int(viewport.get("width") or 1280)
                                        page.mouse.click(
                                            min(width - 5, float(box.get("x") or 0) + float(box.get("width") or 0) + 160),
                                            max(5, float(box.get("y") or 0) + 12),
                                        )
                                        page.wait_for_timeout(150)
                                    page.keyboard.press("Escape")
                                    page.wait_for_timeout(250)
                                except Exception:
                                    pass
                                return fallback_value
                        except Exception:
                            pass
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                # Last-resort fallbacks for portal-rendered dropdowns
                # (Greenhouse React country comboboxes often render options in a
                # portal that JS-based popup detection cannot find, and as plain
                # divs without role="option").
                #
                # Strategy 1: use aria-controls to locate the connected menu
                # and click an item whose text matches.
                if step:
                    _guard_combobox_progress()
                    try:
                        menu_id = page.evaluate(
                            """(ctx) => {
                              const el = ctx.autofillId
                                ? document.querySelector('[data-job-agent-autofill-index="' + ctx.autofillId + '"]')
                                : (ctx.id ? document.getElementById(ctx.id) : document.activeElement);
                              if (!el) return null;
                              return el.getAttribute('aria-controls')
                                  || el.getAttribute('aria-owns')
                                  || el.getAttribute('data-downshift-id');
                            }""",
                            {"autofillId": str(field.get("autofillId") or ""), "id": str(field.get("id") or "")},
                        )
                        if menu_id:
                            menu = page.locator(f"#{menu_id}")
                            if menu.count():
                                items = menu.locator("li, div, [role='option'], [role='menuitem']")
                                count = items.count()
                                for i in range(min(count, 200)):
                                    _guard_combobox_progress()
                                    item = items.nth(i)
                                    if not item.is_visible():
                                        continue
                                    item_text = (item.text_content() or "").strip()
                                    if _option_matches(item_text, step):
                                        item.click(timeout=3000)
                                        page.wait_for_timeout(500)
                                        verified = _verify_control_selection(page, field, step)
                                        if verified:
                                            return verified
                    except Exception:
                        pass
                # Strategy 2: Greenhouse downshift item pattern.
                if step and "greenhouse" in str(getattr(page, "url", "") or "").lower():
                    _guard_combobox_progress()
                    try:
                        items = page.locator('[id*="downshift-"][id*="-item-"]')
                        count = items.count()
                        for i in range(min(count, 200)):
                            _guard_combobox_progress()
                            item = items.nth(i)
                            if not item.is_visible():
                                continue
                            item_text = (item.text_content() or "").strip()
                            if _option_matches(item_text, step):
                                item.click(timeout=3000)
                                page.wait_for_timeout(500)
                                verified = _verify_control_selection(page, field, step)
                                if verified:
                                    return verified
                    except Exception:
                        pass
                # Strategy 3: keyboard-driven selection for React Select /
                # Greenhouse React comboboxes. Type to filter, ArrowDown to
                # highlight the first match, Enter to commit.
                if country_like_field:
                    _guard_combobox_progress()
                    # Strategy 3a: native <select> element (many Greenhouse
                    # country fields are styled <select> elements).
                    try:
                        locator.select_option(label=step)
                        page.wait_for_timeout(400)
                        verified = _verify_control_selection(page, field, step)
                        if verified:
                            return verified
                    except Exception:
                        pass
                    try:
                        locator.select_option(step)
                        page.wait_for_timeout(400)
                        verified = _verify_control_selection(page, field, step)
                        if verified:
                            return verified
                    except Exception:
                        pass
                    # Strategy 3b: React Select / Greenhouse combobox
                    # keyboard-driven selection. Click to focus, type to
                    # filter, ArrowDown to highlight first match, Enter to
                    # commit. Some React Select variants need Tab or blur
                    # to commit, so try multiple sequences.
                    for key_seq in (
                        ["ArrowDown", "Enter"],
                        ["ArrowDown", "ArrowDown", "Enter"],
                        ["Enter"],
                        ["Tab"],
                    ):
                        _guard_combobox_progress()
                        try:
                            locator.click(timeout=2000)
                            page.wait_for_timeout(400)
                            # Clear any existing text, then type answer
                            page.keyboard.press("Control+a")
                            page.wait_for_timeout(100)
                            page.keyboard.press("Backspace")
                            page.wait_for_timeout(100)
                            page.keyboard.type(step[:20], delay=30)
                            page.wait_for_timeout(1000)
                            for key in key_seq:
                                page.keyboard.press(key)
                                page.wait_for_timeout(200)
                            page.wait_for_timeout(500)
                            verified = _verify_control_selection(page, field, step)
                            if verified:
                                return verified
                        except Exception:
                            pass
                    # Strategy 3b: click the first visible option matching
                    # the answer anywhere on the page (portal-rendered options).
                    try:
                        locator.click(timeout=2000)
                        page.wait_for_timeout(400)
                        items = page.locator("li, div, [role='option']")
                        count = items.count()
                        for i in range(min(count, 200)):
                            _guard_combobox_progress()
                            item = items.nth(i)
                            if not item.is_visible():
                                continue
                            item_text = (item.text_content() or "").strip()
                            if _option_matches(item_text, step):
                                item.click(timeout=3000)
                                page.wait_for_timeout(500)
                                verified = _verify_control_selection(page, field, step)
                                if verified:
                                    return verified
                    except Exception:
                        pass
                _guard_combobox_progress()
                suffix = f"; available options: {', '.join(available)}" if available else ""
                raise RuntimeError(f"no combobox option matches saved answer{suffix}")
            page.wait_for_timeout(1000)
            _guard_combobox_progress()
            if index == len(steps) - 1:
                verified_selection = _verify_control_selection(page, field, step)
                if not verified_selection:
                    try:
                        visual = locator.locator(
                            "xpath=ancestor::div[contains(@class,'select-shell')][1]"
                        ).text_content() or ""
                    except Exception:
                        try:
                            visual = locator.locator(
                                "xpath=ancestor::div[contains(@class,'select__control')][1]"
                            ).text_content() or ""
                        except Exception:
                            visual = ""
                    normalized_visual = _norm(visual)
                    normalized_step = _norm(step)
                    if (
                        normalized_step
                        and not _requires_strict_combobox_commit_readback(field)
                        and (
                        normalized_step in normalized_visual
                        or normalized_visual in normalized_step
                        or ("+1" in visual and "+1" in step)
                        )
                    ):
                        verified_selection = step
        if not verified_selection and required:
            raise RuntimeError("required dropdown selection could not be verified")
        if verified_selection and school_like_field:
            _commit_school_combobox_native_value(locator, verified_selection)
        return verified_selection or "selected-unverified"
    if plan["action"] == "customselect":
        locator.click()
        page.wait_for_timeout(700)
        try:
            selected = _choose_dropdown_option(page, plan["value"], field)
        except RuntimeError:
            selected = None
        if not selected:
            available = page.evaluate(
                """(context) => {
                  const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                  const control = context.autofillId
                    ? document.querySelector(`[data-job-agent-autofill-index="${context.autofillId}"]`)
                    : (context.id ? document.getElementById(context.id) : document.activeElement);
                  const controlled = [
                    context.ariaControls,
                    context.ariaOwns,
                    control && control.getAttribute("aria-controls"),
                    control && control.getAttribute("aria-owns"),
                  ].filter(Boolean).join(" ")
                    .split(/\\s+/)
                    .map((id) => id && document.getElementById(id))
                    .filter(Boolean);
                  const popups = Array.from(new Set([
                    ...controlled,
                    ...Array.from(document.querySelectorAll('[role="listbox"], [role="menu"], [data-automation-id="activeListContainer"], [data-popper-placement], [data-radix-popper-content-wrapper], [data-headlessui-state~="open"]')),
                  ])).filter(visible);
                  const optionSelector = '[role="option"], [role="menuitem"], [role="menuitemradio"], [data-automation-id="menuItem"], [data-option-value], [data-value]';
                  return Array.from(new Set(popups.flatMap((root) => [
                    ...(root.matches && root.matches(optionSelector) ? [root] : []),
                    ...Array.from(root.querySelectorAll(optionSelector)),
                  ]).filter(visible).map((node) => (node.textContent || "").replace(/\\s+/g, " ").trim()).filter(Boolean))).slice(0, 30);
                }""",
                {
                    "id": str(field.get("id") or ""),
                    "autofillId": str(field.get("autofillId") or ""),
                    "ariaControls": str(field.get("ariaControls") or ""),
                    "ariaOwns": str(field.get("ariaOwns") or ""),
                },
            )
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            suffix = f"; available options: {', '.join(available)}" if available else ""
            raise RuntimeError(f"no button dropdown option matches saved answer{suffix}")
        page.wait_for_timeout(500)
        verified_selection = _verify_control_selection(page, field, plan["value"])
        if not verified_selection and required:
            raise RuntimeError("required dropdown selection could not be verified")
        return verified_selection or "selected-unverified"
    return None


def _date_value_for_segment(value: Any, part: str, *, today: date | None = None) -> str | None:
    """Extract one Workday date segment from an approved date value."""
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = _norm(raw)
    base = today or date.today()
    target: date | None = None
    if normalized in {"within a month", "in one month", "one month"}:
        year = base.year + (1 if base.month == 12 else 0)
        month = 1 if base.month == 12 else base.month + 1
        target = date(year, month, min(base.day, calendar.monthrange(year, month)[1]))
    elif normalized in {"within two weeks", "in two weeks", "two weeks"}:
        target = base + timedelta(days=14)
    elif normalized in {"immediately", "as soon as possible", "asap"}:
        target = base
    else:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                target = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
    if target:
        return {
            "Month": f"{target.month:02d}",
            "Day": f"{target.day:02d}",
            "Year": str(target.year),
        }.get(part)
    if part == "Month":
        month = next((index for index, name in _MONTH_NAMES.items() if _norm(name) == normalized), None)
        if month is not None:
            return f"{month:02d}"
        if raw.isdigit() and 1 <= int(raw) <= 12:
            return f"{int(raw):02d}"
    if part == "Day" and raw.isdigit() and 1 <= int(raw) <= 31:
        return f"{int(raw):02d}"
    if part == "Year" and re.fullmatch(r"\d{4}", raw):
        return raw
    return None


def _fill_workday_date_section(page, field_id: str, value: Any) -> str | None:
    """Fill one React-owned Workday date segment and verify its browser state."""
    match = re.fullmatch(r"(?P<prefix>.+)-dateSection(?P<part>Month|Day|Year)-input", field_id)
    if not match:
        return None
    prefix = match.group("prefix")
    part = match.group("part")
    compact_prefix = _norm(prefix).replace(" ", "")
    if "datesignedon" in compact_prefix:
        return _fill_workday_date_sections(page, field_id)
    desired = _date_value_for_segment(value, part)
    if desired is None:
        return None
    locator = page.locator(_attr_selector("id", field_id)).first
    if not locator.count():
        raise RuntimeError(f"Workday date section missing: {part}")
    try:
        current = str(locator.input_value() or "").strip()
        if current.isdigit() and int(current) == int(desired):
            return current
        try:
            locator.click(timeout=3000)
        except TypeError:
            locator.click()
        locator.press("Control+A")
        locator.press("Backspace")
        locator.press_sequentially(desired, delay=100)
        locator.press("Tab")
        page.wait_for_timeout(400)
        readback = str(locator.input_value() or "").strip()
        if readback.isdigit() and int(readback) == int(desired):
            return readback
        if _set_workday_date_segment_js(locator, desired):
            return str(locator.input_value() or "").strip()
        raise RuntimeError(
            f"Workday date section did not retain {part}={desired}: {readback or 'empty'}"
        )
    except Exception as exc:
        if _set_workday_date_segment_js(locator, desired):
            return str(locator.input_value() or "").strip()
        raise RuntimeError(str(exc)) from exc


def _set_workday_date_segment_js(locator, desired: str) -> bool:
    try:
        locator.evaluate(
            """(el, value) => {
              el.focus();
              const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(el, value);
              else el.value = value;
              el.setAttribute('value', value);
              el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              el.dispatchEvent(new Event('blur', { bubbles: true }));
            }""",
            desired,
        )
        readback = str(locator.input_value() or "").strip()
        return readback.isdigit() and int(readback) == int(desired)
    except Exception:
        return False


def _fill_workday_date_sections(page, field_id: str, target: date | None = None) -> str:
    prefix = field_id.split("-dateSection", 1)[0]
    if not prefix:
        raise RuntimeError("Workday date field has no section prefix")
    today = target or date.today()
    values = {
        "Month": f"{today.month:02d}",
        "Day": f"{today.day:02d}",
        "Year": str(today.year),
    }
    if _workday_date_sections_match(page, prefix, values):
        return f"{today.month:02d}/{today.day:02d}/{today.year}"

    # Workday's calendar control is the native path for its React-owned date
    # sections. Selecting a day updates the hidden form state; direct text
    # writes can look successful while the component immediately discards them.
    try:
        icon = page.locator(
            _attr_selector("data-fkit-id", prefix)
            + " "
            + _attr_selector("data-automation-id", "dateIcon")
        ).first
        if icon.count() and icon.is_visible():
            icon.click()
            page.wait_for_timeout(250)
            mmdd = f"{today.month:02d}{today.day:02d}"
            day = page.locator(
                _attr_selector("data-automation-id", "datePicker")
                + " "
                + _attr_selector("data-uxi-datepicker-year", str(today.year))
                + _attr_selector("data-uxi-datepicker-month", str(today.month))
                + _attr_selector("data-uxi-datepicker-mmdd", mmdd)
            ).first
            if day.count() and day.is_visible():
                day.click()
                page.wait_for_timeout(700)
                if _workday_date_sections_match(page, prefix, values):
                    return f"{today.month:02d}/{today.day:02d}/{today.year}"
    except Exception:
        pass

    failed_parts: list[str] = []
    for part, value in values.items():
        locator = page.locator(_attr_selector("id", f"{prefix}-dateSection{part}-input")).first
        try:
            if not locator.count():
                failed_parts.append(part)
                continue
            # Workday owns these segmented spinbuttons through React. `fill()`
            # updates the DOM value but is immediately rolled back unless the
            # component receives its normal key events.
            try:
                locator.click(timeout=3000)
            except TypeError:
                locator.click()
            locator.press("Control+A")
            locator.press("Backspace")
            locator.press_sequentially(value, delay=100)
            locator.press("Tab")
            page.wait_for_timeout(400)
            readback = str(locator.input_value() or "").strip()
            if readback.isdigit() and int(readback) == int(value):
                continue
            if _set_workday_date_segment_js(locator, value):
                continue
            failed_parts.append(f"{part}={readback or 'empty'}")
        except Exception:
            if _set_workday_date_segment_js(locator, value):
                continue
            failed_parts.append(part)
    if failed_parts:
        raise RuntimeError(
            "Workday date sections did not retain typed values: "
            + ", ".join(failed_parts)
        )
    return f"{today.month:02d}/{today.day:02d}/{today.year}"


def _workday_date_sections_match(page, prefix: str, values: dict[str, str]) -> bool:
    try:
        for part, value in values.items():
            locator = page.locator(
                _attr_selector("id", f"{prefix}-dateSection{part}-input")
            ).first
            if not locator.count():
                return False
            readback = str(locator.input_value() or "").strip()
            if not readback.isdigit() or int(readback) != int(value):
                return False
    except Exception:
        return False
    return True


def _selection_matches_answer(value: Any, answer: Any) -> bool:
    normalized = _norm(value)
    if not normalized or re.fullmatch(r"(?:select|select one|choose|please select|--.*--)?", normalized):
        return False
    return _option_matches(str(value), answer)


def _phone_country_dial_codes(answer: Any) -> set[str]:
    raw = str(answer or "")
    codes = set(re.findall(r"\+\d{1,4}", raw))
    normalized = _norm(raw)
    known_codes = {
        "united states": "+1",
        "united states of america": "+1",
        "usa": "+1",
        "us": "+1",
        "canada": "+1",
        "united kingdom": "+44",
        "uk": "+44",
        "china": "+86",
        "india": "+91",
        "australia": "+61",
        "germany": "+49",
        "france": "+33",
        "netherlands": "+31",
        "ireland": "+353",
    }
    for country, code in known_codes.items():
        if normalized == country or normalized.startswith(f"{country} "):
            codes.add(code)
    return codes


def _is_phone_country_control(page, field: dict[str, Any]) -> bool:
    label = _norm(field.get("label") or "")
    if "phone" in label and any(
        marker in label for marker in ("country", "dial", "code")
    ):
        return True
    try:
        return bool(
            page.evaluate(
                """(payload) => {
                  const visibleText = (node) => String((node && node.textContent) || "").replace(/\\s+/g, " ").trim();
                  const controls = Array.from(document.querySelectorAll(
                    'input, textarea, button, [role="combobox"], [aria-haspopup]'
                  ));
                  const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                  const labelFor = (node) => {
                    if (!node) return "";
                    const labels = [];
                    if (node.id) {
                      const explicit = document.querySelector('label[for="' + CSS.escape(node.id) + '"]');
                      if (explicit) labels.push(visibleText(explicit));
                    }
                    const wrapping = node.closest && node.closest("label");
                    if (wrapping) labels.push(visibleText(wrapping));
                    labels.push(
                      node.getAttribute("aria-label") || "",
                      node.getAttribute("placeholder") || "",
                      node.getAttribute("name") || "",
                      node.id || ""
                    );
                    return labels.join(" ").replace(/\\s+/g, " ").trim();
                  };
                  const isPhoneInput = (node) => {
                    if (!node || node.nodeType !== Node.ELEMENT_NODE) return false;
                    const tag = String(node.tagName || "").toLowerCase();
                    const type = String(node.getAttribute("type") || "").toLowerCase();
                    const context = labelFor(node).toLowerCase();
                    return (
                      (tag === "input" && type === "tel") ||
                      /\\bphone\\b|telephone|mobile/.test(context)
                    );
                  };
                  const control = controls.find((node) => payload.autofillId
                    ? node.getAttribute("data-job-agent-autofill-index") === payload.autofillId
                    : (
                      (payload.id && node.id === payload.id) ||
                      (payload.name && node.getAttribute("name") === payload.name)
                    )
                  );
                  if (!control) return false;
                  let root = control.parentElement;
                  for (let depth = 0; root && depth < 4; depth += 1) {
                    const tag = String(root.tagName || "").toLowerCase();
                    if (["form", "body", "html"].includes(tag)) break;
                    const phone = root.querySelector(
                      'input[type="tel"], input[autocomplete="tel"], input[name*="phone" i], input[id*="phone" i]'
                    );
                    if (phone && phone !== control) return true;
                    root = root.parentElement;
                  }
                  const visibleControls = controls.filter(visible);
                  const index = visibleControls.indexOf(control);
                  if (index >= 0) {
                    const ownText = [
                      payload.label || "",
                      labelFor(control),
                      visibleText(control),
                    ].join(" ").toLowerCase();
                    const countryCodeDisplayed = /\\+\\d{1,4}\\b/.test(ownText);
                    const countryLike = /\\bcountry\\b|dial|code/.test(ownText);
                    if (countryLike && countryCodeDisplayed) {
                      for (let offset = -2; offset <= 3; offset += 1) {
                        if (offset === 0) continue;
                        const nearby = visibleControls[index + offset];
                        if (isPhoneInput(nearby)) return true;
                      }
                    }
                  }
                  return false;
                }""",
                {
                    "id": str(field.get("id") or ""),
                    "name": str(field.get("name") or ""),
                    "autofillId": str(field.get("autofillId") or ""),
                    "label": str(field.get("label") or ""),
                },
            )
        )
    except Exception:
        return False


def _field_selected_text(field: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("value", "ariaLabel", "ariaDescription"):
        text = _norm(field.get(key))
        if (
            not text
            or text in {"search", "select", "select one", "expanded"}
            or "select one" in text
        ):
            continue
        if "0 items selected" in text:
            continue
        parts.append(text)
    return " ".join(parts).strip()


def _requires_strict_combobox_commit_readback(field: dict[str, Any]) -> bool:
    field_label = _norm(field.get("label") or "")
    field_id = _norm(field.get("id") or "")
    if (
        field_label in {"location", "current location", "location city"}
        or "location city" in field_label
        or field_label.endswith(" location")
        or field_id in {"location", "current location"}
    ):
        return False
    if (
        field_id == "country"
        or field_label.startswith("country")
        or "country phone code" in field_label
        or "phone country code" in field_label
        or "how did you hear" in field_label
        or "where did you hear" in field_label
    ):
        return True
    return _norm(field.get("role") or "") == "combobox"


def _control_selection_readback(page, field: dict[str, Any]) -> tuple[list[str], bool]:
    """Return committed values exposed by a custom select or combobox."""
    selector = _selector_for(field)
    direct_value = ""
    if selector:
        try:
            direct_value = str(page.locator(selector).first.input_value() or "").strip()
        except Exception:
            pass
    context = {
        "id": str(field.get("id") or ""),
        "name": str(field.get("name") or ""),
        "autofillId": str(field.get("autofillId") or ""),
        "strictCommittedSelection": _requires_strict_combobox_commit_readback(field),
    }
    try:
        state = page.evaluate(
            """(payload) => {
              const visibleText = (node) => String((node && node.textContent) || "").replace(/\\s+/g, " ").trim();
              const controls = Array.from(document.querySelectorAll(
                'input, textarea, button, [contenteditable="true"], [contenteditable="plaintext-only"], [role="combobox"], [aria-haspopup]'
              ));
              const control = controls.find((node) => payload.autofillId
                ? node.getAttribute("data-job-agent-autofill-index") === payload.autofillId
                : (
                  (payload.id && node.id === payload.id) ||
                  (payload.name && node.getAttribute("name") === payload.name)
                )
              ) || document.activeElement;
              if (!control) return { values: [], expanded: false };
              const values = [];
              const role = control.getAttribute("role") || "";
              const strictCommittedSelection = Boolean(payload.strictCommittedSelection);
              const add = (value) => {
                const text = String(value || "").replace(/\\s+/g, " ").trim();
                if (text && !/^(select|select one|choose|please select|--.*--)?$/i.test(text) && !values.includes(text)) values.push(text);
              };
              const expanded = control.getAttribute("aria-expanded") === "true";
              add(control.getAttribute("aria-valuetext"));
              add(control.getAttribute("data-value"));
              add(control.getAttribute("data-selected-value"));
              if (!expanded && !strictCommittedSelection) add(control.value);
              if (String(control.tagName || "").toLowerCase() === "button" || control.isContentEditable || control.getAttribute("aria-haspopup")) add(visibleText(control));
              const reactSelectRoot = control.closest && control.closest('[class*="select__control"]');
              const reactValueRoot = control.closest && control.closest('[class*="select__value-container"]');
              const root = (control.closest && control.closest('[data-automation-id^="formField-"], [role="group"], [role="radiogroup"], fieldset')) || control;
              const controlled = String(control.getAttribute("aria-controls") || control.getAttribute("aria-owns") || "")
                .split(/\\s+/)
                .map((id) => id && document.getElementById(id))
                .filter(Boolean);
              const roots = Array.from(new Set(
                [reactSelectRoot, reactValueRoot, root, ...controlled].filter(Boolean)
              ));
              for (const candidateRoot of roots) {
                if (!candidateRoot || !candidateRoot.querySelectorAll) continue;
                candidateRoot.querySelectorAll(
                  '[class*="select__single-value"], [class*="select__multi-value__label"]'
                ).forEach((node) => add(visibleText(node)));
                candidateRoot.querySelectorAll('[data-automation-id="selectedItem"]').forEach((node) => add(visibleText(node) || node.getAttribute("data-value") || node.getAttribute("aria-label")));
                candidateRoot.querySelectorAll('[data-automation-id="promptSelectionLabel"]').forEach((node) => add(visibleText(node) || node.getAttribute("data-value") || node.getAttribute("aria-label")));
                if (!expanded) {
                  candidateRoot.querySelectorAll('[aria-selected="true"], [aria-checked="true"], [data-state="selected"], [data-state="checked"], [data-state="on"]').forEach((node) => add(visibleText(node) || node.getAttribute("data-value") || node.getAttribute("aria-label")));
                }
                candidateRoot.querySelectorAll('input[type="radio"]:checked').forEach((node) => {
                  const label = (node.closest && node.closest("label")) ||
                    Array.from(document.querySelectorAll("label")).find((item) => item.htmlFor === node.id || item.getAttribute("for") === node.id);
                  add(visibleText(label) || node.getAttribute("aria-label") || node.value);
                });
              }
              const describedBy = String((control.getAttribute("aria-describedby") || ""))
                .split(/\\s+/)
                .map((id) => id && document.getElementById(id))
                .filter(Boolean)
                .map((node) => visibleText(node))
                .filter(Boolean)
                .join(" ");
              if (/^[1-9]\\d*\\s+item(?:s)?\\s+selected\\b/i.test(describedBy)) add(describedBy);
              const activeId = control.getAttribute("aria-activedescendant");
              if (!expanded && activeId) add(visibleText(document.getElementById(activeId)));
              return { values, expanded };
            }""",
            context,
        )
    except Exception:
        state = {"values": [], "expanded": False}
    values = [str(value).strip() for value in (state or {}).get("values", []) if str(value).strip()]
    strict_committed_selection = _requires_strict_combobox_commit_readback(field)
    if (
        not (state or {}).get("expanded")
        and direct_value
        and not strict_committed_selection
        and not re.fullmatch(r"(?:select|select one|choose|please select|--.*--)?", _norm(direct_value))
        and direct_value not in values
    ):
        values.append(direct_value)
    return values, bool((state or {}).get("expanded"))


def _verify_control_selection(page, field: dict[str, Any], answer: Any) -> str | None:
    values, expanded = _control_selection_readback(page, field)
    selected = next((value for value in values if _selection_matches_answer(value, answer)), None)
    if selected is not None:
        return selected
    if _is_phone_country_control(page, field):
        expected_codes = _phone_country_dial_codes(answer)
        selected_code = next(
            (
                re.sub(r"\s+", "", value)
                for value in values
                if re.fullmatch(r"\+\d{1,4}", re.sub(r"\s+", "", value))
                and re.sub(r"\s+", "", value) in expected_codes
            ),
            None,
        )
        if selected_code is not None:
            return selected_code
    if expanded:
        raise RuntimeError("dropdown remained open without a committed selection")
    if not values:
        return None
    raise RuntimeError("dropdown selection readback does not match requested answer")


def _choose_dropdown_option(page, answer: str, field: dict[str, Any]) -> str | None:
    """Select and verify a custom option, following menu sublevels when needed.

    A source selector is not unique to Workday: many bespoke forms first show
    categories such as ``Website`` and then reveal a second menu with the
    concrete option.  Read the control after every click instead of assuming a
    visible option is already a committed value.
    """
    attempted: set[str] = set()
    field_label = _norm(field.get("label") or "")
    # Workday renders simple dropdowns as [data-automation-id="menuItem"] rows.
    # Try that first for short Yes/No/other answers before falling back to the
    # generic ARIA option scanner, which can miss Workday's custom popovers.
    if field.get("automationId") or "myworkdayjobs" in str(page.url or "").lower():
        try:
            selector = _selector_for(field)
            locator_for_field = page.locator(selector).first if selector else None
            if (
                "how did you hear" in field_label
                or "where did you hear" in field_label
                or field_label in {"field of study", "major"}
                or "school or university" in field_label
            ):
                prompt_selected = _click_workday_prompt_option_with_playwright(page, answer, field)
                if prompt_selected:
                    return prompt_selected
            # Scoped JavaScript click is more reliable when multiple Workday
            # dropdowns are present or when Playwright's visible-text filters
            # struggle with detached popovers.
            js_selected = _click_workday_menu_item_js(page, field, answer)
            if js_selected:
                page.wait_for_timeout(450)
                if locator_for_field:
                    try:
                        locator_for_field.dispatch_event("change")
                        locator_for_field.dispatch_event("blur")
                    except Exception:
                        pass
                verified = _verify_control_selection(page, field, answer)
                if verified:
                    return verified
            menu_item = page.locator('[data-automation-id="menuItem"]').filter(
                has_text=re.compile(re.escape(answer), re.IGNORECASE)
            ).first
            if not (menu_item.count() and menu_item.is_visible()):
                # The popover may not be open yet; click the control once.
                if locator_for_field:
                    locator_for_field.click(timeout=3000)
                    page.wait_for_timeout(400)
            if menu_item.count() and menu_item.is_visible():
                menu_item.click(timeout=3000)
                page.wait_for_timeout(450)
                # Workday's React state often needs an explicit change/blur to
                # clear validation errors that were rendered before we filled.
                if locator_for_field:
                    try:
                        locator_for_field.dispatch_event("change")
                        locator_for_field.dispatch_event("blur")
                    except Exception:
                        pass
                verified = _verify_control_selection(page, field, answer)
                if verified:
                    return verified
            # Some Workday simple dropdowns expose radio buttons instead of
            # menu items once opened.
            radio_btn = page.locator('[data-automation-id="radioBtn"]').filter(
                has_text=re.compile(re.escape(answer), re.IGNORECASE)
            ).first
            if radio_btn.count() and radio_btn.is_visible():
                radio_btn.click(timeout=3000)
                page.wait_for_timeout(450)
                if locator_for_field:
                    try:
                        locator_for_field.dispatch_event("change")
                        locator_for_field.dispatch_event("blur")
                    except Exception:
                        pass
                verified = _verify_control_selection(page, field, answer)
                if verified:
                    return verified
        except Exception:
            pass
    if "how did you hear" in field_label or "where did you hear" in field_label:
        selected = _select_workday_nested_prompt_option(page, answer, field)
        if selected:
            return selected

    for _ in range(4):
        clicked = _click_visible_option_with_playwright(
            page,
            answer,
            field,
            exclude=attempted,
            aliases=_answer_aliases(answer),
        )
        if not clicked:
            break
        attempted.add(_norm(clicked))
        page.wait_for_timeout(450)
        values, expanded = _control_selection_readback(page, field)
        selected = next(
            (value for value in values if _selection_matches_answer(value, answer)),
            None,
        )
        if selected and not expanded:
            return selected
        if selected and expanded:
            # A committed custom select occasionally leaves its popover open.
            # Closing it and reading again distinguishes that case from a
            # category row that only opened a nested menu.
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(150)
                values_after, expanded_after = _control_selection_readback(page, field)
                committed = next(
                    (value for value in values_after if _selection_matches_answer(value, answer)),
                    None,
                )
                if committed and not expanded_after:
                    return committed
            except Exception:
                pass
        # An open or uncommitted menu can contain a child option. Keep the
        # parent out of the candidate set and inspect the newly visible layer.
        if not expanded and not values:
            try:
                selector = _selector_for(field)
                if selector:
                    page.locator(selector).first.click(timeout=3000)
                    page.wait_for_timeout(200)
            except Exception:
                pass

    # Some custom selects (e.g. Greenhouse react-select, Workday listboxes)
    # commit the active option on Tab even when Escape leaves the popover open.
    try:
        page.keyboard.press("Tab")
        page.wait_for_timeout(200)
        values, expanded = _control_selection_readback(page, field)
        committed = next(
            (value for value in values if _selection_matches_answer(value, answer)),
            None,
        )
        if committed and not expanded:
            return committed
    except Exception:
        pass

    verified = _verify_control_selection(page, field, answer)
    if verified:
        return verified
    return None


def _click_workday_prompt_option_with_playwright(
    page,
    answer: str,
    field: dict[str, Any],
) -> str | None:
    """Click a Workday prompt/multiselect option through trusted Playwright events."""
    if "myworkdayjobs" not in str(page.url or "").lower():
        return None
    label = str(field.get("label") or "")
    if not label:
        return None
    label_pattern = re.compile(re.escape(label.replace("*", "").strip()), re.I)
    answer_pattern = re.compile(rf"^\s*{re.escape(str(answer).strip())}\s*$", re.I)
    try:
        root = page.locator('[data-automation-id^="formField-"]').filter(
            has_text=label_pattern
        ).first
        if not root.count():
            try:
                fields = page.locator('[data-automation-id^="formField-"]').evaluate_all(
                    """(nodes) => nodes.map((node) => ({
                      automationId: node.getAttribute('data-automation-id') || '',
                      text: (node.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 180)
                    })).slice(0, 12)"""
                )
                if os.getenv("JOB_AGENT_DEBUG_WORKDAY_PROMPTS"):
                    print(
                        f"[_click_workday_prompt_option_with_playwright] no field root for {label!r}; fields={fields}",
                        file=sys.stderr,
                    )
            except Exception:
                pass
            return None
        try:
            existing_values = root.locator('[data-automation-id="selectedItem"]').all_text_contents()
        except Exception:
            existing_values = []
        existing_match = next(
            (value for value in existing_values if _selection_matches_answer(value, answer)),
            None,
        )
        if existing_match:
            return existing_match
        if existing_values:
            for _ in range(5):
                delete_charm = root.locator(
                    '[data-automation-id="DELETE_charm"], [aria-label*="clear value" i]'
                ).first
                if not (delete_charm.count() and delete_charm.is_visible()):
                    break
                try:
                    delete_charm.click(timeout=2000)
                    page.wait_for_timeout(300)
                except Exception:
                    break
        # Source prompts use an icon/list button inside the field. Prefer that
        # over the text input, because typing into the shell can leave Workday
        # with a highlighted but uncommitted row.
        prompt = root.locator(
            '[data-automation-id*="prompt"], [data-automation-id*="Prompt"], '
            '[aria-haspopup], [role="combobox"], button, input'
        ).last
        if prompt.count():
            prompt.click(timeout=3000)
        else:
            root.click(timeout=3000)
        page.wait_for_timeout(700)
        search_box = root.locator('input[data-automation-id="searchBox"]').first
        if search_box.count():
            try:
                search_box.fill(str(answer), timeout=3000)
                page.wait_for_timeout(700)
                search_box.press("Enter", timeout=3000)
                page.wait_for_timeout(700)
                try:
                    committed = _verify_control_selection(page, field, answer)
                    if committed:
                        return committed
                except Exception:
                    pass
            except Exception:
                pass
        try:
            diagnostics = root.evaluate(
                """(node) => ({
                  automationId: node.getAttribute('data-automation-id') || '',
                  text: (node.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 240),
                  controls: Array.from(node.querySelectorAll('button, input, [role], [aria-haspopup], [data-automation-id]')).map((el) => ({
                    tag: el.tagName,
                    role: el.getAttribute('role') || '',
                    automationId: el.getAttribute('data-automation-id') || '',
                    ariaLabel: el.getAttribute('aria-label') || '',
                    ariaExpanded: el.getAttribute('aria-expanded') || '',
                    text: (el.textContent || el.value || '').replace(/\\s+/g, ' ').trim().slice(0, 80)
                  })).slice(0, 20)
                })"""
            )
            menus = page.locator(
                '[data-automation-id="menuItem"], [role="option"], [role="treeitem"], '
                '[role="menuitem"], [data-automation-id="radioBtn"]'
            ).evaluate_all(
                """(nodes) => nodes.filter((node) => node.offsetParent || node.getClientRects().length).map((node) => ({
                  tag: node.tagName,
                  role: node.getAttribute('role') || '',
                  automationId: node.getAttribute('data-automation-id') || '',
                  text: (node.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120)
                })).slice(0, 20)"""
            )
            if os.getenv("JOB_AGENT_DEBUG_WORKDAY_PROMPTS"):
                print(
                    f"[_click_workday_prompt_option_with_playwright] diagnostics label={label!r} root={diagnostics} visibleMenus={menus}",
                    file=sys.stderr,
                )
        except Exception:
            pass
        item = page.locator(
            '[data-automation-id="menuItem"], [role="option"], [role="treeitem"], '
            '[role="menuitem"], [data-automation-id="radioBtn"]'
        ).filter(has_text=answer_pattern).last
        if not (item.count() and item.is_visible()):
            item = page.get_by_text(answer, exact=True).last
        if not (item.count() and item.is_visible()):
            for alias in _answer_aliases(answer):
                if _norm(alias) == _norm(answer):
                    continue
                alias_pattern = re.compile(re.escape(str(alias).strip()), re.I)
                alias_item = page.locator(
                    '[data-automation-id="menuItem"], [role="option"], [role="treeitem"], '
                    '[role="menuitem"], [data-automation-id="radioBtn"]'
                ).filter(has_text=alias_pattern).first
                if alias_item.count() and alias_item.is_visible():
                    item = alias_item
                    break
        if not (item.count() and item.is_visible()):
            return None
        item.click(timeout=3000)
        page.wait_for_timeout(700)
        for key in ("Enter", "Tab", "Escape"):
            try:
                page.keyboard.press(key)
                page.wait_for_timeout(300)
                committed = _verify_control_selection(page, field, answer)
                if committed:
                    return committed
            except Exception:
                pass
        # Some Workday prompts open a nested radio/checkbox list after the
        # parent source row is clicked.
        nested = page.locator(
            '[data-automation-id="activeListContainer"] '
            '[data-automation-id="radioBtn"], '
            '[data-automation-id="activeListContainer"] [role="radio"], '
            '[data-automation-id="activeListContainer"] [role="checkbox"]'
        ).filter(has_text=answer_pattern).last
        if nested.count() and nested.is_visible():
            nested.click(timeout=3000)
            page.wait_for_timeout(700)
        try:
            return _verify_control_selection(page, field, answer)
        except Exception:
            return None
    except Exception:
        return None


def _select_workday_nested_prompt_option(
    page,
    answer: str,
    field: dict[str, Any] | None = None,
) -> str | None:
    """Select Workday source values that open a nested prompt before checking.

    Some Workday source prompts render the visible source as a parent row with a
    right-side charm. A normal click only highlights the row; the actual value is
    committed by opening that row and clicking the radio button inside the nested
    prompt.
    """
    try:
        parent_labels = list(dict.fromkeys([answer, *_answer_aliases(answer)]))
        option = None
        for parent_label in parent_labels:
            candidate = page.locator('[data-automation-id="menuItem"]').filter(
                has_text=parent_label
            ).last
            try:
                candidate.wait_for(state="visible", timeout=1500)
                option = candidate
                break
            except Exception:
                continue
        if option is None:
            return None
        option.click()
        page.wait_for_timeout(600)
        radio = None
        for label in parent_labels:
            candidate_radio = page.locator(
                '[data-automation-id="activeListContainer"] [data-automation-id="radioBtn"]'
            ).filter(has_text=label).last
            if candidate_radio.count() and candidate_radio.is_visible():
                radio = candidate_radio
                break
        if radio is None:
            only_radio = page.locator(
                '[data-automation-id="activeListContainer"] [data-automation-id="radioBtn"]'
            ).first
            if only_radio.count() == 1 and only_radio.is_visible():
                radio = only_radio
        if radio is not None:
            radio.click()
            page.wait_for_timeout(600)
        return _verify_control_selection(page, field or {}, answer)
    except Exception:
        return None


def _click_visible_option_with_playwright(
    page,
    answer: str,
    field: dict[str, Any] | None = None,
    *,
    exclude: set[str] | None = None,
    aliases: list[str] | None = None,
) -> str | None:
    """Choose a visible option, scoped to the active control when possible."""
    context = {
        "id": str((field or {}).get("id") or ""),
        "autofillId": str((field or {}).get("autofillId") or ""),
        "ariaControls": str((field or {}).get("ariaControls") or ""),
        "ariaOwns": str((field or {}).get("ariaOwns") or ""),
        "exclude": sorted(_norm(value) for value in (exclude or set()) if _norm(value)),
        "aliases": [_norm(alias) for alias in (aliases or []) if _norm(alias)],
    }
    option = page.evaluate(
        """(payload) => {
          const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9\\s]/g, " ").replace(/\\s+/g, " ").trim();
          const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
          const stateNames = {
            al: "alabama", ak: "alaska", az: "arizona", ar: "arkansas", ca: "california",
            co: "colorado", ct: "connecticut", de: "delaware", fl: "florida", ga: "georgia",
            hi: "hawaii", id: "idaho", il: "illinois", in: "indiana", ia: "iowa", ks: "kansas",
            ky: "kentucky", la: "louisiana", me: "maine", md: "maryland", ma: "massachusetts",
            mi: "michigan", mn: "minnesota", ms: "mississippi", mo: "missouri", mt: "montana",
            ne: "nebraska", nv: "nevada", nh: "new hampshire", nj: "new jersey", nm: "new mexico",
            ny: "new york", nc: "north carolina", nd: "north dakota", oh: "ohio", ok: "oklahoma",
            or: "oregon", pa: "pennsylvania", ri: "rhode island", sc: "south carolina",
            sd: "south dakota", tn: "tennessee", tx: "texas", ut: "utah", vt: "vermont",
            va: "virginia", wa: "washington", wv: "west virginia", wi: "wisconsin",
            wy: "wyoming", dc: "district of columbia"
          };
          const expandLocation = (s) => norm(s).split(" ").flatMap((token) => {
            if (token === "us" || token === "usa") return ["united", "states"];
            return (stateNames[token] || token).split(" ");
          }).join(" ");
          const want = norm(payload.answer);
          const wants = [want];
          const ctxAliases = (payload.context && payload.context.aliases) || [];
          for (const alias of ctxAliases) {
            const n = norm(alias);
            if (n && !wants.includes(n)) wants.push(n);
          }
          if (["prefer not to say", "prefer not to answer", "decline", "decline to answer"].includes(want)) {
            wants.push("i do not wish to self identify", "i do not wish to answer", "not declared", "declined to state");
          }
          if (["company website", "company site", "company careers", "company career site", "career site", "career website", "careers website", "careers site"].includes(want)) {
            wants.push("corporate website", "career site", "careers website", "career website", "company careers", "careers site", "company careers page website");
          }
          if (["master s degree", "masters degree", "master degree"].includes(want)) {
            wants.push("master degree", "masters degree", "master s degree");
          }
          if (["east asian", "asian"].includes(want)) {
            wants.push("asian", "asian not hispanic or latino");
          }
          if (["united states", "united states of america", "usa", "us"].includes(want)) {
            wants.push("united states +1", "usa +1", "us +1", "united states of america +1");
          }
          if (want === "no") {
            wants.push("i have not previously been employed", "i have not been previously employed", "i have not worked");
          }
          const context = payload.context || {};
          const excluded = new Set((payload.exclude || []).map(norm));
          const control = context.autofillId
            ? document.querySelector(`[data-job-agent-autofill-index="${context.autofillId}"]`)
            : (context.id ? document.getElementById(context.id) : document.activeElement);
          const controlledIds = [
            context.ariaControls,
            context.ariaOwns,
            control && control.getAttribute("aria-controls"),
            control && control.getAttribute("aria-owns"),
          ].filter(Boolean).join(" ")
            .split(/\\s+/)
            .map((id) => id && document.getElementById(id))
            .filter(Boolean);
          const listboxes = Array.from(new Set([
            ...Array.from(document.querySelectorAll('[role="listbox"], [role="menu"]')),
            ...Array.from(document.querySelectorAll('[role="tree"], [role="dialog"], [data-automation-id="activeListContainer"], [data-popper-placement], [data-radix-popper-content-wrapper], [data-headlessui-state~="open"]')),
          ])).filter(visible);
	          let roots = controlledIds.filter(visible);
	          if (listboxes.length) {
	            const labelled = control && control.id
	              ? listboxes.filter((node) => String(node.getAttribute("aria-labelledby") || "").split(/\\s+/).includes(control.id))
	              : [];
            const candidates = labelled.length ? labelled : listboxes;
            const controlBox = control && control.getBoundingClientRect ? control.getBoundingClientRect() : null;
            candidates.sort((left, right) => {
              if (!controlBox) return 0;
              const distance = (node) => {
                const box = node.getBoundingClientRect();
                return Math.abs(box.top - controlBox.bottom) + Math.abs(box.left - controlBox.left);
	              };
	              return distance(left) - distance(right);
	            });
	            roots = Array.from(new Set([...roots, ...candidates.slice(0, roots.length ? 2 : 1)]));
	          }
          const optionSelector = '[role="option"], [role="menuitem"], [role="menuitemradio"], [role="treeitem"], [role="radio"], [role="checkbox"], [data-automation-id="menuItem"], [data-automation-id="radioBtn"], [data-option-value], [data-value], [data-state], li, button';
          const optionNodes = roots.length
            ? roots.flatMap((root) => [
              ...(root.matches && root.matches(optionSelector) ? [root] : []),
              ...Array.from(root.querySelectorAll(optionSelector)),
            ])
            : Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [data-automation-id="menuItem"]'));
          const options = Array.from(new Set(optionNodes))
            .filter(visible)
            .map((node, index) => {
              const autofillId = `option-${index}`;
              node.setAttribute("data-job-agent-option-index", autofillId);
              const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
              const attribute = (name) => typeof node.getAttribute === "function" ? node.getAttribute(name) : "";
              const value = attribute("aria-label") || attribute("data-option-value") || attribute("data-value") || "";
              return { id: node.id || "", text, value, autofillId };
            })
            .filter((node) => node.text || node.value);
          const score = (node) => {
            const text = norm(`${node.text} ${node.value}`);
            const aliasScore = wants.reduce((best, candidate) => {
              if (text === candidate) return Math.max(best, 100);
              const expandedText = expandLocation(text);
              const expandedCandidate = expandLocation(candidate);
              if (expandedText === expandedCandidate) return Math.max(best, 95);
              if (expandedText.includes(expandedCandidate)) return Math.max(best, 70);
              if (expandedCandidate.includes(expandedText)) return Math.max(best, 60);
              return best;
            }, 0);
            return aliasScore;
          };
          const option = options.map((node, index) => ({ ...node, index, score: score(node) }))
            .filter((node) => !excluded.has(norm(node.text)) && !excluded.has(norm(node.value)))
            .filter((node) => node.score > 0)
            .sort((a, b) => b.score - a.score || a.index - b.index)[0];
          if (!option) {
            return {
              noMatch: true,
              optionsCount: options.length,
              sampleTexts: options.slice(0, 8).map((o) => o.text),
            };
          }
          return option;
        }""",
        {"answer": answer, "context": context},
    )
    if not option or (isinstance(option, dict) and option.get("noMatch")):
        return None
    display = str(option.get("text") or option.get("value") or "")
    if option.get("autofillId") is not None:
        try:
            page.locator(
                _attr_selector("data-job-agent-option-index", str(option["autofillId"]))
            ).first.click(timeout=3000)
            return display
        except Exception:
            pass
    # React Select handles trusted pointer events. Use Playwright locators before
    # any synthetic DOM event so an option is committed instead of merely
    # highlighted.
    if option.get("id"):
        try:
            page.locator(_attr_selector("id", option["id"])).first.click(timeout=3000)
            return display
        except Exception:
            pass
    if option.get("text"):
        try:
            page.get_by_role("option", name=option["text"]).first.click(timeout=3000)
            return str(option.get("text") or "")
        except Exception:
            pass
        try:
            page.locator('[data-automation-id="menuItem"]').filter(has_text=option["text"]).last.click(timeout=3000)
            return str(option.get("text") or "")
        except Exception:
            pass
        try:
            page.get_by_text(option["text"], exact=True).last.click(timeout=3000)
            return str(option.get("text") or "")
        except Exception:
            pass
    # Last resort for controls whose option nodes cannot be addressed by
    # Playwright. Callers must still verify the committed control value.
    try:
        clicked = page.evaluate(
            """(payload) => {
              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
              const selectors = [];
              if (payload.autofillId != null) {
                selectors.push(`[data-job-agent-option-index="${payload.autofillId}"]`);
              }
              if (payload.id) {
                const escaped = String(payload.id).replace(/["\\\\]/g, "\\\\$&");
                selectors.push(`#${escaped}`);
              }
              const node = selectors
                .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
                .find(visible);
              if (!node) return false;
              const eventInit = { bubbles: true, cancelable: true, view: window };
              node.dispatchEvent(new MouseEvent("mousedown", eventInit));
              node.dispatchEvent(new MouseEvent("mouseup", eventInit));
              node.click();
              return true;
            }""",
            {"id": option.get("id") or "", "autofillId": option.get("autofillId")},
        )
        if clicked:
            page.wait_for_timeout(250)
            return display
    except Exception:
        pass
    return None


def _field_repair_identity(
    field: dict[str, Any],
    fields: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Prefer stable unique DOM attributes over per-scrape marker indexes."""
    kind = str(field.get("kind") or "single")
    for key in ("id", "name"):
        value = str(field.get(key) or "").strip()
        if not value:
            continue
        matches = sum(
            1
            for candidate in fields
            if str(candidate.get("kind") or "single") == kind
            and str(candidate.get(key) or "").strip() == value
        )
        if matches == 1:
            return (key, kind, value)
    if field.get("autofillId"):
        return ("marker", kind, str(field["autofillId"]))
    return (
        "shape",
        kind,
        str(field.get("tag") or ""),
        str(field.get("type") or ""),
        str(field.get("name") or ""),
        str(field.get("label") or ""),
        str(field.get("section") or ""),
    )


def _fill_page(
    page,
    profile: dict[str, Any],
    resume_file: str | None,
    cover_letter_file: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    filled: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    handled: set[tuple[str, ...]] = set()

    def process_fields(fields: list[dict[str, Any]]) -> None:
        for descriptor in fields:
            descriptor_identity = _field_repair_identity(descriptor, fields)
            if descriptor_identity in handled:
                continue
            current_fields = _scrape_fields(page)
            field = next(
                (
                    candidate
                    for candidate in current_fields
                    if _field_repair_identity(candidate, current_fields)
                    == descriptor_identity
                ),
                None,
            )
            if field is None and descriptor.get("autofillId"):
                field = next(
                    (
                        candidate
                        for candidate in current_fields
                        if candidate.get("autofillId") == descriptor.get("autofillId")
                        and candidate.get("kind") == descriptor.get("kind")
                    ),
                    None,
                )
            if field is None and descriptor.get("id"):
                field = next(
                    (
                        candidate
                        for candidate in current_fields
                        if candidate.get("id") == descriptor.get("id")
                        and candidate.get("kind") == descriptor.get("kind")
                    ),
                    None,
                )
            if field is None and descriptor.get("name"):
                field = next(
                    (
                        candidate
                        for candidate in current_fields
                        if candidate.get("name") == descriptor.get("name")
                        and candidate.get("kind") == descriptor.get("kind")
                    ),
                    None,
                )
            if field is None and descriptor.get("kind") in {"radiogroup", "buttongroup", "checkboxgroup"}:
                descriptor_label = str(descriptor.get("label") or descriptor.get("name") or "")
                field = next(
                    (
                        candidate
                        for candidate in current_fields
                        if candidate.get("kind") == descriptor.get("kind")
                        and _same_required_field(
                            descriptor_label,
                            str(candidate.get("label") or candidate.get("name") or ""),
                        )
                    ),
                    None,
                )
            if field is None:
                field = next(
                    (
                        candidate
                        for candidate in current_fields
                        if candidate.get("label") == descriptor.get("label")
                        and candidate.get("tag") == descriptor.get("tag")
                        and candidate.get("type") == descriptor.get("type")
                    ),
                    None,
            )
            if field is None:
                continue
            identity = _field_repair_identity(field, current_fields)
            if identity in handled:
                continue
            runtime_context = _runtime_fill_context(current_fields)
            plan = _plan_field(field, profile, resume_file, cover_letter_file, runtime_context)
            if plan["action"] == "skip":
                handled.add(identity)
                if plan.get("reason") in {
                    "button dropdown already selected",
                    "combobox already selected",
                    "field already selected",
                    "optional empty field",
                    "optional non-resume file field",
                    "preferred name checkbox not needed",
                    "approved No answer leaves checkbox unchecked",
                    "approved No answer has no matching optional option",
                    "approved No answer has no matching optional checkbox option",
                    "non-required unmapped field",
                    "honeypot field",
                    "email verification handled after submit",
                }:
                    continue
                review.append(
                    {
                        "label": field.get("label") or field.get("id") or field.get("name") or "unlabeled field",
                        "reason": plan.get("reason", "skipped"),
                        "sensitive": bool(plan.get("sensitive")),
                        "blocking": bool(plan.get("blocking", True)),
                    }
                )
                continue
            try:
                field_label = field.get("label") or field.get("id") or field.get("name") or "unlabeled field"
                print(f"Autofill field: {field_label} ({plan['action']})")
                readback = _apply_fill(page, field, plan)
                filled.append({"label": field.get("label"), "action": plan["action"], "readback": readback})
                handled.add(identity)
            except Exception as exc:
                handled.add(identity)
                field_label = (
                    field.get("label")
                    or field.get("id")
                    or field.get("name")
                    or "unlabeled field"
                )
                review.append(
                    {
                        "label": field.get("label"),
                        "reason": f"fill error: {exc}",
                        "sensitive": bool(
                            plan.get("sensitive") or _is_sensitive(str(field_label))
                        ),
                        "blocking": True,
                    }
                )

    def repair_invalid_required_fields(findings: list[dict[str, str]]) -> None:
        if not findings:
            return
        current_fields = _scrape_fields(page)
        runtime_context = _runtime_fill_context(current_fields)
        for finding in findings:
            finding_label = str(finding.get("label") or "")
            if not finding_label or _is_email_verification_field(finding_label):
                continue
            if _repair_invalid_source_combobox_by_label(page, finding_label, profile):
                print(f"Autofill repair field: {finding_label} (source-combobox)")
                filled.append({"label": finding_label, "action": "combobox", "readback": "filled"})
                continue
            field = next(
                (
                    candidate
                    for candidate in current_fields
                    if candidate.get("type") != "file"
                    and _same_required_field(
                        finding_label,
                        str(candidate.get("label") or candidate.get("id") or candidate.get("name") or ""),
                    )
                ),
                None,
            )
            if field is None:
                if _repair_invalid_text_field_by_label(page, finding_label, profile):
                    print(f"Autofill repair field: {finding_label} (direct-fill)")
                    filled.append({"label": finding_label, "action": "fill", "readback": "filled"})
                continue
            plan = _plan_field(field, profile, resume_file, cover_letter_file, runtime_context)
            if plan.get("action") == "skip":
                continue
            try:
                field_label = field.get("label") or field.get("id") or field.get("name") or "unlabeled field"
                print(f"Autofill repair field: {field_label} ({plan['action']})")
                readback = _apply_fill(page, field, plan)
                filled.append({"label": field.get("label"), "action": plan["action"], "readback": readback})
                handled.add(_field_repair_identity(field, current_fields))
            except Exception:
                pass

    initial_fields = _ensure_application_fields_ready(page)
    seen_signatures: set[tuple[str, ...]] = set()
    file_fields = sorted(
        (field for field in initial_fields if field.get("type") == "file"),
        key=lambda field: "cover letter" in _norm(field.get("label") or ""),
    )
    for descriptor in file_fields:
        current_fields = _scrape_fields(page)
        field = next(
            (
                candidate
                for candidate in current_fields
                if candidate.get("type") == "file"
                and (
                    (
                        descriptor.get("autofillId")
                        and candidate.get("autofillId") == descriptor.get("autofillId")
                    )
                    or (
                        not descriptor.get("autofillId")
                        and (
                            candidate.get("id") == descriptor.get("id")
                            or candidate.get("label") == descriptor.get("label")
                        )
                    )
                )
            ),
            descriptor,
        )
        before = len(filled)
        process_fields([field])
        if len(filled) > before and filled[-1].get("action") == "upload":
            wait_ms = 6000 if "resume" in _norm(field.get("label") or "") else 1500
            page.wait_for_timeout(wait_ms)

    non_file_fields = [field for field in _scrape_fields(page) if field.get("type") != "file"]
    process_fields(
        [
            field
            for field in non_file_fields
            if field.get("kind") == "buttongroup" and field.get("required")
        ]
    )
    process_fields(non_file_fields)
    # Conditional questions often appear only after a prior choice or upload.
    # Re-scan until the visible form structure stabilizes, without re-running
    # already completed controls on every pass.
    for _ in range(3):
        page.wait_for_timeout(300)
        dynamic_fields = _scrape_fields(page)
        signature = _form_field_signature(dynamic_fields)
        if signature in seen_signatures:
            break
        seen_signatures.add(signature)
        dynamic_files = sorted(
            (field for field in dynamic_fields if field.get("type") == "file"),
            key=lambda field: "cover letter" in _norm(field.get("label") or ""),
        )
        for field in dynamic_files:
            before = len(filled)
            process_fields([field])
            if len(filled) > before and filled[-1].get("action") == "upload":
                page.wait_for_timeout(6000 if "resume" in _norm(field.get("label") or "") else 1500)
        non_file_fields = [field for field in _scrape_fields(page) if field.get("type") != "file"]
        process_fields(
            [
                field
                for field in non_file_fields
                if field.get("kind") == "buttongroup" and field.get("required")
            ]
        )
        process_fields(non_file_fields)
    required_findings = _audit_required_fields(page)
    repair_invalid_required_fields(required_findings)
    if required_findings:
        try:
            page.wait_for_timeout(500)
        except Exception:
            pass
    _append_required_audit(review, _audit_required_fields(page), filled=filled)
    return {"filled": filled, "review": review}


def _recover_application_form_from_job_page(page, application_url: str | None) -> bool:
    """Retry an unavailable Ashby direct-application route through its job page."""
    parsed = urlparse(str(application_url or ""))
    path = parsed.path.rstrip("/")
    if parsed.hostname != "jobs.ashbyhq.com" or not path.endswith("/application"):
        return False
    job_path = path[: -len("/application")]
    if not job_path:
        return False
    job_page_url = parsed._replace(path=job_path, query="", fragment="").geturl()
    try:
        page.goto(job_page_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1000)
        _open_application_form_if_needed(page)
        return _wait_for_application_form_context(page, attempts=4, delay_ms=750)
    except Exception:
        return False


def _ensure_application_fields_ready(
    page,
    *,
    attempts: int = 8,
    delay_ms: int = 1000,
) -> list[dict[str, Any]]:
    for attempt in range(max(1, attempts)):
        fields = _scrape_fields(page)
        if _meaningful_application_fields(fields):
            return fields
        if attempt == max(1, attempts) - 1:
            return fields
        _wait_for_application_form_context(page, attempts=1, delay_ms=delay_ms)
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            pass
    return _scrape_fields(page)


def _embedded_application_frame_url(page) -> str | None:
    try:
        frame_url = page.evaluate(
            """() => {
              const frames = Array.from(document.querySelectorAll('iframe[src]'))
                .map((frame) => String(frame.src || '').trim())
                .filter(Boolean);
              return frames.find((src) => /boards\\.greenhouse\\.io\\/embed\\/job_app\\?/i.test(src)) || null;
            }"""
        )
    except Exception:
        return None
    value = str(frame_url or "").strip()
    return value or None


def _open_embedded_application_iframe_if_needed(page) -> bool:
    frame_url = _embedded_application_frame_url(page)
    if not frame_url:
        return False
    current_url = str(getattr(page, "url", "") or "").strip()
    if current_url == frame_url:
        return False
    try:
        page.goto(frame_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        try:
            page.goto(frame_url, timeout=30000)
        except Exception:
            return False
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    return True


def _open_application_form_if_needed(page) -> bool:
    opened = False
    for _ in range(3):
        if _open_embedded_application_iframe_if_needed(page):
            opened = True
        current_fields = _scrape_fields(page)
        entry = _find_application_entry(page)
        if current_fields and not _is_workday_apply_gate(page, current_fields):
            if _has_application_form_context(page) and not _is_job_page_apply_button(page, entry, current_fields):
                return opened
        if not current_fields and _open_workday_email_sign_in_if_needed(page):
            opened = True
            continue
        if not entry:
            return opened
        _click_button(page, entry)
        opened = True
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
    return opened


def _find_workday_email_sign_in_entry(page) -> dict[str, Any] | None:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return None
    try:
        entries = page.evaluate(
            """() => {
              const visible = (node) => {
                if (!node) return false;
                if (node.offsetParent) return true;
                const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
                return rects && rects.length > 0;
              };
              let index = 0;
              return Array.from(document.querySelectorAll("a,button,[role='button']"))
                .filter(visible)
                .map((node) => {
                  const autofillId = String(index++);
                  node.setAttribute("data-job-agent-button-index", autofillId);
                  return {
                    text: (node.textContent || node.value || "").trim(),
                    id: node.id || "",
                    tag: node.tagName.toLowerCase(),
                    href: node.href || "",
                    automationId: node.getAttribute("data-automation-id") || "",
                    autofillId,
                  };
                })
                .filter((node) => node.text);
            }"""
        )
    except Exception:
        return None
    for entry in entries:
        text = _norm(entry.get("text") or "")
        if text in {"sign in with email", "sign in with e mail"}:
            return entry
    return None


def _open_workday_email_sign_in_if_needed(page) -> bool:
    if _has_workday_login_controls(page):
        return False
    entry = _find_workday_email_sign_in_entry(page)
    if not entry:
        return False
    try:
        _click_button(page, entry)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def _find_workday_create_account_entry(page) -> dict[str, Any] | None:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return None
    try:
        entries = page.evaluate(
            """() => {
              const visible = (node) => {
                if (!node) return false;
                if (node.offsetParent) return true;
                const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
                return rects && rects.length > 0;
              };
              let index = 0;
              return Array.from(document.querySelectorAll("a,button,[role='button']"))
                .filter(visible)
                .map((node) => {
                  const autofillId = String(index++);
                  node.setAttribute("data-job-agent-button-index", autofillId);
                  return {
                    text: (node.textContent || node.value || "").trim(),
                    id: node.id || "",
                    tag: node.tagName.toLowerCase(),
                    href: node.href || "",
                    automationId: node.getAttribute("data-automation-id") || "",
                    autofillId,
                  };
                })
                .filter((node) => node.text);
            }"""
        )
    except Exception:
        return None
    for entry in entries:
        if _norm(entry.get("text") or "") == "create account":
            return entry
    return None


def _open_workday_create_account_from_sign_in_if_available(
    page,
    *,
    require_failure: bool = True,
) -> bool:
    if require_failure and not _workday_sign_in_failure_reason(page, allow_generic=False):
        return False
    entry = _find_workday_create_account_entry(page)
    try:
        if entry:
            try:
                _click_button(page, entry)
            except Exception:
                locator = page.get_by_text("Create Account", exact=True).first
                if not locator.count() or not locator.is_visible():
                    return False
                locator.click()
        else:
            locator = page.get_by_text("Create Account", exact=True).first
            if not locator.count() or not locator.is_visible():
                return False
            locator.click()
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def _is_workday_apply_gate(page, fields: list[dict[str, Any]] | None = None) -> bool:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return False
    entry = _find_application_entry(page)
    if not entry:
        return False
    current_fields = fields if fields is not None else _scrape_fields(page)
    meaningful_fields = _meaningful_application_fields(current_fields)
    if not meaningful_fields:
        return False
    labels = " ".join(_norm(field.get("label") or "") for field in meaningful_fields)
    if any(field.get("type") == "file" for field in meaningful_fields):
        return False
    login_markers = ("email", "username", "password")
    return all(
        any(marker in _norm(field.get("label") or "") for marker in login_markers)
        for field in meaningful_fields
    ) and "apply manually" in _norm(entry.get("text"))


def _meaningful_application_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    meaningful_fields: list[dict[str, Any]] = []
    for field in fields:
        mapping_label = " ".join(
            str(field.get(key) or "")
            for key in ["label", "id", "name", "section", "ariaLabel", "ariaDescription", "placeholder"]
        )
        normalized_label = _norm(mapping_label)
        normalized_field_label = _norm(field.get("label") or "")
        normalized_id = _norm(field.get("id") or "")
        normalized_automation = _norm(field.get("automationId") or "")
        if _is_honeypot_field(mapping_label):
            continue
        if (
            normalized_field_label == "settings"
            or normalized_id == "settingsselectorbutton"
            or normalized_automation == "utilitymenubutton"
        ):
            continue
        if normalized_label in {"search"} or normalized_label.startswith("search for jobs"):
            continue
        if field.get("type") == "file" and (
            "how well you match" in normalized_label
            or "match with this job" in normalized_label
            or normalized_field_label in {"upload your resume", "upload resume"}
        ):
            continue
        meaningful_fields.append(field)
    return meaningful_fields


def _has_application_form_context(page) -> bool:
    """Reject generic newsletters and hidden login widgets before autofilling."""
    fields = _scrape_fields(page)
    if not fields:
        return False
    meaningful_fields = _meaningful_application_fields(fields)
    if not meaningful_fields:
        return False
    if _is_workday_apply_gate(page, fields):
        return False
    entry = _find_application_entry(page)
    if _is_job_page_apply_button(page, entry, fields):
        return False
    url = str(getattr(page, "url", "") or "").lower()
    if "/embed/job_board" in url:
        return False
    if any(host in url for host in ("greenhouse.io", "ashbyhq.com", "lever.co", "myworkdayjobs.com")):
        return True
    labels = " ".join(_norm(field.get("label") or "") for field in meaningful_fields)
    if any(field.get("type") == "file" for field in meaningful_fields):
        return True
    if "password" in labels:
        return True
    identity_markers = ("first name", "last name", "phone", "resume", "curriculum vitae", "cover letter")
    return sum(marker in labels for marker in identity_markers) >= 2


def _application_host_aliases(application_url: str | None) -> set[str]:
    host = (urlparse(str(application_url or "")).hostname or "").lower()
    aliases = {host} if host else set()
    if "greenhouse.io" in host:
        aliases.update({"boards.greenhouse.io", "job-boards.greenhouse.io"})
    if "myworkdayjobs.com" in host:
        aliases.add(host)
    if "lever.co" in host:
        aliases.add(host)
    if "ashbyhq.com" in host:
        aliases.add(host)
    return {alias for alias in aliases if alias}


def _is_application_context_url(current_url: str | None, application_url: str | None) -> bool:
    current_host = (urlparse(str(current_url or "")).hostname or "").lower()
    if not current_host:
        return True
    aliases = _application_host_aliases(application_url)
    if current_host in aliases:
        return True
    if current_host.endswith(".myworkdayjobs.com") and any(host.endswith(".myworkdayjobs.com") for host in aliases):
        return True
    return False


def _install_application_navigation_guard(page, application_url: str | None) -> bool:
    """Prevent informational privacy/terms links from replacing the active form."""
    if not application_url:
        return False
    try:
        page.evaluate(
            """(payload) => {
              if (window.__jobAgentApplicationNavigationGuardInstalled) return true;
              window.__jobAgentApplicationNavigationGuardInstalled = true;
              const applicationHosts = new Set((payload.hosts || []).filter(Boolean));
              const infoPattern = /privacy|notice|policy|terms|arbitration|personnel|candidate|pdf/i;
              document.addEventListener("click", (event) => {
                const anchor = event.target && event.target.closest ? event.target.closest("a[href]") : null;
                if (!anchor) return;
                let url = null;
                try { url = new URL(anchor.href, window.location.href); } catch (_) { return; }
                if (!url || applicationHosts.has(url.hostname.toLowerCase())) return;
                const text = [
                  anchor.textContent || "",
                  anchor.getAttribute("aria-label") || "",
                  anchor.getAttribute("title") || "",
                  anchor.href || "",
                ].join(" ");
                if (!infoPattern.test(text)) return;
                event.preventDefault();
                event.stopPropagation();
                if (typeof event.stopImmediatePropagation === "function") event.stopImmediatePropagation();
              }, true);
              return true;
            }""",
            {"hosts": sorted(_application_host_aliases(application_url))},
        )
        return True
    except Exception:
        return False


def _restore_application_context_if_external(page, application_url: str | None) -> bool:
    """Return to the application form if an informational link navigated away."""
    if not application_url:
        return False
    current_url = str(getattr(page, "url", "") or "")
    if _is_application_context_url(current_url, application_url):
        return False
    try:
        page.go_back(wait_until="domcontentloaded", timeout=15000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        _open_application_form_if_needed(page)
        if _wait_for_application_form_context(page, attempts=2, delay_ms=500):
            return True
    except Exception:
        pass
    try:
        page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        _open_application_form_if_needed(page)
        return _wait_for_application_form_context(page, attempts=3, delay_ms=750)
    except Exception:
        return False


def _restore_workday_application_from_candidate_home(page, application_url: str | None) -> bool:
    """Return from empty Workday Candidate Home to the original job apply URL."""
    if not application_url:
        return False
    current_url = str(getattr(page, "url", "") or "")
    current_url_lower = current_url.lower()
    if "myworkdayjobs.com" not in current_url_lower or "/userhome" not in current_url_lower:
        return False
    text = _norm(_current_page_text(page))
    if "candidate home" not in text or "you have no applications" not in text:
        return False
    try:
        page.goto(application_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        _open_application_form_if_needed(page)
        return True
    except Exception:
        return False


def _workday_sign_in_field_set(fields: list[dict[str, Any]]) -> bool:
    meaningful_fields: list[dict[str, Any]] = []
    for field in fields:
        mapping_label = " ".join(
            str(field.get(key) or "")
            for key in ["label", "id", "name", "section", "ariaLabel", "ariaDescription", "placeholder"]
        )
        if _is_honeypot_field(mapping_label):
            continue
        meaningful_fields.append(field)
    if len(meaningful_fields) > 3:
        return False
    if any(field.get("type") == "file" for field in meaningful_fields):
        return False
    labels = [_norm(field.get("label") or "") for field in meaningful_fields]
    if any(
        "verify" in label
        or "confirm" in label
        or "new password" in label
        or "create account" in label
        for label in labels
    ):
        return False
    has_email = any("email" in label or "username" in label for label in labels)
    has_password = any(str(field.get("type") or "").lower() == "password" for field in meaningful_fields)
    return has_email and has_password


def _workday_sign_in_fill_signature(page, filled: list[dict[str, Any]]) -> str:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return ""
    labels = [_norm(item.get("label") or "") for item in filled or [] if item.get("action") != "upload"]
    labels = [label for label in labels if label]
    if len(labels) < 2:
        return ""
    if any(
        "verify" in label
        or "confirm" in label
        or "new password" in label
        or "create account" in label
        for label in labels
    ):
        return ""
    has_email = any("email" in label or "username" in label for label in labels)
    has_password = any("password" in label for label in labels)
    only_sign_in_fields = all(
        "email" in label or "username" in label or "password" in label
        for label in labels
    )
    if not has_email or not has_password or not only_sign_in_fields:
        return ""
    normalized_parts = {
        "email" if ("email" in label or "username" in label) else "password"
        for label in labels
    }
    return "|".join(sorted(normalized_parts))


def _workday_account_verification_reason(page) -> str | None:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return None
    text = _norm(_current_page_text(page))
    if not text:
        return None
    if (
        "verify your account before you sign in" in text
        or "account may need verification" in text
        or "resend account verification" in text
    ):
        return "candidate account verification required by Workday"
    return None


def _request_workday_account_verification_email(page) -> bool:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return False
    for text in ("Resend Account Verification", "Request Verification Email"):
        try:
            locator = page.get_by_text(text, exact=True).first
            if locator.count() and locator.is_visible():
                locator.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(2000)
                return True
        except Exception:
            continue
    return False


def _safe_evidence_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return str(url or "")
    path = parsed.path or ""
    if re.search(r"/activate(?:/|$)", path, re.I):
        return f"{parsed.scheme}://{parsed.netloc}/<workday-activation-link-redacted>"
    if parsed.query or parsed.fragment:
        return f"{parsed.scheme}://{parsed.netloc}{path}"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _redact_evidence_text(text: str) -> str:
    redacted = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "<email-redacted>", str(text), flags=re.I)
    redacted = re.sub(
        r"https?://[^\s\"'<>]*(?:activate)[^\s\"'<>]*",
        "<workday-activation-link-redacted>",
        redacted,
        flags=re.I,
    )
    redacted = re.sub(r"https?://[^\s\"'<>]+", lambda match: _safe_evidence_url(match.group(0)), redacted)
    return redacted


def _write_workday_account_verification_evidence(page, payload: dict[str, Any]) -> str | None:
    directory = payload.get("_runtimeScriptDir")
    if not directory:
        return None
    out = Path(directory) / "workday-account-verification.txt"
    try:
        state = page.evaluate(
            """() => ({
              url: window.location.href,
              title: document.title || "",
              text: (document.body && document.body.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 20000),
            })"""
        )
        screenshot = None
        try:
            screenshot_path = Path(directory) / "workday-account-verification.png"
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot = str(screenshot_path)
        except Exception:
            screenshot = None
        text = _redact_evidence_text(str(state.get("text") or ""))
        url = _safe_evidence_url(str(state.get("url") or ""))
        out.write_text(
            "\n".join(
                [
                    "account_verification: handled",
                    f"url: {url}",
                    f"title: {state.get('title') or ''}",
                    f"screenshot: {screenshot or 'not captured'}",
                    "",
                    "page_text_head:",
                    text[:4000],
                ]
            )
        )
        return str(out)
    except Exception:
        return None


def _verify_workday_candidate_account_if_configured(
    page,
    *,
    requested_after_ns: int,
    payload: dict[str, Any] | None = None,
) -> bool:
    if not _workday_account_verification_reason(page):
        return False
    if not _request_workday_account_verification_email(page):
        return False
    query = str(
        os.getenv("JOB_AGENT_WORKDAY_ACCOUNT_VERIFICATION_QUERY")
        or '("Verify your account" OR "Account Verification" OR "Workday") newer_than:1d'
    )
    link = _email_verification_link(
        requested_after_ns=requested_after_ns,
        query=query,
        url_pattern=r"workday|myworkdayjobs",
    )
    if not link:
        return False
    try:
        page.goto(link, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        if payload is not None:
            artifact = _write_workday_account_verification_evidence(page, payload)
            if artifact:
                print(f"{WORKDAY_ACCOUNT_VERIFIED_LINE_PREFIX} {artifact}")
        return True
    except Exception:
        return False


def _workday_transient_error_page(page) -> bool:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return False
    text = _norm(_current_page_text(page))
    return "something went wrong" in text and "refresh the page" in text


def _workday_sign_in_failure_reason(page, *, allow_generic: bool = True) -> str | None:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return None
    if _workday_account_verification_reason(page):
        return None
    fields = _scrape_fields(page)
    if not _workday_sign_in_field_set(fields):
        return None
    text = _norm(_current_page_text(page))
    if not text or "sign in" not in text:
        return None
    explicit_patterns = {
        "wrong email address or password": "candidate account sign-in rejected by Workday: wrong email address or password",
        "your account might be locked": "candidate account sign-in rejected by Workday: account might be locked",
        "account might be locked": "candidate account sign-in rejected by Workday: account might be locked",
        "invalid username or password": "candidate account sign-in rejected by Workday: invalid username or password",
        "incorrect email or password": "candidate account sign-in rejected by Workday: incorrect email or password",
    }
    for pattern, reason in explicit_patterns.items():
        if pattern in text:
            return reason
    if "please enter a valid email" in text or "please enter your password" in text:
        return "candidate account sign-in rejected by Workday: sign-in form remained invalid after automation"
    if not allow_generic:
        return None
    return "candidate account sign-in rejected by Workday"


def _workday_create_account_failure_reason(page) -> str | None:
    url = str(getattr(page, "url", "") or "").lower()
    if "myworkdayjobs.com" not in url:
        return None
    fields = _scrape_fields(page)
    password_count = sum(
        1 for field in fields if str(field.get("type") or "").lower() == "password"
    )
    labels = [_norm(field.get("label") or "") for field in fields]
    has_email = any("email" in label or "username" in label for label in labels)
    if not has_email or password_count < 2:
        return None
    text = _norm(_current_page_text(page))
    if not text or "create account" not in text:
        return None
    if "please check the box to continue" in text:
        return "candidate account creation blocked by required privacy consent checkbox"
    if "passwords do not match" in text:
        return "candidate account creation rejected by Workday: passwords do not match"
    if "password requirements" in text and "error" in text:
        return "candidate account creation rejected by Workday: password requirements not satisfied"
    return "candidate account creation blocked by Workday"


def _page_did_not_advance(
    step_before: str,
    step_after: str,
    fields_before_next: tuple[str, ...],
    fields_after_next: tuple[str, ...],
) -> bool:
    if fields_after_next != fields_before_next:
        return False
    if step_before and step_after and step_after != step_before:
        return False
    return bool(fields_after_next or step_before or step_after)


def _wait_for_application_form_context(
    page,
    attempts: int = 5,
    delay_ms: int = 1000,
) -> bool:
    for attempt in range(max(1, attempts)):
        _open_embedded_application_iframe_if_needed(page)
        if _has_application_form_context(page):
            return True
        if attempt == max(1, attempts) - 1:
            break
        _dismiss_cookie_banner(page)
        _open_application_form_if_needed(page)
        try:
            page.wait_for_timeout(delay_ms)
        except Exception:
            pass
    return _has_application_form_context(page)


def _sign_in_to_candidate_home_if_available(page, profile: dict[str, Any]) -> bool:
    """Authenticate through Workday Candidate Home before opening the application."""
    email = str(profile.get("email") or "").strip()
    password = _candidate_account_password(profile)
    if not email or not password:
        return False
    utility_sign_in = page.locator(
        _attr_selector("data-automation-id", "utilityButtonSignIn")
    ).first
    try:
        if not utility_sign_in.count() or not utility_sign_in.is_visible():
            return False
        utility_sign_in.click(force=True)
        page.wait_for_timeout(3000)
        email_field = page.locator(_attr_selector("data-automation-id", "email")).first
        password_field = page.locator(_attr_selector("data-automation-id", "password")).first
        if not email_field.count() or not password_field.count():
            return False
        email_field.fill(email)
        email_field.press("Tab")
        password_field.fill(password)
        password_field.press("Tab")
        overlays = page.locator(
            _attr_selector("data-automation-id", "click_filter")
            + _attr_selector("aria-label", "Sign In")
        )
        clicked = False
        for index in range(overlays.count() - 1, -1, -1):
            overlay = overlays.nth(index)
            if overlay.is_visible():
                overlay.click(force=True)
                clicked = True
                break
        if not clicked:
            page.locator(
                _attr_selector("data-automation-id", "signInSubmitButton")
            ).first.click(force=True)
        for _ in range(24):
            page.wait_for_timeout(500)
            text = _norm(_current_page_text(page))
            if "candidate home" in text and "sign in" not in text:
                return True
            submit = page.locator(
                _attr_selector("data-automation-id", "signInSubmitButton")
            ).first
            if not submit.count() or not submit.is_visible():
                return True
    except Exception:
        return False
    return False


def _has_workday_login_controls(page) -> bool:
    try:
        return bool(
            page.locator(_attr_selector("data-automation-id", "signInSubmitButton")).count()
            and page.locator(_attr_selector("data-automation-id", "email")).count()
            and page.locator(_attr_selector("data-automation-id", "password")).count()
        )
    except Exception:
        return False


def _switch_to_candidate_sign_in_if_needed(page) -> bool:
    """Use the existing Workday account when the create-account form is shown."""
    verify_password = page.locator(
        _attr_selector("data-automation-id", "verifyPassword")
    ).first
    sign_in_link = page.locator(
        _attr_selector("data-automation-id", "signInLink")
    ).first
    try:
        if not verify_password.count() or not verify_password.is_visible():
            return False
        if not sign_in_link.count() or not sign_in_link.is_visible():
            return False
        sign_in_link.click(force=True)
        page.wait_for_timeout(500)
        return True
    except Exception:
        return False


def _find_application_entry(page) -> dict[str, Any] | None:
    try:
        entries = page.evaluate(
            """() => {
              const visible = (node) => {
                if (!node) return false;
                if (node.offsetParent) return true;
                const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
                return rects && rects.length > 0;
              };
              return Array.from(document.querySelectorAll("a,button"))
                .filter(visible)
                .map((node) => ({
                  text: (node.textContent || node.value || "").trim(),
                  id: node.id || "",
                  tag: node.tagName.toLowerCase(),
                  href: node.href || "",
                  inForm: Boolean(node.closest("form")),
                  accessibilitySkip: node.id === "accessibilitySkipToMainContent"
                    || (node.getAttribute("data-automation-id") || "").toLowerCase().includes("accessibilityskip")
                    || /^\\s*skip\\s+to\\b/i.test(node.textContent || ""),
                }))
                .filter((node) => node.text);
            }"""
        )
    except Exception:
        return None
    for entry in entries:
        if entry.get("accessibilitySkip"):
            continue
        text = _norm(entry["text"])
        href = str(entry.get("href") or "").lower()
        if text == "apply manually" or "/apply/applymanually" in href:
            return entry
    for entry in entries:
        if entry.get("accessibilitySkip"):
            continue
        text = _norm(entry["text"])
        href = str(entry.get("href") or "").lower()
        if text in {
            "application",
            "apply for this job",
            "apply now",
            "autofill with resume",
            "i m interested",
            "i am interested",
            "interested",
        } or (
            text in {"apply", "apply now"}
            and (
                "/application" in href
                or "/apply" in href
                or entry.get("tag") == "button"
            )
        ):
            return entry
    return None


def _find_button(page, kind: str) -> dict[str, Any] | None:
    buttons = page.evaluate(
        """() => {
          const visible = (node) => {
            if (!node) return false;
            if (node.offsetParent) return true;
            const rects = typeof node.getClientRects === "function" ? node.getClientRects() : [];
            return rects && rects.length > 0;
          };
          let index = 0;
          return Array.from(document.querySelectorAll("button, input[type='button'], input[type='submit'], a"))
            .filter(visible)
            .map((node) => {
              const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : { top: 0 };
              const autofillId = String(index++);
              node.setAttribute("data-job-agent-button-index", autofillId);
              return {
                text: (node.textContent || node.value || "").trim(),
                id: node.id || "",
                className: typeof node.className === "string" ? node.className : "",
                title: node.getAttribute("title") || "",
                ariaLabel: node.getAttribute("aria-label") || "",
                automationId: node.getAttribute("data-automation-id") || "",
                tag: node.tagName.toLowerCase(),
                type: (node.getAttribute("type") || "").toLowerCase(),
                href: node.href || "",
                inForm: Boolean(node.closest("form")),
                inDatepicker: Boolean(node.closest([
                  ".ui-datepicker",
                  ".datepicker",
                  ".flatpickr-calendar",
                  ".react-datepicker",
                  "[class*='datepicker' i]",
                  "[id*='datepicker' i]",
                  "[class*='calendar' i]",
                  "[id*='calendar' i]"
                ].join(","))),
                y: rect.top + (window.scrollY || 0),
                autofillId,
              };
            })
            .filter((node) => node.text);
        }"""
    )
    if kind == "next":
        next_re = re.compile(
            r"^\s*(next|continue|save\s+and\s+continue|create\s+account|sign\s+up|sign\s+in|->|→|step\s|\d+\s*/\s*\d+|forward|\u4e0b\u4e00\u6b65|\u7ee7\u7eed|\u4fdd\u5b58\u5e76\u7ee7\u7eed)",
            re.I,
        )
        submit_re = re.compile(
            r"(submit|apply|send\s+application|complete\s+application|finish|submit\s+application|\u63d0\u4ea4(?:\u7533\u8bf7)?|\u5b8c\u6210\u7533\u8bf7)",
            re.I,
        )
        candidates = [
            button
            for button in buttons
            if next_re.search(button["text"])
            and not submit_re.search(button["text"])
            and not _is_calendar_navigation_button(button)
        ]
        if not candidates:
            return None

        def next_score(button: dict[str, Any]) -> tuple[int, float]:
            score = 0
            automation_id = _norm(button.get("automationId"))
            if automation_id in {"signinsubmitbutton", "createaccountsubmitbutton"}:
                score += 300
            if button.get("inForm"):
                score += 100
            if button.get("type") == "submit":
                score += 80
            if button.get("tag") != "a":
                score += 20
            return score, float(button.get("y") or 0)

        return max(candidates, key=next_score)
    submit_re = re.compile(
        r"(submit|apply|send\s+application|complete\s+application|finish|submit\s+application|\u63d0\u4ea4(?:\u7533\u8bf7)?|\u5b8c\u6210\u7533\u8bf7)",
        re.I,
    )
    candidates = [
        button
        for button in buttons
        if submit_re.search(button["text"])
        and not (
            _norm(button.get("text")) in {"apply", "apply now", "apply manually", "autofill with resume"}
            and not button.get("inForm")
            and str(button.get("type") or "").lower() != "submit"
        )
    ]
    if not candidates:
        return None

    def submit_score(button: dict[str, Any]) -> tuple[int, float]:
        tag = str(button.get("tag") or "")
        button_type = str(button.get("type") or "")
        score = 0
        if tag != "a":
            score += 100
        if button_type == "submit":
            score += 100
        if button.get("inForm"):
            score += 80
        if re.search(
            r"submit\s+application|complete\s+application|send\s+application|\u63d0\u4ea4(?:\u7533\u8bf7)?|\u5b8c\u6210\u7533\u8bf7",
            str(button.get("text") or ""),
            re.I,
        ):
            score += 50
        return score, float(button.get("y") or 0)

    return max(candidates, key=submit_score)


def _is_job_page_apply_button(
    page,
    button: dict[str, Any] | None,
    fields: list[dict[str, Any]] | None = None,
) -> bool:
    text = _norm(button.get("text")) if button else ""
    current_fields = fields if fields is not None else _scrape_fields(page)
    meaningful_fields = _meaningful_application_fields(current_fields)
    non_form_apply_entry = bool(button and not button.get("inForm"))
    return bool(
        button
        and text in {"apply", "apply now", "apply manually", "autofill with resume"}
        and (
            not meaningful_fields
            or (non_form_apply_entry and _only_resume_match_probe_fields(meaningful_fields))
        )
    )


def _only_resume_match_probe_fields(fields: list[dict[str, Any]]) -> bool:
    if not fields:
        return False
    for field in fields:
        if field.get("type") != "file":
            return False
        mapping_label = " ".join(
            str(field.get(key) or "")
            for key in ["label", "id", "name", "section", "ariaLabel", "ariaDescription", "placeholder"]
        )
        normalized_label = _norm(mapping_label)
        if normalized_label and not (
            "how well you match" in normalized_label
            or "match with this job" in normalized_label
            or "upload resume" in normalized_label
            or "upload your resume" in normalized_label
        ):
            return False
    return True


def _click_button(page, button: dict[str, Any]) -> None:
    _dismiss_cookie_banner(page)

    def click_with_fallback(locator) -> None:
        label = str(button.get("text") or "").strip()
        try:
            local_overlay = locator.locator("xpath=..").locator(
                _attr_selector("data-automation-id", "click_filter")
            ).first
            if local_overlay.count() and local_overlay.is_visible():
                local_overlay.click(force=True)
                return
        except Exception:
            pass
        if label:
            overlay_selector = (
                _attr_selector("data-automation-id", "click_filter")
                + _attr_selector("aria-label", label)
            )
            overlay = page.locator(overlay_selector).last
            try:
                if overlay.count() and overlay.is_visible():
                    overlay.click(force=True)
                    return
            except Exception:
                pass
        try:
            locator.click()
            return
        except Exception as initial_error:
            try:
                locator.click(force=True)
                return
            except Exception:
                raise initial_error

    if button.get("autofillId"):
        click_with_fallback(
            page.locator(_attr_selector("data-job-agent-button-index", button["autofillId"])).first
        )
        return
    if button.get("id"):
        click_with_fallback(page.locator(_attr_selector("id", button["id"])).first)
        return
    if button.get("href") and not _is_noop_href(button.get("href")):
        clicked = page.evaluate(
            """(href) => {
              const link = Array.from(document.querySelectorAll("a")).find((node) => node.href === href);
              if (!link) return false;
              link.click();
              return true;
            }""",
            button["href"],
        )
        if clicked:
            return
    click_with_fallback(page.get_by_text(button["text"], exact=False).first)


def _is_calendar_navigation_button(button: dict[str, Any]) -> bool:
    if button.get("inDatepicker"):
        return True
    haystack = " ".join(
        str(button.get(key) or "")
        for key in ("id", "className", "title", "ariaLabel", "href")
    ).lower()
    return any(token in haystack for token in ("datepicker", "date-picker", "calendar", "ui-datepicker"))


def _is_noop_href(href: object) -> bool:
    value = str(href or "").strip()
    return not value or value == "#" or value.endswith("#")


def _dismiss_cookie_banner(page) -> bool:
    """Dismiss common cookie-consent banners and modals before interacting with the page.

    Handles Waymo's Bootstrap ``.consent-modal`` (with ``aria-label="Cookie consent"``)
    in addition to the generic banner buttons.  Prefer decline/reject options where
    available; accept only when necessary to proceed.
    """
    try:
        dismissed = page.evaluate(
            """() => {
              const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
              const norm = (s) => (s || "").toLowerCase().replace(/\\s+/g, " ").trim();
              const matchesText = (node, texts) => {
                const data = node.getAttribute("data-ui") || "";
                const text = norm(node.textContent || node.value || node.getAttribute("aria-label") || "");
                return texts.includes(data) || texts.includes(text);
              };

              // 1. Explicit cookie-consent modal (e.g. Waymo careers page).
              const modalSelectors = [
                '.consent-modal',
                '[aria-label="Cookie consent"]',
                '[data-controller*="explicit-consent-modal"]',
              ];
              let modal = null;
              for (const selector of modalSelectors) {
                modal = document.querySelector(selector);
                if (modal && visible(modal)) break;
                modal = null;
              }
              if (modal) {
                const modalButtons = Array.from(
                  modal.querySelectorAll('button, [role="button"], a[role="button"]')
                );
                const declineTexts = ["decline all", "reject all", "i do not accept", "decline", "reject"];
                const acceptTexts = ["accept all", "i accept", "accept cookies", "allow cookies", "accept"];
                const declineBtn = modalButtons.find((node) => matchesText(node, declineTexts));
                if (declineBtn) { declineBtn.click(); return true; }
                const acceptBtn = modalButtons.find((node) => matchesText(node, acceptTexts));
                if (acceptBtn) { acceptBtn.click(); return true; }
                const manageBtn = modalButtons.find((node) => matchesText(node, ["manage cookies"]));
                if (manageBtn) { manageBtn.click(); return true; }
              }

              // 2. Generic cookie-banner buttons.
              const selectors = [
                '[data-ui="cookie-consent-decline"]',
                '[data-ui="cookie-consent-accept"]',
                'button',
                '[role="button"]',
              ];
              const nodes = Array.from(new Set(selectors.flatMap((selector) => Array.from(document.querySelectorAll(selector)))));
              const button = nodes.find((node) => {
                if (!visible(node)) return false;
                const data = node.getAttribute("data-ui") || "";
                const text = norm(node.textContent || node.value || node.getAttribute("aria-label") || "");
                return data === "cookie-consent-decline"
                  || data === "cookie-consent-accept"
                  || ["decline all", "reject all", "accept all"].includes(text);
              });
              if (!button) return false;
              button.click();
              return true;
            }"""
        )
        if dismissed:
            page.wait_for_timeout(500)
            return True
    except Exception:
        return False
    return False

def _wait_for_submit_settle(page, timeout_ms: int = 35000) -> None:
    elapsed = 0
    interval = 1000
    while elapsed < timeout_ms:
        page.wait_for_timeout(interval)
        elapsed += interval
        text = _norm(_current_page_text(page))
        if "submitting" in text:
            continue
        if _detect_submission_confirmation(page) or _detect_email_verification_request(page):
            return
        processing_error = _detect_submission_processing_error(page)
        # Ashby keeps an invisible CAPTCHA container in the DOM during its
        # success-page transition. Do not misclassify that as an immediate
        # terminal error before confirmation has a chance to render.
        if processing_error and not _is_ambient_captcha_presence(processing_error):
            return


def _current_page_text(page) -> str:
    try:
        return str(
            page.evaluate(
                """() => (document.body && document.body.innerText || "")
                  .replace(/\\s+/g, " ")
                  .trim()
                  .slice(0, 60000)"""
            )
            or ""
        )
    except Exception:
        return ""


def _current_application_step(page) -> str:
    try:
        return str(
            page.evaluate(
                """() => {
                  const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                  const known = new Set([
                    "My Information", "My Experience", "Application Questions",
                    "Voluntary Disclosures", "Self Identify", "Review"
                  ]);
                  return Array.from(document.querySelectorAll("h1,h2,h3,legend"))
                    .filter(visible)
                    .map((node) => (node.textContent || "").replace(/\\s+/g, " ").trim())
                    .find((text) => known.has(text)) || "";
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def _visible_form_validation_errors(page) -> list[str]:
    try:
        return list(
            page.evaluate(
                """() => {
                  const visible = (node) => !!(node && (node.offsetParent || node.getClientRects().length));
                  const texts = Array.from(document.querySelectorAll(
                    '[data-automation-id="errorMessage"], [role="alert"]'
                  ))
                    .filter(visible)
                    .map((node) => (node.innerText || node.textContent || "").replace(/\\s+/g, " ").trim())
                    .filter((text) => text && /error|required|must have a value|invalid/i.test(text));
                  return texts
                    .filter((text, index) => texts.indexOf(text) === index)
                    .sort((left, right) => left.length - right.length)
                    .slice(0, 10);
                }"""
            )
            or []
        )
    except Exception:
        return []


def _recover_from_validation_errors(
    page,
    profile: dict[str, Any],
    resume_file: str | None,
    cover_letter_file: str | None,
    *,
    action_runner: RuntimeActionRunner | None = None,
    application_url: str = "",
    page_index: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Re-scrape and retry a page after client-side form validation fails.

    Required controls may be mounted only after the first submit attempt, or a
    framework may reject a value it appeared to accept.  Retrying from the live
    DOM makes the recovery work across form vendors and keeps the final failure
    tied to the browser's own validation signal.
    """
    recovered_filled: list[dict[str, Any]] = []
    recovered_review: list[dict[str, Any]] = []
    errors = _visible_form_validation_errors(page)
    for _ in range(max(1, _self_heal_passes() - 1)):
        if not errors:
            break
        result = _runtime_agent_action(
            action_runner,
            "ats_fill_fields",
            "write",
            {
                "phase": "validation_recovery",
                "page_index": page_index,
                "application_url": application_url,
            },
            lambda: _fill_page(
                page,
                profile,
                resume_file,
                cover_letter_file,
            ),
        )
        _extend_unique_filled(recovered_filled, result["filled"])
        recovered_review.extend(result["review"])
        if any(item.get("blocking", True) for item in result["review"]):
            break
        next_button = _find_button(page, kind="next")
        if not next_button:
            break
        try:
            _runtime_agent_action(
                action_runner,
                "ats_advance_page",
                "write",
                {
                    "phase": "validation_recovery_navigation",
                    "page_index": page_index,
                    "application_url": application_url,
                    "blocking_review_count": 0,
                },
                lambda: _click_button(page, next_button),
            )
        except Exception:
            break
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
        errors = _visible_form_validation_errors(page)
    return recovered_filled, recovered_review, errors


def _detect_ats(url: str | None) -> str:
    return detect_ats_from_url(url)


def _discover_captcha(page) -> dict[str, Any] | None:
    try:
        return page.evaluate(
            """() => {
              const attr = (node, name) => node && node.getAttribute ? node.getAttribute(name) : "";
              const visibleCaptchaFrame = (frame) => {
                if (!frame || !frame.src) return false;
                if (/[?&]size=invisible(?:&|$)/.test(frame.src)) return false;
                const style = window.getComputedStyle ? window.getComputedStyle(frame) : null;
                const rect = frame.getBoundingClientRect ? frame.getBoundingClientRect() : { width: 0, height: 0 };
                return rect.width >= 40 && rect.height >= 40 && !(style && (style.display === "none" || style.visibility === "hidden"));
              };
              const keyFromUrl = (raw) => {
                try {
                  const url = new URL(raw, window.location.href);
                  return url.searchParams.get("k") || url.searchParams.get("sitekey") || url.searchParams.get("render") || "";
                } catch (e) {
                  return "";
                }
              };
              const urlParam = (raw, name) => {
                try {
                  const url = new URL(raw, window.location.href);
                  return url.searchParams.get(name) || "";
                } catch (e) {
                  return "";
                }
              };
              const cookieValue = (name) => {
                const found = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(name + "="));
                return found || "";
              };
              const scriptText = Array.from(document.querySelectorAll("script:not([src])")).map((script) => script.textContent || "").join("\\n");
              const currentURL = window.location.href;
              const capturedTurnstile = window.__jobAgentTurnstileCapture || {};
              const cloudflareText = `${document.title || ""} ${document.body && document.body.innerText || ""}`;
              const cloudflareChallengePage = /(^|\\.)cloudflare\\.com$/i.test(window.location.hostname)
                || /just a moment|checking your browser|enable javascript and cookies|waiting room|cloudflare/i.test(cloudflareText)
                || /__cf_chl_|cf_chl|cdn-cgi\\/challenge-platform/i.test(currentURL + "\\n" + scriptText);
              const htmlPageBase64 = cloudflareChallengePage && document.documentElement
                ? btoa(unescape(encodeURIComponent(document.documentElement.outerHTML.slice(0, 600000))))
                : "";
              const dataDomeFrame = Array.from(document.querySelectorAll("iframe[src*='captcha-delivery.com'], iframe[src*='geo.captcha-delivery.com']")).find((frame) => frame.src);
              const dataDomeScript = Array.from(document.querySelectorAll("script[src*='captcha-delivery.com']")).find((script) => script.src);
              const dataDomeCaptchaUrl = currentURL.includes("captcha-delivery.com") ? currentURL : (dataDomeFrame ? dataDomeFrame.src : (dataDomeScript ? dataDomeScript.src : ""));
              if (dataDomeCaptchaUrl || /\\bdatadome\\b/i.test(document.body && document.body.innerText || "")) {
                const ddmCid = (scriptText.match(/cid\\s*[:=]\\s*['"]([^'"]+)['"]/) || [])[1] || "";
                const datadomeCookie = cookieValue("datadome") || (ddmCid ? "datadome=" + ddmCid : "");
                if (dataDomeCaptchaUrl && datadomeCookie) {
                  return { kind: "datadome", websiteURL: currentURL, captchaUrl: dataDomeCaptchaUrl, datadomeCookie, datadomeVersion: "new", userAgent: navigator.userAgent };
                }
              }
              const findSiteKey = (selector) => {
                const node = document.querySelector(selector);
                return node ? attr(node, "data-sitekey") || attr(node, "sitekey") || "" : "";
              };
              const turnstileNode = document.querySelector(".cf-turnstile[data-sitekey], [data-sitekey][class*='turnstile' i], [data-sitekey][id*='turnstile' i]");
              const turnstileKey = turnstileNode ? attr(turnstileNode, "data-sitekey") || attr(turnstileNode, "sitekey") || "" : "";
              if (turnstileKey) return {
                kind: "turnstile",
                websiteURL: window.location.href,
	                websiteKey: turnstileKey,
	                pageAction: attr(turnstileNode, "data-action") || attr(turnstileNode, "action") || capturedTurnstile.pageAction || "",
	                data: attr(turnstileNode, "data-cdata") || attr(turnstileNode, "cdata") || capturedTurnstile.data || "",
	                pageData: capturedTurnstile.pageData || "",
	                cloudflareTaskType: (capturedTurnstile.pageData || cloudflareChallengePage) ? "cf_clearance" : "",
	                htmlPageBase64,
	                apiJsUrl: capturedTurnstile.apiJsUrl || "",
	                userAgent: navigator.userAgent,
	              };
              if (capturedTurnstile.websiteKey) return {
                kind: "turnstile",
                websiteURL: window.location.href,
                websiteKey: capturedTurnstile.websiteKey,
                pageAction: capturedTurnstile.pageAction || "",
                data: capturedTurnstile.data || "",
                pageData: capturedTurnstile.pageData || "",
                cloudflareTaskType: (capturedTurnstile.pageData || cloudflareChallengePage) ? "cf_clearance" : "",
                htmlPageBase64,
                apiJsUrl: capturedTurnstile.apiJsUrl || "",
                userAgent: navigator.userAgent,
              };
              const turnstileFrame = Array.from(document.querySelectorAll("iframe[src*='challenges.cloudflare.com']")).find((frame) => frame.src);
              const turnstileFrameKey = turnstileFrame ? keyFromUrl(turnstileFrame.src) : "";
              if (turnstileFrameKey) return {
                kind: "turnstile",
                websiteURL: window.location.href,
	                websiteKey: turnstileFrameKey,
	                pageAction: urlParam(turnstileFrame.src, "action") || capturedTurnstile.pageAction || "",
	                data: urlParam(turnstileFrame.src, "cData") || urlParam(turnstileFrame.src, "cdata") || capturedTurnstile.data || "",
	                pageData: capturedTurnstile.pageData || urlParam(turnstileFrame.src, "chlPageData") || "",
	                cloudflareTaskType: (capturedTurnstile.pageData || urlParam(turnstileFrame.src, "chlPageData") || cloudflareChallengePage) ? "cf_clearance" : "",
	                htmlPageBase64,
	                apiJsUrl: capturedTurnstile.apiJsUrl || "",
	                userAgent: navigator.userAgent,
	              };
              const hcaptchaNode = document.querySelector(".h-captcha[data-sitekey], [data-sitekey][class*='h-captcha' i], [data-sitekey][id*='h-captcha' i], [data-sitekey][class*='hcaptcha' i], [data-sitekey][id*='hcaptcha' i]");
              const hcaptchaKey = hcaptchaNode ? attr(hcaptchaNode, "data-sitekey") || attr(hcaptchaNode, "sitekey") || "" : "";
              if (hcaptchaKey) return {
                kind: "hcaptcha",
                websiteURL: currentURL,
                websiteKey: hcaptchaKey,
                invisible: attr(hcaptchaNode, "data-size") === "invisible",
                data: attr(hcaptchaNode, "data-rqdata") || attr(hcaptchaNode, "rqdata") || "",
                callback: attr(hcaptchaNode, "data-callback") || "",
                userAgent: navigator.userAgent,
                cookies: document.cookie || "",
              };
              const hcaptchaFrame = Array.from(document.querySelectorAll("iframe[src*='hcaptcha.com']")).find((frame) => frame.src);
              const hcaptchaFrameKey = hcaptchaFrame ? keyFromUrl(hcaptchaFrame.src) : "";
              if (hcaptchaFrameKey) return { kind: "hcaptcha", websiteURL: currentURL, websiteKey: hcaptchaFrameKey, userAgent: navigator.userAgent, cookies: document.cookie || "" };
              const funToken = document.querySelector("#verification-token, #FunCaptcha-Token, input[name='fc-token'], input[name='verification-token']");
              const funTokenValue = funToken ? String(funToken.value || attr(funToken, "value") || "") : "";
              const funParams = Object.fromEntries(funTokenValue.split("|").map((item) => item.split("=")).filter((item) => item.length >= 2));
              const funFrame = Array.from(document.querySelectorAll("iframe[src*='arkoselabs.com'], iframe[src*='funcaptcha.com']")).find((frame) => frame.src);
              const funPublicKey = funParams.pk || findSiteKey("[data-pkey], [data-pk], [data-public-key], [data-fc-public-key]") || (funFrame ? urlParam(funFrame.src, "public_key") || urlParam(funFrame.src, "pk") : "");
              let funSubdomain = "";
              try {
                funSubdomain = funParams.surl ? new URL(decodeURIComponent(funParams.surl), currentURL).hostname : (funFrame ? new URL(funFrame.src, currentURL).hostname : "");
              } catch (e) {
                funSubdomain = "";
              }
              const funBlobNode = document.querySelector("[data-blob], [data-fc-blob]");
              const funBlob = funBlobNode ? attr(funBlobNode, "data-blob") || attr(funBlobNode, "data-fc-blob") : "";
              if (funPublicKey) return { kind: "funcaptcha", websiteURL: currentURL, websitePublicKey: funPublicKey, funcaptchaApiJSSubdomain: funSubdomain, data: funBlob ? JSON.stringify({ blob: funBlob }) : "", userAgent: navigator.userAgent };
              const geetestNode = document.querySelector("[data-gt], [data-geetest-gt]");
              const geetestGt = geetestNode ? attr(geetestNode, "data-gt") || attr(geetestNode, "data-geetest-gt") : ((scriptText.match(/["']gt["']\\s*:\\s*["']([^"']+)["']/) || [])[1] || "");
              const geetestChallenge = geetestNode ? attr(geetestNode, "data-challenge") || attr(geetestNode, "data-geetest-challenge") : ((scriptText.match(/["']challenge["']\\s*:\\s*["']([^"']+)["']/) || [])[1] || "");
              if (geetestGt) return { kind: "geetest", websiteURL: currentURL, gt: geetestGt, challenge: geetestChallenge, version: 3, userAgent: navigator.userAgent };
              const enterpriseFrame = Array.from(document.querySelectorAll("iframe[src*='recaptcha/enterprise']")).find(visibleCaptchaFrame);
              const enterpriseFrameKey = enterpriseFrame ? keyFromUrl(enterpriseFrame.src) : "";
              const enterpriseFramePayload = enterpriseFrame ? urlParam(enterpriseFrame.src, "s") : "";
              const enterpriseScript = Array.from(document.querySelectorAll("script[src*='recaptcha/enterprise']")).find((script) => script.src);
              const enterpriseRenderKey = enterpriseScript ? keyFromUrl(enterpriseScript.src) : "";
              const enterpriseNode = document.querySelector(".g-recaptcha[data-sitekey], [data-sitekey][class*='recaptcha' i], [data-sitekey][id*='recaptcha' i]");
              const enterprisePayload = {};
              if (enterpriseNode) {
                Array.from(enterpriseNode.attributes || []).forEach((attribute) => {
                  if (attribute.name.startsWith("data-") && attribute.name !== "data-sitekey") {
                    enterprisePayload[attribute.name.slice(5)] = attribute.value;
                  }
                });
              }
              const greenhouseEnterpriseKey = window.ENV && window.ENV.GOOGLE_RECAPTCHA_INVISIBLE_KEY;
              const greenhouseEnterpriseEndpoint = String(window.ENV && window.ENV.GOOGLE_RECAPTCHA_ENDPOINT || "");

              // ── Greenhouse / job-boards specific ──────────────────────
              // Greenhouse forms load recaptcha/enterprise.js which would
              // match enterpriseRenderKey below, but the script render key
              // may differ from ENV.GOOGLE_RECAPTCHA_INVISIBLE_KEY and the
              // action would be "verify" instead of "apply_to_job".
              // Check for Greenhouse markers FIRST so we use the right
              // sitekey, pageAction, and minScore.
              const isGreenhouseBoard = /(^|\\.)greenhouse\\.io$/i.test(window.location.hostname)
                || /(^|\.)greenhouse\.com$/i.test(window.location.hostname);
              if (isGreenhouseBoard && greenhouseEnterpriseKey) {
                const ghEndpoint = greenhouseEnterpriseEndpoint || "";
                const ghKind = ghEndpoint.includes("recaptcha/enterprise")
                  ? "recaptchaV3Enterprise"
                  : "recaptchaV3";
                return {
                  kind: ghKind,
                  websiteURL: currentURL,
                  websiteKey: greenhouseEnterpriseKey,
                  pageAction: "apply_to_job",
                  minScore: 0.7,
                  userAgent: navigator.userAgent,
                };
              }
              if (enterpriseFramePayload) enterprisePayload.s = enterpriseFramePayload;
              if (enterpriseFrameKey) return { kind: "recaptchaV2Enterprise", websiteURL: currentURL, websiteKey: enterpriseFrameKey, enterprisePayload, invisible: /[?&]size=invisible(?:&|$)/.test(enterpriseFrame.src), userAgent: navigator.userAgent };
              const isAshby = /(^|\\.)ashbyhq\\.com$/i.test(window.location.hostname);
              const v3Action = isAshby ? "job_apply" : "verify";
              const v3MinScore = isAshby ? 0.7 : null;
              if (enterpriseRenderKey && enterpriseRenderKey !== "explicit") return { kind: "recaptchaV3Enterprise", websiteURL: currentURL, websiteKey: enterpriseRenderKey, pageAction: v3Action, minScore: v3MinScore, userAgent: navigator.userAgent };
              const recaptchaV3Script = Array.from(document.querySelectorAll("script[src*='recaptcha/api.js?render=']")).find((script) => script.src);
              const recaptchaV3Key = recaptchaV3Script ? keyFromUrl(recaptchaV3Script.src) : "";
              if (recaptchaV3Key && recaptchaV3Key !== "explicit") return { kind: "recaptchaV3", websiteURL: currentURL, websiteKey: recaptchaV3Key, pageAction: v3Action, minScore: v3MinScore, userAgent: navigator.userAgent };
              const recaptchaNode = document.querySelector(".g-recaptcha[data-sitekey], [data-sitekey][class*='recaptcha' i], [data-sitekey][id*='recaptcha' i]");
              const recaptchaKey = recaptchaNode ? attr(recaptchaNode, "data-sitekey") || attr(recaptchaNode, "sitekey") || "" : "";
              if (recaptchaKey) {
                return {
                  kind: "recaptchaV2",
                  websiteURL: window.location.href,
                  websiteKey: recaptchaKey,
                  invisible: attr(recaptchaNode, "data-size") === "invisible",
                  callback: attr(recaptchaNode, "data-callback"),
                  userAgent: navigator.userAgent,
                  cookies: document.cookie || "",
                  recaptchaDataSValue: attr(recaptchaNode, "data-s") || "",
                };
              }
              const recaptchaFrame = Array.from(document.querySelectorAll("iframe[src*='recaptcha']")).find(visibleCaptchaFrame);
              const recaptchaFrameKey = recaptchaFrame ? keyFromUrl(recaptchaFrame.src) : "";
              if (recaptchaFrameKey) return {
                kind: "recaptchaV2",
                websiteURL: window.location.href,
                websiteKey: recaptchaFrameKey,
                invisible: false,
                callback: "",
                userAgent: navigator.userAgent,
                cookies: document.cookie || "",
                recaptchaDataSValue: urlParam(recaptchaFrame.src, "s"),
              };
              return null;
            }"""
        )
    except Exception:
        return None


def _capmonster_task_for(challenge: dict[str, Any] | None) -> dict[str, Any] | None:
    if not challenge or not challenge.get("websiteURL"):
        return None
    if challenge.get("kind") == "turnstile":
        if not challenge.get("websiteKey"):
            return None
        task = build_turnstile_task(
            str(challenge["websiteURL"]),
            str(challenge["websiteKey"]),
            user_agent=str(challenge.get("userAgent") or "") or None,
            page_action=str(challenge.get("pageAction") or "") or None,
            data=str(challenge.get("data") or "") or None,
            cloudflare_task_type=str(challenge.get("cloudflareTaskType") or "") or None,
            page_data=str(challenge.get("pageData") or "") or None,
            html_page_base64=str(challenge.get("htmlPageBase64") or "") or None,
            api_js_url=str(challenge.get("apiJsUrl") or "") or None,
        )
        proxy = proxy_settings_from_env(required=False)
        if proxy:
            task.update(proxy)
        return task
    if challenge.get("kind") == "hcaptcha":
        if not challenge.get("websiteKey"):
            return None
        return build_hcaptcha_task(
            str(challenge["websiteURL"]),
            str(challenge["websiteKey"]),
            invisible=bool(challenge.get("invisible")),
            data=str(challenge.get("data") or "") or None,
            user_agent=str(challenge.get("userAgent") or "") or None,
            cookies=str(challenge.get("cookies") or "") or None,
            fallback_to_actual_ua=False if challenge.get("userAgent") else None,
        )
    if challenge.get("kind") == "recaptchaV2":
        if not challenge.get("websiteKey"):
            return None
        return build_recaptcha_v2_task(
            str(challenge["websiteURL"]),
            str(challenge["websiteKey"]),
            invisible=bool(challenge.get("invisible")),
            user_agent=str(challenge.get("userAgent") or "") or None,
            cookies=str(challenge.get("cookies") or "") or None,
            recaptcha_data_s_value=str(challenge.get("recaptchaDataSValue") or "") or None,
        )
    if challenge.get("kind") == "recaptchaV2Enterprise":
        if not challenge.get("websiteKey"):
            return None
        payload = challenge.get("enterprisePayload")
        return build_recaptcha_v2_enterprise_task(
            str(challenge["websiteURL"]),
            str(challenge["websiteKey"]),
            enterprise_payload=payload if isinstance(payload, dict) else None,
            page_action=str(challenge.get("pageAction") or "") or None,
            invisible=bool(challenge.get("invisible")),
            user_agent=str(challenge.get("userAgent") or "") or None,
        )
    if challenge.get("kind") in {"recaptchaV3", "recaptchaV3Enterprise"}:
        if not challenge.get("websiteKey"):
            return None
        min_score = challenge.get("minScore")
        if not isinstance(min_score, (int, float)):
            min_score = _parse_capmonster_min_score()
        return build_recaptcha_v3_task(
            str(challenge["websiteURL"]),
            str(challenge["websiteKey"]),
            page_action=str(challenge.get("pageAction") or "verify"),
            min_score=min_score,
            enterprise=challenge.get("kind") == "recaptchaV3Enterprise",
            user_agent=str(challenge.get("userAgent") or "") or None,
        )
    if challenge.get("kind") == "funcaptcha":
        if not challenge.get("websitePublicKey"):
            return None
        task = build_funcaptcha_task(
            str(challenge["websiteURL"]),
            str(challenge["websitePublicKey"]),
            funcaptcha_api_js_subdomain=str(challenge.get("funcaptchaApiJSSubdomain") or "") or None,
            data=str(challenge.get("data") or "") or None,
            user_agent=str(challenge.get("userAgent") or "") or None,
        )
        proxy = proxy_settings_from_env(required=False)
        if proxy:
            task.update(proxy)
        return task
    if challenge.get("kind") == "geetest":
        if not challenge.get("gt"):
            return None
        task = build_geetest_task(
            str(challenge["websiteURL"]),
            str(challenge["gt"]),
            challenge=str(challenge.get("challenge") or "") or None,
            version=int(challenge.get("version") or 3),
            geetest_api_server_subdomain=str(challenge.get("geetestApiServerSubdomain") or "") or None,
            user_agent=str(challenge.get("userAgent") or "") or None,
        )
        proxy = proxy_settings_from_env(required=False)
        if proxy:
            task.update(proxy)
        return task
    if challenge.get("kind") == "datadome":
        proxy = proxy_settings_from_env(required=True)
        if not proxy or not challenge.get("captchaUrl") or not challenge.get("datadomeCookie"):
            return None
        return build_datadome_task(
            str(challenge["websiteURL"]),
            str(challenge["captchaUrl"]),
            str(challenge["datadomeCookie"]),
            proxy,
            datadome_version=str(challenge.get("datadomeVersion") or "new"),
            user_agent=str(challenge.get("userAgent") or "") or None,
        )
    return None


def _capmonster_hcaptcha_tasks_for(challenge: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not challenge or challenge.get("kind") != "hcaptcha" or not challenge.get("websiteURL") or not challenge.get("websiteKey"):
        return []
    base: dict[str, Any] = {
        "invisible": bool(challenge.get("invisible")),
        "data": str(challenge.get("data") or "") or None,
        "user_agent": str(challenge.get("userAgent") or "") or None,
        "cookies": str(challenge.get("cookies") or "") or None,
        "fallback_to_actual_ua": False if challenge.get("userAgent") else None,
    }
    task_types = ["HCaptchaTaskProxyless", "HCaptchaTask"]
    configured_type = str(os.getenv("CAPMONSTER_HCAPTCHA_TASK_TYPE") or "").strip()
    if configured_type in task_types:
        task_types.remove(configured_type)
        task_types.insert(0, configured_type)
    tasks: list[dict[str, Any]] = []
    for task_type in task_types:
        task = build_hcaptcha_task(
            str(challenge["websiteURL"]),
            str(challenge["websiteKey"]),
            task_type=task_type,
            **base,
        )
        if task_type == "HCaptchaTask":
            proxy = proxy_settings_from_env(required=False)
            if proxy:
                task.update(proxy)
        tasks.append(task)
    return tasks


def _capmonster_tasks_for(challenge: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not challenge:
        return []
    if challenge.get("kind") == "hcaptcha":
        return _capmonster_hcaptcha_tasks_for(challenge)
    task = _capmonster_task_for(challenge)
    if not task:
        return []
    tasks = [task]
    if challenge.get("kind") == "recaptchaV2":
        for task_type in ("NoCaptchaTaskProxyless", "RecaptchaV2TaskProxyless"):
            if task_type != task.get("type"):
                alternate = dict(task)
                alternate["type"] = task_type
                tasks.append(alternate)
    if challenge.get("kind") == "turnstile" and task.get("type") != "TurnstileTaskProxyless":
        alternate = dict(task)
        alternate["type"] = "TurnstileTaskProxyless"
        tasks.append(alternate)
    return tasks


def _is_capmonster_task_type_error(exc: BaseException) -> bool:
    detail = str(exc)
    return "ERROR_TASK_NOT_SUPPORTED" in detail or "Task type is not supported" in detail or "typed incorrectly" in detail


def _parse_capmonster_min_score() -> float | None:
    raw = os.getenv("CAPMONSTER_RECAPTCHA_MIN_SCORE")
    if raw is None or raw.strip() == "":
        return 0.3
    try:
        score = float(raw)
    except ValueError:
        return 0.3
    return min(0.9, max(0.1, score))


def _inject_captcha_solution(page, challenge: dict[str, Any], solution: dict[str, Any]) -> bool:
    if challenge.get("kind") == "datadome":
        return _inject_datadome_solution(page, solution)
    if challenge.get("kind") == "geetest":
        return _inject_geetest_solution(page, solution)
    if challenge.get("kind") == "turnstile" and solution.get("cf_clearance"):
        return _inject_cloudflare_clearance_solution(page, challenge, solution)
    token = (
        solution.get("gRecaptchaResponse")
        or solution.get("token")
        or solution.get("recaptchaResponse")
        or solution.get("cf_clearance")
    )
    if not token:
        return False
    try:
        return bool(
            page.evaluate(
                """({ challenge, token }) => {
                  let captchaApiIntercepted = false;
                  try {
                    const solved = () => Promise.resolve(token);
                    const response = () => token;
                    const reset = () => {};

                    let target = null;
                    if (challenge.kind === "recaptchaV3Enterprise") {
                      target = (window.grecaptcha && window.grecaptcha.enterprise) || window.grecaptcha || null;
                    } else if (String(challenge.kind || "").startsWith("recaptcha")) {
                      target = window.grecaptcha || null;
                    }
                    if (target) {
                      const patchProperty = (object, name, value) => {
                        try {
                          Object.defineProperty(object, name, {
                            configurable: true,
                            writable: true,
                            value,
                          });
                        } catch (e) {
                          try { object[name] = value; } catch (e2) {}
                        }
                        return object[name] === value;
                      };
                      if (typeof target.execute === "function") {
                        captchaApiIntercepted = patchProperty(target, "execute", solved) || captchaApiIntercepted;
                      }
                      if (typeof target.getResponse === "function") {
                        captchaApiIntercepted = patchProperty(target, "getResponse", response) || captchaApiIntercepted;
                      }
                      if (typeof target.reset === "function") {
                        patchProperty(target, "reset", reset);
                      }
                      // ── Proxy fallback when direct patching fails ──────
                      if (!captchaApiIntercepted && typeof target.execute === "function") {
                        try {
                          const proxy = new Proxy(target, {
                            get(t, prop) {
                              if (prop === "execute") return solved;
                              if (prop === "getResponse") return response;
                              if (prop === "reset") return reset;
                              const val = t[prop];
                              return typeof val === "function" ? val.bind(t) : val;
                            },
                          });
                          if (challenge.kind === "recaptchaV3Enterprise" && window.grecaptcha) {
                            try { window.grecaptcha.enterprise = proxy; } catch (e) {
                              try { Object.defineProperty(window.grecaptcha, "enterprise", { configurable: true, get() { return proxy; } }); } catch (e2) {}
                            }
                          }
                          captchaApiIntercepted = true;
                        } catch (e) {}
                      }
                      if (String(challenge.kind || "").startsWith("recaptchaV3")) {
                        const installGuard = () => {
                          try {
                            patchProperty(target, "execute", solved);
                            patchProperty(target, "getResponse", response);
                            patchProperty(target, "reset", reset);
                            captchaApiIntercepted = true;
                          } catch (e) {}
                        };
                        installGuard();
                        try {
                          clearInterval(window.__jobAgentRecaptchaGuardInterval);
                          window.__jobAgentRecaptchaGuardInterval = setInterval(installGuard, 50);
                          setTimeout(() => clearInterval(window.__jobAgentRecaptchaGuardInterval), 15000);
                        } catch (e) {}
                      }
                    }
                  } catch (e) {}
                  const selectors = challenge.kind === "turnstile"
                    ? ["textarea[name='cf-turnstile-response']", "input[name='cf-turnstile-response']"]
                    : challenge.kind === "hcaptcha"
                      ? ["textarea[name='h-captcha-response']", "textarea[name='hcaptcha-response']", "input[name='h-captcha-response']", "input[name='hcaptcha-response']"]
                    : challenge.kind === "funcaptcha"
                      ? ["#verification-token", "#FunCaptcha-Token", "input[name='fc-token']", "input[name='verification-token']"]
                      : ["textarea[name='g-recaptcha-response']", "input[name='g-recaptcha-response']"];
                  const setValue = (node) => {
                    if (!node) return false;
                    const proto = node.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    const descriptor = Object.getOwnPropertyDescriptor(proto, "value");
                    if (descriptor && descriptor.set) descriptor.set.call(node, token);
                    else node.value = token;
                    node.dispatchEvent(new Event("input", { bubbles: true }));
                    node.dispatchEvent(new Event("change", { bubbles: true }));
                    return true;
                  };
                  let injected = false;
                  selectors.forEach((selector) => {
                    document.querySelectorAll(selector).forEach((node) => { injected = setValue(node) || injected; });
                  });
                  if (!injected && String(challenge.kind || "").startsWith("recaptcha")) {
                    const textarea = document.createElement("textarea");
                    textarea.name = "g-recaptcha-response";
                    textarea.style.display = "none";
                    document.body.appendChild(textarea);
                    injected = setValue(textarea);
                  }
                  if (!injected && challenge.kind === "hcaptcha") {
                    const textarea = document.createElement("textarea");
                    textarea.name = "h-captcha-response";
                    textarea.style.display = "none";
                    document.body.appendChild(textarea);
                    injected = setValue(textarea);
                  }
                  if (!injected && challenge.kind === "funcaptcha") {
                    const input = document.createElement("input");
                    input.type = "hidden";
                    input.name = "fc-token";
                    document.body.appendChild(input);
                    injected = setValue(input);
                  }
                  if (challenge.callback && typeof window[challenge.callback] === "function") {
                    try { window[challenge.callback](token); } catch (e) {}
                  }
                  return injected || captchaApiIntercepted;
                }""",
                {"challenge": challenge, "token": token},
            )
        )
    except Exception:
        return False


def _inject_cloudflare_clearance_solution(page, challenge: dict[str, Any], solution: dict[str, Any]) -> bool:
    value = str(solution.get("cf_clearance") or "").strip()
    if not value:
        return False
    url = str(challenge.get("websiteURL") or getattr(page, "url", "") or "")
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    cookie_url = f"{parsed.scheme}://{parsed.netloc}"
    try:
        page.context.add_cookies([{"name": "cf_clearance", "value": value, "url": cookie_url, "path": "/"}])
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _inject_geetest_solution(page, solution: dict[str, Any]) -> bool:
    if not solution.get("validate") and not solution.get("seccode"):
        return False
    try:
        return bool(
            page.evaluate(
                """(solution) => {
                  const values = {
                    geetest_challenge: solution.challenge || "",
                    geetest_validate: solution.validate || "",
                    geetest_seccode: solution.seccode || "",
                  };
                  const setValue = (name, value) => {
                    if (!value) return false;
                    let node = document.querySelector(`input[name="${name}"], textarea[name="${name}"]`);
                    if (!node) {
                      node = document.createElement("input");
                      node.type = "hidden";
                      node.name = name;
                      document.body.appendChild(node);
                    }
                    const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
                    if (descriptor && descriptor.set) descriptor.set.call(node, value);
                    else node.value = value;
                    node.dispatchEvent(new Event("input", { bubbles: true }));
                    node.dispatchEvent(new Event("change", { bubbles: true }));
                    return true;
                  };
                  let injected = false;
                  Object.entries(values).forEach(([name, value]) => { injected = setValue(name, value) || injected; });
                  return injected;
                }""",
                solution,
            )
        )
    except Exception:
        return False


def _inject_datadome_solution(page, solution: dict[str, Any]) -> bool:
    domains = solution.get("domains")
    if not isinstance(domains, dict):
        return False
    cookies: list[dict[str, Any]] = []
    for domain, payload in domains.items():
        if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), dict):
            continue
        for name, value in payload["cookies"].items():
            if name and value:
                cookies.append({"name": str(name), "value": str(value), "domain": str(domain), "path": "/"})
    if not cookies:
        return False
    try:
        page.context.add_cookies(cookies)
        return True
    except Exception:
        return False


def _wait_for_captcha_api_ready(page, challenge: dict[str, Any]) -> bool:
    """Wait briefly for an in-page CAPTCHA API that the site itself will call.

    Greenhouse renders invisible reCAPTCHA Enterprise from a module-loaded
    client.  If we solve the challenge before `grecaptcha.enterprise.execute`
    exists, token injection can only create hidden response fields, but the
    Greenhouse submit path posts JSON from `performAssessment()` instead of
    reading those fields.  Waiting lets `_inject_captcha_solution` patch the
    exact API method the submit handler will call.
    """
    kind = str(challenge.get("kind") or "")
    if not kind.startswith("recaptchaV3"):
        return True
    try:
        return bool(
            page.wait_for_function(
                """(kind) => {
                  if (!window.grecaptcha) return false;
                  if (kind === "recaptchaV3Enterprise") {
                    return !!(window.grecaptcha.enterprise
                      && typeof window.grecaptcha.enterprise.execute === "function");
                  }
                  return typeof window.grecaptcha.execute === "function";
                }""",
                arg=kind,
                timeout=12000,
            )
        )
    except Exception:
        return False


def _captcha_solution_detail(challenge: dict[str, Any], *, api_ready: bool = True) -> str:
    url = _safe_evidence_url(str(challenge.get("websiteURL") or ""))
    detail = f"{challenge.get('kind')} at {url or 'current page'}"
    if not api_ready:
        detail += " (api not ready before solve)"
    return detail


def _solve_captcha_if_configured(page) -> dict[str, str]:
    config = CapMonsterConfig.from_env()
    challenge = _discover_captcha(page)
    if not isinstance(challenge, dict) or not challenge.get("kind"):
        return {"status": "none", "detail": "no supported CAPTCHA detected"}
    if challenge.get("kind") == "hcaptcha":
        if _captcha_vision_enabled():
            return _solve_hcaptcha_with_vision(page)
        return {
            "status": "unsupported",
            "detail": "hcaptcha is not supported by CapMonster token tasks; vision fallback disabled or missing API key",
        }
    if not config.enabled or not config.api_key:
        return {"status": "skipped", "detail": "disabled"}
    try:
        tasks = _capmonster_tasks_for(challenge)
        if not tasks:
            return {"status": "unsupported", "detail": str(challenge.get("kind") or "unknown")}
        api_ready = _wait_for_captcha_api_ready(page, challenge)
        client = CapMonsterClient(config.api_key)
        errors: list[str] = []
        solution: dict[str, Any] | None = None
        for index, candidate_task in enumerate(tasks):
            try:
                solution = client.solve_task(
                    candidate_task,
                    timeout_seconds=config.timeout_seconds,
                    poll_interval_seconds=config.poll_interval_seconds,
                )
                break
            except (CapMonsterError, TimeoutError) as exc:
                errors.append(f"{candidate_task.get('type')}: {exc}")
                if not _is_capmonster_task_type_error(exc) or index >= len(tasks) - 1:
                    if challenge.get("kind") == "hcaptcha" and errors:
                        raise CapMonsterError("; ".join(errors)) from exc
                    raise
        if solution is None:
            raise CapMonsterError("; ".join(errors) or "CapMonster did not return a solution")
        injected = _inject_captcha_solution(page, challenge, solution)
        detail = _captcha_solution_detail(challenge, api_ready=api_ready)
        if solution.get("userAgent"):
            detail += " (solution userAgent returned)"
        return {
            "status": "solved" if injected else "solution_not_injected",
            "detail": detail,
        }
    except (CapMonsterError, TimeoutError) as exc:
        if challenge.get("kind") == "hcaptcha":
            vision_result = _solve_hcaptcha_with_vision(page)
            if vision_result["status"] == "solved":
                return vision_result
            return {
                "status": vision_result["status"],
                "detail": f"CapMonster token API: {exc}; vision fallback: {vision_result['detail']}",
            }
        return {"status": "error", "detail": str(exc)}


def _captcha_vision_enabled() -> bool:
    raw = str(os.getenv("CAPTCHA_VISION_FALLBACK") or "0").strip().lower()
    enabled = raw in {"1", "true", "yes", "on"}
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    return enabled and bool(api_key)


def _captcha_vision_max_rounds() -> int:
    try:
        value = int(os.getenv("CAPTCHA_VISION_MAX_ROUNDS") or "8")
    except ValueError:
        value = 8
    return min(12, max(1, value))


def _captcha_vision_llm_retries() -> int:
    try:
        value = int(os.getenv("CAPTCHA_VISION_LLM_RETRIES") or "3")
    except ValueError:
        value = 3
    return min(5, max(1, value))


def _visible_hcaptcha_challenge_frame(page):
    for frame in page.frames:
        if "frame=challenge" not in str(frame.url):
            continue
        try:
            if frame.frame_element().is_visible():
                return frame
        except Exception:
            continue
    return None


def _hcaptcha_response(page) -> str:
    try:
        return str(
            page.evaluate(
                """() => window.hcaptcha && typeof window.hcaptcha.getResponse === "function"
                  ? (window.hcaptcha.getResponse() || "") : """  # noqa: E501
            )
            or ""
        )
    except Exception:
        return ""


def _parse_vision_clicks(raw: str, width: float, height: float) -> list[dict[str, float]]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError("vision response did not contain JSON")
    payload = json.loads(match.group(0))
    clicks = payload.get("clicks") or payload.get("target_points") or payload.get("targets")
    if not isinstance(clicks, list) and isinstance(payload.get("objects"), list):
        objects = [item for item in payload["objects"] if isinstance(item, dict)]
        explicitly_selected = [item for item in objects if item.get("matches") is True or item.get("selected") is True]
        clicks = explicitly_selected or objects
    if not isinstance(clicks, list) and payload.get("x") is not None and payload.get("y") is not None:
        clicks = [{"x": payload["x"], "y": payload["y"]}]
    if not isinstance(clicks, list):
        raise ValueError("vision response did not contain clicks")
    parsed: list[dict[str, float]] = []
    for click in clicks[:12]:
        if isinstance(click, (list, tuple)) and len(click) >= 2:
            click = {"x": click[0], "y": click[1]}
        if not isinstance(click, dict):
            continue
        try:
            x = float(click["x"])
            y = float(click["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= x <= width and 0 <= y <= height:
            parsed.append({"x": x, "y": y})
    if not parsed:
        raise ValueError("vision response had no in-bounds clicks")
    return parsed


def _parse_complex_image_clicks(solution: dict[str, Any], width: float, height: float) -> list[dict[str, float]]:
    raw_clicks = (
        solution.get("clicks")
        or solution.get("coordinates")
        or solution.get("objects")
        or solution.get("answer")
        or solution.get("answers")
    )
    if not isinstance(raw_clicks, list):
        return []
    parsed: list[dict[str, float]] = []
    grid_answers: list[bool] = []
    for item in raw_clicks:
        if isinstance(item, bool):
            grid_answers.append(item)
            continue
        if isinstance(item, (int, float)):
            grid_answers.append(bool(item))
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            item = {"x": item[0], "y": item[1]}
        if not isinstance(item, dict):
            continue
        candidate = item.get("center") if isinstance(item.get("center"), dict) else item
        try:
            x = float(candidate["x"])
            y = float(candidate["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= x <= width and 0 <= y <= height:
            parsed.append({"x": x, "y": y})
    if parsed:
        return parsed[:12]
    if not grid_answers:
        return []
    columns = rows = 3
    metadata = solution.get("metadata") if isinstance(solution.get("metadata"), dict) else {}
    grid_match = re.search(r"(\d+)\s*[xX]\s*(\d+)", str(metadata.get("Grid") or ""))
    if grid_match:
        columns = max(1, int(grid_match.group(1)))
        rows = max(1, int(grid_match.group(2)))
    elif len(grid_answers) in {4, 9, 16}:
        columns = rows = int(len(grid_answers) ** 0.5)
    for index, selected in enumerate(grid_answers[: columns * rows]):
        if not selected:
            continue
        column = index % columns
        row = index // columns
        parsed.append(
            {
                "x": (column + 0.5) * width / columns,
                "y": (row + 0.5) * height / rows,
            }
        )
    return parsed[:12]


def _solve_complex_image_clicks_with_capmonster(
    image_bytes: bytes,
    instruction: str,
    width: float,
    height: float,
) -> list[dict[str, float]]:
    config = CapMonsterConfig.from_env()
    if not config.enabled or not config.api_key:
        return []
    task = build_complex_image_task(
        base64.b64encode(image_bytes).decode("ascii"),
        instruction,
        task_class=os.getenv("CAPMONSTER_COMPLEX_IMAGE_CLASS") or "recognition",
    )
    solution = CapMonsterClient(config.api_key).solve_task(
        task,
        timeout_seconds=config.timeout_seconds,
        poll_interval_seconds=config.poll_interval_seconds,
    )
    return _parse_complex_image_clicks(solution, width, height)


def _parse_vision_drag(raw: str, width: float, height: float) -> dict[str, dict[str, float]]:
    parsed = _parse_vision_drag_candidates(raw, width, height, require_source=True)
    return {"source": parsed["source"], "target": parsed["targets"][0]}


def _parse_vision_drag_candidates(
    raw: str,
    width: float,
    height: float,
    *,
    require_source: bool = True,
) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError("vision response did not contain JSON")
    payload = json.loads(match.group(0))
    source = (
        payload.get("source")
        or payload.get("drag_source")
        or payload.get("character")
        or payload.get("from")
    )
    raw_targets = payload.get("targets") or payload.get("drop_targets") or payload.get("silhouettes")
    if not isinstance(raw_targets, list):
        raw_targets = [
            payload.get("target")
            or payload.get("drop_target")
            or payload.get("silhouette")
            or payload.get("to")
        ]
    if isinstance(source, list) and len(source) >= 2:
        source = {"x": source[0], "y": source[1]}
    parsed_source: dict[str, float] | None = None
    if isinstance(source, dict):
        try:
            source_x = float(source["x"])
            source_y = float(source["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("vision response contained invalid drag coordinates") from exc
        if not (0 <= source_x <= width and 0 <= source_y <= height):
            raise ValueError("vision response drag source was out of bounds")
        parsed_source = {"x": source_x, "y": source_y}
    elif require_source:
        raise ValueError("vision response did not contain drag source")

    parsed_targets: list[dict[str, float]] = []
    saw_out_of_bounds_target = False
    for raw_target in raw_targets[:4]:
        target = raw_target
        if isinstance(target, list) and len(target) >= 2:
            target = {"x": target[0], "y": target[1]}
        if not isinstance(target, dict):
            continue
        try:
            target_x = float(target["x"])
            target_y = float(target["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= target_x <= width and 0 <= target_y <= height:
            parsed_targets.append({"x": target_x, "y": target_y})
        else:
            saw_out_of_bounds_target = True
    if not parsed_targets:
        if saw_out_of_bounds_target:
            raise ValueError("vision response drag target was out of bounds")
        raise ValueError("vision response did not contain in-bounds drag targets")
    return {"source": parsed_source, "targets": parsed_targets}


def _hcaptcha_drag_source_from_frame(frame, box: dict[str, float]) -> dict[str, float] | None:
    move_box = None
    frame_relative_box = False
    try:
        move_box = frame.evaluate(
            """() => {
              const textOf = (node) => String(node.innerText || node.textContent || node.getAttribute("aria-label") || "");
              const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
              const moveNode = nodes.find((node) => /\\bmove\\b/i.test(textOf(node)));
              if (!moveNode) return null;
              const rectOf = (node) => {
                const rect = node.getBoundingClientRect();
                return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
              };
              const candidates = [];
              for (let node = moveNode; node; node = node.parentElement) {
                const rect = rectOf(node);
                if (
                  rect.width >= 55 &&
                  rect.height >= 55 &&
                  rect.width <= 240 &&
                  rect.height <= 240
                ) {
                  candidates.push(rect);
                }
              }
              candidates.sort((a, b) => (b.width * b.height) - (a.width * a.height));
              return candidates[0] || rectOf(moveNode);
            }"""
        )
        frame_relative_box = bool(move_box)
    except Exception:
        move_box = None
    for selector in (
        "button:has-text('Move')",
        "[role='button']:has-text('Move')",
    ):
        if move_box:
            break
        try:
            move = frame.locator(selector).last
            if move.count() and move.is_visible(timeout=1000):
                move_box = move.bounding_box(timeout=1000)
                break
        except Exception:
            continue
    if not move_box:
        try:
            move = frame.get_by_text(re.compile(r"\bMove\b", re.IGNORECASE)).last
            if move.count() and move.is_visible(timeout=1000):
                move_box = move.bounding_box(timeout=1000)
        except Exception:
            move_box = None
    if not move_box:
        try:
            move_box = frame.evaluate(
                """() => {
                  const nodes = Array.from(document.querySelectorAll("button,[role='button'],div,span"));
                  const candidates = nodes
                    .filter((node) => /\\bmove\\b/i.test(String(node.innerText || node.textContent || node.getAttribute("aria-label") || "")))
                    .map((node) => {
                      const rect = node.getBoundingClientRect();
                      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
                    })
                    .filter((rect) => rect.width > 8 && rect.height > 8);
                  candidates.sort((a, b) => (a.width * a.height) - (b.width * b.height));
                  return candidates[0] || null;
                }"""
            )
        except Exception:
            move_box = None
    if not move_box:
        return None
    if frame_relative_box:
        try:
            frame_box = frame.frame_element().bounding_box() or {}
            move_box = {
                **move_box,
                "x": float(frame_box.get("x") or 0) + float(move_box.get("x") or 0),
                "y": float(frame_box.get("y") or 0) + float(move_box.get("y") or 0),
            }
        except Exception:
            pass
    source = {
        "x": float(move_box.get("x") or 0) + float(move_box.get("width") or 0) / 2 - box["x"],
        "y": float(move_box.get("y") or 0) + float(move_box.get("height") or 0) / 2 - box["y"],
    }
    if 0 <= source["x"] <= box["width"] and 0 <= source["y"] <= box["height"]:
        return source
    return None


def _write_hcaptcha_vision_debug(
    *,
    round_number: int,
    instruction: str,
    image_bytes: bytes,
    response: str,
    parsed: Any,
) -> None:
    debug_dir = os.getenv("CAPTCHA_VISION_DEBUG_DIR")
    if not debug_dir:
        return
    try:
        path = Path(debug_dir)
        path.mkdir(parents=True, exist_ok=True)
        stem = f"hcaptcha-round-{round_number:02d}"
        (path / f"{stem}.png").write_bytes(image_bytes)
        (path / f"{stem}.json").write_text(
            json.dumps(
                {
                    "instruction": instruction,
                    "response": response,
                    "parsed": parsed,
                },
                indent=2,
                ensure_ascii=True,
            )
        )
    except Exception:
        pass


def _solve_hcaptcha_with_vision(page) -> dict[str, str]:
    if not _captcha_vision_enabled():
        return {"status": "unsupported", "detail": "hcaptcha vision fallback disabled or missing API key"}
    try:
        page.evaluate(
            """() => {
              if (!window.hcaptcha || typeof window.hcaptcha.execute !== "function") return false;
              window.hcaptcha.execute();
              return true;
            }"""
        )
        page.wait_for_timeout(4000)
        llm = HelloAgentsLLM(
            model=os.getenv("CAPTCHA_VISION_MODEL") or os.getenv("LLM_MODEL_ID"),
            temperature=0,
            timeout=90,
        )
        for round_number in range(1, _captcha_vision_max_rounds() + 1):
            if _hcaptcha_response(page):
                return {"status": "solved", "detail": f"hcaptcha vision fallback in {round_number - 1} rounds"}
            frame = _visible_hcaptcha_challenge_frame(page)
            if frame is None:
                page.wait_for_timeout(1500)
                if _hcaptcha_response(page):
                    return {"status": "solved", "detail": f"hcaptcha vision fallback in {round_number - 1} rounds"}
                continue
            canvas_box = None
            try:
                canvas_box = frame.locator("canvas").first.bounding_box(timeout=1500)
            except Exception:
                canvas_box = None
            if canvas_box:
                header_box = None
                try:
                    header_box = frame.locator(".challenge-header, .challenge-prompt").first.bounding_box(timeout=1000)
                except Exception:
                    header_box = None
                image_top = max(
                    float(canvas_box.get("y") or 0),
                    float((header_box or {}).get("y") or 0)
                    + float((header_box or {}).get("height") or 0)
                    + 10,
                )
                box = {
                    "x": float(canvas_box.get("x") or 0),
                    "y": image_top,
                    "width": float(canvas_box.get("width") or 0),
                    "height": max(
                        0,
                        float(canvas_box.get("y") or 0)
                        + float(canvas_box.get("height") or 0)
                        - image_top,
                    ),
                }
            else:
                frame_box = frame.frame_element().bounding_box() or {}
                box = {
                    "x": float(frame_box.get("x") or 0),
                    "y": float(frame_box.get("y") or 0),
                    "width": float(frame_box.get("width") or 0),
                    "height": float(frame_box.get("height") or 0),
                }
            width = box["width"]
            height = box["height"]
            if width <= 0 or height <= 0:
                continue
            image_bytes = page.screenshot(clip=box)
            try:
                instruction = frame.locator(".challenge-prompt").inner_text(timeout=3000).strip()
            except Exception:
                instruction = "Select every image that satisfies the visible instruction."
            is_drag_challenge = bool(re.search(r"\bdrag\b|\bsilhouette\b|\bmatching\b", instruction, re.IGNORECASE))
            if is_drag_challenge:
                prompt = (
                    f"Instruction: {instruction!r}. Inspect the entire image and solve the drag CAPTCHA. "
                    "The draggable object is inside the translucent card marked '+ Move'. "
                    "Do not choose a source point from the background scene. "
                    "The drop target is the dark silhouette cutout inside the larger scene. "
                    "First identify the shape of the draggable object. Then find the silhouette with the same outline; "
                    "ignore unrelated dark shadows, terrain, buildings, and blobs. Return up to three ranked candidate silhouette centers if uncertain. "
                    "Return ONLY JSON "
                    f'with {{"source":{{"x":number,"y":number}},"targets":[{{"x":number,"y":number}}],'
                    f'"confidence":number,"reason":"short"}}. '
                    f"Coordinates are pixels in the full supplied image ({round(width)}x{round(height)}). "
                    "Do not include markdown."
                )
            else:
                prompt = (
                    f"Instruction: {instruction!r}. Translate it exactly if needed. Inspect the entire image despite camouflage, "
                    "identify every distinct depicted item including vehicles and machinery, and apply the instruction exactly. "
                    "Treat each picture/card as the real-world object it represents, not as a lightweight photo or card. Return ONLY JSON "
                    f'with {{"objects":[{{"name":"item","x":number,"y":number,"matches":boolean}}],'
                    f'"clicks":[{{"x":number,"y":number}}],"confidence":number,"reason":"short"}}. '
                    f"Coordinates are pixels in the full supplied image ({round(width)}x{round(height)}). "
                    "Include every target that must be selected, in click order. Do not include markdown."
                )
            response = ""
            complex_image_clicks: list[dict[str, float]] = []
            complex_image_error = ""
            if not is_drag_challenge:
                try:
                    complex_image_clicks = _solve_complex_image_clicks_with_capmonster(
                        image_bytes,
                        instruction,
                        width,
                        height,
                    )
                    if complex_image_clicks:
                        response = "[skipped LLM vision because CapMonster ComplexImage returned clicks]"
                        print(f"hCaptcha CapMonster ComplexImage: clicking {len(complex_image_clicks)} target(s)")
                except Exception as exc:
                    complex_image_error = f"{type(exc).__name__}: {exc}"
            if not complex_image_clicks:
                for attempt in range(1, _captcha_vision_llm_retries() + 1):
                    try:
                        response = llm.invoke(
                            [
                                {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": prompt},
                                        {
                                            "type": "image_url",
                                            "image_url": {
                                                "url": "data:image/png;base64,"
                                                + base64.b64encode(image_bytes).decode("ascii"),
                                                "detail": "high",
                                            },
                                        },
                                    ],
                                }
                            ],
                            temperature=0,
                            max_tokens=300,
                        )
                        break
                    except Exception:
                        if attempt >= _captcha_vision_llm_retries():
                            raise
                        page.wait_for_timeout(1000 * attempt)
            if is_drag_challenge:
                handle_source = _hcaptcha_drag_source_from_frame(frame, box)
                drag = _parse_vision_drag_candidates(
                    response,
                    width,
                    height,
                    require_source=handle_source is None,
                )
                source = handle_source or drag["source"]
                if not source:
                    raise ValueError("vision response did not contain drag source")
                parsed_debug = {"source": source, "targets": drag["targets"]}
                _write_hcaptcha_vision_debug(
                    round_number=round_number,
                    instruction=instruction,
                    image_bytes=image_bytes,
                    response=response,
                    parsed=parsed_debug,
                )
                print(
                    "hCaptcha vision round "
                    + str(round_number)
                    + ": dragging from "
                    + f"({round(source['x'])},{round(source['y'])})"
                    + " to "
                    + ", ".join(f"({round(target['x'])},{round(target['y'])})" for target in drag["targets"])
                )
                current_source = source
                for target in drag["targets"][:3]:
                    try:
                        page.mouse.move(box["x"] + current_source["x"], box["y"] + current_source["y"])
                        page.mouse.down()
                        page.wait_for_timeout(250)
                        page.mouse.move(
                            box["x"] + target["x"],
                            box["y"] + target["y"],
                            steps=24,
                        )
                        page.wait_for_timeout(250)
                        page.mouse.up()
                    except Exception:
                        try:
                            page.mouse.up()
                        except Exception:
                            pass
                        continue
                    current_source = target
                    try:
                        verify = frame.get_by_role("button", name=re.compile(r"^(verify|next)$", re.IGNORECASE))
                        if verify.count() and verify.last.is_visible():
                            verify.last.click(force=True, timeout=5000)
                    except Exception:
                        pass
                    page.wait_for_timeout(1800)
                    if _hcaptcha_response(page):
                        return {"status": "solved", "detail": f"hcaptcha vision fallback in {round_number} rounds"}
            else:
                clicks = complex_image_clicks
                if not clicks:
                    clicks = _parse_vision_clicks(response, width, height)
                _write_hcaptcha_vision_debug(
                    round_number=round_number,
                    instruction=instruction,
                    image_bytes=image_bytes,
                    response=response,
                    parsed={"clicks": clicks, "complex_image_error": complex_image_error},
                )
                print(f"hCaptcha vision round {round_number}: clicking {len(clicks)} target(s)")
                for click in clicks:
                    try:
                        page.mouse.click(box["x"] + click["x"], box["y"] + click["y"])
                        page.wait_for_timeout(400)
                    except Exception:
                        break
            try:
                verify = frame.get_by_role("button", name=re.compile(r"^(verify|next)$", re.IGNORECASE))
                if verify.count() and verify.last.is_visible():
                    verify.last.click(force=True, timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(3500)
        if _hcaptcha_response(page):
            return {"status": "solved", "detail": "hcaptcha vision fallback"}
        return {"status": "error", "detail": "hcaptcha vision fallback exhausted rounds"}
    except Exception as exc:
        return {"status": "error", "detail": f"hcaptcha vision fallback failed: {type(exc).__name__}: {exc}"}


def _readback_status(readback: Any) -> str:
    if readback is True:
        return "checked"
    if readback is False:
        return "unchecked"
    if readback == "file-selected":
        return "file selected"
    if readback == "selected":
        return "selected"
    if isinstance(readback, str) and readback.startswith("selected: "):
        return readback
    if readback is None:
        return "unknown"
    return "filled" if str(readback) else "empty"


def _display_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: python -m job_agent.python_runtime <autofill-runtime.js>", file=sys.stderr)
        return 2
    try:
        return run_runtime_script(args[0])
    except Exception as exc:
        print(f"Runtime autofill failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
