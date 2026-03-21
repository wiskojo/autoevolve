from __future__ import annotations

import re

from autoevolve.models import MetricDirection, PrimaryMetricSpec

PRIMARY_METRIC_SPEC_PATTERN = re.compile(r"^(max|min)\s+([A-Za-z_][A-Za-z0-9_.-]*)$")


def extract_markdown_section(text: str, heading: str) -> str | None:
    lines = text.splitlines()
    section_header = f"## {heading}"
    try:
        start_index = next(
            index for index, line in enumerate(lines) if line.strip() == section_header
        )
    except StopIteration:
        return None

    section_lines: list[str] = []
    for line in lines[start_index + 1 :]:
        if re.match(r"^##\s+", line.strip()):
            break
        section_lines.append(line)
    return "\n".join(section_lines)


def parse_primary_metric_spec(text: str) -> PrimaryMetricSpec:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first_line:
        raise ValueError(
            'PROBLEM.md section "Metric" must start with "max <metric>" or "min <metric>".'
        )

    match = PRIMARY_METRIC_SPEC_PATTERN.fullmatch(first_line)
    if not match:
        raise ValueError(
            'PROBLEM.md section "Metric" must start with "max <metric>" '
            'or "min <metric>" (for example: "max benchmark_score").'
        )

    direction, metric = match.groups()
    metric_direction: MetricDirection = "max" if direction == "max" else "min"
    return PrimaryMetricSpec(
        direction=metric_direction,
        metric=metric,
        raw=f"{direction} {metric}",
    )


def parse_problem_primary_metric(problem_text: str) -> PrimaryMetricSpec:
    metric_section = extract_markdown_section(problem_text, "Metric")
    if metric_section is None:
        raise ValueError(
            'PROBLEM.md must contain a "## Metric" section whose first '
            'non-empty line is "max <metric>" or "min <metric>".'
        )
    return parse_primary_metric_spec(metric_section)
