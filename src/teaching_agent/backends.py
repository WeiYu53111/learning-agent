"""Backend selection and serializable, credential-free configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .demo import DemoGateway
from .gateway import Gateway, Settings
from .models import AppError


class ModelBackend(Protocol):
    model: str

    def extract(self, chunks): ...
    def audit(self, draft, chunks): ...
    def revise(self, draft, chunks, instruction): ...
    def task(self, objective, chunks, action, history, misconceptions): ...
    def grade(self, question, answer, chunks, misconceptions): ...


class BackendSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    type: Literal["codex_cli", "openai_compatible", "demo"]
    model: str = ""
    executable: str = "codex"
    auth: Literal["existing_login"] = "existing_login"
    base_url: str = ""
    api_key_env: str = "TEACH_API_KEY"
    timeout_seconds: float = Field(default=180, gt=0)
    request_timeout_seconds: float = Field(default=90, gt=0)
    input_chars: int = Field(default=48000, gt=0)
    output_tokens: int = Field(default=8000, gt=0)


class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    active_backend: str | None = None
    backends: dict[str, BackendSpec] = Field(default_factory=dict)


class BackendRegistry:
    def __init__(self, path: Path | None = None, selected: str | None = None):
        self.path = path
        self.selected = selected
        self._config = None

    def choose(self, demo=False):
        if self._config is None:
            path = self.path or Path("teach.toml")
            if self.path and not path.is_file():
                raise AppError("指定的后端配置文件不存在。")
            try:
                raw_config = (
                    tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                )
            except (ValueError, OSError) as exc:
                raise AppError("无法读取 teach.toml 后端配置，请检查 TOML 格式。") from exc
            self._config = BackendConfig.model_validate(raw_config)
        config = self._config
        if demo and not self.selected:
            return "demo", BackendSpec(type="demo")
        name = self.selected or config.active_backend
        if name:
            spec = config.backends.get(name)
            if spec is None:
                raise AppError(f"后端配置不存在：{name}")
            if spec.type == "demo" and not demo:
                raise AppError("离线演示后端只能用于内置演示教材。")
            return name, spec
        settings = Settings.from_env()
        return "legacy_http", BackendSpec(
            type="openai_compatible",
            model=settings.model,
            base_url=settings.base,
            timeout_seconds=settings.deadline,
            request_timeout_seconds=settings.timeout,
            input_chars=settings.input_chars,
            output_tokens=settings.output_tokens,
        )

    @staticmethod
    def build(spec: BackendSpec) -> ModelBackend:
        if spec.type == "demo":
            return DemoGateway()
        if spec.type == "codex_cli":
            from .codex_gateway import CodexGateway

            return CodexGateway(spec)
        return Gateway(
            Settings(
                base=spec.base_url.rstrip("/"),
                model=spec.model,
                key=os.getenv(spec.api_key_env, ""),
                deadline=spec.timeout_seconds,
                timeout=min(spec.request_timeout_seconds, spec.timeout_seconds),
                input_chars=spec.input_chars,
                output_tokens=spec.output_tokens,
            )
        )
