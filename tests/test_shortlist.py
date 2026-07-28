from job_agent import shortlist as shortlist_module
from job_agent.jobs import import_job_from_text
from job_agent.models import FitScore, Job
from job_agent.shortlist import shortlist_jobs


def test_shortlist_jobs_filters_by_fit_score_and_sorts_descending():
    agent_job = import_job_from_text(
        "Company: Acme\nTitle: Agent Engineer\n\nBuild LangChain agents, RAG workflows, tools, and LLM systems."
    )
    backend_job = import_job_from_text(
        "Company: WebCo\nTitle: Backend Engineer\n\nBuild APIs with Postgres and Redis."
    )
    unrelated_job = import_job_from_text(
        "Company: RetailCo\nTitle: Store Manager\n\nManage retail operations and staffing."
    )

    shortlisted = shortlist_jobs([unrelated_job, backend_job, agent_job], min_score=60, limit=2)

    assert [item.job.title for item in shortlisted] == ["Agent Engineer", "Backend Engineer"]
    assert shortlisted[0].fit.score >= shortlisted[1].fit.score
    assert all(item.fit.score >= 60 for item in shortlisted)


def test_shortlist_can_diversify_companies_before_using_duplicate_slots(monkeypatch):
    scores = {
        "Acme Platform": 99,
        "Acme Product": 98,
        "Web Backend": 90,
        "Data Infra": 80,
    }
    monkeypatch.setattr(
        shortlist_module,
        "score_fit",
        lambda job: FitScore(score=scores[job.title], role_track="software"),
    )
    jobs = [
        Job(title="Acme Platform", company="Acme", raw_jd="platform"),
        Job(title="Acme Product", company="Acme", raw_jd="product"),
        Job(title="Web Backend", company="WebCo", raw_jd="backend"),
        Job(title="Data Infra", company="DataCo", raw_jd="infra"),
    ]

    shortlisted = shortlist_jobs(
        jobs,
        min_score=70,
        limit=3,
        diversify_companies=True,
    )

    assert [item.job.title for item in shortlisted] == [
        "Acme Platform",
        "Web Backend",
        "Data Infra",
    ]
