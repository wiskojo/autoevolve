from dataclasses import dataclass
from pathlib import Path

from autoevolve.models.experiment import ExperimentWorktree


@dataclass(frozen=True)
class StartedExperiment:
    branch: str
    base_ref: str
    path: Path


@dataclass(frozen=True)
class RecordedExperiment:
    branch: str
    sha: str
    path: Path


@dataclass(frozen=True)
class CleanedWorktrees:
    experiment_name: str
    removed: tuple[ExperimentWorktree, ...]
