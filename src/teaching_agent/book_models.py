"""Contracts for book navigation, staged lesson generation and grounded answers."""

from typing import Literal

from pydantic import Field

from .models import ContentItem, Contract, Misconception, Question, Ref


class KnowledgePoint(Contract):
    key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_refs: list[Ref] = Field(min_length=1)


class NavigationItem(Contract):
    key: str = ""
    description: str = Field(min_length=1)
    kind: Literal["definition", "mechanism", "procedure", "boundary", "example", "background"]
    point_keys: list[str] = Field(min_length=1)
    source_refs: list[Ref] = Field(min_length=1)


class Navigation(Contract):
    points: list[KnowledgePoint]
    items: list[NavigationItem]


class Relation(Contract):
    source: str
    target: str
    kind: Literal["prerequisite", "related"]
    reason: str = Field(min_length=1)


class Relations(Contract):
    relations: list[Relation] = Field(default_factory=list)


class ChapterSummary(Contract):
    summary: str = Field(min_length=1, max_length=3000)


class LearningObjective(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1)
    dimension: Literal["concept", "reasoning", "application", "transfer"]
    prerequisites: list[str] = Field(default_factory=list)
    source_refs: list[Ref] = Field(min_length=1)
    excluded_reason: str | None = None


class LearningPoint(Contract):
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    knowledge_keys: list[str] = Field(min_length=1)
    title: str = Field(min_length=1)
    objectives: list[LearningObjective] = Field(min_length=1)


class LearningStructure(Contract):
    points: list[LearningPoint] = Field(min_length=1)
    content_items: list[ContentItem] = Field(min_length=1)
    misconceptions: list[Misconception] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def objectives(self):
        return {o.key: o for p in self.points for o in p.objectives}


class ObjectiveQuestion(Contract):
    objective_key: str
    question: Question


class Questions(Contract):
    questions: list[ObjectiveQuestion] = Field(min_length=1)


class AnswerPart(Contract):
    text: str = Field(min_length=1)
    source_refs: list[Ref] = Field(min_length=1)


class BookAnswer(Contract):
    sufficient: bool
    parts: list[AnswerPart] = Field(default_factory=list)
    limitation: str = ""
