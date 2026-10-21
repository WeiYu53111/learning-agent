"""Terminal views and command parsing; no teaching rules live in this module."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .answering import answer_state
from .application import Application
from .backends import BackendRegistry
from .demo import make_pdf
from .knowledge import quality_report
from .models import AppError, Audit, Draft
from .progress import DraftProgress
from .storage import Store, writer_lock


def parser():
    root = argparse.ArgumentParser(prog="teach", description="先审核教材，再开始教学")
    root.add_argument("--data-dir", type=Path)
    root.add_argument("--env-file", type=Path, help="显式读取 TEACH_ 配置")
    root.add_argument("--config", type=Path, help="后端配置文件，默认查找 teach.toml")
    root.add_argument("--backend", help="显式选择后端；覆盖并记录当前会话的后端选择")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="创建明确标注的离线演示教材与草稿")
    command = sub.add_parser("import", help="只解析并保存 PDF，不调用模型")
    command.add_argument("path", type=Path)
    sub.add_parser("books")
    command = sub.add_parser("delete-book", help="永久删除没有关联版本的教材及 PDF 副本")
    command.add_argument("book", metavar="BOOK")
    command = sub.add_parser("outline")
    command.add_argument("book")
    command = sub.add_parser("draft", help="选择章节并自动提取、审核和修订学习内容")
    command.add_argument("book", nargs="?")
    command.add_argument("--section", default="s1")
    command.add_argument("--pages", help="只处理章节中的物理页范围，例如 1:5")
    command.add_argument("--resume", metavar="VERSION")
    command.add_argument("--extract-only", action="store_true", help="仅提取，不自动审核修订")
    command = sub.add_parser("prepare", help="准备已有草稿或恢复审核修订流程")
    command.add_argument("version")
    command.add_argument("--retry", action="store_true", help="开始新任务，重新给予两轮修订额度")
    sub.add_parser("drafts")
    command = sub.add_parser("delete", help="永久删除未发布草稿及其关联数据")
    command.add_argument("version", metavar="VERSION")
    for name in ["inspect", "review", "audit", "approve", "publish", "clone"]:
        command = sub.add_parser(name)
        command.add_argument("version")
    command = sub.add_parser("source")
    command.add_argument("version")
    command.add_argument("chunk", nargs="?")
    for name in ["export", "apply"]:
        command = sub.add_parser(name)
        command.add_argument("version")
        command.add_argument("file", type=Path)
    command = sub.add_parser("revise")
    command.add_argument("version")
    command.add_argument("--instruction", required=True)
    command = sub.add_parser("resolve")
    command.add_argument("version")
    command.add_argument("index", type=int, help="inspect 显示的问题编号（从1开始）")
    command.add_argument("--note", required=True)
    command = sub.add_parser("start")
    command.add_argument("version")
    command.add_argument("--point")
    command.add_argument("--due", action="store_true", help="只复习到期目标")
    command.add_argument("--no-chat", action="store_true")
    command = sub.add_parser("resume")
    command.add_argument("session", nargs="?")
    command.add_argument("--no-chat", action="store_true")
    sub.add_parser("sessions")
    command = sub.add_parser("attempts", help="查看会话作答及最新评分修订")
    command.add_argument("session")
    command = sub.add_parser("progress")
    command.add_argument("version")
    command = sub.add_parser("answer")
    command.add_argument("session")
    command.add_argument("--text", required=True)
    command = sub.add_parser("next")
    command.add_argument("session")
    command = sub.add_parser("reassess")
    command.add_argument("attempt")
    command.add_argument("--reason", required=True)
    return root


def list_drafts(app):
    rows = app.store.rows("SELECT * FROM versions ORDER BY created_at DESC,id DESC")
    if not rows:
        print("暂无草稿。请先导入教材，再选择章节生成草稿。")
        return
    print(f"共 {len(rows)} 个内容版本（含已发布版本，按创建时间从新到旧）：")
    for row in rows:
        book = app.book(row["book_id"])
        section_id, _, page_range = row["section_id"].partition("@")
        section = next(s for s in book.sections if s.id == section_id)
        pages = page_range.replace(":", "–") or f"{section.start_page}–{section.end_page}"
        status = "草稿"
        detail = "尚未执行模型审核"
        command = f"prepare {row['id']}"
        if row["status"] == "published":
            status, detail = "已发布", "可以开始学习"
            command = f"start {row['id']}"
        elif row["data"] == "{}":
            detail = "提取尚未完成，可继续"
            command = f"draft --resume {row['id']}"
        elif row["approved_revision"] == row["revision"]:
            detail = "人工已确认，等待发布"
            command = f"publish {row['id']}"
        elif row["reviewed_revision"] == row["revision"] and row["audit"]:
            audit = Audit.model_validate_json(row["audit"])
            unresolved = sum(issue.severity == "error" for issue in audit.issues)
            warnings = sum(issue.severity == "warning" for issue in audit.issues)
            detail = f"模型审核：{unresolved} 项错误待处理，{warnings} 项建议"
        preparation = app.preparation(row["id"])
        if preparation and preparation["revision"] == row["revision"] and row["status"] == "draft":
            phase_name = {
                "extract": "提取",
                "check": "程序校验",
                "audit": "审核",
                "evaluate": "整理审核结果",
                "revise": "修订",
                "validate": "候选校验",
                "merge": "合并局部修改",
                "ready": "待确认",
                "blocked": "准备未完成",
            }.get(preparation["phase"], preparation["phase"])
            detail = f"准备阶段：{phase_name}，已用 {preparation['rounds']}/2 轮修订"
            command = f"prepare {row['id']}"
            if preparation["phase"] == "ready":
                detail = "准备完成，待确认学习范围"
                command = f"start {row['id']}"
            elif preparation["phase"] == "blocked":
                command += " --retry"
        print(f"\n教材：{book.filename}" + ("（离线演示教材）" if book.demo else ""))
        print(f"章节：{section.title} · PDF 第 {pages} 页")
        print(f"状态：{status} · 第 {row['revision']} 次修订 · {detail}")
        print(f"版本 ID：{row['id']}")
        print(f"下一步：teach {command}")


def inspect(app, identifier):
    row, book, chunks = app.version(identifier)
    print(f"\n版本 {identifier} | {row['status']} | 修订 {row['revision']} | {book.filename}")
    if book.demo:
        print("[离线演示] 固定教材/场景，关键词评分；不代表真实模型效果。")
    if row["data"] == "{}":
        print(f"提取未完成。运行 teach draft --resume {identifier}")
        return
    draft = Draft.model_validate_json(row["data"])
    report = quality_report(draft, chunks)
    print("建议教学顺序：" + " → ".join(report["order"]))
    pages = {c.id: c.page for c in chunks}
    for point in draft.points:
        print(f"\n[{point.key}] {point.title}")
        for obj in point.objectives:
            print(f"  {obj.key} [{obj.dimension}] {obj.description}")
            print(f"    前置：{', '.join(obj.prerequisites) or '无'}")
            if obj.excluded_reason:
                print(f"    已排除：{obj.excluded_reason}")
            for ref in obj.source_refs:
                print(f"    原文 p.{pages.get(ref.chunk_id, '?')} ({ref.chunk_id})：{ref.quote}")
            print(f"    代表题：{obj.sample_question.stem}")
            print(f"    参考答案：{obj.sample_question.reference_answer}")
            for rubric in obj.sample_question.rubric:
                print(f"      {'关键' if rubric.critical else '补充'}要点：{rubric.description}")
    print("\n原文内容覆盖清单：")
    for item in draft.content_items:
        print(f"  {item.key} [{item.kind}] {item.description}")
        print(
            f"    → {', '.join(item.objective_keys) or '无目标'}"
            + (f"；排除：{item.excluded_reason}" if item.excluded_reason else "")
        )
    print(f"片段有清单记录：{report['covered_chunks']}/{report['chunk_count']}（不是语义完整率）")
    for error in report["errors"]:
        print(f"[结构错误] {error}")
    for warning in report["warnings"]:
        print(f"[注意] {warning}")
    if row["audit"]:
        decisions = json.loads(row["decisions"])
        audit = Audit.model_validate_json(row["audit"])
        print(
            f"\n模型审核：{len(audit.issues)} 项；人工确认："
            f"{'有效' if row['approved_revision'] == row['revision'] else '尚未确认'}"
        )
        for index, issue in enumerate(audit.issues, 1):
            print(f"  {index}. [{issue.severity}] {issue.description}")
            if str(index - 1) in decisions:
                print(f"     人工备注（不放行 error）：{decisions[str(index - 1)]}")
    else:
        print("尚未完成当前修订的模型审核。修改后须重新审核、确认。")


def print_progress(app, identifier):
    for key, value in app.progress(identifier).items():
        trend = "未验证" if value["score"] is None else f"{value['score']:.3f}"
        print(
            f"{key}: {value['status']} | 独立通过 {value['independent_passes']}"
            f" | 趋势 {trend} | 复习 {value['due'] or '-'}"
        )
        if value["misconceptions"]:
            print("  关键误解：" + ", ".join(value["misconceptions"]))


def print_session(state):
    print(f"\n[{state['lifecycle']}/{state['phase']}] {state['why']}")
    task = state.get("task")
    if task:
        if task["explanation"]:
            print("讲解：" + task["explanation"])
        if state["phase"] == "awaiting_answer" and task.get("question"):
            saved = answer_state(state)
            index = len(saved["answers"])
            if saved["context"]:
                print("共同背景：" + saved["context"])
            print(f"问题（第 {index + 1}/{len(saved['parts'])} 问）：{saved['parts'][index]}")
            print("回车换行；/end 提交当前小问，全部完成后统一评分；/cancel 清空当前输入。")
            print("/hint 提示，/explain 讲解，/skip 跳过目标，/pause 保存暂停。")
            if saved["lines"]:
                print("已保存的当前输入：\n" + "\n".join(saved["lines"]))
    if state.get("feedback"):
        feedback = state["feedback"]
        print(f"评估状态：{feedback['status']}\n{feedback['feedback']}")
        print(feedback["correction"])
    if state["phase"] == "feedback_ready" and state["lifecycle"] == "active":
        print("按回车或 /continue 继续。")
    if state["lifecycle"] in {"completed", "ended_with_gaps"}:
        print(
            f"跳过：{state['skipped']}；达到练习预算：{state['deferred']}；"
            f"前置阻塞：{state.get('blocked', [])}"
        )


def review_loop(app, identifier):
    inspect(app, identifier)
    print("\n审核命令：show | source [片段] | edit 目标 描述 | pre 目标 前置1,前置2")
    print("exclude 目标 理由 | audit | resolve 编号 理由 | approve | publish | quit")
    print("拆分、合并或复杂编辑：退出后使用 export/apply 或 revise --instruction。")
    while True:
        try:
            line = input("审核> ").strip()
            if not line:
                continue
            parts = shlex.split(line)
            command = parts[0]
            if command in {"quit", "exit"}:
                return
            row, _, chunks = app.version(identifier)
            if command == "show":
                inspect(app, identifier)
            elif command == "source":
                for chunk in chunks:
                    if len(parts) == 1 or chunk.id == parts[1]:
                        print(f"[{chunk.id}] p.{chunk.page}\n{chunk.text}")
            elif command in {"edit", "pre", "exclude"}:
                if len(parts) < 3:
                    raise AppError("请输入目标 key 和内容；pre 用 - 表示无前置。")
                draft = Draft.model_validate_json(row["data"])
                obj = draft.objectives().get(parts[1])
                if not obj:
                    raise AppError("目标不存在。")
                text = " ".join(parts[2:])
                if command == "edit":
                    obj.description = text
                elif command == "pre":
                    obj.prerequisites = [] if text == "-" else text.split(",")
                else:
                    obj.excluded_reason = text
                    for item in draft.content_items:
                        if item.objective_keys and all(
                            draft.objectives()[key].excluded_reason for key in item.objective_keys
                        ):
                            item.excluded_reason = text
                app.save_draft(identifier, Draft.model_validate(draft.model_dump()), line)
                print("已保存草稿修订；原审核和确认已失效。")
            elif command == "audit":
                with DraftProgress() as progress:
                    app.audit(identifier, progress=progress)
                inspect(app, identifier)
            elif command == "resolve" and len(parts) >= 3:
                app.resolve_issue(identifier, int(parts[1]) - 1, " ".join(parts[2:]))
            elif command == "approve":
                app.approve(identifier)
                print("人工确认已记录。")
            elif command == "publish":
                app.publish(identifier)
                print(f"已发布。开始学习：teach start {identifier}")
                return
            else:
                print("未知命令，请使用 show/edit/pre/exclude/audit/resolve/approve/publish/quit。")
        except EOFError:
            return
        except (AppError, ValueError) as exc:
            print(f"错误：{safe_error(exc)}")


def chat(app, identifier, state):
    redraw = True
    try:
        while True:
            if redraw:
                print_session(state)
            redraw = False
            if state["lifecycle"] in {"completed", "ended_with_gaps"}:
                return
            try:
                answer = input("学习> ")
                if answer in {"/quit", "/pause"}:
                    app.pause(identifier)
                    print(f"已暂停。恢复：teach resume {identifier}")
                    return
                if answer == "/help":
                    print("回车换行，/end 提交小问，/cancel 清空当前输入；")
                    print(
                        "/hint /explain /skip /progress /why /continue /answer /pause；"
                        "// 输入字面斜线。"
                    )
                elif answer == "/why":
                    print(state["why"])
                elif answer == "/progress":
                    print_progress(app, app.session(identifier)[0]["version_id"])
                elif answer == "/hint":
                    print(app.hint(identifier))
                    _, state = app.session(identifier)
                elif answer == "/explain":
                    state = app.explain(identifier)
                    redraw = True
                elif answer == "/skip":
                    state = app.skip(identifier)
                    redraw = True
                elif answer == "/continue" or (
                    state["phase"] == "feedback_ready" and answer in {"", "继续"}
                ):
                    with DraftProgress() as progress:
                        state = app.advance(identifier, progress=progress)
                    redraw = True
                elif answer == "/answer":
                    print("已默认使用多行输入：回车换行，/end 提交当前小问，/cancel 清空当前输入。")
                elif answer == "/end":
                    with DraftProgress(lazy=True) as progress:
                        state = app.submit_part(identifier, progress=progress)
                    if state["phase"] == "awaiting_answer":
                        print("当前小问已保存；全部完成后统一评分。")
                    redraw = True
                elif answer == "/cancel":
                    state = app.cancel_answer(identifier)
                    print("已清空当前输入，已提交的小问保留。")
                elif answer.startswith("/") and not answer.startswith("//"):
                    print("未知命令，使用 /help。")
                else:
                    state = app.append_answer_line(
                        identifier, answer[1:] if answer.startswith("//") else answer
                    )
            except AppError as exc:
                print(f"错误：{exc}")
                _, state = app.session(identifier)
                if state["phase"] in {"evaluating", "planning"}:
                    app.pause(identifier)
                    print(f"已保存检查点，请修正问题后运行 teach resume {identifier}")
                    return
    except (EOFError, KeyboardInterrupt):
        app.pause(identifier)
        print(f"\n已暂停。恢复：teach resume {identifier}")


def preparation_summary(app, identifier):
    row, book, chunks = app.version(identifier)
    state = app.preparation(identifier)
    if not state or state["revision"] != row["revision"]:
        raise AppError(f"请先准备当前草稿：teach prepare {identifier}")
    print(f"\n学习范围：{book.filename} · PDF 页 {chunks[0].page}–{chunks[-1].page}")
    if row["data"] != "{}":
        draft = Draft.model_validate_json(row["data"])
        for point in draft.points:
            print(f"  {point.title}")
            for objective in point.objectives:
                if not objective.excluded_reason:
                    print(f"    · {objective.description}")
    print(f"自动修订：{state['rounds']}/2 轮")
    for change in state["changes"]:
        print(
            f"  第 {change['round']} 轮：新增 {change['added']}、"
            f"移除/合并 {change['removed']}、修改 {change['updated']} 个目标；"
            f"备注{'有更新' if change['notes_changed'] else '未变'}"
        )
        for name, label in (
            ("points", "知识点标题"),
            ("content_items", "内容清单"),
            ("misconceptions", "误解条目"),
        ):
            counts = change.get("objects", {}).get(name)
            if counts and any(counts.values()):
                print(
                    f"    {label}：新增 {counts['added']}、删除 {counts['removed']}、"
                    f"修改 {counts['updated']}"
                )
        moved = change.get("objects", {}).get("moved_objectives", 0)
        if moved:
            print(f"    移动目标：{moved}")
    for error in state["errors"]:
        print(f"[程序错误] {error}")
    for issue in state["issues"]:
        print(f"[{issue['severity']}] {issue['description']}")
    print(f"详情：teach inspect {identifier}")
    if state["phase"] != "ready":
        suffix = " --retry" if state["phase"] == "blocked" else ""
        print(f"准备未完成。继续：teach prepare {identifier}{suffix}")
        return False
    print("内容满足当前检查条件。确认表示接受学习范围和剩余提示，不要求你专业校对。")
    return True


def confirm_prepared(app, identifier):
    if not preparation_summary(app, identifier):
        return False
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(f"待确认。请在交互终端运行：teach start {identifier}")
        return False
    try:
        accepted = input("接受以上学习范围和剩余提示，开始学习吗？[y/N] ").strip().lower()
    except EOFError:
        accepted = ""
    if accepted not in {"y", "yes", "是"}:
        print(f"已保留，稍后开始：teach start {identifier}")
        return False
    app.approve(identifier)
    app.publish(identifier)
    return True


def run_preparation(app, identifier, retry=False):
    with DraftProgress() as progress:
        state = app.prepare(identifier, retry=retry, progress=progress)
    if confirm_prepared(app, identifier):
        dispatch(app, parser().parse_args(["start", identifier]))
    elif state["phase"] == "blocked":
        raise AppError(f"准备未完成：teach prepare {identifier} --retry")


def dispatch(app, args):
    command = args.command
    if command == "demo":
        path = app.store.root / "demo.pdf"
        make_pdf(path)
        book = app.import_pdf(path, demo=True)
        identifier = app.create_draft(book.id, "s1")
        app.build_draft(identifier)
        app.audit(identifier)
        inspect(app, identifier)
        print(f"\n下一步：teach review {identifier}\n离线演示无需模型密钥。")
    elif command == "import":
        book = app.import_pdf(args.path)
        print(f"教材：{book.id}；{book.page_count} 页")
        for section in book.sections:
            print(f"  {section.id} p.{section.start_page}–{section.end_page} {section.title}")
        if book.unavailable_pages:
            print(f"无文本页：{book.unavailable_pages}，不在可教学范围内。")
        print(f"下一步：teach draft {book.id} --section s1")
    elif command == "books":
        for row in app.store.rows("SELECT id,data FROM books"):
            data = json.loads(row["data"])
            print(f"{row['id']} | {data['filename']} | {data['page_count']} 页")
    elif command == "delete-book":
        app.delete_book(args.book)
        print(f"已删除教材：{args.book}")
    elif command == "outline":
        for section in app.book(args.book).sections:
            print(f"{section.id} p.{section.start_page}–{section.end_page} {section.title}")
    elif command == "draft":
        if not args.resume and not args.book:
            raise AppError("请提供教材 ID 或 --resume VERSION。")
        section = args.section + ("@" + args.pages if args.pages else "")
        identifier = args.resume or app.create_draft(args.book, section)
        print(f"草稿：{identifier}；失败后可用 draft --resume 继续。", flush=True)
        if args.extract_only:
            with DraftProgress() as progress:
                app.build_draft(identifier, progress=progress)
            inspect(app, identifier)
            print(f"下一步：teach prepare {identifier}")
        else:
            run_preparation(app, identifier)
    elif command == "prepare":
        run_preparation(app, args.version, retry=args.retry)
    elif command == "drafts":
        list_drafts(app)
    elif command == "delete":
        app.delete_draft(args.version)
        print(f"已删除草稿：{args.version}")
    elif command == "inspect":
        inspect(app, args.version)
    elif command == "source":
        for chunk in app.version(args.version)[2]:
            if not args.chunk or chunk.id == args.chunk:
                print(f"[{chunk.id}] p.{chunk.page}\n{chunk.text}")
    elif command == "review":
        review_loop(app, args.version)
    elif command == "audit":
        with DraftProgress() as progress:
            app.audit(args.version, progress=progress)
        inspect(app, args.version)
    elif command == "approve":
        app.approve(args.version)
        print("已记录人工确认。")
    elif command == "publish":
        app.publish(args.version)
        print(f"已发布。开始：teach start {args.version}")
    elif command == "resolve":
        app.resolve_issue(args.version, args.index - 1, args.note)
        print("处理理由已保存。")
    elif command == "clone":
        print("新草稿：" + app.clone(args.version))
    elif command == "export":
        row = app.version(args.version)[0]
        with args.file.open("x", encoding="utf-8") as target:
            json.dump(json.loads(row["data"]), target, ensure_ascii=False, indent=2)
        print(f"已导出：{args.file}。apply 后须重新审核。")
    elif command == "apply":
        draft = Draft.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
        app.save_draft(args.version, draft, "人工JSON编辑")
        inspect(app, args.version)
    elif command == "revise":
        with DraftProgress() as progress:
            app.revise(args.version, args.instruction, progress=progress)
        inspect(app, args.version)
    elif command in {"start", "resume"}:
        if command == "start":
            if app.store.get("versions", args.version)["status"] == "draft":
                if args.due or args.point:
                    raise AppError("草稿首次确认请使用 start VERSION，不附加 --due 或 --point。")
                preparation = app.preparation(args.version)
                if not preparation or preparation["phase"] != "ready":
                    suffix = " --retry" if preparation and preparation["phase"] == "blocked" else ""
                    raise AppError(f"请先完成准备：teach prepare {args.version}{suffix}")
                if not confirm_prepared(app, args.version):
                    return
            identifier = app.start(args.version, args.point, args.due)
            print(f"会话：{identifier}", flush=True)
            with DraftProgress() as progress:
                state = app.advance(identifier, progress=progress)
        else:
            identifier = args.session
            if not identifier:
                candidates = [
                    r
                    for r in app.store.rows("SELECT * FROM sessions ORDER BY updated_at DESC")
                    if json.loads(r["data"])["lifecycle"] in {"active", "paused"}
                ]
                if len(candidates) != 1:
                    raise AppError("请用 teach sessions 查询并指定要恢复的会话。")
                identifier = candidates[0]["id"]
            with DraftProgress(lazy=True) as progress:
                state = app.resume(identifier, progress=progress)
        binding = app.store.rows(
            "SELECT name,spec FROM backend_bindings WHERE owner_id=?", (identifier,)
        )
        if binding:
            spec = json.loads(binding[0]["spec"])
            print(
                f"后端：{binding[0]['name']} ({spec['type']}) | 模型：{spec['model'] or '后端默认'}"
            )
        if args.no_chat:
            print_session(state)
        else:
            chat(app, identifier, state)
    elif command == "sessions":
        for row in app.store.rows("SELECT * FROM sessions ORDER BY updated_at DESC"):
            state = json.loads(row["data"])
            print(f"{row['id']} | {row['version_id']} | {state['lifecycle']}/{state['phase']}")
    elif command == "progress":
        print_progress(app, args.version)
    elif command == "attempts":
        app.session(args.session)
        for row in app.store.rows(
            "SELECT a.id,a.objective_key,a.answer,s.revision,s.data,s.model FROM attempts a "
            "LEFT JOIN assessments s ON s.attempt_id=a.id AND s.revision="
            "(SELECT MAX(revision) FROM assessments WHERE attempt_id=a.id) "
            "WHERE a.session_id=? ORDER BY a.created_at,a.id",
            (args.session,),
        ):
            status = json.loads(row["data"])["status"] if row["data"] else "pending"
            print(f"{row['id']} | {row['objective_key']} | {status} r{row['revision'] or 0}")
            print(f"  回答：{row['answer']}")
            print(f"  评分后端/模型：{row['model'] or '-'}")
    elif command == "answer":
        with DraftProgress(lazy=True) as progress:
            state = app.submit(args.session, args.text, progress=progress)
        print_session(state)
    elif command == "next":
        with DraftProgress() as progress:
            state = app.advance(args.session, progress=progress)
        print_session(state)
    elif command == "reassess":
        print(app.reassess(args.attempt, args.reason).model_dump_json(indent=2))


def safe_error(exc):
    if isinstance(exc, ValidationError):
        fields = [".".join(map(str, e["loc"])) for e in exc.errors()[:6]]
        return "数据契约错误，请检查字段：" + ", ".join(fields)
    if isinstance(exc, sqlite3.Error):
        return "数据库操作失败；当前事务已回滚，请检查磁盘、数据版本或重复操作。"
    return str(exc)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    if args.env_file:
        if not args.env_file.is_file():
            print("配置文件不存在。", file=sys.stderr)
            return 2
        load_dotenv(args.env_file, override=False)
    store = Store(args.data_dir or Path(os.getenv("TEACH_DATA_DIR", "data-v2")))
    app = Application(store, backends=BackendRegistry(args.config, args.backend))
    readonly = {
        "books",
        "outline",
        "drafts",
        "inspect",
        "source",
        "sessions",
        "progress",
        "export",
        "attempts",
    }
    try:
        if args.command in readonly:
            dispatch(app, args)
        else:
            with writer_lock(store.root):
                store.initialize()
                dispatch(app, args)
        return 0
    except KeyboardInterrupt:
        print("\n操作已中断，已提交的数据可恢复。", file=sys.stderr)
        return 130
    except (AppError, ValidationError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"错误：{safe_error(exc)}", file=sys.stderr)
        return 1
