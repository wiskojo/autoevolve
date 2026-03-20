from __future__ import annotations

import re
from typing import cast

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


def build_problem_metric_section(metric_spec: str, metric_description: str) -> str:
    trimmed_spec = metric_spec.strip()
    trimmed_description = metric_description.strip()

    if not trimmed_spec:
        return "\n".join(
            [
                ("TODO: first non-empty line must be `max <metric_name>` or `min <metric_name>`."),
                "",
                (
                    "Optional: provide a natural language description of what "
                    "we're trying to optimize."
                ),
            ]
        )

    if not trimmed_description:
        return trimmed_spec

    return f"{trimmed_spec}\n\n{trimmed_description}"


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
    return PrimaryMetricSpec(
        direction=cast(MetricDirection, direction),
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
