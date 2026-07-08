from hello_agents import ToolRegistry
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.models import JobApplicationState
from hello_agents.tools.builtin.career import (
    ApplicationTrackerTool,
    ApplicationPackageTool,
    FitScorerTool,
    JDParserTool,
    ManualJDImportTool,
    ResumeIndexerTool,
    ResumeSelectorTool,
    ResumeTailorTool,
    ReviewPacketTool,
    SubmitGateTool,
    TruthfulnessCheckTool,
)


class FakeLLM:
    provider = "fake"

    def invoke(self, messages, **kwargs):
        return "fake response"


def test_career_tools_register_with_hello_agents_registry():
    registry = ToolRegistry()

    registry.register_tool(ManualJDImportTool())
    registry.register_tool(ApplicationTrackerTool())
    registry.register_tool(ApplicationPackageTool())
    registry.register_tool(FitScorerTool())
    registry.register_tool(JDParserTool())
    registry.register_tool(ResumeIndexerTool())
    registry.register_tool(ResumeSelectorTool())
    registry.register_tool(ResumeTailorTool())
    registry.register_tool(ReviewPacketTool())
    registry.register_tool(SubmitGateTool())
    registry.register_tool(TruthfulnessCheckTool())

    assert {
        "manual_jd_import",
        "application_tracker",
        "application_package",
        "fit_scorer",
        "jd_parser",
        "resume_indexer",
        "resume_selector",
        "resume_tailor",
        "review_packet",
        "submit_gate",
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


def test_application_tracker_tool_creates_application_record(tmp_path):
    db_path = tmp_path / "agent.db"
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents."

    result = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": jd})

    assert "application_id=1" in result
    assert "status=needs_review" in result
    assert db_path.exists()
