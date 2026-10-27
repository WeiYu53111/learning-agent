"""Explicit, backed-up removal of unused legacy knowledge indexes."""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .extraction import is_current
from .models import AppError
from .storage import Store, writer_lock


def purge_legacy(root: Path, book_id: str, *, apply=False):
    store = Store(root)
    with writer_lock(root), store.connect() as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 6:
            raise AppError("请先升级至 schema 6，再清理旧知识。")
        rows = conn.execute("SELECT * FROM book_indexes WHERE book_id=?", (book_id,)).fetchall()
        targets = [r for r in rows if not is_current(r)]
        ids = {r["id"] for r in targets}
        old_keys = {p["key"] for r in targets for p in json.loads(r["data"]).get("points", [])}
        retained = [r for r in rows if r["id"] not in ids]
        old_keys -= {p["key"] for r in retained for p in json.loads(r["data"]).get("points", [])}
        for row in retained:
            if json.loads(row["data"]).get("base_index") in ids:
                raise AppError("新版索引引用了旧索引，停止清理。")
        for identifier in ids:
            for table in ("lesson_links", "lesson_relation_snapshots"):
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE index_id=?", (identifier,)
                ).fetchone():
                    raise AppError("旧索引已有课程引用，停止清理，请决定如何处理课程。")
        report = dict(book_id=book_id, indexes=sorted(ids), points=len(old_keys), applied=False)
        if not apply or not ids:
            return report
        backup = root / "backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") / "learning.db"
        backup.parent.mkdir(parents=True, exist_ok=False)
        with sqlite3.connect(backup) as destination:
            conn.backup(destination)
            assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.execute("BEGIN IMMEDIATE")
        for identifier in ids:
            for table in ("book_steps", "backend_bindings", "events"):
                conn.execute(f"DELETE FROM {table} WHERE owner_id=?", (identifier,))
            conn.execute("DELETE FROM book_indexes WHERE id=?", (identifier,))
        for key in old_keys:
            conn.execute("DELETE FROM self_mastery WHERE book_id=? AND point_key=?", (book_id, key))
        for row in conn.execute(
            "SELECT id,left_key,right_key FROM relation_checks WHERE book_id=?", (book_id,)
        ).fetchall():
            if row["left_key"] in old_keys or row["right_key"] in old_keys:
                conn.execute(
                    "DELETE FROM relation_checks WHERE id=? AND NOT EXISTS "
                    "(SELECT 1 FROM relation_task_pairs WHERE check_id=?) AND NOT EXISTS "
                    "(SELECT 1 FROM relation_graph_edges WHERE check_id=?)",
                    (row["id"],) * 3,
                )
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise AppError("清理后的外键检查失败，已回滚。")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AppError("清理后的完整性检查失败，已回滚。")
        report.update(applied=True, backup=str(backup))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--book", required=True)
    parser.add_argument("--apply", action="store_true", help="备份后执行；默认只预览")
    args = parser.parse_args()
    print(
        json.dumps(
            purge_legacy(args.data_dir, args.book, apply=args.apply), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
