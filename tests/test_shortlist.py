from job_agent.jobs import import_job_from_text
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
