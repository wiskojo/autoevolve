from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Any, cast, overload

import click

from autoevolve.commands.analytics import run_best, run_pareto, run_recent
from autoevolve.commands.human import run_init, run_validate
from autoevolve.commands.inspect import (
    run_compare,
    run_graph,
    run_list,
    run_show,
    run_status,
)
from autoevolve.commands.lifecycle import run_clean, run_record, run_start
from autoevolve.constants import (
    MANAGED_WORKTREE_ROOT,
    SUPPORTED_HARNESSES,
    format_home_relative_path,
)
from autoevolve.errors import AutoevolveError
from autoevolve.models import (
    GraphDirection,
    GraphEdges,
    Objective,
    ObjectOutputFormat,
    SetOutputFormat,
)

TOP_LEVEL_EXAMPLES = (
    "autoevolve init",
    'autoevolve start tune-thresholds "Try a tighter threshold sweep" --from 07f1844',
    "autoevolve record",
    "autoevolve list",
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
CommandCallback = Callable[..., Any]


class AutoevolveGroup(click.Group):
    @overload
    def command(self, __func: CommandCallback, /) -> click.Command: ...

    @overload
    def command(
        self,
        *args: Any,
        section: str = "Other",
        **kwargs: Any,
    ) -> Callable[[CommandCallback], click.Command]: ...

    def command(
        self,
        *args: Any,
        section: str = "Other",
        **kwargs: Any,
    ) -> click.Command | Callable[[CommandCallback], click.Command]:
        result = super().command(*args, **kwargs)
        if isinstance(result, click.Command):
            result.help_section = section  # type: ignore[attr-defined]
            return result
        decorator = cast(Callable[[CommandCallback], click.Command], result)

        def wrapper(callback: CommandCallback) -> click.Command:
            command = decorator(callback)
            command.help_section = section  # type: ignore[attr-defined]
            return command

        return wrapper

    def list_commands(self, ctx: click.Context) -> list[str]:
        return [name for name, command in self.commands.items() if not command.hidden]

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        sections: dict[str, list[tuple[str, str]]] = {}
        for command_name in self.list_commands(ctx):
            command = self.get_command(ctx, command_name)
            if command is None or command.hidden:
                continue
            section = getattr(command, "help_section", "Other")
            rows = sections.setdefault(section, [])
            rows.append((command_name, command.get_short_help_str(formatter.width)))

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
    section="Human",
    short_help="Scaffold PROBLEM.md and agent instructions.",
    help=(
        "Scaffold PROBLEM.md and agent instructions.\n\n"
        "If no harness is provided, init prompts for one. Use --yes to skip confirmation "
        "prompts and write files immediately."
    ),
)
@click.argument("harness_arg", required=False, type=click.Choice(SUPPORTED_HARNESSES))
@click.option("--harness", type=click.Choice(SUPPORTED_HARNESSES), help="Target agent harness.")
@click.option(
    "--mode",
    type=click.Choice(("now", "scaffold")),
    help="Problem setup mode.",
)
@click.option("--goal", help="Goal for the problem definition.")
@click.option("--metric", help="Primary metric spec, for example: 'max benchmark_score'.")
@click.option("--metric-description", help="Optional explanation for the primary metric.")
@click.option("--constraints", help="Constraints or non-goals.")
@click.option("--validation", help="Validation command or procedure.")
@click.option(
    "--continue-hook",
    is_flag=True,
    help="Install a continue-forever stop hook for supported harnesses.",
)
@click.option("--yes", is_flag=True, help="Skip confirmation prompts.")
def init_command(
    harness_arg: str | None,
    harness: str | None,
    mode: str | None,
    goal: str | None,
    metric: str | None,
    metric_description: str | None,
    constraints: str | None,
    validation: str | None,
    continue_hook: bool,
    yes: bool,
) -> None:
    if harness_arg is not None and harness is not None and harness_arg != harness:
        raise click.UsageError("Provide either a positional harness or --harness, not both.")
    run_init(
        harness=harness or harness_arg,
        mode=mode,
        goal=goal,
        metric=metric,
        metric_description=metric_description,
        constraints=constraints,
        validation=validation,
        continue_hook=continue_hook,
        yes=yes,
    )


@cli.command(
    "validate",
    section="Human",
    short_help="Validate that the repo is correctly initialized for autoevolve.",
    help="Validate that the repo is correctly initialized for autoevolve.",
)
def validate_command() -> None:
    run_validate()


@cli.command(
    "start",
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
    section="Inspect",
    short_help="Show the current experiment snapshot.",
    help="Show the current experiment snapshot.",
)
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
def status_command(output_format: str) -> None:
    run_status(cast(ObjectOutputFormat, output_format))


@cli.command(
    "list",
    section="Inspect",
    short_help="List recent experiments.",
    help="List recent experiments in a compact human-readable log.",
)
@click.option("--limit", type=click.IntRange(min=1), default=10, show_default=True)
def list_command(limit: int) -> None:
    run_list(limit)


@cli.command(
    "show",
    section="Inspect",
    short_help="Show JOURNAL.md and EXPERIMENT.json for one ref.",
    help="Show JOURNAL.md and EXPERIMENT.json for one ref.",
)
@click.argument("ref")
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
def show_command(ref: str, output_format: str) -> None:
    run_show(ref, cast(ObjectOutputFormat, output_format))


@cli.command(
    "compare",
    section="Inspect",
    short_help="Compare two experiment commits.",
    help="Compare two experiment commits.",
)
@click.argument("left_ref")
@click.argument("right_ref")
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
@click.option("--patch", is_flag=True, help="Include the git patch.")
def compare_command(left_ref: str, right_ref: str, output_format: str, patch: bool) -> None:
    run_compare(left_ref, right_ref, cast(ObjectOutputFormat, output_format), patch)


@cli.command(
    "graph",
    section="Inspect",
    short_help="Traverse lineage around one ref.",
    help="Traverse lineage around one ref.",
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
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
def graph_command(
    ref: str,
    edges: str,
    direction: str,
    depth: int | None,
    output_format: str,
) -> None:
    run_graph(
        ref=ref,
        edges=cast(GraphEdges, edges),
        direction=cast(GraphDirection, direction),
        depth=depth,
        output_format=cast(ObjectOutputFormat, output_format),
    )


@cli.command(
    "recent",
    section="Analytics",
    short_help="Return the most recent experiments.",
    help="Return the most recent experiments.",
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
    section="Analytics",
    short_help="Return the top experiments for one objective.",
    help=(
        "Return the top experiments for one objective.\n\n"
        "If no objective is provided, best defaults to the primary metric from PROBLEM.md."
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
    section="Analytics",
    short_help="Return the Pareto frontier for the selected objectives.",
    help="Return the Pareto frontier for the selected objectives.",
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
            "pareto requires at least one objective, for example: --max primary_metric --min runtime_sec"
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
