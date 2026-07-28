import os
import shutil
import subprocess

import pytest

from job_agent.runtime_filler import render_runtime_autofill_script


def _profile():
    return {
        "name": "Gaoyi Wu",
        "email": "gaoyi@example.com",
        "phone": "+1 555 0100",
        "linkedin": "https://linkedin.com/in/gaoyi",
        "location": "New York, NY",
        "answers": {
            "How did you hear about us?": "Company website",
            "Are you authorized to work in the United States?": "Yes",
        },
    }


def test_runtime_autofill_script_embeds_profile_and_url():
    script = render_runtime_autofill_script(
        profile=_profile(),
        resume_file="/tmp/tailored-resume.pdf",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    assert 'require("playwright")' in script
    assert "chromium.launch" in script
    # profile + url are embedded
    assert "Gaoyi Wu" in script
    assert "gaoyi@example.com" in script
    assert "https://boards.greenhouse.io/acme/jobs/1" in script
    assert "/tmp/tailored-resume.pdf" in script
    # Simplify-style engine pieces are present
    assert "scrapeFields" in script
    assert "findNextButton" in script
    assert "findSubmitButton" in script
    assert "planField" in script
    assert 'querySelectorAll("label")' in script
    assert "CSS.escape" not in script
    # unresolved required fields stop automatic submission
    assert "automatic submission not performed" in script
    assert "retainUnresolvedControlReviews" in script
    assert 'const CANDIDATE_ACCOUNT_REQUIRED_LINE_PREFIX = "Candidate account required:"' in script


def test_runtime_autofill_script_includes_office_location_combobox_rule():
    script = render_runtime_autofill_script(profile=_profile())

    assert "function preferredOfficeLocationOption" in script
    assert "preferredOfficeLocationOption(field, profile)" in script
    assert "function preferredOfficeLocationAnswer" in script
    assert "preferredOfficeLocationAnswer(field, profile)" in script
    assert 'const APPLICATION_FORM_UNAVAILABLE_LINE_PREFIX = "Application form unavailable:"' in script
    assert "hasApplicationFormContext" in script
    assert "installApplicationNavigationGuard" in script
    assert "restoreApplicationContextIfExternal" in script
    assert "privacy|notice|policy|terms|arbitration|personnel|candidate|pdf" in script
    assert 'item.reason === "candidate account creation required"' in script
    assert "candidateAccountPasswordStorePath" in script
    assert "generateCandidateAccountPassword" in script
    assert "workdaySignInFailureReason" in script
    assert "options = {}" in script
    assert "options.allowGeneric !== false" in script
    assert "repeatedWorkdaySignInPages > 1" in script
    assert "function workdaySignInFillSignature" in script
    assert "repeatedWorkdaySignInFillPages > 1" in script
    assert "candidate account sign-in did not advance after filling email and password" in script
    assert 'label: "Candidate account sign-in"' in script
    assert "function findWorkdayEmailSignInEntry" in script
    assert "openWorkdayEmailSignInIfNeeded" in script
    assert "function findWorkdayCreateAccountEntry" in script
    assert "openWorkdayCreateAccountFromSignInIfAvailable" in script
    assert "restoreWorkdayApplicationFromCandidateHome" in script
    assert "you have no applications" in script
    assert "const isWorkdayApplication" in script


def test_runtime_autofill_script_includes_ashby_motivation_question_rules():
    script = render_runtime_autofill_script(profile=_profile())

    assert "function isMotivationQuestion" in script
    assert 'normalizedLabel.includes("what excites you about")' in script
    assert 'normalizedLabel.includes("this role interests you")' in script
    assert 'normalizedLabel.includes("what about") && normalizedLabel.includes("interests you")' in script
    assert 'normalizedLabel.includes("why are you applying to")' in script
    assert "function motivationAnswerForLabel" in script
    assert "LangChain multi-agent workflows and agent tooling" in script
    assert "Kubernetes, Kafka, MLflow" in script


def test_runtime_autofill_script_includes_ashby_button_group_click_verification():
    script = render_runtime_autofill_script(profile=_profile())

    assert "async function clickAshbyButtonGroup" in script
    assert "data-job-agent-ashby-click-target" in script
    assert 'String(target.className || "").includes("_active_")' in script
    assert 'input[type="radio"],input[type="checkbox"]' in script
    assert "async function isAshbyYesNoOption" in script
    assert "!(await isAshbyYesNoOption(locator))" in script
    assert "workdayAccountVerificationReason" in script
    assert "candidate account verification is required by Workday" in script
    assert "workdayCreateAccountFailureReason" in script
    assert "pageDidNotAdvance" in script


def test_runtime_autofill_script_includes_current_ashby_screening_rules():
    script = render_runtime_autofill_script(profile=_profile())

    assert "function developerFacingProductsAnswer" in script
    assert 'n.includes("developer facing")' in script
    assert "function relevantProfessionalExperienceRangeAnswer" in script
    assert 'return "1-2 years"' in script
    assert 'optionValue: String(ans), optionText: String(ans)' in script
    assert 'button option had no autofill selector and Ashby text click failed' in script
    assert 'n.includes("foster city")' in script
    assert 'n.includes("unrestricted") && authorizationField && !sponsorshipField' in script


def test_runtime_autofill_script_clears_stale_autofill_markers_before_rescrape():
    script = render_runtime_autofill_script(profile=_profile())

    assert 'document.querySelectorAll("[data-job-agent-autofill-index]")' in script
    assert 'node.removeAttribute("data-job-agent-autofill-index")' in script
    selector_start = script.index("function selectorFor(f)")
    selector_end = script.index("function recoverTextFillLocator", selector_start)
    selector_source = script[selector_start:selector_end]
    assert selector_source.index("if (f.autofillId)") < selector_source.index("if (f.id)")
    identity_start = script.index("const fieldIdentity = (field)")
    identity_end = script.index("const fieldSignature", identity_start)
    identity_source = script[identity_start:identity_end]
    assert identity_source.index("if (field.autofillId)") < identity_source.index("if (field.id)")
    assert '&& /^(agree|accept|yes|i agree)$/i.test(explicitText)' in script
    assert "function inferPhoneCountryCode(profile)" in script
    assert "function runtimeApplicationUrl(applicationUrl)" in script
    assert "job-boards.greenhouse.io/embed/job_app?for=coinbase&token=" in script
    assert "job-boards.greenhouse.io/embed/job_app?for=samsara&token=" in script
    assert "job-boards.greenhouse.io/embed/job_app?for=pinterest&token=" in script
    assert "where have you learned about" in script
    assert "LinkedIn Jobs" in script
    assert '"East Asian"' in script
    assert 'const countryLikeField = String(f.id || "").toLowerCase() === "country"' in script
    assert "officeLocationCheckboxPlan" in script
    assert 'n.includes("compensation expectation")' in script
    assert 'n.includes("hybrid schedule")' in script
    assert 'n.includes("built ai agents")' in script
    assert 'n.includes("what ai tools")' in script
    assert '"United States +1"' in script
    assert 'if (n === "male") aliases.push("Man");' in script
    assert '[data-automation-id^="formField-"] button' in script
    assert '"I Acknowledge"' in script
    assert 'return "I Acknowledge";' in script
    assert 'return "I Agree";' in script
    assert 'n.includes("sexual orientation")' in script
    assert "aliases: answerAliases(answer)" in script
    assert "function requiresExternalApplicationPortal(label)" in script
    assert "external application portal required" in script
    assert "function invalidFindingCanUseSuccessfulReadback(label)" in script
    assert "when will you graduate" in script
    assert "function captchaResultBlocksSubmission(captchaResult)" in script
    assert "captcha blocked automatic submission" in script
    assert '"apply now", "apply manually"' in script
    assert '"apply for this job", "apply now"' in script
    assert "Airbnb Candidate Privacy Policy" in script
    assert "I will require immigration sponsorship in the future" in script
    assert "I do not have direct Community Support domain experience" in script
    assert "I have never worked for SpaceX or SpaceXAI" in script
    assert "Company careers page / website" in script
    assert "company careers page website" in script
    assert "function workdayPhoneDeviceTypeAnswer" in script
    assert 'return "Primary";' in script
    assert 'return "Career Website";' in script
    assert "engaged with" in script
    assert "compensation offer" in script
    assert "biopharmaComplianceAnswer" in script
    assert "oig list of excluded individuals entities" in script
    assert "immigration-related employer sponsorship" in script
    assert "legalTermsConsentAnswer" in script
    assert "terms and conditions" in script
    assert "productionScreeningAnswer" in script
    assert "automatically optimizes decisions" in script
    assert "advertising systems" in script
    assert 'if (n === "language")' in script
    assert 'if (n === "date")' in script
    assert "single selectable needs saved answer / manual selection" in script
    assert "recoverTextFillLocator" in script
    assert "data-job-agent-fill-target" in script


def test_runtime_captcha_retry_is_bounded_to_one_recovery_attempt():
    script = render_runtime_autofill_script(profile=_profile())

    assert "const CAPTCHA_RECOVERY_ATTEMPTS = 1;" in script
    assert "retryNumber <= CAPTCHA_RECOVERY_ATTEMPTS" in script
    assert "retryNumber <= selfHealPasses" not in script
    assert 'if (text.includes("possible spam")) return false;' in script
    assert 'if (text.includes("too many requests")) return false;' in script
    assert "processingError = captchaRecoveryFailure(retryCaptcha)" in script


def test_runtime_autofill_script_omits_url_when_none():
    script = render_runtime_autofill_script(profile=_profile())

    # no url in payload -> the goto is guarded and skipped at runtime
    assert '"applicationUrl": null' in script
    assert "const applicationUrl = runtimeApplicationUrl(CFG.applicationUrl)" in script
    assert "if (applicationUrl) await page.goto(applicationUrl)" in script


def test_runtime_autofill_script_supports_headless_toggle():
    headed = render_runtime_autofill_script(profile=_profile(), headless=False)
    headless = render_runtime_autofill_script(profile=_profile(), headless=True)

    assert '"headless": false' in headed
    assert '"headless": true' in headless
    assert "const effectiveHeadless" in headed
    assert "process.env.BROWSER_HEADLESS" in headed
    assert "function isAmbientCaptchaPresence" in headed
    assert "waitForManualReview" not in headed
    assert "Browser remains open for manual review" not in headed


def test_runtime_autofill_script_stops_on_generic_newsletter_form(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile=_profile(),
            application_url="https://careers.example.com/updates",
        )
    )
    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const page = {
  url() { return 'https://careers.example.com/updates'; },
  async goto() {},
  locator() { return { first() { return this; } }; },
  getByText() { return { first() { return this; } }; },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [{ kind: 'single', tag: 'input', type: 'email', label: 'Enter email', required: true, options: [], value: '' }];
    }
    return { url: 'https://careers.example.com/updates', title: 'Updates', text: 'Get updates' };
  },
};
module.exports = { chromium: { async launch() { return {
  async newPage() { return page; },
  async close() {},
}; } } };
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
    assert "Application form unavailable:" in result.stdout
    assert "automatic submission not performed" in result.stdout
    assert not (tmp_path / "submission-confirmation.txt").exists()


def test_runtime_autofill_script_navigates_into_embedded_greenhouse_iframe(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile=_profile(),
            application_url="https://www.quantifind.com/open-positions/?gh_jid=7587260",
            max_pages=1,
        )
    )
    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const state = {
  url: 'https://www.quantifind.com/open-positions/?gh_jid=7587260',
  gotos: [],
  values: {},
};

function locator(selector) {
  return {
    first() { return this; },
    last() { return this; },
    filter() { return this; },
    async click() {},
    async fill(value) { state.values[selector] = value; },
    async inputValue() { return state.values[selector] || ''; },
    async selectOption(option) { state.values[selector] = option.label; },
    async setInputFiles(value) { state.values[selector] = value; },
    async check() { state.values[selector] = true; },
    async isChecked() { return Boolean(state.values[selector]); },
    async count() { return selector === '[id="name"]' ? 1 : 0; },
    async isVisible() { return true; },
    async getAttribute() { return ''; },
    async evaluate() { return null; },
    locator() { return this; },
  };
}

const page = {
  url() { return state.url; },
  async goto(url) { state.gotos.push(url); state.url = url; },
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('iframe[src]')) {
      return state.url.includes('quantifind.com')
        ? 'https://boards.greenhouse.io/embed/job_app?for=quantifind&token=7587260'
        : null;
    }
    if (body.includes('input, textarea, select')) {
      if (state.url.includes('quantifind.com')) return [];
      return [{ kind: 'single', tag: 'input', type: 'text', label: 'Full Name', id: 'name', name: '', required: true, options: [], value: '' }];
    }
    if (body.includes('a, button')) return [];
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    if (body.includes('required field remains empty after fill')) return [];
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('GOTOS=' + JSON.stringify(state.gotos)); },
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
    assert 'GOTOS=["https://www.quantifind.com/open-positions/?gh_jid=7587260","https://boards.greenhouse.io/embed/job_app?for=quantifind&token=7587260"]' in result.stdout
    assert "Filled fields (1):" in result.stdout
    assert "Full Name | readback=filled" in result.stdout


def test_runtime_autofill_script_maps_soonest_start_date_from_existing_availability_answer(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["When can you start?"] = "Within a month"
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile=profile,
            application_url="https://job-boards.greenhouse.io/acme/jobs/1",
            max_pages=1,
        )
    )
    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    last() { return this; },
    filter() { return this; },
    async fill(value) { values[selector] = value; },
    async inputValue() { return values[selector] || ''; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async isChecked() { return Boolean(values[selector]); },
    async click() {},
    async count() { return selector === '[id="start_date"]' ? 1 : 0; },
    async isVisible() { return true; },
    async getAttribute() { return ''; },
    async evaluate() { return null; },
    locator() { return this; },
  };
}
const page = {
  url() { return 'https://job-boards.greenhouse.io/acme/jobs/1'; },
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('iframe[src]')) return null;
    if (body.includes('input, textarea, select')) {
      return [{ kind: 'single', tag: 'input', type: 'text', label: 'What is the soonest date you would be available to start?', id: 'start_date', name: '', required: true, options: [], value: '' }];
    }
    if (body.includes('a, button')) return [];
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    if (body.includes('required field remains empty after fill')) return [];
    return null;
  },
};
module.exports = { chromium: { async launch() { return {
  async newPage() { return page; },
  async close() { console.log(JSON.stringify(values)); },
}; } } };
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
    assert "Filled fields (1):" in result.stdout
    assert "What is the soonest date you would be available to start? | readback=filled" in result.stdout
    assert '"[id=\\"start_date\\"]":"Within a month"' in result.stdout


def test_runtime_autofill_script_has_simplify_style_section_engine():
    script = render_runtime_autofill_script(profile=_profile())

    # ATS detection + repeatable section filling (Simplify-style)
    assert "detectATS" in script
    assert "fillRepeatableSection" in script
    assert "mapWorkField" in script
    assert "mapEduField" in script
    assert "clickAddAnother" in script


def test_runtime_autofill_script_includes_optional_capmonster_support():
    script = render_runtime_autofill_script(profile=_profile())

    assert "CAPMONSTER_API_KEY" in script
    assert "CAPMONSTER_SOLVE_CAPTCHA" in script
    assert "createTask" in script
    assert "getTaskResult" in script
    assert "RecaptchaV2Task" in script
    assert "NoCaptchaTaskProxyless" in script
    assert "TurnstileTask" in script
    assert "TurnstileTaskProxyless" in script
    assert "CAPMONSTER_HCAPTCHA_TASK_TYPE" in script
    assert "HCaptchaTaskProxyless" in script
    assert "HCaptchaTask" in script
    assert "ComplexImageTask" in script
    assert "solveComplexImageClicksWithCapMonster" in script
    assert "h-captcha-response" in script
    assert "CAPTCHA_VISION_FALLBACK" in script
    assert "solveHcaptchaWithVision" in script
    assert "visibleHcaptchaChallengeFrame" in script
    assert "frame=challenge" in script
    assert "RecaptchaV3TaskProxyless" in script
    assert "RecaptchaV2EnterpriseTask" in script
    assert "RecaptchaV3EnterpriseTask" in script
    assert "FunCaptchaTask" in script
    assert "GeeTestTask" in script
    assert "DataDome" in script
    assert "CapMonster CAPTCHA:" in script
    assert "captcha recovery failed:" in script
    assert "blocking review fields present" in script
    assert 'if (captchaResult.status !== "skipped")' not in script
    assert "JOB_AGENT_BROWSER_USER_AGENT" in script
    assert "JOB_AGENT_SUBMIT_HUMAN_DELAY_SECONDS" in script
    assert "newContext(browserContextOptions())" in script
    assert "waitBeforeSubmit(page)" in script
    assert "solution userAgent returned" in script
    assert "repairInvalidRequiredFields" in script
    assert "Autofill repair field: " in script
    assert "graduationDateAliases" in script
    assert "Already graduated" in script


def test_runtime_autofill_script_includes_live_screening_answer_rules():
    script = render_runtime_autofill_script(profile=_profile())

    assert "hands on engineering experience" in script
    assert "student or new grad" in script
    assert "expected graduation" in script
    assert "where have you published your work" in script
    assert "monday friday" in script


def test_runtime_autofill_script_groups_unnamed_radios_by_group_label():
    script = render_runtime_autofill_script(profile=_profile())

    assert "const groupLabel = groupLabelFor(c);" in script
    assert "const name = c.name || groupLabel || c.id || c.value || autofillId;" in script


def test_runtime_autofill_script_uses_group_label_for_required_radio_audit():
    script = render_runtime_autofill_script(profile=_profile())

    assert "const groupLabelFor = (control) =>" in script
    assert '? groupLabelFor(control) : labelFor(control);' in script


def test_runtime_autofill_script_carries_work_history_and_education():
    profile = _profile()
    profile["work_history"] = [{"title": "Engineer", "company": "Acme"}]
    profile["education"] = [{"school": "State U", "degree": "B.S."}]
    script = render_runtime_autofill_script(profile=profile)

    assert "Engineer" in script
    assert "State U" in script


def test_runtime_autofill_script_supports_composite_education_dates():
    profile = _profile()
    profile["education"] = [{"school": "State U", "end_date": "2026-05"}]

    script = render_runtime_autofill_script(profile=profile)

    assert "function entryDatePart(entry, boundary, part)" in script
    assert "section: sectionFor(c)" in script
    assert 'end_month: ["end date month"]' in script
    assert 'end_year: ["end date year"]' in script
    assert 'if (last.role === "combobox")' in script


def test_runtime_autofill_script_searches_school_comboboxes():
    script = render_runtime_autofill_script(profile=_profile())

    assert "function isSchoolComboboxField(field)" in script
    assert "async function typeIntoComboboxSearch(page, selector, field, query)" in script
    assert "const schoolLikeField = isSchoolComboboxField(f);" in script


def test_runtime_autofill_script_includes_ai_screening_yes_no_mappings():
    script = render_runtime_autofill_script(profile=_profile())

    assert 'n.includes("large language models")' in script
    assert 'n.includes("working proficiency in python")' in script
    assert 'n.includes("at any point in the future")' in script
    assert 'n.includes("relatives currently work")' in script
    assert 'n.includes("current work status")' in script
    assert 'n.includes("desired compensation")' in script


def test_runtime_autofill_script_embeds_shared_field_semantics():
    script = render_runtime_autofill_script(profile=_profile())

    assert '"fieldSemantics"' in script
    assert '"fieldAutocompleteSemantics"' in script
    assert '"atsAdapters"' in script
    assert 'input[name=\\"preferred_name\\"]' in script
    assert "legalNameSection_firstName" in script
    assert '"education.end.month"' in script
    assert "function semanticForField(fieldOrLabel)" in script
    assert "function semanticValue(semantic, profile)" in script
    assert "function expandedLocationText(value)" in script
    assert "const ashbyRequired = (control) =>" in script


def test_runtime_autofill_script_supports_generic_aria_controls_and_dynamic_fields():
    script = render_runtime_autofill_script(profile=_profile())

    assert "genericPromptLabel" in script
    assert '[contenteditable="true"]' in script
    assert '[role="radio"], [role="checkbox"]' in script
    assert "function selectableState(locator)" in script
    assert "const seenSignatures = new Set([fieldSignature(fields)]);" in script
    assert "data-option-value" in script


def test_runtime_autofill_script_commits_generic_aria_radio_choice(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["Would you like to receive product updates?"] = "Yes"
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
let selected = false;
function locator(selector) {
  const isChoice = selector.includes('choice-yes');
  return {
    first() { return this; },
    async getAttribute(name) {
      if (isChoice && name === 'role') return 'radio';
      if (isChoice && name === 'aria-checked') return selected ? 'true' : 'false';
      return '';
    },
    async click() { if (isChoice) selected = true; },
    async check() { if (isChoice) selected = true; },
    async isChecked() { return selected; },
    async inputValue() { return ''; },
    async fill() {},
    async selectOption() {},
    async setInputFiles() {},
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [{
        kind: 'radiogroup', type: 'radio', label: 'Would you like to receive product updates?',
        name: 'updates', required: true,
        options: [
          { id: '', value: 'yes', label: 'Yes', autofillId: 'choice-yes', custom: true },
          { id: '', value: 'no', label: 'No', autofillId: 'choice-no', custom: true },
        ],
      }];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    return null;
  },
};
module.exports = { chromium: { async launch() { return {
  async newPage() { return page; },
  async close() { console.log('ARIA_SELECTED=' + selected); },
}; } } };
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
    assert "readback=selected: Yes" in result.stdout
    assert "ARIA_SELECTED=true" in result.stdout


def test_runtime_autofill_script_fills_field_revealed_after_prior_choice(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
let revealed = false;
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; if (selector.includes('full-name')) revealed = true; },
    async inputValue() { return values[selector] || ''; },
    async click() {}, async check() {}, async isChecked() { return false; },
    async selectOption() {}, async setInputFiles() {},
  };
}
const page = {
  async goto() {}, locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {}, async waitForTimeout() {},
  async evaluate(fn, arg) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      const fields = [{ kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: 'full-name', required: true, options: [], value: '' }];
      if (revealed) fields.push({ kind: 'single', tag: 'input', type: 'email', label: 'Email', id: 'email', required: true, options: [], value: '' });
      return fields;
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    return null;
  },
};
module.exports = { chromium: { async launch() { return {
  async newPage() { return page; },
  async close() { console.log('DYNAMIC_FIELDS=' + Object.keys(values).length); },
}; } } };
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
    assert "Filled fields (2):" in result.stdout
    assert "DYNAMIC_FIELDS=2" in result.stdout


def test_runtime_autofill_script_maps_standalone_work_and_education_fields(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["work_history"] = [
        {"title": "Engineer", "company": "OldCo", "current": False},
        {"title": "Staff Engineer", "company": "Acme AI", "current": True},
    ]
    profile["education"] = [{"school": "State University", "degree": "B.S.", "field": "Computer Science"}]
    profile["years_experience"] = "4"
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Current company', id: 'company', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Current role', id: 'role', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'select', type: 'select-one', label: 'How many years of relevant, post college work experience do you have?', id: 'years', name: '', required: true, options: ['Select...', '0', '1', '2', '3', '4', '5'], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Which university did you last attend?', id: 'school', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Degree', id: 'degree', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {
          console.log(JSON.stringify(values));
        },
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
    assert "Filled fields (5):" in result.stdout
    assert '"[id=\\"company\\"]":"Acme AI"' in result.stdout
    assert '"[id=\\"role\\"]":"Staff Engineer"' in result.stdout
    assert '"[id=\\"years\\"]":"4"' in result.stdout
    assert '"[id=\\"school\\"]":"State University"' in result.stdout
    assert '"[id=\\"degree\\"]":"B.S."' in result.stdout


def test_runtime_autofill_script_fills_workday_date_sections_from_profile(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["work_history"] = [{"current": True, "start_date": "2022-01-04"}]
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async count() { return 1; },
    async isVisible() { return true; },
    async click() {},
    async fill(value) { values[selector] = value; },
    async press(key) { if (key === 'Backspace') values[selector] = ''; },
    async pressSequentially(value) { values[selector] = value; },
    async inputValue() { return values[selector] || ''; },
    async getAttribute() { return ''; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  keyboard: { async press() {}, async insertText() {}, async type() {} },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Month', id: 'workExperience-startDate-dateSectionMonth-input', name: '', section: 'work', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Day', id: 'workExperience-startDate-dateSectionDay-input', name: '', section: 'work', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Year', id: 'workExperience-startDate-dateSectionYear-input', name: '', section: 'work', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes('required field remains empty after fill')) return [];
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log(JSON.stringify(values)); },
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
    assert "Filled fields (3):" in result.stdout
    assert 'workExperience-startDate-dateSectionMonth-input\\"]":"01"' in result.stdout
    assert 'workExperience-startDate-dateSectionDay-input\\"]":"04"' in result.stdout
    assert 'workExperience-startDate-dateSectionYear-input\\"]":"2022"' in result.stdout


def test_runtime_autofill_script_does_not_use_name_for_pronunciation(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile={"name": "Your Name"}, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Name Pronunciation | How do you pronounce your name?', id: 'pronunciation', name: '', required: false, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log(JSON.stringify(values)); },
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
    assert "Filled fields (0):" in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Your Name" not in result.stdout
    assert '"[id=\\"pronunciation\\"]"' not in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_maps_demographics_to_eeo_only(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = {
        "demographics": {
            "gender": "Prefer not to say",
            "disability": "Prefer not to say",
        }
    }
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'select', type: 'select-one', label: 'Gender', id: 'gender', name: '', required: false, options: ['Male', 'Female', 'Decline To Self Identify'], value: '' },
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Disability Status',
          name: 'disability',
          required: false,
          options: [
            { id: 'disability_yes', value: 'yes', label: 'Yes, I have a disability, or have had one in the past', autofillId: '1' },
            { id: 'disability_no', value: 'no', label: "No, I don't have a disability and have not had one in the past", autofillId: '2' },
            { id: 'disability_decline', value: 'decline', label: 'I do not want to answer', autofillId: '3' },
          ],
        },
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Are you eligible to obtain the security clearance specified in the job description?',
          name: 'clearance',
          required: false,
          options: [
            { id: 'clearance_yes', value: 'yes', label: 'Yes', autofillId: '4' },
            { id: 'clearance_no', value: 'no', label: 'No', autofillId: '5' },
          ],
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log(JSON.stringify(values)); },
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
    assert "Filled fields (0):" in result.stdout
    assert "optional demographic left unselected" in result.stdout
    assert '"[id=\\"gender\\"]"' not in result.stdout
    assert '"[id=\\"disability_decline\\"]"' not in result.stdout
    assert "Submit clicked but confirmation not detected" in result.stdout


def test_runtime_autofill_script_checks_real_consent_only_when_approved(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

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
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'checkbox', label: 'Applicant Arbitration Agreement Acknowledgement', id: 'arbitration', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'checkbox', label: 'I hereby certify that the answers given by me are true and correct', id: 'certify', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'checkbox', label: 'As part of our interview process, we may use AI notetakers to transcribe conversations', id: 'ai_notes', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'checkbox', label: 'Palantir will process your personal data to consider you for employment', id: 'privacy', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log(JSON.stringify(values)); },
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
    assert "Filled fields (4):" in result.stdout
    assert '"[id=\\"arbitration\\"]":true' in result.stdout
    assert '"[id=\\"certify\\"]":true' in result.stdout
    assert '"[id=\\"ai_notes\\"]":true' in result.stdout
    assert '"[id=\\"privacy\\"]":true' in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_executes_with_fake_playwright_submit_gate(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() { values[selector + ':clicked'] = true; },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: 'name', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'email', label: 'Email', id: 'email', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Are you authorized to work in the United States?', id: 'work_auth', name: '', required: false, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake launch headless=' + options.headless);
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "fake launch headless=true" in result.stdout
    assert "Filled fields (3):" in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Are you authorized to work in the United States? | readback=filled" in result.stdout
    assert "Final submit button present: Submit Application" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout
    assert "fake browser closed" in result.stdout


def test_runtime_autofill_script_extracts_explicit_label_without_css_escape(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
const fieldId = 'email:field"1';
const control = {
  id: fieldId,
  tagName: 'INPUT',
  type: 'email',
  required: true,
  options: [],
  value: '',
  offsetParent: {},
  name: 'email',
  setAttribute(name, value) { this[name] = value; },
  getAttribute(name) {
    if (name === 'type') return 'email';
    if (name === 'aria-label') return '';
    if (name === 'placeholder') return '';
    return '';
  },
  closest() { return null; },
};
const label = {
  htmlFor: fieldId,
  textContent: 'Email',
  getAttribute(name) {
    if (name === 'for') return fieldId;
    return '';
  },
};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) {
      if (value !== 'gaoyi@example.com') throw new Error('wrong value ' + value);
      values[selector] = value;
    },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

global.document = {
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [control];
    if (selector === 'label') return [label];
    if (selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") {
      return [{ offsetParent: {}, textContent: 'Submit Application', value: '', id: 'submit', tagName: 'BUTTON' }];
    }
    return [];
  },
};

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) { return fn(arg); },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "Email" in result.stdout
    assert "readback=filled" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_extracts_aria_labelledby_label(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
const control = {
  id: '',
  tagName: 'INPUT',
  type: 'email',
  required: true,
  options: [],
  value: '',
  offsetParent: {},
  name: '',
  setAttribute(name, value) { this[name] = value; },
  getAttribute(name) {
    if (name === 'type') return 'email';
    if (name === 'aria-labelledby') return 'email-label email-helper';
    if (name === 'aria-label') return '';
    if (name === 'placeholder') return '';
    return '';
  },
  closest() { return null; },
};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) {
      if (value !== 'gaoyi@example.com') throw new Error('wrong value ' + value);
      values[selector] = value;
    },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

global.document = {
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [control];
    if (selector === 'label') return [];
    if (selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") {
      return [{ offsetParent: {}, textContent: 'Submit Application', value: '', id: 'submit', tagName: 'BUTTON' }];
    }
    return [];
  },
  getElementById(id) {
    if (id === 'email-label') return { textContent: 'Email' };
    if (id === 'email-helper') return { textContent: 'address' };
    return null;
  },
};

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[data-job-agent-autofill-index="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) { return fn(arg); },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "Email address" in result.stdout
    assert "readback=filled" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_uses_aria_describedby_before_name_fallback(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
const control = {
  id: '',
  tagName: 'INPUT',
  type: 'email',
  required: true,
  options: [],
  value: '',
  offsetParent: {},
  name: 'field_123',
  setAttribute(name, value) { this[name] = value; },
  getAttribute(name) {
    if (name === 'type') return 'email';
    if (name === 'aria-labelledby') return '';
    if (name === 'aria-describedby') return 'email-help';
    if (name === 'aria-label') return '';
    if (name === 'placeholder') return '';
    return '';
  },
  closest() { return null; },
};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) {
      if (value !== 'gaoyi@example.com') throw new Error('wrong value ' + value);
      values[selector] = value;
    },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

global.document = {
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [control];
    if (selector === 'label') return [];
    if (selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") {
      return [{ offsetParent: {}, textContent: 'Submit Application', value: '', id: 'submit', tagName: 'BUTTON' }];
    }
    return [];
  },
  getElementById(id) {
    if (id === 'email-help') return { textContent: 'Email address' };
    return null;
  },
};

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[data-job-agent-autofill-index="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) { return fn(arg); },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "Email address" in result.stdout
    assert "field_123" not in result.stdout
    assert "readback=filled" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_includes_fixed_visible_fields_and_skips_hidden(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function makeControl(id, label, rectCount) {
  return {
    id,
    tagName: 'INPUT',
    type: 'email',
    required: true,
    options: [],
    value: '',
    offsetParent: null,
    name: '',
    setAttribute(name, value) { this[name] = value; },
    getClientRects() { return Array.from({ length: rectCount }, () => ({ width: 100, height: 20 })); },
    getAttribute(name) {
      if (name === 'type') return 'email';
      if (name === 'aria-label') return label;
      if (name === 'placeholder') return '';
      return '';
    },
    closest() { return null; },
  };
}
const fixedControl = makeControl('fixed_email', 'Email', 1);
const hiddenControl = makeControl('hidden_email', 'Hidden Email', 0);

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) {
      if (selector.includes('hidden_email')) throw new Error('hidden field should not be filled');
      if (value !== 'gaoyi@example.com') throw new Error('wrong value ' + value);
      values[selector] = value;
    },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

global.window = {
  getComputedStyle(node) {
    if (node.id === 'hidden_email') return { display: 'none', visibility: 'visible' };
    return { display: 'block', visibility: 'visible' };
  },
};
global.document = {
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [fixedControl, hiddenControl];
    if (selector === 'label') return [];
    if (selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") {
      return [{ offsetParent: {}, textContent: 'Submit Application', value: '', id: 'submit', tagName: 'BUTTON' }];
    }
    return [];
  },
};

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) { return fn(arg); },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "Email" in result.stdout
    assert "Hidden Email" not in result.stdout
    assert "readback=filled" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_headed_closes_without_manual_review_wait(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1, headless=False))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() { values[selector + ':clicked'] = true; },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: 'name', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch(options) {
      console.log('fake launch headless=' + options.headless);
      return {
        async newPage() { return page; },
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
        env={**os.environ, "JOB_AGENT_SUBMIT_COMPLETE": "0"},
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "fake launch headless=false" in result.stdout
    assert "Submit gate: automatic submission not performed" in result.stdout
    assert "Browser remains open for manual review" not in result.stdout
    assert "fake browser closed after manual review" in result.stdout


def test_runtime_autofill_script_blocks_repeated_workday_sign_in_loop(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile={"email": "gaoyi@example.com", "candidate_account_password": "secret"},
            application_url="https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply",
            max_pages=3,
        )
    )

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() { values[selector + ':clicked'] = true; },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
    async count() { return 0; },
    async isVisible() { return false; },
    async evaluate() {},
  };
}

const loginFields = [
  { kind: 'single', tag: 'input', type: 'text', label: 'Email Address*', id: 'email', name: '', required: true, options: [], value: '', autofillId: '0' },
  { kind: 'single', tag: 'input', type: 'password', label: 'Password*', id: 'password', name: '', required: true, options: [], value: '', autofillId: '1' },
];

const page = {
  url() { return 'https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually'; },
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async screenshot() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) return loginFields;
    if (body.includes('button, input[type=\\'button\\'], a')) {
      return [{ text: 'Sign In', id: 'signInSubmitButton', className: '', title: '', ariaLabel: '', href: '', tag: 'button', name: '', inDatepicker: false }];
    }
    if (body.includes('button, input[type=\\'submit\\'], a')) return [];
    if (body.includes('h1,h2,h3,legend')) return '';
    if (body.includes('window.location.href')) {
      return { url: this.url(), title: 'Sign In', text: 'Sign In Email Address Password' };
    }
    if (body.includes('document.body && document.body.innerText')) {
      return { text: 'Sign In Email Address Password' };
    }
    if (body.includes('a, button')) return [];
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {},
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        env={**os.environ, "JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD": "secret"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Pages filled: 2" in result.stdout
    assert "Candidate account sign-in" in result.stdout
    assert "candidate account sign-in rejected by Workday" in result.stdout
    assert "Candidate account required: configured candidate account credentials were rejected by Workday" in result.stdout
    assert "Autofill stats: filled=2 review=1" in result.stdout


def test_runtime_autofill_script_switches_repeated_workday_sign_in_to_create_account(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile={"email": "gaoyi@example.com"},
            application_url="https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply",
            max_pages=3,
        )
    )

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
let mode = 'signIn';

function fields() {
  if (mode === 'create') {
    return [
      { kind: 'single', tag: 'input', type: 'text', label: 'Email Address*', id: 'email', name: '', required: true, options: [], value: '', autofillId: '0' },
      { kind: 'single', tag: 'input', type: 'password', label: 'Password*', id: 'password', name: '', required: true, options: [], value: '', autofillId: '1' },
      { kind: 'single', tag: 'input', type: 'password', label: 'Verify New Password*', id: 'verifyPassword', name: '', required: true, options: [], value: '', autofillId: '2' },
    ];
  }
  return [
    { kind: 'single', tag: 'input', type: 'text', label: 'Email Address*', id: 'email', name: '', required: true, options: [], value: '', autofillId: '0' },
    { kind: 'single', tag: 'input', type: 'password', label: 'Password*', id: 'password', name: '', required: true, options: [], value: '', autofillId: '1' },
  ];
}

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {
      values[selector + ':clicked'] = true;
      if (selector.includes('data-job-agent-button-index="1"') || selector === 'text:Create Account') mode = 'create';
    },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
    async count() { return 1; },
    async isVisible() { return true; },
    async evaluate() {},
  };
}

const page = {
  url() { return 'https://company.wd5.myworkdayjobs.com/en-US/careers/job/123/apply/applyManually'; },
  async goto() {},
  locator,
  getByText(text) { return locator('text:' + text); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async screenshot() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) return fields();
    if (body.includes('button, input[type=\\'button\\'], a')) {
      return mode === 'create'
        ? []
        : [
            { text: 'Sign In', id: 'signInSubmitButton', className: '', title: '', ariaLabel: '', href: '', tag: 'button', name: '', inDatepicker: false, autofillId: '0' },
            { text: 'Create Account', id: '', className: '', title: '', ariaLabel: '', href: '', tag: 'button', name: '', inDatepicker: false, autofillId: '1' },
          ];
    }
    if (body.includes('button, input[type=\\'submit\\'], a')) return [];
    if (body.includes('h1,h2,h3,legend')) return mode === 'create' ? 'Create Account' : '';
    if (body.includes('window.location.href')) {
      return { url: this.url(), title: mode === 'create' ? 'Create Account' : 'Sign In', text: mode === 'create' ? 'Create Account Email Address Password Verify New Password' : 'Sign In Email Address Password Create Account' };
    }
    if (body.includes('document.body && document.body.innerText')) {
      return { text: mode === 'create' ? 'Create Account Email Address Password Verify New Password' : 'Sign In Email Address Password Create Account' };
    }
    if (body.includes('a,button,[role=\\'button\\']') || body.includes('a, button')) {
      return mode === 'create' ? [] : [{ text: 'Create Account', id: '', tag: 'button', href: '', automationId: 'createAccountLink', autofillId: '1' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {},
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        env={**os.environ, "JOB_AGENT_CANDIDATE_ACCOUNT_PASSWORD": "secret"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Verify New Password*" in result.stdout
    assert "Candidate account required: configured candidate account credentials were rejected by Workday" not in result.stdout


def test_runtime_autofill_script_escapes_css_attribute_selectors(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=2))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
let pageIndex = 0;

function validateSelector(selector) {
  const attr = /\\[(id|name|value)="((?:\\\\.|[^"\\\\])*)"\\]/g;
  let pos = 0;
  let match;
  while ((match = attr.exec(selector)) !== null) {
    if (match.index !== pos) throw new Error('malformed selector: ' + selector);
    pos = attr.lastIndex;
  }
  if (pos !== selector.length) throw new Error('malformed selector: ' + selector);
}

function locator(selector) {
  validateSelector(selector);
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {
      values[selector + ':clicked'] = true;
      if (selector.includes('next:step')) pageIndex = 1;
    },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { throw new Error('test should use id attribute selectors'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      if (pageIndex > 0) return [];
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: 'candidate"name', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'email', label: 'Email', id: '', name: 'contact[email]', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) {
      return pageIndex === 0
        ? [{ text: 'Next', id: 'next:step"1', tag: 'button', name: '' }]
        : [];
    }
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit:final', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "malformed selector" not in result.stderr
    assert "Pages filled: 2" in result.stdout
    assert "Filled fields (2):" in result.stdout
    assert "Final submit button present: Submit Application" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_fills_sensitive_field_from_approved_kb(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["Are you authorized to work in the United States?"] = "No"
    profile["sensitive_answers"] = {
        "work_authorization": {
            "patterns": ["authorized to work"],
            "answer": "Yes",
            "approved": True,
        }
    }
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Are you authorized to work in the United States?', id: 'work_auth', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "readback=filled" in result.stdout
    assert 'readback="Yes"' not in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_answers_lever_new_grad_common_groups(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["What type of roles are you looking for?"] = "Full-Time"
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() { values[selector] = true; },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        {
          kind: 'radiogroup',
          tag: 'input',
          type: 'radio',
          label: 'Are you looking for a full-time or internship job?',
          id: '',
          name: 'role_type',
          required: true,
          options: [
            { label: 'Full-time', value: 'Full-time', id: 'role-full-time', autofillId: '1' },
            { label: 'Internship', value: 'Internship', id: 'role-internship', autofillId: '2' },
            { label: 'Both', value: 'Both', id: 'role-both', autofillId: '3' },
          ],
          value: '',
        },
        {
          kind: 'checkboxgroup',
          tag: 'input',
          type: 'checkbox',
          label: 'If there are no suitable positions in U.S, are you open to positions in other countries?',
          id: '',
          name: 'other_countries',
          required: true,
          options: [
            { label: 'Shanghai, China', value: 'Shanghai, China', id: 'shanghai', autofillId: '4' },
            { label: "I'm not open to other locations", value: "I'm not open to other locations", id: 'not-open', autofillId: '5' },
          ],
          value: '',
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return [];
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log(JSON.stringify(values)); },
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
    assert "Filled fields (2):" in result.stdout
    assert "[check] Are you looking for a full-time or internship job? | readback=selected: Full-time" in result.stdout
    assert "[checkmany] If there are no suitable positions in U.S, are you open to positions in other countries? | readback=selected: I'm not open to other locations" in result.stdout
    assert '"[id=\\"role-full-time\\"]":true' in result.stdout
    assert '"[data-job-agent-autofill-index=\\"5\\"]":true' in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_matches_stemmed_sensitive_kb_patterns(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["sensitive_answers"] = {
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
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        {
          kind: 'single',
          tag: 'select',
          type: 'select-one',
          label: 'Work Authorization',
          id: 'work_auth',
          name: '',
          required: true,
          options: ['Yes', 'No'],
          value: '',
        },
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Visa Sponsorship',
          name: 'sponsorship',
          required: true,
          options: [
            { id: 'sponsor_no', value: 'no', label: 'No, I do not require sponsorship' },
            { id: 'sponsor_yes', value: 'yes', label: 'Yes, I require sponsorship' },
          ],
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (2):" in result.stdout
    assert "Work Authorization" in result.stdout
    assert "Visa Sponsorship" in result.stdout
    assert "readback=filled" in result.stdout
    assert "readback=selected: No, I do not require sponsorship" in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_treats_legal_name_as_identity_field(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["sensitive_answers"] = {
        "legal_attestation": {
            "patterns": ["legal attestation"],
            "answer": "Yes",
            "approved": True,
        }
    }
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) {
      if (selector === '[id="legal_first"]' && value !== 'Gaoyi') throw new Error('wrong first name: ' + value);
      if (selector === '[id="legal_last"]' && value !== 'Wu') throw new Error('wrong last name: ' + value);
      values[selector] = value;
    },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Legal First Name', id: 'legal_first', name: '', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'text', label: 'Legal Last Name', id: 'legal_last', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (2):" in result.stdout
    assert "Legal First Name" in result.stdout
    assert "Legal Last Name" in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_matches_verbose_select_and_radio_options(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["Have you built production AI agents?"] = "Yes"
    profile["sensitive_answers"] = {
        "work_authorization": {
            "patterns": ["authorized to work"],
            "answer": "Yes",
            "approved": True,
        }
    }
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        {
          kind: 'single',
          tag: 'select',
          type: 'select-one',
          label: 'Are you authorized to work in the United States?',
          id: 'work_auth',
          name: '',
          required: true,
          options: ['Yes, I am legally authorized to work in the United States', 'No, I require sponsorship'],
          value: '',
        },
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Have you built production AI agents?',
          name: 'built_agents',
          required: false,
          options: [
            { id: 'agents_yes', value: 'yes_prod', label: 'Yes, I have built production AI agent workflows' },
            { id: 'agents_no', value: 'no_prod', label: 'No, not yet' },
          ],
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) {
      return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    }
    return null;
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (2):" in result.stdout
    assert "readback=filled" in result.stdout
    assert "readback=selected: Yes, I have built production AI agent workflows" in result.stdout
    assert 'readback="Yes, I am legally authorized to work in the United States"' not in result.stdout
    assert "yes_prod" not in result.stdout
    assert "Review-required (0):" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_redacts_resume_upload_path_in_report(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    package_dir = tmp_path / "package"
    source_dir = tmp_path / "private"
    package_dir.mkdir()
    source_dir.mkdir()
    resume_path = source_dir / "GAOYI_WU_SDE.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nsource resume")
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile=_profile(),
            resume_file=str(resume_path),
            resume_source_dir=str(source_dir),
            max_pages=1,
        )
    )

    playwright_dir = package_dir / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'file', label: 'Resume / CV', id: 'resume', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=package_dir,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Resume / CV -> file selected" in result.stdout
    assert str(resume_path) not in result.stdout
    assert "readback=file selected" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_uploads_greenhouse_resume_when_label_is_attach(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    package_dir = tmp_path / "package"
    source_dir = tmp_path / "resumes"
    package_dir.mkdir()
    source_dir.mkdir()
    resume_path = source_dir / "GAOYI_WU_SDE.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nsource resume")
    script_path = package_dir / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile=_profile(),
            resume_file=str(resume_path),
            resume_source_dir=str(source_dir),
            max_pages=1,
        )
    )

    playwright_dir = package_dir / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) {
      if (selector !== '[id="resume"]') throw new Error('expected resume id selector, got ' + selector);
      values[selector] = value;
    },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'file', label: 'Attach', id: 'resume', name: '', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
      };
    },
  },
};
"""
    )

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=package_dir,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Attach -> file selected" in result.stdout
    assert str(resume_path) not in result.stdout
    assert "readback=file selected" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_rejects_non_pdf_resume_before_browser_launch(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    resume_path = tmp_path / "tailored-resume.docx"
    resume_path.write_text("fake docx")
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile=_profile(),
            resume_file=str(resume_path),
            max_pages=1,
        )
    )

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
module.exports = {
  chromium: {
    async launch() {
      console.log('BROWSER_LAUNCHED');
      throw new Error('browser should not launch');
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
    assert "resume upload must be an existing PDF" in result.stderr
    assert "BROWSER_LAUNCHED" not in result.stdout


def test_runtime_autofill_script_selects_greenhouse_combobox_option(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["sensitive_answers"] = {
        "work_authorization_current_country": {
            "patterns": ["authorized to work in the country where this position is located"],
            "answer": "Yes",
            "approved": True,
        }
    }
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
let comboOpen = false;
const combo = {
  id: 'question_15678803008',
  tagName: 'INPUT',
  type: 'text',
  required: false,
  options: [],
  value: '',
  offsetParent: {},
  name: '',
  setAttribute(name, value) { this[name] = value; },
  getClientRects() { return [{ width: 100, height: 20 }]; },
    getAttribute(name) {
      if (name === 'type') return 'text';
      if (name === 'role') return 'combobox';
      if (name === 'aria-expanded') return comboOpen ? 'true' : 'false';
    if (name === 'aria-labelledby') return 'question_15678803008-label';
    if (name === 'aria-label') return '';
    if (name === 'placeholder') return '';
    return '';
  },
  closest() { return null; },
};
const backing = {
  id: '',
  tagName: 'INPUT',
  type: 'input',
  required: true,
  options: [],
  value: '',
  offsetParent: {},
  name: '',
  setAttribute() {},
  getClientRects() { return [{ width: 10, height: 10 }]; },
  getAttribute(name) {
    if (name === 'type') return 'input';
    return '';
  },
  closest() { return null; },
};
function option(id, text) {
  return {
    id,
    textContent: text,
    offsetParent: comboOpen ? {} : null,
    getClientRects() { return comboOpen ? [{ width: 80, height: 20 }] : []; },
    setAttribute(name, value) { this[name] = value; },
    click() { if (id === 'work-auth-yes') { combo.value = 'Yes'; comboOpen = false; } },
  };
}
const options = [option('work-auth-yes', 'Yes'), option('work-auth-no', 'No')];

function optionForSelector(selector) {
  return options.find((item) => selector.includes(item['data-job-agent-option-index'] || '__none__'));
}

function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {
      if (
        selector === '[id="question_15678803008"]' ||
        selector.startsWith('[data-job-agent-autofill-index=')
      ) comboOpen = true;
      const item = optionForSelector(selector);
      if (item) item.click();
      if (selector === '[id="work-auth-yes"]') { combo.value = 'Yes'; comboOpen = false; }
    },
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}

global.document = {
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [combo, backing];
    if (selector === 'input, textarea, button, [role="combobox"]') return [combo, backing];
    if (selector === 'label') return [];
    if (selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") return [{ offsetParent: {}, textContent: 'Submit Application', value: '', id: 'submit', tagName: 'BUTTON' }];
    if (selector.includes('[role="option"]')) return options;
    return [];
  },
  getElementById(id) {
    if (id === 'question_15678803008-label') return { textContent: 'Are you legally authorized to work in the country where this position is located?*' };
    return null;
  },
};
global.window = { getComputedStyle() { return { display: 'block', visibility: 'visible' }; } };

const page = {
  async goto() {},
  locator,
  getByText() { return locator('[id="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) { return fn(arg); },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('COMBO=' + combo.value); console.log('fake browser closed'); },
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
    assert "COMBO=Yes" in result.stdout
    assert "unmapped field" not in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_commits_nested_source_radio_before_reporting_success(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const state = { menuOpen: false, nestedOpen: false, radioClicked: false, selected: '' };
const formField = {
  querySelectorAll(selector) {
    if (selector.includes('selectedItem') && state.selected) return [{ textContent: state.selected }];
    return [];
  },
};
const sourceControl = {
  id: 'source-control', tagName: 'INPUT', value: '', name: '', offsetParent: {},
  setAttribute(name, value) { this[name] = value; },
  getClientRects() { return [{ width: 120, height: 24 }]; },
  getAttribute(name) {
    if (name === 'type') return 'text';
    if (name === 'role') return 'combobox';
    if (name === 'aria-expanded') return state.menuOpen ? 'true' : 'false';
    return '';
  },
  closest(selector) { return selector.includes('formField') ? formField : null; },
};

function locator(selector, matchedText = '') {
  const menu = selector.includes('data-automation-id="menuItem"');
  const radio = selector.includes('data-automation-id="radioBtn"');
  const source = selector === '[id="source-control"]';
  const count = () => {
    if (source) return 1;
    if (menu) return state.menuOpen && matchedText === 'Website' ? 1 : 0;
    if (radio) return state.nestedOpen && ['Company website', 'Company Website'].includes(matchedText) ? 1 : 0;
    return 0;
  };
  return {
    first() { return this; },
    last() { return this; },
    filter({ hasText }) { return locator(selector, String(hasText || '')); },
    async count() { return count(); },
    async isVisible() { return count() > 0; },
    async click() {
      if (source) state.menuOpen = true;
      if (menu && matchedText === 'Website') state.nestedOpen = true;
      if (radio && count()) {
        state.radioClicked = true;
        if (process.env.NESTED_SOURCE_COMMITS !== '0') {
          state.selected = 'Company Website';
          sourceControl.value = state.selected;
          state.menuOpen = false;
        }
      }
    },
    async fill(value) { if (source) sourceControl.value = value; },
    async inputValue() { return source ? sourceControl.value : ''; },
    async selectOption() {},
    async setInputFiles() {},
    async check() {},
    async isChecked() { return false; },
  };
}

global.document = {
  activeElement: sourceControl,
  querySelectorAll(selector) {
    if (selector === 'input, textarea, button, [role="combobox"]') return [sourceControl];
    if (selector === 'label' || selector === 'h1,h2,h3,h4,legend') return [];
    return [];
  },
  getElementById(id) { return id === 'source-control' ? sourceControl : null; },
};
global.window = { getComputedStyle() { return { display: 'block', visibility: 'visible' }; } };

const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  keyboard: { async press() {}, async insertText() {}, async type() {} },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [{
        kind: 'single', tag: 'input', type: 'text', role: 'combobox',
        label: 'How did you hear about us?', id: 'source-control', name: '',
        required: true, options: [], value: '',
      }];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    if (body.includes('required field remains empty after fill')) return [];
    return fn(arg);
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {
          console.log('NESTED_RADIO=' + state.radioClicked);
          console.log('SOURCE_VALUE=' + state.selected);
        },
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
    assert "NESTED_RADIO=true" in result.stdout
    assert "SOURCE_VALUE=Company Website" in result.stdout
    assert "Review-required (0):" in result.stdout

    uncommitted = subprocess.run(
        ["node", str(script_path)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
        env={**os.environ, "NESTED_SOURCE_COMMITS": "0"},
    )

    assert uncommitted.returncode == 0, uncommitted.stderr
    assert "NESTED_RADIO=true" in uncommitted.stdout
    assert "SOURCE_VALUE=" in uncommitted.stdout
    assert "SOURCE_VALUE=Company Website" not in uncommitted.stdout
    assert "Submit gate: automatic submission not performed" in uncommitted.stdout


def test_runtime_autofill_script_handles_notion_screening_and_suppresses_stale_required_audit(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"].update(
        {
            "Are you open to working in-person in one of our offices 25% of the time?": "Yes",
            "Please indicate all of the locations that you would be interested in relocating to for this position.": "San Francisco, CA",
        }
    )
    profile["education"] = [{"degree": "Master's Degree"}]

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const state = { values: {}, checked: {} };
const labels = {
  '[data-job-agent-autofill-index="anchor_yes"]': 'Yes',
  '[data-job-agent-autofill-index="anchor_no"]': 'No',
  '[data-job-agent-autofill-index="pronouns_he_auto"]': 'He/Him',
  '[data-job-agent-autofill-index="pronouns_decline_auto"]': 'Prefer not to say',
  '[data-job-agent-autofill-index="degree_masters_auto"]': "Master's Degree",
  '[data-job-agent-autofill-index="reloc_sf_auto"]': 'San Francisco, CA',
};

function hiddenLocator() {
  return {
    first() { return this; },
    locator() { return this; },
    async count() { return 0; },
    async isVisible() { return false; },
    async click() {},
    async isChecked() { return false; },
    async getAttribute() { return ''; },
  };
}

function locator(selector) {
  const label = labels[selector] || '';
  return {
    first() { return this; },
    last() { return this; },
    filter() { return this; },
    locator() { return hiddenLocator(); },
    async count() { return selector === 'text' ? 1 : (label ? 1 : 0); },
    async isVisible() { return true; },
    async click() {
      if (selector.includes('anchor_yes')) state.values.anchor = 'Yes';
      if (label) state.checked[selector] = true;
    },
    async fill(value) { state.values[selector] = value; },
    async inputValue() { return state.values[selector] || ''; },
    async selectOption(option) { state.values[selector] = option.label; },
    async setInputFiles(value) { state.values[selector] = value; },
    async check() { state.checked[selector] = true; },
    async isChecked() { return Boolean(state.checked[selector]); },
    async getAttribute() { return ''; },
    async evaluate() { return null; },
    async textContent() { return label; },
  };
}

global.document = {
  title: '',
  body: { innerText: '', textContent: '' },
  activeElement: null,
  querySelectorAll() { return []; },
  getElementById() { return null; },
};
global.window = {
  location: { href: 'https://jobs.ashbyhq.com/notion/apply' },
  getComputedStyle() { return { display: 'block', visibility: 'visible' }; },
};

const page = {
  url() { return 'https://jobs.ashbyhq.com/notion/apply'; },
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  getByRole() { return locator('text'); },
  keyboard: { async press() {}, async insertText() {}, async type() {} },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        {
          kind: 'buttongroup',
          type: 'button',
          label: 'Are you able to commit to working from one of our offices on Anchor Days each week?',
          name: 'anchor',
          required: true,
          options: [
            { label: 'Yes', value: 'Yes', autofillId: 'anchor_yes' },
            { label: 'No', value: 'No', autofillId: 'anchor_no' },
          ],
        },
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'What pronouns would you like our team to use when addressing you?',
          name: 'pronouns',
          required: true,
          options: [
            { id: 'pronouns_he', value: 'He/Him', label: 'He/Him', autofillId: 'pronouns_he_auto' },
            { id: 'pronouns_decline', value: 'Prefer not to say', label: 'Prefer not to say', autofillId: 'pronouns_decline_auto' },
          ],
        },
        {
          kind: 'checkboxgroup',
          type: 'checkbox',
          label: 'Please indicate all of the locations that you would be interested in relocating to for this position.',
          name: 'relocation',
          required: true,
          options: [
            { id: 'reloc_sf', value: 'San Francisco, CA', label: 'San Francisco, CA', autofillId: 'reloc_sf_auto' },
          ],
        },
        {
          kind: 'checkboxgroup',
          type: 'checkbox',
          label: 'Degree Type',
          name: 'degree_type',
          required: true,
          options: [
            { id: 'degree_masters', value: "Master's Degree", label: "Master's Degree", autofillId: 'degree_masters_auto' },
          ],
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    if (body.includes('required field remains empty after fill')) {
      return [
        { label: 'What pronouns would you like our team to use when addressing you?', reason: 'required field remains empty after fill' },
        { label: 'Please indicate all of the locations that you would be interested in relocating to for this position.', reason: 'required field remains empty after fill' },
        { label: 'Degree Type', reason: 'required field remains empty after fill' },
      ];
    }
    return fn(arg);
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {
          console.log('ANCHOR=' + state.values.anchor);
        },
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
    assert "ANCHOR=Yes" in result.stdout
    assert "[buttonclick] Are you able to commit to working from one of our offices on Anchor Days each week? | readback=filled" in result.stdout
    assert "[check] What pronouns would you like our team to use when addressing you? | readback=selected: Prefer not to say" in result.stdout
    assert "[checkmany] Please indicate all of the locations that you would be interested in relocating to for this position. | readback=selected: San Francisco, CA" in result.stdout
    assert "[checkmany] Degree Type | readback=selected: Master's Degree" in result.stdout
    assert "Review-required (0):" in result.stdout


def test_runtime_autofill_script_maps_official_company_site_source_to_company_job_board(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile.update(
        {
            "target_company": "Siemens Healthineers",
            "job_source": "siemens-healthineers:official-careers",
            "job_source_url": "https://careers.siemens-healthineers.com/global/en/search-results?keywords=Data%20Engineer",
        }
    )
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const state = { menuOpen: false, parentOpen: false, selected: '', parentClicks: 0, leafClicks: 0 };
const formField = {
  querySelectorAll(selector) {
    if (selector.includes('selectedItem') && state.selected) return [{ textContent: state.selected }];
    return [];
  },
};
const sourceControl = {
  id: 'source-control', tagName: 'INPUT', value: '', name: '', offsetParent: {},
  setAttribute(name, value) { this[name] = value; },
  getClientRects() { return [{ width: 120, height: 24 }]; },
  getAttribute(name) {
    if (name === 'type') return 'text';
    if (name === 'role') return 'combobox';
    if (name === 'aria-expanded') return (state.menuOpen || state.parentOpen) ? 'true' : 'false';
    return '';
  },
  closest(selector) { return selector.includes('formField') ? formField : null; },
};

function optionLocator(name) {
  return {
    first() { return this; },
    async click() {
      if (name === 'Job Board' && (state.menuOpen || !state.selected)) {
        state.parentClicks += 1;
        state.parentOpen = true;
        return;
      }
      if (name === 'Siemens Healthineers Job Board' && state.parentOpen) {
        state.leafClicks += 1;
        state.selected = 'Siemens Healthineers Job Board';
        sourceControl.value = state.selected;
        state.menuOpen = false;
        state.parentOpen = false;
        return;
      }
      throw new Error('option not available: ' + name);
    },
  };
}

function locator(selector) {
  const source = selector === '[id="source-control"]';
  return {
    first() { return this; },
    locator() { return this; },
    filter() { return this; },
    async count() { return source ? 1 : 0; },
    async isVisible() { return source; },
    async click() { if (source) state.menuOpen = true; },
    async fill(value) { if (source) sourceControl.value = value; },
    async inputValue() { return source ? sourceControl.value : ''; },
    async selectOption() {},
    async setInputFiles() {},
    async check() {},
    async isChecked() { return false; },
    async textContent() { return state.selected; },
  };
}

global.document = {
  activeElement: sourceControl,
  querySelectorAll(selector) {
    if (selector === 'input, textarea, button, [role="combobox"]') return [sourceControl];
    if (selector.includes('[contenteditable') || selector === 'label' || selector === 'h1,h2,h3,h4,legend') return [];
    return [];
  },
  getElementById(id) { return id === 'source-control' ? sourceControl : null; },
};
global.window = { getComputedStyle() { return { display: 'block', visibility: 'visible' }; } };

const page = {
  async goto() {},
  locator,
  getByRole(role, { name }) {
    if (role === 'option') return optionLocator(String(name || ''));
    return optionLocator('__missing__');
  },
  getByText() { return locator('text'); },
  keyboard: { async press() {}, async insertText() {}, async type() {} },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn, arg) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [{
        kind: 'single', tag: 'input', type: 'text', role: 'combobox',
        label: 'How did you hear about us?', id: 'source-control', name: '',
        required: true, options: [], value: '',
      }];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    if (body.includes('required field remains empty after fill')) return [];
    return fn(arg);
  },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() {
          console.log('PARENT_CLICKS=' + state.parentClicks);
          console.log('LEAF_CLICKS=' + state.leafClicks);
          console.log('SOURCE_VALUE=' + state.selected);
        },
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
    assert "PARENT_CLICKS=1" in result.stdout
    assert "LEAF_CLICKS=1" in result.stdout
    assert "SOURCE_VALUE=Siemens Healthineers Job Board" in result.stdout
    assert "Review-required (0):" in result.stdout


def test_runtime_autofill_script_disambiguates_greenhouse_city_by_state(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile.update({"location": "Jersey City, NJ, USA", "city": "Jersey City"})
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
let comboOpen = false;
let queryReady = false;
let selected = '';
const combo = {
  id: 'location', tagName: 'INPUT', type: 'text', required: true, options: [], value: '',
  offsetParent: {}, name: '',
  setAttribute(name, value) { this[name] = value; },
  getClientRects() { return [{ width: 100, height: 20 }]; },
  getAttribute(name) {
    if (name === 'type') return 'text';
    if (name === 'role') return 'combobox';
    if (name === 'aria-labelledby') return 'location-label';
    return '';
  },
  closest() { return null; },
};
function option(id, text) {
  return {
    id, textContent: text, offsetParent: comboOpen && queryReady ? {} : null,
    getClientRects() { return comboOpen && queryReady ? [{ width: 80, height: 20 }] : []; },
  };
}
const options = [
  option('jersey-city-wi', 'Jersey City, Wisconsin, United States'),
  option('jersey-city-nj', 'Jersey City, New Jersey, United States'),
];
function clickable(text) {
  return {
    first() { return this; }, last() { return this; },
    async click() { selected = text; console.log('SELECTED=' + text); },
  };
}
function locator(selector) {
  return {
    first() { return this; }, last() { return this; },
    filter() { throw new Error('use exact text fallback'); },
    async fill(value) { combo.value = value; queryReady = value === 'Jersey City'; console.log('SEARCH=' + value); },
    async click() {
      if (
        selector === '[id="location"]' ||
        selector.startsWith('[data-job-agent-autofill-index=')
      ) comboOpen = true;
    },
    async inputValue() { return selected || combo.value; },
    async isChecked() { return false; },
  };
}
global.document = {
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [combo];
    if (selector === 'label' || selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") return [];
    if (selector.includes('[role="option"]')) return options;
    return [];
  },
  getElementById(id) {
    if (id === 'location-label') return { textContent: 'Location (City)*' };
    return null;
  },
};
global.window = { getComputedStyle() { return { display: 'block', visibility: 'visible' }; } };
const page = {
  async goto() {}, locator,
  getByText(text) { return clickable(text); },
  async waitForLoadState() {}, async waitForTimeout() {},
  async evaluate(fn, arg) { return fn(arg); },
};
module.exports = { chromium: { async launch() { return {
  async newPage() { return page; },
  async close() { console.log('FINAL=' + selected); },
}; } } };
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
    assert "SEARCH=Jersey City" in result.stdout
    assert "SELECTED=Jersey City, New Jersey, United States" in result.stdout
    assert "FINAL=Jersey City, New Jersey, United States" in result.stdout
    assert "SELECTED=Jersey City, Wisconsin, United States" not in result.stdout


def test_runtime_autofill_script_fills_fields_without_id_or_name_using_marker(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  if (!selector.includes('data-job-agent-autofill-index')) {
    throw new Error('expected fallback selector, got ' + selector);
  }
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('[data-job-agent-autofill-index="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        { kind: 'single', tag: 'input', type: 'text', label: 'Full name', id: '', name: '', autofillId: '0', required: true, options: [], value: '' },
        { kind: 'single', tag: 'input', type: 'email', label: 'Email', id: '', name: '', autofillId: '1', required: true, options: [], value: '' },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (2):" in result.stdout
    assert "readback=filled" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_checks_radio_without_id_name_or_value_using_marker(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["Have you built production AI agents?"] = "Yes"
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  if (!selector.includes('data-job-agent-autofill-index')) {
    throw new Error('expected fallback selector, got ' + selector);
  }
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('[data-job-agent-autofill-index="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Have you built production AI agents?',
          name: '',
          required: false,
          options: [
            { id: '', value: '', label: 'Yes, production systems', autofillId: '4' },
            { id: '', value: '', label: 'No', autofillId: '5' },
          ],
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "readback=selected: Yes, production systems" in result.stdout
    assert "Submit clicked but confirmation not detected:" in result.stdout


def test_runtime_autofill_script_handles_multiple_unnamed_radio_groups_separately(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    profile = _profile()
    profile["answers"]["Have you built production AI agents?"] = "Yes"
    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=profile, max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  if (!selector.includes('data-job-agent-autofill-index')) {
    throw new Error('expected fallback selector, got ' + selector);
  }
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async click() {},
    async inputValue() { return values[selector] || ''; },
    async isChecked() { return Boolean(values[selector]); },
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('[data-job-agent-autofill-index="text"]'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Have you built production AI agents?',
          name: 'Have you built production AI agents?',
          required: false,
          options: [
            { id: '', value: '', label: 'Yes', autofillId: '4' },
            { id: '', value: '', label: 'No', autofillId: '5' },
          ],
        },
        {
          kind: 'radiogroup',
          type: 'radio',
          label: 'Are you open to relocation?',
          name: 'Are you open to relocation?',
          required: false,
          options: [
            { id: '', value: '', label: 'Yes', autofillId: '6' },
            { id: '', value: '', label: 'No', autofillId: '7' },
          ],
        },
      ];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log('fake browser closed'); },
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
    assert "Filled fields (1):" in result.stdout
    assert "Have you built production AI agents?" in result.stdout
    assert "Are you open to relocation?" not in result.stdout
    assert "readback=selected: Yes" in result.stdout
    assert "Submit clicked but confirmation not detected" in result.stdout


def test_runtime_autofill_script_closes_browser_on_runtime_failure(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(render_runtime_autofill_script(profile=_profile(), max_pages=1))

    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const page = {
  async goto() {},
  async evaluate() { throw new Error('dom snapshot failed'); },
};

module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
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
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 1
    assert "fake browser closed after failure" in result.stdout
    assert "Runtime autofill failed: dom snapshot failed" in result.stderr
    assert "Submit gate:" not in result.stdout


def test_runtime_autofill_script_uses_autocomplete_semantics_without_label(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile={"email": "semantics@example.com"},
            max_pages=1,
        )
    )
    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
const values = {};
function locator(selector) {
  return {
    first() { return this; },
    async fill(value) { values[selector] = value; },
    async inputValue() { return values[selector] || ''; },
    async selectOption(option) { values[selector] = option.label; },
    async setInputFiles(value) { values[selector] = value; },
    async check() { values[selector] = true; },
    async isChecked() { return Boolean(values[selector]); },
    async click() {},
  };
}
const page = {
  async goto() {},
  locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {},
  async waitForTimeout() {},
  async evaluate(fn) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [{
        kind: 'single', tag: 'input', type: 'email', label: '',
        id: 'candidate_email', name: '', ariaLabel: '', ariaDescription: '',
        placeholder: '', autocomplete: 'email', section: '', required: true,
        options: [], value: '',
      }];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']")) return [];
    if (body.includes("input[type='submit']")) return [{ text: 'Submit Application', id: 'submit', tag: 'button' }];
    return null;
  },
};
module.exports = {
  chromium: {
    async launch() {
      return {
        async newPage() { return page; },
        async close() { console.log(JSON.stringify(values)); },
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
    assert "Filled fields (1):" in result.stdout
    assert '"[id=\\"candidate_email\\"]":"semantics@example.com"' in result.stdout


def test_runtime_autofill_scopes_combobox_options_to_aria_controls(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node executable is required for runtime script execution test")

    script_path = tmp_path / "autofill-runtime.js"
    script_path.write_text(
        render_runtime_autofill_script(
            profile={"education": [{"end_date": "2026-05"}]},
            max_pages=1,
        )
    )
    playwright_dir = tmp_path / "node_modules" / "playwright"
    playwright_dir.mkdir(parents=True)
    (playwright_dir / "index.js").write_text(
        """
let comboOpen = false;
let selected = '';
function option(id, text) {
  return {
    id, textContent: text, offsetParent: comboOpen ? {} : null,
    getClientRects() { return comboOpen ? [{ width: 80, height: 20 }] : []; },
    setAttribute(name, value) { this[name] = value; },
  };
}
const educationMay = option('education-may', 'May');
const unrelatedMay = option('unrelated-may', 'May');
const educationListbox = {
  offsetParent: {},
  getClientRects() { return [{ width: 100, height: 20 }]; },
  matches() { return false; },
  querySelectorAll() { return [educationMay]; },
};
const unrelatedListbox = {
  offsetParent: {},
  getClientRects() { return [{ width: 100, height: 20 }]; },
  matches() { return false; },
  querySelectorAll() { return [unrelatedMay]; },
};
const combo = {
  id: 'end-month--0', tagName: 'INPUT', type: 'text', required: true,
  value: '', offsetParent: {}, name: '',
  getClientRects() { return [{ width: 100, height: 20 }]; },
  setAttribute() {},
  getAttribute(name) {
    if (name === 'type') return 'text';
    if (name === 'role') return 'combobox';
    if (name === 'aria-controls') return 'education-listbox';
    return '';
  },
  closest() { return null; },
};
function optionForSelector(selector) {
  return [educationMay, unrelatedMay].find((item) => selector.includes(item['data-job-agent-option-index'] || '__none__'));
}
function locator(selector) {
  return {
    first() { return this; }, last() { return this; },
    async click() {
      if (selector === '[id="end-month--0"]') comboOpen = true;
      const item = optionForSelector(selector);
      if (item) { selected = item.id; console.log('SELECTED=' + selected); }
    },
    async fill() {},
    async inputValue() { return selected ? 'May' : ''; },
    async selectOption() {}, async setInputFiles() {}, async check() {},
    async isChecked() { return false; },
    filter() { throw new Error('scoped attribute selector should be used first'); },
  };
}
global.document = {
  activeElement: combo,
  querySelectorAll(selector) {
    if (selector === 'input, textarea, select') return [combo];
    if (selector === '[role="listbox"], [role="menu"]') return [educationListbox, unrelatedListbox];
    if (selector === 'label' || selector === 'h1,h2,h3,h4,legend') return [];
    if (selector === "button, input[type='button'], a") return [];
    if (selector === "button, input[type='submit'], a") return [];
    return [];
  },
  getElementById(id) {
    if (id === 'end-month--0') return combo;
    if (id === 'education-listbox') return educationListbox;
    return null;
  },
};
global.window = { getComputedStyle() { return { display: 'block', visibility: 'visible' }; } };
const page = {
  async goto() {}, locator,
  getByText() { return locator('text'); },
  async waitForLoadState() {}, async waitForTimeout() {},
  async evaluate(fn, arg) {
    const body = String(fn);
    if (body.includes('input, textarea, select')) {
      return [{
        kind: 'single', tag: 'input', type: 'text', label: 'End date month',
        id: 'end-month--0', name: '', role: 'combobox', section: 'education',
        ariaControls: 'education-listbox', required: true, options: [], value: '',
      }];
    }
    if (body.includes('h1,h2,h3,h4,legend')) return false;
    if (body.includes("input[type='button']") || body.includes("input[type='submit']")) return [];
    return fn(arg);
  },
};
module.exports = { chromium: { async launch() { return {
  async newPage() { return page; },
  async close() { console.log('FINAL=' + selected); },
}; } } };
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
    assert "SELECTED=education-may" in result.stdout
    assert "FINAL=education-may" in result.stdout


def test_runtime_embeds_package_truthfulness_blockers():
    script = render_runtime_autofill_script(
        profile={"submission_blockers": ["resume does not substantiate required JD keyword: Rust"]}
    )

    assert "package truthfulness gate" in script
    assert "resume does not substantiate required JD keyword: Rust" in script


def test_generated_runtime_requires_committed_react_selection_and_complete_audit():
    script = render_runtime_autofill_script(profile=_profile())

    assert "const reactSelectRoot = control.closest" in script
    assert '[class*="select__single-value"]' in script
    assert "const nativeInvalid = committed ? false" in script
    assert "labelAppearsRequired(labelFor(control))" in script
    assert "Greenhouse option click did not commit a selected value" in script
    assert "recoverApplicationFormFromJobPage" in script
    assert (
        'if (!verifiedSelection && clicked && String(page.url && page.url() || "").toLowerCase().includes("greenhouse.io"))'
        not in script
    )
