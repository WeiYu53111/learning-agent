"""Replayable evidence projection; smoothed scores never gate mastery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import AppError, Assessment, Question


def validate_assessment(assessment: Assessment, question: Question, answer: str, codes):
    known = {r.key for r in question.rubric}
    keys = [r.rubric_key for r in assessment.items]
    if assessment.status == "valid" and (set(keys) != known or len(keys) != len(known)):
        raise AppError("评分项与固定 rubric 不一致，回答已保留。")
    for item in assessment.items:
        if item.rubric_key not in known:
            raise AppError("评分引用未知 rubric。")
        if any(not q or q not in answer for q in item.answer_quotes):
            raise AppError("评分引用不在原回答中。")
        if item.credit and not item.answer_quotes:
            raise AppError("给分缺少回答证据。")
    results = assessment.misconception_results
    if len({r.code for r in results}) != len(results):
        raise AppError("误解评估重复。")
    if assessment.status == "valid" and not set(question.targeted_misconception_codes) <= {
        r.code for r in results
    }:
        raise AppError("缺少针对误解的评估结果。")
    for result in results:
        if result.code not in codes or not set(result.rubric_keys) <= known:
            raise AppError("误解码或评分依据不存在。")
        if any(not q or q not in answer for q in result.answer_quotes):
            raise AppError("误解评估引用不在原回答中。")
        if result.relation != "inconclusive" and (
            not result.rubric_keys or not result.answer_quotes
        ):
            raise AppError("误解结论缺少证据。")
        if (
            result.relation == "refutes"
            and result.code not in question.targeted_misconception_codes
        ):
            raise AppError("普通题不能解除未针对的误解。")


def score(assessment: Assessment, question: Question):
    if assessment.status != "valid":
        return None, False
    items = {r.rubric_key: r.credit for r in assessment.items}
    value = sum(r.weight * items[r.key] for r in question.rubric)
    passed = value >= 0.8 and all(items[r.key] == 1 for r in question.rubric if r.critical)
    return value, passed


def project(evidence, objective_keys, misconceptions=(), at=None):
    at = at or datetime.now(UTC)
    states = {
        key: {
            "score": None,
            "status": "unknown",
            "groups": set(),
            "gap": False,
            "due": None,
            "stage": 0,
            "count": 0,
        }
        for key in objective_keys
    }
    patterns = {
        m.code: {"status": "unknown", "supports": set(), "refutes": set()} for m in misconceptions
    }
    for entry in evidence:
        assessment = Assessment.model_validate(entry["assessment"])
        question = Question.model_validate(entry["question"])
        key = entry["objective_key"]
        if key not in states or assessment.status != "valid" or entry["practice_only"]:
            continue
        state = states[key]
        value, passed = score(assessment, question)
        group = question.novelty_group
        independent = not entry["assisted"]
        adjusted = value * (1 if independent else 0.7)
        state["score"] = (
            adjusted if state["score"] is None else (0.6 * state["score"] + 0.4 * adjusted)
        )
        state["count"] += 1
        if independent:
            if passed:
                state["groups"].add(group)
                state["gap"] = False
            else:
                state["groups"].clear()
                state["gap"] = True
                state["due"] = None
                state["stage"] = 0
        for result in assessment.misconception_results:
            pattern = patterns.get(result.code)
            if pattern is None:
                continue
            if result.relation == "supports":
                if pattern["status"] == "resolved":
                    pattern["supports"].clear()
                pattern["supports"].add(group)
                pattern["refutes"].clear()
                pattern["status"] = "confirmed" if len(pattern["supports"]) >= 2 else "suspected"
            elif result.relation == "refutes" and independent and passed:
                pattern["refutes"].add(group)
                if len(pattern["refutes"]) >= 2:
                    pattern["status"] = "resolved"
        # Misconceptions may affect multiple objectives. Re-evaluate all states.
        for obj_key, obj_state in states.items():
            blocked = any(
                patterns[m.code]["status"] == "confirmed"
                for m in misconceptions
                if obj_key in m.objective_keys
            )
            ready = len(obj_state["groups"]) >= 2 and not obj_state["gap"] and not blocked
            if ready and obj_state["due"] is None:
                obj_state["due"] = datetime.fromisoformat(entry["created_at"]) + timedelta(days=1)
                obj_state["stage"] = 0
            elif not ready:
                obj_state["due"] = None
        if (
            state["due"]
            and entry["action"] == "REVIEW"
            and independent
            and passed
            and datetime.fromisoformat(entry["created_at"]) >= state["due"]
        ):
            state["stage"] = min(state["stage"] + 1, 3)
            state["due"] = datetime.fromisoformat(entry["created_at"]) + timedelta(
                days=[1, 3, 7, 14][state["stage"]]
            )
    for key, state in states.items():
        blocked = any(
            patterns[m.code]["status"] == "confirmed"
            for m in misconceptions
            if key in m.objective_keys
        )
        if state["gap"] or blocked:
            state["status"] = "needs_remediation"
        elif len(state["groups"]) >= 2:
            state["status"] = (
                "review_due" if state["due"] and at >= state["due"] else ("provisionally_mastered")
            )
        elif state["count"]:
            state["status"] = "learning"
        state["independent_passes"] = len(state.pop("groups"))
        state["due"] = state["due"].isoformat() if state["due"] else None
        state["misconceptions"] = [
            m.code
            for m in misconceptions
            if key in m.objective_keys and patterns[m.code]["status"] == "confirmed"
        ]
    return states
