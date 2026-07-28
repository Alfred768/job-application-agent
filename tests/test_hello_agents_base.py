from __future__ import annotations

from pathlib import Path

from hello_agents.agents.job_application_agent import JobApplicationAgent
from hello_agents.agents.plan_solve_agent import PlanAndSolveAgent
from hello_agents.agents.react_agent import ReActAgent
from hello_agents.agents.reflection_agent import ReflectionAgent
from hello_agents.agents.simple_agent import SimpleAgent
from hello_agents.core.contracts import ToolCall, ToolEffect
from hello_agents.core.conversation_manager import ConversationManager
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.policy import ReadOnlyPolicyGate
from hello_agents.core.runtime import AgentCore
from hello_agents.tools.async_executor import AsyncTask, AsyncToolExecutor
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.builtin.calculator import CalculatorTool
from hello_agents.tools.builtin.search import SearchTool
from hello_agents.tools.chain import (
    ChainStep,
    ToolChain,
    build_jd_review_chain,
)
from hello_agents.tools.registry import ToolRegistry


class SequenceLLM:
    provider = "fake"

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs or ["ok"])
        self.calls: list[list[dict[str, str]]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if len(self.outputs) == 1:
            return self.outputs[0]
        return self.outputs.pop(0)

    def stream_invoke(self, messages, **kwargs):
        yield self.invoke(messages, **kwargs)


class RecordingTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        effect: ToolEffect = ToolEffect.READ,
    ) -> None:
        super().__init__(name, "Record one input.", effect=effect)
        self.calls: list[dict] = []

    def get_parameters(self):
        return [
            ToolParameter(
                name="input",
                type="string",
                description="Input text.",
            )
        ]

    def run(self, parameters):
        self.calls.append(dict(parameters))
        return f"{self.name}:{parameters['input']}"


def test_simple_agent_uses_managed_conversation():
    manager = ConversationManager()
    conversation = manager.create_conversation("test")
    agent = SimpleAgent(
        name="simple",
        llm=SequenceLLM("hello"),
        system_prompt="Be brief.",
        conversation_manager=manager,
    )

    result = agent.run("hi", conversation_id=conversation.conversation_id)

    assert result == "hello"
    assert [message.role for message in conversation.messages] == [
        "user",
        "assistant",
    ]
    assert agent.get_history(conversation.conversation_id)[-1].content == "hello"


def test_conversation_can_fork_and_round_trip_json(tmp_path: Path):
    manager = ConversationManager()
    conversation = manager.create_conversation("main")
    first = manager.add_message(conversation.conversation_id, "one", "user")
    assert first is not None
    manager.add_message(conversation.conversation_id, "two", "assistant")

    branch = manager.fork_conversation(
        conversation.conversation_id,
        first.message_id,
        "branch",
    )
    assert branch is not None
    assert branch.messages[-1].branch_point is True
    assert branch.metadata["forked_from"] == conversation.conversation_id

    path = tmp_path / "conversations.json"
    manager.save_to_json(path)
    restored = ConversationManager.load_from_json(path)

    restored_branch = restored.get_conversation(branch.conversation_id)
    assert restored_branch is not None
    assert restored_branch.messages[-1].content == "one"


def test_calculator_tool_evaluates_only_safe_arithmetic():
    calculator = CalculatorTool()

    assert calculator.run({"expression": "2 + 3 * 4"}) == "14"
    assert calculator.run({"expression": "(2 + 3) ** 2"}) == "25"
    assert "Error" in calculator.run({"expression": "__import__('os')"})
    assert "Error" in calculator.run({"expression": "2 ** 1000"})


def test_search_tool_parses_public_results_without_network(monkeypatch):
    import hello_agents.tools.builtin.search as search_module

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (
                b'<a class="result__a">First result</a>'
                b'<a class="result__snippet">Useful snippet</a>'
            )

    monkeypatch.setattr(search_module, "urlopen", lambda *args, **kwargs: Response())

    output = SearchTool().run({"query": "agent architecture"})

    assert "First result" in output
    assert "Useful snippet" in output


def test_tool_chain_threads_outputs_through_controlled_execution():
    registry = ToolRegistry()
    registry.register_tool(RecordingTool("first"))
    registry.register_tool(RecordingTool("second"))
    chain = ToolChain(
        "test",
        [
            ChainStep("first", lambda context: {"input": "start"}),
            ChainStep(
                "second",
                lambda context: {"input": context["first"]},
            ),
        ],
        registry,
    )

    result = chain.run()

    assert result.outputs["second"] == "second:first:start"
    assert result.status == "completed"
    assert len(result.tool_results) == 2
    assert all(
        item.policy_decision is not None
        and item.policy_decision.allowed
        for item in result.tool_results
    )


def test_jd_review_chain_factory_uses_structured_results():
    from hello_agents.tools.builtin.career import (
        FitScorerTool,
        JDParserTool,
        ReviewPacketTool,
    )

    registry = ToolRegistry()
    registry.register_tool(JDParserTool())
    registry.register_tool(FitScorerTool())
    registry.register_tool(ReviewPacketTool())
    chain = build_jd_review_chain(
        registry,
        "Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents.",
    )

    result = chain.run()

    assert result.status == "completed"
    assert "# Application Review" in result.outputs["review_packet"]
    assert all(item.ok for item in result.tool_results)


def test_tool_chain_stops_when_policy_denies_write():
    registry = ToolRegistry()
    write_tool = RecordingTool("write", effect=ToolEffect.WRITE)
    after = RecordingTool("after")
    registry.register_tool(write_tool)
    registry.register_tool(after)
    execution = ControlledExecution(
        registry,
        policy_gate=ReadOnlyPolicyGate(),
    )
    chain = ToolChain(
        "safe",
        [
            ChainStep("write", lambda context: {"input": "blocked"}),
            ChainStep("after", lambda context: {"input": "unused"}),
        ],
        registry,
        execution=execution,
    )

    result = chain.run()

    assert result.status == "policy_blocked"
    assert result.tool_results[0].policy_decision.code == "read_only_execution"
    assert write_tool.calls == []
    assert after.calls == []


def test_async_executor_runs_reads_through_policy_gate():
    registry = ToolRegistry()
    registry.register_tool(RecordingTool("a"))
    registry.register_tool(RecordingTool("b"))
    executor = AsyncToolExecutor(registry, max_workers=2)

    results = executor.run_concurrent(
        [
            AsyncTask("a", {"input": "1"}, label="job-a"),
            AsyncTask("b", {"input": "2"}, label="job-b"),
        ]
    )

    assert [result.label for result in results] == ["job-a", "job-b"]
    assert [result.output for result in results] == ["a:1", "b:2"]
    assert all(result.ok for result in results)
    assert all(
        result.tool_result is not None
        and result.tool_result.policy_decision is not None
        for result in results
    )


def test_async_executor_refuses_write_effects():
    registry = ToolRegistry()
    write_tool = RecordingTool("write", effect=ToolEffect.WRITE)
    registry.register_tool(write_tool)

    result = AsyncToolExecutor(registry).run_concurrent(
        [AsyncTask("write", {"input": "blocked"})]
    )[0]

    assert result.ok is False
    assert "read_only_execution" in (result.error or "")
    assert write_tool.calls == []


def test_agent_core_owns_strategies_conversation_and_execution_composition():
    registry = ToolRegistry()
    registry.register_tool(RecordingTool("echo"))
    core = AgentCore(
        ControlledExecution(registry),
        llm=SequenceLLM("core response"),
    )
    core.register_recovery_planner(
        "test",
        lambda status, context: None,
    )
    conversation = core.conversation_manager.create_conversation("core")

    strategy = core.run_strategy(
        "simple",
        "hello",
        run_options={"conversation_id": conversation.conversation_id},
    )
    chain = core.run_chain(
        "core-chain",
        [ChainStep("echo", lambda context: {"input": "chain"})],
    )
    concurrent = core.run_concurrent_reads(
        [AsyncTask("echo", {"input": "read"})],
    )
    capabilities = core.capabilities()

    assert strategy.output == "core response"
    assert strategy.status == "completed"
    assert conversation.messages[-1].content == "core response"
    assert chain.final_output == "echo:chain"
    assert concurrent[0].output == "echo:read"
    assert capabilities.strategies == (
        "simple",
        "plan_and_solve",
        "react",
        "reflection",
    )
    assert capabilities.tools == ("echo",)
    assert capabilities.recovery_planners == ("test",)
    assert capabilities.conversation_enabled is True
    assert capabilities.tool_chain_enabled is True
    assert capabilities.concurrent_read_enabled is True


def test_react_agent_routes_action_through_controlled_execution():
    registry = ToolRegistry()
    tool = RecordingTool("echo")
    registry.register_tool(tool)
    llm = SequenceLLM(
        'Thought: inspect\nAction: echo[{"input": "hello"}]',
        "Thought: done\nAction: Finish[complete]",
    )
    agent = ReActAgent(
        name="react",
        llm=llm,
        tool_registry=registry,
    )

    result = agent.run("Use the tool.")

    assert result == "complete"
    assert tool.calls == [{"input": "hello"}]
    assert agent.last_tool_results[0].ok is True
    assert agent.last_tool_results[0].policy_decision is not None


def test_react_agent_cannot_bypass_write_policy():
    registry = ToolRegistry()
    tool = RecordingTool("write", effect=ToolEffect.WRITE)
    registry.register_tool(tool)
    execution = ControlledExecution(
        registry,
        policy_gate=ReadOnlyPolicyGate(),
    )
    llm = SequenceLLM(
        'Thought: write\nAction: write[{"input": "no"}]',
        "Thought: stop\nAction: Finish[blocked]",
    )
    agent = ReActAgent(
        name="react",
        llm=llm,
        execution=execution,
    )

    assert agent.run("Try a write.") == "blocked"
    assert tool.calls == []
    assert agent.last_tool_results[0].policy_decision.code == "read_only_execution"


def test_plan_and_solve_uses_agent_core_for_bounded_reasoning():
    llm = SequenceLLM(
        '["analyze", "answer"]',
        "analysis complete",
        "final answer",
    )
    agent = PlanAndSolveAgent(name="planner", llm=llm)

    result = agent.run("Solve this.")

    assert result == "final answer"
    assert agent.last_plan == ["analyze", "answer"]
    assert agent.executor.last_results == [
        "analysis complete",
        "final answer",
    ]


def test_plan_and_solve_explicit_tool_plan_uses_policy_gate():
    registry = ToolRegistry()
    tool = RecordingTool("write", effect=ToolEffect.WRITE)
    registry.register_tool(tool)
    core = AgentCore(
        ControlledExecution(
            registry,
            policy_gate=ReadOnlyPolicyGate(),
        ),
        llm=SequenceLLM(),
    )
    agent = PlanAndSolveAgent(
        name="planner",
        llm=SequenceLLM(),
        agent_core=core,
    )

    result = agent.run_tool_plan(
        "Attempt a controlled write.",
        [
            ToolCall(
                tool_name="write",
                parameters={"input": "blocked"},
                effect=ToolEffect.WRITE,
            )
        ],
    )

    assert result.status == "policy_blocked"
    assert tool.calls == []


def test_reflection_agent_records_bounded_trajectory():
    agent = ReflectionAgent(
        name="reflection",
        llm=SequenceLLM(
            "initial answer",
            "No improvement needed",
        ),
    )

    result = agent.run("Answer carefully.")

    assert result == "initial answer"
    assert [record.record_type for record in agent.memory.records] == [
        "execution",
        "reflection",
    ]


def test_job_application_agent_exposes_shared_reasoning_strategies():
    agent = JobApplicationAgent(
        name="career",
        llm=SequenceLLM("ok"),
    )

    simple = agent.create_reasoning_strategy("simple")
    react = agent.create_reasoning_strategy("react")
    planner = agent.create_reasoning_strategy("plan-and-solve")
    reflection = agent.create_reasoning_strategy("reflection")

    assert isinstance(simple, SimpleAgent)
    assert isinstance(react, ReActAgent)
    assert isinstance(planner, PlanAndSolveAgent)
    assert isinstance(reflection, ReflectionAgent)
    assert react.execution is agent.execution
    assert planner.agent_core is agent.agent_core
    assert simple.conversation_manager is agent.conversation_manager
    assert react.conversation_manager is agent.conversation_manager
    assert agent.agent_core.capabilities().recovery_planners == (
        "job_application",
    )
    assert agent.agent_core.capabilities().evaluators == (
        "job_application_round",
    )

    evaluation = agent.evaluate_round(
        "test-round",
        state={"run_id": "test-round", "phase": "prepared"},
        manifest={"counts": {"imported": 1, "prepared": 1}},
        audit={},
    )

    assert evaluation.evaluator == "job_application_round"
    assert evaluation.round_id == "test-round"
    assert evaluation.status == "pending"
