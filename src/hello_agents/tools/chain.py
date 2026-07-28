"""Sequential Tool composition through ControlledExecution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from hello_agents.core.contracts import ToolCall, ToolEffect, ToolResult
from hello_agents.core.execution import ControlledExecution

from .registry import ToolRegistry


@dataclass(frozen=True)
class ChainStep:
    tool_name: str
    params_builder: Callable[[dict[str, Any]], dict[str, Any]]
    description: str = ""
    context_builder: Optional[
        Callable[[dict[str, Any]], Mapping[str, Any]]
    ] = None


@dataclass(frozen=True)
class ChainResult:
    name: str
    outputs: dict[str, Any] = field(default_factory=dict)
    final_output: Any = ""
    tool_results: tuple[ToolResult, ...] = ()
    status: str = "completed"

    def get(self, tool_name: str, default: Any = None) -> Any:
        return self.outputs.get(tool_name, default)


class ToolChain:
    """Run ordered ToolCalls without bypassing the policy gate."""

    def __init__(
        self,
        name: str,
        steps: list[ChainStep],
        registry: ToolRegistry,
        *,
        execution: Optional[ControlledExecution] = None,
    ) -> None:
        if execution is not None and execution.registry is not registry:
            raise ValueError(
                "ToolChain registry must match ControlledExecution.registry."
            )
        self.name = name
        self.steps = list(steps)
        self.registry = registry
        self.execution = execution or ControlledExecution(registry)

    def run(
        self,
        context: Optional[dict[str, Any]] = None,
        *,
        stop_on_policy_denial: bool = True,
    ) -> ChainResult:
        values = dict(context or {})
        results: list[ToolResult] = []
        final_output: Any = ""
        status = "completed"

        for step in self.steps:
            tool = self.registry.get_tool(step.tool_name)
            parameters = step.params_builder(values)
            call = ToolCall(
                tool_name=step.tool_name,
                parameters=parameters,
                effect=(
                    tool.effect
                    if tool is not None
                    else ToolEffect.READ
                ),
                purpose=step.description or f"{self.name}:{step.tool_name}",
                context=(
                    dict(step.context_builder(values))
                    if step.context_builder is not None
                    else {}
                ),
            )
            result = self.execution.execute(call)
            results.append(result)
            final_output = (
                result.output
                if result.ok
                else f"Error: {result.error}"
            )
            values[step.tool_name] = final_output
            if not result.ok:
                if (
                    result.policy_decision is not None
                    and not result.policy_decision.allowed
                ):
                    status = "policy_blocked"
                    if stop_on_policy_denial:
                        break
                else:
                    status = "tool_failed"

        return ChainResult(
            name=self.name,
            outputs=values,
            final_output=final_output,
            tool_results=tuple(results),
            status=status,
        )

    def run_calls(
        self,
        calls: list[ToolCall] | tuple[ToolCall, ...],
        *,
        stop_on_policy_denial: bool = True,
    ) -> ChainResult:
        """Execute exact pre-built ToolCalls through this chain's runtime.

        Agent Core uses this path so call IDs, bounded-plan context, effects,
        and policy bindings are preserved instead of being reconstructed from
        ``ChainStep`` objects.
        """
        results: list[ToolResult] = []
        outputs: dict[str, Any] = {}
        final_output: Any = ""
        status = "completed"
        for call in calls:
            result = self.execution.execute(call)
            results.append(result)
            final_output = (
                result.output
                if result.ok
                else f"Error: {result.error}"
            )
            outputs[call.tool_name] = final_output
            if not result.ok:
                if (
                    result.policy_decision is not None
                    and not result.policy_decision.allowed
                ):
                    status = "policy_blocked"
                    if stop_on_policy_denial:
                        break
                else:
                    status = "tool_failed"
        return ChainResult(
            name=self.name,
            outputs=outputs,
            final_output=final_output,
            tool_results=tuple(results),
            status=status,
        )


def build_jd_review_chain(
    registry: ToolRegistry,
    jd_text: str,
    resume_source_dir: Optional[str] = None,
    *,
    execution: Optional[ControlledExecution] = None,
) -> ToolChain:
    """Build JD parse, fit score, optional resume selection, and review."""
    steps = [
        ChainStep(
            "jd_parser",
            lambda context, jd=jd_text: {"jd_text": jd},
            "Parse the observed job description.",
        ),
        ChainStep(
            "fit_scorer",
            lambda context, jd=jd_text: {"input": jd},
            "Score the job against approved role tracks.",
        ),
    ]
    if resume_source_dir:
        steps.append(
            ChainStep(
                "resume_selector",
                lambda context, jd=jd_text, source=resume_source_dir: {
                    "source_dir": source,
                    "jd_text": jd,
                },
                "Select an unchanged approved PDF.",
            )
        )
    steps.append(
        ChainStep(
            "review_packet",
            lambda context, jd=jd_text: {"input": jd},
            "Render the review packet.",
        )
    )
    return ToolChain(
        "jd_review",
        steps,
        registry,
        execution=execution,
    )

def build_application_form_chain(
    registry: ToolRegistry,
    form_snapshot_json: str,
    profile_json: str,
    *,
    execution: Optional[ControlledExecution] = None,
) -> ToolChain:
    """Build form inspection, sensitive detection, and truthful fill plan."""
    return ToolChain(
        "application_form",
        [
            ChainStep(
                "form_inspector",
                lambda context, snapshot=form_snapshot_json: {
                    "form_snapshot_json": snapshot
                },
                "Normalize the observed ATS form.",
            ),
            ChainStep(
                "sensitive_field_detector",
                lambda context, snapshot=form_snapshot_json: {
                    "form_snapshot_json": snapshot
                },
                "Identify policy-controlled sensitive fields.",
            ),
            ChainStep(
                "form_filler",
                lambda context, snapshot=form_snapshot_json, profile=profile_json: {
                    "form_snapshot_json": snapshot,
                    "profile_json": profile,
                },
                "Create a truthful fill plan without submitting.",
            ),
        ],
        registry,
        execution=execution,
    )
