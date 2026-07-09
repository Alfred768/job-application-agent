from io import BytesIO
from zipfile import ZipFile

from job_agent.document_export import markdown_to_docx_bytes


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
