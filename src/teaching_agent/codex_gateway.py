"""Codex subprocess transport; authentication remains owned by Codex."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .gateway import ModelOperations, strict_json
from .models import AppError
from .prompt_loader import load_prompt


def strict_schema(value):
    """Convert Pydantic defaults/optional properties to Codex strict JSON Schema."""
    if isinstance(value, list):
        return [strict_schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: strict_schema(v) for k, v in value.items() if k != "default"}
    if result.get("type") == "object":
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


class CodexGateway(ModelOperations):
    def __init__(self, spec):
        self.spec = spec
        # CLI does not reliably expose the resolved default model in the final response.
        self.model = spec.model or "codex-default (resolved model unknown)"

    def run(self, argv, *, cwd, input=None, timeout=None):
        try:
            return subprocess.run(
                argv,
                input=input,
                cwd=cwd,
                capture_output=True,
                env=os.environ.copy(),
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise AppError("Codex 调用超时，已保存的数据可恢复；没有自动切换后端。") from exc
        except OSError as exc:
            raise AppError("无法启动 Codex，请检查 executable 配置及本机安装。") from exc

    def call(self, operation, payload, contract):
        executable = shutil.which(self.spec.executable)
        if not executable:
            raise AppError("未找到 Codex CLI，请先安装或设置 executable。")
        schema = strict_schema(contract.model_json_schema())
        body = json.dumps(payload, ensure_ascii=False)
        schema_text = json.dumps(schema, ensure_ascii=False)
        input_chars = len(body) + len(schema_text)
        if input_chars > self.spec.input_chars:
            raise AppError(
                f"本次 {operation} 输入过长：请求数据及输出结构共 {input_chars} 字符，"
                f"超过 input_chars={self.spec.input_chars}。"
                "请调整 teach.toml（或 --config 指定文件）中对应后端的 input_chars，"
                "并用 --backend 显式选择该后端以应用新配置；或缩小教材范围。"
            )
        messages = self.messages(operation, body, schema_text)
        prompt = (
            messages[0]["content"]
            + load_prompt("codex_data")
            + "\n"
            + messages[1]["content"]
        )
        with tempfile.TemporaryDirectory(prefix="teach-codex-") as directory:
            root = Path(directory)
            status = self.run([executable, "login", "status"], cwd=root, timeout=15)
            if status.returncode:
                raise AppError("Codex 登录不可用。请在终端运行 codex login，然后恢复教学。")
            schema_path, output_path = root / "schema.json", root / "result.json"
            schema_path.write_text(schema_text, encoding="utf-8")
            argv = [
                executable,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "-c",
                "features.shell_tool=false",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--color",
                "never",
            ]
            if self.spec.model:
                argv.extend(["--model", self.spec.model])
            result = self.run(
                [*argv, "-"], cwd=root, input=prompt, timeout=self.spec.timeout_seconds
            )
            if result.returncode:
                # Never echo provider output: it can contain material or credentials.
                raise AppError(
                    f"Codex 执行失败（退出码 {result.returncode}）。"
                    "请检查 codex login status、额度和网络，再恢复操作。"
                )
            if not output_path.is_file() or output_path.stat().st_size > 4 * 1024 * 1024:
                raise AppError("Codex 未返回有效结果文件或结果过大。")
            try:
                return contract.model_validate(strict_json(output_path.read_text(encoding="utf-8")))
            except (ValueError, TypeError) as exc:
                raise AppError("Codex 返回内容不符合数据契约，已保存的数据未覆盖。") from exc
