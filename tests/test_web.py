"""Read-only Web contracts, checkpoint projections, and index-isolated relation views."""

import json

import pytest
from fastapi.testclient import TestClient
from test_book_learning import book_id as book_id

from teaching_agent.cli import main
from teaching_agent.storage import Store, dumps, now, writer_lock
from teaching_agent.web import create_app


@pytest.fixture
def client(app):
    return TestClient(create_app(app.store))


def test_missing_store_does_not_create_data(tmp_path):
    root = tmp_path / "absent"
    client = TestClient(create_app(Store(root)))
    assert client.get("/api/books").json() == []
    assert client.get("/api/books/missing").status_code == 404
    assert not root.exists()
    assert client.post("/api/books").status_code == 405
    assert client.get("/api/no-such-endpoint").status_code == 404
    assert client.get("/", headers={"host": "attacker.example"}).status_code == 400


def test_original_content_and_readonly_during_writer(app, client, book_id, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Web must never initialize a store or invoke a model")

    monkeypatch.setattr(app.store, "initialize", forbidden)
    monkeypatch.setattr(app, "gateway", forbidden)
    before = app.store.path.read_bytes()
    with writer_lock(app.store.root):
        assert client.get("/api/books").json()[0]["id"] == book_id
        catalog = client.get(f"/api/books/{book_id}").json()
        assert catalog["indexes"] == []
        chapter = client.get(f"/api/books/{book_id}/sections/s1").json()
        assert chapter["source"] == "original" and chapter["points"] == []
        assert client.get(f"/api/books/{book_id}/pages/1").json()["chunks"][0]["id"] == "c1"
        assert client.get(f"/api/books/{book_id}/pages/4").json()["unavailable"]
        assert client.get(f"/api/books/{book_id}/pages/99").status_code == 404
        assert client.get(f"/api/books/{book_id}/chunks/missing").status_code == 404
    assert app.store.path.read_bytes() == before


def test_ready_partial_and_rejected_checkpoints(app, client, book_id):
    ready = app.library.index(book_id)
    url = f"/api/books/{book_id}/sections/s1"
    result = client.get(url, params={"index": ready["id"]}).json()
    assert result["source"] == "snapshot" and result["points"] and result["summary"]
    step = app.library.step(ready["id"], "s1:semantic-v1:complete")
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            ("partial", book_id, "building", dumps({"pipeline": "semantic-v1"}), now()),
        )
        conn.execute(
            "INSERT INTO book_steps VALUES(?,?,?)",
            ("partial", "s1:semantic-v1:complete", dumps(step)),
        )
        conn.execute(
            "INSERT INTO book_steps VALUES(?,?,?)",
            ("partial", "s2:extract:0:rejected", dumps({"candidate": step})),
        )
        conn.execute(
            "INSERT INTO book_steps VALUES(?,?,?)",
            ("partial", "s1:summary:0:0", dumps({"summary": "中间摘要"})),
        )
    partial = client.get(url, params={"index": "partial"}).json()
    assert partial["points"] == result["points"]
    assert partial["source"] == "checkpoints" and partial["summary"] is None
    assert partial["quality"] == "reviewed"
    assert not client.get(f"/api/books/{book_id}/sections/s2?index=partial").json()["points"]
    assert client.get(url + "?index=missing").status_code == 404
    assert client.get(url, params={"index": ready["id"]}).json()["source"] == "snapshot"


def test_new_raw_extraction_is_not_shown_as_reviewed(app, client, book_id):
    ready = app.library.index(book_id)
    raw = app.library.step(ready["id"], "s1:semantic-v1:extract:0")
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            ("unreviewed", book_id, "building", dumps({"pipeline": "semantic-v1"}), now()),
        )
        conn.execute(
            "INSERT INTO book_steps VALUES(?,?,?)",
            ("unreviewed", "s1:semantic-v1:extract:0", dumps(raw)),
        )
    view = client.get(f"/api/books/{book_id}/sections/s1?index=unreviewed").json()
    assert view["quality"] == "pending"
    assert view["points"] == []
    assert view["saved_batches"] == 1


def test_pending_section_rebuild_still_shows_unchanged_chapters(app, client, book_id):
    ready = app.library.index(book_id)
    before = client.get(f"/api/books/{book_id}/sections/s1?index={ready['id']}").json()
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            (
                "partial_rebuild",
                book_id,
                "building",
                dumps(
                    {"pipeline": "semantic-v1", "base_index": ready["id"], "rebuild_section": "s2"}
                ),
                now(),
            ),
        )
    view = client.get(f"/api/books/{book_id}/sections/s1?index=partial_rebuild").json()
    assert view["points"] == before["points"]
    assert view["quality"] == "reviewed"
    pending = client.get(f"/api/books/{book_id}/sections/s2?index=partial_rebuild").json()
    assert pending["points"] == [] and pending["quality"] == "pending"


def test_relation_direction_coverage_and_version_isolation(app, client, book_id):
    if not hasattr(app.library, "relations"):
        pytest.skip("按章关系写入功能尚未安装；只读旧数据库仍受支持。")
    index = app.library.index(book_id, content_only=True)
    data = json.loads(index["data"])
    a, b = data["points"][:2]
    edge = dict(source=a["key"], target=b["key"], kind="prerequisite", reason="先理解上下文")
    data["relations"] = [edge, {**edge, "kind": "related", "reason": "共同主题"}]
    with app.store.connect() as conn:
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (dumps(data), index["id"]))
    # Create persisted candidates without invoking a relation model.
    app.library.relations(book_id, max_calls=0)
    url = f"/api/books/{book_id}/points/{a['key']}"
    view = client.get(url, params={"index": index["id"]}).json()
    assert view["relations"] == data["relations"]
    assert b["key"] in {p["key"] for p in view["neighbors"]}
    assert any(p["status"] == "unchecked" for p in view["pairs"])
    app.library.relations(book_id, max_calls=2)
    view = client.get(url, params={"index": index["id"]}).json()
    assert any(p["status"] == "without_relations" for p in view["pairs"])
    with app.store.connect() as conn:
        newer = {**data, "relations": []}
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            ("new_index", book_id, "ready", dumps(newer), now()),
        )
    second = client.get(url + "?index=new_index").json()
    assert second["relations"] == [] and second["pairs"] == []
    assert client.get(url + "?index=missing").status_code == 404
    result = client.get(f"/api/books/{book_id}/sections/s1?index={index['id']}").json()
    assert result["coverage"][0]["checked"] > 0


def test_legacy_missing_tables_and_missing_relation_endpoint(app, client, book_id):
    index = app.library.index(book_id)
    data = json.loads(index["data"])
    key = data["points"][0]["key"]
    data["relations"] = [dict(source=key, target="missing", kind="related", reason="旧数据")]
    with app.store.connect() as conn:
        conn.execute(
            "UPDATE book_indexes SET data=?,status='ready' WHERE id=?", (dumps(data), index["id"])
        )
        for table in (
            "relation_requests",
            "relation_runs",
            "relation_task_pairs",
            "relation_tasks",
            "index_content_state",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    result = client.get(f"/api/books/{book_id}/points/{key}?index={index['id']}").json()
    assert result["missing_keys"] == ["missing"]
    assert result["coverage_available"] is False
    assert client.get(f"/api/books/{book_id}/sections/s1").json()["source"] == "snapshot"


def test_web_command_does_not_initialize_or_load_backend(app, monkeypatch):
    calls = []
    monkeypatch.setattr("teaching_agent.web.serve", lambda store, port: calls.append((store, port)))
    monkeypatch.setattr(Store, "initialize", lambda _: pytest.fail("unexpected initialize"))
    assert (
        main(
            ["--data-dir", str(app.store.root), "--config", "missing.toml", "web", "--port", "9001"]
        )
        == 0
    )
    assert calls[0][1] == 9001


def test_static_assets_and_no_filesystem_access(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<h1>工作台</h1>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("/* app */")
    client = TestClient(create_app(Store(tmp_path / "missing"), static))
    assert "工作台" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/unknown").status_code == 404
    assert client.get("/learning.db").status_code == 404
