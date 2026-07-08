"""PEAS-designed job application agent built on HelloAgents."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hello_agents.agents.plan_solve_agent import PlanAndSolveAgent
from hello_agents.core.config import Config
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.tools.builtin.career import (
    ApplicationTrackerTool,
    FitScorerTool,
    ManualJDImportTool,
    ResumeIndexerTool,
    ResumeSelectorTool,
    ReviewPacketTool,
    SubmitGateTool,
)
from hello_agents.tools.registry import ToolRegistry

JOB_APPLICATION_SYSTEM_PROMPT = """
You are a careful personal career operations agent.
Optimize for fit, truthfulness, compliance, traceability, and user control.
Never invent user experience. Never auto-submit ordinary browser applications.
"""


class JobApplicationAgent(PlanAndSolveAgent):
    """Career agent that turns JD text into a safe application review packet."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: Optional[ToolRegistry] = None,
        config: Optional[Config] = None,
        resume_source_dir: Optional[str | Path] = None,
        database_path: Optional[str | Path] = None,
    ):
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=JOB_APPLICATION_SYSTEM_PROMPT,
            config=config,
        )
        self.tool_registry = tool_registry or self._default_registry()
        self.resume_source_dir = Path(resume_source_dir) if resume_source_dir else None
        self.database_path = Path(database_path) if database_path else None

    @staticmethod
    def _default_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register_tool(ApplicationTrackerTool())
        registry.register_tool(ManualJDImportTool())
        registry.register_tool(FitScorerTool())
        registry.register_tool(ResumeIndexerTool())
        registry.register_tool(ResumeSelectorTool())
        registry.register_tool(ReviewPacketTool())
        registry.register_tool(SubmitGateTool())
        return registry

    def run(self, input_text: str, **kwargs) -> str:
        """Run a deterministic MVP career review flow using HelloAgents tools."""
        review = self.tool_registry.execute_tool("review_packet", input_text)
        sections = [review]

        if self.resume_source_dir is not None:
            selected_resume = self.tool_registry.get_tool("resume_selector").run(
                {"source_dir": str(self.resume_source_dir), "jd_text": input_text}
            )
            sections.append(f"## Recommended Resume\n\n{selected_resume}")

        if self.database_path is not None:
            tracking = self.tool_registry.get_tool("application_tracker").run(
                {"database_path": str(self.database_path), "jd_text": input_text}
            )
            sections.append(f"## Tracking\n\n{tracking}")

        submit_gate = self.tool_registry.execute_tool("submit_gate", "")
        sections.append(f"## Submit Gate\n\n{submit_gate}")
        return "\n\n".join(sections) + "\n"
