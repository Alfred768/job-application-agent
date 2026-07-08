from hello_agents import ToolRegistry
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.models import JobApplicationState
from hello_agents.tools.builtin.career import (
    ApplicationTrackerTool,
    FitScorerTool,
    ManualJDImportTool,
    ResumeIndexerTool,
    ResumeSelectorTool,
    ReviewPacketTool,
    SubmitGateTool,
)


class FakeLLM:
    provider = "fake"

    def invoke(self, messages, **kwargs):
        return "fake response"


def test_career_tools_register_with_hello_agents_registry():
    registry = ToolRegistry()

    registry.register_tool(ManualJDImportTool())
    registry.register_tool(ApplicationTrackerTool())
    registry.register_tool(FitScorerTool())
    registry.register_tool(ResumeIndexerTool())
    registry.register_tool(ResumeSelectorTool())
    registry.register_tool(ReviewPacketTool())
    registry.register_tool(SubmitGateTool())

    assert {
        "manual_jd_import",
        "application_tracker",
        "fit_scorer",
        "resume_indexer",
        "resume_selector",
        "review_packet",
        "submit_gate",
    } <= set(registry.list_tools())


def test_job_application_agent_reviews_manual_jd():
    agent = JobApplicationAgent(name="career-agent", llm=FakeLLM())
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents with LangChain and FastAPI."

    result = agent.run(jd)

    assert "# Application Review" in result
    assert "Agent Engineer" in result
    assert "Final Submit remains manual" in result


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


def test_application_tracker_tool_creates_application_record(tmp_path):
    db_path = tmp_path / "agent.db"
    jd = "Company: Acme AI\nTitle: Agent Engineer\nLocation: Remote\n\nBuild LLM agents."

    result = ApplicationTrackerTool().run({"database_path": str(db_path), "jd_text": jd})

    assert "application_id=1" in result
    assert "status=needs_review" in result
    assert db_path.exists()
