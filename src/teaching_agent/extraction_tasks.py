"""Extraction-only control channel, independent of the library writer lock."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager

from .models import AppError
from .storage import now, uid


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class ExtractionStopped(AppError):
    pass


class Tasks:
    def __init__(self, root):
        self.root = root

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.root / "extraction-control.sqlite", timeout=10) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def list(self, book=None):
        if not (self.root / "extraction-control.sqlite").exists():
            return []
        with self.connect() as conn:
            values = [
                json.loads(r[0]) for r in conn.execute("SELECT data FROM tasks ORDER BY rowid DESC")
            ]
            for value in values:
                if (
                    value["status"] in {"running", "stopping", "queued"}
                    and value.get("pid")
                    and not process_alive(value["pid"])
                ):
                    value.update(status="interrupted", error="执行进程已退出，可从断点继续。")
                    conn.execute(
                        "UPDATE tasks SET data=? WHERE id=?", (json.dumps(value), value["id"])
                    )
        return [v for v in values if book is None or v["book"] == book]

    def update(self, task, **changes):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM tasks WHERE id=?", (task,)).fetchone()
            if not row:
                raise AppError("抽取任务不存在。")
            value = json.loads(row[0])
            if value["status"] == "stopping" and changes.get("status") == "running":
                raise ExtractionStopped("已停止，将从当前文本块重新抽取。")
            value.update(changes, updated_at=now())
            conn.execute(
                "UPDATE tasks SET data=? WHERE id=?", (json.dumps(value, ensure_ascii=False), task)
            )
            return value

    def create(self, book, section=None, new=False):
        task = uid("extract")
        value = dict(
            id=task,
            book=book,
            section=section,
            new=new,
            status="queued",
            pid=os.getpid(),
            updated_at=now(),
        )
        with self.connect() as conn:
            conn.execute("INSERT INTO tasks VALUES(?,?)", (task, json.dumps(value)))
        return task

    def stop(self, task):
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM tasks WHERE id=?", (task,)).fetchone()
            if not row:
                raise AppError("抽取任务不存在。")
            value = json.loads(row[0])
            if value["status"] in {"queued", "running"}:
                value.update(status="stopping", updated_at=now())
                conn.execute("UPDATE tasks SET data=? WHERE id=?", (json.dumps(value), task))
            return value

    def check(self, task):
        self.update(task, status="running")

    def delete_book(self, book):
        if not (self.root / "extraction-control.sqlite").exists():
            return
        with self.connect() as conn:
            ids = [
                row[0]
                for row in conn.execute("SELECT id,data FROM tasks")
                if json.loads(row[1])["book"] == book
            ]
            conn.executemany("DELETE FROM tasks WHERE id=?", [(task,) for task in ids])


def launch(store, book, section=None, new=False, resume=None, config=None, backend=None):
    from .storage import writer_lock

    with writer_lock(store.root):
        tasks = Tasks(store.root)
        if any(t["status"] in {"queued", "running", "stopping"} for t in tasks.list()):
            raise AppError("已有抽取任务，请先停止或等待完成。")
        prior = None
        if resume:
            prior = next((t for t in tasks.list(book) if t["id"] == resume), None)
            if not prior or prior["status"] not in {"stopped", "failed", "interrupted"}:
                raise AppError("任务不存在或不可恢复。")
            section, new = prior["section"], False
            config, backend = prior.get("config", config), prior.get("backend", backend)
        task = tasks.create(book, section, new)
        tasks.update(task, config=str(config) if config else None, backend=backend)
        if prior:
            tasks.update(task, resume_index=prior.get("index"))
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "teaching_agent.extraction_worker",
                    str(store.root.resolve()),
                    task,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            tasks.update(task, pid=process.pid)
        except OSError as exc:
            tasks.update(task, status="failed", error=str(exc))
            raise
    return task
