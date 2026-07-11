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
        resume_file="/tmp/tailored-resume.docx",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
    )

    assert 'require("playwright")' in script
    assert "chromium.launch" in script
    # profile + url are embedded
    assert "Gaoyi Wu" in script
    assert "gaoyi@example.com" in script
    assert "https://boards.greenhouse.io/acme/jobs/1" in script
    assert "/tmp/tailored-resume.docx" in script
    # Simplify-style engine pieces are present
    assert "scrapeFields" in script
    assert "findNextButton" in script
    assert "findSubmitButton" in script
    assert "planField" in script
    # safety gate: stops before submit, never auto-submits
    assert "STOPPED before final Submit" in script


def test_runtime_autofill_script_omits_url_when_none():
    script = render_runtime_autofill_script(profile=_profile())

    # no url in payload -> the goto is guarded and skipped at runtime
    assert '"applicationUrl": null' in script
    assert "if (CFG.applicationUrl)" in script


def test_runtime_autofill_script_supports_headless_toggle():
    headed = render_runtime_autofill_script(profile=_profile(), headless=False)
    headless = render_runtime_autofill_script(profile=_profile(), headless=True)

    assert '"headless": false' in headed
    assert '"headless": true' in headless


def test_runtime_autofill_script_has_simplify_style_section_engine():
    script = render_runtime_autofill_script(profile=_profile())

    # ATS detection + repeatable section filling (Simplify-style)
    assert "detectATS" in script
    assert "fillRepeatableSection" in script
    assert "mapWorkField" in script
    assert "mapEduField" in script
    assert "clickAddAnother" in script


def test_runtime_autofill_script_carries_work_history_and_education():
    profile = _profile()
    profile["work_history"] = [{"title": "Engineer", "company": "Acme"}]
    profile["education"] = [{"school": "State U", "degree": "B.S."}]
    script = render_runtime_autofill_script(profile=profile)

    assert "Engineer" in script
    assert "State U" in script
