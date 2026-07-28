"""Lifecycle and optional JSON persistence for conversations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .conversation import Conversation
from .message import Message, MessageRole


class ConversationManager:
    """Manage bounded conversations and branches."""

    def __init__(
        self,
        max_conversations: int = 50,
        max_messages_per_conversation: int = 100,
    ) -> None:
        if max_conversations < 1 or max_messages_per_conversation < 1:
            raise ValueError("Conversation limits must be positive.")
        self.conversations: Dict[str, Conversation] = {}
        self.active_conversation_id: Optional[str] = None
        self.max_conversations = max_conversations
        self.max_messages_per_conversation = max_messages_per_conversation

    def create_conversation(
        self,
        name: str = "",
        system_prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        conversation = Conversation(
            name=name,
            system_prompt=system_prompt,
            metadata=metadata,
        )
        self.conversations[conversation.conversation_id] = conversation
        self.active_conversation_id = conversation.conversation_id
        if len(self.conversations) > self.max_conversations:
            oldest = min(
                self.conversations.values(),
                key=lambda item: item.updated_at,
            )
            del self.conversations[oldest.conversation_id]
        return conversation

    def delete_conversation(self, conversation_id: str) -> bool:
        if conversation_id not in self.conversations:
            return False
        del self.conversations[conversation_id]
        if self.active_conversation_id == conversation_id:
            self.active_conversation_id = next(
                iter(self.conversations),
                None,
            )
        return True

    def get_conversation(
        self,
        conversation_id: str,
    ) -> Optional[Conversation]:
        return self.conversations.get(conversation_id)

    def list_conversations(self) -> List[Conversation]:
        return sorted(
            self.conversations.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )

    def set_active(self, conversation_id: str) -> bool:
        if conversation_id not in self.conversations:
            return False
        self.active_conversation_id = conversation_id
        return True

    def get_active(self) -> Optional[Conversation]:
        if self.active_conversation_id is None:
            return None
        return self.conversations.get(self.active_conversation_id)

    def fork_conversation(
        self,
        conversation_id: str,
        at_message_id: str,
        new_name: str = "",
    ) -> Optional[Conversation]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return None
        branch = conversation.fork(at_message_id, new_name)
        self.conversations[branch.conversation_id] = branch
        self.active_conversation_id = branch.conversation_id
        return branch

    def add_message(
        self,
        conversation_id: str,
        content: str,
        role: MessageRole,
        **kwargs: Any,
    ) -> Optional[Message]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return None
        message = conversation.add_message(
            Message(
                content=content,
                role=role,
                conversation_id=conversation_id,
                **kwargs,
            )
        )
        excess = (
            len(conversation.messages)
            - self.max_messages_per_conversation
        )
        if excess > 0:
            conversation.messages = conversation.messages[excess:]
            if conversation.messages:
                conversation.messages[0].parent_id = None
        return message

    def delete_message(
        self,
        conversation_id: str,
        message_id: str,
    ) -> bool:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return False
        remaining = [
            message
            for message in conversation.messages
            if message.message_id != message_id
        ]
        if len(remaining) == len(conversation.messages):
            return False
        conversation.messages = remaining
        self._relink(conversation)
        return True

    def edit_message(
        self,
        conversation_id: str,
        message_id: str,
        new_content: str,
    ) -> bool:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return False
        message = conversation.get_message_by_id(message_id)
        if message is None:
            return False
        message.content = new_content
        return True

    def save_to_json(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(
                {
                    "active_conversation_id": self.active_conversation_id,
                    "conversations": [
                        conversation.to_dict()
                        for conversation in self.conversations.values()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load_from_json(cls, path: str | Path) -> "ConversationManager":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        manager = cls()
        for raw in data.get("conversations", []):
            conversation = Conversation.from_dict(raw)
            manager.conversations[conversation.conversation_id] = conversation
        manager.active_conversation_id = data.get("active_conversation_id")
        return manager

    def clear_all(self) -> None:
        self.conversations.clear()
        self.active_conversation_id = None

    @staticmethod
    def _relink(conversation: Conversation) -> None:
        previous_id: Optional[str] = None
        for message in conversation.messages:
            message.parent_id = previous_id
            previous_id = message.message_id
