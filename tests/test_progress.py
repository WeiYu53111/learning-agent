import io
import threading

import pytest

from teaching_agent.cli import main
from teaching_agent.progress import DraftProgress


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
def test_heartbeat_runs_while_waiting_and_stops_on_exit(fail, lazy):
    emitted = threading.Event()

    class Output(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if "[等待]" in text:
                emitted.set()
            return result

    output = Output()
    progress = DraftProgress(interval=0.01, stream=output, lazy=lazy)
    try:
        with progress:
            progress("等待模型提取")
            assert emitted.wait(2)
            if fail:
                raise RuntimeError("test failure")
    except RuntimeError:
        assert fail
    assert not progress._thread.is_alive()
    assert "本阶段已等待" in output.getvalue()
    assert "总耗时" in output.getvalue()
    assert "仍在等待当前阶段完成" in output.getvalue()
    assert "仍在运行" not in output.getvalue()


def test_lazy_progress_without_scoring_is_silent():
    output = io.StringIO()
    with DraftProgress(stream=output, lazy=True) as progress:
        assert not progress._thread.is_alive()
    assert output.getvalue() == ""


def test_scoring_progress_follows_database_commits(app, published):
    identifier = app.start(published)
    app.advance(identifier)
    messages = []
    original = app.injected_gateway.grade

    def report(message):
        state = app.session(identifier)[1]
        assert len(app.store.rows("SELECT * FROM attempts")) == 1
        if "正在评分" in message:
            assert state["phase"] == "evaluating"
            assert not app.store.rows("SELECT * FROM assessments")
        else:
            assert state["phase"] == "feedback_ready"
            assert len(app.store.rows("SELECT * FROM assessments")) == 1
        messages.append(message)

    def grade(*args):
        assert len(messages) == 1 and "正在评分" in messages[0]
        return original(*args)

    app.injected_gateway.grade = grade
    app.submit(identifier, "answer", progress=report)
    assert messages[-1] == "评分完成。"


def test_failed_scoring_can_resume_with_progress(app, published):
    from teaching_agent.models import AppError

    identifier = app.start(published)
    app.advance(identifier)
    original = app.injected_gateway.grade
    messages = []

    def fail(*args):
        raise AppError("model unavailable")

    app.injected_gateway.grade = fail
    with pytest.raises(AppError, match=f"teach resume {identifier}"):
        app.submit(identifier, "answer", progress=messages.append)
    assert len(messages) == 1 and "正在评分" in messages[0]
    app.injected_gateway.grade = original
    app.resume(identifier, progress=messages.append)
    assert messages[-1] == "评分完成。"
    assert len(app.store.rows("SELECT * FROM attempts")) == 1
    assert len(app.store.rows("SELECT * FROM assessments")) == 1


def test_score_save_failure_does_not_report_completion(app, published, monkeypatch):
    from teaching_agent.models import AppError

    identifier = app.start(published)
    app.advance(identifier)
    original = app.store.save_session
    messages = []

    def save(session_id, revision, state, conn=None):
        if state["phase"] == "feedback_ready":
            raise AppError("score write failed")
        return original(session_id, revision, state, conn)

    monkeypatch.setattr(app.store, "save_session", save)
    with pytest.raises(AppError, match="score write failed"):
        app.submit(identifier, "answer", progress=messages.append)
    assert len(messages) == 1 and "正在评分" in messages[0]
    assert app.session(identifier)[1]["phase"] == "evaluating"
    assert len(app.store.rows("SELECT * FROM attempts")) == 1
    assert not app.store.rows("SELECT * FROM assessments")


def test_cli_draft_progress(app, draft_id, capsys):
    book_id = app.store.get("versions", draft_id)["book_id"]
    assert main(["--data-dir", str(app.store.root), "draft", book_id]) == 0
    output = capsys.readouterr().err
    for message in (
        "第 1/1 批",
        "PDF 页",
        "等待模型提取",
        "校验提取结果",
        "已保存",
        "草稿提取完成",
    ):
        assert message in output


def test_resume_reports_cached_batch_without_model_call(app, draft_id):
    with app.store.connect() as conn:
        conn.execute("UPDATE versions SET data='{}' WHERE id=?", (draft_id,))
    app.injected_gateway.extract = lambda _: pytest.fail("must reuse cached batch")
    messages = []
    app.build_draft(draft_id, progress=messages.append)
    assert any("复用已保存结果" in message for message in messages)
    assert not any("等待模型提取" in message for message in messages)
    assert messages[-1] == "草稿提取完成"
