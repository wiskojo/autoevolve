from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence

import click

from autoevolve.commands.analytics import run_best, run_pareto, run_recent
from autoevolve.commands.human import run_init, run_validate
from autoevolve.commands.inspect import run_compare, run_graph, run_list, run_show, run_status
from autoevolve.commands.lifecycle import run_clean, run_record, run_start
from autoevolve.constants import (
    MANAGED_WORKTREE_ROOT,
    SUPPORTED_HARNESSES,
    format_home_relative_path,
)
from autoevolve.errors import AutoevolveError

HELP_CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
TOP_LEVEL_USAGE = "autoevolve <command> [options]"
TOP_LEVEL_EXAMPLES = (
    "autoevolve init",
    'autoevolve start tune-thresholds "Try a tighter threshold sweep" --from 07f1844',
    "autoevolve record",
    "autoevolve list",
    "autoevolve recent --limit 5",
    "autoevolve best --max benchmark_score --limit 5",
)
COMMAND_SECTIONS = (
    ("Human", ("init", "validate")),
    ("Lifecycle", ("start", "record", "clean")),
    ("Inspect", ("status", "list", "show", "compare", "graph")),
    ("Analytics", ("recent", "best", "pareto")),
)


class DepthParamType(click.ParamType):
    name = "n|all"

    def convert(
        self,
        value: object,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> str:
        text = str(value).strip().lower()
        if text == "all":
            return text
        try:
            parsed = int(text)
        except ValueError:
            self.fail("must be a positive integer or 'all'", param, ctx)
        if parsed <= 0:
            self.fail("must be a positive integer or 'all'", param, ctx)
        return text


DEPTH = DepthParamType()


class AutoevolveGroup(click.Group):
    def list_commands(self, ctx: click.Context) -> list[str]:
        ordered_names: list[str] = []
        for _, names in COMMAND_SECTIONS:
            ordered_names.extend(names)
        visible_names = [
            name
            for name in ordered_names
            if name in self.commands and not self.commands[name].hidden
        ]
        remaining_names = sorted(
            name
            for name, command in self.commands.items()
            if not command.hidden and name not in visible_names
        )
        return [*visible_names, *remaining_names]

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        lines = [ctx.command_path, ""]
        if self.help:
            lines.extend([self.help, ""])
        lines.extend(["Usage:", f"  {TOP_LEVEL_USAGE}", ""])
        for title, command_names in COMMAND_SECTIONS:
            lines.append(f"{title}:")
            for command_name in command_names:
                command = self.get_command(ctx, command_name)
                if command is None or command.hidden:
                    continue
                help_text = command.short_help or ""
                lines.append(f"  {command_name:<12}{help_text}")
            lines.append("")
        lines.append("Examples:")
        lines.extend(f"  {example}" for example in TOP_LEVEL_EXAMPLES)
        lines.extend(
            [
                "",
                'Run "autoevolve <command> --help" for command-specific details.',
                "",
                "",
            ]
        )
        formatter.write("\n".join(lines))


def append_option(args: list[str], option_name: str, value: str | int | None) -> None:
    if value is None:
        return
    args.extend([option_name, str(value)])


def append_flag(args: list[str], option_name: str, enabled: bool) -> None:
    if enabled:
        args.append(option_name)


def extend_repeat_option(args: list[str], option_name: str, values: Iterable[str]) -> None:
    for value in values:
        args.extend([option_name, value])


@click.group(
    cls=AutoevolveGroup,
    context_settings=HELP_CONTEXT_SETTINGS,
    help="Git-backed experiment loops for coding agents.",
    invoke_without_command=True,
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None and not ctx.resilient_parsing:
        click.echo(ctx.get_help())


@cli.command(
    "help",
    context_settings=HELP_CONTEXT_SETTINGS,
    hidden=True,
    short_help="Show help for a command.",
)
@click.argument("command_name", required=False)
@click.pass_context
def help_command(ctx: click.Context, command_name: str | None) -> None:
    parent_ctx = ctx.parent
    if parent_ctx is None:
        raise AutoevolveError("help is only available from the autoevolve command group.")
    if command_name is None:
        click.echo(parent_ctx.get_help())
        return
    group = parent_ctx.command
    if not isinstance(group, click.Group):
        raise AutoevolveError("help is only available from the autoevolve command group.")
    command = group.get_command(parent_ctx, command_name)
    if command is None or command.hidden:
        raise click.UsageError(f"No such command '{command_name}'.")
    with click.Context(command, info_name=command_name, parent=parent_ctx) as command_ctx:
        click.echo(command.get_help(command_ctx))


@cli.command(
    "init",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    args: list[str] = []
    if harness_arg is not None:
        args.append(harness_arg)
    append_option(args, "--harness", harness)
    append_option(args, "--mode", mode)
    append_option(args, "--goal", goal)
    append_option(args, "--metric", metric)
    append_option(args, "--metric-description", metric_description)
    append_option(args, "--constraints", constraints)
    append_option(args, "--validation", validation)
    append_flag(args, "--continue-hook", continue_hook)
    append_flag(args, "--yes", yes)
    run_init(args)


@cli.command(
    "validate",
    context_settings=HELP_CONTEXT_SETTINGS,
    short_help="Validate that the repo is correctly initialized for autoevolve.",
    help="Validate that the repo is correctly initialized for autoevolve.",
)
def validate_command() -> None:
    run_validate()


@cli.command(
    "start",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    args = [name, summary]
    append_option(args, "--from", from_ref)
    run_start(args)


@cli.command(
    "record",
    context_settings=HELP_CONTEXT_SETTINGS,
    short_help="Validate, commit, and remove the current managed worktree.",
    help=(
        "Validate, commit, and remove the current managed worktree.\n\n"
        "record stages all changes, commits using the first line of EXPERIMENT.json "
        f"summary, and removes the current managed worktree under "
        f"{format_home_relative_path(MANAGED_WORKTREE_ROOT)}."
    ),
)
def record_command() -> None:
    run_record([])


@cli.command(
    "clean",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    args: list[str] = []
    if name is not None:
        args.append(name)
    append_flag(args, "--force", force)
    run_clean(args)


@cli.command(
    "status",
    context_settings=HELP_CONTEXT_SETTINGS,
    short_help="Show the current experiment snapshot.",
    help="Show the current experiment snapshot.",
)
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
def status_command(output_format: str) -> None:
    run_status(["--format", output_format])


@cli.command(
    "list",
    context_settings=HELP_CONTEXT_SETTINGS,
    short_help="List recent experiments.",
    help="List recent experiments in a compact human-readable log.",
)
@click.option("--limit", type=click.IntRange(min=1), default=10, show_default=True)
def list_command(limit: int) -> None:
    run_list(["--limit", str(limit)])


@cli.command(
    "show",
    context_settings=HELP_CONTEXT_SETTINGS,
    short_help="Show JOURNAL.md and EXPERIMENT.json for one ref.",
    help="Show JOURNAL.md and EXPERIMENT.json for one ref.",
)
@click.argument("ref")
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
def show_command(ref: str, output_format: str) -> None:
    run_show([ref, "--format", output_format])


@cli.command(
    "compare",
    context_settings=HELP_CONTEXT_SETTINGS,
    short_help="Compare two experiment commits.",
    help="Compare two experiment commits.",
)
@click.argument("left_ref")
@click.argument("right_ref")
@click.option("--format", "output_format", type=click.Choice(("text", "json")), default="text")
@click.option("--patch", is_flag=True, help="Include the git patch.")
def compare_command(left_ref: str, right_ref: str, output_format: str, patch: bool) -> None:
    args = [left_ref, right_ref, "--format", output_format]
    append_flag(args, "--patch", patch)
    run_compare(args)


@cli.command(
    "graph",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    depth: str,
    output_format: str,
) -> None:
    run_graph(
        [
            ref,
            "--edges",
            edges,
            "--direction",
            direction,
            "--depth",
            depth,
            "--format",
            output_format,
        ]
    )


@cli.command(
    "recent",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    run_recent(["--limit", str(limit), "--format", output_format])


@cli.command(
    "best",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    args = ["--limit", str(limit), "--format", output_format]
    append_option(args, "--max", max_metric)
    append_option(args, "--min", min_metric)
    run_best(args)


@cli.command(
    "pareto",
    context_settings=HELP_CONTEXT_SETTINGS,
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
    args = ["--format", output_format]
    append_option(args, "--limit", limit)
    extend_repeat_option(args, "--max", max_metrics)
    extend_repeat_option(args, "--min", min_metrics)
    run_pareto(args)


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
