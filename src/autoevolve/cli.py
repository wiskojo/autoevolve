from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any, cast

import click

from autoevolve.commands.analytics import run_best, run_pareto, run_recent
from autoevolve.commands.human import run_init, run_validate
from autoevolve.commands.inspect import (
    run_compare,
    run_lineage,
    run_log,
    run_show,
    run_status,
)
from autoevolve.commands.lifecycle import run_clean, run_record, run_start
from autoevolve.constants import (
    MANAGED_WORKTREE_ROOT,
    ROOT_FILES,
    format_home_relative_path,
)
from autoevolve.errors import AutoevolveError
from autoevolve.harnesses import HARNESS_NAMES, parse_harness
from autoevolve.models import (
    GraphDirection,
    GraphEdges,
    Objective,
    SetOutputFormat,
)

TOP_LEVEL_EXAMPLES = (
    'autoevolve start tune-thresholds "Try a tighter threshold sweep" --from 07f1844',
    "autoevolve record",
    "autoevolve log",
    "autoevolve recent --limit 5",
    "autoevolve best --max benchmark_score --limit 5",
)
TOP_LEVEL_EPILOG = "\n".join(
    [
        "Examples:",
        *(f"  {example}" for example in TOP_LEVEL_EXAMPLES),
        "",
        'Run "autoevolve <command> --help" for command-specific details.',
    ]
)


class DepthParamType(click.ParamType):
    name = "n|all"

    def convert(
        self,
        value: object,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> int | None:
        text = str(value).strip().lower()
        if text == "all":
            return None
        try:
            parsed = int(text)
        except ValueError:
            self.fail("must be a positive integer or 'all'", param, ctx)
        if parsed <= 0:
            self.fail("must be a positive integer or 'all'", param, ctx)
        return parsed


DEPTH = DepthParamType()


class SectionedCommand(click.Command):
    section: str

    def __init__(self, *args: Any, section: str = "Other", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.section = section


class AutoevolveGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        return [name for name, command in self.commands.items() if not command.hidden]

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        sections: dict[str, list[tuple[str, str]]] = {}
        command_names = self.list_commands(ctx)
        command_width = max((len(command_name) for command_name in command_names), default=0)
        for command_name in command_names:
            command = self.get_command(ctx, command_name)
            if command is None or command.hidden:
                continue
            section = command.section if isinstance(command, SectionedCommand) else "Other"
            rows = sections.setdefault(section, [])
            rows.append(
                (command_name.ljust(command_width), command.get_short_help_str(formatter.width))
            )

        for title, rows in sections.items():
            if not rows:
                continue
            with formatter.section(title):
                formatter.write_dl(rows)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        if self.epilog is None:
            return
        formatter.write_paragraph()
        formatter.write(f"{self.epilog}\n")


@click.group(
    cls=AutoevolveGroup,
    help="Git-backed experiment loops for coding agents.",
    epilog=TOP_LEVEL_EPILOG,
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None and not ctx.resilient_parsing:
        click.echo(ctx.get_help())


@cli.command(
    "init",
    cls=SectionedCommand,
    section="Human",
    short_help="Set up PROBLEM.md and agent instructions.",
    help=(
        "Set up PROBLEM.md and agent instructions.\n\n"
        f"If {ROOT_FILES.problem} does not exist, init writes a stub. If it already exists, "
        "init leaves it unchanged. If no harness is provided, init prompts for one. "
        "Use --yes to skip confirmation prompts and write files immediately."
    ),
)
@click.option("--harness", type=click.Choice(HARNESS_NAMES), help="Target agent harness.")
@click.option(
    "--continue-hook",
    is_flag=True,
    help="Install a continue-forever stop hook for supported harnesses.",
)
@click.option("--yes", is_flag=True, help="Skip confirmation prompts.")
def init_command(
    harness: str | None,
    continue_hook: bool,
    yes: bool,
) -> None:
    run_init(
        harness=parse_harness(harness) if harness is not None else None,
        continue_hook=continue_hook,
        yes=yes,
    )


@cli.command(
    "validate",
    cls=SectionedCommand,
    section="Human",
    short_help="Check that the repo is ready for autoevolve.",
    help=(
        "Check that the repo is ready for autoevolve.\n\n"
        "validate checks the required protocol files and validates the current "
        "experiment record when one is present."
    ),
)
def validate_command() -> None:
    run_validate()


@cli.command(
    "start",
    cls=SectionedCommand,
    section="Lifecycle",
    short_help="Create a managed experiment branch and worktree.",
    help=(
        "Create a managed experiment branch and worktree.\n\n"
        f"Managed worktrees are created under {format_home_relative_path(MANAGED_WORKTREE_ROOT)} "
        "on branches named autoevolve/<name>."
    ),
)
@click.argument("name")
@click.argument("summary")
@click.option("--from", "from_ref", help="Base git ref. Defaults to the current branch or HEAD.")
def start_command(name: str, summary: str, from_ref: str | None) -> None:
    run_start(name, summary, from_ref)


@cli.command(
    "record",
    cls=SectionedCommand,
    section="Lifecycle",
    short_help="Validate, commit, and remove the current managed worktree.",
    help=(
        "Validate, commit, and remove the current managed worktree.\n\n"
        "record stages all changes, commits using the first line of EXPERIMENT.json "
        f"summary, and removes the current managed worktree under "
        f"{format_home_relative_path(MANAGED_WORKTREE_ROOT)}."
    ),
)
def record_command() -> None:
    run_record()


@cli.command(
    "clean",
    cls=SectionedCommand,
    section="Lifecycle",
    short_help="Remove stale managed worktrees for this repository.",
    help=(
        "Remove stale managed worktrees for this repository.\n\n"
        f"clean only removes worktrees under {format_home_relative_path(MANAGED_WORKTREE_ROOT)} "
        "that belong to the current repository."
    ),
)
@click.argument("name", required=False)
@click.option("-f", "--force", is_flag=True, help="Remove dirty or missing managed worktrees too.")
def clean_command(name: str | None, force: bool) -> None:
    run_clean(name, force)


@cli.command(
    "status",
    cls=SectionedCommand,
    section="Inspect",
    short_help="Show the current experiment status.",
    help=(
        "Show the current experiment status.\n\n"
        "status shows the checkout state, recent results, and managed worktrees "
        "for the current repository."
    ),
)
def status_command() -> None:
    run_status()


@cli.command(
    "log",
    cls=SectionedCommand,
    section="Inspect",
    short_help="Show experiment logs.",
    help=(
        "Show experiment logs.\n\n"
        "log shows the most recent recorded experiments with full metrics and "
        "JOURNAL.md content."
    ),
)
@click.option("--limit", type=click.IntRange(min=1), default=10, show_default=True)
def log_command(limit: int) -> None:
    run_log(limit)


@cli.command(
    "show",
    cls=SectionedCommand,
    section="Inspect",
    short_help="Show experiment details.",
    help=(
        "Show experiment details.\n\n"
        "show prints the journal, experiment summary, and the git diff from "
        "the previous experiment, or from the first parent commit when there "
        "is no earlier experiment ancestor."
    ),
)
@click.argument("ref")
def show_command(ref: str) -> None:
    run_show(ref)


@cli.command(
    "compare",
    cls=SectionedCommand,
    section="Inspect",
    short_help="Compare two experiments.",
    help=(
        "Compare two experiments.\n\n"
        "compare shows commit metadata, summaries, metrics, references, and "
        "the git diff between two recorded experiments."
    ),
)
@click.argument("left_ref")
@click.argument("right_ref")
def compare_command(left_ref: str, right_ref: str) -> None:
    run_compare(left_ref, right_ref)


@cli.command(
    "lineage",
    cls=SectionedCommand,
    section="Inspect",
    short_help="Show experiment lineage around one ref.",
    help=(
        "Show experiment lineage around one ref.\n\n"
        "lineage traverses git ancestry and recorded references around one "
        "experiment."
    ),
)
@click.argument("ref")
@click.option(
    "--edges",
    type=click.Choice(("git", "references", "all")),
    default="all",
    show_default=True,
)
@click.option(
    "--direction",
    type=click.Choice(("backward", "forward", "both")),
    default="backward",
    show_default=True,
)
@click.option("--depth", type=DEPTH, default="3", show_default=True)
def lineage_command(
    ref: str,
    edges: str,
    direction: str,
    depth: int | None,
) -> None:
    run_lineage(
        ref=ref,
        edges=cast(GraphEdges, edges),
        direction=cast(GraphDirection, direction),
        depth=depth,
    )


@cli.command(
    "recent",
    cls=SectionedCommand,
    section="Analytics",
    short_help="List the most recent recorded experiments.",
    help=(
        "List the most recent recorded experiments.\n\n"
        "recent emits recent experiments in TSV or JSONL format for scripting "
        "and analysis."
    ),
)
@click.option("--limit", type=click.IntRange(min=1), default=10, show_default=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(("tsv", "jsonl")),
    default="tsv",
    show_default=True,
)
def recent_command(limit: int, output_format: str) -> None:
    run_recent(limit, cast(SetOutputFormat, output_format))


@cli.command(
    "best",
    cls=SectionedCommand,
    section="Analytics",
    short_help="List the top experiments for one metric.",
    help=(
        "List the top experiments for one metric.\n\n"
        "best ranks recorded experiments by one metric. If no metric is "
        "provided, it defaults to the primary metric from PROBLEM.md."
    ),
)
@click.option("--max", "max_metric", help="Metric to maximize.")
@click.option("--min", "min_metric", help="Metric to minimize.")
@click.option("--limit", type=click.IntRange(min=1), default=5, show_default=True)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(("tsv", "jsonl")),
    default="tsv",
    show_default=True,
)
def best_command(
    max_metric: str | None,
    min_metric: str | None,
    limit: int,
    output_format: str,
) -> None:
    if max_metric is not None and min_metric is not None:
        raise click.UsageError("Use either --max <metric> or --min <metric>, not both.")
    objective = None
    if max_metric is not None:
        objective = Objective(direction="max", metric=max_metric)
    if min_metric is not None:
        objective = Objective(direction="min", metric=min_metric)
    run_best(objective, limit, cast(SetOutputFormat, output_format))


@cli.command(
    "pareto",
    cls=SectionedCommand,
    section="Analytics",
    short_help="List the Pareto frontier for selected metrics.",
    help=(
        "List the Pareto frontier for selected metrics.\n\n"
        "pareto returns the non-dominated recorded experiments for the selected "
        "metrics in TSV or JSONL format."
    ),
)
@click.option("--max", "max_metrics", multiple=True, help="Metric to maximize. Repeat as needed.")
@click.option("--min", "min_metrics", multiple=True, help="Metric to minimize. Repeat as needed.")
@click.option("--limit", type=click.IntRange(min=1))
@click.option(
    "--format",
    "output_format",
    type=click.Choice(("tsv", "jsonl")),
    default="tsv",
    show_default=True,
)
def pareto_command(
    max_metrics: tuple[str, ...],
    min_metrics: tuple[str, ...],
    limit: int | None,
    output_format: str,
) -> None:
    objectives = [Objective(direction="max", metric=metric) for metric in max_metrics]
    objectives.extend(Objective(direction="min", metric=metric) for metric in min_metrics)
    if not objectives:
        raise click.UsageError(
            "pareto requires at least one metric, for example: --max primary_metric --min runtime_sec"
        )
    run_pareto(objectives, limit, cast(SetOutputFormat, output_format))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        cli.main(
            args=list(argv) if argv is not None else None,
            prog_name="autoevolve",
            standalone_mode=False,
        )
        return 0
    except SystemExit as error:
        code = error.code
        if isinstance(code, int):
            return code
        return 0 if code is None else 1
    except click.ClickException as error:
        error.show()
        return error.exit_code
    except AutoevolveError as error:
        click.echo(f"autoevolve: {error}", err=True)
        return 1
    except Exception as error:
        message = error.args[0] if error.args else str(error)
        click.echo(f"autoevolve: {message}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
