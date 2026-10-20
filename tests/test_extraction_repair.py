import pytest

from teaching_agent.demo import make_pdf
from teaching_agent.knowledge import check_refs
from teaching_agent.models import AppError, Chunk, Ref


def test_overlapping_quotes_are_ambiguous():
    chunk = Chunk(id="c", page=1, text="aaaa")
    assert check_refs([Ref(chunk_id="c", quote="aaa")], {"c": chunk})
    assert check_refs([Ref(chunk_id="c", quote="aaaa")], {"c": chunk}) == []


def prepare(app, tmp_path):
    path = tmp_path / "repair.pdf"
    make_pdf(path)
    book = app.import_pdf(path, demo=True)
    version = app.create_draft(book.id, "s1")
    chunks = app.version(version)[2]
    healthy = app.injected_gateway.extract(chunks)
    invalid = healthy.model_copy(deep=True)
    invalid.points[0].objectives[0].source_refs[0].quote = "列表"
    return version, healthy, invalid


def test_invalid_reference_is_repaired_before_batch_saved(app, tmp_path):
    version, healthy, invalid = prepare(app, tmp_path)
    app.injected_gateway.extract = lambda chunks: invalid
    instructions = []

    def revise(candidate, chunks, instruction):
        instructions.append(instruction)
        return healthy

    app.injected_gateway.revise = revise
    app.build_draft(version)
    assert len(instructions) == 1
    assert "引用不唯一" in instructions[0] and "列表" in instructions[0]
    assert app.version(version)[0]["data"] != "{}"
    assert len(app.store.rows("SELECT * FROM draft_batches")) == 1


def test_failed_repair_resumes_candidate_without_reextracting(app, tmp_path):
    version, healthy, invalid = prepare(app, tmp_path)
    app.injected_gateway.extract = lambda chunks: invalid
    calls = []

    def fail(*args):
        calls.append(1)
        raise AppError("temporary network failure")

    app.injected_gateway.revise = fail
    with pytest.raises(AppError):
        app.build_draft(version)
    assert len(calls) == 1
    assert app.store.rows("SELECT * FROM draft_batches") == []
    app.injected_gateway.extract = lambda *args: pytest.fail("must reuse saved candidate")
    app.injected_gateway.revise = lambda *args: healthy
    app.build_draft(version)
    assert app.version(version)[0]["data"] != "{}"


def test_still_invalid_repair_is_bounded_and_cannot_publish(app, tmp_path):
    version, healthy, invalid = prepare(app, tmp_path)
    app.injected_gateway.extract = lambda chunks: invalid
    calls = []

    def unchanged(*args):
        calls.append(1)
        return invalid

    app.injected_gateway.revise = unchanged
    with pytest.raises(AppError, match="候选已保存"):
        app.build_draft(version)
    assert len(calls) == 1
    assert app.version(version)[0]["data"] == "{}"
    assert app.store.rows("SELECT * FROM draft_batches") == []
    with pytest.raises(AppError):
        app.publish(version)
