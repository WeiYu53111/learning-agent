import json

import pytest

from teaching_agent.application import Application
from teaching_agent.cli import main
from teaching_agent.models import AppError, Audit, Draft, DraftPatch, Issue, ObjectiveChange


def patch_for(draft):
    return DraftPatch(
        objectives=[
            ObjectiveChange(point_key=p.key, objective=o)
            for p in draft.points
            for o in p.objectives
        ],
        content_items=draft.content_items,
        notes=draft.notes,
    )


def note_patch(draft):
    return DraftPatch(notes=[*draft.notes, "revision note"])


def issue(severity="warning"):
    return Audit(issues=[Issue(description="改进题干及评分公平性", severity=severity)])


def test_clean_preparation_reaudits_old_draft_and_is_idempotent(app, draft_id):
    app.audit(draft_id)
    calls = []
    app.injected_gateway.audit = lambda *args: calls.append(1) or Audit(issues=[])
    messages = []
    result = app.prepare(draft_id, progress=messages.append)
    assert result["phase"] == "ready" and result["rounds"] == 0
    assert len(calls) == 1
    assert app.prepare(draft_id) == result
    assert len(calls) == 1
    assert any("报告已保存" in message for message in messages)
    assert app.version(draft_id)[0]["approved_revision"] is None


@pytest.mark.parametrize("severity,expected", [("warning", "ready"), ("error", "blocked")])
def test_bounded_repair_and_explicit_retry(app, draft_id, severity, expected):
    audits, revisions = [], []
    app.injected_gateway.audit = lambda *args: audits.append(1) or issue(severity)

    def revise(draft, chunks, instruction):
        revisions.append(instruction)
        draft.notes.append("已检查措辞")
        return patch_for(draft)

    app.injected_gateway.revise_patch = revise
    result = app.prepare(draft_id)
    assert result["phase"] == expected and result["rounds"] == 2
    assert len(audits) == 3 and len(revisions) == 2
    assert "改进题干及评分公平性" in revisions[0]
    assert "禁止删除必要知识" in revisions[0]
    assert app.prepare(draft_id) == result
    assert len(revisions) == 2
    restarted = app.prepare(draft_id, retry=True)
    assert restarted["id"] != result["id"] and len(revisions) == 4


def test_one_round_repair_updates_draft_and_summary(app, draft_id):
    replies = iter([issue(), Audit(issues=[])])
    app.injected_gateway.audit = lambda *args: next(replies)

    def revise(draft, chunks, instruction):
        draft.points[0].objectives[0].description += "（补充说明）"
        return patch_for(draft)

    app.injected_gateway.revise_patch = revise
    result = app.prepare(draft_id)
    assert result["phase"] == "ready" and result["rounds"] == 1
    assert result["changes"][0]["updated"] == 1
    row = app.version(draft_id)[0]
    assert row["reviewed_revision"] == row["revision"] == result["revision"]


def test_resume_reaudit_does_not_repeat_revision(app, draft_id):
    calls = []
    replies = iter([issue(), AppError("timeout"), Audit(issues=[])])

    def audit(*args):
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        return result

    app.injected_gateway.audit = audit
    app.injected_gateway.revise_patch = lambda draft, *args: calls.append(1) or note_patch(draft)
    with pytest.raises(AppError, match="timeout"):
        app.prepare(draft_id)
    assert app.preparation(draft_id)["phase"] == "audit"
    # A new application instance uses only durable state.
    resumed = Application(app.store, gateway=app.injected_gateway)
    result = resumed.prepare(draft_id)
    assert result["phase"] == "ready" and result["rounds"] == 1
    assert calls == [1]


def test_failed_revision_call_does_not_consume_round(app, draft_id):
    app.injected_gateway.audit = lambda *args: issue()
    app.injected_gateway.revise_patch = lambda *args: (_ for _ in ()).throw(AppError("too long"))
    with pytest.raises(AppError, match="too long"):
        app.prepare(draft_id)
    state = app.preparation(draft_id)
    assert state["phase"] == "revise" and state["rounds"] == 0


def test_invalid_candidates_preserve_formal_draft_and_resume_candidate(app, draft_id):
    original = app.version(draft_id)[0]["data"]
    app.injected_gateway.audit = lambda *args: issue("error")
    calls = []

    def revise(draft, chunks, instruction):
        calls.append(instruction)
        if len(calls) == 2:
            assert draft.points[0].objectives[0].source_refs[0].quote == "无效引用"
            assert "无效引用" in instruction
        draft.points[0].objectives[0].source_refs[0].quote = "无效引用"
        return patch_for(draft)

    app.injected_gateway.revise_patch = revise
    result = app.prepare(draft_id)
    assert result["phase"] == "blocked" and result["rounds"] == 2
    assert result["patch"] and result["errors"]
    assert app.version(draft_id)[0]["data"] == original


def test_saved_candidate_resumes_validation_without_model_call(app, draft_id, monkeypatch):
    from teaching_agent import preparation

    app.injected_gateway.audit = lambda *args: issue()
    app.injected_gateway.revise_patch = lambda draft, *args: note_patch(draft)
    original = preparation.quality_report

    def interrupted(draft, chunks):
        if app.preparation(draft_id)["phase"] == "validate":
            raise KeyboardInterrupt
        return original(draft, chunks)

    monkeypatch.setattr(preparation, "quality_report", interrupted)
    with pytest.raises(KeyboardInterrupt):
        app.prepare(draft_id)
    assert app.preparation(draft_id)["rounds"] == 1
    monkeypatch.setattr(preparation, "quality_report", original)
    app.injected_gateway.revise_patch = lambda *args: pytest.fail("candidate already saved")
    app.injected_gateway.audit = lambda *args: Audit(issues=[])
    assert app.prepare(draft_id)["phase"] == "ready"


def test_edit_invalidates_preparation_and_delete_cleans_state(app, draft_id):
    app.prepare(draft_id)
    draft = Draft.model_validate_json(app.version(draft_id)[0]["data"])
    app.save_draft(draft_id, draft, "manual edit")
    assert app.preparation(draft_id) is None
    app.prepare(draft_id)
    app.delete_draft(draft_id)
    assert app.store.rows("SELECT * FROM preparations") == []


@pytest.mark.parametrize("answer", ["y", "n", "EOF"])
def test_start_confirmation(app, draft_id, monkeypatch, answer):
    from teaching_agent import cli

    app.prepare(draft_id)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    def respond(prompt):
        if answer == "EOF":
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", respond)
    assert main(["--data-dir", str(app.store.root), "start", draft_id, "--no-chat"]) == 0
    assert (app.version(draft_id)[0]["status"] == "published") == (answer == "y")
    assert len(app.store.rows("SELECT * FROM sessions")) == (1 if answer == "y" else 0)


def test_prepare_noninteractive_does_not_publish(app, draft_id, capsys):
    assert main(["--data-dir", str(app.store.root), "prepare", draft_id]) == 0
    assert "待确认" in capsys.readouterr().out
    assert app.version(draft_id)[0]["status"] == "draft"
    assert not app.store.rows("SELECT * FROM sessions")


def test_separate_audit_and_revise_have_progress(app, draft_id, monkeypatch, capsys):
    from teaching_agent.demo import DemoGateway

    assert main(["--data-dir", str(app.store.root), "audit", draft_id]) == 0
    assert "报告已保存" in capsys.readouterr().err
    monkeypatch.setattr(DemoGateway, "revise_patch", lambda self, draft, *args: note_patch(draft))
    assert (
        main(["--data-dir", str(app.store.root), "revise", draft_id, "--instruction", "检查措辞"])
        == 0
    )
    assert "新草稿已保存" in capsys.readouterr().err


def test_v2_migration_preserves_records(app, draft_id):
    before = app.version(draft_id)[0]
    with app.store.connect() as conn:
        conn.execute("DROP TABLE preparations")
        conn.execute("PRAGMA user_version=2")
    app.store.initialize()
    app.store.initialize()
    assert app.version(draft_id)[0] == before
    assert app.preparation(draft_id) is None
    assert app.store.rows("PRAGMA foreign_key_check") == []
    assert json.loads(before["data"])["points"]


def test_invalid_audit_reference_keeps_stage_recoverable(app, draft_id):
    from teaching_agent.models import Ref

    original = app.version(draft_id)[0]["data"]
    app.injected_gateway.audit = lambda *args: Audit(
        issues=[
            Issue(
                description="incorrect reference",
                severity="error",
                source_refs=[Ref(chunk_id="missing", quote="missing")],
            )
        ]
    )
    with pytest.raises(AppError, match="无效原文引用"):
        app.prepare(draft_id)
    assert app.preparation(draft_id)["phase"] == "audit"
    assert app.version(draft_id)[0]["data"] == original
    assert app.version(draft_id)[0]["audit"] is None


def test_manual_revision_checks_before_overwriting(app, draft_id):
    original = app.version(draft_id)[0]["data"]

    def revise(draft, *args):
        draft.content_items.clear()
        return patch_for(draft)

    app.injected_gateway.revise_patch = revise
    with pytest.raises(AppError, match="原草稿未覆盖"):
        app.revise(draft_id, "修改")
    assert app.version(draft_id)[0]["data"] == original
    assert app.store.rows("SELECT * FROM events WHERE kind='revision_rejected'")


def test_extract_only_then_resume_complete_flow(app, draft_id, monkeypatch):
    from teaching_agent.demo import DemoGateway

    book_id = app.version(draft_id)[0]["book_id"]
    assert main(["--data-dir", str(app.store.root), "draft", book_id, "--extract-only"]) == 0
    target = next(r["id"] for r in app.store.rows("SELECT id FROM versions") if r["id"] != draft_id)
    assert app.preparation(target) is None
    monkeypatch.setattr(DemoGateway, "extract", lambda *args: pytest.fail("already extracted"))
    assert main(["--data-dir", str(app.store.root), "draft", "--resume", target]) == 0
    assert app.preparation(target)["phase"] == "ready"
    assert app.version(target)[0]["status"] == "draft"


def test_prepare_can_confirm_and_enter_teaching(app, draft_id, monkeypatch):
    from teaching_agent import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    answers = iter(["y", "/pause"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert main(["--data-dir", str(app.store.root), "prepare", draft_id]) == 0
    assert app.version(draft_id)[0]["status"] == "published"
    sessions = app.store.rows("SELECT * FROM sessions")
    assert len(sessions) == 1
    assert json.loads(sessions[0]["data"])["lifecycle"] == "paused"


def test_manual_audit_does_not_reset_preparation_budget(app, draft_id):
    app.injected_gateway.audit = lambda *args: issue("error")
    calls = []
    app.injected_gateway.revise_patch = lambda draft, *args: calls.append(1) or note_patch(draft)
    assert app.prepare(draft_id)["phase"] == "blocked"
    app.audit(draft_id)
    assert app.prepare(draft_id)["phase"] == "blocked"
    assert len(calls) == 2
