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
from .learner import outcome, score
from .models import AppError, Assessment, Audit, Draft, Question
from .progress import DraftProgress
from .storage import Store, writer_lock

COMMAND_GROUPS = (
    (
        "教材与学习",
        (
            ("import", '"教材.pdf"', "导入文本 PDF"),
            ("books", "", "查看教材与 BOOK 编号"),
            ("web", "[--port 8765]", "打开本地内容与关联知识查看服务"),
            ("index", "BOOK", "梳理全书知识结构，可恢复"),
            ("outline", "BOOK", "查看章节与 SECTION 编号"),
            ("learn", "BOOK --section SECTION", "准备章节并开始学习"),
            ("ask", 'BOOK --text "问题"', "依据教材原文自由提问"),
            ("mastery", "BOOK --section SECTION --mark", "标记已掌握的章节"),
            ("demo", "", "体验无需密钥的离线演示"),
        ),
    ),
    (
        "继续学习与查看进度",
        (
            ("sessions", "", "查看学习会话与 SESSION 编号"),
            ("resume", "[SESSION]", "恢复学习会话"),
            ("progress", "VERSION", "查看学习目标与掌握状态"),
            ("start", "VERSION", "从内容版本开始学习"),
            ("attempts", "SESSION", "查看作答与 ATTEMPT 编号"),
            ("reassess", 'ATTEMPT --reason "理由"', "复核评分"),
            ("answer", 'SESSION --text "回答"', "通过命令提交整题回答"),
            ("next", "SESSION", "继续下一步教学"),
        ),
    ),
    (
        "内容准备与审核",
        (
            ("drafts", "", "查看内容版本与 VERSION 编号"),
            ("prepare", "VERSION", "恢复内容准备、审核与修订"),
            ("inspect", "VERSION", "查看内容和审核意见"),
            ("review", "VERSION", "交互审核与确认发布"),
            ("draft", "BOOK --section SECTION", "使用原有流程生成草稿"),
            ("audit", "VERSION", "运行模型审核"),
            ("revise", 'VERSION --instruction "要求"', "按要求修订内容"),
            ("approve", "VERSION", "确认当前内容"),
            ("publish", "VERSION", "发布已确认的内容"),
        ),
    ),
    (
        "内容维护",
        (
            ("source", "VERSION [CHUNK]", "查看教材原文"),
            ("clone", "VERSION", "复制内容为新草稿"),
            ("export", "VERSION FILE", "导出内容 JSON"),
            ("apply", "VERSION FILE", "应用编辑后的 JSON"),
            ("resolve", 'VERSION INDEX --note "备注"', "记录审核问题处理备注"),
            ("delete", "VERSION", "永久删除未发布草稿"),
            ("delete-book", "BOOK [--cascade]", "删除教材；--cascade 清除全部关联数据"),
        ),
    ),
)


class TeachParser(argparse.ArgumentParser):
    def format_help(self):
        lines = [
            "teach · PDF 学习助手",
            "导入教材，梳理全书，按章学习。",
            "",
            "用法：teach [全局选项] <命令> [命令参数]",
            "",
            "快速开始",
            '  teach import "教材.pdf"',
            "  teach books",
            "  teach index BOOK",
            "  teach outline BOOK",
            "  teach learn BOOK --section SECTION",
        ]
        for title, commands in COMMAND_GROUPS:
            lines.extend(["", title])
            for name, _, description in commands:
                lines.append(f"  {name:<14}  {description}")
        lines.extend(
            [
                "",
                "全局选项（放在命令之前）",
                "  --data-dir PATH       数据目录，默认 data-v2，可由 TEACH_DATA_DIR 设置",
                "  --config PATH         后端配置，默认查找 teach.toml",
                "  --backend NAME        选择模型后端并更新当前任务的后端绑定",
                "  --env-file PATH       显式读取环境变量文件",
                "  -h, --help            显示帮助",
                "",
                "BOOK / SECTION / VERSION / SESSION / ATTEMPT 为占位符，请替换为实际编号。",
                "查看命令详情：teach <命令> --help，例如 teach learn --help。",
            ]
        )
        return "\n".join(lines) + "\n"


def parser():
    root = TeachParser(prog="teach", usage="teach [全局选项] <命令> [命令参数]")
    root.add_argument("--data-dir", type=Path, help="数据目录，默认 data-v2")
    root.add_argument("--env-file", type=Path, help="显式读取 TEACH_ 配置")
    root.add_argument("--config", type=Path, help="后端配置文件，默认查找 teach.toml")
    root.add_argument("--backend", help="显式选择后端；覆盖并记录当前会话的后端选择")
    sub = root.add_subparsers(
        dest="command",
        metavar="命令",
        prog="teach",
        parser_class=argparse.ArgumentParser,
    )
    sub.add_parser("demo", help="创建明确标注的离线演示教材与草稿")
    command = sub.add_parser("web", help="启动本地只读 Web，Ctrl+C 退出")
    command.add_argument("--port", type=int, default=8765)
    command = sub.add_parser("import", help="只解析并保存 PDF，不调用模型")
    command.add_argument("path", type=Path)
    command = sub.add_parser("index", help="梳理全书知识结构，可恢复")
    command.add_argument("book")
    command.add_argument("--new", action="store_true", help="创建新的知识结构版本")
    command.add_argument("--section", help="只重建指定章节；省略则处理全书")
    command = sub.add_parser("learn", help="按章两阶段准备并开始学习")
    command.add_argument("book")
    command.add_argument("--section", required=True)
    command.add_argument("--new", action="store_true")
    command = sub.add_parser("ask", help="仅依据当前教材原文回答")
    command.add_argument("book")
    command.add_argument("--text", required=True)
    command = sub.add_parser("mastery", help="标记或撤销用户自报掌握")
    command.add_argument("book")
    target = command.add_mutually_exclusive_group(required=True)
    target.add_argument("--section")
    target.add_argument("--point")
    action = command.add_mutually_exclusive_group(required=True)
    action.add_argument("--mark", action="store_true")
    action.add_argument("--clear", action="store_true")
    sub.add_parser("books")
    command = sub.add_parser("delete-book", help="永久删除没有关联版本的教材及 PDF 副本")
    command.add_argument("book", metavar="BOOK")
    command.add_argument(
        "--cascade",
        action="store_true",
        help="永久清除该教材全部数据，包括已发布版本、会话、作答及评分",
    )
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
    for _, commands in COMMAND_GROUPS:
        for name, arguments, description in commands:
            command = sub.choices[name]
            command.description = description
            command.epilog = f"示例：teach {name} {arguments}".rstrip()
            command._positionals.title = "位置参数"
            command._optionals.title = "选项"
            descriptions = {
                "help": "显示帮助并退出",
                "book": "教材 ID，使用 teach books 查询",
                "version": "内容版本 ID，使用 teach drafts 查询",
                "session": "会话 ID，使用 teach sessions 查询",
                "attempt": "作答 ID，使用 teach attempts SESSION 查询",
                "section": "章节 ID，使用 teach outline BOOK 查询",
                "path": "待导入的 PDF 路径",
                "file": "JSON 文件路径",
                "text": "要提交的文本",
                "instruction": "具体修改要求",
                "reason": "评分复核理由",
                "new": "创建新版本，不复用已有结果",
                "no_chat": "只显示当前状态，不进入交互对话",
            }
            for argument in command._actions:
                if not argument.option_strings and argument.metavar is None:
                    argument.metavar = argument.dest.upper()
                if argument.help is None or argument.dest == "help":
                    argument.help = descriptions.get(argument.dest)
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


MASTERY_LABELS = {
    "provisionally_mastered": "已掌握",
    "review_due": "待复习",
    "learning": "学习中",
    "needs_remediation": "待补练",
    "unknown": "未验证",
}


def print_progress(app, identifier):
    row, _, _ = app.version(identifier)
    draft = Draft.model_validate_json(row["data"])
    progress = app.progress(identifier)
    self_reported = set()
    if app.library.available():
        links = app.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (identifier,))
        if links:
            index = app.store.rows(
                "SELECT * FROM book_indexes WHERE id=?", (links[0]["index_id"],)
            )[0]
            marked = app.library.marks(row["book_id"], index)
            self_reported = {
                p["draft_point"]
                for p in json.loads(links[0]["data"]).get("point_map", [])
                if set(p["knowledge_keys"]) <= marked
            }
    labels = MASTERY_LABELS
    details = app.progress_details(identifier)
    included = [obj for obj in draft.objectives().values() if not obj.excluded_reason]
    counts = {status: 0 for status in labels}
    for obj in included:
        counts[progress[obj.key]["status"]] += 1
    print(f"共 {len(included)} 个学习目标")
    print(" · ".join(f"{label} {counts[status]}" for status, label in labels.items()))
    print("已练习不等于已掌握；未验证表示尚无有效评分证据。")
    print("以下按教材知识点排列，实际下一题由当前会话决定。")
    misconceptions = {m.code: m.description for m in draft.misconceptions}
    for index, point in enumerate(draft.points, 1):
        print(f"\n{index}. {point.title}")
        if point.key in self_reported:
            weak = any(progress[o.key]["status"] == "needs_remediation" for o in point.objectives)
            print(
                "   自报掌握，但练习显示需复习。"
                if weak
                else "   用户自报已掌握（练习状态独立记录）。"
            )
        for obj in point.objectives:
            if obj.excluded_reason:
                print(f"   [已排除] {obj.description}")
                print(f"   编号：{obj.key} · 原因：{obj.excluded_reason}")
                continue
            value = progress[obj.key]
            trend = "未验证" if value["score"] is None else f"{value['score']:.3f}"
            print(f"   [{labels[value['status']]}] {obj.description}")
            detail = (
                f"   编号：{obj.key} · 独立通过 {value['independent_passes']}/2 · 趋势分 {trend}"
            )
            if value["due"]:
                detail += f" · 复习时间 {value['due']}"
            print(detail)
            history = details.get(obj.key)
            if history:
                print(
                    f"   已作答 {history['attempts']} 次"
                    f" · 评分待复核 {len(history['needs_review'])} 次"
                    f" · 题目无效 {history['invalid_question']} 次 · 待评分 {history['pending']} 次"
                )
                if value["status"] == "unknown":
                    print("   已有作答，但尚无可计入掌握的有效评分证据；请查看上述评分状态。")
                for attempt_id in history["needs_review"]:
                    print(f'   复核：teach reassess {attempt_id} --reason "复核评分"')
            for code in value["misconceptions"]:
                print(f"   关键误解：{misconceptions.get(code, '未知误解')}（{code}）")


def print_session(state, view=None):
    lifecycle = {
        "active": "学习中",
        "paused": "已暂停",
        "completed": "已完成",
        "ended_with_gaps": "本轮结束，仍有待学内容",
    }
    phases = {
        "planning": "准备下一题",
        "awaiting_answer": "等待回答",
        "evaluating": "正在评分",
        "feedback_ready": "查看反馈",
        "awaiting_clarification": "等待补充回答",
    }
    print(
        f"\n[{lifecycle.get(state['lifecycle'], state['lifecycle'])} / "
        f"{phases.get(state['phase'], state['phase'])}]"
    )
    if view:
        print(f"知识点：{view['title']}\n学习目标：{view['description']}")
        print(f"编号：{view['key']} · 当前状态：{MASTERY_LABELS[view['mastery']['status']]}")
    task = state.get("task")
    if task:
        actions = {
            "DIAGNOSE": "首次诊断",
            "REMEDIATE": "针对性补练",
            "REASSESS": "再次验证",
            "REVIEW": "到期复习",
            "EXPLAIN": "目标讲解",
        }
        print("本题教学动作：" + actions.get(task["action"], task["action"]))
        if task["explanation"]:
            print("讲解：" + task["explanation"])
        if state["phase"] in {"awaiting_answer", "awaiting_clarification"} and task.get("question"):
            saved = answer_state(state)
            index = len(saved["answers"])
            if saved["context"]:
                print("共同背景：" + saved["context"])
            print(f"问题（第 {index + 1}/{len(saved['parts'])} 问）：{saved['parts'][index]}")
            print("回车换行；/end 提交当前小问，全部完成后统一评分；/cancel 清空当前输入。")
            print("/hint 提示，/explain 讲解，/skip 跳过目标，/pause 保存暂停。")
            if saved["lines"]:
                print("已保存的当前输入：\n" + "\n".join(saved["lines"]))
    if state.get("feedback") and state["phase"] == "feedback_ready":
        attempt = view.get("attempt") if view else None
        feedback = attempt["assessment"] if attempt else state["feedback"]
        statuses = {
            "valid": "有效评分",
            "needs_review": "评分待复核",
            "invalid_question": "题目无效",
        }
        print(f"评估状态：{statuses[feedback['status']]}\n{feedback['feedback']}")
        if feedback["status"] == "valid" and (attempt or (task and task.get("question"))):
            question = Question.model_validate(attempt["question"] if attempt else task["question"])
            assessment = Assessment.model_validate(feedback)
            value, passed = score(assessment, question)
            if assessment.rule_version == 2:
                labels = {
                    "passed": "已满足核心要求",
                    "incorrect": "核心要求存在明确错误",
                    "insufficient": "证据不足，不扣减独立达标次数",
                }
                print("本题：" + labels[outcome(assessment, question)] + "。")
            else:
                print(f"本题 {value * 100:g} 分；{'已达标' if passed else '尚未达标'}。")
            items = {item.rubric_key: item for item in assessment.items}
            for rubric in question.rubric:
                item = items[rubric.key]
                if assessment.rule_version == 2:
                    label = {"met": "已满足", "incorrect": "明确错误", "insufficient": "证据不足"}[
                        item.verdict
                    ]
                    print(f"{rubric.description}：{label}；{item.reason}")
                elif item.credit < 1:
                    print(
                        f"{'关键项待补全' if rubric.critical else '待完善项'}：{rubric.description}"
                    )
                    print(f"  得分比例 {item.credit:g}；原因：{item.reason}")
            if attempt and attempt["assisted"]:
                print("本题使用过提示，属于辅助作答，不计入独立达标次数。")
            if attempt and attempt["practice_only"]:
                print("本题仅作练习，不计入掌握证据。")
        elif feedback["status"] != "valid":
            print("本次结果不计入掌握证据，不能据此判断你未掌握。")
            if attempt and feedback["status"] == "needs_review":
                print(f'复核：teach reassess {attempt["id"]} --reason "复核评分"')
        print(feedback["correction"])
        if view:
            mastery = view["mastery"]
            count = mastery["independent_passes"]
            if mastery.get("rule_version", 1) == 2:
                print(f"当前有效独立达标 {count}/2；核心要求全部满足才通过。")
                print("明确独立失败减一次，最低为零；证据不足不扣减。")
                for gap in mastery.get("gaps", []):
                    print("待补练缺口：" + gap["requirement"])
            else:
                print(f"当前独立达标 {count}/2；单题须至少 80 分且关键项全对。")
            if count < 2:
                suffix = "" if mastery.get("rule_version", 1) == 2 else "；独立失败后重新累计"
                print(f"还需 {2 - count} 次不同场景的独立达标{suffix}。")
            if view["misconceptions"]:
                print("仍需解除已确认的关键误解：" + "；".join(view["misconceptions"]))
            if mastery["status"] == "provisionally_mastered":
                print("本目标已掌握。")
            elif mastery["status"] == "review_due":
                print("本目标已到复习时间，需要完成复习验证。")
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
                print_session(state, app.teaching_view(identifier))
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
                        "/hint /explain /skip /progress /why /continue /answer /pause /ask 问题；"
                        "// 输入字面斜线。"
                    )
                elif answer == "/ask" or answer.startswith("/ask "):
                    question = answer[4:].strip()
                    try:
                        book = app.version(app.session(identifier)[0]["version_id"])[1]
                        with DraftProgress() as progress:
                            record = app.library.ask(
                                book.id, question, progress, session_id=identifier
                            )
                        print_book_answer(record)
                        print("已回到原题，原答案输入已保留。")
                    except (AppError, ValidationError) as exc:
                        print(f"提问失败，原题及答案输入已保留：{safe_error(exc)}")
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


def print_book_answer(record):
    answer = record["answer"]
    if not answer["sufficient"]:
        print("依据不足：" + answer["limitation"])
        return
    pages = {c["id"]: c["page"] for c in record["chunks"]}
    for part in answer["parts"]:
        print(part["text"])
        for ref in part["source_refs"]:
            print(f"  [PDF p.{pages[ref['chunk_id']]} · {ref['chunk_id']}] {ref['quote']}")
    if answer["limitation"]:
        print(answer["limitation"])


def prerequisite_choices(app, version):
    if not app.library.available():
        return []
    links = app.store.rows("SELECT * FROM lesson_links WHERE version_id=?", (version,))
    if not links:
        return []
    link = links[0]
    index = app.store.rows("SELECT * FROM book_indexes WHERE id=?", (link["index_id"],))[0]
    book_id = index["book_id"]
    needed = app.library.prerequisites(book_id, link["section_id"], index)
    skipped = []
    for point in needed:
        print(f"必要前置：{point['title']}（{point['section_id']} · {point['key']}）")
        if not sys.stdin.isatty():
            raise AppError(f"请在交互终端选择补学、自报掌握或本次跳过：teach start {version}")
        choice = input("1 补学 / 2 我已掌握 / 3 本次跳过 [1]：").strip()
        if choice == "2":
            app.library.mastery(book_id, point=point["key"], index=index)
        elif choice == "3":
            skipped.append(point["key"])
        else:
            print(f"补学入口：teach learn {book_id} --section {point['section_id']}")
            print(f"完成后返回：teach start {version}")
            return None
    return skipped


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
        print(f"下一步：teach index {book.id}；或直接 teach ask {book.id} --text '问题'")
    elif command == "index":
        with DraftProgress() as progress:
            result = app.library.index(args.book, progress, new=args.new, section_id=args.section)
        print(f"知识结构：{result['id']}；查看：teach outline {args.book}")
    elif command == "learn":
        with DraftProgress() as progress:
            identifier = app.library.learn(args.book, args.section, progress, new=args.new)
        print(f"教学版本：{identifier}；恢复：teach prepare {identifier}")
        if app.version(identifier)[0]["status"] == "published":
            existing = [
                r
                for r in app.store.rows(
                    "SELECT * FROM sessions WHERE version_id=? ORDER BY updated_at DESC",
                    (identifier,),
                )
                if json.loads(r["data"])["lifecycle"] in {"active", "paused"}
            ]
            argv = ["resume", existing[0]["id"]] if existing else ["start", identifier]
            dispatch(app, parser().parse_args(argv))
        else:
            run_preparation(app, identifier)
    elif command == "ask":
        with DraftProgress() as progress:
            record = app.library.ask(args.book, args.text, progress)
        print_book_answer(record)
    elif command == "mastery":
        index = app.library.active(args.book)
        if not index:
            raise AppError("请先完成 teach index BOOK。")
        points = [
            p
            for p in json.loads(index["data"])["points"]
            if p["section_id"] == args.section or p["key"] == args.point
        ]
        if not points:
            raise AppError("没有匹配的章节或知识点。")
        print(f"范围：{args.section or args.point}；{len(points)} 个知识点")
        if args.section and sys.stdin.isatty():
            if input("确认标记/撤销整章自报掌握？[y/N] ").strip().lower() not in {"y", "yes", "是"}:
                return
        app.library.mastery(args.book, section=args.section, point=args.point, mark=args.mark)
        print(
            "已记录用户自报掌握（不替代评分证据）。"
            if args.mark
            else "已撤销自报掌握，练习记录保留。"
        )
    elif command == "books":
        for row in app.store.rows("SELECT id,data FROM books"):
            data = json.loads(row["data"])
            print(f"{row['id']} | {data['filename']} | {data['page_count']} 页")
    elif command == "delete-book":
        app.delete_book(args.book, cascade=args.cascade)
        print(f"已删除教材：{args.book}")
    elif command == "outline":
        for section in app.book(args.book).sections:
            print(f"{section.id} p.{section.start_page}–{section.end_page} {section.title}")
        if app.library.available():
            index = app.library.active(args.book)
            pending = app.store.rows(
                "SELECT id FROM book_indexes WHERE book_id=? AND status='building'", (args.book,)
            )
            print(
                "梳理状态："
                + ("已有可用版本" if index else "尚未完成")
                + ("；有未完成任务，可运行 index 恢复" if pending else "")
            )
            if index:
                marks = app.library.marks(args.book, index)
                verified = app.library.verified(args.book, index)
                data = json.loads(index["data"])
                for point in data["points"]:
                    status = (
                        "已验证掌握"
                        if point["key"] in verified
                        else "用户自报已掌握"
                        if point["key"] in marks
                        else "未验证"
                    )
                    print(f"  {point['key']} [{point['section_id']}] {point['title']} · {status}")
                for edge in data["relations"]:
                    print(
                        f"  {edge['source']} → {edge['target']} [{edge['kind']}] {edge['reason']}"
                    )
        unavailable = app.book(args.book).unavailable_pages
        if unavailable:
            print(f"不可读取页面：{unavailable}；知识结构不覆盖这些页面。")
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
            skipped = prerequisite_choices(app, args.version)
            if skipped is None:
                return
            identifier = app.start(args.version, args.point, args.due)
            if skipped:
                session_row, session_state = app.session(identifier)
                session_state["prerequisite_skips"] = skipped
                app.store.save_session(identifier, session_row["revision"], session_state)
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
            print_session(state, app.teaching_view(identifier))
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
        print_session(state, app.teaching_view(args.session))
    elif command == "next":
        with DraftProgress() as progress:
            state = app.advance(args.session, progress=progress)
        print_session(state, app.teaching_view(args.session))
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
    root = parser()
    args = root.parse_args(argv)
    if args.command is None:
        root.print_help()
        return 0
    if args.env_file:
        if not args.env_file.is_file():
            print("配置文件不存在。", file=sys.stderr)
            return 2
        load_dotenv(args.env_file, override=False)
    store = Store(args.data_dir or Path(os.getenv("TEACH_DATA_DIR", "data-v2")))
    if args.command == "web":
        try:
            from .web import serve

            serve(store, args.port)
            return 0
        except ImportError:
            print('请先安装 Web 依赖：python -m pip install -e ".[web]"', file=sys.stderr)
            return 1
        except (AppError, OSError) as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
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
