"""Deterministic book operations for the explicitly selected offline demo."""

from .models import Question, Ref, Rubric


def book_operation(operation, payload, contract):
    chunks = payload.get("chunks", [])
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
