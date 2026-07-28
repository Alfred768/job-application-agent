from io import BytesIO
from zipfile import ZipFile

from job_agent.document_export import (
    convert_docx_to_pdf,
    markdown_to_docx_bytes,
)


def test_markdown_to_docx_bytes_writes_basic_word_document():
    data = markdown_to_docx_bytes(
        "# Tailored Resume Draft\n\n## Base Resume\n\nGaoyi Wu\n\nBuilt FastAPI services."
    )

    with ZipFile(BytesIO(data)) as docx:
        names = set(docx.namelist())
        document_xml = docx.read("word/document.xml").decode("utf-8")

    assert "[Content_Types].xml" in names
    assert "word/document.xml" in names
    assert "Tailored Resume Draft" in document_xml
    assert "Base Resume" in document_xml
    assert "Gaoyi Wu" in document_xml
    assert "Built FastAPI services." in document_xml


def test_markdown_to_docx_bytes_removes_markdown_formatting_artifacts():
    data = markdown_to_docx_bytes(
        "**Languages:** Python\n\n[LinkedIn](https://linkedin.example/gaoyi)"
    )

    with ZipFile(BytesIO(data)) as docx:
        document_xml = docx.read("word/document.xml").decode("utf-8")

    assert "**" not in document_xml
    assert "[LinkedIn]" not in document_xml
    assert "Languages: Python" in document_xml
    assert "LinkedIn: https://linkedin.example/gaoyi" in document_xml


def test_convert_docx_to_pdf_returns_false_without_converter(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(markdown_to_docx_bytes("Gaoyi Wu"))

    monkeypatch.setattr("job_agent.document_export.shutil.which", lambda _: None)

    assert convert_docx_to_pdf(source, tmp_path / "source.pdf") is False
