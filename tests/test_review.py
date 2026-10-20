import json
import sqlite3

import pytest

from teaching_agent.demo import make_pdf
from teaching_agent.knowledge import parse_pdf, quality_report
from teaching_agent.models import AppError, Audit, Draft, Issue


def test_review_required_and_edits_invalidate(app, draft_id):
    with pytest.raises(AppError, match="审核"):
        app.start(draft_id)
    with pytest.raises(AppError, match="审核"):
        app.approve(draft_id)
    app.audit(draft_id)
    app.approve(draft_id)
    row = app.version(draft_id)[0]
    draft = Draft.model_validate_json(row["data"])
    draft.points[0].title = "人工校正标题"
    app.save_draft(draft_id, draft, "校正")
    with pytest.raises(AppError, match="approve"):
        app.publish(draft_id)
    app.audit(draft_id)
    app.approve(draft_id)
    app.publish(draft_id)
    with pytest.raises(AppError, match="不可修改"):
        app.save_draft(draft_id, draft, "覆盖")
    with pytest.raises(sqlite3.IntegrityError):
        with app.store.connect() as conn:
            conn.execute("UPDATE versions SET data='{}' WHERE id=?", (draft_id,))
    clone = app.clone(draft_id)
    assert app.version(clone)[0]["status"] == "draft"
    assert app.version(clone)[0]["approved_revision"] is None


def test_coverage_references_and_cycles(app, draft_id):
    row, _, chunks = app.version(draft_id)
    draft = Draft.model_validate_json(row["data"])
    assert quality_report(draft, chunks)["errors"] == []
    draft.content_items.pop()
    errors = quality_report(draft, chunks)["errors"]
    assert any("没有内容清单" in e for e in errors)
    draft.points[0].objectives[0].source_refs[0].quote = "不存在的证据"
    draft.points[0].objectives[0].prerequisites = ["b0_o3"]
    errors = quality_report(draft, chunks)["errors"]
    assert any("无效引用" in e for e in errors)
    assert any("循环" in e for e in errors)


def test_audit_errors_require_recorded_disposition(app, draft_id):
    app.injected_gateway.audit = lambda draft, chunks: Audit(
        issues=[Issue(description="请检查目标粒度", severity="error")]
    )
    app.audit(draft_id)
    with pytest.raises(AppError, match="尚未处理"):
        app.approve(draft_id)
    app.resolve_issue(draft_id, 0, "已对照原文：该目标只包含一个可独立评估行为。")
    app.approve(draft_id)
    decisions = json.loads(app.version(draft_id)[0]["decisions"])
    assert "原文" in decisions["0"]


def test_pdf_chinese_and_dedup(app, tmp_path):
    path = tmp_path / "中文 路径.pdf"
    make_pdf(path)
    book = parse_pdf(path)
    assert book.page_count == 3
    assert "可变" in book.chunks[0].text
    assert book.sections[0].start_page == 1
    first = app.import_pdf(path)
    second = app.import_pdf(path)
    assert first.id == second.id


def test_draft_batches_survive_failure(app, tmp_path):
    path = tmp_path / "demo.pdf"
    make_pdf(path)
    book = app.import_pdf(path, demo=True)
    identifier = app.create_draft(book.id, "s1")
    original = app.injected_gateway.extract
    app.injected_gateway.extract = lambda chunks: (_ for _ in ()).throw(AppError("离线失败"))
    with pytest.raises(AppError):
        app.build_draft(identifier)
    assert app.version(identifier)[0]["data"] == "{}"
    app.injected_gateway.extract = original
    app.build_draft(identifier)
    assert app.version(identifier)[0]["revision"] == 1
