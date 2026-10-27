"""Schema 6: additive migration, executed inside Store's migration transaction."""

import hashlib
import json

from .storage import dumps


def migrate(conn):
    statements = [
        """CREATE TABLE IF NOT EXISTS index_content_state(
        index_id TEXT PRIMARY KEY REFERENCES book_indexes(id) ON DELETE CASCADE,
        status TEXT NOT NULL CHECK(status IN ('building','ready')),
        content_fingerprint TEXT NOT NULL, graph_revision INTEGER NOT NULL DEFAULT 0,
        legacy_coverage TEXT NOT NULL DEFAULT 'unknown')""",
        """CREATE TABLE IF NOT EXISTS relation_checks(
        id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
        pair_key TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
        left_fingerprint TEXT NOT NULL, right_fingerprint TEXT NOT NULL,
        validator_version TEXT NOT NULL, result TEXT NOT NULL, origin TEXT NOT NULL,
        model TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE INDEX IF NOT EXISTS relation_cache_lookup ON relation_checks(
        book_id,pair_key,left_fingerprint,right_fingerprint,validator_version)""",
        """CREATE TABLE IF NOT EXISTS relation_tasks(
        id TEXT PRIMARY KEY, index_id TEXT NOT NULL REFERENCES book_indexes(id) ON DELETE CASCADE,
        section_id TEXT NOT NULL, selector_version TEXT NOT NULL, validator_version TEXT NOT NULL,
        status TEXT NOT NULL, max_depth INTEGER NOT NULL, frontier TEXT NOT NULL,
        last_error TEXT, pause_reason TEXT, UNIQUE(index_id,section_id,selector_version,
        validator_version))""",
        """CREATE TABLE IF NOT EXISTS relation_task_pairs(
        task_id TEXT NOT NULL REFERENCES relation_tasks(id) ON DELETE CASCADE,
        pair_key TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
        left_fingerprint TEXT NOT NULL, right_fingerprint TEXT NOT NULL,
        reason TEXT NOT NULL, check_id TEXT REFERENCES relation_checks(id),
        PRIMARY KEY(task_id,pair_key))""",
        """CREATE TABLE IF NOT EXISTS relation_runs(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES relation_tasks(id) ON DELETE CASCADE,
        max_calls INTEGER, calls_used INTEGER NOT NULL DEFAULT 0,
        reused_pairs INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS relation_requests(
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES relation_tasks(id) ON DELETE CASCADE,
        run_id TEXT NOT NULL REFERENCES relation_runs(id) ON DELETE CASCADE,
        payload TEXT NOT NULL, members TEXT NOT NULL, status TEXT NOT NULL,
        correction_of TEXT, candidate TEXT, error TEXT, model TEXT NOT NULL,
        input_bytes INTEGER NOT NULL, output_bytes INTEGER NOT NULL DEFAULT 0,
        elapsed_seconds REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        repairable INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS relation_graph_edges(
        index_id TEXT NOT NULL REFERENCES book_indexes(id) ON DELETE CASCADE,
        check_id TEXT REFERENCES relation_checks(id), source TEXT NOT NULL, target TEXT NOT NULL,
        kind TEXT NOT NULL, reason TEXT NOT NULL, graph_revision INTEGER NOT NULL,
        PRIMARY KEY(index_id,source,target,kind))""",
        """CREATE TABLE IF NOT EXISTS relation_legacy_imports(
        index_id TEXT PRIMARY KEY REFERENCES book_indexes(id) ON DELETE CASCADE,
        algorithm_version TEXT NOT NULL, evidence TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS lesson_relation_snapshots(
        version_id TEXT PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
        index_id TEXT NOT NULL REFERENCES book_indexes(id), graph_revision INTEGER NOT NULL,
        data TEXT NOT NULL)""",
    ]
    for statement in statements:
        conn.execute(statement)
    for table in ("relation_checks", "lesson_relation_snapshots"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS immutable_{table} BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT,'relation judgment/snapshot is immutable'); END"
        )
    for row in conn.execute("SELECT * FROM book_indexes").fetchall():
        data = json.loads(row["data"])
        ready = row["status"] == "ready"
        digest = (
            hashlib.sha256(
                dumps({k: v for k, v in data.items() if k != "relations"}).encode()
            ).hexdigest()
            if ready
            else ""
        )
        conn.execute(
            "INSERT OR IGNORE INTO index_content_state VALUES(?,?,?,?,?)",
            (
                row["id"],
                "ready" if ready else "building",
                digest,
                0,
                "old_strategy_complete" if ready else "unknown",
            ),
        )
    for row in conn.execute(
        "SELECT l.version_id,l.index_id,b.data FROM lesson_links l "
        "JOIN book_indexes b ON b.id=l.index_id"
    ).fetchall():
        data = json.loads(row["data"])
        snapshot = dict(
            relations=data.get("relations", []),
            origin="legacy",
            coverage={"status": "legacy_unknown"},
            allow_partial=False,
        )
        conn.execute(
            "INSERT OR IGNORE INTO lesson_relation_snapshots VALUES(?,?,?,?)",
            (row["version_id"], row["index_id"], 0, dumps(snapshot)),
        )
    conn.execute("PRAGMA user_version=6")
