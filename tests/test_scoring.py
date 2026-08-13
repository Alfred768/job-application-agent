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


def test_obvious_non_technical_agent_titles_are_hard_rejected():
    titles = [
        "Real Estate Field Agent",
        "Customer Service Agent",
        "Front Desk Agent",
        "Insurance Agent",
        "Commissary Agent",
        "Executive Protection Agent",
        "Junior Accountant",
        "Entry Level Auto Body Repair Technician",
        "Business Development Representative",
    ]
    for title in titles:
        job = import_job_from_text(
            f"Title: {title}\n\nBuild Python AI agents with LangChain and Kubernetes."
        )

        result = score_fit(job)

        assert result.role_track == "Other"
        assert result.score < 50
        assert result.recommendation == "reject"


def test_engineering_agent_titles_are_not_hard_rejected():
    titles = [
        "AI Agent Engineer",
        "ML Agent Engineer",
        "Software Engineer, New Grad",
        "Forward Deployed Engineer",
        "Site Reliability Engineer",
        "Machine Learning Engineer",
    ]
    for title in titles:
        job = import_job_from_text(
            f"Title: {title}\n\nBuild Python AI agents with LangChain and Kubernetes."
        )

        result = score_fit(job)

        assert result.role_track != "Other"
        assert result.score >= 50


def test_itar_citizenship_requirements_are_hard_rejected():
    job = import_job_from_text(
        "Title: Software Engineer, Defense\n\n"
        "Due to ITAR regulations, applicants must be U.S. Citizens or lawful "
        "permanent residents."
    )

    result = score_fit(job)

    assert result.recommendation == "reject"
    assert result.score == 5
    assert result.role_track == "Other"


def test_no_sponsorship_requirements_are_hard_rejected():
    job = import_job_from_text(
        "Title: Software Engineer\n\n"
        "This role is not eligible for visa sponsorship."
    )

    result = score_fit(job)

    assert result.recommendation == "reject"
    assert result.score == 5


def test_active_clearance_requirements_are_hard_rejected():
    job = import_job_from_text(
        "Title: Software Engineer\n\n"
        "An active U.S. government security clearance is required."
    )

    result = score_fit(job)

    assert result.recommendation == "reject"
    assert result.score == 5


def test_non_us_location_is_hard_rejected():
    job = import_job_from_text(
        "Title: Software Engineer\nLocation: Brasil\n\nBuild Python services."
    )

    result = score_fit(job)

    assert result.recommendation == "reject"
    assert result.score == 5


def test_non_us_diacritic_and_foreign_city_locations_are_hard_rejected():
    for location in ("Zürich", "Wien", "Rotterdam", "Kyiv, Ukraine", "Brasil", "Toulouse", "Nantes", "Espoo"):
        job = import_job_from_text(
            f"Title: Software Engineer\nLocation: {location}\n\nBuild Python services."
        )

        result = score_fit(job)

        assert result.recommendation == "reject"
        assert result.score == 5


def test_us_location_and_remote_roles_are_not_hard_rejected_by_location():
    for location in ("New York, NY, United States", "Remote", "United States"):
        job = import_job_from_text(
            f"Title: Software Engineer\nLocation: {location}\n\nBuild Python services."
        )

        assert score_fit(job).recommendation != "reject"
