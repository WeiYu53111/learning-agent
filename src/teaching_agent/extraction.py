"""Source-first, checkpointed semantic extraction with bounded quality review.

Original chunks are never rewritten. All region coordinates are offsets into
those chunks, so exclusions and regenerated knowledge remain auditable.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .book_models import KnowledgePoint, Navigation
from .extraction_models import PIPELINE, BoundaryReview, ExtractionReview, SourceAnalysis
from .knowledge import check_refs
from .models import AppError
from .storage import dumps


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()[:16]


def is_current(row):
    return bool(row and json.loads(row["data"]).get("pipeline") == PIPELINE)


def source_units(chunks):
    """Number source lines; offsets include intervening whitespace without loss."""
    units = []
    for chunk in chunks:
        offset = 0
        for line in chunk.text.splitlines(keepends=True):
            # A pathological unbroken paragraph cannot exceed the request budget.
            for start in range(0, len(line), 800):
                text = line[start : start + 800]
                units.append(
                    dict(
                        id=len(units),
                        chunk_id=chunk.id,
                        page=chunk.page,
                        start=offset + start,
                        end=offset + start + len(text),
                        text=text,
                    )
                )
            offset += len(line)
    return units


def analysis_errors(value, units):
    expected = {u["id"] for u in units}
    seen = set()
    errors = []
    for region in value.regions:
        if region.last < region.first or region.last - region.first > len(units):
            errors.append("区域起止编号无效。")
            continue
        ids = set(range(region.first, region.last + 1))
        if ids - expected or ids & seen:
            errors.append("区域引用未知行或与其他区域重叠。")
        seen |= ids
    if seen != expected:
        errors.append("必须完整且唯一分类每个输入行，不能丢弃内容。")
    return errors


def materialize(analysis, units, section_id):
    by_id = {u["id"]: u for u in units}
    result = []
    for region in analysis.regions:
        selected = [by_id[i] for i in range(region.first, region.last + 1)]
        spans = []
        for u in selected:
            if spans and spans[-1]["chunk_id"] == u["chunk_id"] and spans[-1]["end"] == u["start"]:
                spans[-1]["end"] = u["end"]
            else:
                spans.append({k: u[k] for k in ("chunk_id", "start", "end")})
        for span in spans:
            result.append(
                {
                    **span,
                    "section_id": section_id,
                    "role": region.role,
                    "path": region.path,
                    "topic": region.topic,
                    "reason": region.reason,
                    "excluded": region.role in {"navigation", "publication"},
                }
            )
    return result


def region_text(region, chunk_map):
    return chunk_map[region["chunk_id"]].text[region["start"] : region["end"]]


def navigation_errors(nav, chunks, regions):
    chunk_map = {c.id: c for c in chunks}
    allowed = defaultdict(list)
    for r in regions:
        allowed[r["chunk_id"]].append((r["start"], r["end"]))
    keys = [p.key for p in nav.points]
    errors = []
    if len(set(keys)) != len(keys):
        errors.append("知识点 key 重复。")
    identities = [
        (p.title, tuple(sorted((r.chunk_id, r.quote) for r in p.source_refs))) for p in nav.points
    ]
    if len(identities) != len(set(identities)):
        errors.append("相同标题和证据的知识点重复。")
    coverage = defaultdict(list)
    mapped = set()
    for obj in [*nav.points, *nav.items]:
        errors.extend(check_refs(obj.source_refs, chunk_map))
        for ref in obj.source_refs:
            chunk = chunk_map.get(ref.chunk_id)
            if not chunk or chunk.text.count(ref.quote) != 1:
                continue
            start = chunk.text.index(ref.quote)
            end = start + len(ref.quote)
            # Multiple adjacent teaching regions may support one quote.
            permitted = [False] * (end - start)
            for left, right in allowed[ref.chunk_id]:
                for i in range(max(left, start), min(right, end)):
                    permitted[i - start] = True
            if any(
                not ok and not chunk.text[start + i].isspace() for i, ok in enumerate(permitted)
            ):
                errors.append("引用超出本批正文区域，包含排除内容或参考上下文。")
        if hasattr(obj, "point_keys"):
            mapped.update(obj.point_keys)
            if set(obj.point_keys) - set(keys):
                errors.append("内容清单引用未知知识点。")
            for ref in obj.source_refs:
                chunk = chunk_map.get(ref.chunk_id)
                if chunk and chunk.text.count(ref.quote) == 1:
                    left = chunk.text.index(ref.quote)
                    coverage[ref.chunk_id].append((left, left + len(ref.quote)))
    if set(keys) - mapped:
        errors.append("知识点没有关联内容清单。")
    for r in regions:
        text = chunk_map[r["chunk_id"]].text
        if any(
            not text[i].isspace() and not any(a <= i < b for a, b in coverage[r["chunk_id"]])
            for i in range(r["start"], r["end"])
        ):
            errors.append(f"教学区域没有完整内容证据：{r['chunk_id']}:{r['start']}:{r['end']}")
    return list(dict.fromkeys(errors))


def preserved_items_errors(before, after):
    """Review may reorganize points, but cannot silently erase extracted evidence."""
    old = {(r.chunk_id, r.quote) for i in before.items for r in i.source_refs}
    new = {(r.chunk_id, r.quote) for i in after.items for r in i.source_refs}
    return [] if old <= new else ["修订必须保留已有内容清单的全部证据，可新增但不能删除。"]


def lesson_chunks(chunks, data, section_id):
    regions = [
        r for r in data.get("regions", []) if r["section_id"] == section_id and not r["excluded"]
    ]
    ids = {r["chunk_id"] for r in regions}
    return [c for c in chunks if c.id in ids]


def teaching_errors(structure, chunks, regions):
    """Prevent excluded text from becoming objectives during lesson generation."""
    chunk_map = {c.id: c for c in chunks}
    errors = []
    for obj in structure.objectives().values():
        if obj.excluded_reason:
            continue
        for ref in obj.source_refs:
            chunk = chunk_map.get(ref.chunk_id)
            if chunk is None or chunk.text.count(ref.quote) != 1:
                continue  # The existing exact-reference validator reports this.
            start = chunk.text.index(ref.quote)
            end = start + len(ref.quote)
            allowed = [r for r in regions if r["chunk_id"] == ref.chunk_id and not r["excluded"]]
            if any(
                not chunk.text[i].isspace() and not any(r["start"] <= i < r["end"] for r in allowed)
                for i in range(start, end)
            ):
                errors.append("学习目标引用了已排除的目录或出版内容。")
    return list(dict.fromkeys(errors))


class SemanticExtractor:
    def __init__(self, library, book, owner, emit):
        self.library, self.book, self.owner, self.emit = library, book, owner, emit
        gateway = library.app.gateway(book, owner)
        config = getattr(gateway, "spec", None) or getattr(gateway, "settings", None)
        maximum = getattr(config, "input_chars", 48000)
        # Leave room for schema, review output and a repair request.
        self.budget = min(10000, max(2000, (maximum - 12000) // 5))
        if maximum < 16000:
            raise AppError("新提取流程需要 input_chars 至少 16000；请提高后端输入预算。")

    def call(self, key, op, payload, contract, validate):
        return self.library.call(
            self.book, self.owner, key, op, payload, contract, validate, self.emit
        )

    def section(self, section):
        from .book_learning import groups

        prefix = f"{section.id}:{PIPELINE}"
        saved = self.library.step(self.owner, prefix + ":complete")
        if saved is not None:
            self.emit(f"{section.title}：复用已审核章节")
            return saved
        chunks = self.library.app.section_chunks(self.book, section.id)
        chunk_map = {c.id: c for c in chunks}
        units = source_units(chunks)
        bookmarks = [
            b.model_dump()
            for b in self.book.bookmarks
            if section.start_page <= b.start_page <= section.end_page
        ]
        regions = []
        for i, batch in enumerate(groups(units, min(9000, self.budget))):
            local_bookmarks = [
                b for b in bookmarks if batch[0]["page"] <= b["start_page"] <= batch[-1]["page"]
            ]
            payload = {
                "section": section.title,
                "bookmarks": local_bookmarks,
                "units": batch,
                "preceding_structure": [
                    {k: r[k] for k in ("path", "topic", "role")} for r in regions[-2:]
                ],
            }
            analysis = self.call(
                prefix + f":source:{i}",
                "source_analysis",
                payload,
                SourceAnalysis,
                lambda v, batch=batch: analysis_errors(v, batch),
            )
            regions.extend(materialize(analysis, batch, section.id))
        regions.sort(
            key=lambda r: (
                next(i for i, c in enumerate(chunks) if c.id == r["chunk_id"]),
                r["start"],
            )
        )
        self.library.save_step(self.owner, prefix + ":regions", {"regions": regions})
        eligible = [r for r in regions if not r["excluded"] and region_text(r, chunk_map).strip()]
        plan = self.library.step(self.owner, prefix + ":plan")
        if plan is None:
            batches, current = [], []
            for region in eligible:
                trial = current + [region]
                ids = {r["chunk_id"] for r in trial}
                size = len(dumps([chunk_map[k].model_dump() for k in ids])) + len(dumps(trial))
                changed = current and (current[-1]["path"], current[-1]["topic"]) != (
                    region["path"],
                    region["topic"],
                )
                if current and (size > min(self.budget, 10000) or changed):
                    batches.append(current)
                    current = []
                current.append(region)
            if current:
                batches.append(current)
            plan = {"batches": batches}
            self.library.save_step(self.owner, prefix + ":plan", plan)
        points, items = [], []
        batch_of = {}
        for i, batch in enumerate(plan["batches"]):
            ids = {r["chunk_id"] for r in batch}
            selected = [c for c in chunks if c.id in ids]
            first, last = eligible.index(batch[0]), eligible.index(batch[-1])
            neighbors = eligible[max(0, first - 1) : first] + eligible[last + 1 : last + 2]
            context = [
                {"text": region_text(r, chunk_map)[:1600], "path": r["path"]} for r in neighbors
            ]
            payload = {
                "section": section.title,
                "path": batch[0]["path"],
                "topic": batch[0]["topic"],
                "regions": batch,
                "chunks": [c.model_dump() for c in selected],
                "reference_context": context,
            }

            def validate(v, selected=selected, batch=batch):
                return navigation_errors(v, selected, batch)

            nav = self.call(prefix + f":extract:{i}", "book_index", payload, Navigation, validate)
            nav = self.review(prefix + f":quality:{i}", payload, nav, validate)
            mapping = {p.key: f"b{i}_{p.key}" for p in nav.points}
            for point in nav.points:
                points.append(point.model_copy(update={"key": mapping[point.key]}))
                batch_of[mapping[point.key]] = i
            for j, item in enumerate(nav.items):
                items.append(
                    item.model_copy(
                        update={
                            "key": f"{section.id}_b{i}_i{j}",
                            "point_keys": [mapping[k] for k in item.point_keys],
                        }
                    )
                )
        nav = Navigation(points=points, items=items)
        nav = self.boundaries(prefix, section, nav, batch_of, chunks, eligible)
        mapping = {
            p.key: "k_"
            + digest(
                [PIPELINE, self.owner, section.id, p.title, [r.model_dump() for r in p.source_refs]]
            )
            for p in nav.points
        }
        result = {
            "points": [
                {**p.model_dump(), "key": mapping[p.key], "section_id": section.id}
                for p in nav.points
            ],
            "items": [
                {**it.model_dump(), "point_keys": [mapping[k] for k in it.point_keys]}
                for it in nav.items
            ],
            "regions": regions,
        }
        self.library.save_step(self.owner, prefix + ":complete", result)
        return result

    def review(self, prefix, payload, nav, validate):
        def review_errors(result):
            keys = {p.key for p in nav.points}
            return (
                ["审核引用未知知识点。"]
                if any(set(i.point_keys) - keys for i in result.issues)
                else []
            )

        report = self.call(
            prefix + ":audit",
            "extraction_review",
            {**payload, "navigation": nav.model_dump()},
            ExtractionReview,
            review_errors,
        )
        if not report.issues:
            return nav
        corrected = self.call(
            prefix + ":repair",
            "book_index",
            {
                **payload,
                "previous": nav.model_dump(),
                "errors": [i.model_dump() for i in report.issues],
            },
            Navigation,
            lambda v: validate(v) + preserved_items_errors(nav, v),
        )
        final = self.call(
            prefix + ":verify",
            "extraction_review",
            {**payload, "navigation": corrected.model_dump()},
            ExtractionReview,
            lambda _: [],
        )
        if final.issues:
            self.library.event(
                self.owner,
                "book_operation_failed",
                {
                    "step": prefix + ":verify",
                    "operation": "extraction_review",
                    "errors": [i.description for i in final.issues],
                },
            )
            raise AppError(
                "提取修订后审核仍未通过；检查点已保存，请查看问题后使用 --new 重建："
                + "；".join(i.description for i in final.issues)
            )
        return corrected

    def boundaries(self, prefix, section, nav, batch_of, chunks, regions):
        from .book_learning import tokens
        from .relation_planner import candidate_pairs, relation_batches

        if len(set(batch_of.values())) < 2:
            return nav
        briefs = [
            {"key": p.key, "title": p.title, "description": p.description, "section_id": section.id}
            for p in nav.points
        ]
        pairs = [
            (i, j)
            for i, j in candidate_pairs(briefs, tokens)
            if batch_of[briefs[i]["key"]] != batch_of[briefs[j]["key"]]
        ]
        original = {p.key: p for p in nav.points}
        parent = {k: k for k in original}

        def root(k):
            while parent[k] != k:
                k = parent[k]
            return k

        titles = {}
        evidence = [{**p.model_dump(), "section_id": section.id} for p in nav.points]
        for i, request in enumerate(relation_batches(evidence, pairs, budget=self.budget)):
            allowed = {frozenset(pair) for pair in request["candidate_pairs"]}

            def validate(value, allowed=allowed):
                errors = []
                seen = set()
                for group in value.merges:
                    if len(group.members) != 2 or frozenset(group.members) not in allowed:
                        errors.append("只允许合并 candidate_pairs 中的两个知识点。")
                    if seen.intersection(group.members):
                        errors.append("同批合并不能重复使用知识点。")
                    seen.update(group.members)
                return errors

            review = self.call(
                prefix + f":boundary:{i}", "boundary_review", request, BoundaryReview, validate
            )
            for merge in review.merges:
                a, b = map(root, merge.members)
                if a == b:
                    continue
                parent[b] = a
                titles[a] = (merge.title, merge.description)
        grouped = defaultdict(list)
        for key in original:
            grouped[root(key)].append(original[key])
        merged = []
        for key, members in grouped.items():
            if len(members) == 1:
                merged.extend(members)
                continue
            refs = {(r.chunk_id, r.quote): r for p in members for r in p.source_refs}
            title, description = titles[key]
            merged.append(
                KnowledgePoint(
                    key=key, title=title, description=description, source_refs=list(refs.values())
                )
            )
        result = Navigation(
            points=merged,
            items=[
                it.model_copy(
                    update={
                        "point_keys": list(dict.fromkeys(root(k) for k in it.point_keys)),
                    }
                )
                for it in nav.items
            ],
        )
        errors = navigation_errors(result, chunks, regions) + preserved_items_errors(nav, result)
        if errors:
            raise AppError("跨批归并校验失败：" + "；".join(errors))
        # Every changed group is reviewed against its actual original evidence.
        for i, point in enumerate(merged):
            if len(grouped[point.key]) < 2:
                continue
            selected_items = [it for it in result.items if point.key in it.point_keys]
            selected_keys = {k for it in selected_items for k in it.point_keys}
            selected_points = [p for p in merged if p.key in selected_keys]
            report = self.call(
                prefix + f":boundary-verify:{i}",
                "extraction_review",
                {
                    "section": section.title,
                    "navigation": Navigation(
                        points=selected_points, items=selected_items
                    ).model_dump(),
                    "evidence_only": True,
                },
                ExtractionReview,
                lambda _: [],
            )
            if report.issues:
                self.library.event(
                    self.owner,
                    "book_operation_failed",
                    {
                        "step": prefix + f":boundary-verify:{i}",
                        "operation": "extraction_review",
                        "errors": [issue.description for issue in report.issues],
                    },
                )
                raise AppError(
                    "跨批归并审核未通过，未发布索引："
                    + "；".join(issue.description for issue in report.issues)
                )
        return result
