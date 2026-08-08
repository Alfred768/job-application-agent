from job_agent.jobs import import_job_from_text
from job_agent.scoring import classify_role, score_fit


def test_classify_agent_engineer_from_llm_agent_keywords():
    job = import_job_from_text(
        "Title: AI Agent Engineer\n\nBuild LangChain tools, RAG workflows, FastAPI services."
    )

    assert classify_role(job) == "Agent Engineer"


def test_score_fit_returns_explainable_result():
    job = import_job_from_text(
        "Title: ML Infrastructure Engineer\n\nKubernetes, Kafka, MLflow, FastAPI."
    )

    score = score_fit(job)

    assert score.score >= 70
    assert score.role_track == "ML Infra"
    assert score.reasons


def test_score_fit_counts_explicit_technical_title_as_fit_evidence():
    job = import_job_from_text(
        "Title: Software Engineer, New Grad\n\n"
        "Build distributed Python services."
    )

    score = score_fit(job)

    assert score.score == 76
    assert score.role_track == "SDE"
    assert score.recommendation == "prepare"
    assert "Title explicitly matches SDE" in score.reasons


def test_score_fit_does_not_boost_non_technical_title_from_body_keywords():
    job = import_job_from_text(
        "Title: AI Tutor - French\n\n"
        "Review model training outputs."
    )

    score = score_fit(job)

    assert score.score == 64
    assert all("Title explicitly matches" not in reason for reason in score.reasons)


def test_classify_software_engineer_ml_infrastructure_title_as_ml_infra():
    job = import_job_from_text(
        "Title: Software Engineer, ML Infrastructure, Optimization\n\n"
        "Build model lifecycle infrastructure, optimization tooling, and PyTorch workflows."
    )

    assert classify_role(job) == "ML Infra"


def test_classify_algorithm_software_engineer_title_as_ai_algorithm():
    job = import_job_from_text(
        "Title: New Grads 2026 - Software Engineer, Algorithm\n\n"
        "Design prediction, planning, mapping, and simulation algorithms for autonomous driving."
    )

    assert classify_role(job) == "AI Algorithm Engineer"


def test_classify_role_prefers_explicit_ml_engineer_title_over_agentic_body_terms():
    job = import_job_from_text(
        "Title: Machine Learning Engineer\n\n"
        "Build AI features with LLM applications, agentic workflow optimization, "
        "experimentation, reliability, and Python."
    )

    assert classify_role(job) == "MLE"


def test_classify_role_does_not_select_engineering_resume_track_for_gtm_operations_title():
    job = import_job_from_text(
        "Title: Marketing Ops AI Agent Engineer\n\nBuild LangChain agents and RAG workflows."
    )

    assert classify_role(job) == "Other"
    assert score_fit(job).score == 20


def test_classify_research_engineer_machine_learning_as_mle():
    job = import_job_from_text(
        "Title: Research Engineer, Machine Learning\n\nBuild agentic ML systems."
    )

    assert classify_role(job) == "MLE"
