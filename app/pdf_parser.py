"""从带文本层的 PDF 中提取正文并识别层级章节。"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import pymupdf as fitz


class PDFParseError(ValueError):
    """PDF 无法解析。"""


class PDFTextMissingError(PDFParseError):
    """PDF 没有足够的可提取文本。"""


@dataclass(slots=True)
class TextLine:
    """保存一行文字及其用于标题识别的排版特征。"""

    page_number: int
    text: str
    font_size: float
    is_bold: bool


@dataclass(slots=True)
class ParsedSection:
    """表示已识别的章节范围、正文和父子关系。"""

    level: int
    number: str | None
    title: str
    start_page: int
    end_page: int
    content: str
    confidence: float
    parent_index: int | None = None


@dataclass(slots=True)
class ParsedDocument:
    """汇总 PDF 页文本和解析出的章节。"""

    page_count: int
    pages: list[str]
    sections: list[ParsedSection] = field(default_factory=list)


_CHINESE_NUMBER = "一二三四五六七八九十百千万零〇两0-9"
_CHAPTER_RE = re.compile(rf"^(第\s*[{_CHINESE_NUMBER}]+\s*章)\s*[:：、.．-]?\s*(.*)$")
_SECTION_RE = re.compile(rf"^(第\s*[{_CHINESE_NUMBER}]+\s*节)\s*[:：、.．-]?\s*(.*)$")
_EN_CHAPTER_RE = re.compile(r"^(chapter\s+\d+)\s*[:：.\-]?\s*(.*)$", re.IGNORECASE)
_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+){0,3})[\s、.．:-]+(.{1,100})$")
_SENTENCE_ENDINGS = ("。", "！", "？", ".", "!", "?", ";", "；")


def _normalize(text: str) -> str:
    """移除空白并统一大小写，便于宽松匹配标题。"""

    return re.sub(r"\s+", "", text).lower()


def _number_and_level(text: str) -> tuple[str | None, int | None, str]:
    """从中英文标题编号推断章节编号、层级和纯标题。"""

    stripped = re.sub(r"\s+", " ", text).strip()
    match = _CHAPTER_RE.match(stripped) or _EN_CHAPTER_RE.match(stripped)
    if match:
        number, rest = match.groups()
        return number.replace(" ", ""), 1, (rest.strip() or stripped)

    match = _SECTION_RE.match(stripped)
    if match:
        number, rest = match.groups()
        return number.replace(" ", ""), 2, (rest.strip() or stripped)

    match = _NUMBERED_RE.match(stripped)
    if match:
        number, rest = match.groups()
        return number, min(number.count(".") + 1, 4), rest.strip()

    return None, None, stripped


def _extract_lines(document: fitz.Document) -> tuple[list[TextLine], list[str], float]:
    """逐页提取文本行，并估算正文的典型字号。"""

    all_lines: list[TextLine] = []
    page_texts: list[str] = []
    weighted_sizes: list[float] = []

    for page_index, page in enumerate(document):
        page_lines: list[str] = []
        raw = page.get_text("dict", sort=True)
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(span.get("text", "") for span in spans).strip()
                if not text:
                    continue
                size = max((float(span.get("size", 0)) for span in spans), default=0)
                fonts = " ".join(str(span.get("font", "")) for span in spans).lower()
                is_bold = "bold" in fonts or "black" in fonts
                all_lines.append(TextLine(page_index + 1, text, size, is_bold))
                page_lines.append(text)
                weighted_sizes.extend([size] * min(max(len(text), 1), 80))
        page_texts.append("\n".join(page_lines))

    body_size = statistics.median(weighted_sizes) if weighted_sizes else 10.0
    return all_lines, page_texts, body_size


def _toc_page_numbers(page_texts: list[str]) -> set[int]:
    """识别前十页中的目录页，避免将目录项当成正文标题。"""

    result: set[int] = set()
    for index, text in enumerate(page_texts[:10], start=1):
        compact = _normalize(text[:500])
        lines = text.splitlines()
        is_toc = compact.startswith("目录") or compact.startswith("contents")
        has_leaders = sum("..." in line or "……" in line for line in lines) >= 2
        if is_toc or has_leaders:
            result.add(index)
    return result


def _find_line_index(
    lines: list[TextLine], title: str, page_number: int, used: set[int]
) -> int | None:
    """在目标页附近查找与目录标题最接近且尚未使用的文本行。"""

    normalized_title = _normalize(title)
    candidates: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if index in used or abs(line.page_number - page_number) > 1:
            continue
        normalized_line = _normalize(line.text)
        if normalized_title == normalized_line:
            return index
        if normalized_title in normalized_line or normalized_line in normalized_title:
            candidates.append((abs(len(normalized_line) - len(normalized_title)), index))
    return min(candidates)[1] if candidates else None


def _outline_candidates(
    document: fitz.Document, lines: list[TextLine]
) -> list[tuple[int, int, str | None, str, float]]:
    """优先根据 PDF 内置目录生成高置信度章节候选。"""

    outline = document.get_toc(simple=True)
    if not outline:
        return []

    used: set[int] = set()
    candidates: list[tuple[int, int, str | None, str, float]] = []
    for raw_level, raw_title, raw_page in outline:
        if not raw_title.strip() or raw_page < 1:
            continue
        index = _find_line_index(lines, raw_title, raw_page, used)
        if index is None:
            index = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if line.page_number >= raw_page and i not in used
                ),
                None,
            )
        if index is None:
            continue
        used.add(index)
        number, detected_level, clean_title = _number_and_level(raw_title)
        level = max(1, min(int(raw_level or detected_level or 1), 4))
        candidates.append((index, level, number, clean_title, 0.98))
    return sorted(candidates)


def _detected_candidates(
    lines: list[TextLine], page_texts: list[str], body_size: float
) -> list[tuple[int, int, str | None, str, float]]:
    """根据编号、字号和粗体特征从正文中推断章节候选。"""

    toc_pages = _toc_page_numbers(page_texts)
    candidates: list[tuple[int, int, str | None, str, float]] = []

    for index, line in enumerate(lines):
        if line.page_number in toc_pages:
            continue
        number, level, clean_title = _number_and_level(line.text)
        if level is not None:
            candidates.append((index, level, number, clean_title, 0.92))
            continue

        short_title = 2 <= len(line.text) <= 60 and not line.text.endswith(_SENTENCE_ENDINGS)
        visually_distinct = line.font_size >= body_size * 1.25 or (
            line.is_bold and line.font_size >= body_size * 1.08
        )
        if short_title and visually_distinct:
            level = 1 if line.font_size >= body_size * 1.6 else 2
            candidates.append((index, level, None, clean_title, 0.68))

    # 同一行只保留一个候选，并限制异常密集的误识别。
    unique = {candidate[0]: candidate for candidate in candidates}
    result = sorted(unique.values())
    if len(result) > max(80, len(page_texts) * 8):
        result = [candidate for candidate in result if candidate[2] is not None]
    return result


def _build_sections(
    lines: list[TextLine], candidates: list[tuple[int, int, str | None, str, float]]
) -> list[ParsedSection]:
    """按候选标题边界切分正文，并通过栈恢复章节父子关系。"""

    if not lines:
        return []
    if not candidates:
        return [
            ParsedSection(
                level=1,
                number=None,
                title="全文",
                start_page=1,
                end_page=lines[-1].page_number,
                content="\n".join(line.text for line in lines),
                confidence=1.0,
            )
        ]

    sections: list[ParsedSection] = []
    stack: list[tuple[int, int]] = []
    for position, (line_index, level, number, title, confidence) in enumerate(candidates):
        end_index = len(lines)
        for next_candidate in candidates[position + 1 :]:
            if next_candidate[1] <= level:
                end_index = next_candidate[0]
                break
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_index = stack[-1][1] if stack else None
        content_lines = lines[line_index:end_index]
        content = "\n".join(line.text for line in content_lines).strip()
        if not content:
            continue
        section = ParsedSection(
            level=level,
            number=number,
            title=title,
            start_page=lines[line_index].page_number,
            end_page=content_lines[-1].page_number,
            content=content,
            confidence=confidence,
            parent_index=parent_index,
        )
        sections.append(section)
        stack.append((level, len(sections) - 1))
    return sections


def parse_text_pdf(data: bytes) -> ParsedDocument:
    """解析 PDF 字节；无有效文本层时抛出明确的业务异常。"""

    if not data.startswith(b"%PDF"):
        raise PDFParseError("上传的文件不是有效的 PDF。")
    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # pragma: no cover - PyMuPDF 异常类型较多
        raise PDFParseError("PDF 已损坏或无法读取。") from exc

    try:
        if document.needs_pass:
            raise PDFParseError("暂不支持加密 PDF。")
        if document.page_count == 0:
            raise PDFParseError("PDF 不包含页面。")

        lines, page_texts, body_size = _extract_lines(document)
        non_whitespace_chars = sum(len(re.sub(r"\s+", "", text)) for text in page_texts)
        text_pages = sum(bool(text.strip()) for text in page_texts)
        if non_whitespace_chars < 80 or text_pages / document.page_count < 0.3:
            raise PDFTextMissingError(
                "该 PDF 没有足够的可提取文本，可能是扫描件；第一版暂不支持 OCR。"
            )

        candidates = _outline_candidates(document, lines)
        if not candidates:
            candidates = _detected_candidates(lines, page_texts, body_size)
        sections = _build_sections(lines, candidates)
        return ParsedDocument(document.page_count, page_texts, sections)
    finally:
        document.close()

