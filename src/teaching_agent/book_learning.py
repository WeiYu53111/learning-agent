"""Resumable book navigation and on-demand lessons, independent of legacy drafts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter

from pydantic import ValidationError

from .book_models import (
    BookAnswer,
    ChapterSummary,
    LearningStructure,
    Questions,
)
from .extraction import SemanticExtractor, is_current, lesson_chunks, teaching_errors
from .extraction_models import PIPELINE
from .knowledge import check_refs, quality_report
from .models import AppError, Draft
from .relation_planner import candidate_pairs as candidate_pairs
from .relation_planner import relation_batches as relation_batches
from .storage import dumps, now, uid


def tokens(text):
    text = text.casefold()
    result = re.findall(r"[a-z0-9_]+", text)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(run[i : i + 2] for i in range(len(run) - 1))
        result.extend(run)
    return result


def knowledge_key(section, point):
    identity = [section, point.title, sorted({r.chunk_id for r in point.source_refs})]
    return "k_" + hashlib.sha256(dumps(identity).encode()).hexdigest()[:16]


def fingerprint(point):
    content = {k: point[k] for k in ("title", "description", "source_refs")}
    return hashlib.sha256(dumps(content).encode()).hexdigest()


def groups(values, budget=10000):
    batch, size = [], 0
    for value in values:
        count = len(dumps(value))
        if batch and size + count > budget:
            yield batch
            batch, size = [], 0
        batch.append(value)
        size += count
    if batch:
        yield batch


def relation_errors(relations, keys):
    errors, graph = [], {key: [] for key in keys}
    for edge in relations:
        if edge["source"] not in keys or edge["target"] not in keys:
            errors.append("关系引用未知知识点。")
        elif edge["source"] == edge["target"]:
            errors.append("知识点不能依赖或关联自身。")
        elif edge["kind"] == "prerequisite":
            graph[edge["target"]].append(edge["source"])
    visiting, done = set(), set()

    def visit(key):
        if key in visiting:
            raise AppError("必要前置关系形成循环。")
        if key in done:
            return
        visiting.add(key)
        for pre in graph[key]:
            visit(pre)
        visiting.remove(key)
        done.add(key)

    try:
        for key in graph:
            visit(key)
    except (AppError, RecursionError):
        errors.append("必要前置关系形成循环或层级过深。")
    return errors


class BookLearning:
    def __init__(self, app):
        self.app = app
        self.store = app.store

    def available(self):
        return bool(self.store.rows("SELECT name FROM sqlite_master WHERE name='book_indexes'"))

    def active(self, book_id):
        if not self.available():
            return None
        rows = self.store.rows(
            "SELECT * FROM book_indexes WHERE book_id=? AND status='ready' "
            "ORDER BY created_at DESC,id DESC",
            (book_id,),
        )
        return next((r for r in rows if is_current(r)), None)

    def step(self, owner, key):
        rows = self.store.rows(
            "SELECT data FROM book_steps WHERE owner_id=? AND step=?", (owner, key)
        )
        return json.loads(rows[0]["data"]) if rows else None

    def save_step(self, owner, key, data):
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO book_steps VALUES(?,?,?) ON CONFLICT(owner_id,step) "
                "DO UPDATE SET data=excluded.data",
                (owner, key, dumps(data)),
            )

    def event(self, owner, kind, data):
        with self.store.connect() as conn:
            self.store.event(conn, owner, kind, data)

    def call(self, book, owner, key, operation, payload, contract, validate, emit):
        cached = self.step(owner, key)
        if cached is not None:
            emit(f"{key}：复用已保存结果")
            value = contract.model_validate(cached)
            errors = validate(value)
            if errors:
                raise AppError("已保存检查点未通过校验：" + "；".join(errors))
            return value
        gateway = self.app.gateway(book, owner)
        previous = self.step(owner, key + ":rejected")
        request = dict(payload)
        if previous:
            request.update(previous=previous["candidate"], errors=previous["errors"])
        for attempt in range(2):
            emit(
                f"{key}：等待模型{'纠正' if previous or attempt else '处理'}；"
                f"后端/模型：{gateway.model}"
            )
            started = time.monotonic()
            candidate = None
            try:
                candidate = gateway.book_operation(operation, request, contract)
                errors = validate(candidate)
                if errors:
                    raise AppError("；".join(errors))
            except (AppError, ValidationError) as exc:
                errors = [str(exc)] if isinstance(exc, AppError) else ["返回内容不符合数据契约"]
                rejected = {
                    "candidate": candidate.model_dump() if candidate else request.get("previous"),
                    "errors": errors if candidate else request.get("errors", []) + errors,
                }
                self.save_step(owner, key + ":rejected", rejected)
                self.event(
                    owner,
                    "book_operation_failed",
                    {
                        "step": key,
                        "operation": operation,
                        "model": gateway.model,
                        "elapsed_seconds": time.monotonic() - started,
                        "input_chars": len(dumps(request)),
                        **rejected,
                    },
                )
                # Network/size failures have no usable candidate; do not repeat costly requests.
                if attempt or candidate is None:
                    raise AppError(
                        f"{key} 暂停，结果已保存可恢复：" + "；".join(errors[:5])
                    ) from exc
                request = {**payload, "previous": rejected["candidate"], "errors": errors}
                continue
            elapsed = time.monotonic() - started
            self.save_step(owner, key, candidate.model_dump())
            self.event(
                owner,
                "book_operation_completed",
                {
                    "step": key,
                    "operation": operation,
                    "model": gateway.model,
                    "elapsed_seconds": elapsed,
                    "input_chars": len(dumps(request)),
                    "output_chars": len(candidate.model_dump_json()),
                },
            )
            emit(
                f"{key}：已保存；耗时 {elapsed:.1f} 秒，输入 {len(dumps(request))} 字符，"
                f"输出 {len(candidate.model_dump_json())} 字符"
            )
            return candidate

    def ensure_search(self, book):
        with self.store.connect() as conn:
            for chunk in book.chunks:
                conn.execute(
                    "INSERT OR IGNORE INTO search_chunks VALUES(?,?,?)",
                    (book.id, chunk.id, dumps(dict(Counter(tokens(chunk.text))))),
                )

    def index(self, book_id, emit=lambda _: None, new=False, section_id=None, content_only=False):
        book = self.app.book(book_id)
        # Old imports lack nested bookmark metadata; reading the outline does not
        # reparse or rewrite their original text chunks and citation coordinates.
        if not book.bookmarks:
            path = self.store.root / "documents" / f"{book.sha256}.pdf"
            if path.is_file():
                import pymupdf

                from .models import Section

                with pymupdf.open(path) as pdf:
                    book.bookmarks = [
                        Section(
                            id=f"bookmark{i}",
                            title=title,
                            level=level,
                            start_page=page,
                            end_page=page,
                            chunk_ids=[],
                        )
                        for i, (level, title, page) in enumerate(pdf.get_toc())
                        if 1 <= page <= pdf.page_count
                    ]
        self.ensure_search(book)
        pending = self.store.rows(
            "SELECT * FROM book_indexes WHERE book_id=? AND status='building' "
            "ORDER BY created_at DESC",
            (book_id,),
        )
        active = self.content_active(book_id)
        section_ids = [s.id for s in book.sections]
        if section_id and section_id not in section_ids:
            raise AppError("章节不存在。")
        wanted = section_ids[: section_ids.index(section_id) + 1] if section_id else section_ids
        baseline = json.loads(active["data"]) if active else {}
        completed = baseline.get("indexed_sections", list(baseline.get("summaries", {})))
        donors = [r for r in pending if is_current(r)]
        pending = [
            r
            for r in donors
            if json.loads(r["data"]).get("through_section") == section_id
            and "through_section" in json.loads(r["data"])
        ]
        if pending and not new:
            owner = pending[0]["id"]
        elif active and not new and set(wanted) <= set(completed):
            emit("复用已审核内容。")
            if not content_only:
                self.relations(book_id, section_id, emit=emit, index=active)
            return self.index_row(active["id"])
        else:
            owner = uid("index")
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
                    (
                        owner,
                        book_id,
                        "building",
                        dumps(
                            {
                                "pipeline": PIPELINE,
                                "through_section": section_id,
                                "requested_sections": wanted,
                                "rebuild_section": section_id if new else None,
                                "base_index": active["id"]
                                if active and (section_id or not new)
                                else None,
                            }
                        ),
                        now(),
                    ),
                )
                # Reuse durable checkpoints when narrowing an interrupted full-book run.
                # Explicit --new always regenerates the requested chapter (or full book).
                for chapter in wanted:
                    if new and (not section_id or chapter == section_id):
                        continue
                    donor = next(
                        (
                            r
                            for r in donors
                            if self.step(r["id"], f"{chapter}:{PIPELINE}:complete") is not None
                        ),
                        None,
                    )
                    if donor:
                        conn.execute(
                            "INSERT OR IGNORE INTO book_steps SELECT ?,step,data FROM book_steps "
                            "WHERE owner_id=? AND step LIKE ?",
                            (owner, donor["id"], chapter + ":%"),
                        )
        state_rows = self.store.rows(
            "SELECT status FROM index_content_state WHERE index_id=?", (owner,)
        )
        if state_rows and state_rows[0]["status"] == "ready":
            if not content_only:
                self.finish_index_relations(book_id, owner, emit)
            return self.index_row(owner)
        emit(
            f"内容梳理：处理 {wanted[0]}～{wanted[-1]}，复用已完成章节；"
            f"恢复命令：teach index {book_id}" + (f" --section {section_id}" if section_id else "")
        )
        state = json.loads(
            self.store.rows("SELECT data FROM book_indexes WHERE id=?", (owner,))[0]["data"]
        )
        base = None
        if state.get("base_index"):
            base = json.loads(
                self.store.rows("SELECT data FROM book_indexes WHERE id=?", (state["base_index"],))[
                    0
                ]["data"]
            )
        points, items, summaries, regions = [], [], {}, []
        extractor = SemanticExtractor(self, book, owner, emit)
        requested = state.get("requested_sections", wanted)
        wanted = [
            s.id
            for s in book.sections
            if s.id in requested or (base and s.id in base.get("summaries", {}))
        ]
        for section in book.sections:
            if section.id not in wanted:
                continue
            if (
                base
                and section.id in base.get("summaries", {})
                and section.id != state.get("rebuild_section")
            ):
                copied = [p for p in base["points"] if p["section_id"] == section.id]
                keys = {p["key"] for p in copied}
                points.extend(copied)
                items.extend(it for it in base["items"] if keys.intersection(it["point_keys"]))
                regions.extend(r for r in base["regions"] if r["section_id"] == section.id)
                summaries[section.id] = base["summaries"][section.id]
                continue
            result = extractor.section(section)
            section_points = result["points"]
            points.extend(section_points)
            items.extend(result["items"])
            regions.extend(result["regions"])
            if not section_points:
                summaries[section.id] = "本章没有可提取的教学正文；原文及排除理由已保留。"
                continue
            units = [self.brief(p) for p in section_points]
            level = 0
            while True:
                results = []
                for i, batch in enumerate(groups(units, 8000)):
                    value = self.call(
                        book,
                        owner,
                        f"{section.id}:summary:{level}:{i}",
                        "book_summary",
                        {"section": section.title, "content": batch},
                        ChapterSummary,
                        lambda _: [],
                        emit,
                    )
                    results.append({"summary": value.summary})
                if len(results) == 1:
                    summaries[section.id] = results[0]["summary"]
                    break
                units = results
                level += 1
        all_keys = {p["key"] for p in points}
        changed = {p["key"] for p in points if p["section_id"] == section_id}
        reused = (
            [
                e
                for e in base["relations"]
                if e["source"] in all_keys
                and e["target"] in all_keys
                and e["source"] not in changed
                and e["target"] not in changed
            ]
            if base
            else []
        )
        errors = relation_errors(reused, all_keys)
        if errors:
            raise AppError("继承关系校验失败：" + "；".join(errors))
        data = {
            "pipeline": PIPELINE,
            "regions": regions,
            "base_index": state.get("base_index"),
            "rebuild_section": state.get("rebuild_section"),
            "through_section": state.get("through_section"),
            "requested_sections": requested,
            "indexed_sections": wanted,
            "reviewed_sections": wanted,
            "points": points,
            "items": items,
            "summaries": summaries,
            "relations": reused,
            "unavailable_pages": book.unavailable_pages,
        }
        digest = hashlib.sha256(
            dumps({k: v for k, v in data.items() if k != "relations"}).encode()
        ).hexdigest()
        with self.store.connect() as conn:
            conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (dumps(data), owner))
            conn.execute(
                "INSERT INTO index_content_state(index_id,status,content_fingerprint) "
                "VALUES(?,'ready',?) ON CONFLICT(index_id) DO UPDATE SET "
                "status='ready',content_fingerprint=excluded.content_fingerprint",
                (owner, digest),
            )
        emit(f"内容整理完成：{len(points)} 个知识点；不可读取页面 {book.unavailable_pages}")
        if not content_only:
            self.finish_index_relations(book_id, owner, emit)
        return self.index_row(owner)

    def finish_index_relations(self, book_id, owner, emit):
        from .relations import RelationAnalysis

        index = self.index_row(owner)
        section = json.loads(index["data"]).get("through_section")
        # Rebuild only checks candidates touching the changed chapter. It does
        # not claim that any still-unchecked baseline candidates were checked.
        scope = "@through:" + section if section else None
        RelationAnalysis(self, index, emit).run(scope, max_depth=0 if section else 2)

    def index_row(self, identifier):
        return self.store.rows("SELECT * FROM book_indexes WHERE id=?", (identifier,))[0]

    def content_active(self, book_id):
        # Read-only commands still work before a schema-5 database has been migrated.
        if not self.store.rows("SELECT name FROM sqlite_master WHERE name='index_content_state'"):
            return self.active(book_id)
        rows = self.store.rows(
            "SELECT b.* FROM book_indexes b JOIN index_content_state c ON c.index_id=b.id "
            "WHERE b.book_id=? AND c.status='ready' ORDER BY b.created_at DESC,b.id DESC",
            (book_id,),
        )
        return next((r for r in rows if is_current(r)), None)

    def relations(
        self, book_id, section=None, max_calls=None, max_depth=2, emit=lambda _: None, index=None
    ):
        from .relations import RelationAnalysis

        index = index or self.content_active(book_id)
        if not index or not is_current(index):
            raise AppError(f"内容尚未就绪，请先恢复：teach index {book_id} --content-only")
        if section and max_calls is None:
            max_calls = 4
        return RelationAnalysis(self, index, emit).run(section, max_calls, max_depth)

    def lesson_index(self, owner):
        link = self.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (owner,))[0]
        index = self.index_row(link["index_id"])
        if self.store.rows("SELECT name FROM sqlite_master WHERE name='lesson_relation_snapshots'"):
            snapshots = self.store.rows(
                "SELECT data FROM lesson_relation_snapshots WHERE version_id=?", (owner,)
            )
            if snapshots:
                data = json.loads(index["data"])
                data["relations"] = json.loads(snapshots[0]["data"])["relations"]
                index["data"] = dumps(data)
        return index

    @staticmethod
    def brief(point):
        return {k: point[k] for k in ("key", "title", "description", "section_id")}

    def marks(self, book_id, index=None):
        index = index or self.content_active(book_id)
        if not index:
            return set()
        records = {
            (r["point_key"], r["fingerprint"])
            for r in self.store.rows("SELECT * FROM self_mastery WHERE book_id=?", (book_id,))
        }
        return {
            p["key"]
            for p in json.loads(index["data"])["points"]
            if (p["key"], fingerprint(p)) in records
        }

    def mastery(self, book_id, *, section=None, point=None, mark=True, index=None):
        index = index or self.content_active(book_id)
        if not index:
            raise AppError("请先完成 teach index BOOK。")
        selected = [
            p
            for p in json.loads(index["data"])["points"]
            if p["section_id"] == section or p["key"] == point
        ]
        if not selected:
            raise AppError("没有匹配的章节或知识点。")
        with self.store.connect() as conn:
            for value in selected:
                if mark:
                    conn.execute(
                        "INSERT OR REPLACE INTO self_mastery VALUES(?,?,?,?)",
                        (book_id, value["key"], fingerprint(value), now()),
                    )
                else:
                    conn.execute(
                        "DELETE FROM self_mastery WHERE book_id=? AND point_key=?",
                        (book_id, value["key"]),
                    )
            self.store.event(
                conn,
                book_id,
                "self_mastery",
                {"points": [p["key"] for p in selected], "mark": mark},
            )
        return selected

    def prerequisites(self, book_id, section, index=None):
        index = index or self.content_active(book_id)
        if not index:
            raise AppError("请先完成 teach index BOOK。")
        data = json.loads(index["data"])
        points = {p["key"]: p for p in data["points"]}
        selected = {k for k, p in points.items() if p["section_id"] == section}
        if not selected:
            raise AppError("章节不存在或没有可学习的知识点。")
        needed = set(selected)
        while True:
            extra = {
                e["source"]
                for e in data["relations"]
                if e["kind"] == "prerequisite" and e["target"] in needed
            }
            if extra <= needed:
                break
            needed |= extra
        known = self.marks(book_id, index) | self.verified(book_id, index)
        return [points[k] for k in points if k in needed - selected - known]

    def verified(self, book_id, index):
        result = set()
        for link in self.store.rows(
            "SELECT l.* FROM lesson_links l JOIN versions v ON v.id=l.version_id "
            "WHERE v.book_id=? AND l.index_id=?",
            (book_id, index["id"]),
        ):
            row, _, _ = self.app.version(link["version_id"])
            if row["data"] == "{}":
                continue
            draft = Draft.model_validate_json(row["data"])
            progress = self.app.progress(row["id"])
            evidence = {}
            for point in json.loads(link["data"]).get("point_map", []):
                current = next((p for p in draft.points if p.key == point["draft_point"]), None)
                active = [o for o in current.objectives if not o.excluded_reason] if current else []
                mastered = bool(active) and all(
                    progress[o.key]["status"] == "provisionally_mastered" for o in active
                )
                for key in point["knowledge_keys"]:
                    evidence.setdefault(key, []).append(mastered)
            result.update(key for key, states in evidence.items() if all(states))
        return result

    def learn(
        self,
        book_id,
        section,
        emit=lambda _: None,
        new=False,
        allow_partial_relations=False,
        choose_partial=None,
    ):
        from .relations import RelationFailure, RelationsPaused

        book = self.app.book(book_id)
        self.app.section_chunks(book, section)
        links = self.store.rows(
            "SELECT l.* FROM lesson_links l JOIN versions v ON v.id=l.version_id "
            "WHERE v.book_id=? AND l.section_id=? ORDER BY v.created_at DESC,v.id DESC",
            (book_id, section),
        )
        links = [
            link
            for link in links
            if is_current(self.index_row(link["index_id"]))
            or self.store.get("versions", link["version_id"])["data"] != "{}"
        ]
        if links and not new:
            owner = links[0]["version_id"]
        else:
            index = self.content_active(book_id)
            if not index:
                raise AppError(f"内容尚未就绪，请先恢复：teach index {book_id} --content-only")
            data = json.loads(index["data"])
            order = [s.id for s in book.sections]
            required = order[: order.index(section) + 1]
            if not set(required) <= set(data.get("indexed_sections", data.get("summaries", {}))):
                raise AppError(
                    f"前序章节内容尚未就绪，请运行：teach index {book_id} "
                    f"--section {section} --content-only"
                )
            if not any(p["section_id"] == section for p in json.loads(index["data"])["points"]):
                raise AppError("所选范围没有可学习知识点（目录或出版信息已排除）。")
            while True:
                try:
                    coverage = self.relations(book_id, section, emit=emit, index=index)
                except RelationFailure as exc:
                    from .relations import RelationAnalysis

                    analysis = RelationAnalysis(self, index, emit)
                    task = analysis.task(section, 2)
                    coverage = analysis.coverage(task)
                    coverage["last_error"] = str(exc)
                    if not allow_partial_relations and choose_partial is None:
                        raise
                if coverage["status"] == "candidate_complete" or allow_partial_relations:
                    break
                choice = choose_partial(coverage) if choose_partial else "exit"
                if choice == "continue":
                    # A depth-limited traversal needs a larger scope as well as more calls.
                    if coverage["unexpanded"]:
                        with self.store.connect() as conn:
                            conn.execute(
                                "UPDATE relation_tasks SET max_depth=max_depth+1 WHERE id=?",
                                (coverage["task_id"],),
                            )
                    continue
                if choice == "partial":
                    allow_partial_relations = True
                    break
                raise RelationsPaused(
                    "关系分析尚未完整；请运行 teach relations BOOK --section SECTION "
                    "继续，或显式传入 --allow-partial-relations。"
                )
            index = self.index_row(index["id"])
            data = json.loads(index["data"])
            state = self.store.rows(
                "SELECT * FROM index_content_state WHERE index_id=?", (index["id"],)
            )[0]
            allowed = {p["key"] for p in data["points"] if p["section_id"] in required}
            data["relations"] = [
                e for e in data["relations"] if e["source"] in allowed and e["target"] in allowed
            ]
            chapter_keys = {p["key"] for p in data["points"] if p["section_id"] == section}
            used_keys = set(chapter_keys)
            while True:
                ancestors = {
                    e["source"]
                    for e in data["relations"]
                    if e["kind"] == "prerequisite" and e["target"] in used_keys
                }
                if ancestors <= used_keys:
                    break
                used_keys |= ancestors
            snapshot = dict(
                relations=[
                    e
                    for e in data["relations"]
                    if e["source"] in used_keys and e["target"] in used_keys
                ],
                coverage=coverage,
                origin="explicit",
                allow_partial=allow_partial_relations,
                prerequisite_context=[
                    self.brief(p) for p in data["points"] if p["key"] in used_keys - chapter_keys
                ],
            )
            # Version, link, and snapshot share one commit. Backend binding may precede it.
            owner = uid("ver")
            model = self.app.gateway(book, owner).model
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        owner,
                        book_id,
                        section,
                        "draft",
                        0,
                        "{}",
                        None,
                        None,
                        "{}",
                        None,
                        model,
                        now(),
                    ),
                )
                conn.execute(
                    "INSERT INTO lesson_links VALUES(?,?,?,?)", (owner, index["id"], section, "{}")
                )
                conn.execute(
                    "INSERT INTO lesson_relation_snapshots VALUES(?,?,?,?)",
                    (owner, index["id"], state["graph_revision"], dumps(snapshot)),
                )
        emit(f"教学版本：{owner}；恢复：teach prepare {owner}")
        self.build_lesson(owner, emit)
        return owner

    def freeze_lesson(self, owner, link):
        if self.store.rows("SELECT 1 FROM lesson_relation_snapshots WHERE version_id=?", (owner,)):
            return
        index = self.index_row(link["index_id"])
        data = json.loads(index["data"])
        if not is_current(index):
            return
        section = link["section_id"].partition("@")[0]
        order = [s.id for s in self.app.book(index["book_id"]).sections]
        prefix = order[: order.index(section) + 1]
        allowed = {p["key"] for p in data["points"] if p["section_id"] in prefix}
        data["relations"] = [
            e for e in data["relations"] if e["source"] in allowed and e["target"] in allowed
        ]
        from .relations import RelationAnalysis

        analysis = RelationAnalysis(self, index)
        tasks = self.store.rows(
            "SELECT * FROM relation_tasks WHERE index_id=? AND section_id=''",
            (index["id"],),
        )
        coverage = analysis.coverage(tasks[0]) if tasks else {"status": "legacy_unknown"}
        with self.store.connect() as conn:
            state = conn.execute(
                "SELECT graph_revision FROM index_content_state WHERE index_id=?", (index["id"],)
            ).fetchone()
            roots = {p["key"] for p in data["points"] if p["section_id"] == section}
            used = set(roots)
            while True:
                parents = {
                    e["source"]
                    for e in data["relations"]
                    if e["kind"] == "prerequisite" and e["target"] in used
                }
                if parents <= used:
                    break
                used |= parents
            snapshot = dict(
                relations=[
                    e for e in data["relations"] if e["source"] in used and e["target"] in used
                ],
                coverage=coverage,
                origin="explicit",
                allow_partial=False,
            )
            conn.execute(
                "INSERT INTO lesson_relation_snapshots VALUES(?,?,?,?)",
                (owner, index["id"], state[0] if state else 0, dumps(snapshot)),
            )

    def build_lesson(self, owner, emit=lambda _: None):
        row, book, chunks = self.app.version(owner)
        if row["data"] != "{}":
            emit("复用已保存教学版本。")
            return
        link = self.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (owner,))[0]
        self.freeze_lesson(owner, link)
        data = json.loads(self.lesson_index(owner)["data"])
        if data.get("pipeline") != PIPELINE:
            raise AppError("旧提取流程已废弃，请重新运行 teach index BOOK 后创建教学版本。")
        actual_section = link["section_id"].partition("@")[0]
        chunks = lesson_chunks(chunks, data, actual_section)
        selected_ids = {c.id for c in chunks}
        chapter = [
            p
            for p in data["points"]
            if p["section_id"] == actual_section
            and any(r["chunk_id"] in selected_ids for r in p["source_refs"])
        ]
        if not chapter:
            raise AppError("所选范围没有可学习知识点（目录或出版信息已排除）。")
        chapter_keys = {p["key"] for p in chapter}
        context_keys = set(chapter_keys)
        while True:
            parents = {
                e["source"]
                for e in data["relations"]
                if e["kind"] == "prerequisite" and e["target"] in context_keys
            }
            if parents <= context_keys:
                break
            context_keys |= parents
        context_keys -= chapter_keys
        prerequisite_context = [self.brief(p) for p in data["points"] if p["key"] in context_keys]
        structures = []
        point_map = []
        # All chapter structures finish before any question generation begins.
        for i, batch in enumerate(self.app.batches(chunks)):
            chunk_ids = {c.id for c in batch}
            selected = [
                p for p in chapter if any(r["chunk_id"] in chunk_ids for r in p["source_refs"])
            ]

            def validate(value, batch=batch, selected=selected):
                errors = quality_report(value, batch, structure_only=True)["errors"]
                errors.extend(teaching_errors(value, batch, data["regions"]))
                mapped = {k for p in value.points for k in p.knowledge_keys}
                expected = {p["key"] for p in selected}
                if expected - mapped:
                    errors.append("教学结构遗漏本批知识点。")
                if mapped - expected:
                    errors.append("教学结构引用未知章节知识点。")
                return errors

            structure = self.call(
                book,
                owner,
                f"structure:{i}",
                "learning_structure",
                {
                    "chunks": [c.model_dump() for c in batch],
                    "knowledge_points": selected,
                    "source_regions": [r for r in data["regions"] if r["chunk_id"] in chunk_ids],
                    "chapter_summary": data["summaries"][actual_section],
                    "prerequisite_knowledge": prerequisite_context,
                    "relations": [
                        e for e in data["relations"] if e["target"] in {p["key"] for p in selected}
                    ],
                },
                LearningStructure,
                validate,
                emit,
            )
            value = structure.model_dump()
            prefix = f"b{i}_"
            for point in value["points"]:
                point["key"] = prefix + point["key"]
                point_map.append(
                    {"draft_point": point["key"], "knowledge_keys": point["knowledge_keys"]}
                )
                for obj in point["objectives"]:
                    obj["key"] = prefix + obj["key"]
                    obj["prerequisites"] = [prefix + k for k in obj["prerequisites"]]
            for item in value["content_items"]:
                item["key"] = prefix + item["key"]
                item["objective_keys"] = [prefix + k for k in item["objective_keys"]]
            for misconception in value["misconceptions"]:
                misconception["code"] = prefix + misconception["code"]
                misconception["objective_keys"] = [
                    prefix + k for k in misconception["objective_keys"]
                ]
            structures.append(value)
        combined = {
            field: [item for part in structures for item in part[field]]
            for field in ("points", "content_items", "misconceptions", "notes")
        }
        structure = LearningStructure.model_validate(combined)
        # Resolve within-chapter navigation prerequisites into objective-level dependencies.
        by_knowledge = {}
        for point in structure.points:
            for key in point.knowledge_keys:
                by_knowledge.setdefault(key, []).extend(
                    o for o in point.objectives if not o.excluded_reason
                )
        for edge in data["relations"]:
            if (
                edge["kind"] == "prerequisite"
                and edge["source"] in by_knowledge
                and edge["target"] in by_knowledge
            ):
                for target in by_knowledge[edge["target"]]:
                    for source in by_knowledge[edge["source"]]:
                        if source.key != target.key and source.key not in target.prerequisites:
                            target.prerequisites.append(source.key)
        errors = quality_report(structure, chunks, structure_only=True)["errors"]
        errors.extend(teaching_errors(structure, chunks, data["regions"]))
        if errors:
            self.save_step(
                owner,
                "structure:combined:rejected",
                {"candidate": structure.model_dump(), "errors": errors},
            )
            known_keys = {p["key"] for p in chapter}

            def validate_combined(candidate):
                issues = quality_report(candidate, chunks, structure_only=True)["errors"]
                issues.extend(teaching_errors(candidate, chunks, data["regions"]))
                if {k for p in candidate.points for k in p.knowledge_keys} != known_keys:
                    issues.append("章节知识点必须完整覆盖，不能增加或删除。")
                return issues

            structure = self.call(
                book,
                owner,
                "structure:combined",
                "learning_structure",
                {
                    "chunks": [c.model_dump() for c in chunks],
                    "knowledge_points": chapter,
                    "source_regions": [
                        r for r in data["regions"] if r["section_id"] == actual_section
                    ],
                    "previous": structure.model_dump(),
                    "errors": errors,
                    "relations": data["relations"],
                },
                LearningStructure,
                validate_combined,
                emit,
            )
        point_map = [
            {"draft_point": p.key, "knowledge_keys": p.knowledge_keys} for p in structure.points
        ]
        self.save_step(owner, "structure:complete", structure.model_dump())
        objective_list = list(structure.objectives().values())
        question_map = {}
        chunk_map = {c.id: c for c in chunks}
        for i, group in enumerate(groups([o.model_dump() for o in objective_list], 5000)):
            keys = {o["key"] for o in group}
            refs = {r["chunk_id"] for o in group for r in o["source_refs"]}

            def validate_questions(value, keys=keys):
                actual = [q.objective_key for q in value.questions]
                errors = []
                if len(actual) != len(set(actual)) or set(actual) != keys:
                    errors.append("题目必须完整且唯一覆盖本组目标，不能包含额外目标。")
                codes = {m.code for m in structure.misconceptions}
                for entry in value.questions:
                    errors.extend(check_refs(entry.question.source_refs, chunk_map))
                    if set(entry.question.targeted_misconception_codes) - codes:
                        errors.append("题目引用未知误区码。")
                return errors

            questions = self.call(
                book,
                owner,
                f"questions:{i}",
                "learning_questions",
                {
                    "objectives": group,
                    "chapter_objectives": [
                        {"key": o.key, "description": o.description} for o in objective_list
                    ],
                    "chapter_summary": data["summaries"][actual_section],
                    "prerequisite_knowledge": prerequisite_context,
                    "prerequisites": [
                        o.model_dump()
                        for o in objective_list
                        if o.key in {k for obj in group for k in obj["prerequisites"]}
                    ],
                    "chunks": [chunk_map[k].model_dump() for k in chunk_map if k in refs],
                    "misconceptions": [m.model_dump() for m in structure.misconceptions],
                },
                Questions,
                validate_questions,
                emit,
            )
            question_map.update(
                {q.objective_key: q.question.model_dump() for q in questions.questions}
            )
        value = structure.model_dump()
        for point in value["points"]:
            del point["knowledge_keys"]
            for obj in point["objectives"]:
                obj["sample_question"] = question_map[obj["key"]]
        draft = Draft.model_validate(value)
        errors = quality_report(draft, chunks)["errors"]
        if errors:
            raise AppError("完整教学草稿校验失败：" + "；".join(errors[:5]))
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE lesson_links SET data=? WHERE version_id=?",
                (dumps({"point_map": point_map}), owner),
            )
        self.app.save_draft(owner, draft, "按章两阶段生成完成")
        emit("章节教学内容已保存，继续完整审核。")

    def search(self, book, question):
        self.ensure_search(book)
        query = Counter(tokens(question))
        indexed = {
            r["chunk_id"]: json.loads(r["data"])
            for r in self.store.rows("SELECT * FROM search_chunks WHERE book_id=?", (book.id,))
        }
        df = Counter(term for counts in indexed.values() for term in counts)
        boosts = Counter()
        index = self.active(book.id)
        if index:
            for point in json.loads(index["data"])["points"]:
                overlap = set(tokens(point["title"] + " " + point["description"])) & query.keys()
                if overlap:
                    for ref in point["source_refs"]:
                        boosts[ref["chunk_id"]] += len(overlap) * 0.5
        for section in book.sections:
            overlap = set(tokens(section.title)) & query.keys()
            for key in section.chunk_ids:
                boosts[key] += len(overlap) * 0.2

        def score(chunk):
            counts = indexed.get(chunk.id, {})
            length = max(1, sum(counts.values()))
            return (
                sum(
                    (1 + math.log1p(len(indexed) / (1 + df[t])))
                    * counts.get(t, 0)
                    / (counts.get(t, 0) + 0.5 + length / 500)
                    for t in query
                )
                + boosts[chunk.id]
            )

        ranked = sorted(enumerate(book.chunks), key=lambda pair: (-score(pair[1]), pair[0]))
        selected, seen, size = [], set(), 0
        # Reserve first for matches across chapters, then add adjacent source context.
        hits = [(i, c) for i, c in ranked[:8] if score(c) > 0]
        candidates = [c for _, c in hits]
        for i, _ in hits:
            candidates.extend(book.chunks[j] for j in (i - 1, i + 1) if 0 <= j < len(book.chunks))
        for chunk in candidates:
            if chunk.id not in seen and size + len(chunk.text) <= 22000:
                selected.append(chunk)
                seen.add(chunk.id)
                size += len(chunk.text)
        return selected

    def ask(self, book_id, question, emit=lambda _: None, session_id=None):
        if not question.strip():
            raise AppError("请输入问题。")
        book = self.app.book(book_id)
        if (
            session_id
            and self.app.version(self.app.session(session_id)[0]["version_id"])[1].id != book_id
        ):
            raise AppError("会话与教材不匹配。")
        owner = uid("ask")
        if session_id and not self.app.backends.selected:
            bindings = self.store.rows(
                "SELECT name,spec FROM backend_bindings WHERE owner_id=?", (session_id,)
            )
            if bindings:
                with self.store.connect() as conn:
                    conn.execute(
                        "INSERT INTO backend_bindings VALUES(?,?,?)",
                        (owner, bindings[0]["name"], bindings[0]["spec"]),
                    )
        chunks = self.search(book, question)
        ready = self.active(book_id) is not None
        if not ready:
            emit("全书知识结构尚未完成，使用原文检索回答。")
        pending = {
            "question": question,
            "chunks": [c.model_dump() for c in chunks],
            "answer": None,
            "status": "pending",
            "index_ready": ready,
        }
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO book_questions VALUES(?,?,?,?,?)",
                (owner, book_id, session_id, dumps(pending), now()),
            )
        if not chunks:
            answer = BookAnswer(
                sufficient=False, limitation="当前检索未找到充分依据，请补充教材术语或问题背景。"
            )
        else:
            chunk_map = {c.id: c for c in chunks}

            def validate(value):
                if not value.sufficient:
                    return (
                        []
                        if not value.parts and value.limitation.strip()
                        else ["依据不足时应返回空 parts 并说明限制。"]
                    )
                if not value.parts:
                    return ["有效答案必须包含带原文引用的段落。"]
                return [
                    error
                    for part in value.parts
                    for error in check_refs(part.source_refs, chunk_map)
                ]

            try:
                answer = self.call(
                    book,
                    owner,
                    "answer",
                    "book_answer",
                    {"question": question, "chunks": [c.model_dump() for c in chunks]},
                    BookAnswer,
                    validate,
                    emit,
                )
            except AppError as exc:
                pending.update(status="failed", error=str(exc))
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE book_questions SET data=? WHERE id=?", (dumps(pending), owner)
                    )
                raise

        record = {
            "question": question,
            "chunks": [c.model_dump() for c in chunks],
            "answer": answer.model_dump(),
            "status": "answered" if answer.sufficient else "insufficient",
            "index_ready": ready,
            "backend": self.store.rows(
                "SELECT name,spec FROM backend_bindings WHERE owner_id=?", (owner,)
            ),
        }
        with self.store.connect() as conn:
            conn.execute("UPDATE book_questions SET data=? WHERE id=?", (dumps(record), owner))
        return record
