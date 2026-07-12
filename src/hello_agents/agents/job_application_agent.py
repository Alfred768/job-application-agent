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
from hello_agents.tools.chain import (
    build_application_form_chain,
    build_jd_review_chain,
    build_resume_preparation_chain,
)
from hello_agents.tools.registry import ToolRegistry
from job_agent.jobs import import_job_from_text
from job_agent.resumes import index_resume_templates
from job_agent.scoring import classify_role

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
        """Run a deterministic MVP career review flow using HelloAgents tools.

        The flow is orchestrated through ToolChains (the HelloAgents tool-chain
        base): a JD-review chain, a resume-preparation chain, and (when form
        data is supplied) an application-form chain. Outputs are assembled into
        the review packet; the submit gate always stays manual.
        """
        resume_source = str(self.resume_source_dir) if self.resume_source_dir else None
        review_chain = build_jd_review_chain(
            self.tool_registry, input_text, resume_source_dir=resume_source
        )
        review_res = review_chain.run()

        sections: list[str] = [review_res.outputs["review_packet"]]
        jd_analysis = review_res.outputs["jd_parser"]
        sections.append(f"## JD Analysis\n\n```json\n{jd_analysis}\n```")

        if self.resume_source_dir is not None:
            sections.append(f"## Recommended Resume\n\n{review_res.outputs['resume_selector']}")

        prep_chain = build_resume_preparation_chain(
            self.tool_registry,
            input_text,
            resume_text=self._selected_resume_evidence(input_text),
        )
        prep_res = prep_chain.run()
        edit_plan = prep_res.outputs["resume_tailor"]
        sections.append(f"## Resume Edit Plan\n\n```json\n{edit_plan}\n```")

        truthfulness = prep_res.outputs["truthfulness_check"]
        sections.append(f"## Truthfulness Gate\n\n{truthfulness}")

        if self._should_use_llm_notes():
            llm_notes = self._generate_llm_review_notes(
                jd_text=input_text,
                jd_analysis=jd_analysis,
                edit_plan=edit_plan,
                truthfulness=truthfulness,
            )
            if llm_notes.strip():
                sections.append(f"## LLM Review Notes\n\n{llm_notes.strip()}")

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
            form_chain = build_application_form_chain(
                self.tool_registry, self.form_snapshot_json, self.profile_json
            )
            form_res = form_chain.run()
            sections.append(
                "## Form Fill Plan\n\n"
                f"### Form Fields\n\n```json\n{form_res.outputs['form_inspector']}\n```\n\n"
                f"### Sensitive Fields\n\n{form_res.outputs['sensitive_field_detector']}\n\n"
                f"### Fill Plan\n\n{form_res.outputs['form_filler']}"
            )

        submit_gate = self.tool_registry.execute_tool("submit_gate", "")
        sections.append(f"## Submit Gate\n\n{submit_gate}")
        return "\n\n".join(sections) + "\n"

    def _selected_resume_evidence(self, jd_text: str) -> str | None:
        """Load only the selected local template as evidence for keyword edits."""
        if self.resume_source_dir is None:
            return None
        job = import_job_from_text(jd_text)
        target_track = classify_role(job)
        selected = next(
            (
                template
                for template in index_resume_templates(self.resume_source_dir)
                if template.track == target_track and template.parsed_text
            ),
            None,
        )
        return selected.parsed_text if selected else None

    def _should_use_llm_notes(self) -> bool:
        return getattr(self.llm, "provider", "deterministic") != "deterministic"

    def _generate_llm_review_notes(
        self,
        jd_text: str,
        jd_analysis: str,
        edit_plan: str,
        truthfulness: str,
    ) -> str:
        prompt = (
            "Review this job application plan for a human applicant. "
            "Give concise advice only. Do not invent experience, do not weaken the "
            "truthfulness gate, and do not suggest automatic final submission.\n\n"
            f"JD:\n{jd_text}\n\n"
            f"JD analysis JSON:\n{jd_analysis}\n\n"
            f"Resume edit plan JSON:\n{edit_plan}\n\n"
            f"Truthfulness gate:\n{truthfulness}\n"
        )
        try:
            return self.llm.invoke([{"role": "user", "content": prompt}], max_tokens=400) or ""
        except Exception:
            # Degrade gracefully: a failing/miss-configured LLM must not crash
            # the whole application preparation flow.
            return ""
