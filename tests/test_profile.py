from job_agent.profile import (
    EducationEntry,
    RichProfile,
    WorkEntry,
    parse_resume_to_profile,
    render_profile_template,
)

SAMPLE_RESUME = """Gaoyi Wu
Agent Engineer  |  New York, NY  |  gaoyi@example.com  |  github.com/gaoyi

Summary
Software engineer focused on LLM agent workflows.

Skills
Python, FastAPI, LangChain, RAG

Experience
AI Platform Engineer — Acme Bots
Built guarded agent orchestration with tool registry and safety gates.
Shipped a RAG service on FastAPI.
Backend Engineer at DataForge
Built data pipelines with Postgres.

Education
B.S. Computer Science — State University
"""


def test_parse_resume_extracts_contact_and_skills():
    profile = parse_resume_to_profile(SAMPLE_RESUME)

    assert profile.name == "Gaoyi Wu"
    assert profile.email == "gaoyi@example.com"
    assert "New York" in profile.location
    assert "github.com/gaoyi" in profile.github
    assert "Python" in profile.skills
    assert "LangChain" in profile.skills


def test_parse_resume_extracts_multiple_work_entries():
    profile = parse_resume_to_profile(SAMPLE_RESUME)

    assert len(profile.work_history) == 2
    first = profile.work_history[0]
    assert first.title == "AI Platform Engineer"
    assert first.company == "Acme Bots"
    assert "guarded agent orchestration" in first.description
    second = profile.work_history[1]
    assert second.title == "Backend Engineer"
    assert second.company == "DataForge"


def test_parse_resume_extracts_education():
    profile = parse_resume_to_profile(SAMPLE_RESUME)

    assert len(profile.education) == 1
    assert profile.education[0].degree == "B.S. Computer Science"
    assert profile.education[0].school == "State University"


def test_profile_to_dict_roundtrip():
    profile = RichProfile(
        name="Gaoyi Wu",
        work_history=[WorkEntry(title="Engineer", company="Acme")],
        education=[EducationEntry(school="State U", degree="B.S.")],
    )
    d = profile.to_dict()

    assert d["name"] == "Gaoyi Wu"
    assert d["work_history"][0]["title"] == "Engineer"
    assert d["education"][0]["school"] == "State U"


def test_profile_template_has_structured_sections():
    tpl = render_profile_template()

    assert "work_history" in tpl and isinstance(tpl["work_history"], list)
    assert "education" in tpl and isinstance(tpl["education"], list)
    assert "demographics" in tpl
    assert "answers" in tpl
