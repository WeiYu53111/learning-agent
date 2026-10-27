"""Offline acceptance tests for pair checkpoints and bounded chapter analysis."""

import copy
import json

import pytest
from test_book_learning import book_id as book_id

from teaching_agent import book_learning as learning
from teaching_agent.cli import main, prerequisite_choices
from teaching_agent.models import AppError, ModelResponseError
from teaching_agent.relation_planner import relation_batches
from teaching_agent.relations import RelationAnalysis, RelationsPaused


def content(app, book_id):
    return app.library.index(book_id, content_only=True)


def batch_size(monkeypatch, size):
    monkeypatch.setattr(
        learning,
        "relation_batches",
        lambda points, pairs: relation_batches(points, pairs, pair_limit=size),
    )


def spy(app, responder=None):
    calls = []
    original = app.injected_gateway.book_operation

    def operation(op, payload, contract):
        if op == "book_relations":
            calls.append(copy.deepcopy(payload))
            if responder:
                return responder(payload, contract)
        return original(op, payload, contract)

    app.injected_gateway.book_operation = operation
    return calls


def test_content_only_publishes_no_relations_and_outline_is_read_only(app, book_id, capsys):
    calls = spy(app)
    index = content(app, book_id)
    assert not calls and app.library.active(book_id) is None
    assert app.library.content_active(book_id)["id"] == index["id"]
    before = app.store.path.read_bytes()
    assert main(["--data-dir", str(app.store.root), "outline", book_id]) == 0
    assert before == app.store.path.read_bytes()
    assert "内容就绪" in capsys.readouterr().out
    assert app.store.rows("SELECT * FROM relation_tasks") == []


def test_budget_resume_rebatches_only_unchecked_pairs(app, book_id, monkeypatch):
    content(app, book_id)
    calls = spy(app)
    batch_size(monkeypatch, 1)
    first = app.library.relations(book_id, max_calls=1)
    assert first["checked"] == 1 and first["status"] == "partial"
    completed = {p["pair_id"] for p in calls[0]["pairs"]}
    batch_size(monkeypatch, 64)
    second = app.library.relations(book_id, max_calls=1)
    assert second["status"] == "candidate_complete"
    assert len(calls) == 2
    assert not completed & {p["pair_id"] for p in calls[1]["pairs"]}
    assert second["without_relations"] == second["total"]
    assert app.library.relations(book_id, max_calls=0)["calls_used"] == 0


@pytest.mark.parametrize("invalid", ["missing", "duplicate", "extra", "self", "outside", "legacy"])
def test_invalid_response_atomic_and_correction_consumes_budget(app, book_id, invalid):
    content(app, book_id)

    def response(payload, contract):
        checks = [dict(pair_id=p["pair_id"], relations=[]) for p in payload["pairs"]]
        if invalid == "missing":
            checks.pop()
        elif invalid == "duplicate":
            checks.append(checks[0])
        elif invalid == "extra":
            checks.append(dict(pair_id="unknown", relations=[]))
        elif invalid in ("self", "outside"):
            a, b = payload["pairs"][0]["points"]
            checks[0]["relations"] = [
                dict(
                    source=a,
                    target=a if invalid == "self" else "unknown",
                    kind="related",
                    reason="bad",
                )
            ]
        else:
            return contract.model_validate({"relations": []})
        return contract.model_validate({"checks": checks})

    calls = spy(app, response)
    paused = app.library.relations(book_id, max_calls=1)
    assert paused["calls_used"] == 1 and paused["checked"] == 0
    assert len(calls) == 1 and not app.store.rows("SELECT * FROM relation_checks")
    with pytest.raises(AppError, match="暂停"):
        app.library.relations(book_id, max_calls=1)
    assert len(calls) == 2
    assert not app.store.rows("SELECT * FROM relation_checks")
    assert app.library.content_active(book_id)


def test_schema_transport_repair_is_budgeted(app, book_id):
    content(app, book_id)
    calls = spy(app, lambda *_: (_ for _ in ()).throw(ModelResponseError("坏 JSON")))
    with pytest.raises(AppError, match="暂停"):
        app.library.relations(book_id, max_calls=2)
    assert len(calls) == 2
    assert len(app.store.rows("SELECT * FROM relation_requests")) == 2


def test_network_failure_is_not_a_negative_check(app, book_id):
    content(app, book_id)
    calls = spy(app, lambda *_: (_ for _ in ()).throw(AppError("网络失败")))
    with pytest.raises(AppError, match="网络失败"):
        app.library.relations(book_id, max_calls=4)
    assert len(calls) == 1
    assert not app.store.rows("SELECT * FROM relation_checks")


def test_local_candidates_include_reverse_selection_and_exclude_unrelated(
    app, book_id, monkeypatch
):
    content(app, book_id)
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(1, 0), (1, 2)])
    calls = spy(app)
    result = app.library.relations(book_id, "s2")
    assert result["total"] == 1 and result["status"] == "candidate_complete"
    assert {p["section_id"] for p in calls[0]["points"]} == {"s1", "s2"}
    assert not app.library.active(book_id)  # Local coverage never implies global coverage.


def chain_response(payload, contract, kind="prerequisite"):
    sections = {p["key"]: p["section_id"] for p in payload["points"]}
    checks = []
    for pair in payload["pairs"]:
        a, b = sorted(pair["points"], key=lambda k: sections[k])
        checks.append(
            dict(
                pair_id=pair["pair_id"],
                relations=[dict(source=a, target=b, kind=kind, reason="测试链")],
            )
        )
    return contract(checks=checks)


def test_depth_frontier_persists_and_increasing_depth_resumes(app, book_id, monkeypatch):
    content(app, book_id)
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 1), (1, 2)])
    calls = spy(app, chain_response)
    first = app.library.relations(book_id, "s3", max_depth=0)
    assert first["checked"] == 1 and first["unexpanded"] == 1
    assert first["frontier"][0]["depth"] == 1
    second = app.library.relations(book_id, "s3", max_depth=1)
    assert second["checked"] == 2 and second["unexpanded"] == 1
    assert second["frontier"][0]["depth"] == 2
    third = app.library.relations(book_id, "s3", max_depth=2, max_calls=0)
    assert third["status"] == "candidate_complete"
    assert len(calls) == 2
    assert first["task_id"] == second["task_id"] == third["task_id"]


def test_related_never_expands_predecessors(app, book_id, monkeypatch):
    content(app, book_id)
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 1), (1, 2)])
    calls = spy(app, lambda p, c: chain_response(p, c, "related"))
    result = app.library.relations(book_id, "s3")
    assert result["total"] == 1 and result["unexpanded"] == 0
    assert len(calls) == 1


def test_cached_checks_expand_even_with_zero_call_budget(app, book_id, monkeypatch):
    content(app, book_id)
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 1), (1, 2)])
    calls = spy(app, chain_response)
    app.library.relations(book_id)
    count = len(calls)
    result = app.library.relations(book_id, "s3", max_calls=0)
    assert result["status"] == "candidate_complete" and result["reused_pairs"] == 2
    assert len(calls) == count


def test_noninteractive_partial_requires_explicit_acceptance(app, book_id, monkeypatch):
    content(app, book_id)
    original = app.library.relations
    monkeypatch.setattr(app.library, "relations", lambda *a, **kw: original(*a, **kw, max_calls=0))
    with pytest.raises(RelationsPaused):
        app.library.learn(book_id, "s2")
    assert not app.store.rows("SELECT * FROM versions")
    version = app.library.learn(book_id, "s2", allow_partial_relations=True)
    snapshot = json.loads(
        app.store.rows("SELECT data FROM lesson_relation_snapshots WHERE version_id=?", (version,))[
            0
        ]["data"]
    )
    assert snapshot["allow_partial"] is True and snapshot["coverage"]["status"] == "partial"
    assert not app.store.rows("SELECT * FROM self_mastery")


def test_failure_partial_acceptance_records_error(app, book_id):
    content(app, book_id)
    spy(app, lambda *_: (_ for _ in ()).throw(AppError("网络失败")))
    version = app.library.learn(book_id, "s2", allow_partial_relations=True)
    snapshot = json.loads(app.store.rows("SELECT data FROM lesson_relation_snapshots")[0]["data"])
    assert "网络失败" in snapshot["coverage"]["last_error"]
    assert app.version(version)[0]["data"] != "{}"


def test_new_relations_do_not_change_old_lesson_or_prerequisite_choices(app, book_id, monkeypatch):
    content(app, book_id)
    original = app.library.relations
    monkeypatch.setattr(app.library, "relations", lambda *a, **kw: original(*a, **kw, max_calls=0))
    version = app.library.learn(book_id, "s3", allow_partial_relations=True)
    before = app.version(version)
    monkeypatch.setattr(app.library, "relations", original)
    spy(app, chain_response)
    app.library.relations(book_id)
    assert app.library.prerequisites(book_id, "s3")
    assert prerequisite_choices(app, version) == []
    assert app.version(version) == before
    assert app.library.learn(book_id, "s3") == version
    newer = app.library.learn(book_id, "s3", new=True)
    assert app.library.prerequisites(book_id, "s3", app.library.lesson_index(newer))
    clone = app.clone(version)
    assert prerequisite_choices(app, clone) == []


def test_new_content_invalidates_only_pairs_touching_changed_point(app, book_id):
    old = app.library.index(book_id)
    calls = spy(app)
    # Generate a new content version with one description changed in extraction.
    original = app.injected_gateway.book_operation

    def changed(op, payload, contract):
        value = original(op, payload, contract)
        if op == "book_index" and payload["chunks"][0]["id"] == "c1":
            value.points[0].description += "补充内容"
        return value

    app.injected_gateway.book_operation = changed
    new = app.library.index(book_id, new=True, section_id="s1", content_only=True)
    assert new["id"] != old["id"]
    changed_key = json.loads(new["data"])["points"][0]["key"]
    result = app.library.relations(book_id)
    assert result["reused_pairs"] == 1
    assert all(changed_key in pair["points"] for call in calls for pair in call["pairs"])


def test_existing_lesson_restores_original_content_after_new_index(app, book_id):
    app.library.index(book_id)
    version = app.library.learn(book_id, "s1")
    old_index = app.library.lesson_index(version)["id"]
    app.library.index(book_id, new=True, content_only=True)
    calls = spy(app)
    assert app.library.learn(book_id, "s1") == version
    assert app.library.lesson_index(version)["id"] == old_index
    assert calls == []


def test_v2_success_empty_and_positive_migrate_but_rejected_does_not(app, book_id):
    index = content(app, book_id)
    points = json.loads(index["data"])["points"]
    briefs = [app.library.brief(p) for p in points]
    batches = relation_batches(briefs, [(0, 1), (1, 2), (0, 2)], pair_limit=1)
    app.library.save_step(index["id"], "relations_v2:plan", dict(batches=batches, legacy=[]))
    edge = dict(source=points[0]["key"], target=points[1]["key"], kind="related", reason="旧结果")
    app.library.save_step(index["id"], "relations_v2:batch:0", {"relations": [edge]})
    app.library.save_step(index["id"], "relations_v2:batch:1", {"relations": []})
    app.library.save_step(index["id"], "relations_v2:batch:2:rejected", {"relations": []})
    calls = spy(app)
    result = app.library.relations(book_id)
    assert result["reused_pairs"] == 2
    assert len(calls) == 1 and len(calls[0]["pairs"]) == 1
    before = app.store.rows("SELECT * FROM relation_checks")
    app.library.relations(book_id)
    assert app.store.rows("SELECT * FROM relation_checks") == before


def test_corrupt_legacy_plan_cannot_be_accepted_as_partial(app, book_id):
    index = content(app, book_id)
    app.library.save_step(index["id"], "relations_v2:plan", dict(batches=[], legacy=[]))
    app.library.save_step(index["id"], "relations_v2:batch:3", {"relations": []})
    with pytest.raises(AppError, match="超出计划"):
        app.library.learn(book_id, "s1", allow_partial_relations=True)
    assert not app.store.rows("SELECT * FROM versions")
    assert not app.store.rows("SELECT * FROM relation_legacy_imports")


@pytest.mark.parametrize("after_commit", [False, True])
def test_interruption_around_result_commit_recovers(app, book_id, monkeypatch, after_commit):
    content(app, book_id)
    calls = spy(app)
    original = RelationAnalysis.request
    original_graph = RelationAnalysis.save_graph
    interrupted = False

    def request(self, *args, **kw):
        nonlocal interrupted
        result = original(self, *args, **kw)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return result

    def save_graph(self, conn, accepted):
        nonlocal interrupted
        original_graph(self, conn, accepted)
        if any(check_id for check_id, _ in accepted) and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        RelationAnalysis,
        "request" if after_commit else "save_graph",
        request if after_commit else save_graph,
    )
    with pytest.raises(KeyboardInterrupt):
        app.library.relations(book_id)
    assert len(app.store.rows("SELECT * FROM relation_checks")) == (3 if after_commit else 0)
    app.library.relations(book_id)
    assert len(calls) == (1 if after_commit else 2)
    assert len(app.store.rows("SELECT * FROM relation_checks")) == 3


def test_delete_cascades_all_relation_data_and_lesson_snapshots(app, book_id):
    app.library.index(book_id)
    app.library.learn(book_id, "s1")
    app.delete_book(book_id, cascade=True)
    for table in (
        "index_content_state",
        "relation_checks",
        "relation_tasks",
        "relation_task_pairs",
        "relation_runs",
        "relation_requests",
        "relation_graph_edges",
        "relation_legacy_imports",
        "lesson_relation_snapshots",
    ):
        assert not app.store.rows(f"SELECT * FROM {table}")
    assert not app.store.rows("PRAGMA foreign_key_check")


def test_cli_pause_exit_code_and_negative_budget(app, book_id):
    content(app, book_id)
    common = ["--data-dir", str(app.store.root), "relations", book_id]
    assert main([*common, "--max-calls", "0"]) == 3
    assert main([*common, "--max-calls", "-1"]) == 1
    assert main([*common, "--max-calls", "1"]) == 0


def test_new_graph_rejects_conflicting_cache_without_mutating_it(app, book_id, monkeypatch):
    old = app.library.index(book_id)
    data = json.loads(old["data"])
    a, b, c = [p["key"] for p in data["points"]]
    engine = RelationAnalysis(app.library, old)
    # A judgment from another graph would close the new A -> B -> C chain.
    with app.store.connect() as conn:
        check_id = engine.insert_check(
            conn,
            engine.member(a, c),
            [dict(source=c, target=a, kind="prerequisite", reason="旧图判断")],
            "explicit",
            "test",
        )
    original_check = app.store.rows("SELECT * FROM relation_checks WHERE id=?", (check_id,))[0]
    new = app.library.index(book_id, new=True, section_id="s2", content_only=True)
    a, b, c = [p["key"] for p in json.loads(new["data"])["points"]]
    engine = RelationAnalysis(app.library, new)
    engine.import_legacy()
    chain = [
        dict(source=x, target=y, kind="prerequisite", reason="新图") for x, y in [(a, b), (b, c)]
    ]
    with app.store.connect() as conn:
        engine.save_graph(conn, [(None, chain)])
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 2)])
    # Remove the earlier demo negative for this pair so the conflict is the only cache entry.
    # This represents independent valid caches from different indexes, not mutable judgments.
    with app.store.connect() as conn:
        conn.execute("DELETE FROM relation_task_pairs")
        conn.execute(
            "DELETE FROM relation_checks WHERE pair_key=? AND id<>?",
            (original_check["pair_key"], check_id),
        )
    calls = spy(app)
    result = app.library.relations(book_id)
    assert result["calls_used"] == 1 and len(calls) == 1
    assert (
        app.store.rows("SELECT * FROM relation_checks WHERE id=?", (check_id,))[0] == original_check
    )
    assert not learning.relation_errors(
        json.loads(app.library.content_active(book_id)["data"])["relations"], {a, b, c}
    )


def test_selector_change_reuses_checks_and_legacy_group_evidence(app, book_id, monkeypatch):
    import teaching_agent.relations as module

    index = content(app, book_id)
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 1)])
    app.library.save_step(index["id"], "relations:0:0", {"relations": []})
    calls = spy(app)
    first = app.library.relations(book_id)
    assert first["total"] == 1 and not calls
    monkeypatch.setattr(module, "SELECTOR_VERSION", "test-selector-v2")
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 1), (0, 2)])
    second = app.library.relations(book_id)
    assert first["task_id"] != second["task_id"]
    assert second["checked"] == 2 and second["reused_pairs"] == 2 and not calls
    assert len(app.store.rows("SELECT * FROM relation_checks")) == 2
    monkeypatch.setattr(module, "VALIDATOR_VERSION", "test-validator-v2")
    third = app.library.relations(book_id)
    assert third["calls_used"] == 1  # A new validator cannot reuse old implicit negatives.


def test_unreconstructable_legacy_groups_do_not_invent_negative_coverage(app, book_id):
    index = content(app, book_id)
    app.library.save_step(index["id"], "relations:99:99", {"relations": []})
    logs = []
    calls = spy(app)
    result = app.library.relations(book_id, emit=logs.append)
    assert result["reused_pairs"] == 0 and len(calls) == 1
    assert any("负结果需要重查" in line for line in logs)


def test_migration_freezes_historical_lesson_without_claiming_coverage(app, book_id):
    from teaching_agent.storage import now, uid

    book = app.book(book_id)
    owner, version = uid("index"), app.create_draft(book_id, "s1")
    data = dict(points=[], items=[], summaries={}, relations=[], unavailable_pages=[])
    with app.store.connect() as conn:
        conn.execute(
            "INSERT INTO book_indexes VALUES(?,?,?,?,?)",
            (owner, book.id, "ready", json.dumps(data), now()),
        )
        conn.execute("INSERT INTO lesson_links VALUES(?,?,?,?)", (version, owner, "s1", "{}"))
        conn.execute("PRAGMA user_version=5")
    app.store.initialize()
    snapshot = app.store.rows(
        "SELECT * FROM lesson_relation_snapshots WHERE version_id=?", (version,)
    )[0]
    assert json.loads(snapshot["data"])["coverage"]["status"] == "legacy_unknown"
    assert app.library.content_active(book_id) is None
    assert app.library.lesson_index(version)["id"] == owner
    app.store.initialize()
    assert app.store.rows(
        "SELECT * FROM lesson_relation_snapshots WHERE version_id=?", (version,)
    ) == [snapshot]


def test_shortest_prerequisite_path_reopens_node_at_allowed_depth(app, book_id):
    index = content(app, book_id)
    engine = RelationAnalysis(app.library, index)
    engine.import_legacy()
    a, b, c = engine.points
    task = engine.task("s3", 1)
    chain = [
        dict(source=x, target=y, kind="prerequisite", reason="test") for x, y in [(a, b), (b, c)]
    ]
    with app.store.connect() as conn:
        engine.save_graph(conn, [(None, chain)])
    assert engine.coverage(task)["unexpanded"] == 1
    with app.store.connect() as conn:
        engine.save_graph(
            conn, [(None, [dict(source=a, target=c, kind="prerequisite", reason="short")])]
        )
    assert engine.coverage(task)["unexpanded"] == 0


def test_graph_commit_updates_other_chapter_frontier_atomically(app, book_id, monkeypatch):
    index = content(app, book_id)
    monkeypatch.setattr(learning, "candidate_pairs", lambda *_: [(0, 1), (1, 2)])
    spy(app, chain_response)
    shallow = app.library.relations(book_id, "s3", max_depth=0)
    assert shallow["unexpanded"] == 1
    app.library.relations(book_id, "s2", max_depth=2)
    engine = RelationAnalysis(app.library, app.library.index_row(index["id"]))
    shallow_task = engine.task("s3", 0)
    assert engine.coverage(shallow_task)["unexpanded"] == 2


def test_http_relation_scheduler_has_no_hidden_transport_retries(monkeypatch):
    import httpx

    from teaching_agent.book_models import RelationChecks
    from teaching_agent.gateway import Gateway, Settings

    calls = []

    def post(*a, **kw):
        calls.append(kw)
        raise httpx.TransportError("offline")

    monkeypatch.setattr(httpx.Client, "post", post)
    gateway = Gateway(Settings(base="https://test.invalid/v1", key="fake", model="test"))
    with pytest.raises(AppError):
        gateway.book_operation("book_relations", {"pairs": []}, RelationChecks)
    assert len(calls) == 1


def test_lesson_snapshot_excludes_unrelated_graph_context(app, book_id):
    index = content(app, book_id)
    spy(app, chain_response)
    app.library.relations(book_id)
    # s1 has no prerequisites: other chapters' dependencies must not enter its snapshot.
    version = app.library.learn(book_id, "s1")
    snapshot = json.loads(
        app.store.rows("SELECT data FROM lesson_relation_snapshots WHERE version_id=?", (version,))[
            0
        ]["data"]
    )
    assert snapshot["relations"] == [] and snapshot["prerequisite_context"] == []
    assert json.loads(app.library.index_row(index["id"])["data"])["relations"]


def test_schema6_migration_rolls_back_ddl_and_marker_together(app, monkeypatch):
    import teaching_agent.relation_schema as schema

    with app.store.connect() as conn:
        conn.execute("PRAGMA user_version=5")

    def fail(conn):
        conn.execute("CREATE TABLE migration_probe(id INTEGER)")
        conn.execute("PRAGMA user_version=6")
        raise AppError("simulated migration interruption")

    monkeypatch.setattr(schema, "migrate", fail)
    with pytest.raises(AppError, match="interruption"):
        app.store.initialize()
    assert app.store.rows("PRAGMA user_version")[0]["user_version"] == 5
    assert not app.store.rows("SELECT name FROM sqlite_master WHERE name='migration_probe'")


def test_section_rebuild_coverage_does_not_claim_full_completion(app, book_id):
    first = content(app, book_id)
    baseline = json.loads(first["data"])
    rebuilt = app.library.index(book_id, section_id="s1", new=True)
    data = json.loads(rebuilt["data"])
    changed = {p["key"] for p in data["points"] if p["section_id"] == "s1"}
    assert data["pipeline"] == baseline["pipeline"]
    assert data["regions"]
    assert rebuilt["status"] == "building"
    assert app.library.content_active(book_id)["id"] == rebuilt["id"]
    tasks = app.store.rows("SELECT * FROM relation_tasks WHERE index_id=?", (rebuilt["id"],))
    assert len(tasks) == 1
    assert tasks[0]["section_id"] == "@through:s1"
    assert tasks[0]["status"] == "candidate_complete"
    pairs = app.store.rows("SELECT * FROM relation_task_pairs WHERE task_id=?", (tasks[0]["id"],))
    assert all(p["left_key"] in changed and p["right_key"] in changed for p in pairs)
