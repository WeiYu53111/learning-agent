"""Deterministic book operations for the explicitly selected offline demo."""

from .models import Question, Ref, Rubric


def book_operation(operation, payload, contract):
    chunks = payload.get("chunks", [])
    if operation == "source_analysis":
        units = payload["units"]
        return contract(
            regions=[
                dict(
                    first=units[0]["id"],
                    last=units[-1]["id"],
                    role="teaching",
                    path=[payload["section"]],
                    topic=payload["section"],
                    reason="离线演示正文",
                )
            ]
        )
    if operation == "extraction_review":
        return contract(issues=[])
    if operation == "boundary_review":
        return contract(merges=[])
    if operation == "book_index":
        return contract.model_validate(
            {
                "points": [
                    {
                        "key": f"p{i}",
                        "title": f"知识点 {i + 1}",
                        "description": c["text"],
                        "source_refs": [{"chunk_id": c["id"], "quote": c["text"].strip()}],
                    }
                    for i, c in enumerate(chunks)
                ],
                "items": [
                    {
                        "description": c["text"],
                        "kind": "mechanism",
                        "point_keys": [f"p{i}"],
                        "source_refs": [{"chunk_id": c["id"], "quote": c["text"].strip()}],
                    }
                    for i, c in enumerate(chunks)
                ],
            }
        )
    if operation == "book_summary":
        return contract(summary="离线演示：本章知识结构与原文导航。")
    if operation == "book_relations":
        return contract(relations=[])
    if operation == "learning_structure":
        # The bundled textbook keeps its established deterministic exercises.
        if chunks and all("Python 对象" in c["text"] for c in chunks):
            from .demo import DemoGateway
            from .models import Chunk

            draft = DemoGateway().fixture_lesson([Chunk.model_validate(c) for c in chunks])
            value = draft.model_dump()
            objective_keys = set(draft.objectives())
            for point in value["points"]:
                refs = {r["chunk_id"] for o in point["objectives"] for r in o["source_refs"]}
                point["knowledge_keys"] = [
                    p["key"]
                    for p in payload["knowledge_points"]
                    if any(r["chunk_id"] in refs for r in p["source_refs"])
                ]
                for obj in point["objectives"]:
                    del obj["sample_question"]
                    obj["prerequisites"] = [k for k in obj["prerequisites"] if k in objective_keys]
            return contract.model_validate(value)
        points, items = [], []
        for i, chunk in enumerate(chunks):
            refs = [{"chunk_id": chunk["id"], "quote": chunk["text"].strip()}]
            knowledge = [
                p["key"]
                for p in payload["knowledge_points"]
                if any(r["chunk_id"] == chunk["id"] for r in p["source_refs"])
            ]
            points.append(
                {
                    "key": f"p{i}",
                    "knowledge_keys": knowledge,
                    "title": f"主题 {i + 1}",
                    "objectives": [
                        {
                            "key": f"o{i}",
                            "description": "解释原文原理",
                            "dimension": "reasoning",
                            "source_refs": refs,
                        }
                    ],
                }
            )
            items.append(
                {
                    "key": f"i{i}",
                    "description": chunk["text"],
                    "kind": "mechanism",
                    "source_refs": refs,
                    "objective_keys": [f"o{i}"],
                }
            )
        return contract(
            points=points, content_items=items, notes=["离线演示，不代表真实教学质量。"]
        )
    if operation == "learning_questions":
        questions = []
        for obj in payload["objectives"]:
            refs = [Ref.model_validate(r) for r in obj["source_refs"]]
            if refs and "Python 对象" in refs[0].quote:
                from .demo import LESSONS

                title, text, words = next(row for row in LESSONS if row[0] in refs[0].quote)
                question = Question(
                    stem=f"请解释{title}，并说明原因。",
                    reference_answer=text,
                    rubric=[
                        Rubric(key=f"r{i}", description=w, weight=0.5) for i, w in enumerate(words)
                    ],
                    hints=[f"请考虑：{words[0]}"],
                    novelty_group=f"definition-{obj['key']}",
                    source_refs=refs,
                )
                questions.append({"objective_key": obj["key"], "question": question})
                continue
            question = Question(
                stem="请解释原文的核心原理。",
                reference_answer=refs[0].quote,
                rubric=[Rubric(key="r1", description="解释核心原理", weight=1.0)],
                novelty_group=obj["key"],
                source_refs=refs,
            )
            questions.append({"objective_key": obj["key"], "question": question})
        return contract(questions=questions)
    if operation == "book_answer":
        return contract(
            sufficient=True,
            parts=[
                {
                    "text": "离线演示原文：" + chunks[0]["text"],
                    "source_refs": [
                        {"chunk_id": chunks[0]["id"], "quote": chunks[0]["text"].strip()}
                    ],
                }
            ],
        )
    raise ValueError("未知离线书籍操作")
