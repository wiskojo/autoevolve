import re

from autoevolve.models.experiment import ProblemSpec
from autoevolve.models.types import MetricDirection

PRIMARY_METRIC_PATTERN = re.compile(r"^(max|min)\s+([A-Za-z_][A-Za-z0-9_.-]*)$")


def markdown_section(text: str, heading: str) -> str | None:
    section_header = f"## {heading}"
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != section_header:
            continue
        body: list[str] = []
        for candidate in lines[index + 1 :]:
            if candidate.strip().startswith("## "):
                break
            body.append(candidate)
        return "\n".join(body).strip()
    return None


def parse_problem_spec(text: str) -> ProblemSpec:
    metric_section = markdown_section(text, "Metric")
    if metric_section is None:
        raise ValueError(
            'PROBLEM.md must contain a "## Metric" section whose first non-empty line is '
            '"max <metric>" or "min <metric>".'
        )
    first_line = next((line.strip() for line in metric_section.splitlines() if line.strip()), "")
    if not first_line:
        raise ValueError(
            'PROBLEM.md section "Metric" must start with "max <metric>" or "min <metric>".'
        )
    match = PRIMARY_METRIC_PATTERN.fullmatch(first_line)
    if match is None:
        raise ValueError(
            'PROBLEM.md section "Metric" must start with "max <metric>" or "min <metric>" '
            '(for example: "max benchmark_score").'
        )
    direction_text, metric = match.groups()
    direction: MetricDirection = "max" if direction_text == "max" else "min"
    return ProblemSpec(direction=direction, metric=metric, raw=f"{direction} {metric}")
