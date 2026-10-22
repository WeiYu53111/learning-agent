"""Replayable evidence projection; smoothed scores never gate mastery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import AppError, Assessment, EvidenceItem, Question


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
        if assessment.rule_version == 2 and not isinstance(item, EvidenceItem):
            raise AppError("能力评价必须返回逐项判断。")
        if assessment.rule_version == 1 and isinstance(item, EvidenceItem):
            raise AppError("旧评分必须返回分数。")
        requires_quote = (
            item.verdict != "insufficient" if isinstance(item, EvidenceItem) else bool(item.credit)
        )
        if requires_quote and not item.answer_quotes:
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


def outcome(assessment: Assessment, question: Question):
    if assessment.status != "valid":
        return assessment.status
    if assessment.rule_version == 1:
        return "passed" if score(assessment, question)[1] else "incorrect"
    items = {i.rubric_key: i for i in assessment.items}
    verdicts = [items[r.key].verdict for r in question.rubric if r.critical]
    if "incorrect" in verdicts:
        return "incorrect"
    if "insufficient" in verdicts:
        return "insufficient"
    return "passed"


def score(assessment: Assessment, question: Question):
    if assessment.status != "valid":
        return None, False
    if assessment.rule_version == 2:
        return None, outcome(assessment, question) == "passed"
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
            "gaps": {},
            "effective": 0,
            "rule_version": 1,
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
        state["rule_version"] = max(state["rule_version"], assessment.rule_version)
        if outcome(assessment, question) == "insufficient":
            continue
        value, passed = score(assessment, question)
        group = question.novelty_group
        independent = not entry["assisted"]
        if value is not None:
            adjusted = value * (1 if independent else 0.7)
            state["score"] = (
                adjusted if state["score"] is None else (0.6 * state["score"] + 0.4 * adjusted)
            )
        state["count"] += 1
        if independent:
            if passed:
                if group not in state["groups"]:
                    state["effective"] += 1
                state["groups"].add(group)
                state["gap"] = False
                if assessment.rule_version == 2:
                    items = {i.rubric_key: i for i in assessment.items}
                    for coverage in question.gap_coverage:
                        if all(
                            k in items and items[k].verdict == "met" for k in coverage.rubric_keys
                        ):
                            state["gaps"].pop(coverage.gap_id, None)
            else:
                if assessment.rule_version == 1:
                    state["groups"].clear()
                    state["effective"] = 0
                    state["gap"] = True
                else:
                    state["effective"] = max(0, state["effective"] - 1)
                    items = {i.rubric_key: i for i in assessment.items}
                    for rubric in question.rubric:
                        if rubric.critical and items[rubric.key].verdict == "incorrect":
                            gap_id = entry["id"] + ":" + rubric.key
                            state["gaps"][gap_id] = {
                                "gap_id": gap_id,
                                "requirement": rubric.description,
                                "reason": items[rubric.key].reason,
                            }
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
            ready = (
                obj_state["effective"] >= 2
                and not obj_state["gap"]
                and not obj_state["gaps"]
                and not blocked
            )
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
        if state["gap"] or state["gaps"] or blocked:
            state["status"] = "needs_remediation"
        elif state["effective"] >= 2:
            state["status"] = (
                "review_due" if state["due"] and at >= state["due"] else ("provisionally_mastered")
            )
        elif state["count"]:
            state["status"] = "learning"
        state["historical_passes"] = len(state.pop("groups"))
        state["independent_passes"] = state.pop("effective")
        state["gaps"] = list(state["gaps"].values())
        state["due"] = state["due"].isoformat() if state["due"] else None
        state["misconceptions"] = [
            m.code
            for m in misconceptions
            if key in m.objective_keys and patterns[m.code]["status"] == "confirmed"
        ]
    return states
