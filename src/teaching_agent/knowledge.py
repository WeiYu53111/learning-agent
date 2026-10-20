"""PDF parsing, graph and exact-quote checks, and review coverage."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf

from .models import AppError, Book, Chunk, Draft, Ref, Section
from .storage import uid


def parse_pdf(path: Path) -> Book:
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise AppError("请选择本地 PDF 文件。")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise AppError("PDF 超过第一版 20 MiB 上限。")
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF"):
        raise AppError("文件不是有效 PDF。")
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise AppError("PDF 无法解析，可能已损坏。") from exc
    with doc:
        if doc.is_encrypted or not 0 < doc.page_count <= 2000:
            raise AppError("不支持加密、空白或超过 2000 页的 PDF。")
        chunks, unavailable, headings = [], [], []
        toc = doc.get_toc()
        for page_index, page in enumerate(doc):
            text = page.get_text(sort=True).replace("\r\n", "\n").strip()
            if not text:
                unavailable.append(page_index + 1)
                continue
            # Retain every character; split by paragraph boundaries when possible.
            offset = 0
            while offset < len(text):
                end = min(offset + 1600, len(text))
                if end < len(text):
                    boundary = text.rfind("\n", offset + 800, end)
                    if boundary > offset:
                        end = boundary + 1
                part = text[offset:end]
                chunks.append(
                    Chunk(id=f"c{page_index + 1}_{offset}", page=page_index + 1, text=part)
                )
                offset = end
            for line in text.splitlines()[:8]:
                if re.match(r"^(第[一二三四五六七八九十\d]+章|Chapter\s+\d+)", line):
                    headings.append((1, line.strip(), page_index + 1))
                    break
        if not chunks:
            raise AppError("没有可读取文本；第一版不支持 OCR。")
        # Chapter selection uses top-level bookmarks; subheadings stay in source text.
        top = [
            (level, title, page)
            for level, title, page in toc
            if level == 1 and 1 <= page <= doc.page_count
        ]
        top = top or headings or [(1, "全文", 1)]
        top = sorted(top, key=lambda item: item[2])
        if top[0][2] > 1:
            top.insert(0, (1, "前言与目录", 1))
        # Same-page chapters are explicitly grouped rather than silently losing text.
        grouped = {}
        for _, title, page in top:
            grouped.setdefault(page, []).append(title)
        starts = sorted(grouped)
        sections = []
        for index, start in enumerate(starts):
            end = starts[index + 1] - 1 if index + 1 < len(starts) else doc.page_count
            sections.append(
                Section(
                    id=f"s{index + 1}",
                    title=" / ".join(grouped[start]),
                    start_page=start,
                    end_page=end,
                    chunk_ids=[c.id for c in chunks if start <= c.page <= end],
                )
            )
        return Book(
            id=uid("book"),
            filename=path.name,
            sha256=hashlib.sha256(raw).hexdigest(),
            page_count=doc.page_count,
            sections=sections,
            chunks=chunks,
            unavailable_pages=unavailable,
        )


def check_refs(refs: list[Ref], chunks: dict[str, Chunk]) -> list[str]:
    errors = []
    for ref in refs:
        chunk = chunks.get(ref.chunk_id)
        if not chunk or ref.quote not in chunk.text:
            errors.append(f"无效引用：{ref.chunk_id} / {ref.quote[:35]}")
        elif chunk.text.count(ref.quote) != 1:
            errors.append(f"引用不唯一，请提供更长原文：{ref.chunk_id}")
    return errors


def ordered_objectives(draft: Draft, selected=None):
    objectives = draft.objectives()
    included = {key: obj for key, obj in objectives.items() if not obj.excluded_reason}
    wanted = set(included) if selected is None else set(selected)
    result, visiting, done = [], set(), set()

    def visit(key):
        if key not in included:
            raise AppError(f"必要目标不存在或已排除：{key}")
        if key in visiting:
            raise AppError(f"必要前置形成循环：{key}")
        if key in done:
            return
        visiting.add(key)
        for pre in included[key].prerequisites:
            visit(pre)
        visiting.remove(key)
        done.add(key)
        result.append(key)

    for key in objectives:
        if key in wanted:
            visit(key)
    if wanted - set(objectives):
        raise AppError("选择了不存在的目标。")
    return result


def quality_report(draft: Draft, chunks: list[Chunk]) -> dict:
    errors, warnings = [], []
    chunk_map = {c.id: c for c in chunks}
    points = [p.key for p in draft.points]
    objectives = [o for p in draft.points for o in p.objectives]
    obj_map = draft.objectives()
    if len(points) != len(set(points)) or len(objectives) != len(obj_map):
        errors.append("知识点或学习目标 key 重复。")
    item_keys = [item.key for item in draft.content_items]
    if len(item_keys) != len(set(item_keys)):
        errors.append("内容清单 key 重复。")
    codes = [m.code for m in draft.misconceptions]
    if len(codes) != len(set(codes)):
        errors.append("误解码重复。")
    for m in draft.misconceptions:
        if any(key not in obj_map for key in m.objective_keys):
            errors.append(f"误解 {m.code} 指向不存在的目标。")
    try:
        order = ordered_objectives(draft)
    except AppError as exc:
        errors.append(str(exc))
        order = []
    if not any(not o.excluded_reason for o in objectives):
        errors.append("至少保留一个学习目标。")
    covered_chunks, mapped = set(), set()
    for obj in objectives:
        errors.extend(check_refs(obj.source_refs, chunk_map))
        errors.extend(check_refs(obj.sample_question.source_refs, chunk_map))
        if set(obj.sample_question.targeted_misconception_codes) - set(codes):
            errors.append(f"目标 {obj.key} 的试题引用未知误解码。")
    for item in draft.content_items:
        errors.extend(check_refs(item.source_refs, chunk_map))
        covered_chunks.update(r.chunk_id for r in item.source_refs)
        if any(key not in obj_map for key in item.objective_keys):
            errors.append(f"内容 {item.key} 指向不存在的目标。")
        active = [
            key
            for key in item.objective_keys
            if key in obj_map and not obj_map[key].excluded_reason
        ]
        mapped.update(active)
        if not active and not item.excluded_reason:
            errors.append(f"内容 {item.key} 未覆盖，需补目标或说明排除理由。")
    for chunk in chunks:
        if chunk.id not in covered_chunks:
            errors.append(f"原文片段 {chunk.id} 没有内容清单条目，不能证明已审查。")
    for obj in objectives:
        if not obj.excluded_reason and obj.key not in mapped:
            errors.append(f"目标 {obj.key} 没有关联内容清单。")
    titles = [p.title.casefold() for p in draft.points]
    if len(titles) != len(set(titles)):
        warnings.append("知识点名称重复，请检查是否应合并。")
    warnings.extend(draft.notes)
    return {
        "errors": errors,
        "warnings": warnings,
        "order": order,
        "chunk_count": len(chunks),
        "covered_chunks": len(covered_chunks & chunk_map.keys()),
        "content_items": len(draft.content_items),
    }
