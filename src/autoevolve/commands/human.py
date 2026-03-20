from __future__ import annotations

import os

import click

from autoevolve.constants import ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root
from autoevolve.harnesses import (
    DEFAULT_HARNESS,
    HARNESS_NAMES,
    Harness,
    get_harness_spec,
)
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.prompt import ProblemTemplateOptions, build_harness_prompt, build_problem_template
from autoevolve.utils import (
    file_exists,
    find_prompt_files,
    has_experiment_files,
    parse_experiment_json,
    read_text_file,
    resolve_repo_path,
    write_text_file,
)


def print_post_init_summary(
    repo_root: str, written_files: list[str], next_step: str, example_prompt: str
) -> None:
    click.echo("")
    click.echo(f"Repository: {repo_root}")
    if written_files:
        click.echo("")
        click.echo("Files written:")
        for file_name in written_files:
            click.echo(f"  - {file_name}")
    click.echo("")
    click.echo(f"Next: {next_step}")
    click.echo("")
    click.echo("For example:")
    click.echo(f"  {example_prompt}")


def choose_harness(initial_value: Harness) -> Harness:
    prompt = "Which coding agent should autoevolve target?"
    choice = click.prompt(
        f"{prompt} [{'/'.join(HARNESS_NAMES)}]",
        type=click.Choice(HARNESS_NAMES),
        default=initial_value.value,
        show_choices=False,
    )
    return Harness(str(choice))


def choose_continue_hook(harness: Harness) -> bool:
    click.echo("Continue Forever Hook")
    click.echo(f"Install a {harness.value} stop hook that prevents early termination.")
    click.echo(
        "If enabled, the agent should keep running until it believes it is "
        "done or a human interrupts it."
    )
    return bool(click.confirm("Install the continue-forever hook?", default=False))


def write_file_with_confirmation(
    repo_root: str,
    relative_path: str,
    contents: str,
    overwrite_by_default: bool,
) -> bool:
    absolute_path = resolve_repo_path(repo_root, relative_path)
    if os.path.exists(absolute_path) and not overwrite_by_default:
        if not click.confirm(f"Overwrite {relative_path}?", default=False):
            return False
    write_text_file(absolute_path, contents)
    return True


def write_continue_hook_files(
    repo_root: str, harness: Harness, overwrite_by_default: bool
) -> list[str]:
    written: list[str] = []
    for file_spec in get_harness_spec(harness).continue_hook_files:
        absolute_path = resolve_repo_path(repo_root, file_spec.path)
        existing_text = None
        if os.path.exists(absolute_path):
            with open(absolute_path, encoding="utf-8") as handle:
                existing_text = handle.read()
        wrote = write_file_with_confirmation(
            repo_root,
            file_spec.path,
            file_spec.build_contents(existing_text),
            overwrite_by_default,
        )
        if wrote:
            written.append(file_spec.path)
    return written


def run_init(
    harness: Harness | None = None,
    continue_hook: bool = False,
    yes: bool = False,
) -> None:
    repo_root = find_repo_root(os.getcwd())
    click.echo(f"Repository\n{repo_root}")
    if not yes and not click.confirm("Initialize autoevolve in this repository?", default=True):
        raise SystemExit(0)

    selected_harness = harness or choose_harness(DEFAULT_HARNESS)
    harness_spec = get_harness_spec(selected_harness)
    if continue_hook and not harness_spec.supports_continue_hook:
        raise AutoevolveError(
            f'Continue hooks are not supported for harness "{selected_harness.value}".'
        )

    continue_hook = harness_spec.supports_continue_hook and (
        continue_hook or (not yes and choose_continue_hook(selected_harness))
    )

    prompt_text = build_harness_prompt(selected_harness)
    existing_problem_path = resolve_repo_path(repo_root, ROOT_FILES.problem)
    keep_existing_problem = os.path.exists(existing_problem_path)
    problem_template = None
    if not keep_existing_problem:
        problem_template = build_problem_template(
            ProblemTemplateOptions(
                constraints="",
                goal="",
                metric="",
                metric_description="",
                validation="",
            )
        )

    prompt_path = harness_spec.prompt_path
    harness_extra_files = (
        [file_spec.path for file_spec in harness_spec.continue_hook_files] if continue_hook else []
    )
    planned_write_files = [prompt_path, *harness_extra_files]

    review_lines = [f"Harness: {selected_harness.value}"]
    if continue_hook:
        review_lines.append("Continue hook: enabled")
    if keep_existing_problem:
        review_lines.append(f"Problem: Keep existing {ROOT_FILES.problem}")
        review_lines.append(
            f"Files: keep {ROOT_FILES.problem}, write {', '.join(planned_write_files)}"
        )
    else:
        review_lines.append(f"Files: {', '.join([ROOT_FILES.problem, *planned_write_files])}")
    click.echo("Review")
    click.echo("\n".join(review_lines))

    if not yes and not click.confirm("Write these files?", default=True):
        raise SystemExit(0)

    wrote_problem = (
        False
        if problem_template is None
        else write_file_with_confirmation(
            repo_root,
            ROOT_FILES.problem,
            problem_template,
            yes,
        )
    )
    wrote_prompt = write_file_with_confirmation(
        repo_root,
        prompt_path,
        prompt_text,
        yes,
    )
    wrote_harness_extras = (
        write_continue_hook_files(repo_root, selected_harness, yes) if continue_hook else []
    )

    written_files: list[str] = []
    if wrote_problem:
        written_files.append(ROOT_FILES.problem)
    if wrote_prompt:
        written_files.append(prompt_path)
    written_files.extend(wrote_harness_extras)

    click.echo("Autoevolve initialized.")
    print_post_init_summary(
        repo_root,
        written_files,
        "ask your agent to finish setup.",
        "Follow the setup instructions for autoevolve.",
    )


def run_validate() -> None:
    repo_root = find_repo_root(os.getcwd())
    problems: list[str] = []
    primary_metric = None
    if not file_exists(repo_root, ROOT_FILES.problem):
        problems.append(f"Missing {ROOT_FILES.problem}. Run autoevolve init first.")
    else:
        try:
            primary_metric = parse_problem_primary_metric(
                read_text_file(repo_root, ROOT_FILES.problem)
            )
        except Exception as error:
            problems.append(str(error))

    if not find_prompt_files(repo_root):
        problems.append(
            f"Missing prompt file. Expected {ROOT_FILES.program} or a supported harness skill file."
        )

    if has_experiment_files(repo_root):
        has_journal = file_exists(repo_root, ROOT_FILES.journal)
        has_experiment = file_exists(repo_root, ROOT_FILES.experiment)
        if not has_journal or not has_experiment:
            problems.append(
                "Current checkout must contain both "
                f"{ROOT_FILES.journal} and {ROOT_FILES.experiment} when "
                "one is present."
            )
        if has_journal:
            journal_text = read_text_file(repo_root, ROOT_FILES.journal).strip()
            if not journal_text:
                problems.append(f"{ROOT_FILES.journal} must not be empty.")
        if has_experiment:
            try:
                parsed_experiment = parse_experiment_json(
                    read_text_file(repo_root, ROOT_FILES.experiment)
                )
                if primary_metric:
                    experiment_metrics = parsed_experiment.metrics
                    has_required_metric = (
                        experiment_metrics is not None
                        and primary_metric.metric in experiment_metrics
                    )
                    if not has_required_metric:
                        problems.append(
                            f"{ROOT_FILES.experiment} must record the "
                            f'primary metric "{primary_metric.metric}" '
                            f"declared in {ROOT_FILES.problem} "
                            f"({primary_metric.raw}). You can record "
                            "additional metrics too, but this one is "
                            "required."
                        )
            except AutoevolveError as error:
                problems.append(str(error))
    if problems:
        for problem in problems:
            click.echo(f"FAIL: {problem}")
        raise SystemExit(1)
    click.echo("OK: repository matches the autoevolve protocol.")
    if not has_experiment_files(repo_root):
        click.echo(
            "No current experiment record found. Add "
            f"{ROOT_FILES.journal} and {ROOT_FILES.experiment} in the "
            "first experiment commit."
        )
