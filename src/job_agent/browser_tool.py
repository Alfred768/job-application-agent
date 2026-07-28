"""Controlled bridge between the Agent Core and the production ATS runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from typing import Any, Callable, Mapping

from hello_agents.core.contracts import ToolEffect
from hello_agents.tools.base import Tool, ToolParameter


@dataclass(frozen=True)
class BrowserExecutionOptions:
    """Runtime dependencies supplied by the production execution entry point."""

    node_binary: str = "node"
    timeout_seconds: int = 300
    runner: Callable[..., subprocess.CompletedProcess] | None = None
    use_gmail_verification: bool = True
    browser_headless: bool | None = None
    required_resume_pdf: str | Path | None = None
    required_resume_source_dir: str | Path | None = None


class DeterministicRuntimeLLM:
    """A no-network planner used by production browser execution.

    Browser actions are selected from the bounded plan by JobApplicationAgent;
    this object deliberately never sends candidate or page data to an LLM.
    """

    provider = "deterministic"

    def invoke(self, _messages: list[dict[str, str]], **_kwargs: Any) -> str:
        return ""


class BrowserExecutionTool(Tool):
    """Run one ATS runtime only after ControlledExecution authorizes it."""

    def __init__(self, options: BrowserExecutionOptions) -> None:
        # SUBMIT is the maximum possible effect. effective_effect narrows a
        # particular invocation to WRITE when final submission is disabled.
        super().__init__(
            name="browser_execute",
            description=(
                "Execute one policy-authorized ATS runtime and return sanitized "
                "page, field, review, and terminal observations."
            ),
            effect=ToolEffect.SUBMIT,
        )
        self.options = options

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="application",
                type="object",
                description=(
                    "Prepared single-application runtime descriptor. It is "
                    "never included in conversation or long-term memory."
                ),
            )
        ]

    def effective_effect(self, parameters: dict[str, Any]) -> ToolEffect:
        application = parameters.get("application")
        if not isinstance(application, Mapping):
            return ToolEffect.SUBMIT
        from job_agent.execution import browser_execution_call

        script_path = (
            application.get("runtime_script_path")
            or application.get("fill_script_path")
            or ""
        )
        return browser_execution_call(
            application,
            script_path,
            real_runtime=self.options.runner is None,
            environ=self._runtime_environ(),
        ).effect

    def run(self, parameters: dict[str, Any]) -> dict[str, Any]:
        application = parameters.get("application")
        if not isinstance(application, Mapping):
            raise ValueError("application must be a mapping")
        from job_agent.execution import execute_controlled_application

        record = execute_controlled_application(
            dict(application),
            node_binary=self.options.node_binary,
            timeout_seconds=self.options.timeout_seconds,
            runner=self.options.runner,
            use_gmail_verification=self.options.use_gmail_verification,
            browser_headless=self.options.browser_headless,
            required_resume_pdf=self.options.required_resume_pdf,
            required_resume_source_dir=self.options.required_resume_source_dir,
        )
        return {
            "record": record,
            "runtime_observations": list(
                record.get("runtime_observations") or []
            ),
        }

    def _runtime_environ(self) -> Mapping[str, str] | None:
        if self.options.browser_headless is None:
            return None
        environment = os.environ.copy()
        environment["BROWSER_HEADLESS"] = (
            "true" if self.options.browser_headless else "false"
        )
        return environment
