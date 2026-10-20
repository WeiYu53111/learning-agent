import json

import pytest

from teaching_agent.application import Application
from teaching_agent.demo import DemoGateway
from teaching_agent.models import AppError, Assessment


def answer_current(app, session_id):
    state = app.session(session_id)[1]
    answer = state["task"]["question"]["reference_answer"]
    return app.submit(session_id, answer)


def test_full_loop_two_questions_per_objective(app, published):
    session_id = app.start(published)
    state = app.advance(session_id)
    for index in range(6):
        assert state["phase"] == "awaiting_answer"
        answer_current(app, session_id)
        if index == 0:
            assert app.progress(published)["b0_o1"]["status"] == "learning"
        state = app.advance(session_id)
    assert state["lifecycle"] == "completed"
    assert all(p["status"] == "provisionally_mastered" for p in app.progress(published).values())
    # A second session reuses sufficient evidence without generating another question.
    assert app.advance(app.start(published))["lifecycle"] == "completed"


def test_hint_and_pause_restore_same_question(app, published):
    session_id = app.start(published)
    original = app.advance(session_id)
    app.hint(session_id)
    app.pause(session_id)
    restored_app = Application(app.store, gateway=DemoGateway())
    restored = restored_app.resume(session_id)
    assert restored["question_id"] == original["question_id"]
    assert restored["hint_count"] == 1
    answer_current(restored_app, session_id)
    state = restored_app.progress(published)["b0_o1"]
    assert state["independent_passes"] == 0
    assert state["score"] == pytest.approx(0.7)


def test_grading_failure_recovers_without_duplicate_attempt(app, published):
    session_id = app.start(published)
    app.advance(session_id)
    original = app.injected_gateway.grade
    app.injected_gateway.grade = lambda *args: (_ for _ in ()).throw(AppError("暂时失败"))
    with pytest.raises(AppError):
        answer_current(app, session_id)
    assert app.session(session_id)[1]["phase"] == "evaluating"
    assert len(app.store.rows("SELECT * FROM attempts")) == 1
    app.injected_gateway.grade = original
    assert app.resume(session_id)["phase"] == "feedback_ready"
    assert len(app.store.rows("SELECT * FROM attempts")) == 1
    with pytest.raises(AppError):
        app.submit(session_id, "重复提交")
    assert len(app.store.rows("SELECT * FROM assessments")) == 1


def test_skipped_prerequisite_ends_with_gaps(app, published):
    session_id = app.start(published)
    app.advance(session_id)
    state = app.skip(session_id)
    assert state["lifecycle"] == "ended_with_gaps"
    assert state["blocked"] == ["b0_o2", "b0_o3"]
    assert app.progress(published)["b0_o1"]["status"] == "unknown"


def test_historical_invalid_revision_removes_mastery_and_review(app, published):
    session_id = app.start(published)
    app.advance(session_id)
    answer_current(app, session_id)
    app.advance(session_id)
    answer_current(app, session_id)
    assert app.progress(published)["b0_o1"]["due"]
    app.injected_gateway.grade = lambda *args: Assessment(
        status="invalid_question", items=[], feedback="题目有歧义"
    )
    attempts = app.store.rows("SELECT * FROM attempts ORDER BY created_at")
    for attempt in attempts:
        app.reassess(attempt["id"], "复核题目质量")
    result = app.progress(published)["b0_o1"]
    assert result["status"] == "unknown"
    assert result["score"] is None
    assert result["due"] is None
    assert len(app.store.rows("SELECT * FROM assessments")) == 4
    assert (
        json.loads(
            app.store.rows("SELECT data FROM assessments ORDER BY rowid LIMIT 1")[0]["data"]
        )["status"]
        == "valid"
    )


def test_three_failures_defer_not_complete(app, published):
    session_id = app.start(published)
    state = app.advance(session_id)
    for _ in range(3):
        assert state["phase"] == "awaiting_answer"
        app.submit(session_id, "不知道")
        state = app.advance(session_id)
    assert state["lifecycle"] == "ended_with_gaps"
    assert "b0_o1" in state["deferred"]
