import json

import pytest

from teaching_agent.book_models import Navigation
from teaching_agent.extraction import analysis_errors, navigation_errors, source_units
from teaching_agent.extraction_models import PIPELINE, SourceAnalysis
from teaching_agent.models import AppError, Book, Chunk, Section
from teaching_agent.storage import dumps, now


def save_book(app, texts, sections=False):
    chunks = [Chunk(id=f"c{i}", page=i + 1, text=text) for i, text in enumerate(texts)]
    book = Book(
        id="semantic_test",
        sha256="e" * 64,
        filename="semantic.pdf",
        page_count=len(chunks),
        chunks=chunks,
        demo=True,
        sections=[
            Section(
                id=f"s{i}", title=f"主题{i}", start_page=i + 1, end_page=i + 1, chunk_ids=[c.id]
            )
            for i, c in enumerate(chunks)
        ]
        if sections
        else [
            Section(
                id="s0",
                title="教材",
                start_page=1,
                end_page=len(chunks),
                chunk_ids=[c.id for c in chunks],
            )
        ],
    )
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO books VALUES(?,?,?)", (book.id, book.sha256, book.model_dump_json())
        )
    return book


def classified_gateway(app, roles):
    original = app.injected_gateway.book_operation
    calls = []

    def operation(op, payload, contract):
        calls.append((op, payload))
        if op == "source_analysis":
            return contract(
                regions=[
                    dict(
                        first=u["id"],
                        last=u["id"],
                        role=roles.get(u["text"].strip(), "teaching"),
                        path=["同一概念"],
                        topic="同一概念",
                        reason="人工标注样本",
                    )
                    for u in payload["units"]
                ]
            )
        if op == "book_index":
            chunks = {c["id"]: c for c in payload["chunks"]}
            refs = [
                dict(
                    chunk_id=r["chunk_id"],
                    quote=chunks[r["chunk_id"]]["text"][r["start"] : r["end"]].strip(),
                )
                for r in payload["regions"]
            ]
            return contract(
                points=[
                    dict(key="p", title="核心概念", description="定义及性质", source_refs=refs)
                ],
                items=[
                    dict(
                        key=f"i{i}",
                        description="内容",
                        kind="definition",
                        point_keys=["p"],
                        source_refs=[ref],
                    )
                    for i, ref in enumerate(refs)
                ],
            )
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    return calls, original


@pytest.mark.parametrize(
    "text,role,expected",
    [
        ("目录：第一章……1", "navigation", 0),
        ("版权归作者所有", "publication", 0),
        ("书名与作者", "publication", 0),
        ("出版说明", "publication", 0),
        ("前言：本书面向开发者", "publication", 0),
        ("前言：栈是后进先出结构。", "teaching", 1),
        ("栈是后进先出结构。", "teaching", 1),
        ("栈的入栈出栈均发生在栈顶。", "teaching", 1),
        ("叠盘子类似后进先出。", "teaching", 1),
        ("括号匹配使用栈保存未匹配左括号。", "teaching", 1),
        ("只在栈非空时执行出栈。", "teaching", 1),
        ("无法确定含义的内容", "unknown", 1),
    ],
)
def test_content_roles_do_not_force_points(app, text, role, expected):
    book = save_book(app, [text])
    calls, _ = classified_gateway(app, {text: role})
    data = json.loads(app.library.index(book.id)["data"])
    assert len(data["points"]) == expected
    assert data["regions"][0]["excluded"] == (expected == 0)
    assert data["regions"][0]["reason"]
    assert sum(op == "book_index" for op, _ in calls) == expected
    assert data["pipeline"] == PIPELINE


def test_mixed_page_keeps_original_offsets_and_groups_supporting_content(app):
    text = "目录：第一章……1\n栈是后进先出结构。\n叠盘子类似后进先出。\n"
    book = save_book(app, [text])
    classified_gateway(app, {"目录：第一章……1": "navigation"})
    data = json.loads(app.library.index(book.id)["data"])
    assert len(data["points"]) == 1
    assert len(data["items"]) == 2
    assert all("目录" not in ref["quote"] for p in data["points"] for ref in p["source_refs"])
    assert app.book(book.id).chunks[0].text == text.strip()
    assert data["regions"][1]["start"] == len("目录：第一章……1\n")


def test_classification_must_partition_source_without_overlap_or_omission():
    units = source_units([Chunk(id="c", page=1, text="one\ntwo\n")])
    region = dict(first=0, last=0, role="teaching", path=[], topic="test", reason="test")
    assert analysis_errors(SourceAnalysis(regions=[region]), units)
    assert analysis_errors(SourceAnalysis(regions=[region, region]), units)
    assert not analysis_errors(SourceAnalysis(regions=[{**region, "last": 1}]), units)


def test_quotes_cannot_use_excluded_or_context_text():
    chunks = [Chunk(id="c", page=1, text="目录\n正文")]
    region = dict(chunk_id="c", start=3, end=5)
    nav = Navigation(
        points=[
            dict(
                key="p", title="t", description="d", source_refs=[dict(chunk_id="c", quote="目录")]
            )
        ],
        items=[
            dict(
                description="d",
                kind="definition",
                point_keys=["p"],
                source_refs=[dict(chunk_id="c", quote="正文")],
            )
        ],
    )
    assert any("超出" in error for error in navigation_errors(nav, chunks, [region]))


def test_semantic_review_repairs_once_and_reuses_checkpoints(app):
    book = save_book(app, ["栈是后进先出结构。"])
    calls, _ = classified_gateway(app, {})
    wrapped = app.injected_gateway.book_operation
    reviews = []

    def operation(op, payload, contract):
        if op == "extraction_review":
            reviews.append(payload)
            if len(reviews) == 1:
                return contract(
                    issues=[
                        dict(
                            kind="fragmented",
                            point_keys=["p"],
                            description="合并定义与解释",
                            evidence="原文",
                        )
                    ]
                )
        return wrapped(op, payload, contract)

    app.injected_gateway.book_operation = operation
    index = app.library.index(book.id)
    assert len(reviews) == 2
    assert sum(op == "book_index" for op, _ in calls) == 2
    app.injected_gateway.book_operation = lambda *args: pytest.fail("completed index is immutable")
    assert app.library.index(book.id)["id"] == index["id"]


def test_failed_semantic_verification_cannot_publish_or_loop(app):
    book = save_book(app, ["正文知识"])
    classified_gateway(app, {})
    wrapped = app.injected_gateway.book_operation
    reviews = []

    def operation(op, payload, contract):
        if op == "extraction_review":
            reviews.append(payload)
            return contract(
                issues=[
                    dict(
                        kind="fragmented",
                        point_keys=["p"],
                        description="依然过碎",
                        evidence="正文知识",
                    )
                ]
            )
        return wrapped(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="审核仍未通过"):
        app.library.index(book.id)
    assert len(reviews) == 2
    with pytest.raises(AppError, match="审核仍未通过"):
        app.library.index(book.id)
    assert len(reviews) == 2
    assert app.library.active(book.id) is None


def test_old_ready_and_pending_indexes_are_not_reused(app):
    book = save_book(app, ["正文知识"])
    with app.store.connect() as conn:
        for status in ("ready", "building"):
            conn.execute(
                "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
                ("old_" + status, book.id, status, "{}", now()),
            )
    assert app.library.active(book.id) is None
    new = app.library.index(book.id)
    assert new["id"] not in {"old_ready", "old_building"}
    assert len(app.store.rows("SELECT * FROM book_indexes")) == 3


def test_new_ignores_pending_and_full_rebuild_does_not_inherit_mastery(app):
    book = save_book(app, ["正文知识"])
    first = app.library.index(book.id)
    app.library.mastery(book.id, section="s0")
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            ("pending", book.id, "building", dumps({"pipeline": PIPELINE}), now()),
        )
    new = app.library.index(book.id, new=True)
    assert new["id"] not in {first["id"], "pending"}
    assert app.library.marks(book.id, new) == set()


def test_section_rebuild_preserves_other_chapters_and_is_resumable(app):
    book = save_book(app, ["知识甲", "知识乙", "知识丙"], sections=True)
    first = app.library.index(book.id)
    baseline = json.loads(first["data"])
    left, _, right = baseline["points"]
    preserved_edge = dict(
        source=left["key"], target=right["key"], kind="related", reason="未变化的章节关系"
    )
    baseline["relations"] = [preserved_edge]
    with app.store.connect() as conn:
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (dumps(baseline), first["id"]))
    original = app.injected_gateway.book_operation
    calls = []
    fail = True

    def operation(op, payload, contract):
        nonlocal fail
        calls.append((op, payload))
        if op == "book_summary" and fail:
            fail = False
            raise AppError("模拟网络故障")
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError):
        app.library.index(book.id, section_id="s1", new=True)
    assert app.library.active(book.id)["id"] == first["id"]
    second = app.library.index(book.id, section_id="s1")
    old, new = json.loads(first["data"]), json.loads(second["data"])
    assert preserved_edge in new["relations"]
    assert [p for p in old["points"] if p["section_id"] != "s1"] == [
        p for p in new["points"] if p["section_id"] != "s1"
    ]
    assert sum(op == "book_index" for op, _ in calls) == 1
    assert all(
        {c["id"] for c in payload["chunks"]} == {"c1"}
        for op, payload in calls
        if op == "book_index"
    )


def test_boundary_merge_preserves_all_evidence(app):
    book = save_book(app, ["栈的定义", "栈的性质"])
    calls, _ = classified_gateway(app, {})
    wrapped = app.injected_gateway.book_operation

    def operation(op, payload, contract):
        if op == "source_analysis":
            result = wrapped(op, payload, contract)
            # Simulate a split at an actual topic boundary to exercise reconciliation.
            for i, r in enumerate(result.regions):
                r.topic = f"part{i}"
            return result
        if op == "boundary_review":
            return contract(
                merges=[
                    dict(
                        members=payload["candidate_pairs"][0],
                        title="栈",
                        description="定义与性质",
                        reason="同一概念",
                    )
                ]
            )
        return wrapped(op, payload, contract)

    app.injected_gateway.book_operation = operation
    data = json.loads(app.library.index(book.id)["data"])
    assert len(data["points"]) == 1
    assert len(data["items"]) == 2
    assert {r["quote"] for r in data["points"][0]["source_refs"]} == {"栈的定义", "栈的性质"}
    extracts = [p for op, p in calls if op == "book_index"]
    assert len(extracts) == 2 and all(p["reference_context"] for p in extracts)


def test_only_new_extraction_transport_is_available():
    from teaching_agent.demo import DemoGateway
    from teaching_agent.gateway import ModelOperations

    assert not hasattr(ModelOperations, "extract")
    assert not hasattr(DemoGateway, "extract")


def test_mixed_page_exclusions_cannot_become_learning_objectives():
    from teaching_agent.book_models import LearningStructure
    from teaching_agent.extraction import teaching_errors

    chunk = Chunk(id="c", page=1, text="版权声明\n知识正文")
    structure = LearningStructure(
        points=[
            dict(
                key="p",
                title="错误目标",
                knowledge_keys=["k"],
                objectives=[
                    dict(
                        key="o",
                        description="解释版权",
                        dimension="concept",
                        source_refs=[dict(chunk_id="c", quote="版权声明")],
                    )
                ],
            )
        ],
        content_items=[
            dict(
                key="i",
                description="错误的教学内容",
                kind="definition",
                objective_keys=["o"],
                source_refs=[dict(chunk_id="c", quote="版权声明")],
            )
        ],
    )
    regions = [dict(chunk_id="c", start=5, end=9, excluded=False)]
    assert teaching_errors(structure, [chunk], regions)


def test_new_models_have_strict_transport_schemas():
    from teaching_agent.codex_gateway import strict_schema
    from teaching_agent.extraction_models import BoundaryReview, ExtractionReview

    for model in (SourceAnalysis, ExtractionReview, BoundaryReview):
        schema = strict_schema(model.model_json_schema())
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False


def test_legacy_unfinished_lesson_is_not_resumed(app):
    book = save_book(app, ["正文知识"])
    version = app.create_draft(book.id, "s0")
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)", ("old", book.id, "ready", "{}", now())
        )
        conn.execute("INSERT INTO lesson_links VALUES(?,?,?,?)", (version, "old", "s0", "{}"))
    with pytest.raises(AppError, match="旧提取流程已废弃"):
        app.build_draft(version)


def test_real_book_draft_does_not_implicitly_start_full_extraction(app):
    book = save_book(app, ["正文知识"])
    book.demo = False
    with app.store.connect() as conn:
        conn.execute("UPDATE books SET data=? WHERE id=?", (book.model_dump_json(), book.id))
    version = app.create_draft(book.id, "s0")
    app.injected_gateway.book_operation = lambda *args: pytest.fail("no implicit full-book job")
    with pytest.raises(AppError, match="旧提取入口已废弃"):
        app.build_draft(version)
    assert not app.store.rows("SELECT * FROM book_indexes")


def test_later_draft_edits_cannot_reintroduce_excluded_text(app):
    from teaching_agent.models import Draft

    book = save_book(app, ["版权声明", "正文知识"])
    classified_gateway(app, {"版权声明": "publication"})
    index = app.library.index(book.id)
    version = app.create_draft(book.id, "s0")
    with app.store.connect() as conn:
        conn.execute("INSERT INTO lesson_links VALUES(?,?,?,?)", (version, index["id"], "s0", "{}"))
    ref = dict(chunk_id="c0", quote="版权声明")
    draft = Draft(
        points=[
            dict(
                key="p",
                title="错误知识",
                objectives=[
                    dict(
                        key="o",
                        description="解释版权",
                        dimension="concept",
                        source_refs=[ref],
                        sample_question=dict(
                            stem="问题",
                            reference_answer="答案",
                            source_refs=[ref],
                            rubric=[dict(key="r", description="规则", weight=1.0)],
                            novelty_group="g",
                        ),
                    )
                ],
            )
        ],
        content_items=[
            dict(
                key="i",
                description="错误内容",
                kind="definition",
                source_refs=[ref],
                objective_keys=["o"],
            )
        ],
    )
    with pytest.raises(AppError, match="已排除"):
        app.save_draft(version, draft, "test")
    assert app.version(version)[0]["data"] == "{}"
