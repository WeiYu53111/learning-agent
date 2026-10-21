from teaching_agent.cli import chat, print_progress
from teaching_agent.models import Draft, Misconception


def test_progress_shows_saved_titles_and_objectives(app, published, capsys):
    row, _, _ = app.version(published)
    draft = Draft.model_validate_json(row["data"])
    print_progress(app, published)
    output = capsys.readouterr().out
    assert "共 3 个学习目标" in output
    assert "未验证 3" in output
    for point in draft.points:
        assert point.title in output
        for obj in point.objectives:
            assert f"[未验证] {obj.description}" in output
            assert f"编号：{obj.key}" in output
    assert "独立通过 0/2" in output
    assert "复习时间" not in output


def test_progress_groups_states_and_excludes_removed_objectives(app, published, monkeypatch, capsys):
    row, book, chunks = app.version(published)
    draft = Draft.model_validate_json(row["data"])
    template = draft.points[0].objectives[0]
    statuses = ["provisionally_mastered", "review_due", "learning", "needs_remediation", "unknown"]
    objectives = [template.model_copy(update={"key": f"o{i}", "description": f"目标 {i}"}) for i in range(6)]
    objectives[-1].excluded_reason = "背景内容，无需考核"
    draft.points = [
        draft.points[0].model_copy(update={"key": "p1", "title": "第一知识点", "objectives": objectives[:3]}),
        draft.points[0].model_copy(update={"key": "p2", "title": "第二知识点", "objectives": objectives[3:]}),
    ]
    draft.misconceptions = [Misconception(code="m1", description="误认为历史自动传入", objective_keys=["o3"])]
    states = {
        f"o{i}": {
            "status": status, "score": None if status == "unknown" else 0.5,
            "independent_passes": 2 if i < 2 else 0,
            "due": "2026-10-22T00:00:00+00:00" if i < 2 else None,
            "misconceptions": ["m1"] if i == 3 else [],
        }
        for i, status in enumerate(statuses)
    }
    monkeypatch.setattr(app, "version", lambda _: ({**row, "data": draft.model_dump_json()}, book, chunks))
    monkeypatch.setattr(app, "progress", lambda _: states)
    print_progress(app, published)
    output = capsys.readouterr().out
    assert "共 5 个学习目标" in output
    assert "已掌握 1 · 待复习 1 · 学习中 1 · 待补练 1 · 未验证 1" in output
    assert output.index("第一知识点") < output.index("目标 2") < output.index("第二知识点") < output.index("目标 3")
    for i, label in enumerate(["已掌握", "待复习", "学习中", "待补练", "未验证", "已排除"]):
        assert f"[{label}] 目标 {i}" in output
    assert "背景内容，无需考核" in output
    assert "误认为历史自动传入（m1）" in output
    assert "复习时间 2026-10-22T00:00:00+00:00" in output
    assert "趋势分 0.500" in output

    for obj in objectives:
        obj.excluded_reason = "不纳入学习"
    print_progress(app, published)
    output = capsys.readouterr().out
    assert "共 0 个学习目标" in output
    assert "已掌握 0 · 待复习 0 · 学习中 0 · 待补练 0 · 未验证 0" in output


def test_interactive_progress_uses_same_view_and_preserves_input(app, published, monkeypatch, capsys):
    identifier = app.start(published)
    app.advance(identifier)
    app.append_answer_line(identifier, "尚未提交的回答")
    before = app.session(identifier)[1]["answer_input"]
    print_progress(app, published)
    expected = capsys.readouterr().out
    answers = iter(["/progress", "/pause"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    chat(app, identifier, app.session(identifier)[1])
    assert expected in capsys.readouterr().out
    assert app.session(identifier)[1]["answer_input"] == before
