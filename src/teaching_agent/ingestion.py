"""Format adapters produce immutable, source-located text for knowledge extraction."""
import hashlib
import re
from pathlib import Path
from typing import Protocol

from .knowledge import parse_pdf
from .models import AppError, Book, Chunk, Section
from .storage import uid

FORMATS = {".pdf": "pdf", ".txt": "text", ".md": "markdown"}
MAX_BYTES = 20 * 1024 * 1024


class SourceAdapter(Protocol):
    def __call__(self, path: Path) -> Book: ...


def parse_text(path: Path) -> Book:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
    except UnicodeError as exc:
        raise AppError("文本编码无法识别，请转换为 UTF-8。") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or "\x00" in text:
        raise AppError("文件为空或不是有效文本。")
    lines = text.splitlines(keepends=True)
    headings, fence, offset = [], None, 0
    boundaries = []
    for i, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        elif fence is None and path.suffix.lower() == ".md":
            heading = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading:
                headings.append((len(heading[1]), offset, heading[2]))
            elif i + 1 < len(lines) and line.strip() and re.fullmatch(r" {0,3}(=+|-+)\s*", lines[i + 1]):
                headings.append((1 if lines[i + 1].lstrip().startswith("=") else 2, offset, line.strip()))
        if fence is None and not line.strip():
            boundaries.append(offset + len(line))
        offset += len(line)
    level = min((h[0] for h in headings), default=0)
    starts = [(pos, title) for depth, pos, title in headings if depth == level]
    if not starts or starts[0][0] > 0:
        starts.insert(0, (0, "全文" if not starts else "引言"))
    chunks, sections = [], []
    for n, (start, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(text)
        ids = []
        while start < end:
            stop = min(start + 3200, end)
            if stop < end:
                candidates = [b for b in boundaries if start < b <= stop]
                if candidates:
                    stop = candidates[-1]
            identifier = f"text_{start}_{stop}"
            chunks.append(Chunk(id=identifier, text=text[start:stop], location={"kind": "text", "start_char": start, "end_char": stop, "start_line": text.count("\n", 0, start) + 1, "end_line": text.count("\n", 0, stop - 1) + 1}))
            ids.append(identifier)
            start = stop
        sections.append(Section(id=f"s{n + 1}", title=title, chunk_ids=ids))
    digest = hashlib.sha256(raw).hexdigest()
    return Book(id=uid("book"), filename=path.name, sha256=digest, source_type=FORMATS[path.suffix.lower()], source_version=f"text-v1:{digest}", sections=sections, chunks=chunks)


def parse_source(path: Path) -> Book:
    if not path.is_file() or path.suffix.lower() not in FORMATS:
        raise AppError("请选择 PDF、TXT 或 Markdown 文件。")
    if path.stat().st_size > MAX_BYTES:
        raise AppError("文件不能超过 20 MiB。")
    book = parse_pdf(path) if path.suffix.lower() == ".pdf" else parse_text(path)
    if book.source_type == "pdf":
        book.source_version = f"pdf-v1:{book.sha256}"
        for chunk in book.chunks:
            chunk.location = {"kind": "pdf", "page": chunk.page}
    return book
