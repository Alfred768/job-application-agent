"""Tests for the completed HelloAgents base framework components."""

from hello_agents.agents.simple_agent import SimpleAgent
from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.chain import (
    ChainStep,
    ToolChain,
    build_jd_review_chain,
    build_resume_preparation_chain,
)
from hello_agents.tools.async_executor import AsyncTask, AsyncToolExecutor
from hello_agents.tools.registry import ToolRegistry
from hello_agents.tools.builtin.calculator import CalculatorTool


class FakeLLM:
    provider = "fake"

    def __init__(self, output="ok"):
        self.output = output
        self.calls = []

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        return self.output

    def stream_invoke(self, messages, **kwargs):
        yield self.output


class EchoTool(Tool):
    def __init__(self, name, delay=0):
        super().__init__(name=name, description="echo")
        self.delay = delay

    def get_parameters(self):
        return [ToolParameter(name="input", type="string", description="text")]

    def run(self, parameters):
        import time

        if self.delay:
            time.sleep(self.delay)
        return f"{self.name}:{parameters.get('input', '')}"


def test_simple_agent_single_turn_llm_call():
    llm = FakeLLM(output="hello world")
    agent = SimpleAgent(name="s", llm=llm, system_prompt="be brief")

    out = agent.run("hi")

    assert out == "hello world"
    # system prompt is sent first
    assert llm.calls[0][0]["role"] == "system"
    assert llm.calls[0][0]["content"] == "be brief"
    assert llm.calls[0][1]["content"] == "hi"


def test_calculator_tool_evaluates_arithmetic():
    calc = CalculatorTool()
    assert calc.run({"expression": "2 + 3 * 4"}) == "14"
    assert calc.run({"expression": "(2 + 3) * 4"}) == "20"
    assert calc.run({"expression": "10 % 3"}) == "1"
    # disallows unsafe characters
    assert "Error" in calc.run({"expression": "__import__('os')"})


def test_tool_chain_threads_outputs_through_context():
    registry = ToolRegistry()
    registry.register_tool(EchoTool("first"))
    registry.register_tool(EchoTool("second"))
    chain = ToolChain(
        "test",
        [
            ChainStep("first", lambda c: {"input": "start"}),
            ChainStep("second", lambda c: {"input": c.get("first", "")}),
        ],
        registry,
    )

    res = chain.run()

    assert res.outputs["first"] == "first:start"
    # second reused first's output as its input, then prefixed its own name
    assert res.outputs["second"] == "second:first:start"
    assert res.final_output == "second:first:start"


def test_jd_review_chain_produces_packet_and_analysis():
    registry = ToolRegistry()
    from hello_agents.tools.builtin.career import (
        FitScorerTool,
        JDParserTool,
        ReviewPacketTool,
    )

    registry.register_tool(JDParserTool())
    registry.register_tool(FitScorerTool())
    registry.register_tool(ReviewPacketTool())

    jd = "Company: Acme\nTitle: Agent Engineer\n\nBuild LLM agents with LangChain and FastAPI."
    chain = build_jd_review_chain(registry, jd)

    res = chain.run()

    assert "review_packet" in res.outputs
    assert "jd_parser" in res.outputs
    assert "fit_scorer" in res.outputs
    assert "# Application Review" in res.outputs["review_packet"]
    assert "Agent Engineer" in res.outputs["jd_parser"]


def test_resume_preparation_chain_threads_tailor_into_truthfulness():
    registry = ToolRegistry()
    from hello_agents.tools.builtin.career import ResumeTailorTool, TruthfulnessCheckTool

    registry.register_tool(ResumeTailorTool())
    registry.register_tool(TruthfulnessCheckTool())

    jd = "Title: Agent Engineer\n\nBuild LangChain agents with FastAPI and Rust."
    chain = build_resume_preparation_chain(registry, jd)

    res = chain.run()

    assert "resume_tailor" in res.outputs
    assert "truthfulness_check" in res.outputs


def test_async_executor_runs_tools_concurrently():
    registry = ToolRegistry()
    registry.register_tool(EchoTool("a", delay=0.05))
    registry.register_tool(EchoTool("b", delay=0.05))

    executor = AsyncToolExecutor(registry, max_workers=2)
    results = executor.run_concurrent(
        [
            AsyncTask(tool_name="a", params={"input": "1"}, label="job-a"),
            AsyncTask(tool_name="b", params={"input": "2"}, label="job-b"),
        ]
    )

    assert len(results) == 2
    labels = {r.label for r in results}
    assert labels == {"job-a", "job-b"}
    assert all(r.ok for r in results)


def test_async_executor_reports_missing_tool_error():
    registry = ToolRegistry()
    executor = AsyncToolExecutor(registry)

    results = executor.run_concurrent([AsyncTask(tool_name="nope", params={})])

    assert len(results) == 1
    assert not results[0].ok
    assert "not registered" in results[0].error
