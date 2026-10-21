import json

import pytest

from teaching_agent.gateway import ModelOperations
from teaching_agent.models import AppError


@pytest.mark.parametrize("invalid", ["quote", "chunk", "ambiguous"])
def test_invalid_task_refs_repaired_once(app, published, invalid):
    identifier = app.start(published)
    original = app.injected_gateway.task
    calls = []

    def task(obj, chunks, action, history, misconceptions):
        calls.append(history)
        candidate = original(obj, chunks, action, [], misconceptions)
        if len(calls) == 1:
            ref = candidate.question.source_refs[0]
            if invalid == "quote":
                ref.quote = "fabricated quotation"
            elif invalid == "chunk":
                ref.chunk_id = "missing_chunk"
            else:
                ref.quote = " "
                assert chunks[0].text.count(" ") > 1
        return candidate

    app.injected_gateway.task = task
    messages = []
    state = app.advance(identifier, progress=messages.append)
    assert state["phase"] == "awaiting_answer"
    assert len(calls) == 2 and calls[1][-1]["validation_errors"]
    assert "自动修正" in messages[-1]
    assert len(app.store.rows("SELECT * FROM events WHERE kind='task_rejected'")) == 1
    assert len(app.store.rows("SELECT * FROM events WHERE kind='question_presented'")) == 1
    assert not app.store.rows("SELECT * FROM attempts")


@pytest.mark.parametrize("failure", ["invalid_ref", "network", "wrong_action"])
def test_failed_repair_preserves_previous_answer_and_allows_retry(app, published, failure):
    identifier = app.start(published)
    app.advance(identifier)
    app.submit(identifier, "I do not know")
    before = app.session(identifier)
    attempts = app.store.rows("SELECT * FROM attempts")
    assessments = app.store.rows("SELECT * FROM assessments")
    original = app.injected_gateway.task
    calls = []

    def task(*args):
        calls.append(args)
        if len(calls) == 2 and failure == "network":
            raise AppError("network failure")
        candidate = original(*args)
        if len(calls) == 2 and failure == "wrong_action":
            candidate.action = "DIAGNOSE"
        else:
            candidate.question.source_refs[0].quote = "fabricated quotation"
        return candidate

    app.injected_gateway.task = task
    with pytest.raises(AppError):
        app.advance(identifier)
    assert len(calls) == 2
    assert app.session(identifier) == before
    assert app.store.rows("SELECT * FROM attempts") == attempts
    assert app.store.rows("SELECT * FROM assessments") == assessments
    rejected = app.store.rows("SELECT * FROM events WHERE kind='task_rejected'")
    assert json.loads(rejected[0]["data"])["validation_errors"]
    app.injected_gateway.task = original
    assert app.advance(identifier)["phase"] == "awaiting_answer"


def test_task_prompt_requires_exact_quotes_and_repairs():
    prompt = ModelOperations.messages("task", "{}", "{}")[0]["content"]
    assert "逐字复制" in prompt
    assert "validation_errors" in prompt and "rejected_task" in prompt
