from __future__ import annotations

import json
import os
from typing import Any

import click

from autoevolve.commands.shared import (
    apply_limit,
    build_experiment_object_for_output,
    collect_metric_names,
    dominates,
    get_experiment_records,
    get_record_numeric_metric_value,
    resolve_best_objective,
    validate_pareto_objectives,
)
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root
from autoevolve.models import (
    ExperimentRecord,
    Objective,
    SetOutputFormat,
)
from autoevolve.utils import format_metric_pairs, short_sha, sort_iso_datetime_value


def sanitize_tsv_field(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def format_experiment_tsv_row(record: ExperimentRecord) -> str:
    fields = [
        short_sha(record.sha),
        record.date,
        sanitize_tsv_field(record.subject),
        sanitize_tsv_field(",".join(record.tip_branches)),
        sanitize_tsv_field(
            format_metric_pairs(record.parsed.metrics if record.parsed else None) or ""
        ),
        sanitize_tsv_field(record.parsed.summary if record.parsed else record.parse_error or ""),
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

    def pareto_key(record: ExperimentRecord) -> tuple[Any, ...]:
        values: list[Any] = []
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
