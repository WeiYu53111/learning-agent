"""Read-only block previews, never used as published learning content."""

import json
import re
from datetime import UTC, datetime

from .extraction_models import PIPELINE


def previews(store, task):
    owner = task.get("index")
    if not owner:
        return []
    result = []
    for row in store.rows(
        "SELECT step,data FROM book_steps WHERE owner_id=? ORDER BY step", (owner,)
    ):
        match = re.fullmatch(r"(.+):" + re.escape(PIPELINE) + r":block:(\d+):complete", row["step"])
        if match:
            value = json.loads(row["data"])
            regions = value.get("regions", [])
            excluded = bool(regions) and all(r.get("excluded") for r in regions)
            result.append(
                dict(
                    chapter=match[1],
                    block=int(match[2]) + 1,
                    quality="excluded" if excluded and not value["points"] else "block_reviewed",
                    points=value["points"],
                    items=value.get("items", []),
                    exclusions=[r for r in regions if r.get("excluded")],
                    block_end=value.get("block_end", int(match[2]) + 1),
                    batch_chunk_ids=value.get("batch_chunk_ids", []),
                    regions=regions,
                )
            )
    preview = task.get("preview") or {}
    if preview.get("points") and not any(
        b["chapter"] == task.get("chapter") and b["block"] == task.get("block") for b in result
    ):
        result.append(
            dict(
                chapter=task.get("chapter"),
                block=task.get("block"),
                quality="pending",
                points=preview["points"],
                items=preview.get("items", []),
                block_end=task.get("block_end") or task.get("block"),
                batch_chunk_ids=task.get("batch_chunk_ids", []),
            )
        )
    expanded = []
    for value in result:
        ids = value.get("batch_chunk_ids", [])
        if not ids:
            expanded.append(value)
            continue
        for offset, chunk_id in enumerate(ids):
            points = [
                p
                for p in value["points"]
                if any(r["chunk_id"] == chunk_id for r in p["source_refs"])
            ]
            keys = {p["key"] for p in points}
            regions = [r for r in value.get("regions", []) if r["chunk_id"] == chunk_id]
            excluded = bool(regions) and all(r["excluded"] for r in regions)
            expanded.append(
                {
                    **value,
                    "block": value["block"] + offset,
                    "block_start": value["block"],
                    "points": points,
                    "items": [i for i in value["items"] if keys.intersection(i["point_keys"])],
                    "exclusions": [r for r in regions if r["excluded"]],
                    "quality": "excluded" if excluded and not points else value["quality"],
                }
            )
    return sorted(expanded, key=lambda v: (v["chapter"] or "", v["block"] or 0))


def detail(store, task, book, chapter=None, block=None):
    """Project saved records, not an immutable historical task snapshot."""
    blocks = previews(store, task)
    available = {(b["chapter"], b["block"]): b for b in blocks}
    if task.get("chapter") and task.get("block"):
        current = (task["chapter"], task["block"])
        available.setdefault(
            current,
            dict(
                chapter=current[0],
                block=current[1],
                points=[],
                items=[],
                quality="processing" if task["status"] in {"running", "stopping"} else "paused",
            ),
        )
        for number in range(task["block"], (task.get("block_end") or task["block"]) + 1):
            available.setdefault((task["chapter"], number), {**available[current], "block": number})
    if task.get("index"):
        for row in store.rows(
            "SELECT step,data FROM book_steps WHERE owner_id=? AND step LIKE '%:timing'",
            (task["index"],),
        ):
            match = re.fullmatch(
                r"(.+):" + re.escape(PIPELINE) + r":block:(\d+):timing", row["step"]
            )
            if not match:
                continue
            key = (match[1], int(match[2]) + 1)
            timing = json.loads(row["data"])
            timing["running"] = (
                key == (task.get("chapter"), task.get("block"))
                and task["status"] in {"running", "stopping"}
                and timing.get("finished_at") is None
            )
            if timing["running"]:
                timing["elapsed_seconds"] = max(
                    0,
                    (
                        datetime.now(UTC) - datetime.fromisoformat(timing["started_at"])
                    ).total_seconds(),
                )
            for number in range(key[1], timing.get("block_end", key[1]) + 1):
                if (key[0], number) in available:
                    available[(key[0], number)]["timing"] = timing
    sections = []
    for section in book.sections:
        sections.append(
            dict(
                id=section.id,
                title=section.title,
                blocks=[
                    dict(
                        number=i + 1,
                        chunk_id=chunk_id,
                        quality=available.get((section.id, i + 1), {}).get("quality", "waiting"),
                        count=len(available.get((section.id, i + 1), {}).get("points", [])),
                        timing=available.get((section.id, i + 1), {}).get("timing"),
                        excluded_roles=list(
                            dict.fromkeys(
                                r["role"]
                                for r in available.get((section.id, i + 1), {}).get(
                                    "exclusions", []
                                )
                            )
                        ),
                    )
                    for i, chunk_id in enumerate(section.chunk_ids)
                ],
            )
        )
    chapter = chapter or task.get("chapter") or (book.sections[0].id if book.sections else None)
    section = next((s for s in book.sections if s.id == chapter), None)
    block = (
        block
        if block is not None
        else min(task.get("block") or 1, max(1, len(section.chunk_ids) if section else 1))
    )
    selected = available.get(
        (chapter, block), dict(chapter=chapter, block=block, points=[], items=[], quality="waiting")
    )
    if section is None or block < 1 or (section.chunk_ids and block > len(section.chunk_ids)):
        from .models import AppError

        raise AppError("文本块不存在。")
    chunk_id = section.chunk_ids[block - 1] if section.chunk_ids else None
    refs = {
        r["chunk_id"] for p in selected["points"] + selected["items"] for r in p["source_refs"]
    } | {chunk_id}
    selected = {**selected, "chunks": [c.model_dump() for c in book.chunks if c.id in refs]}
    final = None
    if task.get("index"):
        rows = store.rows(
            "SELECT data FROM book_steps WHERE owner_id=? AND step=?",
            (task["index"], f"{chapter}:{PIPELINE}:complete"),
        )
        if rows:
            final = json.loads(rows[0]["data"])
        else:
            rows = store.rows("SELECT data FROM book_indexes WHERE id=?", (task["index"],))
            if rows:
                data = json.loads(rows[0]["data"])
                points = [p for p in data.get("points", []) if p["section_id"] == chapter]
                if points:
                    keys = {p["key"] for p in points}
                    final = dict(
                        points=points,
                        items=[
                            i for i in data.get("items", []) if keys.intersection(i["point_keys"])
                        ],
                    )
    if final is not None:
        final["chunks"] = [c.model_dump() for c in book.chunks if c.id in section.chunk_ids]
    return dict(
        task={k: v for k, v in task.items() if k not in {"preview", "preview_parts"}},
        sections=sections,
        selected=selected,
        final=final,
        legacy=not blocks and final is not None,
    )
