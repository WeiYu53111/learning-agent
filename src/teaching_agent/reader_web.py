"""Read-only access to retained source assets."""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


def router(store):
    api = APIRouter(prefix="/api/books")

    def source(identifier):
        from .web import read

        with read(store) as reader:
            book = reader.book(identifier)
        if not re.fullmatch(r"[0-9a-f]{64}", book.sha256):
            raise HTTPException(404, "原文件不可用。")
        relative = book.asset_path or f"documents/{book.sha256}.pdf"
        path = (store.root / relative).resolve()
        documents = (store.root / "documents").resolve()
        if not documents.is_relative_to(store.root.resolve()) or not path.is_relative_to(documents):
            raise HTTPException(404, "原文件不可用。")
        if not path.is_file():
            raise HTTPException(404, "原文件不可用；已有原文摘录仍可阅读。")
        return book, path

    @api.get("/{book_id}/file")
    def file(book_id: str, download: bool = False):
        book, path = source(book_id)
        return FileResponse(
            path,
            media_type="application/pdf"
            if book.source_type == "pdf"
            else "application/octet-stream",
            filename=book.filename,
            content_disposition_type="attachment"
            if download or book.source_type != "pdf"
            else "inline",
        )

    @api.get("/{book_id}/original")
    def original(book_id: str):
        book, path = source(book_id)
        result = {
            "id": book.id,
            "filename": book.filename,
            "sha256": book.sha256,
            "source_type": book.source_type,
            "page_count": book.page_count,
            "sections": [s.model_dump() for s in book.sections],
        }
        if book.source_type != "pdf":
            raw = path.read_bytes()
            try:
                text = raw.decode(
                    "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                )
            except UnicodeError as exc:
                raise HTTPException(400, "原文件编码无法识别。") from exc
            result["text"] = text.replace("\r\n", "\n").replace("\r", "\n")
        return result

    return api
