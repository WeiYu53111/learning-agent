import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from teaching_agent.application import Application
from teaching_agent.backends import BackendRegistry, BackendSpec
from teaching_agent.codex_gateway import CodexGateway, strict_schema
from teaching_agent.models import AppError, Audit
from teaching_agent.storage import SCHEMA, Store


def test_registry_legacy_env_and_no_secret_in_snapshot(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TEACH_API_KEY", "secret-test")
    monkeypatch.setenv("TEACH_MODEL", "model-test")
    monkeypatch.setenv("TEACH_REQUEST_TIMEOUT", "37")
    name, spec = BackendRegistry().choose()
    assert name == "legacy_http"
    assert spec.model == "model-test"
    assert "secret-test" not in spec.model_dump_json()
    assert BackendRegistry.build(spec).settings.key == "secret-test"
    assert BackendRegistry.build(spec).settings.timeout == 37


def test_named_config_and_demo_restriction(tmp_path):
    config = tmp_path / "teach.toml"
    config.write_text(
        'active_backend="c"\n[backends.c]\ntype="codex_cli"\n[backends.d]\ntype="demo"\n',
        encoding="utf-8",
    )
    registry = BackendRegistry(config)
    assert registry.choose()[0] == "c"
    assert registry.choose(demo=True)[1].type == "demo"
    with pytest.raises(AppError):
        BackendRegistry(config, "missing").choose()
    with pytest.raises(AppError):
        BackendRegistry(config, "d").choose()


def test_codex_structured_call_and_cleanup(monkeypatch):
    from teaching_agent import codex_gateway

    monkeypatch.setattr(codex_gateway.shutil, "which", lambda _: "codex.exe")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "exec" in argv:
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text('{"issues":[]}', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    gateway = CodexGateway(BackendSpec(type="codex_cli", model="chosen"))
    monkeypatch.setattr(gateway, "run", run)
    assert gateway.call("audit", {"chunks": []}, Audit).issues == []
    argv, kwargs = calls[1]
    assert argv[-1] == "-"
    assert argv[argv.index("--model") + 1] == "chosen"
    assert "--ignore-user-config" in argv
    assert "--sandbox" in argv and "read-only" in argv
    assert "JSON Schema" in kwargs["input"]
    assert not Path(kwargs["cwd"]).exists()


@pytest.mark.parametrize("failure", ["auth", "exit", "missing", "json", "schema"])
def test_codex_failures_do_not_return_success(monkeypatch, failure):
    from teaching_agent import codex_gateway

    monkeypatch.setattr(codex_gateway.shutil, "which", lambda _: "codex.exe")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "login" in argv:
            return SimpleNamespace(returncode=1 if failure == "auth" else 0)
        if failure in {"json", "schema"}:
            output = Path(argv[argv.index("--output-last-message") + 1])
            output.write_text("broken" if failure == "json" else '{"extra":1}', encoding="utf-8")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)

    gateway = CodexGateway(BackendSpec(type="codex_cli"))
    monkeypatch.setattr(gateway, "run", run)
    with pytest.raises(AppError):
        gateway.call("audit", {}, Audit)
    if failure == "auth":
        assert len(calls) == 1


def test_codex_input_limit_reports_size_before_launch(monkeypatch):
    from teaching_agent import codex_gateway

    monkeypatch.setattr(codex_gateway.shutil, "which", lambda _: "codex.exe")
    payload = {"private_material": "教材内容"}
    expected = len(json.dumps(payload, ensure_ascii=False)) + len(
        json.dumps(strict_schema(Audit.model_json_schema()), ensure_ascii=False)
    )
    gateway = CodexGateway(BackendSpec(type="codex_cli", input_chars=1))
    monkeypatch.setattr(gateway, "run", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(AppError) as error:
        gateway.call("audit", payload, Audit)
    message = str(error.value)
    assert f"{expected} 字符" in message
    assert "input_chars=1" in message
    assert "--backend" in message
    assert "教材内容" not in message


def test_codex_timeout_is_recoverable(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 1)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(AppError, match="超时"):
        CodexGateway(BackendSpec(type="codex_cli")).run(["codex"], cwd=tmp_path, timeout=1)


def test_codex_inherits_proxy_environment(monkeypatch, tmp_path):
    proxy = "http://127.0.0.1:7897"
    monkeypatch.setenv("HTTP_PROXY", proxy)
    monkeypatch.setenv("HTTPS_PROXY", proxy)

    def run(*args, **kwargs):
        assert kwargs["env"]["HTTP_PROXY"] == proxy
        assert kwargs["env"]["HTTPS_PROXY"] == proxy
        assert kwargs["cwd"] == tmp_path
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    result = CodexGateway(BackendSpec(type="codex_cli")).run(["codex"], cwd=tmp_path)
    assert result.returncode == 0


def test_v1_migration_retains_data(tmp_path):
    root = tmp_path / "v1"
    root.mkdir()
    conn = sqlite3.connect(root / "learning.db")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO books VALUES('b','hash','{}')")
    conn.commit()
    conn.close()
    store = Store(root)
    store.initialize()
    store.initialize()
    assert store.get("books", "b")["sha256"] == "hash"
    assert store.rows("PRAGMA user_version")[0]["user_version"] == 3
    assert store.rows("SELECT * FROM preparations") == []
    assert store.rows("SELECT * FROM backend_bindings") == []


def test_session_backend_pinned_and_explicit_switch(app, published, tmp_path):
    config = tmp_path / "backends.toml"
    config.write_text(
        'active_backend="one"\n[backends.one]\ntype="demo"\n[backends.two]\ntype="demo"\n',
        encoding="utf-8",
    )
    first = Application(app.store, backends=BackendRegistry(config, "one"))
    session = first.start(published)
    first.advance(session)
    config.write_text('active_backend="removed"\n', encoding="utf-8")
    restored = Application(app.store, backends=BackendRegistry(config))
    assert restored.resume(session)["phase"] == "awaiting_answer"
    bound = app.store.rows("SELECT * FROM backend_bindings WHERE owner_id=?", (session,))[0]
    assert bound["name"] == "one"
    config.write_text('[backends.two]\ntype="demo"\n', encoding="utf-8")
    changed = Application(app.store, backends=BackendRegistry(config, "two"))
    changed.resume(session)
    state = changed.session(session)[1]
    changed.submit(session, state["task"]["question"]["reference_answer"])
    evidence = app.store.rows("SELECT model FROM assessments")[-1]
    assert evidence["model"].startswith("two/demo:")
    events = app.store.rows("SELECT data FROM events WHERE kind='backend_selected'")
    assert json.loads(events[-1]["data"])["previous"] == "one"


def test_strict_schema_requires_defaults_without_changing_domain_schema():
    original = Audit.model_json_schema()
    converted = strict_schema(original)
    assert converted["required"] == list(converted["properties"])
    assert converted["additionalProperties"] is False
    assert Audit.model_json_schema() == original


def test_named_backend_grading_failure_resumes_same_attempt(app, published, tmp_path, monkeypatch):
    from teaching_agent.demo import DemoGateway

    config = tmp_path / "teach.toml"
    config.write_text('[backends.chosen]\ntype="demo"\n', encoding="utf-8")
    gateway = DemoGateway()
    grade = gateway.grade
    gateway.grade = lambda *args: (_ for _ in ()).throw(AppError("temporary failure"))
    monkeypatch.setattr(BackendRegistry, "build", staticmethod(lambda spec: gateway))
    selected = Application(app.store, backends=BackendRegistry(config, "chosen"))
    session = selected.start(published)
    state = selected.advance(session)
    with pytest.raises(AppError, match="temporary"):
        selected.submit(session, state["task"]["question"]["reference_answer"])
    assert selected.session(session)[1]["phase"] == "evaluating"
    attempt_id = app.store.rows("SELECT id FROM attempts")[0]["id"]
    config.unlink()
    gateway.grade = grade
    resumed = Application(app.store)
    assert resumed.resume(session)["phase"] == "feedback_ready"
    assert app.store.rows("SELECT id FROM attempts") == [{"id": attempt_id}]
    assert app.store.rows("SELECT name FROM backend_bindings WHERE owner_id=?", (session,)) == [
        {"name": "chosen"}
    ]
