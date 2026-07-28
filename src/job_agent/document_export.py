from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


def _markdown_line_to_text(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("#"):
        return stripped.lstrip("#").strip()
    if stripped.startswith(">"):
        return stripped.lstrip(">").strip()
    if stripped.startswith("- "):
        stripped = stripped[2:].strip()
    stripped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: f"{match.group(1)}: {match.group(2)}",
        stripped,
    )
    return stripped.replace("**", "").replace("__", "").replace("`", "")


def _paragraph_xml(text: str) -> str:
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def markdown_to_docx_bytes(markdown_text: str) -> bytes:
    paragraphs = [
        _markdown_line_to_text(line)
        for line in markdown_text.splitlines()
        if _markdown_line_to_text(line)
    ]
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(_paragraph_xml(paragraph) for paragraph in paragraphs)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="720" w:right="720" w:bottom="720" w:left="720"/></w:sectPr>'
        + "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", relationships)
        docx.writestr("word/document.xml", document_xml)
    return output.getvalue()


def convert_docx_to_pdf(docx_path: str | Path, pdf_path: str | Path) -> bool:
    """Convert a DOCX document to PDF using a local office converter.

    Returns False when no supported converter is installed or the conversion
    fails, so callers can decide whether to fall back to the DOCX artifact.
    """
    source = Path(docx_path)
    target = Path(pdf_path)
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter or not source.exists():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        result = subprocess.run(
            [
                converter,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(source),
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        produced = output_dir / f"{source.stem}.pdf"
        if result.returncode != 0 or not produced.exists():
            return False
        shutil.move(str(produced), target)
    return target.exists() and target.stat().st_size > 0
