from typing import Annotated

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt

from autoevolve.harnesses import Harness, get_harness_spec
from autoevolve.repository import PROBLEM_FILE
from autoevolve.scaffold import Scaffolder

app = typer.Typer()
console = Console(highlight=False)


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
        choice = Prompt.ask(
            "Harness",
            choices=[item.value for item in Harness],
            default=Harness.CLAUDE.value,
            console=console,
        )
        selected = Harness(choice.strip())
    else:
        selected = harness
    spec = get_harness_spec(selected)
    if continue_hook and not spec.supports_continue_hook:
        raise RuntimeError(f'Continue hooks are not supported for harness "{selected.value}".')
    if spec.supports_continue_hook and not continue_hook and not yes:
        continue_hook = Confirm.ask(
            f"Install a continue hook for {selected.value}?",
            default=False,
            console=console,
        )

    problem_exists = (scaffolder.root / PROBLEM_FILE).exists()
    files = [PROBLEM_FILE, spec.prompt_path]
    if continue_hook:
        files.extend(item.path for item in spec.continue_hook_files)

    console.print("[bold]Setup[/bold]")
    console.print(f"[bold]{'Repository':<14}[/bold]{scaffolder.root}", soft_wrap=True)
    console.print(f"[bold]{'Harness':<14}[/bold]{selected.value}")
    console.print(
        f"[bold]{'Problem':<14}[/bold]"
        f"{'keep existing' if problem_exists else 'write'} {PROBLEM_FILE}"
    )
    if continue_hook:
        console.print(f"[bold]{'Continue hook':<14}[/bold]enabled")
    console.print()
    console.print("[bold]Files[/bold]")
    for path in files:
        action = "keep" if path == PROBLEM_FILE and problem_exists else "write"
        console.print(f"[dim]{action:<6}[/dim]{path}", soft_wrap=True)
    if not yes and not Confirm.ask("Write these files?", default=True, console=console):
        raise typer.Exit()

    written = scaffolder.apply_init(selected, continue_hook)
    console.print()
    console.print("[bold green]autoevolve initialized[/bold green]")
    if written:
        console.print(f"[bold]{'Written':<14}[/bold]{written[0]}", soft_wrap=True)
        for path in written[1:]:
            console.print(f"{'':14}{path}", soft_wrap=True)
    _print_next_step(selected, spec.display_name, spec.handoff_prompt)


def _print_next_step(harness: Harness, display_name: str, handoff_prompt: str) -> None:
    console.print()
    console.print("[bold cyan]Next Step[/bold cyan]")
    if harness is Harness.OTHER:
        console.print("Tell your coding agent to:")
        console.print(f'  "{handoff_prompt}"', soft_wrap=True)
        return
    console.print(f"Open {display_name} and type:")
    console.print(f"  [bold]{handoff_prompt}[/bold]", soft_wrap=True)


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
