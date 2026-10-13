"""提供 SQLite 表结构和事务化连接管理。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# 表结构使用幂等语句，应用每次启动时都可以安全执行。
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    page_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (document_id, page_number)
);

CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES sections(id) ON DELETE SET NULL,
    level INTEGER NOT NULL,
    number TEXT,
    title TEXT NOT NULL,
    start_page INTEGER NOT NULL,
    end_page INTEGER NOT NULL,
    content TEXT NOT NULL,
    confidence REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    section_id TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    learner_level TEXT NOT NULL,
    learning_goal TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_points (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    learning_objectives_json TEXT NOT NULL,
    prerequisites_json TEXT NOT NULL,
    misconceptions_json TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    importance REAL NOT NULL,
    source_pages_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES learning_sessions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    learning_objectives_json TEXT NOT NULL,
    explanation TEXT NOT NULL,
    example TEXT NOT NULL,
    common_mistakes_json TEXT NOT NULL,
    source_pages_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES learning_sessions(id) ON DELETE CASCADE,
    knowledge_point_ids_json TEXT NOT NULL,
    question_type TEXT NOT NULL,
    difficulty INTEGER NOT NULL,
    question TEXT NOT NULL,
    reference_answer TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    hints_json TEXT NOT NULL,
    common_errors_json TEXT NOT NULL,
    source_pages_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    answer TEXT NOT NULL,
    used_hint INTEGER NOT NULL,
    score REAL NOT NULL,
    assessment_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mastery_records (
    user_id TEXT NOT NULL,
    knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
    mastery_score REAL NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    correct_streak INTEGER NOT NULL,
    misconceptions_json TEXT NOT NULL,
    last_reviewed_at TEXT NOT NULL,
    next_review_at TEXT,
    PRIMARY KEY (user_id, knowledge_point_id)
);

CREATE TABLE IF NOT EXISTS learning_steps (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES learning_sessions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    knowledge_point_id TEXT NOT NULL REFERENCES knowledge_points(id) ON DELETE CASCADE,
    lesson_id TEXT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    correction TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(session_id, position),
    UNIQUE(session_id, knowledge_point_id)
);

CREATE TABLE IF NOT EXISTS learning_step_questions (
    step_id TEXT NOT NULL REFERENCES learning_steps(id) ON DELETE CASCADE,
    question_id TEXT NOT NULL UNIQUE REFERENCES questions(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (step_id, position)
);

INSERT OR IGNORE INTO learning_step_questions (step_id, question_id, position)
SELECT id, question_id, 1 FROM learning_steps;

CREATE INDEX IF NOT EXISTS idx_sections_document ON sections(document_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON learning_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(user_id);
CREATE INDEX IF NOT EXISTS idx_steps_session ON learning_steps(session_id, position);
CREATE INDEX IF NOT EXISTS idx_step_questions_step
    ON learning_step_questions(step_id, position);
"""


class Database:
    """封装数据库初始化和连接生命周期。"""

    def __init__(self, path: Path):
        """记录 SQLite 文件位置，实际连接按操作创建。"""

        self.path = path

    def initialize(self) -> None:
        """创建数据库目录并应用完整表结构。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """提供自动提交、异常回滚且始终关闭的数据库连接。"""

        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

