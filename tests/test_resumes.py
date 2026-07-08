from pathlib import Path

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
