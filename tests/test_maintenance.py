import json
import sqlite3

import pytest
from test_book_learning import book_id as book_id

from teaching_agent.maintenance import purge_legacy
from teaching_agent.models import AppError


def legacy(app, book_id):
    index = app.library.index(book_id, content_only=True)
    with app.store.connect() as conn:
        data = json.loads(index["data"])
        data.pop("pipeline")
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (json.dumps(data), index["id"]))
    return index["id"]


def test_cleanup_backup_preserves_books_and_current_index(app, book_id):
    old = legacy(app, book_id)
    current = app.library.index(book_id, new=True, content_only=True)["id"]
    before = app.store.rows("SELECT * FROM books")
    assert purge_legacy(app.store.root, book_id)["indexes"] == [old]
    assert len(app.store.rows("SELECT * FROM book_indexes")) == 2
    report = purge_legacy(app.store.root, book_id, apply=True)
    assert [r["id"] for r in app.store.rows("SELECT * FROM book_indexes")] == [current]
    assert [dict(r) for r in before] == [dict(r) for r in app.store.rows("SELECT * FROM books")]
    with sqlite3.connect(report["backup"]) as conn:
        assert conn.execute("SELECT count(*) FROM book_indexes").fetchone()[0] == 2
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_cleanup_refuses_course_dependency(app, book_id):
    app.library.index(book_id)
    app.library.learn(book_id, "s1")
    with app.store.connect() as conn:
        row = conn.execute("SELECT * FROM book_indexes").fetchone()
        data = json.loads(row["data"])
        data.pop("pipeline")
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (json.dumps(data), row["id"]))
    with pytest.raises(AppError, match="课程引用"):
        purge_legacy(app.store.root, book_id, apply=True)
    assert app.store.rows("SELECT * FROM book_indexes")
