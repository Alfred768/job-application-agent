from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from job_agent.models import ResumeTemplate
from job_agent.resumes import (
    ResumePathError,
    extract_resume_text,
    index_resume_templates,
    infer_track_from_filename,
    resolve_original_resume_pdf,
    select_best_resume_template,
)


def write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", "<Types></Types>")
        docx.writestr("word/document.xml", document_xml)


def test_infer_track_from_known_resume_names():
    assert infer_track_from_filename("GAOYI_WU_Agent_Engineer.docx") == "Agent Engineer"
    assert infer_track_from_filename("GAOYI_WU_ML_Infra.docx") == "ML Infra"
    assert infer_track_from_filename("GAOYI_WU_Data_Scientist.pdf") == "Data Scientist"


def test_index_resume_templates_reads_pdf_resumes_only(tmp_path):
    (tmp_path / "GAOYI_WU_Agent_Engineer.docx").write_text("docx")
    (tmp_path / "GAOYI_WU_Agent_Engineer.pdf").write_text("pdf")

    templates = index_resume_templates(tmp_path)

    assert len(templates) == 1
    assert templates[0].track == "Agent Engineer"
    assert templates[0].docx_path is None
    assert templates[0].pdf_path == Path(tmp_path / "GAOYI_WU_Agent_Engineer.pdf")


def test_resolve_original_resume_pdf_requires_source_dir_membership(tmp_path):
    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    selected = source_dir / "GAOYI_WU_SDE.pdf"
    selected.write_bytes(b"%PDF-1.4\nsource")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\noutside")

    assert resolve_original_resume_pdf(selected, source_dir=source_dir) == selected.resolve()
    try:
        resolve_original_resume_pdf(outside, source_dir=source_dir)
    except ResumePathError as exc:
        assert "must come from required resume source dir" in str(exc)
    else:
        raise AssertionError("source-dir outsider must be rejected")


def test_resolve_original_resume_pdf_rejects_package_local_generated_pdf(tmp_path):
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    generated = package_dir / "tailored-resume.pdf"
    generated.write_bytes(b"%PDF-1.4\ngenerated")

    try:
        resolve_original_resume_pdf(generated, package_dir=package_dir)
    except ResumePathError as exc:
        assert "not package-local" in str(exc)
    else:
        raise AssertionError("package-local generated resume PDF must be rejected")


def test_resolve_original_resume_pdf_requires_exact_pdf_when_specified(tmp_path):
    source_dir = tmp_path / "resumes"
    source_dir.mkdir()
    required = source_dir / "GAOYI_WU_MLE.pdf"
    required.write_bytes(b"%PDF-1.4\nrequired")
    other = source_dir / "GAOYI_WU_SDE.pdf"
    other.write_bytes(b"%PDF-1.4\nother")

    try:
        resolve_original_resume_pdf(other, source_dir=source_dir, required_pdf=required)
    except ResumePathError as exc:
        assert "does not match required path" in str(exc)
    else:
        raise AssertionError("non-required source PDF must be rejected when an exact PDF is required")


def test_extract_resume_text_reads_docx_paragraphs(tmp_path):
    docx_path = tmp_path / "GAOYI_WU_Agent_Engineer.docx"
    write_minimal_docx(
        docx_path,
        ["Gaoyi Wu", "Built LLM agents with FastAPI and LangChain."],
    )

    text = extract_resume_text(docx_path)

    assert "Gaoyi Wu" in text
    assert "FastAPI and LangChain" in text


def test_select_best_resume_template_prefers_actual_jd_evidence_over_filename_track():
    templates = [
        ResumeTemplate(track="MLE", pdf_path=Path("mle.pdf"), parsed_text="Experience with PyTorch."),
        ResumeTemplate(track="AI Algorithm Engineer", pdf_path=Path("ai.pdf"), parsed_text="Python and PyTorch model training."),
    ]

    selected = select_best_resume_template(
        templates,
        target_track="MLE",
        required_skills=["Python", "PyTorch"],
    )

    assert selected is templates[1]


def test_select_best_resume_template_uses_closest_pdf_when_track_does_not_match():
    templates = [
        ResumeTemplate(track="SDE", pdf_path=Path("backend.pdf"), parsed_text="Python FastAPI Postgres"),
        ResumeTemplate(track="MLE", pdf_path=Path("ml.pdf"), parsed_text="PyTorch MLflow"),
    ]

    selected = select_best_resume_template(
        templates,
        target_track="Agent Engineer",
        required_skills=["Python", "FastAPI"],
    )

    assert selected is templates[0]


def test_select_best_resume_template_uses_target_track_keywords_for_ml_infra():
    templates = [
        ResumeTemplate(
            track="AI Algorithm Engineer",
            pdf_path=Path("ai.pdf"),
            parsed_text="Python and PyTorch model training.",
        ),
        ResumeTemplate(
            track="ML Infra",
            pdf_path=Path("ml-infra.pdf"),
            parsed_text="Python Kubernetes MLflow infrastructure serving Docker platform.",
        ),
    ]

    selected = select_best_resume_template(
        templates,
        target_track="ML Infra",
        required_skills=["Python", "PyTorch"],
    )

    assert selected is templates[1]
