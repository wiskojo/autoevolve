import pytest

from autoevolve.problem import parse_problem_spec


def test_parse_problem_spec_accepts_min_metric() -> None:
    problem = parse_problem_spec(
        """# Problem

## Goal
Minimize runtime.

## Metric
min runtime_sec
"""
    )

    assert problem.direction == "min"
    assert problem.metric == "runtime_sec"
    assert problem.raw == "min runtime_sec"


def test_parse_problem_spec_requires_metric_section() -> None:
    with pytest.raises(ValueError, match='must contain a "## Metric" section'):
        parse_problem_spec("# Problem\n\n## Goal\nImprove ranking.\n")
