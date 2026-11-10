import pytest
from teaching_agent.ingestion import parse_source
from teaching_agent.models import AppError
from teaching_agent.web import create_app
from fastapi.testclient import TestClient


def test_short_markdown_paragraphs_merge(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# 第一章\n\n短段一。\n\n短段二。\n\n# 第二章\n\n正文。", encoding="utf-8")
    book = parse_source(path)
    assert len(book.sections) == 2
    assert len(book.chunks) == 2
    assert book.page_count is None
    assert book.chunks[0].location["start_line"] == 1
    assert "短段二" in book.chunks[0].text


def test_fenced_heading_not_section(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# Title\n\n```python\n# comment\n```\n\ntext", encoding="utf-8")
    assert len(parse_source(path).sections) == 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_text_encoding_and_import(app, tmp_path, encoding):
    path = tmp_path / "notes.txt"
    path.write_text("第一段。\n\n第二段。", encoding=encoding)
    client = TestClient(create_app(app.store))
    response = client.post("/api/import?filename=notes.txt", content=path.read_bytes())
    assert response.status_code == 200
    book = app.book(response.json()["id"])
    assert book.source_type == "text"
    assert app.section_chunks(book, "s1") == book.chunks
    assert client.get(f"/api/books/{book.id}/source?section=s1").json()[0]["location"]["kind"] == "text"
    assert app.import_pdf(path).id == book.id
    assert (app.store.root / book.asset_path).exists()


def test_invalid_encoding(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xffbad")
    with pytest.raises(AppError):
        parse_source(path)


def test_long_text_preserves_every_character(tmp_path):
    path = tmp_path / "long.txt"
    text = ("短段落。\n\n" * 1000) + "结尾\n"
    path.write_text(text, encoding="utf-8")
    book = parse_source(path)
    assert "".join(c.text for c in book.chunks) == text
    assert all(len(c.text) <= 3200 for c in book.chunks)
