"""Agent Core: bounded planning and policy-controlled tool feedback."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from .config import Config
from .contracts import (
    AgentCoreCapabilities,
    AgentEvaluationRequest,
    AgentEvaluationResult,
    AgentLoopContext,
    AgentLoopResult,
    AgentRound,
    AgentRunResult,
    AgentThought,
    MemoryUpdate,
    Observation,
    Plan,
    RecoveryExecutionResult,
    RecoveryPlan,
    StrategyRunResult,
    ToolCall,
    ToolEffect,
    ToolResult,
)
from .conversation_manager import ConversationManager
from .execution import ControlledExecution


RecoveryPlanner = Callable[[str, Mapping[str, Any]], Optional[RecoveryPlan]]
RecoveryExecutor = Callable[
    [RecoveryPlan, Mapping[str, Any], ControlledExecution],
    RecoveryExecutionResult,
]
AgentEvaluator = Callable[[AgentEvaluationRequest], AgentEvaluationResult]
ThoughtBuilder = Callable[[AgentLoopContext], AgentThought]


class AgentCore:
    """Own reasoning strategies, conversation state, and controlled execution."""

    _STRATEGIES = (
        "simple",
        "plan_and_solve",
        "react",
        "reflection",
    )

    def __init__(
        self,
        execution: ControlledExecution,
        *,
        llm: Any = None,
        conversation_manager: ConversationManager | None = None,
        evaluation_history_limit: int = 100,
    ) -> None:
        if evaluation_history_limit < 1:
            raise ValueError("evaluation_history_limit must be positive.")
        self.execution = execution
        self.llm = llm
        self.conversation_manager = (
            conversation_manager or ConversationManager()
        )
        self._recovery_planners: dict[str, RecoveryPlanner] = {}
        self._recovery_executors: dict[str, RecoveryExecutor] = {}
        self._evaluators: dict[str, AgentEvaluator] = {}
        self._evaluation_history: list[AgentEvaluationResult] = []
        self._evaluation_history_limit = evaluation_history_limit

    def create_plan(
        self,
        objective: str,
        steps: Iterable[ToolCall],
    ) -> Plan:
        return Plan(objective=objective, steps=tuple(steps))

    def reason(
        self,
        messages: Sequence[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        """Use the configured LLM for reasoning only, never for direct effects."""
        if self.llm is None:
            return ""
        return self.llm.invoke(list(messages), **kwargs) or ""

    def stream_reason(
        self,
        messages: Sequence[dict[str, str]],
        **kwargs: Any,
    ) -> Iterator[str]:
        """Stream reasoning tokens without granting access to external effects."""
        if self.llm is None:
            return
        yield from self.llm.stream_invoke(list(messages), **kwargs)

    def list_strategies(self) -> tuple[str, ...]:
        return self._STRATEGIES

    def create_reasoning_strategy(
        self,
        strategy: str,
        *,
        name: str = "agent",
        system_prompt: str | None = None,
        config: Config | None = None,
        **kwargs: Any,
    ):
        """Create an outer API backed by this exact Core instance."""
        normalized = strategy.strip().lower().replace("-", "_")
        common = {
            "name": name,
            "llm": self.llm,
            "system_prompt": system_prompt,
            "config": config,
            "conversation_manager": self.conversation_manager,
        }
        if normalized == "simple":
            from hello_agents.agents.simple_agent import SimpleAgent

            return SimpleAgent(
                **common,
                agent_core=self,
                **kwargs,
            )
        if normalized in {"plan_and_solve", "plan_solve"}:
            from hello_agents.agents.plan_solve_agent import PlanAndSolveAgent

            return PlanAndSolveAgent(
                **common,
                agent_core=self,
                **kwargs,
            )
        if normalized == "react":
            from hello_agents.agents.react_agent import ReActAgent

            return ReActAgent(
                **common,
                agent_core=self,
                **kwargs,
            )
        if normalized == "reflection":
            from hello_agents.agents.reflection_agent import ReflectionAgent

            return ReflectionAgent(
                **common,
                agent_core=self,
                **kwargs,
            )
        raise ValueError(f"Unknown reasoning strategy: {strategy}")

    def run_strategy(
        self,
        strategy: str,
        input_text: str,
        *,
        name: str = "agent",
        system_prompt: str | None = None,
        config: Config | None = None,
        strategy_options: Mapping[str, Any] | None = None,
        run_options: Mapping[str, Any] | None = None,
    ) -> StrategyRunResult:
        """Run one registered strategy and normalize its trace."""
        agent = self.create_reasoning_strategy(
            strategy,
            name=name,
            system_prompt=system_prompt,
            config=config,
            **dict(strategy_options or {}),
        )
        output = agent.run(input_text, **dict(run_options or {}))
        normalized = strategy.strip().lower().replace("-", "_")
        plan = tuple(getattr(agent, "last_plan", ()) or ())
        trace: list[Mapping[str, Any]] = []
        for event in getattr(agent, "last_trace", ()) or ():
            if isinstance(event, tuple) and len(event) == 2:
                trace.append({"type": event[0], "content": event[1]})
        memory = getattr(agent, "memory", None)
        for record in getattr(memory, "records", ()) or ():
            trace.append(
                {
                    "type": getattr(record, "record_type", "memory"),
                    "content": getattr(record, "content", str(record)),
                }
            )
        tool_results = tuple(
            getattr(agent, "last_tool_results", ()) or ()
        )
        status = (
            "failed"
            if output.startswith(("Error:", "Unable to"))
            else "completed"
        )
        return StrategyRunResult(
            strategy=normalized,
            output=output,
            status=status,
            plan=plan,
            trace=tuple(trace),
            tool_results=tool_results,
        )

    def run_chain(
        self,
        name: str,
        steps,
        context: Mapping[str, Any] | None = None,
        *,
        stop_on_policy_denial: bool = True,
    ):
        """Run a ToolChain through this Core's execution instance."""
        from hello_agents.tools.chain import ToolChain

        return ToolChain(
            name,
            list(steps),
            self.execution.registry,
            execution=self.execution,
        ).run(
            dict(context or {}),
            stop_on_policy_denial=stop_on_policy_denial,
        )

    def run_concurrent_reads(
        self,
        tasks,
        *,
        max_workers: int = 5,
    ):
        """Run independent read-only ToolCalls through this Core."""
        from hello_agents.tools.async_executor import AsyncToolExecutor

        return AsyncToolExecutor(
            self.execution.registry,
            max_workers=max_workers,
            execution=self.execution,
        ).run_concurrent(list(tasks))

    def run_concurrent_read_loop(
        self,
        plan: Plan,
        *,
        initial_observation: Observation,
        thought_builder: ThoughtBuilder | None = None,
        memory_query: str | None = None,
        remember_rounds: bool = False,
        memory_namespace: str = "agent_run",
        max_workers: int = 5,
    ) -> AgentLoopResult:
        """Run read-only ToolCalls in parallel, then join their Observations.

        Every branch remains an AgentRound grounded in the same parent
        Observation. The explicit join Observation is the only continuation
        point for the next sequential Agent loop.
        """
        if any(
            call.effect not in {ToolEffect.OBSERVE, ToolEffect.READ}
            for call in plan.steps
        ):
            raise ValueError(
                "Concurrent Agent loops allow only OBSERVE or READ ToolCalls."
            )
        memory = self.execution.short_term_memory
        if not any(
            item.observation_id == initial_observation.observation_id
            for item in memory.observations
        ):
            memory.add_observation(initial_observation)
        long_term_hits = self._search_long_term_memory(
            memory_query or plan.objective
        )
        thoughts: list[AgentThought] = []
        for index, action in enumerate(plan.steps, start=1):
            context = AgentLoopContext(
                objective=plan.objective,
                round_index=index,
                observation=initial_observation,
                remaining_actions=(action,),
                short_term_observations=memory.planning_observations(
                    initial_observation
                ),
                tool_results=memory.tool_results,
                long_term_memory_hits=long_term_hits,
            )
            thought = (
                thought_builder(context)
                if thought_builder is not None
                else self._default_thought(context)
            )
            self._validate_thought(context, thought)
            memory.add_thought(thought)
            thoughts.append(thought)

        from hello_agents.tools.async_executor import AsyncToolExecutor

        results = AsyncToolExecutor(
            self.execution.registry,
            max_workers=max_workers,
            execution=self.execution,
        ).run_tool_calls(plan.steps)
        parallel_group_id = uuid4().hex
        rounds: list[AgentRound] = []
        branch_observations: list[Observation] = []
        for index, (thought, action, result) in enumerate(
            zip(thoughts, plan.steps, results),
            start=1,
        ):
            observation = self._tool_result_observation(result)
            branch_observations.append(observation)
            round_id = uuid4().hex
            long_term_updated = False
            long_term_summary = ""
            if remember_rounds:
                long_term_updated, long_term_summary = self._remember_round(
                    memory_namespace,
                    plan,
                    index,
                    result,
                    observation,
                )
            memory_update = MemoryUpdate(
                round_id=round_id,
                tool_call_id=result.call_id,
                observation_id=observation.observation_id,
                short_term_updated=True,
                long_term_updated=long_term_updated,
                long_term_namespace=(
                    memory_namespace if long_term_updated else None
                ),
                summary=(
                    "Stored one parallel read ToolResult and Observation."
                    + (f" {long_term_summary}" if long_term_summary else "")
                ),
            )
            memory.add_memory_update(memory_update)
            round_ = AgentRound(
                index=index,
                input_observation=initial_observation,
                thought=thought,
                action=action,
                policy_decision=result.policy_decision,
                tool_result=result,
                new_observation=observation,
                memory_update=memory_update,
                status=self._action_status(result),
                round_id=round_id,
                parallel_group_id=parallel_group_id,
            )
            memory.add_round(round_)
            rounds.append(round_)

        joined = Observation(
            kind="concurrent_read_join",
            source="agent_core",
            payload={
                "parallel_group_id": parallel_group_id,
                "branch_count": len(rounds),
                "results": [
                    {
                        "call_id": result.call_id,
                        "tool_name": result.tool_name,
                        "ok": result.ok,
                        "policy_code": (
                            result.policy_decision.code
                            if result.policy_decision is not None
                            else None
                        ),
                    }
                    for result in results
                ],
            },
        )
        memory.add_observation(joined)
        return AgentLoopResult(
            plan=plan,
            rounds=tuple(rounds),
            observations=(
                initial_observation,
                *branch_observations,
                joined,
            ),
            status=self._loop_status(rounds, len(plan.steps)),
        )

    def register_recovery_planner(
        self,
        name: str,
        planner: RecoveryPlanner,
    ) -> None:
        self._recovery_planners[name] = planner

    def plan_recovery(
        self,
        status: str,
        context: Mapping[str, Any] | None = None,
        *,
        planner: str | None = None,
    ) -> RecoveryPlan | None:
        """Ask registered domain planners for a bounded recovery plan."""
        if planner is not None:
            selected = self._recovery_planners.get(planner)
            if selected is None:
                raise ValueError(f"Unknown recovery planner: {planner}")
            return selected(status, dict(context or {}))
        for selected in self._recovery_planners.values():
            plan = selected(status, dict(context or {}))
            if plan is not None:
                return plan
        return None

    def register_recovery_executor(
        self,
        name: str,
        executor: RecoveryExecutor,
    ) -> None:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("Recovery executor name must not be empty.")
        self._recovery_executors[normalized] = executor

    def execute_recovery(
        self,
        name: str,
        plan: RecoveryPlan,
        context: Mapping[str, Any] | None = None,
    ) -> RecoveryExecutionResult:
        """Execute a planned recovery through this Core's controlled runtime."""
        selected = self._recovery_executors.get(str(name or "").strip())
        if selected is None:
            raise ValueError(f"Unknown recovery executor: {name}")
        return selected(plan, dict(context or {}), self.execution)

    def register_evaluator(
        self,
        name: str,
        evaluator: AgentEvaluator,
    ) -> None:
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("Evaluator name must not be empty.")
        self._evaluators[normalized] = evaluator

    def evaluate_round(
        self,
        evaluator: str,
        inputs: Mapping[str, Any],
        *,
        round_id: str,
        targets: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentEvaluationResult:
        """Evaluate one immutable round snapshot without executing a Tool."""
        evaluator_name = str(evaluator or "").strip()
        selected = self._evaluators.get(evaluator_name)
        if selected is None:
            raise ValueError(f"Unknown evaluator: {evaluator}")
        normalized_round_id = str(round_id or "").strip()
        if not normalized_round_id:
            raise ValueError("round_id must not be empty.")
        request = AgentEvaluationRequest(
            round_id=normalized_round_id,
            inputs=dict(inputs),
            targets=dict(targets or {}),
            metadata=dict(metadata or {}),
        )
        result = selected(request)
        if not isinstance(result, AgentEvaluationResult):
            raise TypeError(
                f"Evaluator '{evaluator_name}' returned "
                f"{type(result).__name__}, expected AgentEvaluationResult."
            )
        if result.evaluator != evaluator_name:
            raise ValueError(
                "Evaluation result evaluator does not match the registered name."
            )
        if result.round_id != request.round_id:
            raise ValueError(
                "Evaluation result round_id does not match the request."
            )
        if result.evaluation_id != request.evaluation_id:
            raise ValueError(
                "Evaluation result evaluation_id does not match the request."
            )
        self._evaluation_history.append(result)
        if len(self._evaluation_history) > self._evaluation_history_limit:
            del self._evaluation_history[
                : -self._evaluation_history_limit
            ]
        self.execution.short_term_memory.add_observation(
            Observation(
                kind="agent_evaluation",
                source=evaluator_name,
                payload={
                    "evaluation_id": result.evaluation_id,
                    "round_id": result.round_id,
                    "status": result.status,
                    "summary": result.summary,
                    "recommendations": list(result.recommendations),
                },
            )
        )
        return result

    @property
    def evaluation_history(self) -> tuple[AgentEvaluationResult, ...]:
        return tuple(self._evaluation_history)

    def clear_evaluation_history(self) -> None:
        self._evaluation_history.clear()

    def capabilities(self) -> AgentCoreCapabilities:
        return AgentCoreCapabilities(
            strategies=self.list_strategies(),
            tools=tuple(self.execution.registry.list_tools()),
            recovery_planners=tuple(self._recovery_planners),
            evaluators=tuple(self._evaluators),
            conversation_enabled=self.conversation_manager is not None,
            recovery_executors=tuple(self._recovery_executors),
        )

    def run_plan(
        self,
        plan: Plan,
        *,
        stop_on_policy_denial: bool = True,
    ) -> AgentRunResult:
        """Compatibility wrapper over the unified closed-loop runtime."""
        initial_observation = Observation(
            kind="objective",
            source="agent_core",
            payload={
                "objective": plan.objective,
                "plan_id": plan.plan_id,
            },
        )
        loop_result = self.run_loop(
            plan,
            initial_observation=initial_observation,
            stop_on_policy_denial=stop_on_policy_denial,
        )
        return AgentRunResult(
            plan=plan,
            results=loop_result.results,
            observations=loop_result.observations,
            status=loop_result.status,
        )

    def run_loop(
        self,
        plan: Plan,
        *,
        initial_observation: Observation,
        thought_builder: ThoughtBuilder | None = None,
        memory_query: str | None = None,
        remember_rounds: bool = False,
        memory_namespace: str = "agent_run",
        stop_on_policy_denial: bool = True,
        max_rounds: int | None = None,
    ) -> AgentLoopResult:
        """Run one auditable Perception -> Thought -> Action feedback loop."""
        if max_rounds is not None and max_rounds < 1:
            raise ValueError("max_rounds must be positive when provided.")
        memory = self.execution.short_term_memory
        if not any(
            item.observation_id == initial_observation.observation_id
            for item in memory.observations
        ):
            memory.add_observation(initial_observation)

        pending = list(plan.steps)
        current_observation = initial_observation
        loop_observations = [initial_observation]
        rounds: list[AgentRound] = []

        while pending and (
            max_rounds is None or len(rounds) < max_rounds
        ):
            long_term_hits = self._search_long_term_memory(
                memory_query or plan.objective
            )
            context = AgentLoopContext(
                objective=plan.objective,
                round_index=len(rounds) + 1,
                observation=current_observation,
                remaining_actions=tuple(pending),
                short_term_observations=memory.planning_observations(
                    current_observation
                ),
                tool_results=memory.tool_results,
                long_term_memory_hits=long_term_hits,
            )
            thought = (
                thought_builder(context)
                if thought_builder is not None
                else self._default_thought(context)
            )
            action = self._validate_thought(context, thought)
            pending = [
                candidate
                for candidate in pending
                if candidate.call_id != action.call_id
            ]
            memory.add_thought(thought)

            from hello_agents.tools.chain import ToolChain

            chain_result = ToolChain(
                f"agent-round-{len(rounds) + 1}",
                [],
                self.execution.registry,
                execution=self.execution,
            ).run_calls([action], stop_on_policy_denial=stop_on_policy_denial)
            if not chain_result.tool_results:
                raise RuntimeError("ToolChain returned no ToolResult.")
            result = chain_result.tool_results[0]
            new_observation = self._tool_result_observation(result)
            loop_observations.append(new_observation)

            round_id = uuid4().hex
            long_term_updated = False
            long_term_summary = ""
            if remember_rounds:
                long_term_updated, long_term_summary = self._remember_round(
                    memory_namespace,
                    plan,
                    len(rounds) + 1,
                    result,
                    new_observation,
                )
            memory_update = MemoryUpdate(
                round_id=round_id,
                tool_call_id=result.call_id,
                observation_id=new_observation.observation_id,
                short_term_updated=True,
                long_term_updated=long_term_updated,
                long_term_namespace=(
                    memory_namespace if long_term_updated else None
                ),
                summary=(
                    "Stored the ToolResult and resulting Observation in "
                    "short-term memory."
                    + (
                        f" {long_term_summary}"
                        if long_term_summary
                        else ""
                    )
                ),
            )
            memory.add_memory_update(memory_update)

            status = self._action_status(result)
            round_ = AgentRound(
                index=len(rounds) + 1,
                input_observation=current_observation,
                thought=thought,
                action=action,
                policy_decision=result.policy_decision,
                tool_result=result,
                new_observation=new_observation,
                memory_update=memory_update,
                status=status,
                round_id=round_id,
            )
            memory.add_round(round_)
            rounds.append(round_)
            current_observation = new_observation

            if status == "policy_blocked" and stop_on_policy_denial:
                break

        return AgentLoopResult(
            plan=plan,
            rounds=tuple(rounds),
            observations=tuple(loop_observations),
            status=(
                "in_progress"
                if pending
                and rounds
                and all(round_.status == "completed" for round_ in rounds)
                else self._loop_status(rounds, len(plan.steps))
            ),
        )

    def build_thought(self, context: AgentLoopContext) -> AgentThought:
        """Compose production Thought from all registered strategy roles."""
        return self._default_thought(context)

    def _default_thought(self, context: AgentLoopContext) -> AgentThought:
        from hello_agents.agents.plan_solve_agent import PlanAndSolveAgent
        from hello_agents.agents.react_agent import ReActAgent
        from hello_agents.agents.reflection_agent import ReflectionAgent
        from hello_agents.agents.simple_agent import SimpleAgent

        selected_action = SimpleAgent.select_bounded_action(context)
        return AgentThought(
            objective=context.objective,
            observation_id=context.observation.observation_id,
            strategy="plan_and_solve_react_reflection",
            summary=(
                f"Select the next bounded action '{selected_action.tool_name}' "
                f"for: {selected_action.purpose or context.objective}"
            ),
            plan=PlanAndSolveAgent.bounded_plan(context),
            selected_action=selected_action,
            reflection=ReActAgent.observation_reflection(context),
            self_criticism=ReflectionAgent.critique_bounded_action(
                context,
                selected_action,
            ),
            memory_observation_ids=tuple(
                observation.observation_id
                for observation in context.short_term_observations
            ),
            long_term_memory_hit_count=len(context.long_term_memory_hits),
        )

    @staticmethod
    def _validate_thought(
        context: AgentLoopContext,
        thought: AgentThought,
    ) -> ToolCall:
        if thought.observation_id != context.observation.observation_id:
            raise ValueError(
                "Thought must be grounded in the current Observation."
            )
        candidate_ids = {
            candidate.call_id for candidate in context.remaining_actions
        }
        if thought.selected_action.call_id not in candidate_ids:
            raise ValueError(
                "Thought selected an action outside the remaining bounded Plan."
            )
        return thought.selected_action

    def _tool_result_observation(self, result: ToolResult) -> Observation:
        for observation in reversed(
            self.execution.short_term_memory.observations
        ):
            if (
                observation.payload.get("call_id") == result.call_id
            ):
                return observation
        raise RuntimeError(
            "ControlledExecution did not return ToolResult feedback to Perception."
        )

    def _search_long_term_memory(
        self,
        query: str,
    ) -> tuple[Mapping[str, Any], ...]:
        try:
            records = self.execution.long_term_memory.search(query, limit=5)
        except (OSError, ValueError):
            return ()
        return tuple(dict(record) for record in records)

    def _remember_round(
        self,
        namespace: str,
        plan: Plan,
        round_index: int,
        result: ToolResult,
        observation: Observation,
    ) -> tuple[bool, str]:
        record = {
            "event": "closed_loop_round",
            "plan_id": plan.plan_id,
            "round_index": round_index,
            "tool_name": result.tool_name,
            "effect": result.effect.value,
            "ok": result.ok,
            "policy_code": (
                result.policy_decision.code
                if result.policy_decision is not None
                else None
            ),
            "observation_kind": observation.kind,
        }
        try:
            self.execution.long_term_memory.remember(namespace, record)
        except (OSError, ValueError):
            return False, "Long-term memory rejected the sanitized round summary."
        return True, "Stored a sanitized round summary in long-term memory."

    @staticmethod
    def _reflection_summary(observation: Observation) -> str:
        if observation.kind != "tool_result":
            return "Received the initial structured environment observation."
        if bool(observation.payload.get("ok")):
            return (
                "The previous action returned a successful structured result; "
                "only the observed result is treated as fact."
            )
        policy_code = observation.payload.get("policy_code")
        if policy_code and policy_code != "allowed":
            return (
                "The previous action was blocked by Policy Gate "
                f"({policy_code}); no external effect is assumed."
            )
        return (
            "The previous action failed; the error is retained as a new "
            "observation for replanning."
        )

    @staticmethod
    def _action_status(result: ToolResult) -> str:
        if (
            result.policy_decision is not None
            and not result.policy_decision.allowed
        ):
            return "policy_blocked"
        return "completed" if result.ok else "tool_failed"

    @staticmethod
    def _loop_status(rounds: Sequence[AgentRound], step_count: int) -> str:
        if len(rounds) == step_count and all(
            round_.status == "completed" for round_ in rounds
        ):
            return "completed"
        if any(round_.status == "policy_blocked" for round_ in rounds):
            return "policy_blocked"
        return "tool_failed"
