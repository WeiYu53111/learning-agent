"""验证模型配置、调用重试和 JSON 兼容解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import smolagents

from app.config import Settings
from app.teaching import AIServiceError, SmolagentsTeachingService, _extract_json


class NotFoundError(Exception):
    """模拟模型 SDK 携带状态码的 404 异常。"""

    status_code = 404


class FailingModel:
    """始终返回 404 的模型替身。"""

    def generate(self, messages: list[dict]) -> None:
        """忽略消息并抛出模拟异常。"""

        del messages
        raise NotFoundError("Error code: 404")


def test_model_404_has_actionable_configuration_error(monkeypatch) -> None:
    """模型端 404 应转换为可操作的地址和模型配置提示。"""

    service = object.__new__(SmolagentsTeachingService)
    monkeypatch.setattr(service, "model", FailingModel(), raising=False)

    with pytest.raises(AIServiceError, match="LLM_API_BASE"):
        service._run("test")


def test_anthropic_endpoint_is_rejected_before_model_initialization(tmp_path) -> None:
    """Anthropic 协议端点应在构造 OpenAI 兼容模型前被拒绝。"""

    settings = Settings(
        database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads",
        openai_api_key="test-key",
        llm_api_base="https://api.deepseek.com/anthropic",
    )

    with pytest.raises(RuntimeError, match="OpenAI 兼容协议"):
        SmolagentsTeachingService(settings)


def test_deepseek_enables_thinking_without_tool_calls(tmp_path, monkeypatch) -> None:
    """DeepSeek 官方端点应启用思考和 JSON 模式配置。"""

    captured: dict = {}

    class FakeOpenAIModel:
        """捕获传给 SDK 模型构造器的关键字参数。"""

        def __init__(self, **kwargs) -> None:
            """保存构造参数供断言使用。"""

            captured.update(kwargs)

    monkeypatch.setattr(smolagents, "OpenAIModel", FakeOpenAIModel)
    settings = Settings(
        database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads",
        openai_api_key="test-key",
        llm_model_id="deepseek-v4-pro",
        llm_api_base="https://api.deepseek.com",
    )

    service = SmolagentsTeachingService(settings)

    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["reasoning_effort"] == "high"
    assert service.json_mode is True
    assert not hasattr(service, "repository")


def test_run_calls_model_directly_and_parses_content(monkeypatch) -> None:
    """服务应直接调用模型并解析 content 中的 JSON。"""

    captured: list[dict] = []

    class FakeModel:
        """返回固定 JSON 内容的模型替身。"""

        def generate(self, messages: list[dict]) -> SimpleNamespace:
            """记录消息并返回成功响应。"""

            captured.extend(messages)
            return SimpleNamespace(content='{"status": "ok"}')

    service = object.__new__(SmolagentsTeachingService)
    monkeypatch.setattr(service, "model", FakeModel(), raising=False)

    assert service._run("test") == {"status": "ok"}
    assert [message["role"] for message in captured] == ["system", "user"]


def test_extract_json_accepts_fenced_json_and_trailing_commas() -> None:
    """解析器应兼容代码围栏和常见尾随逗号。"""

    response = """
    下面是结果：
    ```json
    {"questions": [{"question": "为什么？",},],}
    ```
    """

    assert _extract_json(response) == {"questions": [{"question": "为什么？"}]}


def test_run_retries_invalid_json_and_uses_deepseek_json_mode(monkeypatch) -> None:
    """无效 JSON 应重试一次，且 DeepSeek 调用应请求 JSON 对象。"""

    calls: list[dict] = []

    class RetryModel:
        """第一次返回截断 JSON、第二次返回有效结果。"""

        def generate(self, messages: list[dict], **kwargs) -> SimpleNamespace:
            """记录每次调用参数并按次数切换响应。"""

            calls.append({"messages": messages, "kwargs": kwargs})
            content = '{"status":' if len(calls) == 1 else '{"status": "ok"}'
            return SimpleNamespace(content=content)

    service = object.__new__(SmolagentsTeachingService)
    monkeypatch.setattr(service, "model", RetryModel(), raising=False)
    monkeypatch.setattr(service, "model_id", "deepseek-v4-pro", raising=False)
    monkeypatch.setattr(service, "api_hostname", "api.deepseek.com", raising=False)
    monkeypatch.setattr(service, "json_mode", True, raising=False)

    assert service._run("test", operation="test_json") == {"status": "ok"}
    assert len(calls) == 2
    assert calls[0]["kwargs"]["response_format"] == {"type": "json_object"}
    assert "上一次响应不是可解析的完整 JSON" in calls[1]["messages"][-1]["content"]
