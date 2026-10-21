"""Review/publish and teaching use cases. All mutations run under the CLI writer lock."""

from __future__ import annotations

import json
from pathlib import Path

from .backends import BackendRegistry, BackendSpec
from .demo import DemoGateway
from .gateway import Gateway, Settings
from .knowledge import check_refs, ordered_objectives, parse_pdf, quality_report
from .learner import project, score, validate_assessment
from .models import AppError, Assessment, Audit, Book, Draft, DraftPatch, Question
from .revision import change_summary, merge_patch
from .storage import Store, dumps, now, uid


class Application:
    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        gateway=None,
        backends: BackendRegistry | None = None,
    ):
        self.store = store
        self.settings = settings
        self.injected_gateway = gateway
        self.backends = backends or BackendRegistry()
        self._gateways = {}

    def gateway(self, book, owner=None):
        if self.injected_gateway:
            return self.injected_gateway
        if self.settings is not None:
            return DemoGateway() if book.demo else Gateway(self.settings)
        binding = (
            self.store.rows("SELECT * FROM backend_bindings WHERE owner_id=?", (owner,))
            if owner
            else []
        )
        if binding and not self.backends.selected:
            name = binding[0]["name"]
            spec = BackendSpec.model_validate_json(binding[0]["spec"])
        else:
            name, spec = self.backends.choose(book.demo)
        if spec.type == "demo" and not book.demo:
            raise AppError("离线演示后端只能用于演示教材。")
        serialized = spec.model_dump_json()
        if owner and (
            not binding or binding[0]["name"] != name or binding[0]["spec"] != serialized
        ):
            with self.store.connect() as conn:
                conn.execute(
                    "INSERT INTO backend_bindings VALUES(?,?,?) "
                    "ON CONFLICT(owner_id) DO UPDATE SET name=excluded.name,spec=excluded.spec",
                    (owner, name, serialized),
                )
                self.store.event(
                    conn,
                    owner,
                    "backend_selected",
                    {
                        "name": name,
                        "spec": spec.model_dump(),
                        "previous": binding[0]["name"] if binding else None,
                    },
                )
        key = (name, serialized)
        if key not in self._gateways:
            backend = self.backends.build(spec)
            backend.model = f"{name}/{spec.type}:{backend.model}"
            self._gateways[key] = backend
        return self._gateways[key]

    def book(self, identifier):
        return Book.model_validate_json(self.store.get("books", identifier)["data"])

    def version(self, identifier):
        row = self.store.get("versions", identifier)
        book = self.book(row["book_id"])
        chunks = self.section_chunks(book, row["section_id"])
        return row, book, chunks

    @staticmethod
    def section_chunks(book, section_spec):
        section_id, _, page_range = section_spec.partition("@")
        section = next((s for s in book.sections if s.id == section_id), None)
        if not section:
            raise AppError("章节不存在。")
        start, end = section.start_page, section.end_page
        if page_range:
            try:
                start, end = map(int, page_range.split(":"))
            except ValueError as exc:
                raise AppError("页码范围使用 起始页:结束页，例如 1:5。") from exc
            if not section.start_page <= start <= end <= section.end_page:
                raise AppError("页码范围必须位于所选章节内。")
        return [c for c in book.chunks if c.id in section.chunk_ids and start <= c.page <= end]

    def import_pdf(self, path: Path, demo=False):
        book = parse_pdf(path)
        existing = self.store.rows("SELECT id FROM books WHERE sha256=?", (book.sha256,))
        if existing:
            return self.book(existing[0]["id"])
        book.demo = demo
        destination = self.store.root / "documents" / f"{book.sha256}.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temp = destination.with_suffix(".part")
            temp.write_bytes(path.read_bytes())
            temp.replace(destination)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO books VALUES(?,?,?)", (book.id, book.sha256, book.model_dump_json())
            )
        return book

    @staticmethod
    def batches(chunks, limit=10000):
        result, current, size = [], [], 0
        for chunk in chunks:
            if current and size + len(chunk.text) > limit:
                result.append(current)
                current, size = [], 0
            current.append(chunk)
            size += len(chunk.text)
        if current:
            result.append(current)
        return result

    def delete_book(self, identifier):
        with self.store.connect() as conn:
            row = conn.execute("SELECT sha256 FROM books WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise AppError(f"找不到 {identifier}，请使用 teach books 列表中的完整 ID。")
            versions = conn.execute(
                "SELECT id,status FROM versions WHERE book_id=? ORDER BY created_at,id",
                (identifier,),
            ).fetchall()
            if versions:
                details = "、".join(f"{v['id']} ({v['status']})" for v in versions)
                raise AppError(
                    f"教材仍有关联版本：{details}。"
                    "请先用 teach delete VERSION 删除未发布草稿；有已发布版本的教材不能删除。"
                )
            digest = row["sha256"]
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise AppError("教材文件摘要无效，无法安全定位 PDF。")
            document = self.store.root / "documents" / f"{digest}.pdf"
            if not document.resolve().is_relative_to(self.store.root.resolve()):
                raise AppError("教材 PDF 路径超出数据目录，停止删除。")
            conn.execute("DELETE FROM events WHERE owner_id=?", (identifier,))
            conn.execute("DELETE FROM backend_bindings WHERE owner_id=?", (identifier,))
            conn.execute("DELETE FROM books WHERE id=?", (identifier,))
        # Remove the copy only after commit, so a database failure cannot lose the PDF.
        try:
            document.unlink(missing_ok=True)
        except OSError as exc:
            raise AppError(f"教材记录已删除，但 PDF 副本清理失败，请手动删除：{document}") from exc

    def delete_draft(self, identifier):
        with self.store.connect() as conn:
            row = conn.execute("SELECT status FROM versions WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise AppError(f"找不到 {identifier}，请使用 teach drafts 列表中的完整 ID。")
            if row["status"] != "draft":
                raise AppError("不能删除已发布版本；teach delete 仅支持未发布草稿。")
            conn.execute("DELETE FROM draft_batches WHERE version_id=?", (identifier,))
            conn.execute("DELETE FROM preparations WHERE version_id=?", (identifier,))
            conn.execute("DELETE FROM events WHERE owner_id=?", (identifier,))
            conn.execute("DELETE FROM backend_bindings WHERE owner_id=?", (identifier,))
            conn.execute("DELETE FROM versions WHERE id=?", (identifier,))

    def create_draft(self, book_id, section_id):
        book = self.book(book_id)
        if not self.section_chunks(book, section_id):
            raise AppError("章节不存在或没有可用文本。")
        identifier = uid("ver")
        model = self.gateway(book, identifier).model
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    book.id,
                    section_id,
                    "draft",
                    0,
                    "{}",
                    None,
                    None,
                    "{}",
                    None,
                    model,
                    now(),
                ),
            )
        return identifier

    def build_draft(self, identifier, progress=None):
        def report_progress(message):
            if progress is not None:
                progress(message)

        row, book, chunks = self.version(identifier)
        if row["status"] != "draft" or row["data"] != "{}":
            raise AppError("草稿已完成提取；请使用 revise 或编辑，而不是覆盖。")
        batches = self.batches(chunks)
        cached = {
            r["position"]: r["data"]
            for r in self.store.rows(
                "SELECT * FROM draft_batches WHERE version_id=?", (identifier,)
            )
        }
        partials = []
        report_progress(
            f"共 {len(chunks)} 个文本块，分为 {len(batches)} 批；已保存 {len(cached)} 批"
        )
        for position, batch in enumerate(batches):
            label = (
                f"第 {position + 1}/{len(batches)} 批（PDF 页 {batch[0].page}–{batch[-1].page}）"
            )
            if position in cached:
                report_progress(f"{label}：复用已保存结果")
                partial = Draft.model_validate_json(cached[position])
            else:
                gateway = self.gateway(book, identifier)
                rejected = self.store.rows(
                    "SELECT data FROM events WHERE owner_id=? AND kind='batch_rejected' "
                    "ORDER BY id DESC",
                    (identifier,),
                )
                previous = next(
                    (
                        json.loads(r["data"])
                        for r in rejected
                        if json.loads(r["data"])["position"] == position
                    ),
                    None,
                )
                report_progress(
                    f"{label}：{'恢复已保存候选' if previous else '等待模型提取'}"
                    f"；后端/模型：{gateway.model}"
                )
                partial = (
                    Draft.model_validate(previous["candidate"])
                    if previous
                    else gateway.extract(batch)
                )
                report_progress(f"{label}：校验提取结果")
                report = quality_report(partial, batch)
                if report["errors"]:
                    self.save_rejected_batch(identifier, position, partial, report, gateway.model)
                    report_progress(
                        f"{label}：发现 {len(report['errors'])} 项错误，等待模型自动校正"
                    )
                    partial = gateway.revise(
                        partial,
                        batch,
                        "修复以下程序校验错误，返回完整草稿。仅修改有问题的引用及必要关联；"
                        "不要重写无关目标。引用必须逐字匹配且在对应chunk内唯一；"
                        "有重复时扩展连续原文上下文（保留换行），不要猜测第一次出现的位置，"
                        "也不要删掉目标或内容清单来绕过检查。\n" + "\n".join(report["errors"]),
                    )
                    report_progress(f"{label}：校验自动校正结果")
                    report = quality_report(partial, batch)
                    if report["errors"]:
                        self.save_rejected_batch(
                            identifier, position, partial, report, gateway.model
                        )
                        raise AppError(
                            "本批自动校正后仍未通过校验；候选已保存，恢复时继续校正："
                            + "；".join(report["errors"][:4])
                        )
                with self.store.connect() as conn:
                    conn.execute(
                        "INSERT INTO draft_batches VALUES(?,?,?)",
                        (identifier, position, partial.model_dump_json()),
                    )
                    self.store.event(
                        conn,
                        identifier,
                        "batch_extracted",
                        {
                            "position": position,
                            "model": gateway.model,
                        },
                    )
                report_progress(f"{label}：已保存")
            value = partial.model_dump()
            prefix = f"b{position}_"
            for point in value["points"]:
                point["key"] = prefix + point["key"]
                for obj in point["objectives"]:
                    obj["key"] = prefix + obj["key"]
                    obj["prerequisites"] = [prefix + k for k in obj["prerequisites"]]
                    obj["sample_question"]["targeted_misconception_codes"] = [
                        prefix + k for k in obj["sample_question"]["targeted_misconception_codes"]
                    ]
            for item in value["content_items"]:
                item["key"] = prefix + item["key"]
                item["objective_keys"] = [prefix + k for k in item["objective_keys"]]
            for misconception in value["misconceptions"]:
                misconception["code"] = prefix + misconception["code"]
                misconception["objective_keys"] = [
                    prefix + k for k in misconception["objective_keys"]
                ]
            partials.append(value)
        if not partials:
            raise AppError("没有可提取内容。")
        combined = {
            field: [item for part in partials for item in part[field]]
            for field in ["points", "content_items", "misconceptions", "notes"]
        }
        if len(partials) > 1:
            combined["notes"].append("多批提取：请人工检查跨批重复和前置关系。")
        report_progress("合并各批结果并保存草稿")
        self.save_draft(identifier, Draft.model_validate(combined), "分批提取完成")
        report_progress("草稿提取完成")

    def save_rejected_batch(self, identifier, position, partial, report, model):
        with self.store.connect() as conn:
            self.store.event(
                conn,
                identifier,
                "batch_rejected",
                {
                    "position": position,
                    "candidate": partial.model_dump(),
                    "errors": report["errors"],
                    "model": model,
                },
            )

    def save_draft(self, identifier, draft: Draft, reason, preparation=None):
        row, _, _ = self.version(identifier)
        if row["status"] != "draft":
            raise AppError("已发布内容不可修改，请先 clone 创建新草稿。")
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE versions SET data=?,revision=revision+1,audit=NULL,"
                "reviewed_revision=NULL,decisions='{}',approved_revision=NULL WHERE id=?",
                (draft.model_dump_json(), identifier),
            )
            self.store.event(
                conn,
                identifier,
                "draft_changed",
                {
                    "reason": reason,
                    "before": json.loads(row["data"]),
                    "after": draft.model_dump(),
                },
            )
            if preparation is None:
                conn.execute("DELETE FROM preparations WHERE version_id=?", (identifier,))
            else:
                preparation["revision"] = row["revision"] + 1
                self.save_preparation(identifier, preparation, conn)

    def preparation(self, identifier):
        # Read-only commands must remain usable on databases not yet migrated.
        if not self.store.rows("SELECT name FROM sqlite_master WHERE name='preparations'"):
            return None
        rows = self.store.rows("SELECT data FROM preparations WHERE version_id=?", (identifier,))
        return json.loads(rows[0]["data"]) if rows else None

    def save_preparation(self, identifier, state, conn=None):
        if conn is None:
            with self.store.connect() as tx:
                return self.save_preparation(identifier, state, tx)
        conn.execute(
            "INSERT INTO preparations VALUES(?,?) "
            "ON CONFLICT(version_id) DO UPDATE SET data=excluded.data",
            (identifier, dumps(state)),
        )

    def prepare(self, identifier, retry=False, progress=None):
        from .preparation import prepare

        return prepare(self, identifier, retry=retry, progress=progress)

    @staticmethod
    def report_patch_metrics(metrics, emit):
        emit("修订：已收到局部修改包")
        if metrics:
            emit(
                f"修订调用耗时 {metrics['elapsed_seconds']:.1f} 秒；"
                f"输入数据 {metrics['input_data_chars']} 字符（不含提示词和 Schema）；"
                f"修改包 {metrics['patch_chars']} 字符，完整草稿 {metrics['draft_chars']} 字符"
            )

    def revise(self, identifier, instruction, progress=None):
        emit = progress or (lambda message: None)
        row, book, chunks = self.version(identifier)
        if row["status"] != "draft":
            raise AppError("请先 clone 已发布版本。")
        draft = Draft.model_validate_json(row["data"])
        events = self.store.rows(
            "SELECT kind,data FROM events WHERE owner_id=? AND "
            "kind IN ('revision_patch_received','revision_patch_processed') ORDER BY id DESC",
            (identifier,),
        )
        completed, saved = set(), None
        for event in events:
            value = json.loads(event["data"])
            if event["kind"] == "revision_patch_processed":
                completed.add(value["request_id"])
            elif (
                value["request_id"] not in completed
                and value["revision"] == row["revision"]
                and value["instruction"] == instruction
            ):
                saved = value
                break
        if saved:
            emit("修订：复用已保存修改包，不重复模型调用")
        else:
            gateway = self.gateway(book, identifier)
            emit(f"修订：检查请求大小并等待局部修订；后端/模型：{gateway.model}")
            patch = gateway.revise_patch(draft, chunks, instruction)
            saved = {
                "request_id": uid("patch"),
                "revision": row["revision"],
                "instruction": instruction,
                "patch": patch.model_dump(),
                "metrics": getattr(gateway, "last_revision_metrics", None),
            }
            with self.store.connect() as conn:
                self.store.event(conn, identifier, "revision_patch_received", saved)
        self.report_patch_metrics(saved["metrics"], emit)
        try:
            emit("修订：合并修改包，保留未改动内容")
            candidate = merge_patch(draft, DraftPatch.model_validate(saved["patch"]))
            emit("修订：校验完整草稿的引用、覆盖和依赖")
            errors = quality_report(candidate, chunks)["errors"]
            if errors:
                raise AppError("；".join(errors[:5]))
        except AppError as exc:
            with self.store.connect() as conn:
                self.store.event(
                    conn,
                    identifier,
                    "revision_patch_processed",
                    {
                        "request_id": saved["request_id"],
                        "outcome": "rejected",
                    },
                )
                self.store.event(
                    conn,
                    identifier,
                    "revision_rejected",
                    {"patch": saved["patch"], "errors": [str(exc)]},
                )
            raise AppError("修订未通过，原草稿未覆盖：" + str(exc)) from exc
        self.save_draft(identifier, candidate, instruction)
        summary = change_summary(draft, candidate)
        with self.store.connect() as conn:
            self.store.event(
                conn,
                identifier,
                "revision_patch_processed",
                {
                    "request_id": saved["request_id"],
                    "outcome": "saved",
                    "changes": summary,
                },
            )
        for name, counts in summary.items():
            if isinstance(counts, dict):
                emit(
                    f"修订变更 {name}：新增 {counts['added']}、"
                    f"删除 {counts['removed']}、修改 {counts['updated']}"
                )
        emit("修订：新草稿已保存，需重新审核")
        return candidate

    def clone(self, identifier):
        row, book, _ = self.version(identifier)
        new_id = self.create_draft(book.id, row["section_id"])
        self.save_draft(new_id, Draft.model_validate_json(row["data"]), f"派生自 {identifier}")
        return new_id

    def audit(self, identifier, progress=None, preparation=None):
        emit = progress or (lambda message: None)
        label = (
            f"第 {preparation['rounds']}/2 轮再审核"
            if preparation and preparation["rounds"]
            else "首次审核"
        )
        emit(f"{label}：检查草稿结构、引用和覆盖")
        row, book, chunks = self.version(identifier)
        task = preparation or self.preparation(identifier)
        if task and task["revision"] != row["revision"]:
            task = None
        if row["status"] != "draft":
            raise AppError("已发布版本无需重新审核；请派生草稿。")
        draft = Draft.model_validate_json(row["data"])
        errors = quality_report(draft, chunks)["errors"]
        if errors:
            raise AppError("请先修复结构错误：" + "；".join(errors[:5]))
        # Whole-draft audit also checks cross-batch ordering. Oversized input is rejected,
        # never silently truncated. User can select a smaller page range when importing.
        gateway = self.gateway(book, identifier)
        emit(f"{label}：检查请求大小并等待模型返回；后端/模型：{gateway.model}")
        audit = gateway.audit(draft, chunks)
        emit(f"{label}：校验审核报告引用")
        chunk_map = {c.id: c for c in chunks}
        for issue in audit.issues:
            if check_refs(issue.source_refs, chunk_map):
                raise AppError("审核返回了无效原文引用，请重试。")
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE versions SET audit=?,reviewed_revision=?,decisions='{}',"
                "approved_revision=NULL WHERE id=?",
                (audit.model_dump_json(), row["revision"], identifier),
            )
            self.store.event(conn, identifier, "audit", audit.model_dump())
            self.store.event(conn, identifier, "audit_backend", {"model": gateway.model})
            if task is not None:
                # An explicit audit must not reset the bounded repair budget or lose a candidate.
                task["issues"] = [issue.model_dump() for issue in audit.issues]
                if task["candidate"] is None and task["phase"] != "merge":
                    task["phase"] = "evaluate"
                self.save_preparation(identifier, task, conn)
        errors_count = sum(issue.severity == "error" for issue in audit.issues)
        emit(
            f"{label}：报告已保存，{errors_count} 项 error，"
            f"{len(audit.issues) - errors_count} 项 warning"
        )
        return audit

    def resolve_issue(self, identifier, index, note):
        row, _, _ = self.version(identifier)
        if row["status"] != "draft" or row["reviewed_revision"] != row["revision"]:
            raise AppError("请先审核当前草稿。")
        audit = Audit.model_validate_json(row["audit"])
        if not 0 <= index < len(audit.issues) or not note.strip():
            raise AppError("问题编号无效或缺少处理理由。")
        decisions = json.loads(row["decisions"])
        decisions[str(index)] = note
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE versions SET decisions=?,approved_revision=NULL WHERE id=?",
                (dumps(decisions), identifier),
            )
            self.store.event(conn, identifier, "issue_resolved", {"index": index, "note": note})

    def approve(self, identifier):
        row, _, chunks = self.version(identifier)
        if row["status"] != "draft":
            raise AppError("版本已经发布。")
        draft = Draft.model_validate_json(row["data"])
        errors = quality_report(draft, chunks)["errors"]
        if row["reviewed_revision"] != row["revision"]:
            errors.append("当前修订尚未完成审核。")
        else:
            audit = Audit.model_validate_json(row["audit"])
            errors.extend(
                f"审核问题 {i + 1} 尚未处理。"
                for i, issue in enumerate(audit.issues)
                if issue.severity == "error"
            )
        if errors:
            raise AppError("不能确认：" + "；".join(errors))
        with self.store.connect() as conn:
            conn.execute("UPDATE versions SET approved_revision=revision WHERE id=?", (identifier,))
            self.store.event(conn, identifier, "human_approved", {"revision": row["revision"]})

    def publish(self, identifier):
        row, _, chunks = self.version(identifier)
        if row["status"] == "published":
            return
        if row["approved_revision"] != row["revision"]:
            raise AppError("必须先审核并人工 approve 当前修订。")
        if row["reviewed_revision"] != row["revision"] or not row["audit"]:
            raise AppError("必须先审核当前修订。")
        if any(i.severity == "error" for i in Audit.model_validate_json(row["audit"]).issues):
            raise AppError("存在审核 error，人工备注不能放行，请修订并重新审核。")
        if quality_report(Draft.model_validate_json(row["data"]), chunks)["errors"]:
            raise AppError("结构校验失败。")
        with self.store.connect() as conn:
            conn.execute("UPDATE versions SET status='published' WHERE id=?", (identifier,))
            self.store.event(conn, identifier, "published", {"revision": row["revision"]})

    def evidence(self, version_id):
        rows = self.store.rows(
            "SELECT a.*,r.data AS assessment FROM attempts a JOIN assessments r ON "
            "r.attempt_id=a.id AND r.revision=(SELECT MAX(revision) FROM assessments "
            "WHERE attempt_id=a.id) WHERE a.version_id=? ORDER BY a.created_at,a.id",
            (version_id,),
        )
        for row in rows:
            row["question"] = json.loads(row["question"])
            row["assessment"] = json.loads(row["assessment"])
        return rows

    def seen_questions(self, version_id, objective_key):
        result = []
        for row in self.store.rows(
            "SELECT e.data FROM events e JOIN sessions s ON s.id=e.owner_id "
            "WHERE s.version_id=? AND e.kind='question_presented' ORDER BY e.id",
            (version_id,),
        ):
            value = json.loads(row["data"])
            if value["objective_key"] == objective_key:
                result.append(value["question"])
        return result

    def progress(self, version_id):
        row, _, _ = self.version(version_id)
        draft = Draft.model_validate_json(row["data"])
        return project(self.evidence(version_id), draft.objectives(), draft.misconceptions)

    def start(self, version_id, point=None, review=False):
        row, _, _ = self.version(version_id)
        if row["status"] != "published":
            raise AppError("请先审核并发布该版本。")
        draft = Draft.model_validate_json(row["data"])
        selected = None
        if point:
            match = next((p for p in draft.points if p.key == point), None)
            if not match:
                raise AppError("知识点不存在。")
            selected = [o.key for o in match.objectives if not o.excluded_reason]
        if review:
            selected = [
                key
                for key, value in self.progress(version_id).items()
                if value["status"] == "review_due"
            ]
            if not selected:
                raise AppError("目前没有到期复习目标。")
        order = ordered_objectives(draft, selected)
        state = {
            "lifecycle": "active",
            "phase": "planning",
            "order": order,
            "skipped": [],
            "deferred": [],
            "current": None,
            "task": None,
            "pending_attempt": None,
            "feedback": None,
            "why": "开始诊断必要目标",
        }
        identifier = uid("session")
        self.gateway(self.book(row["book_id"]), identifier)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?)",
                (identifier, version_id, 0, dumps(state), now()),
            )
        return identifier

    def session(self, identifier):
        row = self.store.get("sessions", identifier)
        return row, json.loads(row["data"])

    def advance(self, identifier):
        row, state = self.session(identifier)
        if state["phase"] not in {"planning", "feedback_ready"}:
            raise AppError("请先完成当前题或恢复待处理评分。")
        if state["lifecycle"] in {"completed", "ended_with_gaps"}:
            return state
        ver, book, chunks = self.version(row["version_id"])
        draft = Draft.model_validate_json(ver["data"])
        objectives, progress = draft.objectives(), self.progress(ver["id"])
        evidence = self.evidence(ver["id"])
        unavailable = set(state["skipped"] + state["deferred"])
        current = None
        for key in state["order"]:
            if key in unavailable:
                continue
            obj = objectives[key]
            if any(pre in unavailable for pre in obj.prerequisites):
                unavailable.add(key)
                continue
            if progress[key]["status"] == "provisionally_mastered":
                continue
            all_attempts = self.store.rows(
                "SELECT id FROM attempts WHERE session_id=? AND objective_key=?", (identifier, key)
            )
            recent = [
                e for e in evidence if e["session_id"] == identifier and e["objective_key"] == key
            ]
            failures = sum(
                Assessment.model_validate(e["assessment"]).status == "valid"
                and not score(
                    Assessment.model_validate(e["assessment"]),
                    Question.model_validate(e["question"]),
                )[1]
                for e in recent
                if not e["practice_only"]
            )
            invalid = sum(e["assessment"]["status"] == "invalid_question" for e in recent)
            unresolved = len(recent) >= 2 and all(
                e["assessment"]["status"] == "needs_review" for e in recent[-2:]
            )
            if len(all_attempts) >= 8 or failures >= 3 or invalid >= 2 or unresolved:
                state["deferred"].append(key)
                unavailable.add(key)
                continue
            current = key
            break
        if current is None:
            state.update(
                lifecycle="ended_with_gaps" if unavailable else "completed",
                phase="feedback_ready",
                task=None,
                current=None,
                why="本章结束；跳过或未达标目标保留为缺口。" if unavailable else "所有目标达标。",
            )
            state["blocked"] = sorted(unavailable - set(state["skipped"] + state["deferred"]))
            self.store.save_session(identifier, row["revision"], state)
            return state
        obj, mastery = objectives[current], progress[current]
        action = {
            "unknown": "DIAGNOSE",
            "learning": "REASSESS",
            "needs_remediation": "REMEDIATE",
            "review_due": "REVIEW",
        }[mastery["status"]]
        history_rows = [e for e in evidence if e["objective_key"] == current]
        history = [
            {
                "stem": e["question"]["stem"],
                "novelty_group": e["question"]["novelty_group"],
                "feedback": e["assessment"]["feedback"],
            }
            for e in history_rows
        ]
        seen = self.seen_questions(ver["id"], current)
        answered_stems = {item["stem"] for item in history}
        history.extend(
            {
                "stem": q["stem"],
                "novelty_group": q["novelty_group"],
                "feedback": "已展示但未作答，不得复用",
            }
            for q in seen
            if q["stem"] not in answered_stems
        )
        source_ids = {ref.chunk_id for ref in obj.source_refs}
        selected_chunks = [c for c in chunks if c.id in source_ids]
        gateway = self.gateway(book, identifier)
        task = gateway.task(
            obj, selected_chunks, action, history, [m.model_dump() for m in draft.misconceptions]
        )
        if task.objective_key != current or task.action != action or not task.question:
            raise AppError("生成任务与指定目标或动作不符。")
        if check_refs(task.question.source_refs, {c.id: c for c in selected_chunks}):
            raise AppError("题目原文引用无效。")
        known_codes = {m.code for m in draft.misconceptions}
        if not set(task.question.targeted_misconception_codes) <= known_codes:
            raise AppError("题目引用了未知误解码。")
        if not set(mastery["misconceptions"]) <= set(task.question.targeted_misconception_codes):
            raise AppError("新题没有针对当前关键误解，请重试生成。")
        prior_groups = {q["novelty_group"] for q in seen}
        prior_stems = {q["stem"].strip().casefold() for q in seen}
        if (
            task.question.novelty_group in prior_groups
            or task.question.stem.casefold() in prior_stems
        ):
            raise AppError("模型生成了已用场景。状态未推进，请重试或请求讲解。")
        state.update(
            current=current,
            phase="awaiting_answer",
            lifecycle="active",
            task=task.model_dump(),
            question_id=uid("q"),
            hint_count=0,
            feedback=None,
            why=f"{current} 状态 {mastery['status']}，选择 {action}。",
        )
        with self.store.connect() as conn:
            self.store.save_session(identifier, row["revision"], state, conn)
            self.store.event(
                conn,
                identifier,
                "question_presented",
                {
                    "objective_key": current,
                    "question_id": state["question_id"],
                    "question": task.question.model_dump(),
                    "action": action,
                    "reason": state["why"],
                    "model": gateway.model,
                },
            )
        return state

    def submit(self, identifier, answer):
        row, state = self.session(identifier)
        if state["phase"] != "awaiting_answer" or not state["task"]:
            raise AppError("当前没有待答题。")
        if not answer.strip() or len(answer) > 10000:
            raise AppError("回答必须为 1–10000 字符。")
        question = state["task"]["question"]
        attempt_id = uid("attempt")
        state.update(phase="evaluating", pending_attempt=attempt_id)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    identifier,
                    row["version_id"],
                    state["current"],
                    state["question_id"],
                    dumps(question),
                    answer,
                    int(state["hint_count"] > 0),
                    0,
                    state["task"]["action"],
                    now(),
                ),
            )
            self.store.save_session(identifier, row["revision"], state, conn)
        return self.finish_assessment(identifier)

    def assess(self, attempt):
        ver, book, chunks = self.version(attempt["version_id"])
        draft = Draft.model_validate_json(ver["data"])
        question = Question.model_validate_json(attempt["question"])
        source_ids = {r.chunk_id for r in question.source_refs}
        gateway = self.gateway(book, attempt["session_id"])
        assessment = gateway.grade(
            question,
            attempt["answer"],
            [c for c in chunks if c.id in source_ids],
            [m.model_dump() for m in draft.misconceptions],
        )
        validate_assessment(
            assessment, question, attempt["answer"], {m.code for m in draft.misconceptions}
        )
        value, passed = score(assessment, question)
        if value is not None and value >= 0.8 and not passed:
            assessment.status = "needs_review"
        return assessment, gateway.model

    def finish_assessment(self, identifier):
        row, state = self.session(identifier)
        if state["phase"] != "evaluating" or not state["pending_attempt"]:
            raise AppError("没有待恢复的评分。")
        attempt = self.store.get("attempts", state["pending_attempt"])
        assessment, model = self.assess(attempt)
        state.update(phase="feedback_ready", feedback=assessment.model_dump(), pending_attempt=None)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO assessments VALUES(?,?,?,?,?,?,?)",
                (
                    uid("assessment"),
                    attempt["id"],
                    1,
                    assessment.model_dump_json(),
                    "首次评分",
                    model,
                    now(),
                ),
            )
            self.store.save_session(identifier, row["revision"], state, conn)
        return state

    def reassess(self, attempt_id, reason):
        attempt = self.store.get("attempts", attempt_id)
        old = self.store.rows(
            "SELECT MAX(revision) AS rev FROM assessments WHERE attempt_id=?", (attempt_id,)
        )[0]["rev"]
        if not old:
            raise AppError("首次评分尚未完成，请恢复原会话。")
        assessment, model = self.assess(attempt)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO assessments VALUES(?,?,?,?,?,?,?)",
                (
                    uid("assessment"),
                    attempt_id,
                    old + 1,
                    assessment.model_dump_json(),
                    reason,
                    model,
                    now(),
                ),
            )
            self.store.event(
                conn,
                attempt["session_id"],
                "assessment_revised",
                {"attempt_id": attempt_id, "revision": old + 1, "reason": reason},
            )
            sessions = conn.execute(
                "SELECT * FROM sessions WHERE version_id=?", (attempt["version_id"],)
            ).fetchall()
            for session in sessions:
                state = json.loads(session["data"])
                if state["lifecycle"] in {"completed", "ended_with_gaps"}:
                    continue
                state["needs_replan"] = True
                if (
                    state["phase"] == "feedback_ready"
                    and state.get("question_id") == attempt["question_id"]
                ):
                    state["feedback"] = assessment.model_dump()
                self.store.save_session(session["id"], session["revision"], state, conn)
        # Projections are replayed from latest revisions, so there are no stale persisted scores.
        return assessment

    def hint(self, identifier):
        row, state = self.session(identifier)
        if state["phase"] != "awaiting_answer":
            raise AppError("当前没有待答题。")
        hints = state["task"]["question"]["hints"]
        if state["hint_count"] >= len(hints):
            return "没有更多提示，可使用 /explain 请求讲解。"
        text = hints[state["hint_count"]]
        state["hint_count"] += 1
        self.store.save_session(identifier, row["revision"], state)
        return text

    def skip(self, identifier):
        row, state = self.session(identifier)
        if state["phase"] == "evaluating":
            raise AppError("回答已保存，请先恢复评分。")
        if state["current"] and state["current"] not in state["skipped"]:
            state["skipped"].append(state["current"])
        state.update(phase="planning", task=None)
        self.store.save_session(identifier, row["revision"], state)
        return self.advance(identifier)

    def pause(self, identifier):
        row, state = self.session(identifier)
        if state["lifecycle"] not in {"completed", "ended_with_gaps"}:
            state["lifecycle"] = "paused"
            self.store.save_session(identifier, row["revision"], state)

    def resume(self, identifier):
        row, state = self.session(identifier)
        if state["lifecycle"] in {"completed", "ended_with_gaps"}:
            return state
        _, book, _ = self.version(row["version_id"])
        self.gateway(book, identifier)
        state["lifecycle"] = "active"
        if state.pop("needs_replan", False) and state["phase"] == "awaiting_answer":
            ver, _, _ = self.version(row["version_id"])
            draft = Draft.model_validate_json(ver["data"])
            current = state["current"]
            mastery = self.progress(ver["id"])
            if mastery[current]["status"] == "provisionally_mastered" or any(
                mastery[pre]["status"] != "provisionally_mastered"
                for pre in draft.objectives()[current].prerequisites
            ):
                state.update(phase="planning", task=None)
        self.store.save_session(identifier, row["revision"], state)
        if state["phase"] == "evaluating":
            return self.finish_assessment(identifier)
        if state["phase"] == "planning":
            return self.advance(identifier)
        return state

    def explain(self, identifier):
        row, state = self.session(identifier)
        if not state["current"] or state["phase"] == "evaluating":
            raise AppError("当前不能请求讲解。")
        ver, book, chunks = self.version(row["version_id"])
        draft = Draft.model_validate_json(ver["data"])
        obj = draft.objectives()[state["current"]]
        # Revoke the question before a remote call; a failure must not restore independence.
        state.update(phase="planning", task=None)
        self.store.save_session(identifier, row["revision"], state)
        refs = {r.chunk_id for r in obj.source_refs}
        task = self.gateway(book, identifier).task(
            obj, [c for c in chunks if c.id in refs], "EXPLAIN", [], []
        )
        if task.action != "EXPLAIN" or task.objective_key != obj.key or task.question:
            raise AppError("讲解任务契约错误。")
        row, state = self.session(identifier)
        state.update(phase="feedback_ready", task=task.model_dump(), feedback=None)
        self.store.save_session(identifier, row["revision"], state)
        return state
