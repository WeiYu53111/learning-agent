"""Local read-only HTTP projections. Never initializes a store or instantiates a gateway."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .book_learning import knowledge_key
from .book_models import KnowledgePoint, Navigation, NavigationItem, Relation
from .extraction_models import PIPELINE
from .models import AppError, Book, Chunk, Section
from .storage import Store, now

STATIC = Path(__file__).with_name("web_static")


class BookInfo(BaseModel):
    id: str
    filename: str
    page_count: int
    section_count: int
    unavailable_pages: list[int]


class IndexInfo(BaseModel):
    id: str
    created_at: str
    status: Literal["ready", "building"]
    pipeline: str | None = None


class Catalog(BaseModel):
    book: BookInfo
    sections: list[Section]
    indexes: list[IndexInfo]


class Point(KnowledgePoint):
    section_id: str


class Coverage(BaseModel):
    task_id: str
    section_id: str
    status: str
    total: int
    checked: int
    with_relations: int
    without_relations: int
    unexpanded: int
    last_error: str | None


class ChapterView(BaseModel):
    section: Section
    index: IndexInfo | None
    source: Literal["snapshot", "checkpoints", "original"]
    points: list[Point] = Field(default_factory=list)
    items: list[NavigationItem] = Field(default_factory=list)
    summary: str | None = None
    saved_batches: int = 0
    last_step: str | None = None
    last_error: str | None = None
    coverage: list[Coverage] = Field(default_factory=list)
    coverage_available: bool = False
    read_at: str = Field(default_factory=now)
    quality: Literal["pending", "reviewed", "legacy"] = "pending"
    exclusions: list[dict] = Field(default_factory=list)


class PairView(BaseModel):
    other_key: str
    status: Literal["with_relations", "without_relations", "unchecked"]
    task_id: str
    section_id: str


class PointView(BaseModel):
    point: Point
    neighbors: list[Point]
    relations: list[Relation]
    pairs: list[PairView]
    coverage_available: bool
    missing_keys: list[str]


class PageView(BaseModel):
    page: int
    page_count: int
    unavailable: bool
    chunks: list[Chunk]


def info(book):
    return BookInfo(**book.model_dump(), section_count=len(book.sections))


class Reader:
    def __init__(self, conn):
        self.conn = conn
        self.tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}

    def rows(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args)]

    def book(self, book_id):
        rows = self.rows("SELECT data FROM books WHERE id=?", (book_id,))
        if not rows:
            raise HTTPException(404, "教材不存在，请重新选择。")
        return Book.model_validate_json(rows[0]["data"])

    def indexes(self, book_id):
        if "book_indexes" not in self.tables:
            return []
        rows = self.rows(
            "SELECT id,created_at,status,data FROM book_indexes WHERE book_id=? "
            "ORDER BY created_at DESC,id DESC",
            (book_id,),
        )
        states = {}
        if "index_content_state" in self.tables:
            states = {
                r["index_id"]: r["status"]
                for r in self.rows("SELECT index_id,status FROM index_content_state")
            }
        return [
            IndexInfo(
                **{
                    **r,
                    "status": states.get(r["id"], r["status"]),
                    "pipeline": json.loads(r["data"]).get("pipeline"),
                }
            )
            for r in rows
        ]

    def content(self, book_id, index_id):
        indexes = self.indexes(book_id)
        selected = (
            next((i for i in indexes if i.id == index_id), None)
            if index_id
            else next((i for i in indexes if i.pipeline == PIPELINE), None)
        )
        if index_id and not selected:
            raise HTTPException(404, "索引不存在或不属于此教材。")
        if not selected:
            return None, {"points": [], "items": [], "summaries": {}, "relations": []}, []
        data = json.loads(
            self.rows("SELECT data FROM book_indexes WHERE id=?", (selected.id,))[0]["data"]
        )
        steps = (
            self.rows(
                "SELECT step,data FROM book_steps WHERE owner_id=? ORDER BY step", (selected.id,)
            )
            if "book_steps" in self.tables
            else []
        )
        if selected.status == "ready":
            return selected, data, steps
        if selected.pipeline == PIPELINE:
            metadata = data
            data = {
                "points": [],
                "items": [],
                "summaries": {},
                "relations": [],
                "regions": [],
                "reviewed_sections": [],
            }
            if metadata.get("base_index"):
                base = json.loads(
                    self.rows(
                        "SELECT data FROM book_indexes WHERE id=?", (metadata["base_index"],)
                    )[0]["data"]
                )
                rebuilt = metadata["rebuild_section"]
                data["points"] = [p for p in base["points"] if p["section_id"] != rebuilt]
                keys = {p["key"] for p in data["points"]}
                data["items"] = [it for it in base["items"] if keys.intersection(it["point_keys"])]
                data["regions"] = [r for r in base["regions"] if r["section_id"] != rebuilt]
                data["summaries"] = {
                    s: text for s, text in base["summaries"].items() if s != rebuilt
                }
                data["reviewed_sections"] = list(data["summaries"])
                data["relations"] = [
                    e for e in base["relations"] if e["source"] in keys and e["target"] in keys
                ]
            for row in steps:
                if row["step"].endswith(f":{PIPELINE}:complete"):
                    value = json.loads(row["data"])
                    for key in ("points", "items"):
                        data[key].extend(value[key])
                    data["reviewed_sections"].append(row["step"].split(":", 1)[0])
                if row["step"].endswith(f":{PIPELINE}:regions"):
                    data["regions"].extend(json.loads(row["data"])["regions"])
            return selected, data, steps
        # Only successful extraction checkpoints; summaries are not final until snapshot commit.
        data = {"points": [], "items": [], "summaries": {}, "relations": []}
        extracts = [
            (match[1], int(match[2]), row)
            for row in steps
            if (match := re.fullmatch(r"([^:]+):extract:(\d+)", row["step"]))
        ]
        for section_id, _, row in sorted(extracts, key=lambda value: value[:2]):
            nav = Navigation.model_validate_json(row["data"])
            mapping = {p.key: knowledge_key(section_id, p) for p in nav.points}
            data["points"].extend(
                {**p.model_dump(), "key": mapping[p.key], "section_id": section_id}
                for p in nav.points
            )
            data["items"].extend(
                {**item.model_dump(), "point_keys": [mapping[k] for k in item.point_keys]}
                for item in nav.items
            )
        return selected, data, steps

    def pairs(self, index_id):
        if not {"relation_tasks", "relation_task_pairs", "relation_checks"} <= self.tables:
            return []
        return self.rows(
            "SELECT p.*,c.result,t.section_id FROM relation_task_pairs p "
            "JOIN relation_tasks t ON t.id=p.task_id "
            "LEFT JOIN relation_checks c ON c.id=p.check_id WHERE t.index_id=?",
            (index_id,),
        )

    def coverage(self, index_id, section_id):
        if "relation_tasks" not in self.tables:
            return []
        pairs = self.pairs(index_id)
        result = []
        for task in self.rows(
            "SELECT * FROM relation_tasks WHERE index_id=? AND section_id IN ('',?)",
            (index_id, section_id),
        ):
            members = [p for p in pairs if p["task_id"] == task["id"]]
            checked = [p for p in members if p["result"] is not None]
            positive = sum(bool(json.loads(p["result"])) for p in checked)
            frontier = json.loads(task["frontier"])
            status = (
                "candidate_complete"
                if len(checked) == len(members) and not frontier
                else ("partial" if members or frontier else "not_started")
            )
            result.append(
                Coverage(
                    task_id=task["id"],
                    section_id=task["section_id"],
                    status=status,
                    total=len(members),
                    checked=len(checked),
                    with_relations=positive,
                    without_relations=len(checked) - positive,
                    unexpanded=len(frontier),
                    last_error=task["last_error"],
                )
            )
        return result


@contextmanager
def read(store):
    if not store.path.exists():
        raise HTTPException(404, "暂无数据，请先使用 teach import 导入教材。")
    with store.connect(readonly=True) as conn:
        conn.execute("BEGIN")
        yield Reader(conn)


def create_app(store: Store, static_dir: Path = STATIC):
    app = FastAPI(title="教材检查工作台", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={"detail": "数据库暂不可读，请稍后刷新。"})

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/books", response_model=list[BookInfo])
    def books():
        if not store.path.exists():
            return []
        with read(store) as r:
            return [
                info(Book.model_validate_json(row["data"]))
                for row in r.rows("SELECT data FROM books ORDER BY id")
            ]

    @app.get("/api/books/{book_id}", response_model=Catalog)
    def catalog(book_id: str):
        with read(store) as r:
            book = r.book(book_id)
            return Catalog(book=info(book), sections=book.sections, indexes=r.indexes(book_id))

    @app.get("/api/books/{book_id}/sections/{section_id}", response_model=ChapterView)
    def chapter(book_id: str, section_id: str, index: str | None = None):
        with read(store) as r:
            book = r.book(book_id)
            section = next((s for s in book.sections if s.id == section_id), None)
            if not section:
                raise HTTPException(404, "章节不存在。")
            selected, data, steps = r.content(book_id, index)
            points = [p for p in data.get("points", []) if p["section_id"] == section_id]
            keys = {p["key"] for p in points}
            chapter_steps = [s for s in steps if s["step"].startswith(section_id + ":")]
            failures = []
            last_step = None
            if selected and "events" in r.tables:
                events = r.rows(
                    "SELECT kind,data FROM events WHERE owner_id=? AND kind IN "
                    "('book_operation_failed','book_operation_completed') ORDER BY id DESC",
                    (selected.id,),
                )
                for event in events:
                    payload = json.loads(event["data"])
                    if not payload.get("step", "").startswith(section_id + ":"):
                        continue
                    if last_step is None:
                        last_step = payload["step"]
                    if event["kind"] == "book_operation_failed":
                        failures = payload.get("errors", [])
                        break
            return ChapterView(
                section=section,
                index=selected,
                source="original"
                if not selected
                else ("snapshot" if selected.status == "ready" else "checkpoints"),
                points=points,
                items=[i for i in data.get("items", []) if keys.intersection(i["point_keys"])],
                summary=data.get("summaries", {}).get(section_id),
                saved_batches=sum(
                    bool(re.fullmatch(r"[^:]+:(?:semantic-v1:)?extract:\d+", s["step"]))
                    for s in chapter_steps
                ),
                quality=(
                    "legacy"
                    if selected and selected.pipeline != PIPELINE
                    else "reviewed"
                    if selected
                    and (
                        selected.status == "ready"
                        or section_id in data.get("reviewed_sections", [])
                        or any(s["step"] == f"{section_id}:{PIPELINE}:complete" for s in steps)
                    )
                    else "pending"
                ),
                exclusions=[
                    {
                        **region,
                        "quote": next(
                            c.text[region["start"] : region["end"]]
                            for c in book.chunks
                            if c.id == region["chunk_id"]
                        ),
                    }
                    for region in data.get("regions", [])
                    if region["section_id"] == section_id and region["excluded"]
                ],
                last_step=last_step,
                last_error="；".join(failures) or None,
                coverage=r.coverage(selected.id, section_id) if selected else [],
                coverage_available="relation_tasks" in r.tables,
            )

    @app.get("/api/books/{book_id}/points/{key}", response_model=PointView)
    def point(book_id: str, key: str, index: str = Query()):
        with read(store) as r:
            r.book(book_id)
            selected, data, _ = r.content(book_id, index)
            points = {p["key"]: p for p in data.get("points", [])}
            if key not in points:
                raise HTTPException(404, "此索引中没有该知识点。")
            edges = [e for e in data.get("relations", []) if key in (e["source"], e["target"])]
            other_keys = {k for e in edges for k in (e["source"], e["target"]) if k != key}
            pairs = []
            for p in r.pairs(selected.id):
                if key not in (p["left_key"], p["right_key"]):
                    continue
                other = p["right_key"] if key == p["left_key"] else p["left_key"]
                other_keys.add(other)
                status = (
                    "unchecked"
                    if p["result"] is None
                    else ("with_relations" if json.loads(p["result"]) else "without_relations")
                )
                pairs.append(
                    PairView(
                        other_key=other,
                        status=status,
                        task_id=p["task_id"],
                        section_id=p["section_id"],
                    )
                )
            return PointView(
                point=points[key],
                neighbors=[points[k] for k in sorted(other_keys) if k in points],
                relations=edges,
                pairs=pairs,
                coverage_available="relation_tasks" in r.tables,
                missing_keys=sorted(other_keys - points.keys()),
            )

    @app.get("/api/books/{book_id}/pages/{page}", response_model=PageView)
    def page(book_id: str, page: int):
        with read(store) as r:
            book = r.book(book_id)
            if not 1 <= page <= book.page_count:
                raise HTTPException(404, "页码超出教材范围。")
            return PageView(
                page=page,
                page_count=book.page_count,
                unavailable=page in book.unavailable_pages,
                chunks=[c for c in book.chunks if c.page == page],
            )

    @app.get("/api/books/{book_id}/chunks/{chunk_id}", response_model=Chunk)
    def chunk(book_id: str, chunk_id: str):
        with read(store) as r:
            book = r.book(book_id)
            result = next((c for c in book.chunks if c.id == chunk_id), None)
            if result is None:
                raise HTTPException(404, "引用的原文文本块不存在。")
            return result

    # Unknown API paths must not return the SPA HTML.
    @app.get("/api/{path:path}", include_in_schema=False)
    def missing_api(path: str):
        raise HTTPException(404, "接口不存在。")

    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def home():
        if not (static_dir / "index.html").is_file():
            raise HTTPException(503, "前端尚未构建，请在 web 目录执行 npm ci 和 npm run build。")
        return FileResponse(static_dir / "index.html")

    return app


def serve(store: Store, port: int):
    import uvicorn

    if not 1 <= port <= 65535:
        raise AppError("端口必须在 1–65535 之间。")
    print(f"只读检查工作台：http://127.0.0.1:{port}（Ctrl+C 退出）", flush=True)
    if not (STATIC / "index.html").is_file():
        print("前端尚未构建：在 web 目录执行 npm ci 和 npm run build；API 仍可用于开发。")
    uvicorn.run(create_app(store), host="127.0.0.1", port=port)
