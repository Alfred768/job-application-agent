"""Explicit short-term and long-term memory boundaries."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable, Mapping, Protocol

from .contracts import (
    AgentRound,
    AgentThought,
    MemoryUpdate,
    Observation,
    PolicyDecision,
    ToolResult,
)


class LongTermMemory(Protocol):
    """Persistent facts and history available to the Agent Core."""

    def search(self, query: str, *, limit: int = 5) -> list[Mapping[str, Any]]:
        ...

    def remember(self, namespace: str, record: Mapping[str, Any]) -> None:
        ...


class NullLongTermMemory:
    """No-op memory used when no persistent adapter is configured."""

    def search(self, query: str, *, limit: int = 5) -> list[Mapping[str, Any]]:
        return []

    def remember(self, namespace: str, record: Mapping[str, Any]) -> None:
        return None


class InMemoryLongTermMemory:
    """Deterministic long-term memory for tests and local compositions."""

    def __init__(self) -> None:
        self._records: list[tuple[str, dict[str, Any]]] = []

    def search(self, query: str, *, limit: int = 5) -> list[Mapping[str, Any]]:
        tokens = {token for token in query.lower().split() if token}
        matches: list[dict[str, Any]] = []
        for namespace, record in reversed(self._records):
            haystack = f"{namespace} {record}".lower()
            if not tokens or all(token in haystack for token in tokens):
                matches.append({"namespace": namespace, **record})
            if len(matches) >= limit:
                break
        return matches

    def remember(self, namespace: str, record: Mapping[str, Any]) -> None:
        self._records.append((namespace, dict(record)))


class ShortTermMemory:
    """Bounded working memory for the current task and feedback loop."""

    def __init__(self, max_items: int = 256) -> None:
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self._observations: deque[Observation] = deque(maxlen=max_items)
        self._tool_results: deque[ToolResult] = deque(maxlen=max_items)
        self._policy_decisions: deque[PolicyDecision] = deque(maxlen=max_items)
        self._thoughts: deque[AgentThought] = deque(maxlen=max_items)
        self._memory_updates: deque[MemoryUpdate] = deque(maxlen=max_items)
        self._rounds: deque[AgentRound] = deque(maxlen=max_items)

    def add_observation(self, observation: Observation) -> None:
        self._observations.append(observation)

    def add_tool_result(self, result: ToolResult) -> None:
        self._tool_results.append(result)

    def add_policy_decision(self, decision: PolicyDecision) -> None:
        self._policy_decisions.append(decision)

    def add_thought(self, thought: AgentThought) -> None:
        self._thoughts.append(thought)

    def add_memory_update(self, update: MemoryUpdate) -> None:
        self._memory_updates.append(update)

    def add_round(self, round_: AgentRound) -> None:
        self._rounds.append(round_)

    @property
    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._observations)

    @property
    def tool_results(self) -> tuple[ToolResult, ...]:
        return tuple(self._tool_results)

    @property
    def policy_decisions(self) -> tuple[PolicyDecision, ...]:
        return tuple(self._policy_decisions)

    @property
    def thoughts(self) -> tuple[AgentThought, ...]:
        return tuple(self._thoughts)

    @property
    def memory_updates(self) -> tuple[MemoryUpdate, ...]:
        return tuple(self._memory_updates)

    @property
    def rounds(self) -> tuple[AgentRound, ...]:
        return tuple(self._rounds)

    def planning_observations(
        self,
        current_observation: Observation | None = None,
        *,
        history_limit: int = 5,
    ) -> tuple[Observation, ...]:
        """Return the compact STM projection used by Thought.

        The append-only queues remain available for audit.  Planning receives
        the current environment state plus a short action summary, so page
        snapshots and old ToolResult payloads do not accumulate in the LLM
        prompt as a second copy of the world.
        """
        projected: list[Observation] = []
        if current_observation is not None:
            projected.append(current_observation)
        elif self._observations:
            projected.append(self._observations[-1])

        history = [
            self._round_summary(round_)
            for round_ in list(self._rounds)[-max(0, history_limit):]
        ]
        if history:
            latest_round = list(self._rounds)[-1]
            projected.append(
                Observation(
                    kind="memory_summary",
                    source="short_term_memory",
                    payload={
                        "mode": "sliding_window",
                        "history": history,
                        "retained_observation_id": (
                            current_observation.observation_id
                            if current_observation is not None
                            else (
                                self._observations[-1].observation_id
                                if self._observations
                                else ""
                            )
                        ),
                    },
                    observation_id=(
                        f"stm-summary-{latest_round.round_id}"
                    ),
                )
            )
        return tuple(projected)

    def latest_output(self, tool_name: str, default: Any = None) -> Any:
        for result in reversed(self._tool_results):
            if result.tool_name == tool_name and result.ok:
                return result.output
        return default

    def extend_observations(self, observations: Iterable[Observation]) -> None:
        for observation in observations:
            self.add_observation(observation)

    def clear(self) -> None:
        self._observations.clear()
        self._tool_results.clear()
        self._policy_decisions.clear()
        self._thoughts.clear()
        self._memory_updates.clear()
        self._rounds.clear()

    @staticmethod
    def _round_summary(round_: AgentRound) -> dict[str, Any]:
        policy_code = (
            round_.policy_decision.code
            if round_.policy_decision is not None
            else None
        )
        if policy_code and policy_code != "allowed":
            summary = (
                f"{round_.action.tool_name} blocked by Policy Gate "
                f"({policy_code})."
            )
        elif round_.tool_result.ok:
            summary = f"{round_.action.tool_name} completed."
        else:
            error_type = (
                str(round_.tool_result.error).split(":", 1)[0]
                if round_.tool_result.error
                else "tool_error"
            )
            summary = f"{round_.action.tool_name} failed ({error_type})."
        return {
            "round_id": round_.round_id,
            "tool_name": round_.action.tool_name,
            "effect": round_.action.effect.value,
            "status": round_.status,
            "ok": round_.tool_result.ok,
            "policy_code": policy_code,
            "summary": summary,
            "new_observation_id": round_.new_observation.observation_id,
        }
