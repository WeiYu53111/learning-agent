import copy
import json

import pytest

from teaching_agent.book_learning import relation_errors
from teaching_agent.book_models import BookAnswer
from teaching_agent.cli import chat, dispatch, parser, prerequisite_choices
from teaching_agent.models import AppError, Book, Chunk, Section
from teaching_agent.storage import dumps


@pytest.fixture
def book_id(app):
    chunks = [
        Chunk(id=f"c{i}", page=i, text=text)
        for i, text in enumerate(
            [
                "上下文包含用户消息和模型回复。",
                "缓存复用共同的消息前缀，提高处理速度。",
                "工具定义改变时，缓存前缀可能发生变化。",
            ],
            1,
        )
    ]
    book = Book(
        id="book_test",
        filename="test.pdf",
        sha256="a" * 64,
        page_count=4,
        sections=[
            Section(
                id=f"s{i}",
                title=f"第{i}章 缓存" if i > 1 else "上下文",
                start_page=i,
                end_page=i,
                chunk_ids=[f"c{i}"],
            )
            for i in range(1, 4)
        ],
        chunks=chunks,
        unavailable_pages=[4],
        demo=True,
    )
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO books VALUES(?,?,?)", (book.id, book.sha256, book.model_dump_json())
        )
    return book.id


def test_index_is_complete_resumable_and_model_free_after_completion(app, book_id):
    calls = []
    original = app.injected_gateway.book_operation

    def operation(op, payload, contract):
        calls.append(op)
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    index = app.library.index(book_id)
    data = json.loads(index["data"])
    assert len(data["points"]) == 3
    assert data["unavailable_pages"] == [4]
    assert all("sample_question" not in p for p in data["points"])
    before = list(calls)
    assert app.library.index(book_id)["id"] == index["id"]
    assert calls == before
    assert len(app.store.rows("SELECT * FROM search_chunks")) == 3


def test_index_failure_preserves_previous_and_resumes(app, book_id):
    old = app.library.index(book_id)
    original = app.injected_gateway.book_operation
    calls = []
    failed = False

    def operation(op, payload, contract):
        nonlocal failed
        calls.append(op)
        if op == "book_summary" and not failed:
            failed = True
            raise AppError("网络超时")
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="暂停"):
        app.library.index(book_id, new=True)
    assert app.library.active(book_id)["id"] == old["id"]
    assert calls.count("book_index") == 1
    result = app.library.index(book_id)
    assert result["id"] != old["id"]
    assert calls.count("book_index") == 3  # first chapter extraction reused


def test_staged_lessons_resume_only_questions(app, book_id):
    app.library.index(book_id)
    original = app.injected_gateway.book_operation
    calls = []
    fail = True

    def operation(op, payload, contract):
        nonlocal fail
        calls.append(op)
        if op == "learning_questions" and fail:
            fail = False
            raise AppError("超时")
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError):
        app.library.learn(book_id, "s2")
    version = app.store.rows("SELECT version_id FROM lesson_links")[0]["version_id"]
    assert app.version(version)[0]["data"] == "{}"
    assert app.library.step(version, "structure:complete")
    # Existing prepare command can also recover new staged lessons.
    state = app.prepare(version)
    assert state["phase"] == "ready"
    assert calls.count("learning_structure") == 1
    assert calls.count("learning_questions") == 2
    assert app.library.learn(book_id, "s2") == version
    assert calls.count("learning_questions") == 2


def test_questions_cannot_change_or_omit_objectives(app, book_id):
    app.library.index(book_id)
    original = app.injected_gateway.book_operation
    calls = []

    def operation(op, payload, contract):
        if op == "learning_questions":
            calls.append(op)
            value = original(op, payload, contract)
            value.questions[0].objective_key = "invented"
            return value
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="完整且唯一"):
        app.library.learn(book_id, "s1")
    assert len(calls) == 2
    assert app.store.rows("SELECT data FROM versions")[0]["data"] == "{}"


def add_prerequisite(app, book_id):
    index = app.library.index(book_id)
    data = json.loads(index["data"])
    data["relations"] = [
        {
            "source": data["points"][0]["key"],
            "target": data["points"][2]["key"],
            "kind": "prerequisite",
            "reason": "先理解上下文",
        }
    ]
    with app.store.connect() as conn:
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (dumps(data), index["id"]))
    return data["points"][0]["key"]


def test_mastery_mark_clear_fingerprint_and_prerequisites(app, book_id):
    key = add_prerequisite(app, book_id)
    assert len(app.library.prerequisites(book_id, "s3")) == 1
    app.library.mastery(book_id, section="s1")
    assert app.library.prerequisites(book_id, "s3") == []
    assert app.store.rows("SELECT * FROM attempts") == []
    assert app.store.rows("SELECT * FROM assessments") == []
    index = app.library.active(book_id)
    changed = copy.deepcopy(index)
    data = json.loads(index["data"])
    data["points"][0]["description"] += "重要变化"
    changed["data"] = dumps(data)
    assert key not in app.library.marks(book_id, changed)
    app.library.mastery(book_id, point=key, mark=False)
    assert len(app.library.prerequisites(book_id, "s3")) == 1


def test_prerequisite_skip_is_only_session_decision(app, book_id, monkeypatch):
    key = add_prerequisite(app, book_id)
    version = app.library.learn(book_id, "s3")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "3")
    assert prerequisite_choices(app, version) == [key]
    assert key not in app.library.marks(book_id)
    assert app.library.prerequisites(book_id, "s3")


def test_ask_across_chapters_without_index_does_not_prepare(app, book_id):
    record = app.library.ask(book_id, "上下文缓存工具是什么关系？")
    assert {c["page"] for c in record["chunks"]} == {1, 2, 3}
    assert record["index_ready"] is False
    assert app.store.rows("SELECT * FROM book_indexes") == []
    assert app.store.rows("SELECT * FROM versions") == []
    assert app.store.rows("SELECT * FROM attempts") == []
    assert len(app.store.rows("SELECT * FROM book_questions")) == 1


def test_invalid_answer_correction_is_bounded(app, book_id):
    calls = []

    def operation(op, payload, contract):
        calls.append(op)
        return BookAnswer(
            sufficient=True,
            parts=[{"text": "回答", "source_refs": [{"chunk_id": "missing", "quote": "编造"}]}],
        )

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="暂停"):
        app.library.ask(book_id, "缓存")
    assert calls == ["book_answer", "book_answer"]
    failed = app.store.rows("SELECT * FROM book_questions")
    assert len(failed) == 1 and json.loads(failed[0]["data"])["status"] == "failed"
    assert len(app.store.rows("SELECT * FROM events WHERE kind='book_operation_failed'")) == 2


def test_insufficient_answer_is_saved(app, book_id):
    app.injected_gateway.book_operation = lambda *args: BookAnswer(
        sufficient=False, limitation="当前证据不足"
    )
    result = app.library.ask(book_id, "缓存")
    assert result["answer"]["sufficient"] is False


def test_chat_ask_preserves_answer_and_session(app, published, monkeypatch, capsys):
    session = app.start(published)
    app.advance(session)
    app.append_answer_line(session, "已有答案")
    row, before = app.session(session)
    book = app.version(published)[1]
    inputs = iter(["/ask 原理是什么？"])

    def read(_):
        try:
            return next(inputs)
        except StopIteration:
            # Assert before EOF intentionally pauses the session.
            assert app.session(session)[1] == before
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read)
    chat(app, session, before)
    assert "原答案输入已保留" in capsys.readouterr().out
    assert app.store.rows("SELECT * FROM attempts") == []
    assert app.store.rows("SELECT * FROM book_questions")[0]["book_id"] == book.id


def test_relation_validation():
    assert relation_errors(
        [
            {"source": "a", "target": "b", "kind": "prerequisite"},
            {"source": "b", "target": "a", "kind": "prerequisite"},
        ],
        {"a", "b"},
    )
    assert relation_errors([{"source": "missing", "target": "b", "kind": "related"}], {"a", "b"})


def test_outline_and_cli_contract(app, book_id, capsys):
    app.library.index(book_id)
    dispatch(app, parser().parse_args(["outline", book_id]))
    output = capsys.readouterr().out
    assert "知识点" in output and "不可读取页面" in output
    for argv in (
        ["index", book_id],
        ["learn", book_id, "--section", "s1"],
        ["ask", book_id, "--text", "问题"],
        ["mastery", book_id, "--section", "s1", "--mark"],
    ):
        assert parser().parse_args(argv).book == book_id


def test_delete_book_cleans_navigation_and_questions(app, book_id):
    app.library.index(book_id)
    app.library.mastery(book_id, section="s1")
    app.library.ask(book_id, "缓存")
    app.delete_book(book_id)
    for table in ("book_indexes", "book_steps", "book_questions", "self_mastery", "search_chunks"):
        assert app.store.rows(f"SELECT * FROM {table}") == []
    assert app.store.rows("PRAGMA foreign_key_check") == []


def test_migration_is_idempotent_preserves_old_records(app, draft_id):
    before = app.version(draft_id)[0]
    with app.store.connect() as conn:
        conn.execute("PRAGMA user_version=4")
    app.store.initialize()
    app.store.initialize()
    assert app.version(draft_id)[0] == before
    assert app.store.rows("PRAGMA user_version")[0]["user_version"] == 5


def test_all_structure_batches_finish_before_questions(app, book_id, monkeypatch):
    # One chapter spanning three independently checkpointed batches.
    book = app.book(book_id)
    book.sections = [
        Section(
            id="s1", title="全书", start_page=1, end_page=3, chunk_ids=[c.id for c in book.chunks]
        )
    ]
    with app.store.connect() as conn:
        conn.execute("UPDATE books SET data=? WHERE id=?", (book.model_dump_json(), book_id))
    monkeypatch.setattr(app, "batches", lambda chunks: [[c] for c in chunks])
    app.library.index(book_id)
    original = app.injected_gateway.book_operation
    calls = []

    def operation(op, payload, contract):
        calls.append(op)
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    version = app.library.learn(book_id, "s1")
    assert calls[:3] == ["learning_structure"] * 3
    assert all(c == "learning_questions" for c in calls[3:])
    assert len(json.loads(app.version(version)[0]["data"])["points"]) == 3


def test_book_operations_use_transport_and_strict_schema(app, book_id):
    from teaching_agent.book_models import (
        BookAnswer,
        ChapterSummary,
        LearningStructure,
        Navigation,
        Questions,
        Relations,
    )
    from teaching_agent.codex_gateway import strict_schema
    from teaching_agent.gateway import ModelOperations
    from teaching_agent.prompt_loader import load_prompt

    gateway = ModelOperations()
    calls = []
    gateway.call = lambda *args: calls.append(args)
    for operation, contract in [
        ("book_index", Navigation),
        ("book_summary", ChapterSummary),
        ("book_relations", Relations),
        ("learning_structure", LearningStructure),
        ("learning_questions", Questions),
        ("book_answer", BookAnswer),
    ]:
        gateway.book_operation(operation, {"example": "value"}, contract)
        assert calls[-1] == (operation, {"example": "value"}, contract)
        assert load_prompt(operation)
        schema = strict_schema(contract.model_json_schema())
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False


def test_real_cli_demo_index_learn_and_ask(app, book_id, capsys):
    from teaching_agent.cli import main

    common = ["--data-dir", str(app.store.root)]
    assert main([*common, "index", book_id]) == 0
    assert main([*common, "learn", book_id, "--section", "s1"]) == 0
    assert main([*common, "ask", book_id, "--text", "上下文"]) == 0
    assert main([*common, "mastery", book_id, "--section", "s1", "--mark"]) == 0
    output = capsys.readouterr().out
    assert "知识结构：" in output and "用户自报掌握" in output and "PDF p." in output
    version = app.store.rows("SELECT version_id FROM lesson_links")[0]["version_id"]
    assert app.preparation(version)["phase"] == "ready"
    assert app.version(version)[0]["status"] == "draft"  # no implicit publishing


def test_index_invalid_reference_repairs_once(app, book_id):
    original = app.injected_gateway.book_operation
    calls = []

    def operation(op, payload, contract):
        calls.append(op)
        value = original(op, payload, contract)
        if op == "book_index" and calls.count(op) == 1:
            value.points[0].source_refs[0].quote = "不存在的引文"
        return value

    app.injected_gateway.book_operation = operation
    app.library.index(book_id)
    assert calls.count("book_index") == 4
    assert app.store.rows("SELECT * FROM events WHERE kind='book_operation_failed'")


def test_questions_duplicate_target_rejected(app, book_id):
    app.library.index(book_id)
    original = app.injected_gateway.book_operation

    def operation(op, payload, contract):
        value = original(op, payload, contract)
        if op == "learning_questions":
            value.questions.append(value.questions[0].model_copy(deep=True))
        return value

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="完整且唯一"):
        app.library.learn(book_id, "s1")


def test_stable_point_ids_do_not_depend_on_model_order(app, book_id):
    from teaching_agent.book_learning import knowledge_key
    from teaching_agent.book_models import KnowledgePoint

    point = KnowledgePoint(
        key="p1",
        title="缓存",
        description="说明",
        source_refs=[{"chunk_id": "c1", "quote": "原文"}],
    )
    renamed = point.model_copy(update={"key": "other"})
    assert knowledge_key("s1", point) == knowledge_key("s1", renamed)


def test_clone_preserves_navigation_binding(app, book_id):
    index = app.library.index(book_id)
    version = app.library.learn(book_id, "s1")
    clone = app.clone(version)
    original = app.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (version,))[0]
    copied = app.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (clone,))[0]
    assert copied["index_id"] == index["id"]
    assert copied["data"] == original["data"]


def test_chat_question_inherits_session_backend_snapshot(app, published):
    session = app.start(published)
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO backend_bindings VALUES(?,?,?)", (session, "remembered", '{"type":"demo"}')
        )
    before = app.session(session)
    book = app.version(published)[1]
    record = app.library.ask(book.id, "原理", session_id=session)
    assert record["backend"] == [{"name": "remembered", "spec": '{"type":"demo"}'}]
    assert app.session(session) == before


def test_relation_candidates_are_bounded_and_find_cross_chapter_terms():
    from teaching_agent.book_learning import tokens
    from teaching_agent.relation_planner import candidate_pairs, relation_batches

    points = [
        dict(key=f"k{i}", title=f"主题 {i}", description="共同内容", section_id=f"s{i // 10}")
        for i in range(100)
    ]
    points[0]["description"] = points[99]["description"] = "独特缓存前缀复用"
    pairs = candidate_pairs(points, tokens)
    assert (0, 99) in pairs
    assert (0, 1) in pairs
    assert len(pairs) <= 7 * len(points)
    assert len(pairs) == len(set(pairs))
    assert pairs == candidate_pairs(points, tokens)
    batches = relation_batches(points, pairs)
    assert all(len(b["candidate_pairs"]) <= 32 for b in batches)
    assert all(len(dumps(b)) <= 12000 for b in batches)
    assert sum(len(b["candidate_pairs"]) for b in batches) == len(pairs)
    assert candidate_pairs([], tokens) == []
    assert candidate_pairs(points[:1], tokens) == []


@pytest.mark.parametrize("with_edge", [True, False])
def test_index_does_not_reuse_legacy_relation_results(app, book_id, with_edge):
    index = app.library.index(book_id)
    points = json.loads(index["data"])["points"]
    edges = (
        [dict(source=points[0]["key"], target=points[1]["key"], kind="related", reason="已有分析")]
        if with_edge
        else []
    )
    with app.store.connect() as conn:
        conn.execute("UPDATE book_indexes SET status='building' WHERE id=?", (index["id"],))
        conn.execute(
            "DELETE FROM book_steps WHERE owner_id=? AND step LIKE 'relations_v2:%'", (index["id"],)
        )
    app.library.save_step(index["id"], "relations:0:0", {"relations": edges})
    restored = app.library.index(book_id)
    assert json.loads(restored["data"])["relations"] == []
    assert app.library.step(index["id"], "relations_v2:plan")["candidate_count"] > 0


def test_relation_batches_resume_without_repeating_saved_requests(app, book_id, monkeypatch):
    import teaching_agent.book_learning as learning
    from teaching_agent.relation_planner import relation_batches

    monkeypatch.setattr(
        learning,
        "relation_batches",
        lambda points, pairs: relation_batches(points, pairs, pair_limit=1),
    )
    original = app.injected_gateway.book_operation
    calls = []
    failed = False

    def operation(op, payload, contract):
        nonlocal failed
        if op == "book_relations":
            calls.append(copy.deepcopy(payload))
            if len(calls) == 2 and not failed:
                failed = True
                raise AppError("网络超时")
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="暂停"):
        app.library.index(book_id)
    first = calls[0]["candidate_pairs"]
    app.library.index(book_id)
    assert sum(c["candidate_pairs"] == first for c in calls) == 1
    assert calls[1]["candidate_pairs"] == calls[2]["candidate_pairs"]


def test_relation_validation_rejects_edges_outside_candidates(app, book_id, monkeypatch):
    import teaching_agent.book_learning as learning

    monkeypatch.setattr(learning, "candidate_pairs", lambda *args: [(0, 1), (1, 2)])
    original = app.injected_gateway.book_operation
    attempts = []

    def operation(op, payload, contract):
        if op == "book_relations":
            attempts.append(payload)
            points = payload["points"]
            return contract.model_validate(
                {
                    "relations": [
                        dict(
                            source=points[0]["key"],
                            target=points[2]["key"],
                            kind="related",
                            reason="不在候选范围内",
                        )
                    ]
                }
            )
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="candidate_pairs"):
        app.library.index(book_id)
    assert len(attempts) == 2
    assert not app.library.active(book_id)


def test_relation_validation_checks_cycles_across_batches(app, book_id, monkeypatch):
    import teaching_agent.book_learning as learning
    from teaching_agent.relation_planner import relation_batches

    monkeypatch.setattr(learning, "candidate_pairs", lambda *args: [(0, 1), (1, 2), (0, 2)])
    monkeypatch.setattr(
        learning,
        "relation_batches",
        lambda points, pairs: relation_batches(points, pairs, pair_limit=1),
    )
    original = app.injected_gateway.book_operation
    calls = []

    def operation(op, payload, contract):
        if op != "book_relations":
            return original(op, payload, contract)
        calls.append(payload)
        source, target = payload["candidate_pairs"][0]
        if len(calls) >= 3:
            source, target = target, source
        return contract.model_validate(
            {
                "relations": [
                    dict(source=source, target=target, kind="prerequisite", reason="测试前置关系")
                ]
            }
        )

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="循环"):
        app.library.index(book_id)
    assert not app.library.active(book_id)
    assert len(calls) == 4  # third batch is corrected once, then paused
    rows = app.store.rows("SELECT step FROM book_steps WHERE step LIKE 'relations_v2:batch:%'")
    assert {r["step"] for r in rows} == {
        "relations_v2:batch:0",
        "relations_v2:batch:1",
        "relations_v2:batch:2:rejected",
    }
