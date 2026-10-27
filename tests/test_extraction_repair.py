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
    return book.id, app.injected_gateway.book_operation


def test_invalid_reference_is_repaired_before_batch_saved(app, tmp_path):
    book, original = prepare(app, tmp_path)
    requests = []

    def operation(op, payload, contract):
        value = original(op, payload, contract)
        if op == "book_index":
            requests.append(payload)
            if len(requests) == 1:
                value.points[0].source_refs[0].quote = "列表"
        return value

    app.injected_gateway.book_operation = operation
    app.library.index(book)
    assert len(requests) == 2
    assert requests[1]["previous"]
    assert any("引用不唯一" in error for error in requests[1]["errors"])
    assert app.store.rows("SELECT * FROM draft_batches") == []


def test_failed_repair_resumes_candidate_without_reextracting(app, tmp_path):
    book, original = prepare(app, tmp_path)
    requests = []

    def operation(op, payload, contract):
        if op == "book_index":
            requests.append(payload)
            if len(requests) == 2:
                raise AppError("temporary network failure")
        value = original(op, payload, contract)
        if op == "book_index" and len(requests) == 1:
            value.points[0].source_refs[0].quote = "列表"
        return value

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError):
        app.library.index(book)
    app.library.index(book)
    assert len(requests) == 3
    assert requests[2]["previous"] == requests[1]["previous"]


def test_still_invalid_repair_is_bounded_and_cannot_publish(app, tmp_path):
    book, original = prepare(app, tmp_path)
    calls = []

    def operation(op, payload, contract):
        value = original(op, payload, contract)
        if op == "book_index":
            calls.append(payload)
            value.points[0].source_refs[0].quote = "不存在的原文"
        return value

    app.injected_gateway.book_operation = operation
    with pytest.raises(AppError, match="暂停"):
        app.library.index(book)
    assert len(calls) == 2
    assert app.library.active(book) is None
