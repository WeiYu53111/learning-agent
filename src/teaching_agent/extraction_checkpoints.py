"""Read-only block previews, never used as published learning content."""

import json
import re

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
            result.append(
                dict(
                    chapter=match[1],
                    block=int(match[2]) + 1,
                    quality="block_reviewed",
                    points=value["points"],
                )
            )
    preview = task.get("preview") or {}
    if preview.get("points"):
        result.append(
            dict(
                chapter=task.get("chapter"),
                block=task.get("block"),
                quality="pending",
                points=preview["points"],
            )
        )
    return sorted(result, key=lambda v: (v["chapter"] or "", v["block"] or 0))
