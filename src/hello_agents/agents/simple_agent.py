"""Single-turn reasoning strategy with no tool effects."""

from __future__ import annotations

from typing import Iterator, Optional

from hello_agents.core.agent import Agent
from hello_agents.core.config import Config
from hello_agents.core.conversation_manager import ConversationManager
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.llm import HelloAgentsLLM
from hello_agents.core.contracts import AgentLoopContext, ToolCall
from hello_agents.core.runtime import AgentCore
from hello_agents.core.stream import StreamEvent
from hello_agents.tools.registry import ToolRegistry


class SimpleAgent(Agent):
    """Call the LLM once through Agent Core."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        conversation_manager: Optional[ConversationManager] = None,
        agent_core: Optional[AgentCore] = None,
    ) -> None:
        super().__init__(
            name=name,
            llm=llm,
            system_prompt=system_prompt,
            config=config,
            conversation_manager=conversation_manager,
        )
        self.agent_core = agent_core or AgentCore(
            ControlledExecution(ToolRegistry()),
            llm=llm,
            conversation_manager=conversation_manager,
        )
        self.conversation_manager = self.agent_core.conversation_manager

    def run(self, input_text: str, **kwargs) -> str:
        conversation_id = kwargs.pop("conversation_id", None)
        messages = self._messages(input_text, conversation_id)
        try:
            response = self.agent_core.reason(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001
            response = f"Error: {type(exc).__name__}: {exc}"
        self._save_conversation_messages(
            input_text,
            response,
            conversation_id,
        )
        return response

    @staticmethod
    def select_bounded_action(context: AgentLoopContext) -> ToolCall:
        """Deterministic fallback used by the production Agent Core."""
        if not context.remaining_actions:
            raise ValueError("Simple strategy requires a remaining action.")
        return context.remaining_actions[0]

    def stream_run(
        self,
        input_text: str,
        **kwargs,
    ) -> Iterator[StreamEvent]:
        conversation_id = kwargs.pop("conversation_id", None)
        messages = self._messages(input_text, conversation_id)
        full_response = ""
        try:
            for chunk in self.agent_core.stream_reason(messages, **kwargs):
                if chunk:
                    full_response += chunk
                    yield StreamEvent.text(chunk)
        except Exception as exc:  # noqa: BLE001
            full_response = f"Error: {type(exc).__name__}: {exc}"
            yield StreamEvent.error(full_response)
        self._save_conversation_messages(
            input_text,
            full_response,
            conversation_id,
        )
        yield StreamEvent.done(full_response)

    def _messages(
        self,
        input_text: str,
        conversation_id: Optional[str],
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        if self.system_prompt:
            messages.append(
                {"role": "system", "content": self.system_prompt}
            )
        for message in self._resolve_history(conversation_id):
            if message.role in {"user", "assistant", "system"}:
                messages.append(
                    {"role": message.role, "content": message.content}
                )
        messages.append({"role": "user", "content": input_text})
        return messages
