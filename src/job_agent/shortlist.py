from __future__ import annotations

from dataclasses import asdict, dataclass

from job_agent.models import FitScore, Job
from job_agent.scoring import score_fit


@dataclass(frozen=True)
class ShortlistedJob:
    job: Job
    fit: FitScore


def shortlist_jobs(
    jobs: list[Job],
    min_score: int = 70,
    limit: int | None = None,
    *,
    diversify_companies: bool = False,
) -> list[ShortlistedJob]:
    ranked = [ShortlistedJob(job=job, fit=score_fit(job)) for job in jobs]
    shortlisted = [item for item in ranked if item.fit.score >= min_score]
    shortlisted.sort(key=lambda item: item.fit.score, reverse=True)
    if diversify_companies:
        first_per_company: list[ShortlistedJob] = []
        repeated_companies: list[ShortlistedJob] = []
        seen_companies: set[str] = set()
        for item in shortlisted:
            company_key = " ".join(str(item.job.company or "").casefold().split())
            if company_key in seen_companies:
                repeated_companies.append(item)
                continue
            seen_companies.add(company_key)
            first_per_company.append(item)
        shortlisted = first_per_company + repeated_companies
    return shortlisted[:limit] if limit is not None else shortlisted


def shortlisted_jobs_to_dicts(items: list[ShortlistedJob]) -> list[dict]:
    rows = []
    for item in items:
        row = asdict(item.job)
        row.update(
            {
                "fit_score": item.fit.score,
                "role_track": item.fit.role_track,
                "recommendation": item.fit.recommendation,
                "matched_skills": item.fit.matched_skills,
                "missing_keywords": item.fit.missing_keywords,
                "fit_explanation": item.fit.explanation,
            }
        )
        rows.append(row)
    return rows
