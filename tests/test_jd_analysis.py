from job_agent.jd_analysis import parse_jd


def test_parse_jd_skill_matching_uses_word_boundaries():
    jd = """
    Company: Quora
    Title: Machine Learning Engineer, New Grad
    Source: ashby:quora
    Source URL: https://jobs.example.com/quora
    Apply URL: https://jobs.example.com/quora/apply
    Employment Type: FullTime

    Responsibilities:
    Build consumer-facing AI features powered by large language models.
    Run experiments to improve engagement, quality, and trust metrics.
    Benefits include medical/dental/vision coverage for eligible employees.

    Minimum Requirements:
    Experience with transformer models.
    Knowledge of Python or C++.
    """

    analysis = parse_jd(jd)

    assert analysis.required_skills == ["Python"]
    assert "RAG" not in analysis.required_skills
    assert "Rust" not in analysis.required_skills


def test_parse_jd_responsibilities_skip_metadata_lines():
    jd = """
    Company: Example
    Title: Agent Engineer
    Source: greenhouse:example
    Source URL: https://boards.greenhouse.io/example/jobs/123
    Apply URL: https://boards.greenhouse.io/example/jobs/123?gh_jid=123
    Employment Type: FullTime

    Responsibilities:
    Build LLM agents for customer workflows.
    Improve evaluation quality and system reliability.
    """

    analysis = parse_jd(jd)

    assert analysis.responsibilities[:2] == [
        "Build LLM agents for customer workflows.",
        "Improve evaluation quality and system reliability.",
    ]
    assert all("Source URL:" not in item for item in analysis.responsibilities)


def test_parse_jd_responsibilities_prefer_responsibilities_section_over_company_blurb():
    jd = """
    Company: Quora
    Title: Machine Learning Engineer

    About Quora:
    Quora is a knowledge platform with millions of users.

    Responsibilities:
    Build consumer-facing AI features powered by large language models.
    Run structured experiments to improve engagement and trust.
    Optimize latency, cost, and scalability of production AI systems.

    Minimum Requirements:
    Knowledge of Python or C++.
    """

    analysis = parse_jd(jd)

    assert analysis.responsibilities == [
        "Build consumer-facing AI features powered by large language models.",
        "Run structured experiments to improve engagement and trust.",
        "Optimize latency, cost, and scalability of production AI systems.",
    ]


def test_parse_jd_normalizes_html_sections_before_extracting_requirements():
    jd = """
    Company: Saviynt
    Title: Marketing Ops AI Agent Engineer
    <h2>What You'll Do</h2><ul><li>Build AI workflows across Salesforce and HubSpot.</li>
    <li>Test prompts and agent outputs for quality.</li></ul>
    <h2>Must-Have</h2><p>4+ years in Marketing Operations.</p>
    <p>Hands-on experience with Python, SQL, Snowflake, and Clay.</p>
    """

    analysis = parse_jd(jd)

    assert analysis.required_skills == ["Python", "SQL", "Salesforce", "HubSpot", "Snowflake", "Clay"]
    assert analysis.responsibilities == [
        "Build AI workflows across Salesforce and HubSpot.",
        "Test prompts and agent outputs for quality.",
    ]


def test_parse_jd_does_not_treat_strong_candidate_skills_as_required():
    analysis = parse_jd(
        """
        Title: Research Engineer, Machine Learning
        Requirements:
        Experience with Python and PyTorch.
        Strong candidates may have:
        Experience with Rust and C++.
        Logistics:
        Remote within the United States.
        """
    )

    assert analysis.required_skills == ["Python", "PyTorch"]
