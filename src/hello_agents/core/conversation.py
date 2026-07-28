"""One branchable conversation and its ordered messages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .message import Message


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Conversation:
    """Maintain one root-to-leaf message history."""

    def __init__(
        self,
        conversation_id: Optional[str] = None,
        name: str = "",
        system_prompt: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.conversation_id = conversation_id or uuid4().hex[:12]
        self.name = name
        self.system_prompt = system_prompt
        self.created_at = _now()
        self.updated_at = self.created_at
        self.messages: List[Message] = []
        self.metadata = dict(metadata or {})

    def add_message(self, message: Message) -> Message:
        message.conversation_id = self.conversation_id
        message.parent_id = (
            self.messages[-1].message_id if self.messages else None
        )
        self.messages.append(message)
        self.updated_at = _now()
        return message

    def get_messages(
        self,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[Message]:
        return self.messages[start:end]

    def get_last_message(self) -> Optional[Message]:
        return self.messages[-1] if self.messages else None

    def get_message_by_id(self, message_id: str) -> Optional[Message]:
        return next(
            (
                message
                for message in self.messages
                if message.message_id == message_id
            ),
            None,
        )

    def fork(self, at_message_id: str, new_name: str = "") -> "Conversation":
        target_index = next(
            (
                index
                for index, message in enumerate(self.messages)
                if message.message_id == at_message_id
            ),
            None,
        )
        if target_index is None:
            raise ValueError(f"Message '{at_message_id}' does not exist.")

        branch = Conversation(
            name=new_name or f"{self.name} (branch)",
            system_prompt=self.system_prompt,
            metadata={
                **self.metadata,
                "forked_from": self.conversation_id,
            },
        )
        for index, message in enumerate(self.messages[: target_index + 1]):
            copied = message.model_copy(deep=True)
            copied.message_id = uuid4().hex[:12]
            copied.branch_point = index == target_index
            branch.add_message(copied)
        return branch

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "name": self.name,
            "system_prompt": self.system_prompt,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "messages": [
                message.to_dict(full=True)
                for message in self.messages
            ],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Conversation":
        conversation = cls(
            conversation_id=data["conversation_id"],
            name=data.get("name", ""),
            system_prompt=data.get("system_prompt"),
            metadata=data.get("metadata", {}),
        )
        conversation.created_at = datetime.fromisoformat(data["created_at"])
        conversation.updated_at = datetime.fromisoformat(data["updated_at"])
        for raw in data.get("messages", []):
            conversation.messages.append(
                Message(
                    content=raw["content"],
                    role=raw["role"],
                    message_id=raw.get("message_id"),
                    conversation_id=raw.get(
                        "conversation_id",
                        conversation.conversation_id,
                    ),
                    parent_id=raw.get("parent_id"),
                    branch_point=raw.get("branch_point", False),
                    timestamp=(
                        datetime.fromisoformat(raw["timestamp"])
                        if raw.get("timestamp")
                        else None
                    ),
                    metadata=raw.get("metadata", {}),
                )
            )
        return conversation

    def to_llm_messages(self) -> List[Dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in self.messages
        ]

    def __len__(self) -> int:
        return len(self.messages)
