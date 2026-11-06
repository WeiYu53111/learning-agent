import json
import time

from test_semantic_extraction import classified_gateway, save_book

from teaching_agent.extraction_checkpoints import detail, previews
from teaching_agent.extraction_tasks import Tasks, launch


def test_stop_second_batch_restarts_batch_not_completed_blocks(app):
    book = save_book(app, [f"正文知识{i}" for i in range(30)])
    classified_gateway(app, {})
    original = app.injected_gateway.book_operation
    calls = []
    stopped = False

    def operation(op, payload, contract):
        nonlocal stopped
        value = original(op, payload, contract)
        if op == "book_index":
            chunk = payload["chunks"][0]["id"]
            calls.append(chunk)
            if chunk == "c3" and not stopped:
                stopped = True
                task = Tasks(app.store.root).list(book.id)[0]
                timing = detail(app.store, task, book)["selected"]["timing"]
                assert timing["running"] is True
                assert timing["elapsed_seconds"] >= 0
                Tasks(app.store.root).stop(task["id"])
        return value

    app.injected_gateway.book_operation = operation
    assert app.library.index(book.id, content_only=True) is None
    task = Tasks(app.store.root).list(book.id)[0]
    assert task["status"] == "stopped"
    assert task["block"] == 4
    assert task["block_end"] == 6
    timing = detail(app.store, task, book)["selected"]["timing"]
    assert timing["running"] is False
    assert timing["finished_at"]
    assert detail(app.store, task, book)["selected"]["timing"] == timing
    assert calls == ["c0", "c3"]
    visible = previews(app.store, task)
    assert [v["quality"] for v in visible] == ["block_reviewed"] * 3 + ["pending"] * 3
    old_index = task["index"]
    result = app.library.index(book.id, content_only=True)
    assert result["id"] == old_index
    assert calls == ["c0", "c3", *[f"c{i}" for i in range(3, 30, 3)]]
    data = json.loads(result["data"])
    assert len({p["key"] for p in data["points"]}) == len(data["points"])
    assert len(data["items"]) == 30


def test_stop_is_idempotent_and_terminal_task_unchanged(tmp_path):
    tasks = Tasks(tmp_path)
    task = tasks.create("book")
    tasks.update(task, status="running", block=2)
    assert tasks.stop(task)["status"] == "stopping"
    assert tasks.stop(task)["block"] == 2
    tasks.update(task, status="completed")
    assert tasks.stop(task)["status"] == "completed"


def test_adjacent_pages_share_model_calls_and_preserve_refs(app):
    book = save_book(app, ["栈的定义", "栈的性质", "栈的例子"])
    calls, _ = classified_gateway(app, {})
    row = app.library.index(book.id, content_only=True)
    extracts = [payload for op, payload in calls if op == "book_index"]
    assert len(extracts) == 1
    assert [c["page"] for c in extracts[0]["chunks"]] == [1, 2, 3]
    data = json.loads(row["data"])
    assert {r["chunk_id"] for p in data["points"] for r in p["source_refs"]} == {"c0", "c1", "c2"}
    task = Tasks(app.store.root).list(book.id)[0]
    for number in (1, 2, 3):
        view = detail(app.store, task, book, "s0", number)
        assert view["selected"]["points"]
        assert view["selected"]["timing"]["block_end"] == 3


def test_pdf_chunks_are_larger_but_keep_pages(tmp_path):
    import pymupdf

    from teaching_agent.knowledge import parse_pdf

    path = tmp_path / "large.pdf"
    with pymupdf.open() as doc:
        for _ in range(2):
            page = doc.new_page(width=1000, height=2000)
            page.insert_text((20, 30), ("abcdefghij " * 8 + "\n") * 100, fontsize=8)
        doc.save(path)
    book = parse_pdf(path)
    assert max(len(c.text) for c in book.chunks) > 1600
    assert all(len(c.text) <= 3200 for c in book.chunks)
    assert {c.page for c in book.chunks} == {1, 2}


def test_full_size_blocks_fit_one_batch_under_default_budget(app):
    import pytest
    from teaching_agent.models import AppError
    book = save_book(app, ["a" * 3200, "b" * 3200, "c" * 3200])
    def fail(*args):
        raise AppError("inspect plan")
    app.injected_gateway.book_operation = fail
    with pytest.raises(AppError, match="inspect plan"):
        app.library.index(book.id, content_only=True)
    task = Tasks(app.store.root).list(book.id)[0]
    plan = app.library.step(task["index"], "s0:semantic-v1:model-batches:v1")
    assert plan["batches"] == [{"start": 0, "end": 2}, {"start": 2, "end": 3}]


def test_dead_worker_is_recoverable(tmp_path):
    tasks = Tasks(tmp_path)
    task = tasks.create("book")
    tasks.update(task, status="running", pid=2147483647, block=2)
    assert tasks.list("book")[0]["status"] == "interrupted"


def test_task_cleanup_keeps_other_books(tmp_path):
    tasks = Tasks(tmp_path)
    tasks.create("deleted")
    other = tasks.create("other")
    tasks.delete_book("deleted")
    assert tasks.list("deleted") == []
    assert tasks.list("other")[0]["id"] == other


def test_preview_accumulates_and_replaces_batches(tmp_path):
    tasks = Tasks(tmp_path)
    task = tasks.create("book")

    def nav(title):
        return dict(points=[dict(key="p", title=title)], items=[dict(key="i", point_keys=["p"])])

    tasks.update(task, preview=nav("first"), preview_part="0")
    tasks.update(task, preview=nav("second"), preview_part="1")
    value = tasks.update(task, preview=nav("corrected"), preview_part="0")
    assert [p["title"] for p in value["preview"]["points"]] == ["corrected", "second"]
    assert value["preview"]["items"][1]["point_keys"] == ["batch1_p"]
    assert tasks.update(task, preview=None)["preview_parts"] == {}


def test_detail_keeps_completed_content_and_source(app):
    book = save_book(app, ["栈是后进先出结构。"])
    classified_gateway(app, {})
    app.library.index(book.id, content_only=True)
    task = Tasks(app.store.root).list(book.id)[0]
    data = detail(app.store, task, book)
    assert data["selected"]["points"]
    assert data["selected"]["items"]
    assert data["selected"]["chunks"][0]["text"] == book.chunks[0].text
    assert data["final"]["points"]
    assert data["task"]["status"] == "completed"
    assert data["selected"]["timing"]["elapsed_seconds"] >= 0
    assert data["selected"]["timing"]["finished_at"]
    assert data["selected"]["timing"]["running"] is False
    assert "preview" not in data["task"]


def test_detail_explains_excluded_blocks(app):
    book = save_book(app, ["封面书名", "目录条目"])
    classified_gateway(app, {"封面书名": "publication", "目录条目": "navigation"})
    app.library.index(book.id, content_only=True)
    task = Tasks(app.store.root).list(book.id)[0]
    data = detail(app.store, task, book, "s0", 1)
    assert data["selected"]["quality"] == "excluded"
    assert data["selected"]["exclusions"][0]["role"] == "publication"
    assert "reason" in data["selected"]["exclusions"][0]
    assert data["sections"][0]["blocks"][1]["excluded_roles"] == ["navigation"]
    assert data["selected"]["points"] == []


def test_real_worker_runs_offline_book_in_fresh_process(app):
    book = save_book(app, ["栈是后进先出结构。"])
    task = launch(app.store, book.id)
    tasks = Tasks(app.store.root)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = next(t for t in tasks.list(book.id) if t["id"] == task)
        if state["status"] not in {"queued", "running", "stopping"}:
            assert state["status"] == "completed", state
            assert state["index"]
            return
        time.sleep(0.1)
    tasks.stop(task)
    raise AssertionError("offline worker did not finish")
