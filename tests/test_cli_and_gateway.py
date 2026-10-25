import json
import os
import shutil
import sqlite3
import subprocess
import sys

import httpx
import pytest

from teaching_agent.cli import main
from teaching_agent.gateway import Gateway, Settings, strict_json
from teaching_agent.models import Audit
from teaching_agent.storage import writer_lock


def test_strict_json():
    for text in ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}']:
        with pytest.raises(ValueError):
            strict_json(text)


def test_gateway_http_contract_and_repair(monkeypatch):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        text = "invalid" if len(calls) == 1 else '{"issues":[]}'
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": text}}]}
        )

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda: original(transport=httpx.MockTransport(handler)))
    gateway = Gateway(Settings(base="https://example.invalid/v1", model="test", key="secret"))
    assert gateway.call("audit", {}, Audit).issues == []
    assert len(calls) == 2
    assert "JSON Schema" in calls[0]["messages"][0]["content"]


def test_readonly_requires_no_key(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path / "missing"), "books"]) == 1
    assert "暂无数据" in capsys.readouterr().err


def test_books_from_wal_database_without_sidecars(tmp_path, capsys):
    from teaching_agent.storage import Store

    source = tmp_path / "source.db"
    with sqlite3.connect(source) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE books(id TEXT, data TEXT)")
        conn.execute("INSERT INTO books VALUES (?, ?)", (
            "book_saved", json.dumps({"filename": "saved.pdf", "page_count": 12})
        ))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    root = tmp_path / "copied"
    root.mkdir()
    shutil.copyfile(source, root / "learning.db")
    assert not (root / "learning.db-wal").exists()
    assert main(["--data-dir", str(root), "books"]) == 0
    assert "book_saved | saved.pdf | 12 页" in capsys.readouterr().out
    store = Store(root)
    with store.connect(readonly=True) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM books")
    assert len(store.rows("SELECT * FROM books")) == 1


def test_demo_and_piped_review(tmp_path, monkeypatch, capsys):
    root = tmp_path / "cli"
    assert main(["--data-dir", str(root), "demo"]) == 0
    from teaching_agent.storage import Store

    version = Store(root).rows("SELECT id FROM versions")[0]["id"]
    inputs = iter(["approve", "publish"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    assert main(["--data-dir", str(root), "review", version]) == 0
    assert main(["--data-dir", str(root), "start", version, "--no-chat"]) == 0
    output = capsys.readouterr().out
    assert "离线演示" in output
    assert "问题（第 1/1 问）" in output


def test_single_writer_across_processes(tmp_path):
    script = (
        "from pathlib import Path\n"
        "from teaching_agent.storage import writer_lock\n"
        "from teaching_agent.models import AppError\n"
        "import sys\n"
        "try:\n"
        "    with writer_lock(Path(sys.argv[1])): print('acquired')\n"
        "except AppError: print('busy')\n"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    with writer_lock(tmp_path):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == "busy"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout.strip() == "acquired"


def test_real_cli_process_completes_teaching_and_exposes_attempts(tmp_path):
    from teaching_agent.demo import LESSONS
    from teaching_agent.storage import Store

    root = tmp_path / "terminal"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    def run(*arguments, answers=""):
        result = subprocess.run(
            [sys.executable, "-m", "teaching_agent", "--data-dir", str(root), *arguments],
            input=answers,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert "离线演示" in run("demo")
    version = Store(root).rows("SELECT id FROM versions")[0]["id"]
    run("review", version, answers="approve\npublish\n")
    answers = "".join((lesson[1] + "\n/end\n\n") * 2 for lesson in LESSONS)
    assert "[已完成 /" in run("start", version, answers=answers)
    assert run("progress", version).count("[已掌握]") == 3
    session = Store(root).rows("SELECT id FROM sessions")[0]["id"]
    assert run("attempts", session).count("valid r1") == 6
