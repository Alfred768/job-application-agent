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


def test_shortlist_excludes_linkedin_jobs_even_with_high_score():
    linkedin_job = Job(
        title="LinkedIn Role",
        company="SomeCo",
        raw_jd="Build agents and AI systems.",
        apply_url="https://www.linkedin.com/jobs/view/123456789",
    )
    source_linkedin_job = Job(
        title="Source LinkedIn Role",
        company="OtherCo",
        raw_jd="Build agents and AI systems.",
        source_url="https://www.linkedin.com/jobs/view/987654321",
    )
    regular_job = import_job_from_text(
        "Company: Acme\nTitle: Agent Engineer\n\nBuild LangChain agents, RAG workflows, tools, and LLM systems."
    )

    shortlisted = shortlist_jobs(
        [linkedin_job, source_linkedin_job, regular_job],
        min_score=0,
    )

    assert [item.job.title for item in shortlisted] == ["Agent Engineer"]


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


def test_shortlist_unique_companies_keeps_only_best_per_company(monkeypatch):
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
        limit=10,
        unique_companies=True,
    )

    companies = [item.job.company for item in shortlisted]
    assert len(companies) == len(set(companies))
    assert "Acme" in companies
    assert "WebCo" in companies
    assert "DataCo" in companies
    assert [item.job.title for item in shortlisted[:1]] == ["Acme Platform"]


def test_shortlist_startup_to_big_orders_by_tier_then_score(monkeypatch):
    scores = {
        "Google Role": 95,
        "Palantir Role": 92,
        "Tiny Startup Role": 90,
    }
    monkeypatch.setattr(
        shortlist_module,
        "score_fit",
        lambda job: FitScore(score=scores[job.title], role_track="software"),
    )
    jobs = [
        Job(title="Google Role", company="Google", raw_jd="big"),
        Job(title="Palantir Role", company="Palantir", raw_jd="mid"),
        Job(title="Tiny Startup Role", company="Tiny Startup", raw_jd="startup"),
    ]

    shortlisted = shortlist_jobs(
        jobs,
        min_score=70,
        startup_to_big=True,
    )

    assert [item.job.company for item in shortlisted] == [
        "Tiny Startup",
        "Palantir",
        "Google",
    ]


def test_shortlist_unique_companies_and_startup_to_big_combined(monkeypatch):
    scores = {
        "Google Role A": 95,
        "Google Role B": 94,
        "Palantir Role": 92,
        "Tiny Startup Role": 90,
    }
    monkeypatch.setattr(
        shortlist_module,
        "score_fit",
        lambda job: FitScore(score=scores[job.title], role_track="software"),
    )
    jobs = [
        Job(title="Google Role A", company="Google", raw_jd="big a"),
        Job(title="Google Role B", company="Google", raw_jd="big b"),
        Job(title="Palantir Role", company="Palantir", raw_jd="mid"),
        Job(title="Tiny Startup Role", company="Tiny Startup", raw_jd="startup"),
    ]

    shortlisted = shortlist_jobs(
        jobs,
        min_score=70,
        unique_companies=True,
        startup_to_big=True,
    )

    companies = [item.job.company for item in shortlisted]
    assert len(companies) == len(set(companies))
    assert companies == ["Tiny Startup", "Palantir", "Google"]
