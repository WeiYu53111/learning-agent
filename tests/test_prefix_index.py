"""Sequential reading can start before later chapters are indexed."""

import json

import pytest
from test_book_learning import book_id as book_id

from teaching_agent.models import AppError


def track(app):
    original = app.injected_gateway.book_operation
    calls = []

    def operation(op, payload, contract):
        calls.append((op, payload))
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    return calls


def test_first_prefix_then_extend_and_full_reuses_reviewed_content(app, book_id):
    calls = track(app)
    first = app.library.index(book_id, section_id="s2", content_only=True)
    data = json.loads(first["data"])
    assert data["indexed_sections"] == ["s1", "s2"]
    assert {p["section_id"] for p in data["points"]} == {"s1", "s2"}
    count = len(calls)
    assert app.library.index(book_id, section_id="s2", content_only=True)["id"] == first["id"]
    assert len(calls) == count
    with pytest.raises(AppError, match="前序章节内容尚未就绪"):
        app.library.learn(book_id, "s3")
    app.library.learn(book_id, "s2")
    calls.clear()
    second = app.library.index(book_id, content_only=True)
    updated = json.loads(second["data"])
    assert updated["indexed_sections"] == ["s1", "s2", "s3"]
    assert [p for p in updated["points"] if p["section_id"] != "s3"] == data["points"]
    extracts = [p for op, p in calls if op == "book_index"]
    assert len(extracts) == 1
    assert {c["id"] for c in extracts[0]["chunks"]} == {"c3"}


def test_new_target_preserves_previous_chapter_and_later_saved_content(app, book_id):
    first = app.library.index(book_id, content_only=True)
    calls = track(app)
    rebuilt = app.library.index(book_id, section_id="s2", new=True, content_only=True)
    old = json.loads(first["data"])["points"]
    new = json.loads(rebuilt["data"])["points"]
    assert [p for p in old if p["section_id"] != "s2"] == [
        p for p in new if p["section_id"] != "s2"
    ]
    assert len([1 for op, _ in calls if op == "book_index"]) == 1


def test_narrow_interrupted_full_run_reuses_completed_chapter(app, book_id):
    original = app.injected_gateway.book_operation

    def fail(op, payload, contract):
        if op == "book_index" and payload["chunks"][0]["id"] == "c2":
            raise AppError("模拟中断")
        return original(op, payload, contract)

    app.injected_gateway.book_operation = fail
    with pytest.raises(AppError):
        app.library.index(book_id, content_only=True)
    app.injected_gateway.book_operation = original
    calls = track(app)
    first = app.library.index(book_id, section_id="s1", content_only=True)
    assert json.loads(first["data"])["indexed_sections"] == ["s1"]
    assert calls == []


def test_prefix_relations_and_lesson_never_use_later_chapter(app, book_id):
    app.library.index(book_id, content_only=True)
    calls = track(app)
    app.library.learn(book_id, "s2")
    requests = [p for op, p in calls if op == "book_relations"]
    assert requests
    assert all({p["section_id"] for p in req["points"]} <= {"s1", "s2"} for req in requests)
    snapshot = json.loads(app.store.rows("SELECT data FROM lesson_relation_snapshots")[0]["data"])
    assert all(p["section_id"] != "s3" for p in snapshot["prerequisite_context"])


def test_web_marks_future_chapter_pending(app, book_id):
    from fastapi.testclient import TestClient

    from teaching_agent.web import create_app

    app.library.index(book_id, section_id="s1", content_only=True)
    with TestClient(create_app(app.store)) as client:
        catalog = client.get(f"/api/books/{book_id}").json()
        assert catalog["indexes"][0]["indexed_sections"] == ["s1"]
        current = client.get(f"/api/books/{book_id}/sections/s1").json()
        future = client.get(f"/api/books/{book_id}/sections/s2").json()
        assert current["quality"] == "reviewed"
        assert future["quality"] == "pending"
        assert future["points"] == []
