"""Single-writer SQLite store with immutable published versions and evidence."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .models import AppError


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def now() -> str:
    return datetime.now(UTC).isoformat()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def writer_lock(root: Path):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / "writer.lock").open("a+b")
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AppError("该数据目录正在被另一写进程使用，请先退出该进程。") from exc
        yield
    finally:
        handle.close()


SCHEMA = """
CREATE TABLE books(id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
CREATE TABLE versions(
 id TEXT PRIMARY KEY, book_id TEXT REFERENCES books(id), section_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','published')), revision INTEGER NOT NULL,
 data TEXT NOT NULL, audit TEXT, reviewed_revision INTEGER, decisions TEXT NOT NULL,
 approved_revision INTEGER, model TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE draft_batches(
 version_id TEXT REFERENCES versions(id), position INTEGER, data TEXT NOT NULL,
 PRIMARY KEY(version_id,position));
CREATE TABLE sessions(
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES versions(id),
 revision INTEGER NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE attempts(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 version_id TEXT NOT NULL REFERENCES versions(id), objective_key TEXT NOT NULL,
 question_id TEXT UNIQUE NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
 assisted INTEGER NOT NULL, practice_only INTEGER NOT NULL, action TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE assessments(
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
 revision INTEGER NOT NULL, data TEXT NOT NULL, reason TEXT NOT NULL,
 model TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(attempt_id,revision));
CREATE TABLE events(
 id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
 data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TRIGGER immutable_version BEFORE UPDATE ON versions WHEN OLD.status='published'
 BEGIN SELECT RAISE(ABORT,'published version is immutable'); END;
CREATE TRIGGER immutable_attempt BEFORE UPDATE ON attempts
 BEGIN SELECT RAISE(ABORT,'attempt is immutable'); END;
CREATE TRIGGER immutable_assessment BEFORE UPDATE ON assessments
 BEGIN SELECT RAISE(ABORT,'assessment is immutable'); END;
PRAGMA user_version=1;
"""


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "learning.db"

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise AppError("数据库版本不兼容，请使用新的数据目录。")
            if version == 0:
                conn.executescript(SCHEMA)
            if version < 2:
                conn.execute(
                    "CREATE TABLE backend_bindings("
                    "owner_id TEXT PRIMARY KEY,name TEXT NOT NULL,spec TEXT NOT NULL)"
                )
                conn.execute("PRAGMA user_version=2")
            if version < 3:
                conn.execute(
                    "CREATE TABLE preparations(version_id TEXT PRIMARY KEY "
                    "REFERENCES versions(id),data TEXT NOT NULL)"
                )
                conn.execute("PRAGMA user_version=3")
            if version < 4:
                conn.execute(
                    "CREATE TABLE attempt_context(attempt_id TEXT PRIMARY KEY "
                    "REFERENCES attempts(id) ON DELETE CASCADE,data TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE answer_rounds(attempt_id TEXT REFERENCES attempts(id) "
                    "ON DELETE CASCADE,round INTEGER NOT NULL,data TEXT NOT NULL,"
                    "PRIMARY KEY(attempt_id,round))"
                )
                for table in ("attempt_context", "answer_rounds"):
                    conn.execute(
                        f"CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'answer context is immutable'); END;"
                    )
                conn.execute("PRAGMA user_version=4")
            if version < 5:
                for statement in (
                    "CREATE TABLE IF NOT EXISTS book_indexes(id TEXT PRIMARY KEY, book_id TEXT "
                    "NOT NULL REFERENCES books(id), status TEXT NOT NULL, data TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS book_steps(owner_id TEXT NOT NULL, step TEXT "
                    "NOT NULL, data TEXT NOT NULL, PRIMARY KEY(owner_id,step))",
                    "CREATE TABLE IF NOT EXISTS lesson_links(version_id TEXT PRIMARY KEY "
                    "REFERENCES versions(id), index_id TEXT NOT NULL REFERENCES "
                    "book_indexes(id), section_id TEXT NOT NULL, data TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS self_mastery(book_id TEXT NOT NULL REFERENCES "
                    "books(id), point_key TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at "
                    "TEXT NOT NULL, PRIMARY KEY(book_id,point_key,fingerprint))",
                    "CREATE TABLE IF NOT EXISTS book_questions(id TEXT PRIMARY KEY, book_id "
                    "TEXT NOT NULL REFERENCES books(id), session_id TEXT, data TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS search_chunks(book_id TEXT NOT NULL REFERENCES "
                    "books(id), chunk_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY "
                    "KEY(book_id,chunk_id))",
                ):
                    conn.execute(statement)
                conn.execute("PRAGMA user_version=5")
            conn.execute("PRAGMA journal_mode=WAL")
            # Materialize WAL state before subsequent read-only connections.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()

    @contextmanager
    def connect(self, readonly=False):
        if readonly and not self.path.exists():
            raise AppError("暂无数据。请先运行 teach demo 或 teach import。")
        target = self.path.as_uri() + "?mode=ro" if readonly else str(self.path)
        conn = sqlite3.connect(target, uri=readonly, timeout=5)
        if readonly:
            try:
                # Some SQLite builds cannot open a WAL database read-only when
                # its sidecar files have been removed after the last close.
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except sqlite3.Error as exc:
                conn.close()
                if getattr(exc, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_CANTOPEN:
                    raise
                # Allow SQLite to manage sidecars without creating a database
                # or permitting application SQL to change persisted data.
                conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
            try:
                conn.execute("PRAGMA query_only=ON")
            except BaseException:
                conn.close()
                raise
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            if not readonly:
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rows(self, sql, args=()):
        with self.connect(readonly=True) as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def get(self, table, identifier):
        if table not in {"books", "versions", "sessions", "attempts"}:
            raise ValueError("invalid table")
        rows = self.rows(f"SELECT * FROM {table} WHERE id = ?", (identifier,))
        if not rows:
            raise AppError(f"找不到 {identifier}，请使用列表中的完整 ID。")
        return rows[0]

    @staticmethod
    def event(conn, owner, kind, data):
        conn.execute(
            "INSERT INTO events(owner_id,kind,data,created_at) VALUES(?,?,?,?)",
            (owner, kind, dumps(data), now()),
        )

    def save_session(self, identifier, revision, state, conn=None):
        if conn is None:
            with self.connect() as tx:
                self.save_session(identifier, revision, state, tx)
            return
        cursor = conn.execute(
            "UPDATE sessions SET data=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (dumps(state), now(), identifier, revision),
        )
        if cursor.rowcount != 1:
            raise AppError("会话已变化，请恢复后继续。")
