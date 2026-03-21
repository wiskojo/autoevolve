from dataclasses import dataclass
from pathlib import Path

from autoevolve.models.types import MetricDirection, MetricValue


@dataclass(frozen=True)
class ProblemSpec:
    direction: MetricDirection
    metric: str
    raw: str


@dataclass(frozen=True)
class ExperimentReference:
    commit: str
    why: str


@dataclass(frozen=True)
class ExperimentDocument:
    summary: str
    metrics: dict[str, MetricValue]
    references: tuple[ExperimentReference, ...]


@dataclass(frozen=True)
class ExperimentRecord:
    sha: str
    date: str
    journal: str
    document: ExperimentDocument


@dataclass(frozen=True)
class ExperimentWorktree:
    name: str
    path: Path
    branch: str | None
    head: str
    dirty: bool
    is_missing: bool
    is_current: bool
    is_primary: bool
    is_managed: bool


@dataclass(frozen=True)
class Objective:
    direction: MetricDirection
    metric: str


@dataclass(frozen=True)
class PromptFile:
    harness: str
    path: Path
