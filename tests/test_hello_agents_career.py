from hello_agents import ToolRegistry
from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.career.models import JobApplicationState
from hello_agents.tools.builtin.career import (
    FitScorerTool,
    ManualJDImportTool,
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
    registry.register_tool(FitScorerTool())
    registry.register_tool(ReviewPacketTool())
    registry.register_tool(SubmitGateTool())

    assert {
        "manual_jd_import",
        "fit_scorer",
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


def test_job_application_state_starts_with_manual_submit_gate():
    state = JobApplicationState()

    assert state.status == "new"
    assert state.form_plan.can_auto_submit is False
