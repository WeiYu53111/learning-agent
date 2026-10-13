"""提供测试套件共享的 PDF 样本和环境配置。"""

from __future__ import annotations

import os

import pymupdf as fitz
import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key")


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """生成包含两页和三级可识别标题的内存 PDF。"""

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Chapter 1 Foundations", fontsize=20)
    page.insert_text(
        (72, 110),
        "Learning systems store evidence about a learner. "
        "Evidence should come from more than one attempt.",
        fontsize=11,
    )
    page.insert_text((72, 155), "1.1 Mastery", fontsize=16)
    page.insert_text(
        (72, 190),
        "Mastery means the learner can explain and apply a concept without relying on hints.",
        fontsize=11,
    )
    page = document.new_page()
    page.insert_text((72, 72), "1.2 Review", fontsize=16)
    page.insert_text(
        (72, 108),
        "Delayed review checks whether knowledge remains available after time has passed.",
        fontsize=11,
    )
    data = document.tobytes()
    document.close()
    return data

