import json
import sqlite3

from fastapi.testclient import TestClient
from test_book_learning import book_id as book_id

from teaching_agent.personal import enqueue, links
from teaching_agent.storage import Store, dumps
from teaching_agent.web import create_app


def test_read_only_empty_does_not_initialize(tmp_path):
    store = Store(tmp_path / "empty")
    client = TestClient(create_app(store))
    assert client.get("/api/personal").json()["cards"] == []
    assert not store.path.exists()


def test_cards_revision_topics_links_and_archive(app):
    client = TestClient(create_app(app.store))
    topic = client.post("/api/personal/topics", json={"name": "算法"}).json()
    a = client.post(
        "/api/personal/cards", json={"title": "栈", "body": "我的理解", "topics": [topic["id"]]}
    ).json()
    b = client.post(
        "/api/personal/cards",
        json={"title": "递归", "body": f"[[{a['id']}|栈]]\n```\n[[card_fake|代码]]\n```"},
    ).json()
    assert client.get("/api/personal").json()["links"] == [{"source": b["id"], "target": a["id"]}]
    updated = client.put(
        f"/api/personal/cards/{a['id']}", json={**a, "title": "调用栈", "archived": True}
    ).json()
    assert updated["revision"] == 2
    assert client.put(f"/api/personal/cards/{a['id']}", json=a).status_code == 409
    assert (
        client.get(f"/api/personal/cards/{a['id']}/graph").json()["edges"][0]["kind"] == "reference"
    )
    client.delete(f"/api/personal/topics/{topic['id']}")
    cards = client.get("/api/personal").json()["cards"]
    assert next(c for c in cards if c["id"] == a["id"])["topics"] == []
    assert len(client.get(f"/api/personal/cards/{a['id']}/history").json()) == 3


def test_merge_does_not_replace_body_and_resolve_is_idempotent(app):
    client = TestClient(create_app(app.store))
    a = client.post("/api/personal/cards", json={"title": "栈", "body": "手写内容"}).json()
    source = {
        "book_id": "removed",
        "filename": "教材",
        "quote": "摘录",
        "location": {"kind": "text", "start_line": 1},
        "chunk_id": "c",
    }
    with app.store.connect() as conn:
        candidate = enqueue(
            conn,
            "test",
            "自动标题",
            "自动正文",
            [source],
            [{"session_id": "s", "needs_understanding": True}],
        )
    url = f"/api/personal/inbox/{candidate['id']}/resolve"
    payload = {"action": "accept", "target": a["id"], "revision": a["revision"]}
    assert client.post(url, json=payload).status_code == 200
    assert client.post(url, json=payload).status_code == 200
    result = client.get("/api/personal").json()
    assert result["cards"][0]["body"] == "手写内容"
    assert len(result["cards"][0]["sources"]) == 1
    assert result["cards"][0]["sources"][0]["available"] is False
    assert result["inbox"] == []


def test_relations_deduplicate_symmetric_and_reject_self(app):
    client = TestClient(create_app(app.store))
    a, b = [client.post("/api/personal/cards", json={"title": t}).json()["id"] for t in ("a", "b")]
    for source, target in ((a, b), (b, a)):
        assert (
            client.post(
                "/api/personal/relations",
                json={"source": source, "target": target, "kind": "related"},
            ).status_code
            == 200
        )
    assert len(client.get("/api/personal").json()["relations"]) == 1
    assert (
        client.post(
            "/api/personal/relations", json={"source": a, "target": a, "kind": "depends"}
        ).status_code
        == 400
    )


def test_completed_session_collects_only_attempted_once(app, published):
    from teaching_agent.personal import collect_session

    # Build minimal evidence-bearing rows using the real published lesson contract.
    version = app.store.get("versions", published)
    draft = json.loads(version["data"])
    objective = draft["points"][0]["objectives"][0]
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?)",
            ("test-session", published, 0, dumps({}), "now"),
        )
        columns = [r[1] for r in conn.execute("PRAGMA table_info(attempts)")]
        values = {c: "" for c in columns}
        values.update(
            id="attempt",
            session_id="test-session",
            version_id=published,
            objective_key=objective["key"],
            question_id="question",
            question="{}",
            answer="answer",
            assisted=0,
            practice_only=0,
        )
        conn.execute(
            f"INSERT INTO attempts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values[c] for c in columns),
        )
        collect_session(conn, "test-session", {"lifecycle": "paused"})
        assert conn.execute("SELECT count(*) FROM personal_inbox").fetchone()[0] == 0
        collect_session(conn, "test-session", {"lifecycle": "ended_with_gaps"})
        collect_session(conn, "test-session", {"lifecycle": "ended_with_gaps"})
        rows = conn.execute("SELECT data FROM personal_inbox").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])["evidence"][0]["needs_understanding"]


def test_link_code_exclusions():
    assert links("[[card_a|A]] `[[card_b|B]]`\n~~~\n[[card_c|C]]\n~~~") == {"card_a"}


def test_manual_collection_does_not_modify_source_and_survives_deletion(app, book_id):
    client = TestClient(create_app(app.store))
    index = app.library.index(book_id)
    point = json.loads(index["data"])["points"][0]
    before = app.book(book_id)
    payload = {"book": book_id, "index": index["id"], "key": point["key"]}
    a = client.post("/api/personal/collect", json=payload).json()
    b = client.post("/api/personal/collect", json=payload).json()
    assert a["id"] == b["id"]
    assert app.book(book_id) == before
    assert (
        client.post(f"/api/personal/inbox/{a['id']}/resolve", json={"action": "accept"}).status_code
        == 200
    )
    app.delete_book(book_id)
    card = client.get("/api/personal").json()["cards"][0]
    assert card["sources"][0]["quote"]
    assert card["sources"][0]["available"] is False


def test_schema_upgrade_backup_and_no_backfill(tmp_path):
    from teaching_agent.storage import SCHEMA

    root = tmp_path / "old"
    root.mkdir()
    with sqlite3.connect(root / "learning.db") as conn:
        conn.executescript(SCHEMA)
    store = Store(root)
    store.initialize()
    store.initialize()
    assert (root / "learning.pre-personal.sqlite").exists()
    assert store.rows("SELECT * FROM personal_events") == []
