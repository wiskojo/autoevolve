from __future__ import annotations

import sys
from collections.abc import Sequence

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
from autoevolve.constants import MANAGED_WORKTREE_DISPLAY_ROOT
from autoevolve.errors import AutoevolveError


def print_help() -> None:
    click.echo(
        """autoevolve

Git-backed experiment loops for coding agents.

Usage:
  autoevolve <command> [options]

Human:
  init        Scaffold PROBLEM.md and agent instructions.
  validate    Validate that the repo is correctly initialized for autoevolve.

Lifecycle:
  start       Create a managed experiment branch and worktree.
  record      Validate, commit, and remove the current managed worktree.
  clean       Remove stale managed worktrees for this repository.

Inspect:
  status      Show the current experiment snapshot.
  list        List recent experiments.
  show        Show JOURNAL.md and EXPERIMENT.json for one ref.
  compare     Compare two experiment commits.
  graph       Traverse lineage around one ref.

Analytics:
  recent      Return the most recent experiments.
  best        Return the top experiments for one objective.
  pareto      Return the Pareto frontier for the selected objectives.

Examples:
  autoevolve init
  autoevolve start tune-thresholds "Try a tighter threshold sweep" --from autoevolve/seed
  autoevolve record
  autoevolve list
  autoevolve recent --limit 5
  autoevolve best --max benchmark_score --limit 5

Run "autoevolve <command> --help" for command-specific details.
"""
    )


def print_status_help() -> None:
    click.echo(
        """autoevolve status

Show the current experiment snapshot.

Usage:
  autoevolve status [--format <text|json>]

Options:
  --format <text|json>  Output format. Default: text.
"""
    )


def print_list_help() -> None:
    click.echo(
        """autoevolve list

List recent experiments.

Usage:
  autoevolve list [--limit <n>]

Options:
  --limit <n>  Number of experiments to show. Default: 10.

Notes:
  list shows the most recent recorded experiments in a compact human-readable log.
"""
    )


def print_best_help() -> None:
    click.echo(
        """autoevolve best

Return the top experiments for one objective.

Usage:
  autoevolve best [--max <metric>|--min <metric>] [--limit <n>] [--format <tsv|jsonl>]

Options:
  --max <metric>         Maximize one metric.
  --min <metric>         Minimize one metric.
  --limit <n>            Number of experiments to show. Default: 5.
  --format <tsv|jsonl>   Output format. Default: tsv.

Notes:
  If no objective is provided, it defaults to the primary metric from PROBLEM.md.
"""
    )


def print_recent_help() -> None:
    click.echo(
        """autoevolve recent

Show the most recent recorded experiments.

Usage:
  autoevolve recent [--limit <n>] [--format <tsv|jsonl>]

Options:
  --limit <n>          Number of experiments to show. Default: 10.
  --format <tsv|jsonl> Output format. Default: tsv.
"""
    )


def print_pareto_help() -> None:
    click.echo(
        """autoevolve pareto

Return the Pareto frontier for the selected objectives.

Usage:
  autoevolve pareto (--max <metric>|--min <metric>)... [--limit <n>] [--format <tsv|jsonl>]

Options:
  --max <metric>         Maximize one metric. Repeat to add objectives.
  --min <metric>         Minimize one metric. Repeat to add objectives.
  --limit <n>            Number of experiments to show.
  --format <tsv|jsonl>   Output format. Default: tsv.
"""
    )


def print_start_help() -> None:
    click.echo(
        f"""autoevolve start

Create a managed experiment branch and worktree.

Usage:
  autoevolve start <name> <summary> [--from <ref>]

Options:
  --from <ref>  Base git ref to branch from. Default: current branch or HEAD.

Notes:
  start creates a managed worktree under {MANAGED_WORKTREE_DISPLAY_ROOT}.
  Managed branches are created under autoevolve/<name>.
"""
    )


def print_record_help() -> None:
    click.echo(
        f"""autoevolve record

Validate, record, and remove the current managed worktree.

Usage:
  autoevolve record

Notes:
  record stages all changes, commits with the first line of
  EXPERIMENT.json summary, and removes the current managed worktree.
  record only works inside managed worktrees under {MANAGED_WORKTREE_DISPLAY_ROOT}.
"""
    )


def print_clean_help() -> None:
    click.echo(
        f"""autoevolve clean

Remove stale managed worktrees for this repository.

Usage:
  autoevolve clean [<name>] [-f|--force]

Options:
  -f, --force  Remove dirty managed worktrees too.

Notes:
  clean removes managed worktrees under {MANAGED_WORKTREE_DISPLAY_ROOT} for this repository.
  With <name>, clean removes the managed worktree for autoevolve/<name>.
  Without <name>, clean removes every managed worktree for this repository.
"""
    )


def print_compare_help() -> None:
    click.echo(
        """autoevolve compare

Compare two experiment commits.

Usage:
  autoevolve compare <left-ref> <right-ref>
    [--format <text|json>] [--patch]

Options:
  --format <text|json>  Output format. Default: text.
  --patch               Include the git patch.
"""
    )


def print_graph_help() -> None:
    click.echo(
        """autoevolve graph

Traverse lineage around one ref.

Usage:
  autoevolve graph <ref> [--edges <git|references|all>]
    [--direction <backward|forward|both>] [--depth <n|all>]
    [--format <text|json>]

Options:
  --edges <git|references|all>              Edge types to include. Default: all.
  --direction <backward|forward|both>       Traversal direction. Default: backward.
  --depth <n|all>                           Traversal depth. Default: 3.
  --format <text|json>                      Output format. Default: text.
"""
    )


def print_show_help() -> None:
    click.echo(
        """autoevolve show

Show JOURNAL.md and EXPERIMENT.json for one ref.

Usage:
  autoevolve show <ref> [--format <text|json>]

Options:
  --format <text|json>  Output format. Default: text.
"""
    )


def print_command_help(command: str) -> None:
    if command in {"init", "validate"}:
        print_help()
        return
    if command == "status":
        print_status_help()
        return
    if command == "list":
        print_list_help()
        return
    if command == "best":
        print_best_help()
        return
    if command == "recent":
        print_recent_help()
        return
    if command == "pareto":
        print_pareto_help()
        return
    if command == "start":
        print_start_help()
        return
    if command == "record":
        print_record_help()
        return
    if command == "clean":
        print_clean_help()
        return
    if command == "compare":
        print_compare_help()
        return
    if command == "graph":
        print_graph_help()
        return
    if command == "show":
        print_show_help()
        return
    raise AutoevolveError(f'unknown command "{command}"')


def dispatch(argv: Sequence[str]) -> None:
    args = list(argv)
    command = args[0] if args else None
    if not command or command in {"help", "--help", "-h"}:
        print_help()
        return
    command_args = args[1:]
    if "--help" in command_args or "-h" in command_args:
        print_command_help(command)
        return
    if command == "init":
        run_init(command_args)
        return
    if command == "validate":
        run_validate()
        return
    if command == "start":
        run_start(command_args)
        return
    if command == "record":
        run_record(command_args)
        return
    if command == "clean":
        run_clean(command_args)
        return
    if command == "status":
        run_status(command_args)
        return
    if command == "list":
        run_list(command_args)
        return
    if command == "recent":
        run_recent(command_args)
        return
    if command == "show":
        run_show(command_args)
        return
    if command == "compare":
        run_compare(command_args)
        return
    if command == "graph":
        run_graph(command_args)
        return
    if command == "best":
        run_best(command_args)
        return
    if command == "pareto":
        run_pareto(command_args)
        return
    raise AutoevolveError(f'unknown command "{command}"')


@click.command(
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    }
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def _entry(args: tuple[str, ...]) -> None:
    dispatch(args)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _entry.main(
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
    except Exception as error:
        message = error.args[0] if error.args else str(error)
        click.echo(f"autoevolve: {message}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
