import pytest

from teaching_agent.models import AppError, Assessment


def test_page_scope_retained_in_clone(app, tmp_path):
    from teaching_agent.demo import make_pdf

    source = tmp_path / "chapter.pdf"
    make_pdf(source)
    book = app.import_pdf(source, demo=True)
    identifier = app.create_draft(book.id, "s1@1:1")
    app.build_draft(identifier)
    _, _, chunks = app.version(identifier)
    assert {c.page for c in chunks} == {1}
    clone = app.clone(identifier)
    assert app.version(clone)[0]["section_id"] == "s1@1:1"
    with pytest.raises(AppError, match="页码"):
        app.create_draft(book.id, "s1@1:100")


def test_explain_revokes_and_next_question_is_new(app, published):
    identifier = app.start(published)
    original = app.advance(identifier)
    app.explain(identifier)
    next_state = app.advance(identifier)
    assert next_state["question_id"] != original["question_id"]
    assert (
        next_state["task"]["question"]["novelty_group"]
        != (original["task"]["question"]["novelty_group"])
    )


def test_reassessment_failure_keeps_old_revision(app, published):
    identifier = app.start(published)
    state = app.advance(identifier)
    app.submit(identifier, state["task"]["question"]["reference_answer"])
    attempt = app.store.rows("SELECT id FROM attempts")[0]["id"]
    before = app.progress(published)
    app.injected_gateway.grade = lambda *args: (_ for _ in ()).throw(AppError("失败"))
    with pytest.raises(AppError):
        app.reassess(attempt, "复核")
    assert app.progress(published) == before
    assert len(app.store.rows("SELECT * FROM assessments")) == 1


def test_reassessment_revokes_task_if_prerequisite_is_no_longer_mastered(app, published):
    identifier = app.start(published)
    app.advance(identifier)
    for _ in range(2):
        state = app.session(identifier)[1]
        app.submit(identifier, state["task"]["question"]["reference_answer"])
        app.advance(identifier)
    assert app.session(identifier)[1]["current"] == "b0_o2"
    original = app.injected_gateway.grade
    app.injected_gateway.grade = lambda *args: Assessment(
        status="invalid_question", items=[], feedback="题目有误"
    )
    attempt = app.store.rows("SELECT id FROM attempts ORDER BY created_at LIMIT 1")[0]["id"]
    app.reassess(attempt, "撤销前置的一条达标证据")
    app.injected_gateway.grade = original
    state = app.resume(identifier)
    assert state["current"] == "b0_o1"


def test_unknown_after_needs_review_replaces_valid_evidence(app, published):
    identifier = app.start(published)
    state = app.advance(identifier)
    app.submit(identifier, state["task"]["question"]["reference_answer"])
    attempt = app.store.rows("SELECT id FROM attempts")[0]["id"]
    app.injected_gateway.grade = lambda *args: Assessment(
        status="needs_review", items=[], feedback="证据不足"
    )
    app.reassess(attempt, "复核")
    assert app.progress(published)["b0_o1"]["status"] == "unknown"
