from __future__ import annotations

from job_agent.models import FitScore, Job


def _bullet_list(items: list[str]) -> str:
    if not items:
        return "- None"
    return "\n".join(f"- {item}" for item in items)


def render_markdown_review(job: Job, score: FitScore) -> str:
    return "\n".join(
        [
            "# Application Review",
            "",
            f"Company: {job.company}",
            f"Title: {job.title}",
            f"Location: {job.location or 'Unknown'}",
            "",
            "## Fit",
            "",
            f"Score: {score.score}",
            f"Recommended resume track: {score.role_track}",
            f"Recommendation: {score.recommendation}",
            "",
            "## Reasons",
            "",
            _bullet_list(score.reasons),
            "",
            "## Missing Keywords",
            "",
            _bullet_list(score.missing_keywords),
            "",
            "## Safety Gates",
            "",
            "- Use compliant job sources only.",
            "- Preserve truthful resume evidence.",
            "- Final Submit remains manual unless an allowed source-specific adapter says otherwise.",
            "",
        ]
    )
