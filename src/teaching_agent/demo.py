"""Explicit offline fixture: only used with the bundled demonstration textbook."""

from __future__ import annotations

import pymupdf

from .models import (
    AppError,
    Assessment,
    Audit,
    ContentItem,
    Draft,
    ItemResult,
    Objective,
    Point,
    Question,
    Ref,
    Rubric,
    Task,
)

LESSONS = [
    ("列表的可变性", "列表是可变对象，可以在原地修改元素。", ["可变", "修改"]),
    (
        "赋值与共享引用",
        "赋值可以让两个变量引用同一个列表，修改会影响共享该对象的变量。",
        ["同一个", "影响"],
    ),
    ("元组的边界", "元组的元素引用不能替换，但元组内部的列表可以修改。", ["不能替换", "可以修改"]),
]


def make_pdf(path):
    with pymupdf.open() as doc:
        for index, (title, text, _) in enumerate(LESSONS):
            page = doc.new_page()
            page.insert_textbox(
                pymupdf.Rect(45, 45, 550, 500),
                f"第一章 Python 对象\n{index + 1}. {title}\n{text}",
                fontsize=13,
                fontname="china-s",
            )
        doc.set_toc([[1, "第一章 Python 对象", 1]])
        doc.save(path)


class DemoGateway:
    model = "offline-demo-keyword-grader"

    def extract(self, chunks):
        points, items = [], []
        for index, chunk in enumerate(chunks):
            lesson_index = chunk.page - 1
            title, text, words = LESSONS[lesson_index]
            refs = [Ref(chunk_id=chunk.id, quote=chunk.text)]
            key = f"o{lesson_index + 1}"
            question = Question(
                stem=f"请解释{title}，并说明原因。",
                reference_answer=text,
                rubric=[
                    Rubric(key=f"r{i}", description=w, weight=0.5) for i, w in enumerate(words)
                ],
                hints=[f"请考虑：{words[0]}"],
                novelty_group=f"definition-{key}",
                source_refs=refs,
            )
            objective = Objective(
                key=key,
                description=f"解释{title}",
                dimension="reasoning",
                prerequisites=[f"o{lesson_index}"] if lesson_index else [],
                source_refs=refs,
                sample_question=question,
            )
            points.append(Point(key=f"p{lesson_index + 1}", title=title, objectives=[objective]))
            items.append(
                ContentItem(
                    key=f"i{index}",
                    description=text,
                    kind="mechanism",
                    source_refs=refs,
                    objective_keys=[key],
                )
            )
        return Draft(
            points=points,
            content_items=items,
            notes=["离线演示：题目为固定场景，评分仅按关键词，不代表真实教学质量。"],
        )

    def audit(self, draft, chunks):
        return Audit(issues=[])

    def revise(self, draft, chunks, instruction):
        raise AppError("离线演示不模拟自然语言校正。请使用 review 的 edit/pre/exclude 或JSON编辑。")

    def revise_patch(self, draft, chunks, instruction):
        raise AppError("离线演示不模拟自然语言局部修订，请使用真实模型后端或人工编辑。")

    def task(self, objective, chunks, action, history, misconceptions):
        index = int(objective.key.rsplit("o", 1)[1]) - 1
        _, text, _ = LESSONS[index]
        if action == "EXPLAIN":
            return Task(objective_key=objective.key, action=action, explanation=text)
        scenes = [
            [
                "a=[1,2]; a.append(3)，a 如何变化？这说明什么？",
                "购物清单删除一项，列表对象必须换成新对象吗？为什么？",
                "用索引修改列表第一个元素，解释对象的可变性。",
            ],
            [
                "a=[1]; b=a; b.append(2)，a 是否变化？为什么？",
                "两个名字指向同一个购物清单，经其中一个删除商品，另一个会看到什么？",
                "函数收到一个列表并原地修改，调用方为何能观察到变化？",
            ],
            [
                "t=([1],2)，比较 t[0].append(3) 和 t[1]=4。",
                "元组中保存一个购物清单，能否添加商品？能否替换元组中的清单引用？",
                "解释元组不可变与它包含的列表可变为什么不矛盾。",
            ],
        ]
        count = len(history)
        q = objective.sample_question.model_copy(deep=True)
        q.stem = "请根据下面的场景回答。"
        q.parts = [scenes[index][count % 3]]
        q.novelty_group = f"demo-{index}-{count % 3}"
        return Task(
            objective_key=objective.key,
            action=action,
            explanation=text if action == "REMEDIATE" else "",
            question=q,
        )

    def grade(self, question, answer, chunks, misconceptions):
        items = [
            ItemResult(
                rubric_key=r.key,
                credit=1 if r.description in answer else 0,
                answer_quotes=[r.description] if r.description in answer else [],
                reason="离线演示关键词匹配",
            )
            for r in question.rubric
        ]
        return Assessment(
            status="valid",
            items=items,
            feedback="离线演示评分：仅检查关键词，请使用真实模型验证语义评分。",
            correction=question.reference_answer,
        )
