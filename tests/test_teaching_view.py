import pytest

from teaching_agent.cli import print_progress, print_session
from teaching_agent.models import Assessment, ItemResult


def test_high_score_with_incomplete_critical_item_remains_valid(app, published, capsys):
    identifier = app.start(published)
    app.advance(identifier)
    row, state = app.session(identifier)
    rubric = state["task"]["question"]["rubric"]
    assert len(rubric) == 2
    rubric[0]["weight"], rubric[1]["weight"] = 0.4, 0.6
    app.store.save_session(identifier, row["revision"], state)

    def grade(question, answer, *args):
        return Assessment(status="valid", feedback="第二项正确，第一项需补充来源。", items=[
            ItemResult(rubric_key=r.key, credit=0.5 if i == 0 else 1,
                       answer_quotes=[answer], reason="需补充来源" if i == 0 else "正确")
            for i, r in enumerate(question.rubric)
        ])

    app.injected_gateway.grade = grade
    state = app.submit(identifier, "answer")
    assert state["feedback"]["status"] == "valid"
    view = app.teaching_view(identifier)
    assert view["mastery"]["status"] == "needs_remediation"
    assert view["mastery"]["score"] == pytest.approx(0.8)
    print_session(state, view)
    output = capsys.readouterr().out
    assert "本题 80 分；尚未达标" in output
    assert "关键项待补全" in output and "需补充来源" in output
    assert "当前状态：待补练" in output
    assert "unknown" not in output and "needs_review" not in output


def test_old_session_shows_objective_without_leaking_rubric(app, published, capsys):
    identifier = app.start(published)
    state = app.advance(identifier)
    before = app.session(identifier)
    view = app.teaching_view(identifier)
    print_session(state, view)
    output = capsys.readouterr().out
    assert f"知识点：{view['title']}" in output
    assert f"学习目标：{view['description']}" in output
    assert "等待回答" in output and "首次诊断" in output
    assert "关键项待补全" not in output and "本题 100 分" not in output
    assert state["task"]["question"]["reference_answer"] not in output
    assert app.session(identifier) == before


def test_feedback_refreshes_mastery_and_explains_two_independent_passes(app, published, capsys):
    identifier = app.start(published)
    for index in range(2):
        state = app.advance(identifier)
        state = app.submit(identifier, state["task"]["question"]["reference_answer"])
        print_session(state, app.teaching_view(identifier))
        output = capsys.readouterr().out
        assert f"当前独立达标 {index + 1}/2" in output
        assert "本题 100 分；已达标" in output
        assert ("当前状态：学习中" if index == 0 else "当前状态：已掌握") in output


def test_assisted_feedback_does_not_claim_independent_pass(app, published, capsys):
    identifier = app.start(published)
    state = app.advance(identifier)
    app.hint(identifier)
    state = app.submit(identifier, state["task"]["question"]["reference_answer"])
    print_session(state, app.teaching_view(identifier))
    output = capsys.readouterr().out
    assert "辅助作答，不计入独立达标次数" in output
    assert "当前独立达标 0/2" in output


def test_feedback_explains_confirmed_misconceptions_and_due_review(app, published, capsys):
    identifier = app.start(published)
    state = app.advance(identifier)
    state = app.submit(identifier, state["task"]["question"]["reference_answer"])
    view = app.teaching_view(identifier)
    view["misconceptions"] = ["误认为工具由模型执行"]
    view["mastery"].update(status="needs_remediation", independent_passes=2)
    print_session(state, view)
    output = capsys.readouterr().out
    assert "仍需解除已确认的关键误解：误认为工具由模型执行" in output
    assert "本目标已掌握" not in output
    view["misconceptions"] = []
    view["mastery"]["status"] = "review_due"
    print_session(state, view)
    assert "本目标已到复习时间" in capsys.readouterr().out


@pytest.mark.parametrize("status,label", [("needs_review", "评分待复核"), ("invalid_question", "题目无效")])
def test_nonvalid_feedback_and_latest_revision_counts(app, published, capsys, status, label):
    identifier = app.start(published)
    state = app.advance(identifier)
    original_grade = app.injected_gateway.grade
    app.injected_gateway.grade = lambda *args: Assessment(status=status, items=[], feedback="待核实")
    state = app.submit(identifier, state["task"]["question"]["reference_answer"])
    before = app.store.rows("SELECT * FROM assessments")
    print_session(state, app.teaching_view(identifier))
    print_progress(app, published)
    output = capsys.readouterr().out
    assert f"评估状态：{label}" in output
    assert "不能据此判断你未掌握" in output
    assert "已有作答，但尚无可计入掌握的有效评分证据" in output
    assert "已作答 1 次" in output
    attempt = app.store.rows("SELECT id FROM attempts")[0]["id"]
    if status == "needs_review":
        assert f"teach reassess {attempt}" in output
    assert app.store.rows("SELECT * FROM assessments") == before
    app.injected_gateway.grade = original_grade
    app.reassess(attempt, "复核评分")
    detail = app.progress_details(published)[state["current"]]
    assert detail == {"attempts": 1, "needs_review": [], "invalid_question": 0, "pending": 0}
    print_session(app.session(identifier)[1], app.teaching_view(identifier))
    assert "当前状态：学习中" in capsys.readouterr().out
