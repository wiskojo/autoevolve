from typing import Annotated

import typer

from autoevolve.app import app
from autoevolve.harnesses import Harness, get_harness_spec
from autoevolve.repository import PROBLEM_FILE
from autoevolve.scaffold import Scaffolder


@app.command(
    "init",
    rich_help_panel="Human",
    short_help="Set up PROBLEM.md and agent instructions.",
    help=(
        "Set up PROBLEM.md and agent instructions.\n\n"
        f"If {PROBLEM_FILE} does not exist, init writes a stub. If it already exists, "
        "init leaves it unchanged. If no harness is provided, init prompts for one. "
        "Use --yes to skip confirmation prompts and write files immediately."
    ),
)
def init(
    harness: Annotated[Harness | None, typer.Option(help="Target agent harness.")] = None,
    continue_hook: Annotated[
        bool,
        typer.Option(help="Install a continue-forever stop hook for supported harnesses."),
    ] = False,
    yes: Annotated[bool, typer.Option(help="Skip confirmation prompts.")] = False,
) -> None:
    scaffolder = Scaffolder()
    if harness is None:
        choice = typer.prompt(
            f"Harness [{'/'.join(item.value for item in Harness)}]",
            default=Harness.CLAUDE.value,
            show_default=False,
        )
        selected = Harness(choice.strip())
    else:
        selected = harness
    spec = get_harness_spec(selected)
    if continue_hook and not spec.supports_continue_hook:
        raise RuntimeError(f'Continue hooks are not supported for harness "{selected.value}".')
    if spec.supports_continue_hook and not continue_hook and not yes:
        continue_hook = bool(
            typer.confirm(f"Install a continue hook for {selected.value}?", default=False)
        )

    problem_exists = (scaffolder.root / PROBLEM_FILE).exists()
    files = [PROBLEM_FILE, spec.prompt_path]
    if continue_hook:
        files.extend(item.path for item in spec.continue_hook_files)

    typer.echo(f"repository: {scaffolder.root}")
    typer.echo(f"harness: {selected.value}")
    if problem_exists:
        typer.echo(f"problem: keep existing {PROBLEM_FILE}")
    typer.echo("files:")
    for path in files:
        action = "keep" if path == PROBLEM_FILE and problem_exists else "write"
        typer.echo(f"  - {action} {path}")
    if not yes and not typer.confirm("Write these files?", default=True):
        raise typer.Exit()

    written = scaffolder.apply_init(selected, continue_hook)
    typer.echo("")
    typer.echo("autoevolve initialized.")
    if written:
        typer.echo("written:")
        for path in written:
            typer.echo(f"  - {path}")
    typer.echo(f"next: {spec.handoff_prompt}")


@app.command(
    "validate",
    rich_help_panel="Human",
    short_help="Check that the repo is ready for autoevolve.",
    help=(
        "Check that the repo is ready for autoevolve.\n\n"
        "validate checks the required autoevolve files and validates the current "
        "experiment record when one is present."
    ),
)
def validate() -> None:
    problems = Scaffolder().validate()
    if problems:
        raise RuntimeError("\n".join(problems))
    typer.echo("OK: repository is ready for autoevolve.")


@app.command(
    "update",
    rich_help_panel="Human",
    short_help="Update detected prompt files to the latest version.",
    help=(
        "Update detected prompt files to the latest version.\n\n"
        "update refreshes any detected harness prompt files in the current "
        "repository. It asks before overwriting PROGRAM.md unless --yes is set."
    ),
)
def update(
    yes: Annotated[bool, typer.Option(help="Skip confirmation prompts.")] = False,
) -> None:
    scaffolder = Scaffolder()
    prompt_files = scaffolder.prompt_files()
    if not prompt_files:
        raise RuntimeError("No prompt files found. Run autoevolve init first.")

    updated: list[str] = []
    skipped: list[str] = []
    typer.echo("detected prompts:")
    for prompt_file in prompt_files:
        relative = prompt_file.path.relative_to(scaffolder.root).as_posix()
        typer.echo(f"  - {relative} ({prompt_file.harness})")
        if relative == "PROGRAM.md" and not yes:
            if not typer.confirm("Overwrite PROGRAM.md?", default=False):
                skipped.append(relative)
                continue
        scaffolder.update_prompt(prompt_file)
        updated.append(relative)

    typer.echo("")
    if updated:
        typer.echo("updated:")
        for path in updated:
            typer.echo(f"  - {path}")
    if skipped:
        typer.echo("skipped:")
        for path in skipped:
            typer.echo(f"  - {path}")
