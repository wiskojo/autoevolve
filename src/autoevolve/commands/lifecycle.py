from typing import Annotated

import typer

from autoevolve.app import app
from autoevolve.repository import WORKTREE_ROOT_DISPLAY
from autoevolve.worktree import ExperimentWorktreeManager


@app.command(
    "start",
    rich_help_panel="Lifecycle",
    short_help="Create a managed experiment branch and worktree.",
    help=(
        "Create a managed experiment worktree.\n\n"
        f"Managed worktrees are created under {WORKTREE_ROOT_DISPLAY}."
    ),
)
def start(
    name: str,
    summary: str,
    from_ref: Annotated[
        str | None,
        typer.Option("--from", help="Base git ref. Defaults to HEAD."),
    ] = None,
) -> None:
    result = ExperimentWorktreeManager().start(name, summary, from_ref)
    typer.echo(f"Branch: {result.branch}")
    typer.echo(f"Base: {result.base_ref}")
    typer.echo(f"Path: {result.path}")


@app.command(
    "record",
    rich_help_panel="Lifecycle",
    short_help="Validate, commit, and remove the current managed worktree.",
    help=(
        "Validate, commit, and remove the current managed worktree.\n\n"
        "record stages all changes, commits using the first line of EXPERIMENT.json "
        f"summary, and removes the current managed worktree under {WORKTREE_ROOT_DISPLAY}."
    ),
)
def record() -> None:
    result = ExperimentWorktreeManager().record()
    typer.echo(f"Committed {result.branch} at {result.sha[:7]}.")
    typer.echo(f"Removed worktree: {result.path}")


@app.command(
    "clean",
    rich_help_panel="Lifecycle",
    short_help="Remove stale managed worktrees for this repository.",
    help=(
        "Remove stale managed worktrees for this repository.\n\n"
        f"clean only removes worktrees under {WORKTREE_ROOT_DISPLAY} that belong "
        "to the current repository."
    ),
)
def clean(
    name: str | None = None,
    force: Annotated[
        bool,
        typer.Option("-f", "--force", help="Remove dirty or missing managed worktrees too."),
    ] = False,
) -> None:
    result = ExperimentWorktreeManager().clean(name, force)
    if not result.removed:
        typer.echo("No managed worktrees to clean.")
        return
    suffix = "" if len(result.removed) == 1 else "s"
    typer.echo(f"Removed {len(result.removed)} linked worktree{suffix} for this repository.")
    if result.experiment_name:
        typer.echo(f"Experiment: {result.experiment_name}")
    for worktree in result.removed:
        state = "missing" if worktree.is_missing else "dirty" if worktree.dirty else "clean"
        typer.echo(
            f"  {worktree.path} ({worktree.branch or '(detached HEAD)'}, {state}, {worktree.head[:7]})"
        )
