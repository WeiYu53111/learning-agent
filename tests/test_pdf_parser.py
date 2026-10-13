"""验证 PDF 文本和章节识别的核心行为。"""

from __future__ import annotations

import pymupdf as fitz
import pytest

from app.pdf_parser import PDFTextMissingError, parse_text_pdf


def test_parse_text_pdf_detects_numbered_sections(sample_pdf_bytes: bytes) -> None:
    """编号、标题层级、页码和正文应从文本 PDF 正确恢复。"""

    parsed = parse_text_pdf(sample_pdf_bytes)

    assert parsed.page_count == 2
    assert len(parsed.sections) >= 3
    assert parsed.sections[0].title == "Foundations"
    assert parsed.sections[0].level == 1
    assert parsed.sections[1].number == "1.1"
    assert "Mastery means" in parsed.sections[1].content
    assert parsed.sections[2].start_page == 2


def test_parse_text_pdf_rejects_image_only_document() -> None:
    """没有足够文本层的 PDF 应提示扫描件限制。"""

    document = fitz.open()
    document.new_page()
    data = document.tobytes()
    document.close()

    with pytest.raises(PDFTextMissingError, match="扫描件"):
        parse_text_pdf(data)

