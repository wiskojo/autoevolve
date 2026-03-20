from __future__ import annotations

import json
import os
from typing import Any, cast

import click

from autoevolve.commands.shared import (
    apply_limit,
    build_experiment_object_for_output,
    collect_metric_names,
    dominates,
    get_experiment_records,
    get_record_numeric_metric_value,
    parse_format,
    parse_positive_integer,
    resolve_best_objective,
    validate_pareto_objectives,
)
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root
from autoevolve.models import (
    BestOptions,
    ExperimentRecord,
    MetricDirection,
    Objective,
    ParetoOptions,
    RecentOptions,
    SetOutputFormat,
)
from autoevolve.utils import format_metric_pairs, short_sha, sort_iso_datetime_value


def parse_recent_options(args: list[str]) -> RecentOptions:
    output_format: SetOutputFormat = "tsv"
    limit = 10
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("tsv", "jsonl"),
            )
            index += 2
            continue
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for recent.')
        raise AutoevolveError(f'Unexpected argument "{token}" for recent.')
    return RecentOptions(format=output_format, limit=limit)


def parse_best_options(args: list[str]) -> BestOptions:
    direction: MetricDirection | None = None
    output_format: SetOutputFormat = "tsv"
    limit = 5
    metric = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("tsv", "jsonl"),
            )
            index += 2
            continue
        if token in {"--min", "--max"}:
            metric = args[index + 1] if index + 1 < len(args) else ""
            if not metric:
                raise AutoevolveError(f"{token} expects a metric name")
            if direction is not None:
                raise AutoevolveError(
                    "best accepts exactly one objective. Use either "
                    "--max <metric> or --min <metric>."
                )
            direction = cast(MetricDirection, token[2:])
            index += 2
            continue
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for best.')
        raise AutoevolveError(f'Unexpected argument "{token}" for best.')
    return BestOptions(direction=direction, format=output_format, limit=limit, metric=metric)


def parse_pareto_options(args: list[str]) -> ParetoOptions:
    output_format: SetOutputFormat = "tsv"
    limit: int | None = None
    objectives: list[Objective] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--max", "--min"}:
            metric = args[index + 1] if index + 1 < len(args) else ""
            if not metric:
                raise AutoevolveError(f"{token} expects a metric name")
            objectives.append(Objective(direction=cast(MetricDirection, token[2:]), metric=metric))
            index += 2
            continue
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("tsv", "jsonl"),
            )
            index += 2
            continue
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for pareto.')
        raise AutoevolveError(f'Unexpected argument "{token}" for pareto.')
    if not objectives:
        raise AutoevolveError(
            "pareto requires at least one objective, for example: "
            "--max primary_metric --min runtime_sec"
        )
    return ParetoOptions(format=output_format, limit=limit, objectives=objectives)


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


def run_recent(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    options = parse_recent_options(args)
    records = apply_limit(
        sorted(
            get_experiment_records(repo_root),
            key=lambda record: record.date,
            reverse=True,
        ),
        options.limit,
    )
    if not records:
        if options.format != "jsonl":
            click.echo("No experiments found.")
        return
    print_set_header(options.format)
    for record in records:
        print_set_record(record, options.format)


def run_best(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    all_records = get_experiment_records(repo_root)
    options = parse_best_options(args)
    objective = resolve_best_objective(repo_root, options, collect_metric_names(all_records))
    records = [
        record
        for record in all_records
        if record.parsed
        and record.parsed.metrics
        and get_record_numeric_metric_value(record, objective.metric) is not None
    ]

    def best_key(record: ExperimentRecord) -> tuple[int | float, int]:
        metric_value = get_record_numeric_metric_value(record, objective.metric)
        if metric_value is None:
            raise AutoevolveError(f'Metric "{objective.metric}" must be numeric for ranking.')
        ranked_value = metric_value if objective.direction == "min" else -metric_value
        return (ranked_value, -sort_iso_datetime_value(record.date))

    records = sorted(records, key=best_key)[: options.limit]
    if not records:
        if options.format != "jsonl":
            click.echo(f'No experiments found with a numeric "{objective.metric}" metric.')
        return
    print_set_header(options.format)
    for record in records:
        print_set_record(record, options.format)


def run_pareto(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    all_records = get_experiment_records(repo_root)
    options = parse_pareto_options(args)
    objectives = validate_pareto_objectives(options, collect_metric_names(all_records))
    candidates = [
        record
        for record in all_records
        if all(
            get_record_numeric_metric_value(record, objective.metric) is not None
            for objective in objectives
        )
    ]
    if not candidates:
        if options.format != "jsonl":
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

    records = apply_limit(sorted(frontier, key=pareto_key), options.limit)
    print_set_header(options.format)
    for record in records:
        print_set_record(record, options.format)
