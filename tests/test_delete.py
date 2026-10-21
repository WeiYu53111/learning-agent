import sqlite3
from pathlib import Path

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


@pytest.mark.parametrize("missing_pdf", [False, True])
def test_delete_book_removes_record_and_copy(app, draft_id, tmp_path, capsys, missing_pdf):
    book_id = app.store.get("versions", draft_id)["book_id"]
    book = app.book(book_id)
    document = app.store.root / "documents" / f"{book.sha256}.pdf"
    app.delete_draft(draft_id)
    if missing_pdf:
        document.unlink()
    with app.store.connect() as conn:
        app.store.event(conn, book_id, "test", {})
        conn.execute("INSERT INTO backend_bindings VALUES(?,?,?)", (book_id, "demo", "{}"))
    assert main(["--data-dir", str(app.store.root), "delete-book", book_id]) == 0
    assert f"已删除教材：{book_id}" in capsys.readouterr().out
    assert not document.exists()
    assert (tmp_path / "教材.pdf").exists()
    assert all(not rows for rows in snapshot(app).values())
    # The original file can be imported again after deletion.
    assert app.import_pdf(tmp_path / "教材.pdf", demo=True).sha256 == book.sha256


@pytest.mark.parametrize("publish", [False, True])
def test_delete_book_rejects_referenced_book(app, draft_id, publish, capsys):
    if publish:
        app.audit(draft_id)
        app.approve(draft_id)
        app.publish(draft_id)
    book_id = app.store.get("versions", draft_id)["book_id"]
    before = snapshot(app)
    assert main(["--data-dir", str(app.store.root), "delete-book", book_id]) == 1
    assert draft_id in capsys.readouterr().err
    assert snapshot(app) == before
    assert list((app.store.root / "documents").glob("*.pdf"))


def test_delete_book_unknown_id(app, draft_id):
    before = snapshot(app)
    with pytest.raises(AppError, match="teach books"):
        app.delete_book("book_missing")
    assert snapshot(app) == before


def test_delete_book_database_failure_preserves_pdf(app, draft_id):
    book_id = app.store.get("versions", draft_id)["book_id"]
    app.delete_draft(draft_id)
    before = snapshot(app)
    with app.store.connect() as conn:
        conn.execute(
            "CREATE TRIGGER reject_book_delete BEFORE DELETE ON books "
            "BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        app.delete_book(book_id)
    assert snapshot(app) == before
    assert list((app.store.root / "documents").glob("*.pdf"))


def test_delete_book_reports_leftover_pdf(app, draft_id, monkeypatch):
    book_id = app.store.get("versions", draft_id)["book_id"]
    app.delete_draft(draft_id)

    def fail_unlink(self, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(AppError, match="教材记录已删除，但 PDF 副本清理失败"):
        app.delete_book(book_id)
    assert app.store.rows("SELECT * FROM books") == []
    assert list((app.store.root / "documents").glob("*.pdf"))
