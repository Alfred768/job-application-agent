"""Async tool executor - concurrent execution of read-only tools.

Used for safe, parallelizable work such as fetching multiple public job
sources at once. Per the PEAS design, it must NOT be used for final form
submission, file overwrites, or database state changes.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from .registry import ToolRegistry


@dataclass
class AsyncTask:
    tool_name: str
    params: dict[str, Any]
    label: str = ""


@dataclass
class AsyncResult:
    label: str
    tool_name: str
    output: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class AsyncToolExecutor:
    """Run read-only tools concurrently with a thread pool."""

    def __init__(self, registry: ToolRegistry, max_workers: int = 5):
        self.registry = registry
        self.max_workers = max_workers

    def run_concurrent(self, tasks: list[AsyncTask]) -> list[AsyncResult]:
        results: list[AsyncResult] = []
        if not tasks:
            return results

        def _exec(task: AsyncTask) -> AsyncResult:
            tool = self.registry.get_tool(task.tool_name)
            if tool is None:
                return AsyncResult(
                    label=task.label or task.tool_name,
                    tool_name=task.tool_name,
                    output="",
                    error=f"tool '{task.tool_name}' not registered",
                )
            try:
                out = tool.run(task.params)
                return AsyncResult(
                    label=task.label or task.tool_name, tool_name=task.tool_name, output=out
                )
            except Exception as exc:  # noqa: BLE001
                return AsyncResult(
                    label=task.label or task.tool_name,
                    tool_name=task.tool_name,
                    output="",
                    error=str(exc),
                )

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks))) as pool:
            futures = {pool.submit(_exec, t): t for t in tasks}
            for fut in as_completed(futures):
                results.append(fut.result())
        return results

    def run_concurrent_simple(
        self, items: list[tuple[str, dict[str, Any]]]
    ) -> list[AsyncResult]:
        """Convenience: pass (tool_name, params) tuples."""
        return self.run_concurrent(
            [AsyncTask(tool_name=name, params=params, label=name) for name, params in items]
        )
