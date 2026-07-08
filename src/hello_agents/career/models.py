"""Career domain state models."""

from __future__ import annotations

from dataclasses import dataclass, field

from job_agent.forms import FormFillPlan
from job_agent.models import FitScore, Job, ResumeTemplate


@dataclass
class JobApplicationState:
    """State tracked by the PEAS-designed job application agent."""

    job: Job | None = None
    fit_score: FitScore | None = None
    selected_resume: ResumeTemplate | None = None
    review_packet: str | None = None
    form_plan: FormFillPlan = field(default_factory=FormFillPlan)
    safety_gates: list[str] = field(default_factory=list)
    status: str = "new"
