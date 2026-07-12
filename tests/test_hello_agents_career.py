import json

from hello_agents import ToolRegistry
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.models import JobApplicationState
from hello_agents.tools.builtin.career import (
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
    ResumeDraftTool,
    ResumeIndexerTool,
    ResumeSelectorTool,
    ResumeTailorTool,
    ReviewPacketTool,
    RSSJobSourceTool,
    SubmitGateTool,
    SensitiveFieldDetectorTool,
    TruthfulnessCheckTool,
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
    registry.register_tool(ResumeTailorTool())
    registry.register_tool(ReviewPacketTool())
    registry.register_tool(RemotiveJobSourceTool())
    registry.register_tool(RSSJobSourceTool())
    registry.register_tool(ResumeDraftTool())
    registry.register_tool(SubmitGateTool())
    registry.register_tool(SensitiveFieldDetectorTool())
    registry.register_tool(TruthfulnessCheckTool())

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
        "resume_tailor",
        "review_packet",
        "remotive_job_source",
        "rss_job_source",
        "resume_draft",
        "submit_gate",
        "sensitive_field_detector",
        "truthfulness_check",
    } <= set(registry.list_tools())


def test_job_application_agent_reviews_manual_jd():
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM())
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents with LangChain and FastAPI."

    result = agent.run(jd)

    assert "# Application Review" in result
    assert "Agent Engineer" in result
    assert "Final Submit remains manual" in result


def test_job_application_agent_includes_jd_analysis_and_resume_plan(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM(), resume_source_dir=tmp_path)
    jd = "Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents with LangChain, RAG, FastAPI, and Rust."

    result = agent.run(jd)

    assert "## JD Analysis" in result
    assert '"role_track": "Agent Engineer"' in result
    assert "## Resume Edit Plan" in result
    assert "LangChain" in result
    assert "unsupported_keywords" in result
    assert "Rust" in result
    assert "## Truthfulness Gate" in result


def test_job_application_agent_selects_resume_and_tracks_application(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
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
    assert "GAOYI_WU_Agent_Engineer.docx" in result
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
    assert (package_dir / "resume-edit-plan.json").exists()
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
    assert "## LLM Review Notes" in result
    assert "Prioritize agent workflow achievements" in result


def test_job_application_state_starts_with_manual_submit_gate():
    state = JobApplicationState()

    assert state.status == "new"
    assert state.form_plan.can_auto_submit is False


def test_resume_indexer_tool_lists_templates(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")

    result = ResumeIndexerTool().run({"source_dir": str(tmp_path)})

    assert "Agent Engineer" in result
    assert "GAOYI_WU_Agent_Engineer.docx" in result


def test_resume_selector_tool_selects_track_from_jd(tmp_path):
    (tmp_path / "GAOYI_WU_ML_Infra.docx").write_text("docx")
    jd = "Title: ML Infrastructure Engineer\n\nBuild Kubernetes, Kafka, and MLflow pipelines."

    result = ResumeSelectorTool().run({"source_dir": str(tmp_path), "jd_text": jd})

    assert "selected_track=ML Infra" in result
    assert "GAOYI_WU_ML_Infra.docx" in result


def test_jd_parser_tool_returns_structured_analysis():
    jd = "Title: Agent Engineer\n\nBuild LangChain tools, RAG workflows, and FastAPI services."

    result = JDParserTool().run({"jd_text": jd})

    assert '"role_track": "Agent Engineer"' in result
    assert '"LangChain"' in result
    assert '"FastAPI"' in result


def test_resume_tailor_tool_flags_unsupported_keywords():
    jd = "Title: Agent Engineer\n\nBuild LangChain agents with Rust and RAG."

    result = ResumeTailorTool().run({"jd_text": jd, "resume_track": "Agent Engineer"})

    assert '"target_track": "Agent Engineer"' in result
    assert '"LangChain"' in result
    assert '"unsupported_keywords": [' in result
    assert '"Rust"' in result


def test_resume_tailor_tool_uses_source_resume_as_evidence():
    result = json.loads(
        ResumeTailorTool().run(
            {
                "jd_text": "Title: Agent Engineer\n\nBuild LangChain agents with FastAPI.",
                "resume_text": "Built FastAPI services.",
            }
        )
    )

    assert result["summary_keywords"] == ["FastAPI"]
    assert result["unsupported_keywords"] == ["LangChain"]


def test_resume_draft_tool_generates_grounded_markdown():
    jd = "Title: Agent Engineer\n\nBuild LangChain agents with FastAPI and Rust."
    resume_text = "Gaoyi Wu\n\nBuilt Python and FastAPI services."

    result = ResumeDraftTool().run({"jd_text": jd, "resume_text": resume_text})

    assert "# Tailored Resume Draft" in result
    assert "LangChain" in result
    assert "FastAPI" in result
    assert "Unsupported JD keywords not inserted: LangChain, Rust" in result
    assert resume_text in result


def test_truthfulness_check_tool_blocks_unsupported_claims():
    plan_json = '{"unsupported_keywords": ["Rust"]}'

    result = TruthfulnessCheckTool().run({"plan_json": plan_json})

    assert "truthfulness_status=needs_review" in result
    assert "Rust" in result


def test_application_package_tool_writes_review_artifacts(tmp_path):
    jd = "Company: Acme AI\nTitle: Agent Engineer\n\nBuild LLM agents with LangChain."
    out_dir = tmp_path / "application-package"

    result = ApplicationPackageTool().run({"jd_text": jd, "output_dir": str(out_dir)})

    assert "package_dir=" in result
    assert (out_dir / "review.md").read_text().startswith("# Application Review")
    assert '"role_track": "Agent Engineer"' in (out_dir / "jd-analysis.json").read_text()
    assert '"target_track": "Agent Engineer"' in (out_dir / "resume-edit-plan.json").read_text()
    assert "Final Submit remains manual" in (out_dir / "submit-gate.txt").read_text()


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


def test_form_fill_script_tool_can_upload_resume_file():
    snapshot = '[{"label": "Resume", "type": "file", "required": true}]'
    profile = '{"email": "gaoyi@example.com"}'

    result = FormFillScriptTool().run(
        {
            "form_snapshot_json": snapshot,
            "profile_json": profile,
            "application_url": "https://jobs.example.com/apply",
            "resume_file": "/tmp/tailored-resume.pdf",
        }
    )

    assert 'await page.getByLabel("Resume").setInputFiles("/tmp/tailored-resume.pdf");' in result
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


def test_remotive_job_source_tool_returns_normalized_jobs_json():
    payload = '{"jobs": [{"title": "Backend Engineer", "company_name": "RemoteCo", "url": "https://remotive.com/jobs/1", "candidate_required_location": "Worldwide", "description": "Build APIs."}]}'

    result = RemotiveJobSourceTool().run({"payload_json": payload})

    assert '"title": "Backend Engineer"' in result
    assert '"company": "RemoteCo"' in result
    assert '"source": "remotive"' in result
    assert '"apply_url": "https://remotive.com/jobs/1"' in result
