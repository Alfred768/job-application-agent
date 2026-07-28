"""Reflection strategy implemented as bounded Agent Core reasoning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

from hello_agents.core.agent import Agent
from hello_agents.core.config import Config
from hello_agents.core.conversation_manager import ConversationManager
from hello_agents.core.contracts import AgentLoopContext, ToolCall
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.runtime import AgentCore
from hello_agents.core.stream import StreamEvent
from hello_agents.tools.registry import ToolRegistry


DEFAULT_PROMPTS = {
    "initial": (
        "Complete the following task accurately.\n\nTask:\n{task}\n"
    ),
    "reflect": (
        "Review the answer against the task. Identify concrete errors or "
        "improvements. Reply 'No improvement needed' when it is complete."
        "\n\nTask:\n{task}\n\nAnswer:\n{content}\n"
    ),
    "refine": (
        "Improve the answer using the feedback.\n\nTask:\n{task}"
        "\n\nPrevious answer:\n{last_attempt}"
        "\n\nFeedback:\n{feedback}\n"
    ),
}


@dataclass(frozen=True)
class ReflectionRecord:
    record_type: str
    content: str


class ReflectionMemory:
    """Store one bounded execution/reflection trajectory."""

    def __init__(self) -> None:
        self.records: list[ReflectionRecord] = []

    def add_record(self, record_type: str, content: str) -> None:
        self.records.append(ReflectionRecord(record_type, content))

    def get_trajectory(self) -> str:
        return "\n\n".join(
            f"{record.record_type}: {record.content}"
            for record in self.records
        )

    def get_last_execution(self) -> str:
        for record in reversed(self.records):
            if record.record_type == "execution":
                return record.content
        return ""


class ReflectionAgent(Agent):
    """Iteratively critique and refine a reasoning result."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        conversation_manager: Optional[ConversationManager] = None,
        max_iterations: int = 3,
        custom_prompts: Optional[dict[str, str]] = None,
        agent_core: Optional[AgentCore] = None,
    ) -> None:
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            conversation_manager=conversation_manager,
        )
        self.max_iterations = max_iterations
        self.memory = ReflectionMemory()
        self.prompts = {**DEFAULT_PROMPTS, **(custom_prompts or {})}
        self.agent_core = agent_core or AgentCore(
            ControlledExecution(ToolRegistry()),
            llm=llm,
            conversation_manager=conversation_manager,
        )
        self.conversation_manager = self.agent_core.conversation_manager

    def run(self, input_text: str, **kwargs) -> str:
        conversation_id = kwargs.pop("conversation_id", None)
        self.memory = ReflectionMemory()
        initial = self._reason(
            self.prompts["initial"].format(task=input_text),
            **kwargs,
        )
        self.memory.add_record("execution", initial)

        for _ in range(self.max_iterations):
            current = self.memory.get_last_execution()
            feedback = self._reason(
                self.prompts["reflect"].format(
                    task=input_text,
                    content=current,
                ),
                **kwargs,
            )
            self.memory.add_record("reflection", feedback)
            normalized = feedback.strip().lower()
            if (
                "no improvement needed" in normalized
                or "无需改进" in feedback
            ):
                break
            refined = self._reason(
                self.prompts["refine"].format(
                    task=input_text,
                    last_attempt=current,
                    feedback=feedback,
                ),
                **kwargs,
            )
            self.memory.add_record("execution", refined)

        result = self.memory.get_last_execution()
        self._save_conversation_messages(
            input_text,
            result,
            conversation_id,
        )
        return result

    def stream_run(
        self,
        input_text: str,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        yield StreamEvent.status(f"Reflecting on: {input_text}")
        result = self.run(input_text, **kwargs)
        for record in self.memory.records:
            if record.record_type == "reflection":
                yield StreamEvent.thought(record.content)
            else:
                yield StreamEvent.text(record.content)
        yield StreamEvent.done(result)

    def _reason(self, prompt: str, **kwargs) -> str:
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append(
                {"role": "system", "content": self.system_prompt}
            )
        messages.append({"role": "user", "content": prompt})
        return self.agent_core.reason(messages, **kwargs)

    @staticmethod
    def critique_bounded_action(
        context: AgentLoopContext,
        action: ToolCall,
    ) -> str:
        """Return an auditable self-criticism for a selected production action."""
        return (
            f"Selecting '{action.tool_name}' is not evidence of success. "
            "Its declared and registered effects must pass Policy Gate, and "
            "only the resulting Observation may update memory."
        )
