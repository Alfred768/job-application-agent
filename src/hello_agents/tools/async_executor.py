"""Concurrent read-only Tool execution with policy decisions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from hello_agents.core.contracts import ToolCall, ToolEffect, ToolResult
from hello_agents.core.execution import ControlledExecution
from hello_agents.core.policy import CompositePolicyGate, ReadOnlyPolicyGate

from .registry import ToolRegistry


@dataclass(frozen=True)
class AsyncTask:
    tool_name: str
    params: dict[str, Any]
    label: str = ""
    purpose: str = ""
    context: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class AsyncResult:
    label: str
    tool_name: str
    output: Any
    error: Optional[str] = None
    tool_result: Optional[ToolResult] = None

    @property
    def ok(self) -> bool:
        return self.error is None


class AsyncToolExecutor:
    """Run OBSERVE/READ tools concurrently through ControlledExecution."""

    def __init__(
        self,
        registry: ToolRegistry,
        max_workers: int = 5,
        *,
        execution: Optional[ControlledExecution] = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive.")
        if execution is not None and execution.registry is not registry:
            raise ValueError(
                "Async executor registry must match execution.registry."
            )
        base = execution or ControlledExecution(registry)
        self.registry = registry
        self.max_workers = max_workers
        self.execution = ControlledExecution(
            registry,
            policy_gate=CompositePolicyGate(
                [ReadOnlyPolicyGate(), base.policy_gate]
            ),
            short_term_memory=base.short_term_memory,
            long_term_memory=base.long_term_memory,
            perception=base.perception,
        )

    def run_concurrent(
        self,
        tasks: list[AsyncTask],
    ) -> list[AsyncResult]:
        if not tasks:
            return []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(tasks))
        ) as pool:
            return list(pool.map(self._execute, tasks))

    def run_tool_calls(
        self,
        calls: list[ToolCall] | tuple[ToolCall, ...],
    ) -> list[ToolResult]:
        """Execute exact OBSERVE/READ calls concurrently.

        The read-only policy is still applied by ``self.execution``. Keeping
        the original ToolCall objects preserves their call IDs for AgentRound
        and Observation continuity.
        """
        if not calls:
            return []
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(calls))
        ) as pool:
            return list(pool.map(self.execution.execute, calls))

    def run_concurrent_simple(
        self,
        items: list[tuple[str, dict[str, Any]]],
    ) -> list[AsyncResult]:
        return self.run_concurrent(
            [
                AsyncTask(
                    tool_name=name,
                    params=params,
                    label=name,
                )
                for name, params in items
            ]
        )

    def _execute(self, task: AsyncTask) -> AsyncResult:
        tool = self.registry.get_tool(task.tool_name)
        call = ToolCall(
            tool_name=task.tool_name,
            parameters=task.params,
            effect=(
                tool.effect
                if tool is not None
                else ToolEffect.READ
            ),
            purpose=task.purpose or f"Concurrent read: {task.tool_name}",
            context=dict(task.context or {}),
        )
        result = self.execution.execute(call)
        return AsyncResult(
            label=task.label or task.tool_name,
            tool_name=task.tool_name,
            output=result.output,
            error=result.error,
            tool_result=result,
        )
