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
