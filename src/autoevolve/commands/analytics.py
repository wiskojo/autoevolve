import json
from typing import Annotated

import typer

from autoevolve.app import app
from autoevolve.models.experiment import ExperimentRecord, Objective
from autoevolve.models.types import SetOutputFormat
from autoevolve.repository import ExperimentRepository


@app.command(
    "recent",
    rich_help_panel="Analytics",
    short_help="List the most recent recorded experiments.",
    help=(
        "List the most recent recorded experiments.\n\n"
        "recent emits recent experiments in TSV or JSONL format for scripting "
        "and analysis."
    ),
)
def recent(
    limit: Annotated[int, typer.Option(min=1, help="Number of experiments to show.")] = 10,
    output_format: Annotated[
        SetOutputFormat,
        typer.Option("--format", help="Output format."),
    ] = SetOutputFormat.TSV,
) -> None:
    _print_records(ExperimentRepository().recent_records(limit), output_format)


@app.command(
    "best",
    rich_help_panel="Analytics",
    short_help="List the top experiments for one metric.",
    help=(
        "List the top experiments for one metric.\n\n"
        "best ranks recorded experiments by one metric. If no metric is "
        "provided, it defaults to the primary metric from PROBLEM.md."
    ),
)
def best(
    max_metric: Annotated[str | None, typer.Option("--max", help="Metric to maximize.")] = None,
    min_metric: Annotated[str | None, typer.Option("--min", help="Metric to minimize.")] = None,
    limit: Annotated[int, typer.Option(min=1, help="Number of experiments to show.")] = 5,
    output_format: Annotated[
        SetOutputFormat,
        typer.Option("--format", help="Output format."),
    ] = SetOutputFormat.TSV,
) -> None:
    if max_metric and min_metric:
        raise typer.BadParameter("Use either --max <metric> or --min <metric>, not both.")

    objective = None
    if max_metric is not None:
        objective = Objective(direction="max", metric=max_metric)
    if min_metric is not None:
        objective = Objective(direction="min", metric=min_metric)

    repository = ExperimentRepository()
    if objective is None:
        try:
            problem = repository.problem()
        except (FileNotFoundError, ValueError) as error:
            raise RuntimeError(
                "best requires an explicit objective, or a valid PROBLEM.md primary metric."
            ) from error
        resolved = Objective(direction=problem.direction, metric=problem.metric)
    else:
        resolved = objective
    records = repository.best_records(resolved, limit)
    if not records:
        typer.echo(f'No experiments found with a numeric "{resolved.metric}" metric.')
        return
    _print_records(records, output_format)


@app.command(
    "pareto",
    rich_help_panel="Analytics",
    short_help="List the Pareto frontier for selected metrics.",
    help=(
        "List the Pareto frontier for selected metrics.\n\n"
        "pareto returns the non-dominated recorded experiments for the selected "
        "metrics in TSV or JSONL format."
    ),
)
def pareto(
    max_metrics: Annotated[
        list[str] | None,
        typer.Option("--max", help="Metric to maximize. Repeat as needed."),
    ] = None,
    min_metrics: Annotated[
        list[str] | None,
        typer.Option("--min", help="Metric to minimize. Repeat as needed."),
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Number of experiments to show.")] = None,
    output_format: Annotated[
        SetOutputFormat,
        typer.Option("--format", help="Output format."),
    ] = SetOutputFormat.TSV,
) -> None:
    objectives = [Objective(direction="max", metric=metric) for metric in max_metrics or ()]
    objectives.extend(Objective(direction="min", metric=metric) for metric in min_metrics or ())
    if not objectives:
        raise typer.BadParameter(
            "pareto requires at least one metric, for example: --max primary_metric --min runtime_sec"
        )

    records = ExperimentRepository().pareto_records(objectives, limit)
    if not records:
        typer.echo("No experiments found with numeric metrics for the requested Pareto objectives.")
        return
    _print_records(records, output_format)


def _print_records(records: list[ExperimentRecord], output_format: SetOutputFormat) -> None:
    if not records:
        typer.echo("No experiments found.")
        return
    if output_format is SetOutputFormat.TSV:
        typer.echo("sha\tdate\tmetrics\tsummary")
        for record in records:
            typer.echo(_tsv_row(record))
        return
    for record in records:
        typer.echo(json.dumps(_json_record(record)))


def _tsv_row(record: ExperimentRecord) -> str:
    return "\t".join(
        [
            record.sha[:7],
            record.date,
            _clean(_metric_pairs(record)),
            _clean(record.document.summary),
        ]
    )


def _json_record(record: ExperimentRecord) -> dict[str, object]:
    return {
        "sha": record.sha,
        "short_sha": record.sha[:7],
        "date": record.date,
        "summary": record.document.summary,
        "metrics": record.document.metrics,
        "references": [
            {"commit": reference.commit, "why": reference.why}
            for reference in record.document.references
        ],
    }


def _metric_pairs(record: ExperimentRecord) -> str:
    return ", ".join(
        f"{name}={json.dumps(value)}" for name, value in record.document.metrics.items()
    )


def _clean(value: str) -> str:
    return value.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
