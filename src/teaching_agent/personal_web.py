"""Local, transactional personal knowledge API."""

import json
from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import personal as service
from .storage import uid, writer_lock


class CardInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=500000)
    aliases: list[str] = []
    topics: list[str] = []
    archived: bool = False
    revision: int | None = None


class Resolution(BaseModel):
    action: Literal["accept", "ignore"]
    target: str | None = None
    revision: int | None = None
    title: str | None = None
    body: str | None = None


class TopicInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RelationInput(BaseModel):
    source: str
    target: str
    kind: Literal["contains", "depends", "applies", "contrast", "related"]
    reason: str = ""


class CollectInput(BaseModel):
    book: str
    index: str
    key: str


def router(store):
    api = APIRouter(prefix="/api/personal")

    @contextmanager
    def write():
        with writer_lock(store.root):
            store.initialize()
            with store.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except service.Conflict as exc:
                    raise HTTPException(409, str(exc)) from exc

    def available():
        if not store.path.exists():
            return False
        with store.connect(readonly=True) as conn:
            return bool(
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='personal_cards'").fetchone()
            )

    @api.get("")
    def overview():
        if not available():
            return {"cards": [], "topics": [], "inbox": [], "relations": [], "links": []}
        with store.connect(readonly=True) as conn:
            cards = [json.loads(r[0]) for r in conn.execute("SELECT data FROM personal_cards")]
            existing = {r[0] for r in conn.execute("SELECT id FROM books")}
            for card in cards:
                for source in card["sources"]:
                    source["available"] = source["book_id"] in existing
            return {
                "cards": cards,
                "topics": [dict(r) for r in conn.execute("SELECT * FROM personal_topics")],
                "inbox": [
                    json.loads(r[0])
                    for r in conn.execute("SELECT data FROM personal_inbox WHERE status='pending'")
                ],
                "relations": [dict(r) for r in conn.execute("SELECT * FROM personal_relations")],
                "links": [dict(r) for r in conn.execute("SELECT * FROM personal_links")],
            }

    @api.post("/cards")
    def create(value: CardInput):
        with write() as conn:
            return service.save_card(conn, value.model_dump())

    @api.put("/cards/{identifier}")
    def update(identifier: str, value: CardInput):
        with write() as conn:
            return service.save_card(conn, value.model_dump(), identifier, value.revision)

    @api.get("/cards/{identifier}/history")
    def history(identifier: str):
        if not available():
            return []
        with store.connect(readonly=True) as conn:
            return [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT data FROM personal_history WHERE card_id=? ORDER BY revision DESC",
                    (identifier,),
                )
            ]

    @api.get("/suggestions")
    def suggestions():
        if not available():
            return []
        from .web import Reader

        data = overview()
        mapping, indexes = {}, set()
        for c in data["cards"]:
            if c["archived"]:
                continue
            for s in c["sources"]:
                if s.get("index_id") and s.get("point_key") and s.get("available"):
                    mapping.setdefault((s["book_id"], s["point_key"]), set()).add(c["id"])
                    indexes.add((s["book_id"], s["index_id"]))
        result = {}
        existing = {(e["source"], e["target"], e["kind"]) for e in data["relations"]}
        with store.connect(readonly=True) as conn:
            reader = Reader(conn)
            for book, index in indexes:
                try:
                    _, content, _ = reader.content(book, index)
                except HTTPException:
                    continue
                for e in content.get("relations", []):
                    for source in mapping.get((book, e["source"]), []):
                        for target in mapping.get((book, e["target"]), []):
                            if source == target:
                                continue
                            kind = "depends" if e["kind"] == "prerequisite" else "related"
                            a, b = (
                                (target, source) if kind == "depends" else sorted((source, target))
                            )
                            key = (a, b, kind)
                            if key not in existing:
                                result[key] = {
                                    "source": a,
                                    "target": b,
                                    "kind": kind,
                                    "reason": e.get("reason", "教材关系建议"),
                                }
        return list(result.values())

    @api.get("/cards/{identifier}/graph")
    def graph(identifier: str):
        data = overview()
        edges = [r for r in data["relations"] if identifier in (r["source"], r["target"])]
        edges += [
            {**r, "kind": "reference", "id": f"link:{r['source']}:{r['target']}"}
            for r in data["links"]
            if identifier in (r["source"], r["target"])
        ]
        ids = {identifier} | {e[k] for e in edges for k in ("source", "target")}
        return {"nodes": [c for c in data["cards"] if c["id"] in ids], "edges": edges}

    @api.post("/topics")
    def topic(value: TopicInput):
        with write() as conn:
            identifier = uid("topic")
            name = value.name.strip()
            if (
                not name
                or conn.execute("SELECT 1 FROM personal_topics WHERE name=?", (name,)).fetchone()
            ):
                raise HTTPException(400, "主题为空或已存在。")
            conn.execute("INSERT INTO personal_topics VALUES(?,?)", (identifier, name))
            return {"id": identifier, "name": name}

    @api.put("/topics/{identifier}")
    def rename_topic(identifier: str, value: TopicInput):
        with write() as conn:
            if (
                not value.name.strip()
                or conn.execute(
                    "SELECT 1 FROM personal_topics WHERE name=? AND id<>?",
                    (value.name.strip(), identifier),
                ).fetchone()
            ):
                raise HTTPException(400, "主题为空或已存在。")
            conn.execute(
                "UPDATE personal_topics SET name=? WHERE id=?", (value.name.strip(), identifier)
            )
        return {"ok": True}

    @api.delete("/topics/{identifier}")
    def delete_topic(identifier: str):
        with write() as conn:
            for row in conn.execute("SELECT data FROM personal_cards").fetchall():
                c = json.loads(row[0])
                if identifier in c["topics"]:
                    service.save_card(
                        conn,
                        {**c, "topics": [t for t in c["topics"] if t != identifier]},
                        c["id"],
                        c["revision"],
                    )
            conn.execute("DELETE FROM personal_topics WHERE id=?", (identifier,))
        return {"ok": True}

    @api.post("/relations")
    def relation(value: RelationInput):
        with write() as conn:
            service.card(conn, value.source)
            service.card(conn, value.target)
            if value.source == value.target:
                raise HTTPException(400, "不能关联自身。")
            source, target = value.source, value.target
            if value.kind in {"contrast", "related"}:
                source, target = sorted((source, target))
            conn.execute(
                "INSERT OR IGNORE INTO personal_relations VALUES(?,?,?,?,?)",
                (uid("rel"), source, target, value.kind, value.reason),
            )
        return {"ok": True}

    @api.delete("/relations/{identifier}")
    def delete_relation(identifier: str):
        with write() as conn:
            conn.execute("DELETE FROM personal_relations WHERE id=?", (identifier,))
        return {"ok": True}

    @api.post("/inbox/{identifier}/resolve")
    def resolve(identifier: str, value: Resolution):
        with write() as conn:
            return service.resolve(conn, identifier, **value.model_dump())

    @api.post("/collect")
    def collect(value: CollectInput):
        from .web import Reader

        with write() as conn:
            reader = Reader(conn)
            book = reader.book(value.book)
            _, data, _ = reader.content(value.book, value.index)
            point = next((p for p in data.get("points", []) if p.get("key") == value.key), None)
            if point is None:
                raise HTTPException(404, "请先完成知识提取，再收藏知识点。")
            return service.enqueue(
                conn,
                f"knowledge:{value.book}:{value.key}",
                point["title"],
                point["description"],
                service.snapshot(
                    book.model_dump(),
                    point["source_refs"],
                    index_id=value.index,
                    point_key=value.key,
                ),
                [],
            )

    return api
