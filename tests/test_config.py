"""验证环境变量和 dotenv 文件的配置优先级。"""

from __future__ import annotations

from app.config import Settings


def test_settings_loads_dotenv_file(tmp_path, monkeypatch) -> None:
    """显式 dotenv 文件应为未设置的配置项提供值。"""

    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=file-key\n"
        "LLM_MODEL_ID=file-model\n"
        "LLM_API_BASE=https://example.test/v1\n"
        "LLM_REASONING_EFFORT=max\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("LLM_REASONING_EFFORT", raising=False)

    settings = Settings.from_env(env_file)

    assert settings.openai_api_key == "file-key"
    assert settings.llm_model_id == "file-model"
    assert settings.llm_api_base == "https://example.test/v1"
    assert settings.llm_reasoning_effort == "max"


def test_process_environment_takes_priority_over_dotenv(tmp_path, monkeypatch) -> None:
    """已有进程环境变量不应被 dotenv 文件覆盖。"""

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=file-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")

    settings = Settings.from_env(env_file)

    assert settings.openai_api_key == "process-key"
