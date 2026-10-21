import io
import threading

import pytest

from teaching_agent.cli import main
from teaching_agent.progress import DraftProgress


@pytest.mark.parametrize("fail", [False, True])
def test_heartbeat_runs_while_waiting_and_stops_on_exit(fail):
    emitted = threading.Event()

    class Output(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if "[等待]" in text:
                emitted.set()
            return result

    output = Output()
    progress = DraftProgress(interval=0.01, stream=output)
    try:
        with progress:
            progress("等待模型提取")
            assert emitted.wait(2)
            if fail:
                raise RuntimeError("test failure")
    except RuntimeError:
        assert fail
    assert not progress._thread.is_alive()
    assert "本阶段已等待" in output.getvalue()
    assert "总耗时" in output.getvalue()
    assert "仍在等待当前阶段完成" in output.getvalue()
    assert "仍在运行" not in output.getvalue()


def test_cli_draft_progress(app, draft_id, capsys):
    book_id = app.store.get("versions", draft_id)["book_id"]
    assert main(["--data-dir", str(app.store.root), "draft", book_id]) == 0
    output = capsys.readouterr().err
    for message in (
        "第 1/1 批",
        "PDF 页",
        "等待模型提取",
        "校验提取结果",
        "已保存",
        "草稿提取完成",
    ):
        assert message in output


def test_resume_reports_cached_batch_without_model_call(app, draft_id):
    with app.store.connect() as conn:
        conn.execute("UPDATE versions SET data='{}' WHERE id=?", (draft_id,))
    app.injected_gateway.extract = lambda _: pytest.fail("must reuse cached batch")
    messages = []
    app.build_draft(draft_id, progress=messages.append)
    assert any("复用已保存结果" in message for message in messages)
    assert not any("等待模型提取" in message for message in messages)
    assert messages[-1] == "草稿提取完成"
