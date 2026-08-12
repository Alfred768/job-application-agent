from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from job_agent.models import FitScore, Job
from job_agent.scoring import score_fit


# Company-size tiers used when ordering a shortlist from startup to big company.
# Tier 1 = startup / early-stage, Tier 2 = mid-size / growth, Tier 3 = big tech / enterprise.
# Lower tiers are ordered first.  Any company not listed is treated as tier 1 (startup)
# by default so that smaller employers are preferred first.
_STARTUP_TIER: set[str] = set()
_MID_TIER: set[str] = {
    "airtable", "algolia", "anduril", "asana", "auth0", "baseten", "brex",
    "checkr", "chime", "cloudflare", "coda", "coinbase", "cursor", "databricks",
    "datadog", "deepgram", "doordash", "doordashusa", "dropbox", "figma",
    "flexport", "gusto", "instacart", "instalilyai", "intercom", "kalshi",
    "klaviyo", "netic", "notion", "okta", "otterai", "palantir", "plaid",
    "postman", "ramp", "robinhood", "scale ai", "scaleai", "snowflake", "splunk",
    "square", "stripe", "toast", "twilio", "waymo", "wework", "wise",
    "zapier", "zendesk", "anyscale", "arkose", "character", "coreweave",
    "decagon", "diligent robotics", "elevenlabs", "formlabs", "glean", "gleanwork",
    "immuta", "iterable", "langchain", "mark43", "recorded future", "recordedfuture",
    "relativity", "shield ai", "shieldai", "singlestore", "snorkel", "snorkelai",
    "togetherai", "true anomaly", "trueanomalyinc", "uncountable", "walkme",
}
_BIG_TIER: set[str] = {
    "adobe", "airbnb", "alphabet", "amazon", "amd", "anthropic", "apple",
    "atlassian", "bloomberg", "booking", "cerebras", "cisco", "ebay",
    "figma", "github", "google", "hubspot", "ibm", "intel", "intuit",
    "linkedin", "lyft", "meta", "microsoft", "netflix", "nvidia", "openai",
    "oracle", "paypal", "pinterest", "qualcomm", "reddit", "roblox",
    "salesforce", "samsung", "sap", "shopify", "snap", "sony",
    "spotify", "squarespace", "tesla", "tiktok", "twitter", "uber",
    "twitch", "vercel", "vmware", "walmart", "x", "xai", "yahoo",
    "adyen", "akuna capital", "akunacapital", "canonical", "checkout.com",
    "deliveroo", "dialpad", "elastic", "epic games", "epicgames", "five9",
    "general assembly", "generalassembly", "grail", "grailbio", "lucid motors",
    "lucidmotors", "match group", "matchgroup", "mongodb", "natera", "new relic",
    "newrelic", "onemedical", "pubmatic", "pure storage", "purestorage", "roku",
    "rti", "samsara", "smartsheet", "sofi", "trainline", "trivago", "wayve",
    "workstream",
}


def _normalize_company(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


def _is_linkedin_job(job: Job) -> bool:
    """Return True when a job points at LinkedIn, which must never be automated."""
    for raw_url in (job.apply_url, job.source_url):
        if not raw_url:
            continue
        try:
            parsed = urlparse(str(raw_url))
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if host.endswith("linkedin.com") and path.startswith("/jobs"):
            return True
    return False


def company_tier(company: str) -> int:
    """Return a size tier for ``company`` (1=startup, 2=mid, 3=big).

    Unknown companies default to tier 1 so that smaller/start-up employers are
    preferred first when ordering by startup-to-big-company.
    """
    key = _normalize_company(company)
    # Strip common suffixes/prefixes so e.g. "Sony Interactive Entertainment"
    # matches "sony".
    tokens = set(key.split())
    for big in _BIG_TIER:
        if big in key or big in tokens:
            return 3
    for mid in _MID_TIER:
        if mid in key or mid in tokens:
            return 2
    return 1


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
    unique_companies: bool = False,
    startup_to_big: bool = False,
) -> list[ShortlistedJob]:
    """Rank eligible jobs and optionally enforce company-level constraints.

    Args:
        jobs: Eligible jobs returned by the candidate screener.
        min_score: Minimum fit score to keep.
        limit: Maximum number of jobs to return.
        diversify_companies: If True, keep one job per company before adding
            additional jobs from the same company.  This reduces concentration
            but still allows multiple jobs per company when the pool is small.
        unique_companies: If True, keep only the highest-scoring job for each
            company.  This guarantees no duplicate companies in the result.
        startup_to_big: If True, order the shortlist so that startup-tier
            companies appear before mid-tier and big-tier companies while still
            respecting fit score within each tier.
    """
    eligible_input = [job for job in jobs if not _is_linkedin_job(job)]
    ranked = [ShortlistedJob(job=job, fit=score_fit(job)) for job in eligible_input]
    shortlisted = [item for item in ranked if item.fit.score >= min_score]
    shortlisted.sort(key=lambda item: item.fit.score, reverse=True)

    if unique_companies:
        best_per_company: dict[str, ShortlistedJob] = {}
        for item in shortlisted:
            key = _normalize_company(item.job.company)
            if key not in best_per_company:
                best_per_company[key] = item
        shortlisted = list(best_per_company.values())
        # Re-sort by tier then score when startup-to-big ordering is requested.
        if startup_to_big:
            shortlisted.sort(
                key=lambda item: (
                    company_tier(item.job.company),
                    -item.fit.score,
                )
            )
        else:
            shortlisted.sort(key=lambda item: item.fit.score, reverse=True)
    elif diversify_companies:
        first_per_company: list[ShortlistedJob] = []
        repeated_companies: list[ShortlistedJob] = []
        seen_companies: set[str] = set()
        for item in shortlisted:
            company_key = _normalize_company(item.job.company)
            if company_key in seen_companies:
                repeated_companies.append(item)
                continue
            seen_companies.add(company_key)
            first_per_company.append(item)
        shortlisted = first_per_company + repeated_companies
        if startup_to_big:
            # Preserve the one-per-company preference; sort each group by tier then score.
            first_per_company.sort(
                key=lambda item: (company_tier(item.job.company), -item.fit.score)
            )
            repeated_companies.sort(
                key=lambda item: (company_tier(item.job.company), -item.fit.score)
            )
            shortlisted = first_per_company + repeated_companies
    elif startup_to_big:
        shortlisted.sort(
            key=lambda item: (
                company_tier(item.job.company),
                -item.fit.score,
            )
        )

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
