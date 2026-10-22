from datetime import UTC, datetime

import pytest
from test_learner import entry

from teaching_agent.cli import print_session
from teaching_agent.learner import outcome, project, validate_assessment
from teaching_agent.models import AppError, EvidenceAssessment, EvidenceItem


def evidence(group, verdict="met", assisted=False, coverage=()):
    row = entry(group, 1, assisted=assisted)
    row["id"] = group
    row["question"]["gap_coverage"] = list(coverage)
    row["assessment"] = EvidenceAssessment(
        status="valid",
        feedback="评价",
        items=[
            EvidenceItem(
                rubric_key=key,
                verdict=verdict,
                answer_quotes=["回答"] if verdict != "insufficient" else [],
                reason="依据回答",
            )
            for key in ("core", "extra")
        ],
    ).model_dump()
    return row


def state(rows):
    return project(rows, ["o"], at=datetime(2026, 1, 1, tzinfo=UTC))["o"]


def test_failure_decrements_once_and_does_not_release_seen_group():
    rows = [evidence("a"), evidence("b"), evidence("bad", "incorrect")]
    assert state(rows)["independent_passes"] == 1
    assert state(rows)["historical_passes"] == 2
    rows.append(evidence("a"))
    assert state(rows)["independent_passes"] == 1
    rows.append(evidence("unrelated"))
    assert state(rows)["independent_passes"] == 2
    assert state(rows)["status"] == "needs_remediation"
    rows.append(
        evidence(
            "targeted",
            coverage=[
                {
                    "gap_id": "bad:core",
                    "rubric_keys": ["core"],
                }
            ],
        )
    )
    assert state(rows)["status"] == "provisionally_mastered"


def test_floor_uncertainty_and_assisted_do_not_penalize():
    assert state([evidence("bad", "incorrect")])["independent_passes"] == 0
    rows = [
        evidence("a"),
        evidence("b"),
        evidence("unclear", "insufficient"),
        evidence("assisted", "incorrect", assisted=True),
    ]
    assert state(rows)["independent_passes"] == 2
    assert state(rows)["gaps"] == []


def test_assisted_cannot_clear_gap():
    rows = [
        evidence("bad", "incorrect"),
        evidence(
            "helped",
            assisted=True,
            coverage=[
                {
                    "gap_id": "bad:core",
                    "rubric_keys": ["core"],
                }
            ],
        ),
    ]
    assert state(rows)["gaps"]


def install_grader(app, verdicts):
    calls = []

    def grade(question, answer, chunks, misconceptions, context):
        calls.append((answer, context))
        verdict = verdicts.pop(0)
        if verdict == "offline":
            raise AppError("offline")
        return EvidenceAssessment(
            status="valid",
            feedback="尚待确认" if verdict == "insufficient" else "评价完成",
            items=[
                EvidenceItem(
                    rubric_key=r.key,
                    verdict=verdict,
                    answer_quotes=[answer.split("\n\n")[-1]] if verdict != "insufficient" else [],
                    reason="对应核心要求",
                )
                for r in question.rubric
            ],
            followup="请说明你的判断依据。" if verdict == "insufficient" else "",
            correction="不能提前显示的纠正答案",
        )

    app.injected_gateway.grade_evidence = grade
    return calls


def begin(app, published, verdicts):
    calls = install_grader(app, verdicts)
    session = app.start(published)
    app.advance(session)
    return session, calls


def test_followup_resume_single_attempt_and_no_answer_leak(app, published, capsys):
    session, calls = begin(app, published, ["insufficient", "offline", "met"])
    first = app.submit(session, "原始说明")
    assert first["phase"] == "awaiting_clarification"
    assert first["feedback"] is None
    print_session(first, app.teaching_view(session))
    assert "不能提前显示" not in capsys.readouterr().out
    assert app.progress(published)[first["current"]]["independent_passes"] == 0
    app.append_answer_line(session, "补充依据")
    app.pause(session)
    assert app.resume(session)["answer_input"]["lines"] == ["补充依据"]
    with pytest.raises(AppError, match="offline"):
        app.submit_part(session)
    final = app.resume(session)
    assert final["phase"] == "feedback_ready"
    attempts = app.store.rows("SELECT * FROM attempts WHERE session_id=?", (session,))
    assert len(attempts) == 1 and attempts[0]["answer"] == "原始说明"
    assert len(app.answer_rounds(attempts[0]["id"])) == 1
    assert calls[-1][0] == "原始说明\n\n补充依据"
    assert calls[-1][1]["objective"]["key"] == first["current"]
    assert app.progress(published)[first["current"]]["independent_passes"] == 1
    print_session(final, app.teaching_view(session))
    assert "已满足核心要求" in capsys.readouterr().out


def test_two_round_limit_and_review_block_during_followup(app, published):
    session, _ = begin(app, published, ["insufficient"] * 3)
    current = app.submit(session, "说明")
    attempt_id = current["clarification"]["attempt_id"]
    with pytest.raises(AppError, match="追问"):
        app.reassess(attempt_id, "复核")
    app.submit(session, "补充一")
    final = app.submit(session, "补充二")
    assert final["phase"] == "feedback_ready"
    assert final["feedback"]["correction"] == ""
    mastery = app.progress(published)[current["current"]]
    assert mastery["independent_passes"] == 0 and not mastery["gaps"]
    assert len(app.evidence(published)) == 1


def test_hint_during_followup_marks_final_evidence_assisted(app, published):
    session, _ = begin(app, published, ["insufficient", "met"])
    first = app.submit(session, "说明")
    app.hint(session)
    app.submit(session, "补充")
    assert app.evidence(published)[0]["assisted"]
    assert app.progress(published)[first["current"]]["independent_passes"] == 0


def test_explicit_error_requires_quote_and_core_only_gates():
    from teaching_agent.models import Question

    row = evidence("a")
    question = Question.model_validate(row["question"])
    assessment = EvidenceAssessment.model_validate(row["assessment"])
    assessment.items[1].verdict = "incorrect"
    assert outcome(assessment, question) == "passed"
    assessment.items[0].verdict = "incorrect"
    assessment.items[0].answer_quotes = []
    with pytest.raises(AppError, match="证据"):
        validate_assessment(assessment, question, "回答", set())


def test_missing_gap_coverage_rejected_before_presenting(app, published):
    session, _ = begin(app, published, ["incorrect"])
    first = app.submit(session, "明确错误")
    assert app.progress(published)[first["current"]]["gaps"]
    with pytest.raises(AppError, match="缺口"):
        app.advance(session)
    assert app.session(session)[1]["phase"] == "feedback_ready"


def test_targeted_generated_question_clears_only_covered_gaps(app, published):
    from teaching_agent.models import GapCoverage

    session, _ = begin(app, published, ["incorrect", "met"])
    first = app.submit(session, "明确错误")
    original = app.injected_gateway.task

    def task(objective, chunks, action, history, misconceptions):
        gaps = next(h["open_gaps"] for h in history if "open_gaps" in h)
        result = original(objective, chunks, action, history, misconceptions)
        result.question.gap_coverage = [
            GapCoverage(
                gap_id=g["gap_id"],
                rubric_keys=[result.question.rubric[0].key],
            )
            for g in gaps
        ]
        return result

    app.injected_gateway.task_evidence = task
    app.advance(session)
    app.submit(session, "正确说明")
    mastery = app.progress(published)[first["current"]]
    assert mastery["gaps"] == []
    assert mastery["independent_passes"] == 1


def test_new_gateway_contract_and_bundled_prompts():
    from teaching_agent.gateway import ModelOperations
    from teaching_agent.models import Question

    class Capture(ModelOperations):
        def call(self, operation, payload, contract):
            return operation, payload, contract

    question = Question.model_validate(evidence("a")["question"])
    operation, payload, contract = Capture().grade_evidence(
        question,
        "回答",
        [],
        [],
        {"objective": {"key": "o"}, "rounds": []},
    )
    assert operation == "grade_evidence"
    assert contract is EvidenceAssessment and payload["objective"]["key"] == "o"
    for name in ("grade_evidence", "task_evidence"):
        assert ModelOperations.messages(name, "{}", "{}")[0]["content"]


def test_review_replays_v2_without_overwriting_original_answer(app, published):
    session, calls = begin(app, published, ["incorrect", "met"])
    first = app.submit(session, "原始回答")
    attempt = app.evidence(published)[0]
    assert app.progress(published)[first["current"]]["gaps"]
    app.reassess(attempt["id"], "修正误判")
    mastery = app.progress(published)[first["current"]]
    assert not mastery["gaps"] and mastery["independent_passes"] == 1
    assert calls[-1][1]["rule_version"] == 2
    assert app.store.get("attempts", attempt["id"])["answer"] == "原始回答"
    assert len(app.store.rows("SELECT * FROM assessments")) == 2
