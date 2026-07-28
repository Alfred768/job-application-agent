"""Controlled execution module between policy and tool use."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hello_agents.tools.registry import ToolRegistry

from .contracts import PolicyDecision, ToolCall, ToolEffect, ToolResult
from .memory import LongTermMemory, NullLongTermMemory, ShortTermMemory
from .perception import StructuredPerception
from .policy import AllowAllPolicyGate, PolicyGate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ControlledExecution:
    """Authorize, execute, structure, and remember every tool invocation."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy_gate: PolicyGate | None = None,
        short_term_memory: ShortTermMemory | None = None,
        long_term_memory: LongTermMemory | None = None,
        perception: StructuredPerception | None = None,
    ) -> None:
        self.registry = registry
        self.policy_gate = policy_gate or AllowAllPolicyGate()
        self.short_term_memory = short_term_memory or ShortTermMemory()
        self.long_term_memory = long_term_memory or NullLongTermMemory()
        self.perception = perception or StructuredPerception()

    def execute(self, call: ToolCall) -> ToolResult:
        started_at = _now()
        tool = self.registry.get_tool(call.tool_name)
        if tool is None:
            missing = PolicyDecision(
                allowed=False,
                code="tool_not_registered",
                reason=f"Tool '{call.tool_name}' is not registered.",
                policy=type(self).__name__,
            )
            self.short_term_memory.add_policy_decision(missing)
            return self._finish(
                call,
                ok=False,
                error=missing.reason,
                decision=missing,
                started_at=started_at,
            )
        effective_call = replace(
            call,
            effect=self._stronger_effect(
                call.effect,
                tool.effective_effect(dict(call.parameters)),
            ),
        )
        decision = self.policy_gate.evaluate(
            effective_call,
            short_term_memory=self.short_term_memory,
            long_term_memory=self.long_term_memory,
        )
        self.short_term_memory.add_policy_decision(decision)
        if not decision.allowed:
            return self._finish(
                effective_call,
                ok=False,
                error=f"policy_denied:{decision.code}:{decision.reason}",
                decision=decision,
                started_at=started_at,
            )

        parameters = dict(effective_call.parameters)
        if not tool.validate_parameters(parameters):
            return self._finish(
                effective_call,
                ok=False,
                error=f"invalid_parameters:{effective_call.tool_name}",
                decision=decision,
                started_at=started_at,
            )
        try:
            output: Any = tool.run(parameters)
        except Exception as exc:  # noqa: BLE001
            return self._finish(
                effective_call,
                ok=False,
                error=f"{type(exc).__name__}:{exc}",
                decision=decision,
                started_at=started_at,
            )
        return self._finish(
            effective_call,
            ok=True,
            output=output,
            decision=decision,
            started_at=started_at,
        )

    @staticmethod
    def _stronger_effect(
        left: ToolEffect,
        right: ToolEffect,
    ) -> ToolEffect:
        order = {
            "observe": 0,
            "read": 1,
            "write": 2,
            "submit": 3,
            "repair": 4,
        }
        left_effect = ToolEffect(left)
        right_effect = ToolEffect(right)
        return (
            left_effect
            if order[left_effect.value] >= order[right_effect.value]
            else right_effect
        )

    def _finish(
        self,
        call: ToolCall,
        *,
        ok: bool,
        output: Any = None,
        error: str | None = None,
        decision: PolicyDecision,
        started_at: str,
    ) -> ToolResult:
        result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            effect=call.effect,
            ok=ok,
            output=output,
            error=error,
            policy_decision=decision,
            started_at=started_at,
            finished_at=_now(),
        )
        self.short_term_memory.add_tool_result(result)
        observation = self.perception.observe_tool_result(result)
        self.short_term_memory.add_observation(observation)
        return result
