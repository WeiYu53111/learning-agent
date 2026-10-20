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

from .application import Application
from .demo import make_pdf
from .knowledge import quality_report
from .models import AppError, Audit, Draft
from .storage import Store, writer_lock


def parser():
    root = argparse.ArgumentParser(prog="teach", description="先审核教材，再开始教学")
    root.add_argument("--data-dir", type=Path)
    root.add_argument("--env-file", type=Path, help="显式读取 TEACH_ 配置")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="创建明确标注的离线演示教材与草稿")
    command = sub.add_parser("import", help="只解析并保存 PDF，不调用模型")
    command.add_argument("path", type=Path)
    sub.add_parser("books")
    command = sub.add_parser("outline")
    command.add_argument("book")
    command = sub.add_parser("draft", help="选择章节生成待审核知识草稿")
    command.add_argument("book", nargs="?")
    command.add_argument("--section", default="s1")
    command.add_argument("--pages", help="只处理章节中的物理页范围，例如 1:5")
    command.add_argument("--resume", metavar="VERSION")
    sub.add_parser("drafts")
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
                print(f"     人工处理：{decisions[str(index - 1)]}")
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
            print("问题：" + task["question"]["stem"])
            print("直接输入回答；/hint 提示，/explain 讲解，/skip 跳过，/pause 暂停。")
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
                app.audit(identifier)
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
    try:
        while True:
            print_session(state)
            if state["lifecycle"] in {"completed", "ended_with_gaps"}:
                return
            try:
                answer = input("学习> ")
                if answer in {"/quit", "/pause"}:
                    app.pause(identifier)
                    print(f"已暂停。恢复：teach resume {identifier}")
                    return
                if answer == "/help":
                    print("/hint /explain /skip /progress /why /continue /answer /pause")
                elif answer == "/why":
                    print(state["why"])
                elif answer == "/progress":
                    print_progress(app, app.session(identifier)[0]["version_id"])
                elif answer == "/hint":
                    print(app.hint(identifier))
                elif answer == "/explain":
                    state = app.explain(identifier)
                elif answer == "/skip":
                    state = app.skip(identifier)
                elif answer == "/continue" or (
                    state["phase"] == "feedback_ready" and answer in {"", "继续"}
                ):
                    state = app.advance(identifier)
                elif answer == "/answer":
                    print("多行回答：/end 提交，/cancel 取消。")
                    lines = []
                    while True:
                        line = input("... ")
                        if line in {"/end", "/cancel"}:
                            break
                        lines.append(line)
                        if sum(map(len, lines)) > 10000:
                            raise AppError("回答超过上限，请重新输入。")
                    if line == "/end":
                        state = app.submit(identifier, "\n".join(lines))
                elif answer.startswith("/") and not answer.startswith("//"):
                    print("未知命令，使用 /help。")
                else:
                    state = app.submit(
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
    elif command == "outline":
        for section in app.book(args.book).sections:
            print(f"{section.id} p.{section.start_page}–{section.end_page} {section.title}")
    elif command == "draft":
        if not args.resume and not args.book:
            raise AppError("请提供教材 ID 或 --resume VERSION。")
        section = args.section + ("@" + args.pages if args.pages else "")
        identifier = args.resume or app.create_draft(args.book, section)
        print(f"草稿：{identifier}；失败后可用 draft --resume 继续。", flush=True)
        app.build_draft(identifier)
        inspect(app, identifier)
        print(f"下一步：teach audit {identifier}，然后 teach review {identifier}")
    elif command == "drafts":
        for row in app.store.rows("SELECT id,book_id,section_id,status,revision FROM versions"):
            print(
                f"{row['id']} | {row['book_id']}/{row['section_id']} | "
                f"{row['status']} r{row['revision']}"
            )
    elif command == "inspect":
        inspect(app, args.version)
    elif command == "source":
        for chunk in app.version(args.version)[2]:
            if not args.chunk or chunk.id == args.chunk:
                print(f"[{chunk.id}] p.{chunk.page}\n{chunk.text}")
    elif command == "review":
        review_loop(app, args.version)
    elif command == "audit":
        app.audit(args.version)
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
        row, book, chunks = app.version(args.version)
        if row["status"] != "draft":
            raise AppError("请先 clone 已发布版本。")
        draft = app.gateway(book).revise(
            Draft.model_validate_json(row["data"]), chunks, args.instruction
        )
        app.save_draft(args.version, draft, args.instruction)
        inspect(app, args.version)
    elif command in {"start", "resume"}:
        if command == "start":
            identifier = app.start(args.version, args.point, args.due)
            print(f"会话：{identifier}", flush=True)
            state = app.advance(identifier)
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
            state = app.resume(identifier)
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
            "SELECT a.id,a.objective_key,a.answer,s.revision,s.data FROM attempts a "
            "LEFT JOIN assessments s ON s.attempt_id=a.id AND s.revision="
            "(SELECT MAX(revision) FROM assessments WHERE attempt_id=a.id) "
            "WHERE a.session_id=? ORDER BY a.created_at,a.id",
            (args.session,),
        ):
            status = json.loads(row["data"])["status"] if row["data"] else "pending"
            print(f"{row['id']} | {row['objective_key']} | {status} r{row['revision'] or 0}")
            print(f"  回答：{row['answer']}")
    elif command == "answer":
        print_session(app.submit(args.session, args.text))
    elif command == "next":
        print_session(app.advance(args.session))
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
    app = Application(store)
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
