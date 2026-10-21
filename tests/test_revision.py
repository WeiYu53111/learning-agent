import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from teaching_agent.backends import BackendSpec
from teaching_agent.codex_gateway import CodexGateway
from teaching_agent.gateway import Gateway, Settings
from teaching_agent.knowledge import quality_report
from teaching_agent.models import (
    AppError,
    Audit,
    Draft,
    DraftPatch,
    Issue,
    Misconception,
    ObjectiveChange,
    PointChange,
)
from teaching_agent.revision import change_summary, merge_patch


@pytest.fixture
def draft(app, draft_id):
    return Draft.model_validate_json(app.version(draft_id)[0]["data"])


def objective_patch(draft):
    obj = draft.points[0].objectives[0].model_copy(deep=True)
    obj.description += "，并解释原因"
    obj.sample_question.stem += "请说明判断依据。"
    obj.sample_question.reference_answer += "以上为判断依据。"
    return DraftPatch(objectives=[ObjectiveChange(point_key=draft.points[0].key, objective=obj)])


def test_single_objective_patch_preserves_everything_else(draft):
    before = draft.model_dump_json()
    patch = objective_patch(draft)
    result = merge_patch(draft, patch)
    assert draft.model_dump_json() == before
    expected = draft.model_copy(deep=True)
    expected.points[0].objectives[0] = patch.objectives[0].objective
    assert result == expected
    assert result.points[1:] == draft.points[1:]
    assert len(patch.model_dump_json()) < len(draft.model_dump_json())


@pytest.mark.parametrize("kind", ["duplicate", "delete_missing", "conflict", "unknown_parent"])
def test_invalid_patch_ids_are_rejected(draft, kind):
    patch = objective_patch(draft)
    if kind == "duplicate":
        patch.objectives *= 2
    elif kind == "delete_missing":
        patch.delete_objectives = ["missing"]
    elif kind == "conflict":
        patch.delete_objectives = [patch.objectives[0].objective.key]
    else:
        patch.objectives[0].point_key = "missing"
    with pytest.raises(AppError):
        merge_patch(draft, patch)


def test_move_and_delete_point_require_explicit_objective_operations(draft, app, draft_id):
    first, second = draft.points[:2]
    with pytest.raises(AppError, match="隐式连带删除"):
        merge_patch(draft, DraftPatch(delete_points=[first.key]))
    result = merge_patch(
        draft,
        DraftPatch(
            delete_points=[first.key],
            objectives=[
                ObjectiveChange(point_key=second.key, objective=o) for o in first.objectives
            ],
        ),
    )
    assert first.key not in [p.key for p in result.points]
    assert first.objectives[0] in result.points[0].objectives
    assert quality_report(result, app.version(draft_id)[2])["errors"] == []
    assert change_summary(draft, result)["moved_objectives"] == len(first.objectives)


def test_split_objective_updates_content_and_dependencies(draft, app, draft_id):
    first, second = draft.points[:2]
    old = first.objectives[0]
    new = old.model_copy(deep=True)
    new.key = "new_objective"
    follower = second.objectives[0].model_copy(deep=True)
    follower.prerequisites = [new.key]
    items = [
        item.model_copy(deep=True) for item in draft.content_items if old.key in item.objective_keys
    ]
    for item in items:
        item.objective_keys = [new.key if k == old.key else k for k in item.objective_keys]
    patch = DraftPatch(
        points=[PointChange(key="new_point", title="新的知识点")],
        delete_points=[first.key],
        delete_objectives=[old.key],
        objectives=[
            ObjectiveChange(point_key="new_point", objective=new),
            ObjectiveChange(point_key=second.key, objective=follower),
        ],
        content_items=items,
        misconceptions=[
            Misconception(code="new_error", description="误解", objective_keys=[new.key])
        ],
    )
    result = merge_patch(draft, patch)
    assert old.key not in result.objectives()
    assert quality_report(result, app.version(draft_id)[2])["errors"] == []


def test_notes_and_noop_semantics(draft):
    with pytest.raises(AppError, match="未产生有效修改"):
        merge_patch(draft, DraftPatch())
    with pytest.raises(AppError, match="未产生有效修改"):
        merge_patch(draft, DraftPatch(notes=draft.notes))
    assert merge_patch(draft, DraftPatch(notes=[])).notes == []
    assert merge_patch(draft, objective_patch(draft)).notes == draft.notes


def test_bad_rubric_rejected_by_patch_contract(draft):
    raw = objective_patch(draft).model_dump()
    raw["objectives"][0]["objective"]["sample_question"]["rubric"][0]["weight"] = 0.1
    with pytest.raises(ValidationError, match="权重总和"):
        DraftPatch.model_validate(raw)


@pytest.mark.parametrize("problem", ["reference", "cycle", "coverage"])
def test_merged_patch_still_requires_full_quality_check(app, draft_id, draft, problem):
    patch = objective_patch(draft)
    obj = patch.objectives[0].objective
    if problem == "reference":
        obj.source_refs[0].quote = "invalid quote"
    elif problem == "cycle":
        obj.prerequisites = [obj.key]
    else:
        patch.delete_content_items = [draft.content_items[0].key]
    app.injected_gateway.revise_patch = lambda *args: patch
    with pytest.raises(AppError, match="原草稿未覆盖"):
        app.revise(draft_id, "fix")
    assert Draft.model_validate_json(app.version(draft_id)[0]["data"]) == draft


def test_empty_patch_consumes_round_without_reaudit_or_save(app, draft_id):
    revision = app.version(draft_id)[0]["revision"]
    audits = []
    app.injected_gateway.audit = lambda *args: (
        audits.append(1) or Audit(issues=[Issue(description="fix", severity="error")])
    )
    app.injected_gateway.revise_patch = lambda *args: DraftPatch()
    result = app.prepare(draft_id)
    assert result["phase"] == "blocked" and result["rounds"] == 2
    assert audits == [1]
    assert app.version(draft_id)[0]["revision"] == revision
    assert "未产生有效修改" in result["errors"][0]


def test_resume_saved_patch_without_another_call(app, draft_id, monkeypatch):
    from teaching_agent import preparation

    app.injected_gateway.audit = lambda *args: Audit(
        issues=[Issue(description="fix", severity="error")]
    )
    app.injected_gateway.revise_patch = lambda draft, *args: objective_patch(draft)
    original = preparation.merge_patch
    monkeypatch.setattr(
        preparation, "merge_patch", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        app.prepare(draft_id)
    state = app.preparation(draft_id)
    assert state["phase"] == "merge" and state["rounds"] == 1 and state["patch"]
    monkeypatch.setattr(preparation, "merge_patch", original)
    app.injected_gateway.revise_patch = lambda *a: pytest.fail("already saved")
    app.injected_gateway.audit = lambda *a: Audit(issues=[])
    assert app.prepare(draft_id)["phase"] == "ready"


def test_manual_revision_resumes_saved_patch(app, draft_id, monkeypatch):
    from teaching_agent import application

    app.injected_gateway.revise_patch = lambda draft, *args: objective_patch(draft)
    original = application.merge_patch
    monkeypatch.setattr(
        application, "merge_patch", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        app.revise(draft_id, "fix")
    monkeypatch.setattr(application, "merge_patch", original)
    app.injected_gateway.revise_patch = lambda *a: pytest.fail("already saved")
    app.revise(draft_id, "fix")
    assert app.version(draft_id)[0]["revision"] == 2


def test_old_full_candidate_state_resumes_without_patch(app, draft_id, draft):
    state = {
        "id": "old_task",
        "revision": 1,
        "rounds": 1,
        "phase": "validate",
        "candidate": draft.model_dump(),
        "errors": [],
        "issues": [],
        "changes": [],
    }
    app.save_preparation(draft_id, state)
    app.injected_gateway.revise_patch = lambda *args: pytest.fail("must use old candidate")
    assert app.prepare(draft_id)["phase"] == "ready"


def test_failed_merge_next_round_uses_formal_draft_and_failure_context(app, draft_id, draft):
    app.injected_gateway.audit = lambda *args: Audit(
        issues=[Issue(description="fix", severity="error")]
    )
    calls = []

    def revise(base, chunks, instruction):
        calls.append(1)
        assert base == draft
        if len(calls) == 1:
            return DraftPatch(delete_objectives=["missing"])
        assert "上次失败修改包" in instruction and "不存在" in instruction
        return objective_patch(base)

    app.injected_gateway.revise_patch = revise
    result = app.prepare(draft_id)
    assert result["rounds"] == 2
    assert app.version(draft_id)[0]["revision"] == 2


def test_manual_empty_patch_does_not_increment_revision(app, draft_id):
    row = app.version(draft_id)[0]
    app.injected_gateway.revise_patch = lambda *args: DraftPatch()
    with pytest.raises(AppError, match="未产生有效修改"):
        app.revise(draft_id, "fix")
    assert app.version(draft_id)[0] == row


def test_dangling_dependency_after_explicit_deletion_is_not_repaired(draft, app, draft_id):
    first = draft.points[0]
    patch = DraftPatch(delete_points=[first.key], delete_objectives=[first.objectives[0].key])
    candidate = merge_patch(draft, patch)
    errors = quality_report(candidate, app.version(draft_id)[2])["errors"]
    assert errors
    assert (
        candidate.points[0].objectives[0].prerequisites
        == draft.points[1].objectives[0].prerequisites
    )


def test_patch_size_limit_prevents_launch_and_metrics(draft, app, draft_id, monkeypatch):
    from teaching_agent import codex_gateway

    monkeypatch.setattr(codex_gateway.shutil, "which", lambda _: "codex.exe")
    gateway = CodexGateway(BackendSpec(type="codex_cli", input_chars=1))
    monkeypatch.setattr(gateway, "run", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(AppError, match="revise_patch 输入过长"):
        gateway.revise_patch(draft, app.version(draft_id)[2], "fix")
    assert gateway.last_revision_metrics is None


@pytest.mark.parametrize("backend", ["http", "codex"])
def test_backends_receive_full_input_and_return_only_patch(
    app, draft_id, draft, monkeypatch, backend
):
    patch = objective_patch(draft)
    chunks = app.version(draft_id)[2]
    prompts = []
    if backend == "http":

        def handler(request):
            body = json.loads(request.content)
            prompts.append(body["messages"])
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": patch.model_dump_json(),
                            },
                        }
                    ]
                },
            )

        original = httpx.Client
        monkeypatch.setattr(
            httpx, "Client", lambda: original(transport=httpx.MockTransport(handler))
        )
        gateway = Gateway(Settings(base="https://example.invalid/v1", model="test", key="test"))
    else:
        from teaching_agent import codex_gateway

        monkeypatch.setattr(codex_gateway.shutil, "which", lambda _: "codex.exe")
        gateway = CodexGateway(BackendSpec(type="codex_cli"))

        def run(argv, **kwargs):
            if "exec" in argv:
                prompts.append(kwargs["input"])
                Path(argv[argv.index("--output-last-message") + 1]).write_text(
                    patch.model_dump_json(), encoding="utf-8"
                )
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(gateway, "run", run)
    result = gateway.revise_patch(draft, chunks, "fix")
    assert result == patch
    text = json.dumps(prompts, ensure_ascii=False)
    assert "DraftPatch" in text
    for objective in draft.objectives().values():
        assert objective.description in text
    for chunk in chunks:
        assert chunk.id in text
    metrics = gateway.last_revision_metrics
    assert metrics["input_data_chars"] > metrics["draft_chars"]
    assert metrics["patch_chars"] < metrics["draft_chars"]
    assert metrics["elapsed_seconds"] >= 0
