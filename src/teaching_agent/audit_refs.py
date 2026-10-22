"""Strict, indexed diagnostics and bounded audit reference replacements."""

from .models import AppError


def audit_ref_errors(audit, chunks):
    errors = []
    for i, issue in enumerate(audit.issues):
        for j, ref in enumerate(issue.source_refs):
            chunk = chunks.get(ref.chunk_id)
            if chunk is None:
                reason = "文本块不存在"
            elif ref.quote not in chunk.text:
                reason = "引文不是原文连续子串"
            elif chunk.text.find(ref.quote) != chunk.text.rfind(ref.quote):
                reason = "引文在文本块内不唯一"
            else:
                continue
            errors.append({"issue_index": i, "ref_index": j, "chunk_id": ref.chunk_id,
                           "quote": ref.quote, "reason": reason})
    return errors


def diagnostic_summary(errors):
    lines = [f"问题 {e['issue_index'] + 1} / 引用 {e['ref_index'] + 1}："
             f"{e['reason']}；文本块 {e['chunk_id']}；引文 {e['quote'][:80]!r}"
             for e in errors[:5]]
    if len(errors) > 5:
        lines.append(f"另有 {len(errors) - 5} 条错误；完整详情已保存到事件记录。")
    return "；".join(lines)


def merge_audit_refs(audit, errors, repair):
    expected = {(e["issue_index"], e["ref_index"]) for e in errors}
    actual = [(r.issue_index, r.ref_index) for r in repair.replacements]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise AppError("引用纠正位置必须完整覆盖错误引用，且不得重复或包含额外位置。")
    candidate = audit.model_copy(deep=True)
    for replacement in repair.replacements:
        candidate.issues[replacement.issue_index].source_refs[replacement.ref_index] = replacement.ref
    return candidate
