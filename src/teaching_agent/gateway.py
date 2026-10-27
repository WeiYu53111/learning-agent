"""OpenAI-compatible JSON HTTP gateway; providers are isolated from domain writes."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx
from pydantic import ValidationError

from .models import (
    AppError,
    Assessment,
    Audit,
    AuditRefRepair,
    Draft,
    DraftPatch,
    EvidenceAssessment,
    Task,
)
from .prompt_loader import load_prompt


@dataclass
class Settings:
    base: str = ""
    model: str = ""
    key: str = ""
    timeout: float = 90
    deadline: float = 180
    output_tokens: int = 8000
    input_chars: int = 48000

    @classmethod
    def from_env(cls):
        try:
            value = cls(
                base=os.getenv("TEACH_API_BASE", "").rstrip("/"),
                model=os.getenv("TEACH_MODEL", ""),
                key=os.getenv("TEACH_API_KEY", ""),
                timeout=float(os.getenv("TEACH_REQUEST_TIMEOUT", "90")),
                deadline=float(os.getenv("TEACH_OPERATION_TIMEOUT", "180")),
                output_tokens=int(os.getenv("TEACH_OUTPUT_TOKENS", "8000")),
                input_chars=int(os.getenv("TEACH_INPUT_CHARS", "48000")),
            )
        except ValueError as exc:
            raise AppError("TEACH 配置中的数值无效。") from exc
        if min(value.timeout, value.deadline, value.output_tokens, value.input_chars) <= 0:
            raise AppError("超时与输入输出上限必须为正数。")
        return value


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("重复 JSON 键")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("不允许非有限数值")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class ModelOperations:
    """Shared teaching operations; transports implement call(operation, payload, contract)."""

    @staticmethod
    def messages(operation, body, schema):
        prompt = load_prompt("common") + "\n" + load_prompt(operation) + "\nJSON Schema:\n" + schema
        return [{"role": "system", "content": prompt}, {"role": "user", "content": body}]

    def book_operation(self, operation, payload, contract):
        return self.call(operation, payload, contract)

    def audit(self, draft, chunks):
        return self.call(
            "audit",
            {"draft": draft.model_dump(), "chunks": [c.model_dump() for c in chunks]},
            Audit,
        )

    def repair_audit_refs(self, audit, errors, chunks):
        indices = sorted({error["issue_index"] for error in errors})
        return self.call(
            "repair_audit_refs",
            {
                "issues": [
                    {"issue_index": i, "issue": audit.issues[i].model_dump()} for i in indices
                ],
                "errors": errors,
                "chunks": [c.model_dump() for c in chunks],
            },
            AuditRefRepair,
        )

    def revise(self, draft, chunks, instruction):
        return self.call(
            "revise",
            {
                "draft": draft.model_dump(),
                "instruction": instruction,
                "chunks": [c.model_dump() for c in chunks],
            },
            Draft,
        )

    def revise_patch(self, draft, chunks, instruction):
        payload = {
            "draft": draft.model_dump(),
            "chunks": [c.model_dump() for c in chunks],
            "instruction": instruction,
        }
        self.last_revision_metrics = None
        started = time.monotonic()
        patch = self.call("revise_patch", payload, DraftPatch)
        self.last_revision_metrics = {
            "input_data_chars": len(json.dumps(payload, ensure_ascii=False)),
            "patch_chars": len(patch.model_dump_json()),
            "draft_chars": len(draft.model_dump_json()),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        return patch

    def task(self, objective, chunks, action, history, misconceptions):
        return self.call(
            "task",
            {
                "objective_key": objective.key,
                "action": action,
                "objective": objective.model_dump(),
                "chunks": [c.model_dump() for c in chunks],
                "seen_questions": history,
                "misconceptions": misconceptions,
            },
            Task,
        )

    def task_evidence(self, objective, chunks, action, history, misconceptions):
        return self.call(
            "task_evidence",
            {
                "objective_key": objective.key,
                "objective": objective.model_dump(),
                "action": action,
                "chunks": [c.model_dump() for c in chunks],
                "seen_questions": history,
                "misconceptions": misconceptions,
            },
            Task,
        )

    def grade_evidence(self, question, answer, chunks, misconceptions, context):
        return self.call(
            "grade_evidence",
            {
                "question": question.model_dump(),
                "answer": answer,
                "chunks": [c.model_dump() for c in chunks],
                "misconceptions": misconceptions,
                **context,
            },
            EvidenceAssessment,
        )

    def grade(self, question, answer, chunks, misconceptions):
        return self.call(
            "grade",
            {
                "question": question.model_dump(),
                "answer": answer,
                "chunks": [c.model_dump() for c in chunks],
                "misconceptions": misconceptions,
            },
            Assessment,
        )


class Gateway(ModelOperations):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = settings.model

    def call(self, operation, payload, contract):
        cfg = self.settings
        if not cfg.key or not cfg.model or not cfg.base.startswith(("https://", "http://")):
            raise AppError("请配置 TEACH_API_KEY、TEACH_MODEL 和 TEACH_API_BASE。")
        body = json.dumps(payload, ensure_ascii=False)
        schema = json.dumps(contract.model_json_schema(), ensure_ascii=False)
        if len(body) + len(schema) > cfg.input_chars:
            raise AppError("本次输入过长。请选择更小章节或提高 TEACH_INPUT_CHARS 后重试。")
        messages = self.messages(operation, body, schema)
        end = time.monotonic() + cfg.deadline
        repaired, network_retries = False, 0
        with httpx.Client() as client:
            for request_index in range(4):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    response = client.post(
                        cfg.base + "/chat/completions",
                        headers={"Authorization": f"Bearer {cfg.key}"},
                        json={
                            "model": cfg.model,
                            "messages": messages,
                            "max_tokens": cfg.output_tokens,
                            "stream": False,
                        },
                        timeout=min(cfg.timeout, remaining),
                    )
                    if response.status_code == 429 or response.status_code >= 500:
                        raise httpx.TransportError("temporary service error")
                    if response.status_code >= 400:
                        raise AppError(
                            f"模型服务返回 HTTP {response.status_code}，请检查地址、模型和配置。"
                        )
                    choice = response.json()["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("模型输出被截断")
                    value = strict_json(choice["message"]["content"])
                    return contract.model_validate(value)
                except httpx.TransportError:
                    network_retries += 1
                    if network_retries > 2 or request_index == 3:
                        break
                    delay = min(2**network_retries, max(0, end - time.monotonic()))
                    time.sleep(delay)
                except (ValueError, KeyError, IndexError, TypeError, ValidationError):
                    if repaired:
                        raise AppError("模型连续返回不符合契约的内容，原数据未覆盖。") from None
                    repaired = True
                    messages.append(
                        {
                            "role": "user",
                            "content": "上次输出不符合契约，请严格按schema重新输出完整JSON。",
                        }
                    )
        raise AppError("模型请求失败或预算耗尽；已保存的数据可恢复后重试。")
