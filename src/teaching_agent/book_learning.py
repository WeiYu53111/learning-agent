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
    Navigation,
    Questions,
    Relations,
)
from .knowledge import check_refs, quality_report
from .models import AppError, Draft
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
        return rows[0] if rows else None

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
            return contract.model_validate(cached)
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
                    "candidate": candidate.model_dump() if candidate else None,
                    "errors": errors,
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

    @staticmethod
    def navigation_errors(nav, chunks):
        keys = [p.key for p in nav.points]
        errors = [] if len(keys) == len(set(keys)) else ["知识点 key 重复。"]
        identities = [knowledge_key("", p) for p in nav.points]
        if len(identities) != len(set(identities)):
            errors.append("相同标题和原文位置的知识点重复，请合并。")
        chunk_map = {c.id: c for c in chunks}
        covered, mapped = set(), set()
        for point in nav.points:
            errors.extend(check_refs(point.source_refs, chunk_map))
        for item in nav.items:
            errors.extend(check_refs(item.source_refs, chunk_map))
            if set(item.point_keys) - set(keys):
                errors.append("内容清单引用未知知识点。")
            mapped.update(item.point_keys)
            covered.update(ref.chunk_id for ref in item.source_refs)
        if set(chunk_map) - covered:
            errors.append("原文片段没有内容清单：" + ",".join(sorted(set(chunk_map) - covered)))
        if set(keys) - mapped:
            errors.append("知识点没有关联内容清单。")
        return errors

    def index(self, book_id, emit=lambda _: None, new=False):
        book = self.app.book(book_id)
        self.ensure_search(book)
        pending = self.store.rows(
            "SELECT * FROM book_indexes WHERE book_id=? AND status='building' "
            "ORDER BY created_at DESC",
            (book_id,),
        )
        active = self.active(book_id)
        if pending:
            owner = pending[0]["id"]
        elif active and not new:
            emit("全书结构已完成，复用现有版本。")
            return active
        else:
            owner = uid("index")
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
                    (owner, book_id, "building", "{}", now()),
                )
        emit(
            f"全书梳理：{len(book.sections)} 个章节，{len(book.chunks)} 个文本块；"
            f"恢复命令：teach index {book_id}"
        )
        points, items, summaries = [], [], {}
        for section in book.sections:
            chunks = self.app.section_chunks(book, section.id)
            section_points = []
            for i, batch in enumerate(self.app.batches(chunks)):
                nav = self.call(
                    book,
                    owner,
                    f"{section.id}:extract:{i}",
                    "book_index",
                    {"chunks": [c.model_dump() for c in batch]},
                    Navigation,
                    lambda value, batch=batch: self.navigation_errors(value, batch),
                    emit,
                )
                mapping = {p.key: knowledge_key(section.id, p) for p in nav.points}
                for point in nav.points:
                    value = point.model_dump()
                    value.update(key=mapping[point.key], section_id=section.id)
                    points.append(value)
                    section_points.append(value)
                for item in nav.items:
                    value = item.model_dump()
                    value["point_keys"] = [mapping[k] for k in item.point_keys]
                    items.append(value)
            if not section_points:
                summaries[section.id] = "无可读取原文。"
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
        # Bounded pairs give cross-chapter nodes a chance to be linked, without a giant request.
        partitions = list(groups([self.brief(p) for p in points], 6000))
        relations = []
        for i, left in enumerate(partitions):
            for j in range(i, len(partitions)):
                subset = left if i == j else left + partitions[j]
                keys = {p["key"] for p in subset}

                def validate(value, keys=keys):
                    edges = [e.model_dump() for e in value.relations]
                    return relation_errors(edges, keys) + relation_errors(
                        relations + edges, {p["key"] for p in points}
                    )

                result = self.call(
                    book,
                    owner,
                    f"relations:{i}:{j}",
                    "book_relations",
                    {"points": subset},
                    Relations,
                    validate,
                    emit,
                )
                relations.extend(e.model_dump() for e in result.relations)
        unique = {(e["source"], e["target"], e["kind"]): e for e in relations}
        data = {
            "points": points,
            "items": items,
            "summaries": summaries,
            "relations": list(unique.values()),
            "unavailable_pages": book.unavailable_pages,
        }
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE book_indexes SET status='ready',data=? WHERE id=?", (dumps(data), owner)
            )
        emit(f"全书梳理完成：{len(points)} 个知识点；不可读取页面 {book.unavailable_pages}")
        return next(r for r in self.store.rows("SELECT * FROM book_indexes WHERE id=?", (owner,)))

    @staticmethod
    def brief(point):
        return {k: point[k] for k in ("key", "title", "description", "section_id")}

    def marks(self, book_id, index=None):
        index = index or self.active(book_id)
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
        index = index or self.active(book_id)
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
        index = index or self.active(book_id)
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

    def learn(self, book_id, section, emit=lambda _: None, new=False):
        book = self.app.book(book_id)
        index = self.active(book_id)
        if not index:
            raise AppError(f"请先完成：teach index {book_id}")
        self.app.section_chunks(book, section)
        links = self.store.rows(
            "SELECT l.* FROM lesson_links l JOIN versions v ON v.id=l.version_id "
            "WHERE l.index_id=? AND l.section_id=? ORDER BY v.created_at DESC",
            (index["id"], section),
        )
        if links and not new:
            owner = links[0]["version_id"]
        else:
            owner = self.app.create_draft(book_id, section)
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO lesson_links VALUES(?,?,?,?)", (owner, index["id"], section, "{}")
                )
        emit(f"教学版本：{owner}；恢复：teach prepare {owner}")
        self.build_lesson(owner, emit)
        return owner

    def build_lesson(self, owner, emit=lambda _: None):
        row, book, chunks = self.app.version(owner)
        if row["data"] != "{}":
            emit("复用已保存教学版本。")
            return
        link = self.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (owner,))[0]
        data = json.loads(
            self.store.rows("SELECT data FROM book_indexes WHERE id=?", (link["index_id"],))[0][
                "data"
            ]
        )
        chapter = [p for p in data["points"] if p["section_id"] == link["section_id"]]
        chapter_keys = {p["key"] for p in chapter}
        context_keys = {
            e["source"]
            for e in data["relations"]
            if e["kind"] == "prerequisite" and e["target"] in chapter_keys
        }
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
                    "chapter_summary": data["summaries"][link["section_id"]],
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
        if errors:
            self.save_step(
                owner,
                "structure:combined:rejected",
                {"candidate": structure.model_dump(), "errors": errors},
            )
            known_keys = {p["key"] for p in chapter}

            def validate_combined(candidate):
                issues = quality_report(candidate, chunks, structure_only=True)["errors"]
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
                    "chapter_summary": data["summaries"][link["section_id"]],
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
