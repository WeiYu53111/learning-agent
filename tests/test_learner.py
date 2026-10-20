from datetime import UTC, datetime, timedelta

import pytest

from teaching_agent.learner import project, validate_assessment
from teaching_agent.models import (
    AppError,
    Assessment,
    ItemResult,
    Misconception,
    MisconceptionResult,
    Question,
    Ref,
    Rubric,
)


def question(group="g1"):
    return Question(
        stem="解释",
        reference_answer="原理",
        rubric=[
            Rubric(key="core", description="核心", weight=0.8),
            Rubric(key="extra", description="补充", weight=0.2, critical=False),
        ],
        novelty_group=group,
        source_refs=[Ref(chunk_id="c", quote="原文")],
    )


def entry(group, core, extra=0, assisted=False, action="REASSESS", stamp=None):
    assessment = Assessment(
        status="valid",
        items=[
            ItemResult(
                rubric_key="core", credit=core, answer_quotes=["核心"] if core else [], reason=""
            ),
            ItemResult(
                rubric_key="extra", credit=extra, answer_quotes=["补充"] if extra else [], reason=""
            ),
        ],
        feedback="反馈",
    )
    return {
        "question": question(group).model_dump(),
        "assessment": assessment.model_dump(),
        "objective_key": "o",
        "assisted": assisted,
        "practice_only": False,
        "action": action,
        "created_at": (stamp or datetime(2026, 1, 1, tzinfo=UTC)).isoformat(),
    }


def test_threshold_passes_not_blocked_by_smoothed_score():
    state = project(
        [entry("g0", 0), entry("g1", 1), entry("g2", 1)], ["o"], at=datetime(2026, 1, 1, tzinfo=UTC)
    )["o"]
    assert state["status"] == "provisionally_mastered"
    assert state["score"] == pytest.approx(0.512)


def test_assisted_does_not_clear_gap():
    state = project([entry("g0", 0), entry("g1", 1, assisted=True)], ["o"])["o"]
    assert state["status"] == "needs_remediation"
    assert state["independent_passes"] == 0


def test_due_review_and_independent_pass_extend_interval():
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    evidence = [entry("g1", 1), entry("g2", 1)]
    assert project(evidence, ["o"], at=stamp + timedelta(days=2))["o"]["status"] == "review_due"
    evidence.append(entry("g3", 1, action="REVIEW", stamp=stamp + timedelta(days=2)))
    result = project(evidence, ["o"], at=stamp + timedelta(days=2))["o"]
    assert result["due"] == (stamp + timedelta(days=5)).isoformat()


def test_misconception_requires_targeted_refutations():
    definition = Misconception(code="m", description="误解", objective_keys=["o"])
    evidence = []
    for group, relation, passed in [
        ("a", "supports", False),
        ("b", "supports", False),
        ("c", "refutes", True),
        ("d", "refutes", True),
    ]:
        item = entry(group, int(passed))
        item["question"]["targeted_misconception_codes"] = ["m"]
        item["assessment"]["misconception_results"] = [
            MisconceptionResult(
                code="m",
                relation=relation,
                rubric_keys=["core"],
                answer_quotes=["核心"],
                reason="依据",
            ).model_dump()
        ]
        evidence.append(item)
        result = project(evidence, ["o"], [definition], at=datetime(2026, 1, 1, tzinfo=UTC))["o"]
        if group in {"b", "c"}:
            assert result["status"] == "needs_remediation"
    assert result["status"] == "provisionally_mastered"


def test_refutation_not_targeted_rejected():
    q = question()
    a = Assessment(
        status="needs_review",
        items=[],
        feedback="",
        misconception_results=[
            MisconceptionResult(
                code="m",
                relation="refutes",
                rubric_keys=["core"],
                answer_quotes=["核心"],
                reason="依据",
            )
        ],
    )
    with pytest.raises(AppError, match="普通题"):
        validate_assessment(a, q, "核心", {"m"})
