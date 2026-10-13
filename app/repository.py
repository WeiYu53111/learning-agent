"""封装教学业务数据的持久化、查询和掌握度更新逻辑。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.db import Database
from app.pdf_parser import ParsedDocument


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""

    return datetime.now(UTC)


def _id(prefix: str) -> str:
    """生成带业务前缀的全局唯一标识。"""

    return f"{prefix}_{uuid4().hex}"


def _json(value: Any) -> str:
    """将值序列化为保留非 ASCII 字符的 JSON。"""

    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    """反序列化非空 JSON，否则返回调用方指定的默认值。"""

    return json.loads(value) if value else default


class Repository:
    """提供面向教学流程的 SQLite 数据访问接口。"""

    def __init__(self, database: Database):
        """绑定底层数据库连接管理器。"""

        self.database = database

    def find_document_by_hash(self, sha256: str) -> dict[str, Any] | None:
        """按内容哈希查找已上传文档，用于上传去重。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
            ).fetchone()
            return dict(row) if row else None

    def create_document(
        self,
        filename: str,
        stored_path: Path,
        sha256: str,
        parsed: ParsedDocument,
    ) -> dict[str, Any]:
        """在同一事务中保存文档、逐页文本和章节层级。"""

        document_id = _id("doc")
        now = utc_now().isoformat()
        section_ids = [_id("sec") for _ in parsed.sections]
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO documents
                   (id, filename, stored_path, sha256, page_count, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'ready', ?)""",
                (document_id, filename, str(stored_path), sha256, parsed.page_count, now),
            )
            connection.executemany(
                "INSERT INTO pages (document_id, page_number, text) VALUES (?, ?, ?)",
                [
                    (document_id, page_number, text)
                    for page_number, text in enumerate(parsed.pages, start=1)
                ],
            )
            for index, section in enumerate(parsed.sections):
                parent_id = (
                    section_ids[section.parent_index]
                    if section.parent_index is not None
                    else None
                )
                connection.execute(
                    """INSERT INTO sections
                       (id, document_id, parent_id, level, number, title, start_page,
                        end_page, content, confidence)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        section_ids[index],
                        document_id,
                        parent_id,
                        section.level,
                        section.number,
                        section.title,
                        section.start_page,
                        section.end_page,
                        section.content,
                        section.confidence,
                    ),
                )
        return self.get_document(document_id)

    def get_document(self, document_id: str) -> dict[str, Any]:
        """读取指定文档；不存在时抛出业务友好的异常。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (document_id,)
            ).fetchone()
        if not row:
            raise KeyError("文档不存在。")
        return dict(row)

    def list_documents(self) -> list[dict[str, Any]]:
        """按上传时间倒序列出全部文档。"""

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM documents ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_sections(self, document_id: str) -> list[dict[str, Any]]:
        """按页码和写入顺序列出文档的章节摘要。"""

        self.get_document(document_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id, document_id, parent_id, level, number, title,
                          start_page, end_page, confidence
                   FROM sections WHERE document_id = ? ORDER BY start_page, rowid""",
                (document_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_section(self, section_id: str) -> dict[str, Any]:
        """读取包含正文的章节详情。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM sections WHERE id = ?", (section_id,)
            ).fetchone()
        if not row:
            raise KeyError("章节不存在。")
        return dict(row)

    def create_session(
        self, user_id: str, section_id: str, learner_level: str, learning_goal: str
    ) -> str:
        """为学习者和章节创建一个进行中的学习会话。"""

        session_id = _id("session")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO learning_sessions
                   (id, user_id, section_id, learner_level, learning_goal, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?)""",
                (
                    session_id,
                    user_id,
                    section_id,
                    learner_level,
                    learning_goal,
                    utc_now().isoformat(),
                ),
            )
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any]:
        """读取学习会话状态和个性化设置。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM learning_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if not row:
            raise KeyError("学习会话不存在。")
        return dict(row)

    def replace_knowledge_points(
        self, section_id: str, points: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """用模型最新输出原子替换章节的知识点集合。"""

        stored: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            connection.execute("DELETE FROM knowledge_points WHERE section_id = ?", (section_id,))
            for point in points:
                item = {**point, "id": _id("kp"), "section_id": section_id}
                connection.execute(
                    """INSERT INTO knowledge_points
                       (id, section_id, name, description, learning_objectives_json,
                        prerequisites_json, misconceptions_json, difficulty, importance,
                        source_pages_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["id"],
                        section_id,
                        item["name"],
                        item["description"],
                        _json(item["learning_objectives"]),
                        _json(item.get("prerequisites", [])),
                        _json(item.get("common_misconceptions", [])),
                        item["difficulty"],
                        item["importance"],
                        _json(item["source_pages"]),
                    ),
                )
                stored.append(item)
        return stored

    def list_knowledge_points(self, section_id: str) -> list[dict[str, Any]]:
        """按生成顺序读取章节知识点并还原 JSON 字段。"""

        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM knowledge_points WHERE section_id = ? ORDER BY rowid",
                (section_id,),
            ).fetchall()
        return [self._knowledge_point(dict(row)) for row in rows]

    def get_question(self, question_id: str) -> dict[str, Any]:
        """读取题目并将持久化的 JSON 字段还原为列表。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM questions WHERE id = ?", (question_id,)
            ).fetchone()
        if not row:
            raise KeyError("题目不存在。")
        return self._question(dict(row))

    def save_learning_step(
        self,
        session_id: str,
        position: int,
        knowledge_point_id: str,
        step: dict[str, Any],
    ) -> dict[str, Any]:
        """原子保存课程、题目及其所属的渐进式学习步骤。"""

        lesson = {
            **step["lesson"],
            "id": _id("lesson"),
            "session_id": session_id,
        }
        questions = [
            {
                **raw_question,
                "id": _id("q"),
                "session_id": session_id,
                "knowledge_point_ids": [knowledge_point_id],
            }
            for raw_question in step["questions"]
        ]
        if not questions:
            raise ValueError("学习步骤至少需要一道题。")
        step_id = _id("step")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO lessons
                   (id, session_id, title, learning_objectives_json, explanation,
                    example, common_mistakes_json, source_pages_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lesson["id"],
                    session_id,
                    lesson["title"],
                    _json(lesson["learning_objectives"]),
                    lesson["explanation"],
                    lesson["example"],
                    _json(lesson["common_mistakes"]),
                    _json(lesson["source_pages"]),
                ),
            )
            for question in questions:
                connection.execute(
                    """INSERT INTO questions
                       (id, session_id, knowledge_point_ids_json, question_type,
                        difficulty, question, reference_answer, rubric_json, hints_json,
                        common_errors_json, source_pages_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        question["id"],
                        session_id,
                        _json(question["knowledge_point_ids"]),
                        question["question_type"],
                        question["difficulty"],
                        question["question"],
                        question["reference_answer"],
                        _json(question["rubric"]),
                        _json(question["hints"]),
                        _json(question["common_errors"]),
                        _json(question["source_pages"]),
                    ),
                )
            connection.execute(
                """INSERT INTO learning_steps
                   (id, session_id, position, knowledge_point_id, lesson_id,
                    question_id, status, correction, created_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'active', NULL, ?, NULL)""",
                (
                    step_id,
                    session_id,
                    position,
                    knowledge_point_id,
                    lesson["id"],
                    questions[0]["id"],
                    utc_now().isoformat(),
                ),
            )
            connection.executemany(
                """INSERT INTO learning_step_questions
                   (step_id, question_id, position) VALUES (?, ?, ?)""",
                [
                    (step_id, question["id"], question_position)
                    for question_position, question in enumerate(questions, start=1)
                ],
            )
        return self.get_learning_step(step_id)

    def get_learning_step(self, step_id: str) -> dict[str, Any]:
        """读取学习步骤并组装课程、题目和知识点。"""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM learning_steps WHERE id = ?", (step_id,)
            ).fetchone()
        if not row:
            raise KeyError("学习步骤不存在。")
        return self._learning_step(dict(row))

    def get_step_by_question(self, question_id: str) -> dict[str, Any]:
        """通过题目反查其所属学习步骤。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT s.* FROM learning_steps s
                   JOIN learning_step_questions sq ON sq.step_id = s.id
                   WHERE sq.question_id = ?""",
                (question_id,),
            ).fetchone()
        if not row:
            raise KeyError("题目不属于渐进式学习步骤。")
        return self._learning_step(dict(row))

    def get_active_step(self, session_id: str) -> dict[str, Any] | None:
        """返回会话最近的未完成步骤。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM learning_steps
                   WHERE session_id = ? AND status = 'active'
                   ORDER BY position DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        return self._learning_step(dict(row)) if row else None

    def get_latest_step(self, session_id: str) -> dict[str, Any] | None:
        """返回会话中位置最靠后的学习步骤。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT * FROM learning_steps
                   WHERE session_id = ? ORDER BY position DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
        return self._learning_step(dict(row)) if row else None

    def question_has_attempt(self, question_id: str, user_id: str) -> bool:
        """检查学习者是否已提交过指定题目。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM attempts
                   WHERE question_id = ? AND user_id = ? LIMIT 1""",
                (question_id, user_id),
            ).fetchone()
        return row is not None

    def finalize_step_if_ready(self, step_id: str, user_id: str) -> tuple[bool, int]:
        """在所有题目均已作答后完成步骤，并返回剩余题数。"""

        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT sq.question_id, a.assessment_json
                   FROM learning_step_questions sq
                   LEFT JOIN attempts a
                     ON a.question_id = sq.question_id AND a.user_id = ?
                   WHERE sq.step_id = ?
                   ORDER BY sq.position""",
                (user_id, step_id),
            ).fetchall()
            if not rows:
                raise KeyError("学习步骤没有关联题目。")
            remaining = sum(row["assessment_json"] is None for row in rows)
            if remaining:
                return False, remaining

            assessments = [_loads(row["assessment_json"], {}) for row in rows]
            correction = "\n".join(
                assessment["correction"]
                for assessment in assessments
                if assessment.get("correction")
            )
            cursor = connection.execute(
                """UPDATE learning_steps
                   SET status = 'completed', correction = ?, completed_at = ?
                   WHERE id = ? AND status = 'active'""",
                (correction, utc_now().isoformat(), step_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("该学习步骤已经完成。")
        return True, 0

    def get_next_untaught_point(self, session_id: str) -> dict[str, Any] | None:
        """返回当前会话尚未生成学习步骤的下一个知识点。"""

        session = self.get_session(session_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT k.* FROM knowledge_points k
                   WHERE k.section_id = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM learning_steps s
                       WHERE s.session_id = ? AND s.knowledge_point_id = k.id
                     )
                   ORDER BY k.rowid LIMIT 1""",
                (session["section_id"], session_id),
            ).fetchone()
        return self._knowledge_point(dict(row)) if row else None

    def next_step_position(self, session_id: str) -> int:
        """计算会话内下一个从一开始的步骤序号。"""

        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT COALESCE(MAX(position), 0) + 1 AS next_position
                   FROM learning_steps WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
        return int(row["next_position"])

    def get_latest_assessment(self, session_id: str) -> dict[str, Any] | None:
        """聚合最近完成步骤的题目评估，为下一步自适应教学提供上下文。"""

        with self.database.connect() as connection:
            step = connection.execute(
                """SELECT id FROM learning_steps
                   WHERE session_id = ? AND status = 'completed'
                   ORDER BY position DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            if not step:
                return None
            rows = connection.execute(
                """SELECT a.assessment_json
                   FROM learning_step_questions sq
                   JOIN attempts a ON a.question_id = sq.question_id
                   WHERE sq.step_id = ?
                   ORDER BY sq.position""",
                (step["id"],),
            ).fetchall()
        assessments = [_loads(row["assessment_json"], {}) for row in rows]
        if not assessments:
            return None
        return {
            "score": sum(float(item.get("score", 0)) for item in assessments)
            / len(assessments),
            "is_correct": all(bool(item.get("is_correct")) for item in assessments),
            "misconception_codes": sorted(
                {
                    code
                    for item in assessments
                    for code in item.get("misconception_codes", [])
                }
            ),
            "corrections": [
                item["correction"] for item in assessments if item.get("correction")
            ],
            "question_assessments": assessments,
        }

    def complete_session(self, session_id: str) -> None:
        """将学习会话标记为已完成。"""

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE learning_sessions SET status = 'completed' WHERE id = ?",
                (session_id,),
            )

    def _learning_step(self, item: dict[str, Any]) -> dict[str, Any]:
        """为步骤记录加载并组装全部关联实体。"""

        with self.database.connect() as connection:
            lesson_row = connection.execute(
                "SELECT * FROM lessons WHERE id = ?", (item["lesson_id"],)
            ).fetchone()
            question_rows = connection.execute(
                """SELECT q.* FROM questions q
                   JOIN learning_step_questions sq ON sq.question_id = q.id
                   WHERE sq.step_id = ? ORDER BY sq.position""",
                (item["id"],),
            ).fetchall()
            point_row = connection.execute(
                "SELECT * FROM knowledge_points WHERE id = ?", (item["knowledge_point_id"],)
            ).fetchone()
        if not lesson_row or not question_rows or not point_row:
            raise KeyError("学习步骤关联的数据不存在。")
        return {
            **item,
            "lesson": self._lesson(dict(lesson_row)),
            "questions": [self._question(dict(row)) for row in question_rows],
            "knowledge_point": self._knowledge_point(dict(point_row)),
        }

    @staticmethod
    def _lesson(item: dict[str, Any]) -> dict[str, Any]:
        """将课程记录中的 JSON 列转换回公开字段。"""

        for source, target in (
            ("learning_objectives_json", "learning_objectives"),
            ("common_mistakes_json", "common_mistakes"),
            ("source_pages_json", "source_pages"),
        ):
            item[target] = _loads(item.pop(source), [])
        return item

    @staticmethod
    def _question(item: dict[str, Any]) -> dict[str, Any]:
        """将题目记录中的 JSON 列转换回公开字段。"""

        for source, target in (
            ("knowledge_point_ids_json", "knowledge_point_ids"),
            ("rubric_json", "rubric"),
            ("hints_json", "hints"),
            ("common_errors_json", "common_errors"),
            ("source_pages_json", "source_pages"),
        ):
            item[target] = _loads(item.pop(source), [])
        return item

    def save_attempt_and_update_mastery(
        self,
        question: dict[str, Any],
        user_id: str,
        answer: str,
        used_hint: bool,
        assessment: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """原子保存答题评估，并用加权规则更新相关知识点掌握度。"""

        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO attempts
                   (id, question_id, user_id, answer, used_hint, score, assessment_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _id("attempt"),
                    question["id"],
                    user_id,
                    answer,
                    int(used_hint),
                    assessment["score"],
                    _json(assessment),
                    now.isoformat(),
                ),
            )

            for point_id in question["knowledge_point_ids"]:
                row = connection.execute(
                    """SELECT * FROM mastery_records
                       WHERE user_id = ? AND knowledge_point_id = ?""",
                    (user_id, point_id),
                ).fetchone()
                old = dict(row) if row else None
                attempt_count = (old["attempt_count"] if old else 0) + 1
                adjusted_score = float(assessment["score"]) * (0.85 if used_hint else 1.0)
                mastery_score = (
                    adjusted_score * 0.6
                    if not old
                    else old["mastery_score"] * 0.65 + adjusted_score * 0.35
                )
                mastery_score = max(0.0, min(1.0, mastery_score))
                correct_streak = (
                    (old["correct_streak"] if old else 0) + 1
                    if assessment["is_correct"] and not used_hint
                    else 0
                )
                misconceptions = set(_loads(old["misconceptions_json"], []) if old else [])
                misconceptions.update(assessment.get("misconception_codes", []))

                if mastery_score < 0.45 or not assessment["is_correct"]:
                    status = "needs_remediation"
                elif attempt_count >= 2 and correct_streak >= 2 and mastery_score >= 0.75:
                    status = "provisionally_mastered"
                else:
                    status = "learning"
                next_review = (
                    now + timedelta(days=1) if status == "provisionally_mastered" else None
                )

                connection.execute(
                    """INSERT INTO mastery_records
                       (user_id, knowledge_point_id, mastery_score, status, attempt_count,
                        correct_streak, misconceptions_json, last_reviewed_at, next_review_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, knowledge_point_id) DO UPDATE SET
                         mastery_score=excluded.mastery_score,
                         status=excluded.status,
                         attempt_count=excluded.attempt_count,
                         correct_streak=excluded.correct_streak,
                         misconceptions_json=excluded.misconceptions_json,
                         last_reviewed_at=excluded.last_reviewed_at,
                         next_review_at=excluded.next_review_at""",
                    (
                        user_id,
                        point_id,
                        mastery_score,
                        status,
                        attempt_count,
                        correct_streak,
                        _json(sorted(misconceptions)),
                        now.isoformat(),
                        next_review.isoformat() if next_review else None,
                    ),
                )
        return self.get_progress(user_id)

    def get_progress(
        self, user_id: str, section_id: str | None = None
    ) -> list[dict[str, Any]]:
        """读取学习者的掌握记录，可选限定到一个章节。"""

        where = "WHERE m.user_id = ?"
        parameters: list[str] = [user_id]
        if section_id:
            where += " AND k.section_id = ?"
            parameters.append(section_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""SELECT m.*, k.name AS knowledge_point_name
                   FROM mastery_records m
                   JOIN knowledge_points k ON k.id = m.knowledge_point_id
                   {where} ORDER BY m.last_reviewed_at DESC""",
                parameters,
            ).fetchall()
        result = []
        for raw in rows:
            item = dict(raw)
            item["misconceptions"] = _loads(item.pop("misconceptions_json"), [])
            result.append(item)
        return result

    @staticmethod
    def _knowledge_point(item: dict[str, Any]) -> dict[str, Any]:
        """将知识点记录中的 JSON 列转换回领域字段。"""

        for source, target in (
            ("learning_objectives_json", "learning_objectives"),
            ("prerequisites_json", "prerequisites"),
            ("misconceptions_json", "common_misconceptions"),
            ("source_pages_json", "source_pages"),
        ):
            item[target] = _loads(item.pop(source), [])
        return item

