"""Load bundled prompt text independently of the working directory."""

from importlib.resources import files

from .models import AppError

_PROMPTS = {
    name: f"{name}.txt"
    for name in (
        "common", "extract", "audit", "revise", "revise_patch", "task", "grade", "codex_data"
    )
}


def load_prompt(name: str) -> str:
    if name not in _PROMPTS:
        raise AppError(f"未知提示词：{name}")
    try:
        text = files("teaching_agent").joinpath("prompts", _PROMPTS[name]).read_text(
            encoding="utf-8"
        ).rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise AppError(f"无法读取提示词资源：{name}") from exc
    if not text.strip():
        raise AppError(f"提示词资源为空：{name}")
    return text
