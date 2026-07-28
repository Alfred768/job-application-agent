"""Policy gate interfaces and composition."""

from __future__ import annotations

from typing import Protocol, Sequence

from .contracts import PolicyDecision, ToolCall, ToolEffect
from .memory import LongTermMemory, ShortTermMemory


class PolicyGate(Protocol):
    def evaluate(
        self,
        call: ToolCall,
        *,
        short_term_memory: ShortTermMemory,
        long_term_memory: LongTermMemory,
    ) -> PolicyDecision:
        ...


class AllowAllPolicyGate:
    """Compatibility policy for agents without domain safety requirements."""

    def evaluate(
        self,
        call: ToolCall,
        *,
        short_term_memory: ShortTermMemory,
        long_term_memory: LongTermMemory,
    ) -> PolicyDecision:
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason="No domain policy denied the tool call.",
            policy=type(self).__name__,
        )


class ReadOnlyPolicyGate:
    """Allow only observation and read effects."""

    def evaluate(
        self,
        call: ToolCall,
        *,
        short_term_memory: ShortTermMemory,
        long_term_memory: LongTermMemory,
    ) -> PolicyDecision:
        if call.effect not in {ToolEffect.OBSERVE, ToolEffect.READ}:
            return PolicyDecision(
                allowed=False,
                code="read_only_execution",
                reason=(
                    "Concurrent execution only permits observation and "
                    "read effects."
                ),
                policy=type(self).__name__,
            )
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason="The tool effect is safe for read-only execution.",
            policy=type(self).__name__,
        )


class CompositePolicyGate:
    """Require every configured policy gate to allow an action."""

    def __init__(self, policies: Sequence[PolicyGate]) -> None:
        self.policies = tuple(policies)

    def evaluate(
        self,
        call: ToolCall,
        *,
        short_term_memory: ShortTermMemory,
        long_term_memory: LongTermMemory,
    ) -> PolicyDecision:
        for policy in self.policies:
            decision = policy.evaluate(
                call,
                short_term_memory=short_term_memory,
                long_term_memory=long_term_memory,
            )
            if not decision.allowed:
                return decision
        return PolicyDecision(
            allowed=True,
            code="allowed",
            reason="All policy gates allowed the tool call.",
            policy=type(self).__name__,
        )
