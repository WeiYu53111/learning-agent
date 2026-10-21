import sqlite3

import pytest

from teaching_agent.cli import main
from teaching_agent.models import AppError


def snapshot(app):
    return {
        table: app.store.rows(f"SELECT * FROM {table}")
        for table in ("books", "versions", "draft_batches", "events", "backend_bindings")
    }


@pytest.mark.parametrize("built", [False, True])
def test_delete_cleans_only_target_draft(app, draft_id, built, capsys):
    book_id = app.store.get("versions", draft_id)["book_id"]
    target = app.create_draft(book_id, "s1")
    if built:
        app.build_draft(target)
    with app.store.connect() as conn:
        conn.execute("INSERT INTO backend_bindings VALUES(?,?,?)", (target, "demo", "{}"))
        app.store.event(conn, target, "test", {})
    before = snapshot(app)
    documents = list((app.store.root / "documents").glob("*.pdf"))

    assert main(["--data-dir", str(app.store.root), "delete", target]) == 0
    assert f"已删除草稿：{target}" in capsys.readouterr().out
    after = snapshot(app)
    for table, key in (
        ("versions", "id"),
        ("draft_batches", "version_id"),
        ("events", "owner_id"),
        ("backend_bindings", "owner_id"),
    ):
        assert after[table] == [row for row in before[table] if row[key] != target]
    assert after["books"] == before["books"]
    assert documents and all(path.exists() for path in documents)
    assert app.store.rows("PRAGMA foreign_key_check") == []


def test_delete_rejects_published(app, published, capsys):
    before = snapshot(app)
    assert main(["--data-dir", str(app.store.root), "delete", published]) == 1
    assert "不能删除已发布版本" in capsys.readouterr().err
    assert snapshot(app) == before


def test_delete_unknown_id_changes_nothing(app, draft_id):
    before = snapshot(app)
    with pytest.raises(AppError, match="找不到"):
        app.delete_draft("ver_missing")
    assert snapshot(app) == before


def test_delete_failure_rolls_back_related_data(app, draft_id):
    with app.store.connect() as conn:
        conn.execute("INSERT INTO backend_bindings VALUES(?,?,?)", (draft_id, "demo", "{}"))
        conn.execute(
            "CREATE TRIGGER reject_delete BEFORE DELETE ON versions "
            "BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    before = snapshot(app)
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        app.delete_draft(draft_id)
    assert snapshot(app) == before
