"""Strict persisted contracts. Model-generated identifiers are local to a version."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class Ref(Contract):
    chunk_id: str
    quote: str = Field(min_length=1)


class Chunk(Contract):
    id: str
    page: int
    text: str


class Section(Contract):
    id: str
    title: str
    level: int = 1
    start_page: int
    end_page: int
    chunk_ids: list[str]


class Book(Contract):
    id: str
    filename: str
    sha256: str
    page_count: int
    sections: list[Section]
    chunks: list[Chunk]
    unavailable_pages: list[int] = Field(default_factory=list)
    demo: bool = False


class Rubric(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1)
    critical: bool = True
    weight: float = Field(gt=0, le=1, allow_inf_nan=False)


class Question(Contract):
    stem: str = Field(min_length=1, max_length=6000)
    reference_answer: str = Field(min_length=1)
    rubric: list[Rubric] = Field(min_length=1)
    hints: list[str] = Field(default_factory=list, max_length=3)
    novelty_group: str = Field(min_length=1)
    source_refs: list[Ref] = Field(min_length=1)
    targeted_misconception_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_rubric(self):
        if abs(sum(r.weight for r in self.rubric) - 1) > 1e-6:
            raise ValueError("rubric 权重总和必须为 1")
        if len({r.key for r in self.rubric}) != len(self.rubric):
            raise ValueError("rubric key 重复")
        if not any(r.critical for r in self.rubric):
            raise ValueError("至少需要一个关键评分项")
        return self


class Objective(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1)
    dimension: Literal["concept", "reasoning", "application", "transfer"]
    prerequisites: list[str] = Field(default_factory=list)
    source_refs: list[Ref] = Field(min_length=1)
    sample_question: Question
    excluded_reason: str | None = None


class Point(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1)
    objectives: list[Objective] = Field(min_length=1)


class ContentItem(Contract):
    key: str
    description: str = Field(min_length=1)
    kind: Literal["definition", "mechanism", "procedure", "boundary", "example", "background"]
    important: bool = True
    source_refs: list[Ref] = Field(min_length=1)
    objective_keys: list[str] = Field(default_factory=list)
    excluded_reason: str | None = None


class Misconception(Contract):
    code: str
    description: str
    objective_keys: list[str] = Field(min_length=1)


class Draft(Contract):
    points: list[Point] = Field(min_length=1)
    content_items: list[ContentItem] = Field(min_length=1)
    misconceptions: list[Misconception] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def objectives(self) -> dict[str, Objective]:
        return {o.key: o for p in self.points for o in p.objectives}


class Issue(Contract):
    description: str = Field(min_length=1)
    severity: Literal["error", "warning"]
    source_refs: list[Ref] = Field(default_factory=list)


class Audit(Contract):
    issues: list[Issue]


class Task(Contract):
    objective_key: str
    action: Literal["DIAGNOSE", "REASSESS", "REMEDIATE", "REVIEW", "EXPLAIN"]
    explanation: str = ""
    question: Question | None = None


class ItemResult(Contract):
    rubric_key: str
    credit: Literal[0, 0.5, 1]
    answer_quotes: list[str] = Field(default_factory=list)
    reason: str


class MisconceptionResult(Contract):
    code: str
    relation: Literal["supports", "refutes", "inconclusive"]
    rubric_keys: list[str]
    answer_quotes: list[str]
    reason: str


class Assessment(Contract):
    status: Literal["valid", "needs_review", "invalid_question"]
    items: list[ItemResult]
    feedback: str
    correction: str = ""
    misconception_results: list[MisconceptionResult] = Field(default_factory=list)


class AppError(Exception):
    """Safe to render; never wrap raw provider bodies in this error."""
