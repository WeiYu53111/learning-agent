"""Pair-level relation cache, bounded chapter traversal, and atomic graph commits."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque

from pydantic import ValidationError

from .book_models import RelationChecks, Relations
from .models import AppError, ModelResponseError
from .storage import dumps, now, uid

SELECTOR_VERSION = "lexical-prefix-v2"
VALIDATOR_VERSION = "explicit-pairs-v1"
LEGACY_COMPATIBLE_VALIDATOR = "explicit-pairs-v1"


class RelationFailure(AppError):
    """Model analysis failed after durable progress was saved."""


class RelationsPaused(AppError):
    """Normal incomplete coverage; CLI uses exit status 3."""


def point_fingerprint(point):
    return hashlib.sha256(
        dumps({k: point[k] for k in ("title", "description", "source_refs", "section_id")}).encode()
    ).hexdigest()


def pair_key(a, b):
    return "pair_" + hashlib.sha256(dumps(sorted((a, b))).encode()).hexdigest()[:32]


class RelationAnalysis:
    def __init__(self, library, index, emit=lambda _: None):
        self.library, self.store, self.index, self.emit = library, library.store, index, emit
        self.data = json.loads(index["data"])
        self.points = {p["key"]: p for p in self.data["points"]}
        self.fingerprints = {k: point_fingerprint(p) for k, p in self.points.items()}
        # Keep planner choices injectable and independent from transport batch sizes.
        from .book_learning import candidate_pairs, tokens

        briefs = [library.brief(p) for p in self.points.values()]
        self.candidates = [
            tuple(sorted((briefs[i]["key"], briefs[j]["key"])))
            for i, j in candidate_pairs(briefs, tokens)
        ]
        self.candidates = list(dict.fromkeys(self.candidates))

    def graph(self):
        return json.loads(
            self.store.rows("SELECT data FROM book_indexes WHERE id=?", (self.index["id"],))[0][
                "data"
            ]
        )["relations"]

    def validate_edges(self, edges):
        from .book_learning import relation_errors

        Relations(relations=edges)
        errors = relation_errors(edges, set(self.points))
        if errors:
            raise AppError("；".join(errors))

    def member(self, a, b, reason="candidate"):
        a, b = sorted((a, b))
        return dict(
            pair_key=pair_key(a, b),
            left_key=a,
            right_key=b,
            left_fingerprint=self.fingerprints[a],
            right_fingerprint=self.fingerprints[b],
            reason=reason,
        )

    def insert_check(self, conn, member, edges, origin, model, validator=None):
        identifier = uid("check")
        conn.execute(
            "INSERT INTO relation_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                self.index["book_id"],
                member["pair_key"],
                member["left_key"],
                member["right_key"],
                member["left_fingerprint"],
                member["right_fingerprint"],
                validator or VALIDATOR_VERSION,
                dumps(edges),
                origin,
                model,
                now(),
            ),
        )
        return identifier

    def save_graph(self, conn, accepted):
        """Caller validates the merged graph; commit graph and pair links together."""
        revision = (
            conn.execute(
                "SELECT graph_revision FROM index_content_state WHERE index_id=?",
                (self.index["id"],),
            ).fetchone()[0]
            + 1
        )
        for check_id, edges in accepted:
            for edge in edges:
                conn.execute(
                    "INSERT OR IGNORE INTO relation_graph_edges VALUES(?,?,?,?,?,?,?)",
                    (
                        self.index["id"],
                        check_id,
                        edge["source"],
                        edge["target"],
                        edge["kind"],
                        edge["reason"],
                        revision,
                    ),
                )
        # Compatibility projection; lessons read immutable snapshots instead.
        data = json.loads(
            conn.execute(
                "SELECT data FROM book_indexes WHERE id=?", (self.index["id"],)
            ).fetchone()[0]
        )
        data["relations"] = [
            dict(r)
            for r in conn.execute(
                "SELECT source,target,kind,reason FROM relation_graph_edges WHERE index_id=? "
                "ORDER BY source,target,kind",
                (self.index["id"],),
            )
        ]
        conn.execute("UPDATE book_indexes SET data=? WHERE id=?", (dumps(data), self.index["id"]))
        conn.execute(
            "UPDATE index_content_state SET graph_revision=? WHERE index_id=?",
            (revision, self.index["id"]),
        )

        # New edges can expose prerequisites in previously complete chapter tasks.
        # Update their frontier in the same commit so read-only outline never overstates coverage.
        for task in conn.execute(
            "SELECT * FROM relation_tasks WHERE index_id=? AND selector_version=? "
            "AND validator_version=?",
            (self.index["id"], SELECTOR_VERSION, VALIDATOR_VERSION),
        ).fetchall():
            self.refresh_in_transaction(conn, task, data["relations"])

    def import_legacy(self):
        if self.store.rows(
            "SELECT 1 FROM relation_legacy_imports WHERE index_id=?", (self.index["id"],)
        ):
            return
        from .book_learning import groups

        steps = {}
        for row in self.store.rows(
            "SELECT step,data FROM book_steps WHERE owner_id=? AND "
            "(step LIKE 'relations:%' OR step LIKE 'relations_v2:%')",
            (self.index["id"],),
        ):
            name = row["step"]
            if name != "relations_v2:plan" and not re.fullmatch(
                r"relations:\d+:\d+|relations_v2:batch:\d+", name
            ):
                continue  # Rejected/interrupted records never establish coverage.
            try:
                steps[name] = json.loads(row["data"])
            except ValueError as exc:
                raise AppError(f"旧关系检查点 {self.index['id']}/{name} JSON 损坏。") from exc
        plan = steps.get("relations_v2:plan")
        checks, edges, evidence = {}, list(self.data.get("relations", [])), {"groups": []}
        briefs = [self.library.brief(p) for p in self.points.values()]
        partitions = list(groups(briefs, 6000))
        partition_of = {p["key"]: i for i, batch in enumerate(partitions) for p in batch}
        try:
            if any(re.fullmatch(r"relations_v2:batch:\d+", key) for key in steps) and not plan:
                raise AppError("存在成功批次但缺少 relations_v2:plan")
            if plan:
                seen = set()
                for n, batch in enumerate(plan["batches"]):
                    point_keys = set()
                    for point in batch["points"]:
                        if point != self.library.brief(self.points[point["key"]]):
                            raise AppError(f"relations_v2:plan 批次 {n} 知识点不一致")
                        point_keys.add(point["key"])
                    for a, b in batch["candidate_pairs"]:
                        key = pair_key(a, b)
                        if a == b or {a, b} - point_keys or key in seen:
                            raise AppError(f"relations_v2:plan 批次 {n} 候选无效或重复")
                        seen.add(key)
                edges.extend(
                    Relations(relations=plan.get("legacy", plan.get("reused", []))).model_dump()[
                        "relations"
                    ]
                )
                for name, result in sorted(steps.items()):
                    match = re.fullmatch(r"relations_v2:batch:(\d+)", name)
                    if not match:
                        continue
                    n = int(match[1])
                    if n >= len(plan["batches"]):
                        raise AppError(f"{name} 超出计划")
                    batch = plan["batches"][n]
                    for point in batch["points"]:
                        if point != self.library.brief(self.points[point["key"]]):
                            raise AppError(f"{name} 知识点与计划不一致")
                    allowed = {pair_key(*p): tuple(sorted(p)) for p in batch["candidate_pairs"]}
                    if len(allowed) != len(batch["candidate_pairs"]):
                        raise AppError(f"{name} 候选重复")
                    result_edges = Relations(relations=result["relations"]).model_dump()[
                        "relations"
                    ]
                    for a, b in allowed.values():
                        if a == b or a not in self.points or b not in self.points:
                            raise AppError(f"{name} 候选无效")
                    if any(pair_key(e["source"], e["target"]) not in allowed for e in result_edges):
                        raise AppError(f"{name} 关系越出候选")
                    for key, (a, b) in allowed.items():
                        checks[key] = (
                            self.member(a, b),
                            [e for e in result_edges if pair_key(e["source"], e["target"]) == key],
                        )
                    edges.extend(result_edges)
            for name, result in sorted(steps.items()):
                match = re.fullmatch(r"relations:(\d+):(\d+)", name)
                if not match:
                    continue
                i, j = map(int, match.groups())
                old_edges = Relations(relations=result["relations"]).model_dump()["relations"]
                self.validate_edges(old_edges)
                reliable = 0 <= i <= j < len(partitions)
                if reliable:
                    reliable = all(
                        tuple(sorted((partition_of[e["source"]], partition_of[e["target"]])))
                        == (i, j)
                        for e in old_edges
                    )
                edges.extend(old_edges)
                if not reliable:
                    self.emit(f"{name} 无法可靠重建分组，仅保留有效正向关系，负结果需要重查。")
                    continue
                evidence["groups"].append([i, j])
                for a, b in self.candidates:
                    if tuple(sorted((partition_of[a], partition_of[b]))) == (i, j):
                        key = pair_key(a, b)
                        checks[key] = (
                            self.member(a, b),
                            [e for e in old_edges if pair_key(e["source"], e["target"]) == key],
                        )
            self.validate_edges(edges)
        except (KeyError, IndexError, TypeError, ValueError, AppError) as exc:
            raise AppError(f"旧关系检查点校验失败（{self.index['id']}）：{exc}") from exc
        evidence["partitions"] = [[p["key"] for p in batch] for batch in partitions]
        evidence["fingerprints"] = self.fingerprints
        evidence["edges"] = edges
        for edge in edges:
            a, b = edge["source"], edge["target"]
            key = pair_key(a, b)
            if key not in checks:
                member = self.member(a, b)
                member["origin"] = "legacy_positive"
                checks[key] = (
                    member,
                    [e for e in edges if pair_key(e["source"], e["target"]) == key],
                )
        with self.store.connect() as conn:
            accepted = [(None, edges)]
            for member, result in checks.values():
                identifier = self.insert_check(
                    conn,
                    member,
                    result,
                    member.get("origin", "legacy_implicit"),
                    "legacy",
                    validator=LEGACY_COMPATIBLE_VALIDATOR,
                )
                accepted.append((identifier, result))
            self.save_graph(conn, accepted)
            conn.execute(
                "INSERT INTO relation_legacy_imports VALUES(?,?,?)",
                (self.index["id"], "legacy-import-v1", dumps(evidence)),
            )
        if checks:
            self.emit(f"旧检查点迁移：{len(checks)} 对候选（legacy_implicit）。")

    def task(self, section, depth):
        actual_section = (
            section.removeprefix("@rebuild:").removeprefix("@through:") if section else None
        )
        rebuilding = bool(section and section.startswith(("@rebuild:", "@through:")))
        if (
            section
            and not rebuilding
            and not any(p["section_id"] == actual_section for p in self.points.values())
        ):
            raise AppError("章节不存在或没有可学习的知识点。")
        rows = self.store.rows(
            "SELECT * FROM relation_tasks WHERE index_id=? AND section_id=? "
            "AND selector_version=? AND validator_version=?",
            (self.index["id"], section or "", SELECTOR_VERSION, VALIDATOR_VERSION),
        )
        if rows:
            task = rows[0]
            depth = max(depth, task["max_depth"])
            with self.store.connect() as conn:
                conn.execute(
                    "UPDATE relation_tasks SET max_depth=? WHERE id=?", (depth, task["id"])
                )
            task["max_depth"] = depth
            return task
        task = dict(id=uid("relations"), section_id=section or "", max_depth=depth)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO relation_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    task["id"],
                    self.index["id"],
                    task["section_id"],
                    SELECTOR_VERSION,
                    VALIDATOR_VERSION,
                    "not_started",
                    depth,
                    "[]",
                    None,
                    None,
                ),
            )
        return task

    def refresh_in_transaction(self, conn, task, graph):
        """Recompute shortest prerequisite depths from durable edges; persist frontier."""
        section = task["section_id"]
        rebuilding = section.startswith(("@rebuild:", "@through:"))
        section = section.removeprefix("@rebuild:").removeprefix("@through:")
        order = [s.id for s in self.library.app.book(self.index["book_id"]).sections]
        allowed_sections = order[: order.index(section) + 1] if section in order else order
        allowed = {k for k, p in self.points.items() if p["section_id"] in allowed_sections}
        roots = {
            k
            for k, p in self.points.items()
            if p["section_id"] == section
            or (task["section_id"].startswith("@through:") and k in allowed)
        }
        depth = {k: 0 for k in sorted(roots)}
        incoming = {}
        for edge in graph:
            if edge["kind"] == "prerequisite":
                incoming.setdefault(edge["target"], []).append(edge["source"])
        queue = deque(sorted(roots))
        while queue:
            target = queue.popleft()
            if rebuilding:
                continue
            for source in sorted(incoming.get(target, [])):
                if source not in allowed:
                    continue
                d = 0 if source in roots else depth[target] + 1
                if source not in depth or d < depth[source]:
                    depth[source] = d
                    queue.append(source)
        expanded = {k for k, d in depth.items() if d <= task["max_depth"]}
        frontier = (
            [dict(key=k, depth=d) for k, d in sorted(depth.items()) if d > task["max_depth"]]
            if section
            else []
        )
        for a, b in self.candidates:
            if a not in allowed or b not in allowed:
                continue
            if not section or a in expanded or b in expanded:
                m = self.member(a, b, "chapter" if a in roots or b in roots else "prerequisite")
                conn.execute(
                    "INSERT OR IGNORE INTO relation_task_pairs VALUES(?,?,?,?,?,?,?,NULL)",
                    (
                        task["id"],
                        m["pair_key"],
                        a,
                        b,
                        m["left_fingerprint"],
                        m["right_fingerprint"],
                        m["reason"],
                    ),
                )
        conn.execute(
            "UPDATE relation_tasks SET frontier=? WHERE id=?", (dumps(frontier), task["id"])
        )
        pending = conn.execute(
            "SELECT count(*) FROM relation_task_pairs WHERE task_id=? AND check_id IS NULL",
            (task["id"],),
        ).fetchone()[0]
        conn.execute(
            "UPDATE relation_tasks SET status=? WHERE id=?",
            ("partial" if pending or frontier else "candidate_complete", task["id"]),
        )
        return frontier

    def refresh(self, task):
        graph = self.graph()
        with self.store.connect() as conn:
            return self.refresh_in_transaction(conn, task, graph)

    def pending(self, task):
        return self.store.rows(
            "SELECT * FROM relation_task_pairs WHERE task_id=? "
            "AND check_id IS NULL ORDER BY pair_key",
            (task["id"],),
        )

    def reuse(self, task):
        accepted = []
        graph = self.graph()
        for member in self.pending(task):
            rows = self.store.rows(
                "SELECT * FROM relation_checks WHERE book_id=? AND pair_key=? "
                "AND left_fingerprint=? AND right_fingerprint=? AND validator_version=? "
                "ORDER BY EXISTS(SELECT 1 FROM relation_task_pairs p JOIN relation_tasks t "
                "ON t.id=p.task_id WHERE t.index_id=? AND p.check_id=relation_checks.id) DESC, "
                "created_at DESC,id DESC",
                (
                    self.index["book_id"],
                    member["pair_key"],
                    member["left_fingerprint"],
                    member["right_fingerprint"],
                    VALIDATOR_VERSION,
                    self.index["id"],
                ),
            )
            if not rows:
                rows = self.legacy_cache(member)
            for check in rows:
                edges = json.loads(check["result"])
                try:
                    self.validate_edges(graph + edges)
                except AppError:
                    continue  # Keep conflicting cached judgments immutable; ask model again.
                accepted.append((member, check["id"], edges))
                graph.extend(edges)
                break
        if accepted:
            with self.store.connect() as conn:
                self.save_graph(conn, [(identifier, edges) for _, identifier, edges in accepted])
                for member, identifier, _ in accepted:
                    self.link_check(conn, member, identifier)
        return len(accepted)

    def link_check(self, conn, member, check_id):
        conn.execute(
            "UPDATE relation_task_pairs SET check_id=? WHERE pair_key=? "
            "AND left_fingerprint=? AND right_fingerprint=? AND check_id IS NULL "
            "AND task_id IN (SELECT id FROM relation_tasks WHERE index_id=? "
            "AND validator_version=?)",
            (
                check_id,
                member["pair_key"],
                member["left_fingerprint"],
                member["right_fingerprint"],
                self.index["id"],
                VALIDATOR_VERSION,
            ),
        )

    def legacy_cache(self, member):
        """Materialize only requested pairs from compact legacy group coverage."""
        if VALIDATOR_VERSION != LEGACY_COMPATIBLE_VALIDATOR:
            return []  # A changed validation contract requires explicit re-evaluation.
        rows = self.store.rows(
            "SELECT m.evidence FROM relation_legacy_imports m JOIN book_indexes b "
            "ON b.id=m.index_id WHERE b.book_id=?",
            (self.index["book_id"],),
        )
        a, b = member["left_key"], member["right_key"]
        for row in rows:
            evidence = json.loads(row["evidence"])
            if any(evidence["fingerprints"].get(k) != self.fingerprints[k] for k in (a, b)):
                continue
            positions = {k: i for i, part in enumerate(evidence["partitions"]) for k in part}
            if a not in positions or b not in positions:
                continue
            if sorted((positions[a], positions[b])) not in evidence["groups"]:
                continue
            edges = [e for e in evidence["edges"] if {e["source"], e["target"]} == {a, b}]
            # Import evidence must be valid in this index before materialization.
            try:
                self.validate_edges(self.graph() + edges)
            except AppError:
                continue
            with self.store.connect() as conn:
                identifier = self.insert_check(conn, member, edges, "legacy_implicit", "legacy")
            return [dict(id=identifier, result=dumps(edges))]
        return []

    def coverage(self, task):
        rows = self.store.rows(
            "SELECT p.check_id,c.result FROM relation_task_pairs p "
            "LEFT JOIN relation_checks c ON c.id=p.check_id WHERE task_id=?",
            (task["id"],),
        )
        saved = self.store.rows("SELECT * FROM relation_tasks WHERE id=?", (task["id"],))[0]
        checked = [r for r in rows if r["check_id"]]
        positive = sum(bool(json.loads(r["result"])) for r in checked)
        frontier = json.loads(saved["frontier"])
        status = (
            "candidate_complete"
            if len(checked) == len(rows) and not frontier
            else "partial"
            if rows or frontier
            else "not_started"
        )
        return dict(
            task_id=task["id"],
            status=status,
            total=len(rows),
            checked=len(checked),
            with_relations=positive,
            without_relations=len(checked) - positive,
            unexpanded=len(frontier),
            frontier=frontier,
            last_error=saved["last_error"],
            selector_version=SELECTOR_VERSION,
            validator_version=VALIDATOR_VERSION,
        )

    def request(self, task, run, members, payload, correction=None):
        """Reserve a call before dispatch. No database transaction spans model waiting."""
        gateway = self.library.app.gateway(
            self.library.app.book(self.index["book_id"]), self.index["id"]
        )
        identifier = uid("request")
        with self.store.connect() as conn:
            conn.execute("UPDATE relation_runs SET calls_used=calls_used+1 WHERE id=?", (run,))
            conn.execute(
                "INSERT INTO relation_requests "
                "(id,task_id,run_id,payload,members,status,correction_of,model,input_bytes,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    task["id"],
                    run,
                    dumps(payload),
                    dumps(members),
                    "sent",
                    correction,
                    gateway.model,
                    len(dumps(payload).encode()),
                    now(),
                ),
            )
        started, candidate = time.monotonic(), None
        try:
            candidate = gateway.book_operation("book_relations", payload, RelationChecks)
            candidate = RelationChecks.model_validate(candidate)
            actual = [c.pair_id for c in candidate.checks]
            expected = {m["pair_key"]: m for m in members}
            if len(actual) != len(set(actual)) or set(actual) != set(expected):
                raise AppError(
                    "checks 必须恰好覆盖每个 candidate_pairs 的 pair_id，不得遗漏、重复或增加。"
                )
            edges = []
            for check in candidate.checks:
                m = expected[check.pair_id]
                result = [e.model_dump() for e in check.relations]
                if any(
                    {e["source"], e["target"]} != {m["left_key"], m["right_key"]} for e in result
                ):
                    raise AppError("关系端点必须属于该 candidate_pairs 配对。")
                edges.extend(result)
            self.validate_edges(self.graph() + edges)
        except (AppError, ValidationError) as exc:
            error = str(exc) if isinstance(exc, AppError) else "返回内容不符合逐对响应契约"
            value = candidate.model_dump() if candidate is not None else None
            repairable = (
                isinstance(exc, (ValidationError, ModelResponseError)) or candidate is not None
            )
            with self.store.connect() as conn:
                conn.execute(
                    "UPDATE relation_requests SET status=?,candidate=?,error=?,"
                    "elapsed_seconds=?,output_bytes=?,repairable=? WHERE id=?",
                    (
                        "failed",
                        dumps(value),
                        error,
                        time.monotonic() - started,
                        len(dumps(value).encode()) if value else 0,
                        int(repairable),
                        identifier,
                    ),
                )
                conn.execute(
                    "UPDATE relation_tasks SET last_error=? WHERE id=?", (error, task["id"])
                )
            # Transport failures have no usable response; do not automatically resend them.
            self.emit(
                f"关系请求失败：业务输入 {len(dumps(payload).encode())} 字节，"
                f"输出 {len(dumps(value).encode()) if value else 0} 字节；"
                f"耗时 {time.monotonic() - started:.1f} 秒；校正 {int(bool(correction))} 次。"
            )
            return (
                identifier,
                value,
                error,
                repairable,
            )
        with self.store.connect() as conn:
            accepted = []
            by_key = {m["pair_key"]: m for m in members}
            for check in candidate.checks:
                edges = [e.model_dump() for e in check.relations]
                check_id = self.insert_check(
                    conn, by_key[check.pair_id], edges, "explicit", gateway.model
                )
                accepted.append((check_id, edges))
                self.link_check(conn, by_key[check.pair_id], check_id)
            self.save_graph(conn, accepted)
            conn.execute(
                "UPDATE relation_requests SET status=?,candidate=?,elapsed_seconds=?,"
                "output_bytes=? WHERE id=?",
                (
                    "succeeded",
                    candidate.model_dump_json(),
                    time.monotonic() - started,
                    len(candidate.model_dump_json().encode()),
                    identifier,
                ),
            )
            conn.execute("UPDATE relation_tasks SET last_error=NULL WHERE id=?", (task["id"],))
        self.emit(
            f"关系请求已提交：业务输入 {len(dumps(payload).encode())} 字节，"
            f"输出 {len(candidate.model_dump_json().encode())} 字节；"
            f"耗时 {time.monotonic() - started:.1f} 秒；校正 {int(bool(correction))} 次。"
        )
        return identifier, None, None, False

    def run(self, section=None, max_calls=None, max_depth=2):
        if max_calls is not None and max_calls < 0 or max_depth < 0:
            raise AppError("调用预算和深度必须为非负整数。")
        self.import_legacy()
        task = self.task(section, max_depth)
        self.emit(
            f"关系任务：{task['id']}；范围 {section or '全书'}；"
            f"展开深度 {task['max_depth']}；"
            f"本轮调用预算 {max_calls if max_calls is not None else '不限'}。"
        )
        run = uid("relation_run")
        with self.store.connect() as conn:
            # The data-directory writer lock rules out a still-running predecessor.
            conn.execute(
                "UPDATE relation_runs SET status='interrupted' "
                "WHERE task_id=? AND status='running'",
                (task["id"],),
            )
            conn.execute(
                "UPDATE relation_requests SET status='interrupted' "
                "WHERE task_id=? AND status='sent'",
                (task["id"],),
            )
            conn.execute(
                "INSERT INTO relation_runs VALUES(?,?,?,0,0,?,?)",
                (run, task["id"], max_calls, "running", now()),
            )
        calls, reused, error, pause = 0, 0, None, None
        while True:
            self.refresh(task)
            hits = self.reuse(task)
            reused += hits
            if hits:
                continue
            coverage = self.coverage(task)
            if coverage["status"] == "candidate_complete":
                break
            pending = self.pending(task)
            if not pending:
                pause = "depth_limit"
                break
            if max_calls is not None and calls >= max_calls:
                pause = "call_limit"
                break
            from .book_learning import relation_batches

            briefs = [self.library.brief(p) for p in self.points.values()]
            positions = {p["key"]: i for i, p in enumerate(briefs)}
            pairs = [(positions[m["left_key"]], positions[m["right_key"]]) for m in pending]
            payload = relation_batches(briefs, pairs)[0]
            keys = {pair_key(*p) for p in payload["candidate_pairs"]}
            members = [m for m in pending if m["pair_key"] in keys]
            payload["pairs"] = [
                dict(pair_id=m["pair_key"], points=[m["left_key"], m["right_key"]]) for m in members
            ]
            # Include explicit pair IDs in the same approximate 12,000-character budget.
            # An oversized single pair remains a standalone request, as in the old planner.
            while len(members) > 1 and len(dumps(payload)) > 12000:
                members.pop()
                payload["pairs"] = [
                    dict(pair_id=m["pair_key"], points=[m["left_key"], m["right_key"]])
                    for m in members
                ]
                payload["candidate_pairs"] = [p["points"] for p in payload["pairs"]]
                endpoints = {k for pair in payload["candidate_pairs"] for k in pair}
                payload["points"] = [p for p in payload["points"] if p["key"] in endpoints]
            keys = {m["pair_key"] for m in members}
            # Recover a deferred correction only if the original batch still matches.
            failures = self.store.rows(
                "SELECT * FROM relation_requests WHERE task_id=? AND status='failed' "
                "ORDER BY created_at DESC LIMIT 1",
                (task["id"],),
            )
            correction = None
            if failures:
                failed = failures[0]
                old_keys = {m["pair_key"] for m in json.loads(failed["members"])}
                if old_keys == keys and failed["repairable"]:
                    payload.update(
                        previous=json.loads(failed["candidate"]), errors=[failed["error"]]
                    )
                    correction = failed["id"]
            for attempt in range(2):
                if max_calls is not None and calls >= max_calls:
                    pause = "call_limit"
                    break
                calls += 1
                self.emit(
                    f"{section or '全书'} 关系检查：{coverage['checked']}/{coverage['total']} 对；"
                    f"本轮第 {calls}/{max_calls if max_calls is not None else '不限'} 次调用，"
                    f"{len(members)} 对候选。"
                )
                request_id, candidate, error, repairable = self.request(
                    task, run, members, payload, correction
                )
                if not error:
                    break
                if not repairable or attempt or correction:
                    pause = "error"
                    break
                correction = request_id
                payload.update(previous=candidate, errors=[error])
            if pause:
                break
        self.refresh(task)
        coverage = self.coverage(task)
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE relation_tasks SET status=?,pause_reason=? WHERE id=?",
                (coverage["status"], pause, task["id"]),
            )
            conn.execute(
                "UPDATE relation_runs SET status=?,reused_pairs=? WHERE id=?",
                (pause or "complete", reused, run),
            )
            if not section and coverage["status"] == "candidate_complete":
                conn.execute(
                    "UPDATE book_indexes SET status='ready' WHERE id=?", (self.index["id"],)
                )
        coverage.update(calls_used=calls, reused_pairs=reused, pause_reason=pause)
        self.emit(
            f"关系覆盖：{coverage['checked']}/{coverage['total']} 对；有关系 "
            f"{coverage['with_relations']}，未建立关系 {coverage['without_relations']}；"
            f"未展开前置 {coverage['unexpanded']}；复用 {reused} 对。"
        )
        if pause == "error":
            raise RelationFailure(f"关系分析暂停，已保存进度：{error}")
        return coverage
