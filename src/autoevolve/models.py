from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MetricDirection = Literal["max", "min"]
MetricValue = bool | int | float | str | None
SetOutputFormat = Literal["jsonl", "tsv"]
ObjectOutputFormat = Literal["json", "text"]
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
class PrimaryMetricSpec:
    direction: MetricDirection
    metric: str
    raw: str


@dataclass(frozen=True)
class Objective:
    direction: MetricDirection
    metric: str


@dataclass(frozen=True)
class StatusOptions:
    format: ObjectOutputFormat = "text"


@dataclass(frozen=True)
class ListOptions:
    limit: int = 10


@dataclass(frozen=True)
class RecentOptions:
    format: SetOutputFormat = "tsv"
    limit: int = 10


@dataclass(frozen=True)
class BestOptions:
    direction: MetricDirection | None = None
    format: SetOutputFormat = "tsv"
    limit: int = 5
    metric: str = ""


@dataclass(frozen=True)
class ParetoOptions:
    format: SetOutputFormat = "tsv"
    limit: int | None = None
    objectives: list[Objective] = field(default_factory=list)


@dataclass(frozen=True)
class GraphOptions:
    depth: int | None = 3
    direction: GraphDirection = "backward"
    edges: GraphEdges = "all"
    format: ObjectOutputFormat = "text"
    ref: str = ""


@dataclass(frozen=True)
class CompareOptions:
    format: ObjectOutputFormat = "text"
    left_ref: str = ""
    patch: bool = False
    right_ref: str = ""


@dataclass(frozen=True)
class ShowOptions:
    format: ObjectOutputFormat = "text"
    ref: str = ""


@dataclass(frozen=True)
class StartOptions:
    from_ref: str = ""
    name: str = ""
    summary: str = ""


@dataclass(frozen=True)
class CleanOptions:
    force: bool = False
    name: str = ""
