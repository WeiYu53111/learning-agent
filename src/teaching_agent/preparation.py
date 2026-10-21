"""Resumable, bounded preparation of unpublished learning content."""

from .knowledge import quality_report
from .models import AppError, Audit, Draft, DraftPatch
from .revision import change_summary, merge_patch
from .storage import dumps, uid


def prepare(app, identifier, retry=False, progress=None):
    emit = progress or (lambda message: None)
    row, book, chunks = app.version(identifier)
    if row["status"] != "draft":
        raise AppError("已发布版本不能重新准备，请先 clone。")
    state = app.preparation(identifier)
    if retry or state is None or state["revision"] != row["revision"]:
        if state:
            with app.store.connect() as conn:
                app.store.event(conn, identifier, "preparation_archived", state)
        state = {
            "id": uid("prep"),
            "revision": row["revision"],
            "rounds": 0,
            "phase": "extract" if row["data"] == "{}" else "check",
            "candidate": None,
            "errors": [],
            "issues": [],
            "changes": [],
            "patch": None,
            "metrics": None,
        }
        app.save_preparation(identifier, state)
    emit(
        f"准备教材：{book.filename}；PDF 页 {chunks[0].page}–{chunks[-1].page}；"
        f"{len(chunks)} 个文本块；阶段 {state['phase']}，已用 {state['rounds']}/2 轮修订"
    )
    try:
        while state["phase"] not in {"ready", "blocked"}:
            row = app.store.get("versions", identifier)
            phase = state["phase"]
            if phase == "extract":
                if row["data"] == "{}":
                    app.build_draft(identifier, progress=emit)
                else:
                    emit("提取：复用已保存的完整草稿")
                state["revision"] = app.store.get("versions", identifier)["revision"]
                state["phase"] = "check"
            elif phase == "check":
                emit("检查完整草稿的结构、引用、覆盖和依赖")
                state["errors"] = quality_report(Draft.model_validate_json(row["data"]), chunks)[
                    "errors"
                ]
                state["phase"] = "revise" if state["errors"] else "audit"
            elif phase == "audit":
                app.audit(identifier, progress=emit, preparation=state)
            elif phase == "evaluate":
                audit = Audit.model_validate_json(row["audit"])
                state["issues"] = [issue.model_dump() for issue in audit.issues]
                has_errors = any(issue.severity == "error" for issue in audit.issues)
                if not audit.issues:
                    state["phase"] = "ready"
                elif state["rounds"] >= 2:
                    state["phase"] = "blocked" if has_errors else "ready"
                else:
                    state["phase"] = "revise"
            elif phase == "revise":
                if state["rounds"] >= 2:
                    state["phase"] = "blocked"
                else:
                    gateway = app.gateway(book, identifier)
                    emit(
                        f"第 {state['rounds'] + 1}/2 轮修订："
                        f"处理 {len(state['issues'])} 项审核意见、"
                        f"{len(state['errors'])} 项程序错误；"
                        f"检查请求大小并等待局部修订；后端/模型：{gateway.model}"
                    )
                    candidate = Draft.model_validate(
                        state["candidate"] or Draft.model_validate_json(row["data"]).model_dump()
                    )
                    instruction = (
                        "根据全部原文修复以下所有审核意见和程序错误，只返回局部修改包。"
                        "保留未受影响的 key 和覆盖；禁止删除必要知识、随意排除内容来消除问题。"
                        "知识错误、重要遗漏、题干与评分不一致必须实质修正。\n"
                        + "\n".join(
                            f"[{issue['severity']}] {issue['description']}；"
                            f"证据：{issue['source_refs']}"
                            for issue in state["issues"]
                        )
                        + "\n"
                        + "\n".join(state["errors"])
                    )
                    if state.get("patch") and state["errors"]:
                        instruction += "\n上次失败修改包（请根据当前输入草稿重新生成）：" + dumps(
                            state["patch"]
                        )
                    patch = gateway.revise_patch(candidate, chunks, instruction)
                    state["patch"] = patch.model_dump()
                    state["metrics"] = getattr(gateway, "last_revision_metrics", None)
                    state["rounds"] += 1
                    state["phase"] = "merge"
                    # Persist the returned patch before merging, validation or replacing the draft.
                    app.save_preparation(identifier, state)
                    app.report_patch_metrics(state["metrics"], emit)
            elif phase == "merge":
                emit(f"第 {state['rounds']}/2 轮修订：合并修改包，保留未改动内容")
                base = Draft.model_validate(
                    state["candidate"] or Draft.model_validate_json(row["data"]).model_dump()
                )
                try:
                    candidate = merge_patch(base, DraftPatch.model_validate(state["patch"]))
                except AppError as exc:
                    state["errors"] = [str(exc)]
                    state["candidate"] = None
                    state["phase"] = "revise" if state["rounds"] < 2 else "blocked"
                    with app.store.connect() as conn:
                        app.store.event(conn, identifier, "preparation_patch_rejected", state)
                        app.save_preparation(identifier, state, conn)
                    emit("修改包未能有效合并；正式草稿未覆盖，错误与修改包已保存")
                else:
                    state["candidate"] = candidate.model_dump()
                    state["phase"] = "validate"
            elif phase == "validate":
                emit(f"第 {state['rounds']}/2 轮修订：校验候选的引用、覆盖和依赖")
                candidate = Draft.model_validate(state["candidate"])
                state["errors"] = quality_report(candidate, chunks)["errors"]
                if state["errors"]:
                    with app.store.connect() as conn:
                        app.store.event(
                            conn,
                            identifier,
                            "preparation_candidate_rejected",
                            {
                                "task": state["id"],
                                "round": state["rounds"],
                                "candidate": state["candidate"],
                                "errors": state["errors"],
                            },
                        )
                    emit("候选校验未通过；保留正式草稿，失败候选已保存")
                    state["phase"] = "revise" if state["rounds"] < 2 else "blocked"
                else:
                    before = Draft.model_validate_json(row["data"])
                    summary = change_summary(before, candidate)
                    state["changes"].append(
                        {
                            "round": state["rounds"],
                            **summary["objectives"],
                            "notes_changed": summary["notes_changed"],
                            "objects": summary,
                            "metrics": state.get("metrics"),
                        }
                    )
                    state["phase"] = "audit"
                    state["candidate"] = None
                    state["patch"] = None
                    app.save_draft(identifier, candidate, "自动准备修订", preparation=state)
                    emit(f"第 {state['rounds']}/2 轮修订：新草稿已保存，开始再审核")
            else:
                raise AppError("准备阶段无效，请使用 prepare VERSION --retry 开始新任务。")
            app.save_preparation(identifier, state)
    except BaseException:
        emit(f"准备暂停，已保存的阶段可恢复：teach prepare {identifier}")
        raise
    emit(
        "准备完成，等待确认"
        if state["phase"] == "ready"
        else f"准备未完成，修订额度已用尽：teach prepare {identifier} --retry"
    )
    return state
