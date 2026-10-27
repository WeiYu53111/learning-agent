"""Contracts for the only supported knowledge extraction pipeline."""

from typing import Literal

from pydantic import Field

from .models import Contract

PIPELINE = "semantic-v1"


class Region(Contract):
    first: int = Field(ge=0)
    last: int = Field(ge=0)
    role: Literal["teaching", "navigation", "publication", "unknown"]
    path: list[str]
    topic: str
    reason: str = Field(min_length=1)


class SourceAnalysis(Contract):
    regions: list[Region]


class ExtractionIssue(Contract):
    kind: Literal["fragmented", "overmerged", "missing", "unsupported", "mapping"]
    point_keys: list[str]
    description: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class ExtractionReview(Contract):
    issues: list[ExtractionIssue]


class MergeGroup(Contract):
    members: list[str] = Field(min_length=2)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class BoundaryReview(Contract):
    merges: list[MergeGroup]
