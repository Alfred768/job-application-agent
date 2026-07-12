"""Tool chain - sequenced tool execution for the HelloAgents base.

A ``ToolChain`` runs an ordered list of tools, threading results through a
shared context dict. Each step has a ``params_builder`` that turns the current
context into the tool's input parameters, so downstream steps can reuse
upstream outputs.

The PEAS/HelloAgents architecture defines three career chains:

- JDReviewChain:        jd_parser -> fit_scorer -> resume_selector -> review_packet
- ResumePreparationChain: resume_tailor -> truthfulness_check
- ApplicationFormChain: form_inspector -> sensitive_field_detector -> form_filler
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .registry import ToolRegistry


@dataclass
class ChainStep:
    tool_name: str
    params_builder: Callable[[dict[str, Any]], dict[str, Any]]
    description: str = ""


@dataclass
class ChainResult:
    name: str
    outputs: dict[str, Any] = field(default_factory=dict)
    final_output: str = ""

    def get(self, tool_name: str, default: Any = None) -> Any:
        return self.outputs.get(tool_name, default)


class ToolChain:
    """Run a sequence of registered tools, threading outputs through context."""

    def __init__(self, name: str, steps: list[ChainStep], registry: ToolRegistry):
        self.name = name
        self.steps = steps
        self.registry = registry

    def run(self, context: Optional[dict[str, Any]] = None) -> ChainResult:
        ctx: dict[str, Any] = dict(context or {})
        final = ""
        for step in self.steps:
            tool = self.registry.get_tool(step.tool_name)
            if tool is None:
                ctx[step.tool_name] = f"Error: tool '{step.tool_name}' not registered"
                final = ctx[step.tool_name]
                continue
            params = step.params_builder(ctx)
            try:
                output = tool.run(params)
            except Exception as exc:  # noqa: BLE001
                output = f"Error: {step.tool_name} failed: {exc}"
            ctx[step.tool_name] = output
            final = output
        return ChainResult(name=self.name, outputs=ctx, final_output=final)


# ---------------------------------------------------------------------------
# Career chain factories (match the PEAS/HelloAgents architecture doc)
# ---------------------------------------------------------------------------


def build_jd_review_chain(
    registry: ToolRegistry,
    jd_text: str,
    resume_source_dir: Optional[str] = None,
) -> ToolChain:
    """jd_parser -> fit_scorer -> resume_selector(optional) -> review_packet."""
    steps = [
        ChainStep("jd_parser", lambda c, jd=jd_text: {"jd_text": jd}),
        ChainStep("fit_scorer", lambda c, jd=jd_text: {"input": jd}),
    ]
    if resume_source_dir:
        steps.append(
            ChainStep(
                "resume_selector",
                lambda c, jd=jd_text, rs=resume_source_dir: {"source_dir": rs, "jd_text": jd},
            )
        )
    steps.append(ChainStep("review_packet", lambda c, jd=jd_text: {"input": jd}))
    return ToolChain("jd_review", steps, registry)


def build_resume_preparation_chain(
    registry: ToolRegistry,
    jd_text: str,
    resume_text: Optional[str] = None,
) -> ToolChain:
    """resume_tailor -> truthfulness_check with optional source evidence."""
    return ToolChain(
        "resume_preparation",
        [
            ChainStep(
                "resume_tailor",
                lambda c, jd=jd_text, rt=resume_text: {
                    "jd_text": jd,
                    "resume_text": rt or "",
                },
            ),
            ChainStep("truthfulness_check", lambda c: {"plan_json": c.get("resume_tailor", "{}")}),
        ],
        registry,
    )


def build_application_form_chain(
    registry: ToolRegistry,
    form_snapshot_json: str,
    profile_json: str,
) -> ToolChain:
    """form_inspector -> sensitive_field_detector -> form_filler."""
    return ToolChain(
        "application_form",
        [
            ChainStep("form_inspector", lambda c, fs=form_snapshot_json: {"form_snapshot_json": fs}),
            ChainStep(
                "sensitive_field_detector",
                lambda c, fs=form_snapshot_json: {"form_snapshot_json": fs},
            ),
            ChainStep(
                "form_filler",
                lambda c, fs=form_snapshot_json, pr=profile_json: {
                    "form_snapshot_json": fs,
                    "profile_json": pr,
                },
            ),
        ],
        registry,
    )
