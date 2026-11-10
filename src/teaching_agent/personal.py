"""Personal cards and inbox. Source snapshots never depend on source lifetime."""

import json
import re

from .models import AppError
from .storage import dumps, now, uid


class Conflict(AppError):
    pass


def migrate(conn):
    for statement in (
        "CREATE TABLE personal_cards(id TEXT PRIMARY KEY, "
        "revision INTEGER NOT NULL, data TEXT NOT NULL)",
        "CREATE TABLE personal_history(card_id TEXT NOT NULL, revision INTEGER NOT NULL, "
        "data TEXT NOT NULL, PRIMARY KEY(card_id,revision))",
        "CREATE TABLE personal_topics(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL)",
        "CREATE TABLE personal_relations(id TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "target TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, "
        "UNIQUE(source,target,kind))",
        "CREATE TABLE personal_links(source TEXT NOT NULL, target TEXT NOT NULL, "
        "PRIMARY KEY(source,target))",
        "CREATE TABLE personal_inbox(id TEXT PRIMARY KEY, identity TEXT NOT NULL, "
        "status TEXT NOT NULL, data TEXT NOT NULL)",
        "CREATE UNIQUE INDEX personal_pending ON personal_inbox(identity) WHERE status='pending'",
        "CREATE TABLE personal_events(id TEXT PRIMARY KEY)",
    ):
        conn.execute(
            statement.replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ").replace(
                "CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS "
            )
        )
    conn.execute("PRAGMA user_version=7")


def links(body):
    # Ignore fenced and inline code before resolving explicit ID links.
    clean, fence = [], None
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            clean.append(line)
    text = re.sub(r"(`+).*?\1", "", "\n".join(clean), flags=re.S)
    return set(re.findall(r"\[\[(card_[a-z0-9]+)\|[^\]\n]*\]\]", text))


def card(conn, identifier):
    row = conn.execute("SELECT data FROM personal_cards WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise AppError("卡片不存在。")
    return json.loads(row[0])


def save_card(conn, value, identifier=None, revision=None):
    old = card(conn, identifier) if identifier else None
    if old and old["revision"] != revision:
        raise Conflict("卡片已在其他窗口修改，请保留当前内容并重新读取。")
    title = value.get("title", "").strip()
    if not title:
        raise AppError("请输入卡片标题。")
    identifier = identifier or uid("card")
    topics = list(dict.fromkeys(value.get("topics", [])))
    for topic in topics:
        if not conn.execute("SELECT 1 FROM personal_topics WHERE id=?", (topic,)).fetchone():
            raise AppError("主题不存在，请重新选择。")
    result = {
        "id": identifier,
        "title": title,
        "aliases": value.get("aliases", []),
        "body": value.get("body", ""),
        "topics": topics,
        "archived": value.get("archived", False),
        "revision": (old["revision"] if old else 0) + 1,
        "sources": old["sources"] if old else [],
        "evidence": old["evidence"] if old else [],
        "created_at": old["created_at"] if old else now(),
        "updated_at": now(),
    }
    targets = links(result["body"]) - {identifier}
    for target in targets:
        card(conn, target)
    persist(conn, result)
    conn.execute("DELETE FROM personal_links WHERE source=?", (identifier,))
    conn.executemany("INSERT INTO personal_links VALUES(?,?)", [(identifier, t) for t in targets])
    return result


def persist(conn, value):
    conn.execute(
        "INSERT INTO personal_cards VALUES(?,?,?) ON CONFLICT(id) DO UPDATE "
        "SET revision=excluded.revision,data=excluded.data",
        (value["id"], value["revision"], dumps(value)),
    )
    conn.execute(
        "INSERT INTO personal_history VALUES(?,?,?)", (value["id"], value["revision"], dumps(value))
    )


def snapshot(book, refs, **extra):
    chunks = {c["id"]: c for c in book["chunks"]}
    return [
        {
            "book_id": book["id"],
            "filename": book["filename"],
            "source_version": book.get("source_version", "legacy"),
            "chunk_id": r["chunk_id"],
            "quote": r["quote"],
            "location": chunks.get(r["chunk_id"], {}).get("location")
            or {"kind": "pdf", "page": chunks.get(r["chunk_id"], {}).get("page")},
            **extra,
        }
        for r in refs
    ]


def enqueue(conn, identity, title, body, sources, evidence):
    row = conn.execute(
        "SELECT id,data FROM personal_inbox WHERE identity=? AND status='pending'", (identity,)
    ).fetchone()
    value = (
        json.loads(row["data"])
        if row
        else {
            "id": uid("inbox"),
            "title": title,
            "body": body,
            "sources": [],
            "evidence": [],
            "created_at": now(),
        }
    )
    for field, items in (("sources", sources), ("evidence", evidence)):
        for item in items:
            if item not in value[field]:
                value[field].append(item)
    conn.execute(
        "INSERT INTO personal_inbox VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
        (value["id"], identity, "pending", dumps(value)),
    )
    return value


def collect_session(conn, identifier, state):
    if state.get("lifecycle") not in {"completed", "ended_with_gaps"}:
        return
    if not conn.execute("INSERT OR IGNORE INTO personal_events VALUES(?)", (identifier,)).rowcount:
        return
    row = conn.execute(
        "SELECT v.* FROM versions v JOIN sessions s ON s.version_id=v.id WHERE s.id=?",
        (identifier,),
    ).fetchone()
    if not row:
        return
    draft = json.loads(row["data"])
    book = json.loads(
        conn.execute("SELECT data FROM books WHERE id=?", (row["book_id"],)).fetchone()[0]
    )
    attempted = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT objective_key FROM attempts WHERE session_id=?", (identifier,)
        )
    }
    link = conn.execute("SELECT * FROM lesson_links WHERE version_id=?", (row["id"],)).fetchone()
    mapping, knowledge = {}, {}
    if link:
        mapping = {
            p["draft_point"]: p["knowledge_keys"]
            for p in json.loads(link["data"]).get("point_map", [])
        }
        index_row = conn.execute(
            "SELECT data FROM book_indexes WHERE id=?", (link["index_id"],)
        ).fetchone()
        content = json.loads(index_row[0]) if index_row else {"points": []}
        knowledge = {p["key"]: p for p in content["points"]}
    for point in draft["points"]:
        objectives = [o for o in point["objectives"] if o["key"] in attempted]
        if not objectives:
            continue
        refs = [r for o in objectives for r in o.get("source_refs", [])]
        mapped = [knowledge[k] for k in mapping.get(point["key"], []) if k in knowledge]
        if mapped:
            for p in mapped:
                enqueue(
                    conn,
                    f"knowledge:{book['id']}:{p['key']}",
                    p["title"],
                    p["description"],
                    snapshot(book, p["source_refs"], index_id=link["index_id"], point_key=p["key"]),
                    [
                        {
                            "session_id": identifier,
                            "status": state["lifecycle"],
                            "needs_understanding": state["lifecycle"] == "ended_with_gaps",
                            "objectives": [o["key"] for o in objectives],
                        }
                    ],
                )
            continue
        enqueue(
            conn,
            f"lesson:{row['id']}:{point['key']}",
            point["title"],
            "\n\n".join(o["description"] for o in objectives),
            snapshot(book, refs, version_id=row["id"]),
            [
                {
                    "session_id": identifier,
                    "status": state["lifecycle"],
                    "needs_understanding": state["lifecycle"] == "ended_with_gaps",
                    "objectives": [o["key"] for o in objectives],
                }
            ],
        )


def resolve(conn, identifier, action, target=None, revision=None, title=None, body=None):
    row = conn.execute("SELECT * FROM personal_inbox WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise AppError("候选不存在。")
    value = json.loads(row["data"])
    if row["status"] != "pending":
        return value
    if action == "ignore":
        conn.execute("UPDATE personal_inbox SET status='ignored' WHERE id=?", (identifier,))
        return value
    if target:
        result = card(conn, target)
        if result["revision"] != revision:
            raise Conflict("目标卡片已变化，请重新读取。")
        result["revision"] += 1
    else:
        result = save_card(
            conn,
            {"title": title or value["title"], "body": value["body"] if body is None else body},
        )
        result["revision"] += 1
    for field in ("sources", "evidence"):
        for item in value[field]:
            if item not in result[field]:
                result[field].append(item)
    result["updated_at"] = now()
    persist(conn, result)
    value["card_id"] = result["id"]
    conn.execute(
        "UPDATE personal_inbox SET status='accepted',data=? WHERE id=?", (dumps(value), identifier)
    )
    return value
