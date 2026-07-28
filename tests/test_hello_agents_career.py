from hello_agents import ToolRegistry
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.models import JobApplicationState
from hello_agents.tools.builtin.career import (
    AshbyJobSourceTool,
    ApplicationTrackerTool,
    ApplicationPackageTool,
    FitScorerTool,
    FormFillerTool,
    FormFillScriptTool,
    FormInspectorTool,
    FormSnapshotScriptTool,
    GreenhouseJobSourceTool,
    JDParserTool,
    LeverJobSourceTool,
    ManualJDImportTool,
    RemotiveJobSourceTool,
    ResumeIndexerTool,
    ResumeSelectorTool,
    ReviewPacketTool,
    RSSJobSourceTool,
    SubmitGateTool,
    SensitiveFieldDetectorTool,
)


class FakeLLM:
    provider = "fake"

    def invoke(self, messages, **kwargs):
        return "fake response"


class RecordingLLM:
    provider = "openai"

    def __init__(self):
        self.messages = []

    def invoke(self, messages, **kwargs):
        self.messages.append(messages)
        return "Prioritize agent workflow achievements and keep claims truthful."


def test_career_tools_register_with_hello_agents_registry():
    registry = ToolRegistry()

    registry.register_tool(ManualJDImportTool())
    registry.register_tool(ApplicationTrackerTool())
    registry.register_tool(ApplicationPackageTool())
    registry.register_tool(FitScorerTool())
    registry.register_tool(FormInspectorTool())
    registry.register_tool(FormFillerTool())
    registry.register_tool(FormFillScriptTool())
    registry.register_tool(FormSnapshotScriptTool())
    registry.register_tool(GreenhouseJobSourceTool())
    registry.register_tool(JDParserTool())
    registry.register_tool(LeverJobSourceTool())
    registry.register_tool(ResumeIndexerTool())
    registry.register_tool(ResumeSelectorTool())
    registry.register_tool(ReviewPacketTool())
    registry.register_tool(RemotiveJobSourceTool())
    registry.register_tool(RSSJobSourceTool())
    registry.register_tool(SubmitGateTool())
    registry.register_tool(SensitiveFieldDetectorTool())

    assert {
        "manual_jd_import",
        "application_tracker",
        "application_package",
        "fit_scorer",
        "form_inspector",
        "form_filler",
        "form_fill_script",
        "form_snapshot_script",
        "greenhouse_job_source",
        "jd_parser",
        "lever_job_source",
        "resume_indexer",
        "resume_selector",
        "review_packet",
        "remotive_job_source",
        "rss_job_source",
        "submit_gate",
        "sensitive_field_detector",
    } <= set(registry.list_tools())


def test_job_application_agent_reviews_manual_jd():
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM())
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents with LangChain and FastAPI."

    result = agent.run(jd)

    assert "# Application Review" in result
    assert "Agent Engineer" in result
    assert "Automatic final submission is enabled" in result


def test_job_application_agent_records_review_and_safety_history():
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM())
    jd = "Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents."

    result = agent.run(jd)

    history = agent.get_history()
    roles = [message.role for message in history]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"
    assert roles.count("observation") >= 2
    assert roles.count("safety_gate") >= 1
    assert roles.count("thought") >= 1
    assert roles.count("action") >= 1
    assert roles.count("memory_update") >= 1
    assert history[0].content == jd
    sections = [message.metadata.get("section") for message in history[1:-1]]
    assert "jd_analysis" in sections
    assert "fit_score" in sections
    assert "submit_gate" in sections
    assert history[-1].content == result
    assert "## Submit Gate" in history[-1].content
    assert "Automatic final submission is enabled" in history[-1].content


def test_job_application_agent_updates_last_state_with_fit_resume_and_gates(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")
    snapshot = '[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]'
    profile = '{"email": "gaoyi@example.com", "sponsorship": "Needs review"}'
    agent = JobApplicationAgent(
        name="career-agent",
        llm=FakeLLM(),
        resume_source_dir=tmp_path,
        form_snapshot_json=snapshot,
        profile_json=profile,
    )

    agent.run("Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents.")

    state = agent.get_last_state()
    assert state is not None
    assert state.job is not None
    assert state.job.company == "Acme AI"
    assert state.fit_score is not None
    assert state.fit_score.role_track == "Agent Engineer"
    assert state.selected_resume is not None
    assert state.selected_resume.track == "Agent Engineer"
    assert state.jd_analysis is not None
    assert state.submit_gate is not None
    assert state.form_fields is not None
    assert state.sensitive_fields is not None
    assert state.form_plan.review_required_fields == ["Do you require visa sponsorship?"]
    assert state.status == "manual_review_required"
    assert len(state.safety_gates) >= 2


def test_job_application_agent_includes_jd_analysis_and_selected_pdf(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM(), resume_source_dir=tmp_path)
    jd = "Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents with LangChain, RAG, FastAPI, and Rust."

    result = agent.run(jd)

    assert "## JD Analysis" in result
    assert '"role_track": "Agent Engineer"' in result
    assert "## Recommended Resume" in result
    assert "GAOYI_WU_Agent_Engineer.pdf" in result


def test_job_application_agent_selects_resume_and_tracks_application(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")
    db_path = tmp_path / "agent.db"
    agent = JobApplicationAgent(
        name="career-agent",
        llm=FakeLLM(),
        resume_source_dir=tmp_path,
        database_path=db_path,
    )
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents with LangChain and FastAPI."

    result = agent.run(jd)

    assert "## Recommended Resume" in result
    assert "selected_track=Agent Engineer" in result
    assert "GAOYI_WU_Agent_Engineer.pdf" in result
    assert "## Tracking" in result
    assert "application_id=1" in result


def test_job_application_agent_exports_application_package(tmp_path):
    package_dir = tmp_path / "package"
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM(), package_dir=package_dir)
    jd = "Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents with LangChain."

    result = agent.run(jd)

    assert "## Application Package" in result
    assert "package_dir=" in result
    assert (package_dir / "review.md").exists()
    assert (package_dir / "jd-analysis.json").exists()
    assert not (package_dir / "resume-edit-plan.json").exists()
    assert (package_dir / "submit-gate.txt").exists()


def test_job_application_agent_includes_form_fill_plan():
    snapshot = '[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]'
    profile = '{"email": "gaoyi@example.com", "sponsorship": "Needs review"}'
    agent = JobApplicationAgent(
        name="career-agent",
        llm=FakeLLM(),
        form_snapshot_json=snapshot,
        profile_json=profile,
    )

    result = agent.run("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")

    assert "## Form Fill Plan" in result
    assert "Email=gaoyi@example.com" in result
    assert "review_required=Do you require visa sponsorship?" in result


def test_job_application_agent_uses_llm_for_review_notes_when_enabled():
    llm = RecordingLLM()
    agent = JobApplicationAgent(name="career-agent", llm=llm)

    result = agent.run("Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.")

    assert llm.messages
    assert "Recent Tool Results:" in llm.messages[0][0]["content"]
    assert "Long-term memory summaries:" in llm.messages[0][0]["content"]
    assert "profile_json" not in llm.messages[0][0]["content"]
    assert "## LLM Review Notes" in result
    assert "Prioritize agent workflow achievements" in result
    state = agent.get_last_state()
    assert state is not None
    assert state.thoughts
    assert (
        state.thoughts[0].summary
        == "Prioritize agent workflow achievements and keep claims truthful."
    )


def test_job_application_state_starts_with_automatic_submit_policy():
    state = JobApplicationState()

    assert state.status == "new"
    assert state.form_plan.can_auto_submit is True


def test_resume_indexer_tool_lists_templates(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")

    result = ResumeIndexerTool().run({"source_dir": str(tmp_path)})

    assert "Agent Engineer" in result
    assert "GAOYI_WU_Agent_Engineer.pdf" in result


def test_resume_selector_tool_selects_track_from_jd(tmp_path):
    (tmp_path / "GAOYI_WU_ML_Infra.pdf").write_text("pdf")
    jd = "Title: ML Infrastructure Engineer\n\nBuild Kubernetes, Kafka, and MLflow pipelines."

    result = ResumeSelectorTool().run({"source_dir": str(tmp_path), "jd_text": jd})

    assert "selected_track=ML Infra" in result
    assert "GAOYI_WU_ML_Infra.pdf" in result


def test_jd_parser_tool_returns_structured_analysis():
    jd = "Title: Agent Engineer\n\nBuild LangChain tools, RAG workflows, and FastAPI services."

    result = JDParserTool().run({"jd_text": jd})

    assert '"role_track": "Agent Engineer"' in result
    assert '"LangChain"' in result
    assert '"FastAPI"' in result


def test_application_package_tool_writes_review_artifacts(tmp_path):
    jd = "Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents with LangChain."
    out_dir = tmp_path / "application-package"

    result = ApplicationPackageTool().run({"jd_text": jd, "output_dir": str(out_dir)})

    assert "package_dir=" in result
    assert (out_dir / "review.md").read_text().startswith("# Application Review")
    assert '"role_track": "Agent Engineer"' in (out_dir / "jd-analysis.json").read_text()
    assert not (out_dir / "resume-edit-plan.json").exists()
    assert "Automatic final submission is enabled" in (out_dir / "submit-gate.txt").read_text()


def test_form_inspector_tool_normalizes_field_snapshot():
    snapshot = '[{"label": "Email", "type": "email", "required": true}, {"label": "Sponsorship", "type": "radio"}]'

    result = FormInspectorTool().run({"form_snapshot_json": snapshot})

    assert '"label": "Email"' in result
    assert '"required": true' in result
    assert '"label": "Sponsorship"' in result


def test_sensitive_field_detector_flags_sponsorship():
    snapshot = '[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]'

    result = SensitiveFieldDetectorTool().run({"form_snapshot_json": snapshot})

    assert "sensitive_fields=Do you require visa sponsorship?" in result


def test_form_filler_tool_creates_review_required_plan():
    snapshot = '[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]'
    profile = '{"email": "gaoyi@example.com", "sponsorship": "Needs review"}'

    result = FormFillerTool().run({"form_snapshot_json": snapshot, "profile_json": profile})

    assert "can_auto_submit=False" in result
    assert "Email=gaoyi@example.com" in result
    assert "review_required=Do you require visa sponsorship?" in result


def test_form_fill_script_tool_generates_guarded_playwright_script():
    snapshot = '[{"label": "Email"}, {"label": "Do you require visa sponsorship?"}]'
    profile = '{"email": "gaoyi@example.com", "sponsorship": "Needs review"}'

    result = FormFillScriptTool().run(
        {
            "form_snapshot_json": snapshot,
            "profile_json": profile,
            "application_url": "https://jobs.example.com/apply",
        }
    )

    assert 'await page.goto("https://jobs.example.com/apply");' in result
    assert 'await page.getByLabel("Email").fill("gaoyi@example.com");' in result
    assert "Do you require visa sponsorship?" in result
    assert ".click(" not in result


def test_form_fill_script_tool_can_upload_resume_file(tmp_path, monkeypatch):
    snapshot = '[{"label": "Resume", "type": "file", "required": true}]'
    profile = '{"email": "gaoyi@example.com"}'
    resume_dir = tmp_path / "resumes"
    resume_dir.mkdir()
    resume_path = resume_dir / "GAOYI_WU_SDE.pdf"
    resume_path.write_bytes(b"%PDF-1.4\nsource resume")
    monkeypatch.setenv("RESUME_SOURCE_DIR", str(resume_dir))

    result = FormFillScriptTool().run(
        {
            "form_snapshot_json": snapshot,
            "profile_json": profile,
            "application_url": "https://jobs.example.com/apply",
            "resume_file": str(resume_path),
        }
    )

    assert f'await page.getByLabel("Resume").setInputFiles("{resume_path.resolve()}");' in result
    assert ".click(" not in result


def test_form_snapshot_script_tool_generates_inspection_only_script():
    result = FormSnapshotScriptTool().run(
        {
            "application_url": "https://jobs.example.com/apply",
            "output_path": "form-snapshot.json",
        }
    )

    assert 'await page.goto("https://jobs.example.com/apply");' in result
    assert 'fs.writeFileSync("form-snapshot.json"' in result
    assert "querySelectorAll" in result
    assert ".fill(" not in result
    assert ".click(" not in result


def test_application_tracker_tool_creates_application_record(tmp_path):
    db_path = tmp_path / "agent.db"
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents."

    result = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": jd})

    assert "application_id=1" in result
    assert "status=needs_review" in result
    assert db_path.exists()

    repeated = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": jd})
    assert "job_id=1" in repeated
    assert "application_id=1" in repeated


def test_application_tracker_tool_does_not_reuse_same_title_with_different_url(tmp_path):
    db_path = tmp_path / "agent.db"
    first_jd = (
        "Company: Acme AI\n"
        "Title: Agent Engineer\n"
        "Location: Remote\n"
        "Apply URL: https://jobs.example.com/acme/1\n\n"
        "Build LLM agents."
    )
    second_jd = (
        "Company: Acme AI\n"
        "Title: Agent Engineer\n"
        "Location: Remote\n"
        "Apply URL: https://jobs.example.com/acme/2\n\n"
        "Build LLM agents for a different team."
    )

    first = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": first_jd})
    second = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": second_jd})

    assert "application_id=1" in first
    assert "application_id=2" in second


def test_application_tracker_tool_reuses_tracking_url_variants(tmp_path):
    db_path = tmp_path / "agent.db"
    first_jd = (
        "Company: Acme AI\n"
        "Title: Agent Engineer\n"
        "Apply URL: https://jobs.example.com/acme/1?utm_source=rss\n\n"
        "Build LLM agents."
    )
    second_jd = (
        "Company: Acme AI\n"
        "Title: Agent Engineer\n"
        "Apply URL: https://jobs.example.com/acme/1?ref=careers\n\n"
        "Build LLM agents."
    )

    first = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": first_jd})
    second = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": second_jd})

    assert "application_id=1" in first
    assert "job_id=1" in second
    assert "application_id=1" in second


def test_rss_job_source_tool_returns_normalized_jobs_json():
    rss = """<rss><channel><item>
    <title>Agent Engineer at Acme AI</title>
    <link>https://jobs.example.com/acme-agent</link>
    <description>Build LLM agents with FastAPI.</description>
    </item></channel></rss>"""

    result = RSSJobSourceTool().run({"rss_xml": rss, "source": "example-rss"})

    assert '"title": "Agent Engineer"' in result
    assert '"company": "Acme AI"' in result
    assert '"source": "example-rss"' in result
    assert '"apply_url": "https://jobs.example.com/acme-agent"' in result


def test_greenhouse_job_source_tool_returns_normalized_jobs_json():
    payload = '{"jobs": [{"title": "Agent Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}, "content": "Build agents."}]}'

    result = GreenhouseJobSourceTool().run({"board_token": "acme", "payload_json": payload})

    assert '"title": "Agent Engineer"' in result
    assert '"company": "acme"' in result
    assert '"source": "greenhouse:acme"' in result
    assert '"apply_url": "https://boards.greenhouse.io/acme/jobs/1"' in result


def test_lever_job_source_tool_returns_normalized_jobs_json():
    payload = '[{"text": "ML Platform Engineer", "hostedUrl": "https://jobs.lever.co/acme/1", "categories": {"location": "Remote"}, "descriptionPlain": "Build ML platforms."}]'

    result = LeverJobSourceTool().run({"site": "acme", "payload_json": payload})

    assert '"title": "ML Platform Engineer"' in result
    assert '"company": "acme"' in result
    assert '"source": "lever:acme"' in result
    assert '"apply_url": "https://jobs.lever.co/acme/1"' in result


def test_ashby_job_source_tool_returns_normalized_jobs_json():
    payload = '{"jobs": [{"title": "AI Product Engineer", "jobUrl": "https://jobs.ashbyhq.com/brainco/1", "applyUrl": "https://jobs.ashbyhq.com/brainco/1/application", "location": "San Francisco", "descriptionHtml": "Build AI products."}]}'

    result = AshbyJobSourceTool().run({"organization": "brainco", "payload_json": payload})

    assert '"title": "AI Product Engineer"' in result
    assert '"company": "brainco"' in result
    assert '"source": "ashby:brainco"' in result
    assert '"apply_url": "https://jobs.ashbyhq.com/brainco/1/application"' in result


def test_remotive_job_source_tool_returns_normalized_jobs_json():
    payload = '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs."}]}'

    result = RemotiveJobSourceTool().run({"payload_json": payload})

    assert '"title": "Backend Engineer"' in result
    assert '"company": "RemoteCo"' in result
    assert '"source": "remotive"' in result
    assert '"apply_url": "https://remotive.com/jobs/1"' in result
