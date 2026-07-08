"""PEAS-designed job application agent built on HelloAgents."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hello_agents.agents.plan_solve_agent import PlanAndSolveAgent
from hello_agents.core.config import Config
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.tools.builtin.career import (
    ApplicationPackageTool,
    ApplicationTrackerTool,
    FitScorerTool,
    FormFillerTool,
    FormInspectorTool,
    JDParserTool,
    ManualJDImportTool,
    ResumeIndexerTool,
    ResumeSelectorTool,
    ResumeTailorTool,
    ReviewPacketTool,
    SubmitGateTool,
    SensitiveFieldDetectorTool,
    TruthfulnessCheckTool,
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
        package_dir: Optional[str | Path] = None,
        form_snapshot_json: Optional[str] = None,
        profile_json: Optional[str] = None,
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
        self.package_dir = Path(package_dir) if package_dir else None
        self.form_snapshot_json = form_snapshot_json
        self.profile_json = profile_json

    @staticmethod
    def _default_registry() -> ToolRegistry:
        registry = ToolRegistry()
        registry.register_tool(ApplicationPackageTool())
        registry.register_tool(ApplicationTrackerTool())
        registry.register_tool(ManualJDImportTool())
        registry.register_tool(FitScorerTool())
        registry.register_tool(FormInspectorTool())
        registry.register_tool(FormFillerTool())
        registry.register_tool(JDParserTool())
        registry.register_tool(ResumeIndexerTool())
        registry.register_tool(ResumeSelectorTool())
        registry.register_tool(ResumeTailorTool())
        registry.register_tool(ReviewPacketTool())
        registry.register_tool(SubmitGateTool())
        registry.register_tool(SensitiveFieldDetectorTool())
        registry.register_tool(TruthfulnessCheckTool())
        return registry

    def run(self, input_text: str, **kwargs) -> str:
        """Run a deterministic MVP career review flow using HelloAgents tools."""
        review = self.tool_registry.execute_tool("review_packet", input_text)
        sections = [review]

        jd_analysis = self.tool_registry.get_tool("jd_parser").run({"jd_text": input_text})
        sections.append(f"## JD Analysis\n\n```json\n{jd_analysis}\n```")

        if self.resume_source_dir is not None:
            selected_resume = self.tool_registry.get_tool("resume_selector").run(
                {"source_dir": str(self.resume_source_dir), "jd_text": input_text}
            )
            sections.append(f"## Recommended Resume\n\n{selected_resume}")

        edit_plan = self.tool_registry.get_tool("resume_tailor").run({"jd_text": input_text})
        sections.append(f"## Resume Edit Plan\n\n```json\n{edit_plan}\n```")

        truthfulness = self.tool_registry.get_tool("truthfulness_check").run(
            {"plan_json": edit_plan}
        )
        sections.append(f"## Truthfulness Gate\n\n{truthfulness}")

        if self.database_path is not None:
            tracking = self.tool_registry.get_tool("application_tracker").run(
                {"database_path": str(self.database_path), "jd_text": input_text}
            )
            sections.append(f"## Tracking\n\n{tracking}")

        if self.package_dir is not None:
            package = self.tool_registry.get_tool("application_package").run(
                {"output_dir": str(self.package_dir), "jd_text": input_text}
            )
            sections.append(f"## Application Package\n\n{package}")

        if self.form_snapshot_json is not None and self.profile_json is not None:
            inspected = self.tool_registry.get_tool("form_inspector").run(
                {"form_snapshot_json": self.form_snapshot_json}
            )
            sensitive = self.tool_registry.get_tool("sensitive_field_detector").run(
                {"form_snapshot_json": self.form_snapshot_json}
            )
            form_plan = self.tool_registry.get_tool("form_filler").run(
                {
                    "form_snapshot_json": self.form_snapshot_json,
                    "profile_json": self.profile_json,
                }
            )
            sections.append(
                "## Form Fill Plan\n\n"
                f"### Form Fields\n\n```json\n{inspected}\n```\n\n"
                f"### Sensitive Fields\n\n{sensitive}\n\n"
                f"### Fill Plan\n\n{form_plan}"
            )

        submit_gate = self.tool_registry.execute_tool("submit_gate", "")
        sections.append(f"## Submit Gate\n\n{submit_gate}")
        return "\n\n".join(sections) + "\n"
