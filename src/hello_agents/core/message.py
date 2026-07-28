"""Messages shared by local history and managed conversations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel

MessageRole = Literal[
    "user",
    "assistant",
    "system",
    "tool",
    "tool_result",
    "observation",
    "safety_gate",
    "thought",
    "action",
    "memory_update",
]


class Message(BaseModel):
    """消息类"""

    content: str
    role: MessageRole
    message_id: str
    conversation_id: str
    parent_id: Optional[str]
    branch_point: bool
    timestamp: datetime
    metadata: Dict[str, Any]

    def __init__(self, content: str, role: MessageRole, **kwargs):
        super().__init__(
            content=content,
            role=role,
            message_id=kwargs.get("message_id") or uuid4().hex[:12],
            conversation_id=kwargs.get("conversation_id", ""),
            parent_id=kwargs.get("parent_id"),
            branch_point=kwargs.get("branch_point", False),
            timestamp=kwargs.get("timestamp") or datetime.now(timezone.utc),
            metadata=kwargs.get("metadata", {}),
        )

    def to_dict(self, full: bool = False) -> Dict[str, Any]:
        if full:
            return {
                "role": self.role,
                "content": self.content,
                "message_id": self.message_id,
                "conversation_id": self.conversation_id,
                "parent_id": self.parent_id,
                "branch_point": self.branch_point,
                "timestamp": self.timestamp.isoformat(),
                "metadata": self.metadata,
            }
        return {"role": self.role, "content": self.content}

    def __str__(self) -> str:
        return f"[{self.role}] {self.content}"
