"""Disposable real SQLite + HTTP service for offline browser acceptance."""

import json
import tempfile
from pathlib import Path

import uvicorn

from teaching_agent.application import Application
from teaching_agent.demo import DemoGateway
from teaching_agent.models import Book, Chunk, Section
from teaching_agent.storage import Store, dumps
from teaching_agent.web import create_app

with tempfile.TemporaryDirectory(prefix="teach-web-e2e-") as directory:
    store = Store(Path(directory))
    store.initialize()
    book = Book(
        id="book_demo",
        filename="离线验收 · 上下文与缓存.pdf",
        sha256="a" * 64,
        page_count=3,
        demo=True,
        unavailable_pages=[3],
        sections=[
            Section(id="s1", title="理解上下文", start_page=1, end_page=1, chunk_ids=["c1"]),
            Section(id="s2", title="缓存与消息前缀", start_page=2, end_page=3, chunk_ids=["c2"]),
        ],
        chunks=[
            Chunk(id="c1", page=1, text="上下文包含用户消息与模型回复，是理解缓存的基础。"),
            Chunk(
                id="c2", page=2, text="缓存复用共同的消息前缀。工具定义发生变化时，前缀也可能变化。"
            ),
        ],
    )
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO books VALUES(?,?,?)", (book.id, book.sha256, book.model_dump_json())
        )
    app = Application(store, gateway=DemoGateway())
    index = app.library.index(book.id)
    data = json.loads(index["data"])
    data["points"][0]["title"] = "上下文的组成"
    data["points"][1]["title"] = "缓存与消息前缀"
    data["relations"] = [
        dict(
            source=data["points"][0]["key"],
            target=data["points"][1]["key"],
            kind="prerequisite",
            reason="理解缓存前需要理解上下文与消息。",
        ),
        dict(
            source=data["points"][0]["key"],
            target=data["points"][1]["key"],
            kind="related",
            reason="二者都涉及消息前缀。",
        ),
    ]
    with store.connect() as conn:
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (dumps(data), index["id"]))
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            (
                "older_index",
                book.id,
                "ready",
                dumps({**data, "relations": []}),
                "2020-01-01T00:00:00Z",
            ),
        )
    uvicorn.run(create_app(store), host="127.0.0.1", port=8766)
