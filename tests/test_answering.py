import json

import pytest
from pydantic import ValidationError

from teaching_agent.answering import answer_state, combined_answer, question_parts, question_text
from teaching_agent.cli import chat, print_session
from teaching_agent.models import AppError, Question


@pytest.fixture
def multipart(app, published):
    identifier = app.start(published)
    app.advance(identifier)
    row, state = app.session(identifier)
    state["task"]["question"]["stem"] = "共同背景"
    state["task"]["question"]["parts"] = ["第一小问？", "第二小问？", "第三小问？"]
    state.pop("answer_input", None)
    app.store.save_session(identifier, row["revision"], state)
    return identifier


@pytest.mark.parametrize("separator", [".", "、", ")"])
def test_legacy_numbered_question_is_presentation_only(separator):
    stem = f"共同背景\n1{separator} 第一问？\n2{separator} 第二问？\n3{separator} 第三问？"
    question = {"stem": stem}
    context, parts = question_parts(question)
    assert context == "共同背景" and parts == ["第一问？", "第二问？", "第三问？"]
    assert question == {"stem": stem}


@pytest.mark.parametrize(
    "stem",
    [
        "单问题",
        "背景\n1. 问一\n3. 问三",
        "1. 无背景\n2. 问二",
        "背景\n```\n1. 示例\n2. 示例\n```",
        "背景\n1. 问一\n2、问二",
        "背景\n 1. 缩进列表\n2. 问二",
        "背景\n1.0 数据\n2.0 数据",
        "背景\n1. \n2. 非空",
        "背景\n1. 第一问\n2. 第二问\n3. ",
        "背景\n1. 一\n2. 二\n3. 三\n4. 四",
    ],
)
def test_unclear_legacy_question_remains_whole(stem):
    assert question_parts({"stem": stem}) == ("", [stem])


def test_multiline_parts_create_only_one_attempt_and_grade_once(app, multipart):
    calls = []
    grade = app.injected_gateway.grade

    def record(*args):
        calls.append(args)
        return grade(*args)

    app.injected_gateway.grade = record
    original = app.session(multipart)[1]["task"]["question"]
    app.append_answer_line(multipart, "中文第一行")
    app.append_answer_line(multipart, "")
    app.append_answer_line(multipart, "    code = 1")
    state = app.submit_part(multipart)
    assert state["answer_input"]["answers"] == ["中文第一行\n\n    code = 1"]
    app.append_answer_line(multipart, "第二问回答")
    app.submit_part(multipart)
    assert calls == [] and not app.store.rows("SELECT * FROM attempts")
    app.append_answer_line(multipart, "第三问回答")
    state = app.submit_part(multipart)
    assert state["phase"] == "feedback_ready" and len(calls) == 1
    attempts = app.store.rows("SELECT * FROM attempts")
    assert len(attempts) == 1
    assert json.loads(attempts[0]["question"]) == original
    assert "第 1 问回答：\n中文第一行\n\n    code = 1" in attempts[0]["answer"]
    assert "第 3 问回答：\n第三问回答" in attempts[0]["answer"]
    with pytest.raises(AppError, match="没有待答题"):
        app.submit_part(multipart)


def test_resume_and_cancel_preserve_completed_parts(app, multipart):
    app.append_answer_line(multipart, "first")
    app.submit_part(multipart)
    app.append_answer_line(multipart, "second draft")
    app.pause(multipart)
    saved = app.resume(multipart)["answer_input"]
    assert saved["answers"] == ["first"] and saved["lines"] == ["second draft"]
    state = app.cancel_answer(multipart)
    assert state["answer_input"]["answers"] == ["first"]
    assert state["answer_input"]["lines"] == []
    with pytest.raises(AppError, match="不能为空"):
        app.submit_part(multipart)
    with pytest.raises(AppError, match="不能覆盖"):
        app.submit(multipart, "replace all")


def test_overflow_preserves_buffer_and_counts_headers(app, multipart):
    state = app.session(multipart)[1]
    overhead = len(combined_answer(answer_state(state), include_buffer=True))
    app.append_answer_line(multipart, "a" * (10000 - overhead))
    before = app.session(multipart)
    with pytest.raises(AppError, match="此行未保存"):
        app.append_answer_line(multipart, "x")
    assert app.session(multipart) == before
    assert not app.store.rows("SELECT * FROM attempts")


def test_grade_failure_resumes_without_duplicate_or_reentry(app, multipart):
    original = app.injected_gateway.grade
    app.injected_gateway.grade = lambda *args: (_ for _ in ()).throw(AppError("offline"))
    for _ in range(2):
        app.append_answer_line(multipart, "answer")
        app.submit_part(multipart)
    app.append_answer_line(multipart, "last")
    with pytest.raises(AppError, match="offline"):
        app.submit_part(multipart)
    assert len(app.session(multipart)[1]["answer_input"]["answers"]) == 3
    assert len(app.store.rows("SELECT * FROM attempts")) == 1
    app.injected_gateway.grade = original
    assert app.resume(multipart)["phase"] == "feedback_ready"
    assert len(app.store.rows("SELECT * FROM attempts")) == 1


@pytest.mark.parametrize("stop", [EOFError, KeyboardInterrupt])
def test_chat_interrupt_saves_lines_without_reprinting_question(
    app, multipart, monkeypatch, capsys, stop
):
    answers = iter(["first line", "", "//path"])

    def read(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise stop from None

    monkeypatch.setattr("builtins.input", read)
    chat(app, multipart, app.session(multipart)[1])
    state = app.resume(multipart)
    assert state["answer_input"]["lines"] == ["first line", "", "/path"]
    output = capsys.readouterr().out
    assert output.count("第一小问？") == 1
    assert "第二小问？" not in output
    assert not app.store.rows("SELECT * FROM attempts")


def test_hint_marks_whole_attempt_assisted(app, multipart):
    for index in range(3):
        app.append_answer_line(multipart, f"answer {index}")
        if index == 1:
            app.hint(multipart)
        app.submit_part(multipart)
    assert app.store.rows("SELECT assisted FROM attempts")[0]["assisted"] == 1


@pytest.mark.parametrize("action", ["skip", "explain"])
def test_revoking_question_clears_partial_input(app, multipart, action):
    original_id = app.session(multipart)[1]["question_id"]
    app.append_answer_line(multipart, "unfinished")
    state = getattr(app, action)(multipart)
    saved = state.get("answer_input")
    assert saved is None or saved["question_id"] != original_id
    assert not app.store.rows("SELECT * FROM attempts")


def test_question_parts_contract_and_text(app, multipart):
    question = app.session(multipart)[1]["task"]["question"]
    assert question_text(question) == "共同背景\n第一小问？\n第二小问？\n第三小问？"
    for parts in ([""], ["   "], ["a"] * 4):
        with pytest.raises(ValidationError):
            Question.model_validate({**question, "parts": parts})
    legacy = dict(question)
    del legacy["parts"]
    assert Question.model_validate(legacy).parts == []


def test_old_session_initializes_legacy_parts_on_resume(app, multipart, capsys):
    row, state = app.session(multipart)
    question = state["task"]["question"]
    question.pop("parts")
    question["stem"] = "共同背景\n1. 第一问？\n2. 第二问？\n3. 第三问？"
    state.pop("answer_input", None)
    app.store.save_session(multipart, row["revision"], state)
    state = app.resume(multipart)
    assert state["answer_input"]["parts"] == ["第一问？", "第二问？", "第三问？"]
    print_session(state)
    output = capsys.readouterr().out
    assert "第一问？" in output and "第二问？" not in output
    assert app.session(multipart)[1]["task"]["question"] == question


def test_last_part_and_attempt_are_atomic(app, multipart, monkeypatch):
    messages = []
    for _ in range(2):
        app.append_answer_line(multipart, "saved")
        app.submit_part(multipart, progress=messages.append)
    assert not messages
    app.append_answer_line(multipart, "last line")
    before = app.session(multipart)
    original = app.store.save_session

    def fail(*args, **kwargs):
        raise AppError("write failed")

    monkeypatch.setattr(app.store, "save_session", fail)
    with pytest.raises(AppError, match="write failed"):
        app.submit_part(multipart, progress=messages.append)
    assert not messages
    assert app.session(multipart) == before
    assert not app.store.rows("SELECT * FROM attempts")
    monkeypatch.setattr(app.store, "save_session", original)
    assert app.submit_part(multipart)["phase"] == "feedback_ready"
    assert len(app.store.rows("SELECT * FROM attempts")) == 1


def test_chat_final_part_reports_scoring(app, multipart, monkeypatch, capsys):
    answers = iter(["one", "/end", "two", "/end", "three", "/end", "/pause"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    chat(app, multipart, app.session(multipart)[1])
    output = capsys.readouterr()
    assert output.out.count("当前小问已保存") == 2
    assert output.err.count("回答已保存，正在评分") == 1
    assert output.err.count("评分完成。") == 1


def test_nonmutating_chat_commands_preserve_buffer(app, multipart, monkeypatch, capsys):
    answers = iter(["draft", "/help", "/why", "/progress", "/answer", "/pause"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    chat(app, multipart, app.session(multipart)[1])
    assert app.session(multipart)[1]["answer_input"]["lines"] == ["draft"]
    assert capsys.readouterr().out.count("第一小问？") == 1
