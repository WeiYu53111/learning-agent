import json
import time

from test_semantic_extraction import classified_gateway, save_book

from teaching_agent.extraction_checkpoints import previews
from teaching_agent.extraction_tasks import Tasks, launch


def test_stop_second_block_restarts_second_not_first(app):
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
            if chunk == "c1" and not stopped:
                stopped = True
                task = Tasks(app.store.root).list(book.id)[0]
                Tasks(app.store.root).stop(task["id"])
        return value

    app.injected_gateway.book_operation = operation
    assert app.library.index(book.id, content_only=True) is None
    task = Tasks(app.store.root).list(book.id)[0]
    assert task["status"] == "stopped"
    assert task["block"] == 2
    assert calls == ["c0", "c1"]
    visible = previews(app.store, task)
    assert [v["quality"] for v in visible] == ["block_reviewed", "pending"]
    old_index = task["index"]
    result = app.library.index(book.id, content_only=True)
    assert result["id"] == old_index
    assert calls == ["c0", "c1", *[f"c{i}" for i in range(1, 30)]]
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
