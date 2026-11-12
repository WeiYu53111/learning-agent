import pytest
from fastapi.testclient import TestClient

from teaching_agent.demo import make_pdf
from teaching_agent.storage import dumps
from teaching_agent.web import create_app


def test_selection_inbox_deduplicates_and_retains_source(app, tmp_path):
    path = tmp_path / "selection.md"
    path.write_text("# 标题\n知识摘录", encoding="utf-8")
    book = app.import_source(path)
    client = TestClient(create_app(app.store))
    payload = {"book": book.id, "quote": "知识摘录", "start_line": 2, "end_line": 2}
    first = client.post("/api/personal/selections", json=payload)
    assert first.status_code == 200
    second = client.post("/api/personal/selections", json=payload)
    assert first.json()["id"] == second.json()["id"]
    source = first.json()["sources"][0]
    assert source["sha256"] == book.sha256
    assert source["location"]["start_line"] == 2
    result = client.get("/api/personal").json()
    assert len(result["inbox"]) == 1
    assert result["cards"] == []
    assert (
        client.post("/api/personal/selections", json={**payload, "quote": " "}).status_code == 400
    )
    assert (
        client.post("/api/personal/selections", json={**payload, "end_line": 1}).status_code == 400
    )


def test_pdf_worker_has_javascript_mime(app, tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "worker.mjs").write_text("export {};", encoding="utf-8")
    client = TestClient(create_app(app.store, static_dir=tmp_path))
    response = client.get("/assets/worker.mjs")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


def test_pdf_bytes_range_download_and_legacy(app, tmp_path):
    path = tmp_path / "原始教材.pdf"
    make_pdf(path)
    book = app.import_source(path)
    client = TestClient(create_app(app.store))
    url = f"/api/books/{book.id}/file"
    assert client.get(url).content == path.read_bytes()
    assert client.get(url).headers["content-type"] == "application/pdf"
    assert client.get(url).headers["content-disposition"].startswith("inline;")
    assert (
        client.get(url + "?download=true").headers["content-disposition"].startswith("attachment;")
    )
    part = client.get(url, headers={"Range": "bytes=0-15"})
    assert part.status_code == 206
    assert part.content == path.read_bytes()[:16]
    assert client.get(url, headers={"Range": "bytes=999999999-"}).status_code == 416
    data = book.model_dump()
    data.pop("asset_path")
    with app.store.connect() as conn:
        conn.execute("UPDATE books SET data=? WHERE id=?", (dumps(data), book.id))
    assert client.get(url).content == path.read_bytes()
    before = app.store.path.read_bytes()
    assert client.get(f"/api/books/{book.id}/original").json()["sha256"] == book.sha256
    assert app.store.path.read_bytes() == before


@pytest.mark.parametrize(
    "suffix,encoding", [(".txt", "utf-8"), (".md", "utf-8-sig"), (".txt", "utf-16")]
)
def test_original_text_and_download(app, tmp_path, suffix, encoding):
    path = tmp_path / ("中文" + suffix)
    raw = "# 标题\r\n\r\n<script>alert(1)</script>\r\n正文".encode(encoding)
    path.write_bytes(raw)
    book = app.import_source(path)
    client = TestClient(create_app(app.store))
    data = client.get(f"/api/books/{book.id}/original").json()
    assert data["text"] == raw.decode(encoding).replace("\r\n", "\n")
    assert client.get(f"/api/books/{book.id}/file?download=true").content == raw


def test_missing_file_and_unsafe_asset_do_not_leak(app, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("正文", encoding="utf-8")
    book = app.import_source(path)
    client = TestClient(create_app(app.store))
    for asset in ["../notes.txt", str(path.resolve()), "documents/missing.txt"]:
        data = book.model_dump()
        data["asset_path"] = asset
        with app.store.connect() as conn:
            conn.execute("UPDATE books SET data=? WHERE id=?", (dumps(data), book.id))
        for endpoint in ("file", "original"):
            result = client.get(f"/api/books/{book.id}/{endpoint}")
            assert result.status_code == 404
            assert str(path) not in result.text
