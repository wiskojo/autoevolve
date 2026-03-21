from __future__ import annotations

import json
import os

import click

from autoevolve.commands.shared import (
    apply_limit,
    build_experiment_object_for_output,
    get_experiment_records,
    get_record_numeric_metric_value,
)
from autoevolve.constants import ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root
from autoevolve.models import (
    ExperimentRecord,
    MetricDirection,
    Objective,
    SetOutputFormat,
)
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.utils import (
    file_exists,
    format_metric_pairs,
    is_number,
    read_text_file,
    short_sha,
    sort_iso_datetime_value,
)


def collect_metric_names(records: list[ExperimentRecord]) -> set[str]:
    metric_names: set[str] = set()
    for record in records:
        if record.parsed and record.parsed.metrics:
            metric_names.update(record.parsed.metrics.keys())
    return metric_names


def format_known_metrics(metric_names: set[str]) -> str:
    return ", ".join(sorted(metric_names)) if metric_names else "none yet"


def normalize_metric_field_name(field: str) -> str:
    if field.startswith("metrics."):
        metric_name = field[len("metrics.") :]
        if not metric_name:
            raise AutoevolveError("Metric fields must use metrics.<name>.")
        return metric_name
    return field


def validate_metric_name(metric: str, metric_names: set[str], flag_name: str) -> str:
    normalized = normalize_metric_field_name(metric)
    if metric_names and normalized not in metric_names:
        raise AutoevolveError(
            f'{flag_name} unknown metric "{normalized}". Known metrics: '
            f"{format_known_metrics(metric_names)}"
        )
    return normalized


def resolve_best_objective(
    repo_root: str,
    metric_names: set[str],
    direction: MetricDirection | None,
    metric: str | None,
) -> Objective:
    if direction is not None:
        return Objective(
            direction=direction,
            metric=validate_metric_name(metric or "", metric_names, f"--{direction}"),
        )

    if not file_exists(repo_root, ROOT_FILES.problem):
        raise AutoevolveError(
            "best requires an explicit objective, or a valid PROBLEM.md primary metric."
        )

    try:
        primary_metric = parse_problem_primary_metric(read_text_file(repo_root, ROOT_FILES.problem))
    except ValueError as error:
        raise AutoevolveError(
            "best requires an explicit objective, or a valid PROBLEM.md primary metric."
        ) from error

    return Objective(
        direction=primary_metric.direction,
        metric=validate_metric_name(
            primary_metric.metric,
            metric_names,
            f"--{primary_metric.direction}",
        ),
    )


def validate_pareto_objectives(
    objectives: list[Objective], metric_names: set[str]
) -> list[Objective]:
    return [
        Objective(
            direction=objective.direction,
            metric=validate_metric_name(
                objective.metric,
                metric_names,
                f"--{objective.direction}",
            ),
        )
        for objective in objectives
    ]


def dominates(
    candidate: ExperimentRecord,
    challenger: ExperimentRecord,
    objectives: list[Objective],
) -> bool:
    strictly_better = False
    for objective in objectives:
        candidate_value = get_record_numeric_metric_value(candidate, objective.metric)
        challenger_value = get_record_numeric_metric_value(challenger, objective.metric)
        if not is_number(candidate_value) or not is_number(challenger_value):
            return False
        if objective.direction == "max":
            if candidate_value < challenger_value:
                return False
            if candidate_value > challenger_value:
                strictly_better = True
        else:
            if candidate_value > challenger_value:
                return False
            if candidate_value < challenger_value:
                strictly_better = True
    return strictly_better


def format_experiment_tsv_row(record: ExperimentRecord) -> str:
    def clean(value: str) -> str:
        return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()

    fields = [
        short_sha(record.sha),
        record.date,
        clean(record.subject),
        clean(",".join(record.tip_branches)),
        clean(format_metric_pairs(record.parsed.metrics if record.parsed else None) or ""),
        clean(record.parsed.summary if record.parsed else record.parse_error or ""),
    ]
    return "\t".join(fields)


def print_set_header(output_format: SetOutputFormat) -> None:
    if output_format == "tsv":
        click.echo("sha\tdate\tsubject\ttips\tmetrics\tsummary")


def print_set_record(record: ExperimentRecord, output_format: SetOutputFormat) -> None:
    if output_format == "jsonl":
        click.echo(json.dumps(build_experiment_object_for_output(record)))
        return
    click.echo(format_experiment_tsv_row(record))


def run_recent(limit: int = 10, output_format: SetOutputFormat = "tsv") -> None:
    repo_root = find_repo_root(os.getcwd())
    records = apply_limit(
        sorted(
            get_experiment_records(repo_root),
            key=lambda record: record.date,
            reverse=True,
        ),
        limit,
    )
    if not records:
        if output_format != "jsonl":
            click.echo("No experiments found.")
        return
    print_set_header(output_format)
    for record in records:
        print_set_record(record, output_format)


def run_best(
    objective: Objective | None = None,
    limit: int = 5,
    output_format: SetOutputFormat = "tsv",
) -> None:
    repo_root = find_repo_root(os.getcwd())
    all_records = get_experiment_records(repo_root)
    resolved_objective = resolve_best_objective(
        repo_root,
        collect_metric_names(all_records),
        objective.direction if objective is not None else None,
        objective.metric if objective is not None else None,
    )
    records = [
        record
        for record in all_records
        if record.parsed
        and record.parsed.metrics
        and get_record_numeric_metric_value(record, resolved_objective.metric) is not None
    ]

    def best_key(record: ExperimentRecord) -> tuple[int | float, int]:
        metric_value = get_record_numeric_metric_value(record, resolved_objective.metric)
        if metric_value is None:
            raise AutoevolveError(
                f'Metric "{resolved_objective.metric}" must be numeric for ranking.'
            )
        ranked_value = metric_value if resolved_objective.direction == "min" else -metric_value
        return (ranked_value, -sort_iso_datetime_value(record.date))

    records = sorted(records, key=best_key)[:limit]
    if not records:
        if output_format != "jsonl":
            click.echo(f'No experiments found with a numeric "{resolved_objective.metric}" metric.')
        return
    print_set_header(output_format)
    for record in records:
        print_set_record(record, output_format)


def run_pareto(
    objectives: list[Objective],
    limit: int | None = None,
    output_format: SetOutputFormat = "tsv",
) -> None:
    repo_root = find_repo_root(os.getcwd())
    all_records = get_experiment_records(repo_root)
    objectives = validate_pareto_objectives(objectives, collect_metric_names(all_records))
    candidates = [
        record
        for record in all_records
        if all(
            get_record_numeric_metric_value(record, objective.metric) is not None
            for objective in objectives
        )
    ]
    if not candidates:
        if output_format != "jsonl":
            click.echo(
                "No experiments found with numeric metrics for the requested Pareto objectives."
            )
        return
    frontier = [
        candidate
        for candidate in candidates
        if not any(
            other.sha != candidate.sha and dominates(other, candidate, objectives)
            for other in candidates
        )
    ]

    def pareto_key(record: ExperimentRecord) -> tuple[int | float, ...]:
        values: list[int | float] = []
        for objective in objectives:
            metric_value = get_record_numeric_metric_value(record, objective.metric)
            if metric_value is None:
                raise AutoevolveError(f'Metric "{objective.metric}" must be numeric for ranking.')
            values.append(metric_value if objective.direction == "min" else -metric_value)
        values.append(-sort_iso_datetime_value(record.date))
        return tuple(values)

    records = apply_limit(sorted(frontier, key=pareto_key), limit)
    print_set_header(output_format)
    for record in records:
        print_set_record(record, output_format)
