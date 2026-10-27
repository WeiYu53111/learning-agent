import json
from pathlib import Path

import pytest

from teaching_agent import prompt_loader
from teaching_agent.backends import BackendSpec
from teaching_agent.codex_gateway import CodexGateway, strict_schema
from teaching_agent.gateway import ModelOperations
from teaching_agent.models import AppError, Audit

BASELINE = json.loads(
    (Path(__file__).parent / "fixtures" / "prompt_baseline.json").read_text(encoding="utf-8")
)
OPERATIONS = ("revise", "revise_patch", "task", "grade")


@pytest.mark.parametrize("operation", OPERATIONS)
def test_messages_match_pre_migration_baseline(operation, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert (
        ModelOperations.messages(operation, '{"sample":"answer"}', '{"type":"object"}')
        == BASELINE[operation]
    )


def test_codex_complete_prompt_matches_baseline(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("teaching_agent.codex_gateway.shutil.which", lambda _: "codex.exe")
    gateway = CodexGateway(BackendSpec(type="codex_cli"))
    captured = []

    def run(argv, **kwargs):
        if "exec" in argv:
            captured.append(kwargs["input"])
            Path(argv[argv.index("--output-last-message") + 1]).write_text(
                '{"issues":[]}', encoding="utf-8"
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gateway, "run", run)
    gateway.call("audit", {"sample": "answer"}, Audit)
    old_system = (
        prompt_loader.load_prompt("common")
        + "\n"
        + prompt_loader.load_prompt("audit")
        + "\nJSON Schema:\n"
    )
    schema = json.dumps(strict_schema(Audit.model_json_schema()), ensure_ascii=False)
    body = json.dumps({"sample": "answer"}, ensure_ascii=False)
    assert captured == [old_system + schema + BASELINE["codex_wrapper"] + body]


@pytest.mark.parametrize("content", [None, "", " \n\t\n"])
def test_missing_or_empty_resource_is_reported(monkeypatch, tmp_path, content):
    directory = tmp_path / "prompts"
    directory.mkdir()
    if content is not None:
        (directory / "grade.txt").write_text(content, encoding="utf-8")
    monkeypatch.setattr(prompt_loader, "files", lambda _: tmp_path)
    with pytest.raises(AppError, match="grade"):
        prompt_loader.load_prompt("grade")


def test_loader_preserves_internal_whitespace(monkeypatch, tmp_path):
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "grade.txt").write_bytes(b"\n  first\nsecond  \r\n")
    monkeypatch.setattr(prompt_loader, "files", lambda _: tmp_path)
    assert prompt_loader.load_prompt("grade") == "\n  first\nsecond  "


def test_unknown_prompt_cannot_select_arbitrary_resource():
    with pytest.raises(AppError, match="未知提示词"):
        prompt_loader.load_prompt("../models.py")


def test_legacy_extraction_prompt_is_removed():
    with pytest.raises(AppError):
        prompt_loader.load_prompt("extract")
