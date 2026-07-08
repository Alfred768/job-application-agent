from pathlib import Path

from job_agent.resume_plans import propose_resume_edit_plan, render_tailored_resume_draft
from job_agent.resumes import index_resume_templates, infer_track_from_filename


def test_infer_track_from_known_resume_names():
    assert infer_track_from_filename("GAOYI_WU_Agent_Engineer.docx") == "Agent Engineer"
    assert infer_track_from_filename("GAOYI_WU_ML_Infra.docx") == "ML Infra"
    assert infer_track_from_filename("GAOYI_WU_Data_Scientist.pdf") == "Data Scientist"


def test_index_resume_templates_pairs_docx_and_pdf(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")

    templates = index_resume_templates(tmp_path)

    assert len(templates) == 1
    assert templates[0].track == "Agent Engineer"
    assert templates[0].docx_path == Path(tmp_path / "GAOYI_WU_Agent_Engineer.docx")
    assert templates[0].pdf_path == Path(tmp_path / "GAOYI_WU_Agent_Engineer.pdf")


def test_render_tailored_resume_draft_inserts_only_supported_keywords():
    jd = "Title: Agent Engineer\n\nBuild LangChain agents with FastAPI, RAG, and Rust."
    plan = propose_resume_edit_plan(jd, resume_track="Agent Engineer")
    base_resume = "Gaoyi Wu\n\nBuilt LLM workflow tools with Python and FastAPI."

    draft = render_tailored_resume_draft(base_resume, plan)

    assert "# Tailored Resume Draft" in draft
    assert "Target track: Agent Engineer" in draft
    assert "LangChain" in draft
    assert "FastAPI" in draft
    supported_section = draft.split("## Review Required", 1)[0]
    assert "Rust" not in supported_section
    assert "Unsupported JD keywords not inserted: Rust" in draft
    assert base_resume in draft
