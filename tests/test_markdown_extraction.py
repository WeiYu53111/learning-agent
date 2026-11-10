from teaching_agent.book_models import KnowledgePoint, Navigation, NavigationItem
from teaching_agent.extraction import markdown_structure, navigation_errors
from teaching_agent.models import Chunk, Ref


def test_heading_only_region_does_not_force_fabricated_point():
    text = "# Agent 开发源码学习笔记\n\n正文解释调用流程。\n"
    chunk = Chunk(id="c", text=text)
    regions = [
        {"chunk_id": "c", "start": 0, "end": len(text), "role": "teaching", "excluded": False}
    ]
    result = markdown_structure(regions, [chunk])
    assert result[0]["excluded"]
    eligible = [r for r in result if not r["excluded"]]
    ref = Ref(chunk_id="c", quote="正文解释调用流程。")
    nav = Navigation(
        points=[
            KnowledgePoint(key="p", title="调用流程", description="正文解释", source_refs=[ref])
        ],
        items=[
            NavigationItem(
                key="i",
                description="调用流程",
                kind="mechanism",
                point_keys=["p"],
                source_refs=[ref],
            )
        ],
    )
    assert navigation_errors(nav, [chunk], eligible) == []
    assert "".join(text[r["start"] : r["end"]] for r in result) == text


def test_setext_titles_and_fenced_code_across_chunks():
    chunks = [
        Chunk(id="a", text="标题\n===\n\n```python\n"),
        Chunk(id="b", text="# comment\n```\n\n## 次级标题\n正文"),
    ]
    regions = [
        {"chunk_id": c.id, "start": 0, "end": len(c.text), "role": "teaching", "excluded": False}
        for c in chunks
    ]
    result = markdown_structure(regions, chunks)
    lookup = {c.id: c.text for c in chunks}
    excluded = "".join(
        lookup[r["chunk_id"]][r["start"] : r["end"]] for r in result if r["excluded"]
    )
    assert "标题" in excluded and "次级标题" in excluded
    assert "comment" not in excluded
