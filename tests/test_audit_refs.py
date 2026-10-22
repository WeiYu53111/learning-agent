import json

import pytest

from teaching_agent.audit_refs import audit_ref_errors, merge_audit_refs
from teaching_agent.gateway import ModelOperations
from teaching_agent.models import (
    AppError, Audit, AuditRefRepair, AuditRefReplacement, Chunk, Issue, Ref,
)


def report(refs):
    return Audit(issues=[Issue(description="原审核意见", severity="warning", source_refs=refs)])


def replacement(ref, i=0, j=0):
    return AuditRefReplacement(issue_index=i, ref_index=j, ref=ref)


@pytest.mark.parametrize("ref,reason", [
    (Ref(chunk_id="missing", quote="甲"), "文本块不存在"),
    (Ref(chunk_id="c", quote="甲，乙"), "引文不是原文连续子串"),
    (Ref(chunk_id="c", quote="甲 乙"), "引文不是原文连续子串"),
    (Ref(chunk_id="c", quote="甲"), "引文在文本块内不唯一"),
    (Ref(chunk_id="c", quote="甲\n乙"), None),
])
def test_strict_diagnostics(ref, reason):
    errors = audit_ref_errors(report([ref]), {"c": Chunk(id="c", page=1, text="甲\n乙甲")})
    assert ([e["reason"] for e in errors] or [None]) == [reason]


@pytest.mark.parametrize("positions", [[], [(0, 0), (0, 0)], [(0, 1)], [(8, 0)]])
def test_repair_positions_must_match(positions):
    audit = report([Ref(chunk_id="missing", quote="x")])
    errors = audit_ref_errors(audit, {})
    repair = AuditRefRepair(replacements=[replacement(Ref(chunk_id="c", quote="x"), i, j) for i, j in positions])
    with pytest.raises(AppError, match="完整覆盖"):
        merge_audit_refs(audit, errors, repair)


def setup_bad_audit(app, draft_id):
    chunks = app.version(draft_id)[2]
    valid = Ref(chunk_id=chunks[0].id, quote=chunks[0].text)
    audit = report([Ref(chunk_id="missing", quote="bad"), valid])
    app.injected_gateway.audit = lambda *args: audit
    return audit, valid


def test_repair_success_preserves_report(app, draft_id):
    original, valid = setup_bad_audit(app, draft_id)
    calls = []
    def repair(*args):
        calls.append(args)
        return AuditRefRepair(replacements=[replacement(valid)])
    app.injected_gateway.repair_audit_refs = repair
    result = app.audit(draft_id)
    assert len(calls) == 1
    assert result.issues[0].description == original.issues[0].description
    assert result.issues[0].severity == original.issues[0].severity
    assert result.issues[0].source_refs == [valid, valid]
    assert original.issues[0].source_refs[0].chunk_id == "missing"
    assert app.version(draft_id)[0]["audit"] is not None
    saved = app.store.rows("SELECT data FROM events WHERE kind='audit_refs_rejected'")
    assert json.loads(saved[0]["data"])["audit"] == original.model_dump()


@pytest.mark.parametrize("failure", ["invalid", "positions", "timeout", "size"])
def test_failed_repair_is_bounded_and_recoverable(app, draft_id, failure):
    original, valid = setup_bad_audit(app, draft_id)
    before = app.version(draft_id)[0]["data"]
    calls = []
    def repair(*args):
        calls.append(1)
        if failure in {"timeout", "size"}:
            raise AppError("调用超时" if failure == "timeout" else "输入过长")
        return AuditRefRepair(replacements=[] if failure == "positions" else [replacement(original.issues[0].source_refs[0])])
    app.injected_gateway.repair_audit_refs = repair
    with pytest.raises(AppError, match="无效原文引用"):
        app.prepare(draft_id)
    assert calls == [1]
    assert app.preparation(draft_id)["phase"] == "audit"
    assert app.preparation(draft_id)["rounds"] == 0
    assert app.version(draft_id)[0]["data"] == before
    assert app.version(draft_id)[0]["audit"] is None
    assert app.store.rows("SELECT * FROM events WHERE kind='audit_refs_repair_failed'")


def test_valid_audit_never_repairs(app, draft_id):
    def unexpected(*args):
        pytest.fail("valid audit must not trigger repair")
    app.injected_gateway.repair_audit_refs = unexpected
    app.audit(draft_id)


def test_preparation_continues_after_reference_repair(app, draft_id):
    original, valid = setup_bad_audit(app, draft_id)
    from teaching_agent.models import DraftPatch
    replies = iter([original, Audit(issues=[])])
    app.injected_gateway.audit = lambda *args: next(replies)
    app.injected_gateway.repair_audit_refs = lambda *args: AuditRefRepair(
        replacements=[replacement(valid)]
    )
    app.injected_gateway.revise_patch = lambda *args: DraftPatch(notes=["已修订"])
    result = app.prepare(draft_id)
    assert result["phase"] == "ready"
    assert result["rounds"] == 1


def test_failed_repair_preserves_previous_audit(app, draft_id):
    app.audit(draft_id)
    before = app.version(draft_id)[0]
    setup_bad_audit(app, draft_id)
    app.injected_gateway.repair_audit_refs = lambda *args: AuditRefRepair(replacements=[])
    with pytest.raises(AppError):
        app.audit(draft_id)
    after = app.version(draft_id)[0]
    assert after["audit"] == before["audit"]
    assert after["reviewed_revision"] == before["reviewed_revision"]


def test_gateway_repair_sends_only_affected_issues_and_chunks():
    gateway = ModelOperations()
    calls = []
    gateway.call = lambda *args: calls.append(args)
    audit = report([Ref(chunk_id="missing", quote="bad")])
    audit.issues.append(Issue(description="unaffected", severity="warning"))
    chunks = [Chunk(id="c", page=1, text="原文")]
    gateway.repair_audit_refs(audit, audit_ref_errors(audit, {}), chunks)
    operation, payload, contract = calls[0]
    assert operation == "repair_audit_refs" and contract is AuditRefRepair
    assert "draft" not in payload
    assert len(payload["issues"]) == 1
    assert payload["chunks"] == [chunks[0].model_dump()]
