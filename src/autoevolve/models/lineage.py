from dataclasses import dataclass

from autoevolve.models.experiment import ExperimentRecord


@dataclass(frozen=True)
class LineageEdge:
    kind: str
    source: str
    target: str
    why: str | None = None


@dataclass(frozen=True)
class LineageGraph:
    root: ExperimentRecord
    node_order: tuple[str, ...]
    edges: tuple[LineageEdge, ...]
