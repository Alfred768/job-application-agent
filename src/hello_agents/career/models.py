"""Career domain state models."""

from __future__ import annotations

from dataclasses import dataclass, field

from hello_agents.core.contracts import (
    AgentRound,
    AgentThought,
    MemoryUpdate,
    Observation,
    PolicyDecision,
    ToolResult,
)
from job_agent.forms import FormFillPlan
from job_agent.models import FitScore, Job, ResumeTemplate


@dataclass
class JobApplicationState:
    """State tracked by the job application Agent Core."""

    job: Job | None = None
    fit_score: FitScore | None = None
    selected_resume: ResumeTemplate | None = None
    review_packet: str | None = None
    jd_analysis: str | None = None
    submit_gate: str | None = None
    llm_review_notes: str | None = None
    tracking: str | None = None
    application_package: str | None = None
    form_fields: str | None = None
    sensitive_fields: str | None = None
    form_plan: FormFillPlan = field(default_factory=FormFillPlan)
    safety_gates: list[str] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    thoughts: list[AgentThought] = field(default_factory=list)
    rounds: list[AgentRound] = field(default_factory=list)
    memory_updates: list[MemoryUpdate] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    policy_decisions: list[PolicyDecision] = field(default_factory=list)
    memory_hits: list[dict] = field(default_factory=list)
    architecture_status: str = "new"
    status: str = "new"
