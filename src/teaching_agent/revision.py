"""Deterministic ID-based draft patch merging, without implicit cascade deletion."""

from pydantic import ValidationError

from .models import AppError, Draft, DraftPatch


def _check_operations(label, existing, replacements, deletions):
    if len(replacements) != len(set(replacements)) or len(deletions) != len(set(deletions)):
        raise AppError(f"{label} 修改包存在重复 ID。")
    if set(replacements) & set(deletions):
        raise AppError(f"{label} 不能同时替换和删除。")
    if set(deletions) - set(existing):
        raise AppError(f"{label} 删除了不存在的 ID。")


def merge_patch(draft: Draft, patch: DraftPatch) -> Draft:
    """Construct and validate a fresh draft; never mutate the input or infer repairs."""
    value = draft.model_dump()
    for label, keys in (
        ("知识点", [p.key for p in draft.points]),
        ("目标", [o.key for p in draft.points for o in p.objectives]),
        ("内容清单", [i.key for i in draft.content_items]),
        ("误解条目", [m.code for m in draft.misconceptions]),
    ):
        if len(keys) != len(set(keys)):
            raise AppError(f"原草稿{label} ID 重复，无法安全定位修改，请人工编辑修复 ID。")
    points = {p["key"]: p for p in value["points"]}
    objectives = draft.objectives()
    _check_operations("知识点", points, [p.key for p in patch.points], patch.delete_points)
    _check_operations(
        "目标", objectives, [o.objective.key for o in patch.objectives], patch.delete_objectives
    )
    for name, key in (("content_items", "key"), ("misconceptions", "code")):
        original = {item[key]: item for item in value[name]}
        updates = getattr(patch, name)
        deleted = getattr(patch, f"delete_{name}")
        _check_operations(name, original, [getattr(item, key) for item in updates], deleted)
        for identifier in deleted:
            del original[identifier]
        for item in updates:
            original[getattr(item, key)] = item.model_dump()
        value[name] = list(original.values())
    for change in patch.points:
        if change.key in points:
            points[change.key]["title"] = change.title
        else:
            points[change.key] = {"key": change.key, "title": change.title, "objectives": []}
    updates = {change.objective.key: change for change in patch.objectives}
    for change in updates.values():
        if change.point_key not in points or change.point_key in patch.delete_points:
            raise AppError("目标的所属知识点不存在或将被删除。")
    placed = set()
    for point in points.values():
        kept = []
        for obj in point["objectives"]:
            if obj["key"] in patch.delete_objectives:
                continue
            change = updates.get(obj["key"])
            if change:
                if change.point_key != point["key"]:
                    continue
                obj = change.objective.model_dump()
                placed.add(obj["key"])
            kept.append(obj)
        point["objectives"] = kept
    for key, change in updates.items():
        if key not in placed:
            points[change.point_key]["objectives"].append(change.objective.model_dump())
    for key in patch.delete_points:
        if points[key]["objectives"]:
            raise AppError("删除知识点前必须显式删除或移动其全部目标，不能隐式连带删除。")
        del points[key]
    value["points"] = list(points.values())
    if patch.notes is not None:
        value["notes"] = list(patch.notes)
    try:
        candidate = Draft.model_validate(value)
    except ValidationError as exc:
        fields = [".".join(map(str, e["loc"])) for e in exc.errors()[:6]]
        raise AppError("修改包合并后结构无效：" + "、".join(fields)) from exc
    if candidate == draft:
        raise AppError("修改包未产生有效修改。")
    return candidate


def change_summary(before: Draft, after: Draft):
    result = {}
    for name, old, new in (
        ("objectives", before.objectives(), after.objectives()),
        ("points", {p.key: p.title for p in before.points}, {p.key: p.title for p in after.points}),
        (
            "content_items",
            {i.key: i for i in before.content_items},
            {i.key: i for i in after.content_items},
        ),
        (
            "misconceptions",
            {m.code: m for m in before.misconceptions},
            {m.code: m for m in after.misconceptions},
        ),
    ):
        result[name] = {
            "added": len(new.keys() - old.keys()),
            "removed": len(old.keys() - new.keys()),
            "updated": sum(old[k] != new[k] for k in old.keys() & new.keys()),
        }
    old_parent = {o.key: p.key for p in before.points for o in p.objectives}
    result["moved_objectives"] = sum(
        o.key in old_parent and old_parent[o.key] != p.key
        for p in after.points
        for o in p.objectives
    )
    result["notes_changed"] = before.notes != after.notes
    return result
