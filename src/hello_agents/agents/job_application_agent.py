"""Job application agent composed from the current runtime architecture."""

from __future__ import annotations

from pathlib import Path
import json
from typing import Any, Mapping, Optional

from hello_agents.career.evaluation import JobApplicationRoundEvaluator
from hello_agents.career.models import JobApplicationState
from hello_agents.career.policies import JobApplicationPolicyGate
from hello_agents.career.recovery import JobApplicationRecoveryPlanner
from hello_agents.core.agent import Agent
from hello_agents.core.config import Config
from hello_agents.core.contracts import (
    AgentEvaluationResult,
    AgentLoopContext,
    AgentLoopResult,
    AgentThought,
    Observation,
    Plan,
    ToolCall,
    ToolEffect,
    ToolResult,
)
from hello_agents.core.conversation_manager import ConversationManager
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.memory import (
    LongTermMemory,
    NullLongTermMemory,
    ShortTermMemory,
)
from hello_agents.core.message import Message
from hello_agents.core.perception import StructuredPerception
from hello_agents.core.policy import PolicyGate
from hello_agents.core.runtime import AgentCore
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
    ReviewPacketTool,
    SubmitGateTool,
    SensitiveFieldDetectorTool,
)
from hello_agents.tools.registry import ToolRegistry
from job_agent.forms import build_form_fill_plan, inspect_form_snapshot
from job_agent.jobs import import_job_from_text
from job_agent.memory import SQLiteApplicationMemory
from job_agent.models import ResumeTemplate
from job_agent.scoring import score_fit

JOB_APPLICATION_SYSTEM_PROMPT = """
You are a careful personal career operations agent.
Optimize for fit, truthfulness, compliance, traceability, and user control.
Never invent user experience. Automatically submit only when every required field
has a truthful answer and no blocking review fields remain.
"""


class JobApplicationAgent(Agent):
    """Career agent that turns JD text into a safe application review packet."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: Optional[ToolRegistry] = None,
        config: Optional[Config] = None,
        conversation_manager: Optional[ConversationManager] = None,
        resume_source_dir: Optional[str | Path] = None,
        database_path: Optional[str | Path] = None,
        package_dir: Optional[str | Path] = None,
        form_snapshot_json: Optional[str] = None,
        profile_json: Optional[str] = None,
        submit_complete: bool = False,
        perception: StructuredPerception | None = None,
        short_term_memory: ShortTermMemory | None = None,
        long_term_memory: LongTermMemory | None = None,
        policy_gate: PolicyGate | None = None,
        agent_runtime_id: str | None = None,
    ):
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=JOB_APPLICATION_SYSTEM_PROMPT,
            config=config,
            conversation_manager=conversation_manager,
        )
        self.tool_registry = tool_registry or self._default_registry()
        self.resume_source_dir = Path(resume_source_dir) if resume_source_dir else None
        self.database_path = Path(database_path) if database_path else None
        self.package_dir = Path(package_dir) if package_dir else None
        self.form_snapshot_json = form_snapshot_json
        self.profile_json = profile_json
        self.submit_complete = submit_complete
        self.perception = perception or StructuredPerception()
        self.short_term_memory = short_term_memory or ShortTermMemory()
        self.long_term_memory = long_term_memory or (
            SQLiteApplicationMemory(self.database_path)
            if self.database_path is not None
            else NullLongTermMemory()
        )
        self.policy_gate = policy_gate or JobApplicationPolicyGate()
        self.agent_runtime_id = str(agent_runtime_id or "").strip() or None
        self.execution = ControlledExecution(
            self.tool_registry,
            policy_gate=self.policy_gate,
            short_term_memory=self.short_term_memory,
            long_term_memory=self.long_term_memory,
            perception=self.perception,
        )
        self.agent_core = AgentCore(
            self.execution,
            llm=self.llm,
            conversation_manager=conversation_manager,
        )
        self.recovery_planner = JobApplicationRecoveryPlanner()
        self.agent_core.register_recovery_planner(
            self.recovery_planner.name,
            self.recovery_planner,
        )
        self.round_evaluator = JobApplicationRoundEvaluator()
        self.agent_core.register_evaluator(
            self.round_evaluator.name,
            self.round_evaluator,
        )
        self.conversation_manager = self.agent_core.conversation_manager
        self.last_state: JobApplicationState | None = None
        self.last_loop_result: AgentLoopResult | None = None
        self.loop_results: list[AgentLoopResult] = []
        self._planning_notes: list[str] = []

    @classmethod
    def resume_runtime(
        cls,
        *,
        name: str,
        llm: HelloAgentsLLM,
        initial_observation: Observation,
        agent_runtime_id: str,
        tool_registry: ToolRegistry | None = None,
        conversation_manager: ConversationManager | None = None,
        database_path: str | Path | None = None,
        long_term_memory: LongTermMemory | None = None,
        policy_gate: PolicyGate | None = None,
    ) -> "JobApplicationAgent":
        """Rehydrate one logical application Agent across process boundaries."""
        agent = cls(
            name=name,
            llm=llm,
            tool_registry=tool_registry,
            conversation_manager=conversation_manager,
            database_path=database_path,
            long_term_memory=long_term_memory,
            policy_gate=policy_gate,
            agent_runtime_id=agent_runtime_id,
        )
        agent.short_term_memory.clear()
        agent.short_term_memory.add_observation(initial_observation)
        restored = AgentLoopResult(
            plan=Plan(
                objective="Restore the persisted application Agent handoff.",
                steps=(),
            ),
            rounds=(),
            observations=(initial_observation,),
            status="restored",
        )
        agent.last_loop_result = restored
        return agent

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
        registry.register_tool(ReviewPacketTool())
        registry.register_tool(SubmitGateTool())
        registry.register_tool(SensitiveFieldDetectorTool())
        return registry

    def run(self, input_text: str, **kwargs) -> str:
        """Run one bounded Perception -> Thought -> Action feedback loop."""
        self.short_term_memory.clear()
        self.last_loop_result = None
        self.loop_results = []
        self._planning_notes = []
        resume_source = str(self.resume_source_dir) if self.resume_source_dir else None
        job = import_job_from_text(input_text)
        job_observation = self.perception.observe_job(
            input_text,
            parser=import_job_from_text,
        )
        self.short_term_memory.add_observation(job_observation)
        form_observation = None
        if self.form_snapshot_json is not None:
            form_observation = self.perception.observe_form(
                self.form_snapshot_json
            )
            self.short_term_memory.add_observation(form_observation)

        state = JobApplicationState(
            job=job,
            fit_score=score_fit(job),
            status="review_generated",
        )
        state.memory_hits = [
            dict(record)
            for record in self.long_term_memory.search(
                f"{job.company} {job.title}",
                limit=5,
            )
        ]

        workflow_steps = [
            self._call(
                "jd_parser",
                {"jd_text": input_text},
                purpose="Structure the observed job description.",
                context={"phase": "review"},
            ),
            self._call(
                "fit_scorer",
                {"input": input_text},
                purpose="Score the observed job against approved role tracks.",
                context={"phase": "review"},
            ),
        ]
        if resume_source:
            workflow_steps.append(
                self._call(
                    "resume_selector",
                    {
                        "source_dir": resume_source,
                        "jd_text": input_text,
                    },
                    purpose="Select an existing approved PDF without rewriting it.",
                    context={"phase": "review"},
                )
            )
        workflow_steps.append(
            self._call(
                "review_packet",
                {"input": input_text},
                purpose="Render the auditable application review.",
                context={"phase": "review"},
            )
        )

        if self.database_path is not None:
            workflow_steps.append(
                self._call(
                    "application_tracker",
                    {
                        "database_path": str(self.database_path),
                        "jd_text": input_text,
                    },
                    purpose="Reuse or create the deduplicated application record.",
                    context={"phase": "persistence"},
                )
            )
        if self.package_dir is not None:
            workflow_steps.append(
                self._call(
                    "application_package",
                    {
                        "output_dir": str(self.package_dir),
                        "jd_text": input_text,
                    },
                    purpose="Write review artifacts without modifying a resume.",
                    context={"phase": "persistence"},
                )
            )

        if self.form_snapshot_json is not None and self.profile_json is not None:
            workflow_steps.extend(
                [
                    self._call(
                        "form_inspector",
                        {"form_snapshot_json": self.form_snapshot_json},
                        purpose="Normalize the observed ATS form.",
                        context={"phase": "form"},
                    ),
                    self._call(
                        "sensitive_field_detector",
                        {"form_snapshot_json": self.form_snapshot_json},
                        purpose=(
                            "Identify fields governed by approved sensitive facts."
                        ),
                        context={"phase": "form"},
                    ),
                    self._call(
                        "form_filler",
                        {
                            "form_snapshot_json": self.form_snapshot_json,
                            "profile_json": self.profile_json,
                        },
                        purpose="Map saved facts to fields without submitting.",
                        context={"phase": "form"},
                    ),
                ]
            )

        workflow_steps.append(
            self._call(
                "submit_gate",
                {"input": ""},
                purpose="Explain the non-bypassable final-submit conditions.",
                context={"phase": "submission_policy"},
            )
        )
        plan = self.agent_core.create_plan(
            (
                "Prepare one truthful, deduplicated job application and assess "
                "submission readiness."
            ),
            workflow_steps,
        )
        loop_result = self.agent_core.run_loop(
            plan,
            initial_observation=job_observation,
            thought_builder=self._build_loop_thought,
            memory_query=f"{job.company} {job.title}",
            remember_rounds=self.database_path is not None,
        )
        self.last_loop_result = loop_result
        self.loop_results.append(loop_result)
        results = loop_result.results

        review_packet = self._result_output(results, "review_packet")
        jd_analysis = self._result_output(results, "jd_parser")
        fit_output = self._result_output(results, "fit_scorer")
        sections: list[str] = [review_packet]
        history_messages = self._loop_history_messages(loop_result)
        state.review_packet = review_packet
        state.jd_analysis = jd_analysis
        sections.append(f"## JD Analysis\n\n```json\n{jd_analysis}\n```")
        history_messages.extend(
            [
                Message(
                    jd_analysis,
                    "observation",
                    metadata={
                        "section": "jd_analysis",
                        "observation_id": job_observation.observation_id,
                    },
                ),
                Message(
                    fit_output,
                    "observation",
                    metadata={"section": "fit_score"},
                ),
            ]
        )

        if self.resume_source_dir is not None:
            resume_output = self._result_output(results, "resume_selector")
            state.selected_resume = self._resume_from_tool_result(
                resume_output
            )
            sections.append(f"## Recommended Resume\n\n{resume_output}")
            history_messages.append(
                Message(
                    resume_output,
                    "observation",
                    metadata={"section": "recommended_resume"},
                )
            )

        if self._planning_notes:
            llm_notes = self._planning_notes[0]
            state.llm_review_notes = llm_notes
            sections.append(f"## LLM Review Notes\n\n{llm_notes}")
            history_messages.append(
                Message(
                    llm_notes,
                    "thought",
                    metadata={"section": "llm_review_notes"},
                )
            )

        if self.database_path is not None:
            tracking = self._result_output(results, "application_tracker")
            state.tracking = tracking
            sections.append(f"## Tracking\n\n{tracking}")
            history_messages.append(
                Message(
                    tracking,
                    "tool_result",
                    metadata={"section": "tracking"},
                )
            )

        if self.package_dir is not None:
            package = self._result_output(results, "application_package")
            state.application_package = package
            sections.append(f"## Application Package\n\n{package}")
            history_messages.append(
                Message(
                    package,
                    "tool_result",
                    metadata={"section": "application_package"},
                )
            )

        if self.form_snapshot_json is not None and self.profile_json is not None:
            state.form_fields = self._result_output(
                results,
                "form_inspector",
            )
            state.sensitive_fields = self._result_output(
                results,
                "sensitive_field_detector",
            )
            state.safety_gates.append(state.sensitive_fields)
            try:
                state.form_plan = build_form_fill_plan(
                    inspect_form_snapshot(self.form_snapshot_json),
                    json.loads(self.profile_json),
                )
                state.status = (
                    "runtime_ready"
                    if state.form_plan.can_auto_submit
                    else "manual_review_required"
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                state.status = "form_plan_unavailable"
            form_fill_output = self._result_output(
                results,
                "form_filler",
            )
            sections.append(
                "## Form Fill Plan\n\n"
                f"### Form Fields\n\n```json\n{state.form_fields}\n```\n\n"
                f"### Sensitive Fields\n\n{state.sensitive_fields}\n\n"
                f"### Fill Plan\n\n{form_fill_output}"
            )
            history_messages.extend(
                [
                    Message(
                        state.form_fields,
                        "observation",
                        metadata={
                            "section": "form_fields",
                            "observation_id": (
                                form_observation.observation_id
                                if form_observation is not None
                                else ""
                            ),
                        },
                    ),
                    Message(
                        state.sensitive_fields,
                        "safety_gate",
                        metadata={"section": "sensitive_fields"},
                    ),
                    Message(
                        form_fill_output,
                        "tool_result",
                        metadata={"section": "form_fill_plan"},
                    ),
                ]
            )

        submit_gate = self._result_output(results, "submit_gate")
        state.submit_gate = submit_gate
        state.safety_gates.append(submit_gate)
        sections.append(f"## Submit Gate\n\n{submit_gate}")
        history_messages.append(
            Message(
                submit_gate,
                "safety_gate",
                metadata={"section": "submit_gate"},
            )
        )

        submission_decision = self.policy_gate.evaluate(
            ToolCall(
                tool_name="browser_execute",
                parameters={},
                effect=ToolEffect.SUBMIT,
                purpose="Evaluate readiness without clicking Submit.",
                context=self._submission_context(state),
            ),
            short_term_memory=self.short_term_memory,
            long_term_memory=self.long_term_memory,
        )
        self.short_term_memory.add_policy_decision(submission_decision)
        policy_observation = self.perception.observe(
            "policy_feedback",
            type(self.policy_gate).__name__,
            {
                "allowed": submission_decision.allowed,
                "code": submission_decision.code,
                "reason": submission_decision.reason,
            },
        )
        self.short_term_memory.add_observation(policy_observation)
        state.safety_gates.append(
            f"{submission_decision.code}: {submission_decision.reason}"
        )

        response = "\n\n".join(sections) + "\n"
        state.architecture_status = loop_result.status
        state.observations = list(self.short_term_memory.observations)
        state.thoughts = list(self.short_term_memory.thoughts)
        state.rounds = list(self.short_term_memory.rounds)
        state.memory_updates = list(self.short_term_memory.memory_updates)
        state.tool_results = list(self.short_term_memory.tool_results)
        state.policy_decisions = list(self.short_term_memory.policy_decisions)
        self._remember_run(state)
        self.last_state = state
        self._save_conversation_messages(
            input_text,
            response,
            conversation_id=kwargs.get("conversation_id"),
            extra_messages=history_messages,
        )
        return response

    def continue_with_tools(
        self,
        objective: str,
        steps: list[ToolCall],
        *,
        memory_query: str | None = None,
    ) -> AgentLoopResult:
        """Continue this application with the prior ToolResult observation."""
        if self.last_loop_result is None:
            raise RuntimeError(
                "JobApplicationAgent must run before it can continue."
            )
        initial_observation = self.last_loop_result.observations[-1]
        loop_result = self.agent_core.run_loop(
            self.agent_core.create_plan(objective, steps),
            initial_observation=initial_observation,
            thought_builder=self._build_loop_thought,
            memory_query=memory_query or objective,
            remember_rounds=self.database_path is not None,
        )
        self.last_loop_result = loop_result
        self.loop_results.append(loop_result)
        if self.last_state is not None:
            self.last_state.architecture_status = loop_result.status
            self.last_state.observations = list(
                self.short_term_memory.observations
            )
            self.last_state.thoughts = list(self.short_term_memory.thoughts)
            self.last_state.rounds = list(self.short_term_memory.rounds)
            self.last_state.memory_updates = list(
                self.short_term_memory.memory_updates
            )
            self.last_state.tool_results = list(
                self.short_term_memory.tool_results
            )
            self.last_state.policy_decisions = list(
                self.short_term_memory.policy_decisions
            )
        return loop_result

    def create_reasoning_strategy(
        self,
        strategy: str,
        **kwargs: Any,
    ) -> Agent:
        """Create a strategy that shares this agent's controlled runtime."""
        normalized = strategy.strip().lower().replace("-", "_")
        return self.agent_core.create_reasoning_strategy(
            normalized,
            name=f"{self.name}-{normalized}",
            system_prompt=self.system_prompt,
            config=self.config,
            **kwargs,
        )

    def evaluate_round(
        self,
        round_id: str,
        *,
        state: Mapping[str, Any],
        manifest: Mapping[str, Any],
        audit: Mapping[str, Any],
    ) -> AgentEvaluationResult:
        """Evaluate aggregate round evidence through this Agent Core."""
        return self.agent_core.evaluate_round(
            self.round_evaluator.name,
            {
                "state": dict(state),
                "manifest": dict(manifest),
                "audit": dict(audit),
            },
            round_id=round_id,
        )

    def _build_loop_thought(
        self,
        context: AgentLoopContext,
    ) -> AgentThought:
        composed = self.agent_core.build_thought(context)
        selected_action = composed.selected_action
        reflection = composed.reflection
        self_criticism = composed.self_criticism
        summary = composed.summary

        if self._should_use_llm_planning():
            llm_decision = self._llm_loop_decision(context)
            if llm_decision is not None:
                selected_action, summary, reflection, self_criticism = (
                    llm_decision
                )
                if summary and summary not in self._planning_notes:
                    self._planning_notes.append(summary)

        return AgentThought(
            objective=context.objective,
            observation_id=context.observation.observation_id,
            strategy="plan_and_solve_react_reflection",
            summary=summary,
            plan=composed.plan,
            selected_action=selected_action,
            reflection=reflection,
            self_criticism=self_criticism,
            memory_observation_ids=tuple(
                observation.observation_id
                for observation in context.short_term_observations
            ),
            long_term_memory_hit_count=len(context.long_term_memory_hits),
        )

    def _llm_loop_decision(
        self,
        context: AgentLoopContext,
    ) -> tuple[ToolCall, str, str, str] | None:
        allowed_actions = [
            {
                "call_id": action.call_id,
                "tool_name": action.tool_name,
                "effect": action.effect.value,
                "purpose": action.purpose,
            }
            for action in context.remaining_actions
        ]
        observation_metadata = {
            "kind": context.observation.kind,
            "source": context.observation.source,
            "ok": context.observation.payload.get("ok"),
            "policy_code": context.observation.payload.get("policy_code"),
        }
        recent_results = self._llm_tool_result_summaries(
            context.tool_results
        )
        long_term_summaries = self._llm_long_term_summaries(
            context.long_term_memory_hits
        )
        prompt = (
            "Choose exactly one next action from the bounded plan. Use the "
            "current Observation, prior Tool Results, and memory summaries. Do not "
            "invent facts, claim tool success, or provide hidden chain-of-thought. "
            "Return one JSON object with selected_call_id, decision_summary, "
            "reflection, and self_criticism.\n\n"
            f"Objective: {context.objective}\n"
            "Observation metadata: "
            f"{json.dumps(observation_metadata, sort_keys=True)}\n"
            f"Short-term observations: {len(context.short_term_observations)}\n"
            "Recent Tool Results: "
            f"{json.dumps(recent_results, sort_keys=True)}\n"
            "Long-term memory summaries: "
            f"{json.dumps(long_term_summaries, sort_keys=True)}\n"
            f"Allowed actions: {json.dumps(allowed_actions, sort_keys=True)}"
        )
        try:
            raw = self.agent_core.reason(
                [{"role": "user", "content": prompt}],
                max_tokens=350,
            ).strip()
        except Exception:
            return None
        if not raw:
            return None

        selected_action = context.remaining_actions[0]
        summary = raw[:1200]
        reflection = self._reflection_for_observation(context)
        self_criticism = (
            "The LLM decision is advisory and constrained to the registered "
            "Plan; Policy Gate and observed ToolResult remain authoritative."
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            selected_call_id = str(payload.get("selected_call_id") or "")
            selected_action = next(
                (
                    action
                    for action in context.remaining_actions
                    if action.call_id == selected_call_id
                ),
                context.remaining_actions[0],
            )
            summary = str(
                payload.get("decision_summary")
                or payload.get("summary")
                or summary
            ).strip()[:1200]
            reflection = str(
                payload.get("reflection") or reflection
            ).strip()[:1200]
            self_criticism = str(
                payload.get("self_criticism") or self_criticism
            ).strip()[:1200]
        return selected_action, summary, reflection, self_criticism

    @staticmethod
    def _llm_tool_result_summaries(
        results: tuple[ToolResult, ...],
    ) -> list[dict[str, Any]]:
        summaries = []
        for result in results[-5:]:
            error_type = (
                str(result.error).split(":", 1)[0]
                if result.error
                else None
            )
            summaries.append(
                {
                    "tool_name": result.tool_name,
                    "effect": result.effect.value,
                    "ok": result.ok,
                    "error_type": error_type,
                    "policy_code": (
                        result.policy_decision.code
                        if result.policy_decision is not None
                        else None
                    ),
                }
            )
        return summaries

    @staticmethod
    def _llm_long_term_summaries(
        records: tuple[Mapping[str, Any], ...],
    ) -> list[dict[str, Any]]:
        allowed_keys = {
            "namespace",
            "company",
            "title",
            "status",
            "architecture_status",
            "tool_name",
            "effect",
            "ok",
            "policy_code",
            "submitted_at",
        }
        return [
            {
                str(key): value
                for key, value in record.items()
                if str(key) in allowed_keys
                and isinstance(value, (str, int, float, bool, type(None)))
            }
            for record in records[-5:]
        ]

    @staticmethod
    def _reflection_for_observation(context: AgentLoopContext) -> str:
        observation = context.observation
        if observation.kind != "tool_result":
            return (
                "The workflow received a structured environment observation "
                "and has not assumed any action outcome."
            )
        if bool(observation.payload.get("ok")):
            return (
                "The previous ToolCall succeeded according to its structured "
                "ToolResult; the next action is reconsidered from that feedback."
            )
        policy_code = observation.payload.get("policy_code")
        if policy_code and policy_code != "allowed":
            return (
                f"The previous action was denied by Policy Gate ({policy_code}); "
                "no environment change is assumed."
            )
        return (
            "The previous ToolCall failed; its error is the new Observation and "
            "the remaining plan must not treat the action as completed work."
        )

    @staticmethod
    def _loop_history_messages(
        loop_result: AgentLoopResult,
    ) -> list[Message]:
        messages: list[Message] = []
        for round_ in loop_result.rounds:
            metadata = {
                "section": "agent_loop",
                "round_id": round_.round_id,
                "round_index": round_.index,
            }
            messages.extend(
                [
                    Message(
                        round_.thought.summary,
                        "thought",
                        metadata={
                            **metadata,
                            "observation_id": (
                                round_.input_observation.observation_id
                            ),
                            "strategy": round_.thought.strategy,
                        },
                    ),
                    Message(
                        (
                            f"{round_.action.tool_name} "
                            f"({round_.action.effect.value})"
                        ),
                        "action",
                        metadata={
                            **metadata,
                            "call_id": round_.action.call_id,
                            "purpose": round_.action.purpose,
                        },
                    ),
                    Message(
                        (
                            f"{round_.new_observation.source}: "
                            f"ok={round_.tool_result.ok}; "
                            f"status={round_.status}"
                        ),
                        "observation",
                        metadata={
                            **metadata,
                            "observation_id": (
                                round_.new_observation.observation_id
                            ),
                            "call_id": round_.tool_result.call_id,
                        },
                    ),
                    Message(
                        round_.memory_update.summary,
                        "memory_update",
                        metadata={
                            **metadata,
                            "observation_id": (
                                round_.memory_update.observation_id
                            ),
                            "long_term_updated": (
                                round_.memory_update.long_term_updated
                            ),
                        },
                    ),
                ]
            )
        return messages

    def _call(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        *,
        purpose: str,
        context: dict[str, Any] | None = None,
    ) -> ToolCall:
        tool = self.tool_registry.get_tool(tool_name)
        effect = tool.effect if tool is not None else ToolEffect.READ
        return ToolCall(
            tool_name=tool_name,
            parameters=parameters,
            effect=effect,
            purpose=purpose,
            context=dict(context or {}),
        )

    @staticmethod
    def _result_output(
        results: tuple[ToolResult, ...],
        tool_name: str,
    ) -> str:
        for result in results:
            if result.tool_name != tool_name:
                continue
            if result.ok:
                return str(result.output or "")
            return f"Error: {tool_name} failed: {result.error}"
        return f"Error: {tool_name} was not executed"

    def _submission_context(self, state: JobApplicationState) -> dict[str, Any]:
        form_available = (
            self.form_snapshot_json is not None
            and self.profile_json is not None
            and state.status != "form_plan_unavailable"
        )
        unapproved_sensitive = [
            field.label
            for field in state.form_plan.fields
            if field.sensitive and not field.approved
        ]
        selected_resume = state.selected_resume
        return {
            "submit_complete": self.submit_complete and form_available,
            "facts_verified": form_available,
            "blocking_review_items": state.form_plan.review_required_fields,
            "unapproved_sensitive_fields": unapproved_sensitive,
            "resume_verified": bool(
                selected_resume is not None and selected_resume.upload_path
            ),
            "confirmation_required": True,
        }

    def _remember_run(self, state: JobApplicationState) -> None:
        if self.database_path is None or state.job is None:
            return
        try:
            self.long_term_memory.remember(
                "agent_run",
                {
                    "company": state.job.company,
                    "title": state.job.title,
                    "status": state.status,
                    "architecture_status": state.architecture_status,
                    "tool_count": len(state.tool_results),
                },
            )
        except (OSError, ValueError):
            return

    def get_last_state(self) -> JobApplicationState | None:
        return self.last_state

    @staticmethod
    def _resume_from_tool_result(output: str) -> ResumeTemplate | None:
        values = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip()
        selected_pdf = values.get("selected_pdf")
        if not selected_pdf or selected_pdf == "None":
            return None
        return ResumeTemplate(
            track=values.get("selected_track") or "General Software Engineer",
            pdf_path=Path(selected_pdf),
        )

    def _should_use_llm_planning(self) -> bool:
        return getattr(self.llm, "provider", "deterministic") != "deterministic"
