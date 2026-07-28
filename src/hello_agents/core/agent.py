"""Agent基类"""

from abc import ABC, abstractmethod
from typing import Optional, Iterator

from .config import Config
from .conversation_manager import ConversationManager
from .message import Message
from .llm import HelloAgentsLLM
from .stream import StreamEvent


class Agent(ABC):
    """Agent基类"""

    def __init__(
        self,
        name: str,
        llm: HelloAgentsLLM,
        system_prompt: Optional[str] = None,
        config: Optional[Config] = None,
        conversation_manager: Optional[ConversationManager] = None,
    ):
        self.name = name
        self.llm = llm
        self.system_prompt = system_prompt
        self.config = config or Config()
        self.conversation_manager = conversation_manager
        self._history: list[Message] = []

    def _resolve_history(
        self,
        conversation_id: Optional[str] = None,
    ) -> list[Message]:
        if self.conversation_manager is not None and conversation_id:
            conversation = self.conversation_manager.get_conversation(
                conversation_id
            )
            if conversation is not None:
                return list(conversation.messages)
        return self._history

    def _save_conversation_messages(
        self,
        input_text: str,
        response: str,
        conversation_id: Optional[str] = None,
        extra_messages: Optional[list[Message]] = None,
    ) -> None:
        messages = list(extra_messages or [])
        if self.conversation_manager is not None and conversation_id:
            self.conversation_manager.add_message(
                conversation_id,
                input_text,
                "user",
            )
            for message in messages:
                self.conversation_manager.add_message(
                    conversation_id,
                    message.content,
                    message.role,
                    metadata=message.metadata,
                )
            self.conversation_manager.add_message(
                conversation_id,
                response,
                "assistant",
            )
            return
        self._save_history_messages(input_text, response, messages)

    def _save_history_messages(
        self,
        input_text: str,
        response: str,
        extra_messages: Optional[list[Message]] = None,
    ) -> None:
        extra_messages = extra_messages or []
        self.add_message(Message(input_text, "user"))
        for message in extra_messages:
            self.add_message(message)
        self.add_message(Message(response, "assistant"))

    @abstractmethod
    def run(self, input_text: str, **kwargs) -> str:
        pass

    def stream_run(self, input_text: str, **kwargs) -> Iterator[StreamEvent]:
        result = self.run(input_text, **kwargs)
        yield StreamEvent.text(result)
        yield StreamEvent.done(result)

    def add_message(self, message: Message):
        self._history.append(message)

    def clear_history(self):
        self._history.clear()

    def get_history(
        self,
        conversation_id: Optional[str] = None,
    ) -> list[Message]:
        return list(self._resolve_history(conversation_id))

    def __str__(self) -> str:
        return f"Agent(name={self.name}, provider={self.llm.provider})"

    def __repr__(self) -> str:
        return self.__str__()
