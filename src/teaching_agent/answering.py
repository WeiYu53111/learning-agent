"""Question presentation and durable multiline answer composition."""

import re


def question_text(question):
    return "\n".join([question["stem"], *question.get("parts", [])])


def question_parts(question):
    if question.get("parts"):
        return question["stem"], list(question["parts"])
    stem = question["stem"]
    if "```" in stem or "~~~" in stem:
        return "", [stem]
    matches = list(re.finditer(r"^(\d+)([.、)])[ \t]*([^\d\s][^\n]*)", stem, re.MULTILINE))
    # Only a clearly numbered final sequence with a nonempty shared context is split.
    if (
        len(matches) not in (2, 3)
        or len(re.findall(r"^\d+[.、)]", stem, re.MULTILINE)) != len(matches)
        or [int(m[1]) for m in matches] != list(range(1, len(matches) + 1))
        or len({m[2] for m in matches}) != 1
        or not stem[: matches[0].start()].strip()
        or re.search(r"^[ \t]+\d+[.、)]", stem, re.MULTILINE)
    ):
        return "", [stem]
    parts = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stem)
        parts.append(stem[match.start(3) : end].strip())
    return stem[: matches[0].start()].strip(), parts


def answer_state(state):
    saved = state.get("answer_input")
    if saved is None or saved["question_id"] != state["question_id"]:
        context, parts = question_parts(state["task"]["question"])
        saved = {
            "question_id": state["question_id"],
            "context": context,
            "parts": parts,
            "answers": [],
            "lines": [],
        }
        state["answer_input"] = saved
    return saved


def combined_answer(saved, include_buffer=False):
    answers = list(saved["answers"])
    if include_buffer:
        answers.append("\n".join(saved["lines"]))
        answers.extend([""] * (len(saved["parts"]) - len(answers)))
    if len(saved["parts"]) == 1:
        return answers[0] if answers else ""
    return "\n\n".join(f"第 {i + 1} 问回答：\n{answer}" for i, answer in enumerate(answers))
