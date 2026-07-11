"""SimpleAgent - single-turn LLM agent with no tool use.

The simplest Agent implementation in the HelloAgents base: it builds a prompt
from the system prompt + user input, calls the LLM once, and returns the
result. It is the baseline against which ReAct / Plan-and-Solve agents are
compared, and a fallback when no tools are needed.
"""

from __future__ import annotations

from typing import Iterator, Optional

from ..core.agent import Agent
from ..core.config import Config
from ..core.llm import HelloAgentsLLM
from ..core.stream import StreamEvent


class SimpleAgent(Agent):
    """Single-turn LLM agent."""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
    ):
        super().__init__(name=name, llm=llm, system_prompt=system_prompt, config=config)

    def run(self, input_text: str, **kwargs) -> str:
        conversation_id = kwargs.pop("conversation_id", None)
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": input_text})

        try:
            response = self.llm.invoke(messages, **kwargs) or ""
        except Exception as exc:  # noqa: BLE001
            response = f"Error: {exc}"

        self._save_conversation_messages(input_text, response, conversation_id)
        return response

    def stream_run(self, input_text: str, **kwargs) -> Iterator[StreamEvent]:
        conversation_id = kwargs.pop("conversation_id", None)
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": input_text})

        full = ""
        for chunk in self.llm.stream_invoke(messages, **kwargs):
            if chunk:
                full += chunk
                yield StreamEvent.text(chunk)
        self._save_conversation_messages(input_text, full, conversation_id)
        yield StreamEvent.done(full)
