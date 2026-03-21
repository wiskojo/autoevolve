from enum import Enum
from typing import Literal

MetricDirection = Literal["max", "min"]
MetricValue = bool | int | float | str | None


class SetOutputFormat(str, Enum):
    JSONL = "jsonl"
    TSV = "tsv"


class GraphDirection(str, Enum):
    BACKWARD = "backward"
    BOTH = "both"
    FORWARD = "forward"


class GraphEdges(str, Enum):
    ALL = "all"
    GIT = "git"
    REFERENCES = "references"
