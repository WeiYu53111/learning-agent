"""读取并校验应用配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    """集中保存数据库、上传目录和模型服务配置。"""

    database_path: Path
    upload_dir: Path
    max_upload_size_mb: int = 20
    llm_model_id: str = "gpt-4o-mini"
    llm_api_base: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    llm_reasoning_effort: Literal["low", "high", "max"] = "high"
    llm_max_tokens: int = 16000
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        """从进程环境和可选的 dotenv 文件构建配置。"""

        load_dotenv(dotenv_path=env_file, override=False)
        key = os.getenv("OPENAI_API_KEY", "").strip() or None
        reasoning_effort = os.getenv("LLM_REASONING_EFFORT", "high").strip().lower()
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("LLM_REASONING_EFFORT 必须是 low、high 或 max。")
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL。")
        max_tokens = int(os.getenv("LLM_MAX_TOKENS", "16000"))
        if max_tokens < 1000:
            raise ValueError("LLM_MAX_TOKENS 不能小于 1000。")
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", "data/learning_agent.db")),
            upload_dir=Path(os.getenv("UPLOAD_DIR", "data/uploads")),
            max_upload_size_mb=int(os.getenv("MAX_UPLOAD_SIZE_MB", "20")),
            llm_model_id=os.getenv("LLM_MODEL_ID", "gpt-4o-mini"),
            llm_api_base=os.getenv("LLM_API_BASE", "https://api.openai.com/v1"),
            openai_api_key=key,
            llm_reasoning_effort=reasoning_effort,
            llm_max_tokens=max_tokens,
            log_level=log_level,
        )

    def ensure_directories(self) -> None:
        """创建数据库父目录和上传目录。"""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

