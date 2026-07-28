"""ReAct reasoning strategy with policy-controlled tool execution."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Iterator, Optional, Tuple

from hello_agents.core.agent import Agent
from hello_agents.core.config import Config
from hello_agents.core.contracts import (
    AgentLoopContext,
    ToolCall,
    ToolEffect,
    ToolResult,
)
from hello_agents.core.conversation_manager import ConversationManager
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.memory import LongTermMemory, ShortTermMemory
from hello_agents.core.perception import StructuredPerception
from hello_agents.core.policy import PolicyGate
from hello_agents.core.runtime import AgentCore
from hello_agents.core.stream import StreamEvent
from hello_agents.tools.registry import ToolRegistry


DEFAULT_REACT_PROMPT = """\
Use bounded reasoning and one action per response.

Available tools:
{tools}

Question:
{question}

History:
{history}

Respond exactly as:
Thought: concise reasoning
Action: tool_name[{{"parameter": "value"}}]

When finished:
Thought: concise reasoning
Action: Finish[final answer]
"""


class ReActAgent(Agent):
    """Iterate Thought -> controlled ToolCall -> Observation."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        tool_registry: Optional[ToolRegistry] = None,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        conversation_manager: Optional[ConversationManager] = None,
        max_steps: int = 5,
        custom_prompt: Optional[str] = None,
        agent_core: Optional[AgentCore] = None,
        execution: Optional[ControlledExecution] = None,
        policy_gate: Optional[PolicyGate] = None,
        short_term_memory: Optional[ShortTermMemory] = None,
        long_term_memory: Optional[LongTermMemory] = None,
        perception: Optional[StructuredPerception] = None,
    ) -> None:
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            conversation_manager=conversation_manager,
        )
        if agent_core is not None:
            self.agent_core = agent_core
            self.execution = agent_core.execution
            self.tool_registry = self.execution.registry
        else:
            self.tool_registry = (
                execution.registry
                if execution is not None
                else (tool_registry or ToolRegistry())
            )
            self.execution = execution or ControlledExecution(
                self.tool_registry,
                policy_gate=policy_gate,
                short_term_memory=short_term_memory,
                long_term_memory=long_term_memory,
                perception=perception,
            )
            self.agent_core = AgentCore(
                self.execution,
                llm=llm,
                conversation_manager=conversation_manager,
            )
        self.conversation_manager = self.agent_core.conversation_manager
        self.max_steps = max_steps
        self.prompt_template = custom_prompt or DEFAULT_REACT_PROMPT
        self.current_history: list[str] = []
        self.last_tool_results: list[ToolResult] = []
        self.last_trace: list[tuple[str, str]] = []

    def add_tool(self, tool: Any) -> None:
        self.tool_registry.register_tool(tool)

    def run(self, input_text: str, **kwargs) -> str:
        conversation_id = kwargs.pop("conversation_id", None)
        tool_context = dict(kwargs.pop("tool_context", {}) or {})
        self.current_history = []
        self.last_tool_results = []
        self.last_trace = []

        final_answer = ""
        for step_number in range(1, self.max_steps + 1):
            response = self.agent_core.reason(
                [
                    {
                        "role": "user",
                        "content": self.prompt_template.format(
                            tools=self.tool_registry.describe_tools(),
                            question=input_text,
                            history=(
                                "\n".join(self.current_history)
                                or "None"
                            ),
                        ),
                    }
                ],
                **kwargs,
            )
            thought, action = self._parse_output(response)
            if thought:
                self.last_trace.append(("thought", thought))
            if not action:
                final_answer = "Unable to parse a valid ReAct action."
                break
            if action.startswith("Finish["):
                final_answer = self._parse_action_input(action)
                self.last_trace.append(("finish", final_answer))
                break

            tool_name, raw_input = self._parse_action(action)
            if not tool_name or raw_input is None:
                observation = "invalid_action_format"
                self.current_history.extend(
                    [f"Action: {action}", f"Observation: {observation}"]
                )
                self.last_trace.append(("observation", observation))
                continue

            tool = self.tool_registry.get_tool(tool_name)
            parameters = self._parameters_for_action(
                tool,
                raw_input,
            )
            call = ToolCall(
                tool_name=tool_name,
                parameters=parameters,
                effect=(
                    tool.effect
                    if tool is not None
                    else ToolEffect.READ
                ),
                purpose=f"ReAct step {step_number}: {thought or action}",
                context=tool_context,
            )
            result = self.execution.execute(call)
            self.last_tool_results.append(result)
            observation = (
                str(result.output)
                if result.ok
                else f"Error: {result.error}"
            )
            self.current_history.extend(
                [f"Action: {action}", f"Observation: {observation}"]
            )
            self.last_trace.extend(
                [
                    ("action", action),
                    ("observation", observation),
                ]
            )
        else:
            final_answer = "Unable to complete the task within the step limit."

        self._save_conversation_messages(
            input_text,
            final_answer,
            conversation_id,
        )
        return final_answer

    @staticmethod
    def observation_reflection(context: AgentLoopContext) -> str:
        """Summarize the last Action/Observation transition for production."""
        observation = context.observation
        if observation.kind not in {"tool_result", "ats_runtime"}:
            return (
                "Received a structured environment observation; no action "
                "outcome has been assumed."
            )
        if bool(observation.payload.get("ok")):
            return (
                "The prior ToolCall returned a successful structured result; "
                "the next action is selected from that observed feedback."
            )
        policy_code = observation.payload.get("policy_code")
        if policy_code and policy_code != "allowed":
            return (
                f"The prior action was denied by Policy Gate ({policy_code}); "
                "no environment change is assumed."
            )
        return (
            "The prior ToolCall failed; its structured error is the current "
            "Observation and must constrain the next action."
        )

    def stream_run(
        self,
        input_text: str,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        yield StreamEvent.status(f"Processing: {input_text}")
        final_answer = self.run(input_text, **kwargs)
        for event_type, content in self.last_trace:
            if event_type == "thought":
                yield StreamEvent.thought(content)
            elif event_type == "action":
                yield StreamEvent.action(content)
            elif event_type == "observation":
                yield StreamEvent.observation(content)
        yield StreamEvent.text(final_answer)
        yield StreamEvent.done(final_answer)

    @staticmethod
    def _parse_output(
        text: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        thought = re.search(
            r"^Thought:\s*(.+)$",
            text,
            re.MULTILINE,
        )
        action = re.search(
            r"^Action:\s*(.+)$",
            text,
            re.MULTILINE,
        )
        return (
            thought.group(1).strip() if thought else None,
            action.group(1).strip() if action else None,
        )

    @staticmethod
    def _parse_action(
        action_text: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        match = re.fullmatch(r"([A-Za-z_]\w*)\[(.*)\]", action_text)
        if match is None:
            return None, None
        return match.group(1), match.group(2)

    @staticmethod
    def _parse_action_input(action_text: str) -> str:
        match = re.fullmatch(r"[A-Za-z_]\w*\[(.*)\]", action_text)
        return match.group(1) if match else ""

    @staticmethod
    def _parameters_for_action(tool: Any, raw_input: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_input)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            return dict(parsed)
        if tool is None:
            return {"input": raw_input}
        parameters = tool.get_parameters()
        if not parameters:
            return {}
        return {parameters[0].name: raw_input}
