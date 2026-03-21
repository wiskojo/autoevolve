from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MetricDirection = Literal["max", "min"]
MetricValue = bool | int | float | str | None
SetOutputFormat = Literal["jsonl", "tsv"]
GraphDirection = Literal["backward", "both", "forward"]
GraphEdges = Literal["all", "git", "references"]


@dataclass(frozen=True)
class ExperimentReference:
    commit: str
    why: str


@dataclass(frozen=True)
class ExperimentDocument:
    summary: str
    metrics: dict[str, MetricValue] | None = None
    references: list[ExperimentReference] | None = None


@dataclass(frozen=True)
class ExperimentRecord:
    date: str
    sha: str
    subject: str
    experiment_text: str
    journal_text: str
    parsed: ExperimentDocument | None
    parse_error: str | None
    tip_branches: list[str]


@dataclass(frozen=True)
class BranchTip:
    name: str
    sha: str
    subject: str


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    head: str
    short_head: str
    branch: str | None
    is_current: bool
    is_primary: bool
    dirty: bool | None
    is_missing: bool
    is_managed_experiment: bool


@dataclass(frozen=True)
class PromptFile:
    harness: str
    relative_path: str


@dataclass(frozen=True)
class PrimaryMetricSpec:
    direction: MetricDirection
    metric: str
    raw: str


@dataclass(frozen=True)
class Objective:
    direction: MetricDirection
    metric: str
