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
from autoevolve.problem import (
    PRIMARY_METRIC_SPEC_EXAMPLE,
    build_primary_metric_init_note,
    parse_primary_metric_spec,
    parse_problem_primary_metric,
)
from autoevolve.prompt import (
    ProblemTemplateOptions,
    build_harness_prompt,
    build_loop_handoff_prompt,
    build_problem_template,
    build_setup_handoff_prompt,
)
from autoevolve.utils import (
    file_exists,
    find_prompt_files,
    has_experiment_files,
    parse_experiment_json,
    read_text_file,
    resolve_repo_path,
    write_text_file,
)


def has_explicit_problem_inputs(
    mode: str | None,
    goal: str | None,
    metric: str | None,
    metric_description: str | None,
    constraints: str | None,
    validation: str | None,
) -> bool:
    return any(
        value is not None
        for value in [
            mode,
            goal,
            metric,
            metric_description,
            constraints,
            validation,
        ]
    )


def require_filled_value(value: str, field_name: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise AutoevolveError(
            f"Missing {field_name} for `Set up now`. Choose scaffold mode "
            "if the problem is not ready yet."
        )
    return trimmed


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


def choose_setup_mode(initial_value: str) -> str:
    click.echo("Problem Setup")
    click.echo(
        "Choose `Set up now` only if you already know the goal, metric, "
        "constraints, and validation."
    )
    click.echo(
        "If you do not have that ready yet, scaffold a stub and finish it with your coding agent."
    )
    choice = click.prompt(
        "How do you want to set up the problem? [now/scaffold]",
        type=click.Choice(["now", "scaffold"]),
        default=initial_value,
        show_choices=False,
    )
    return str(choice)


def choose_keep_existing_problem() -> bool:
    return bool(click.confirm(f"Keep the existing {ROOT_FILES.problem}?", default=True))


def choose_continue_hook(harness: Harness) -> bool:
    click.echo("Continue Forever Hook")
    click.echo(f"Install a {harness.value} stop hook that prevents early termination.")
    click.echo(
        "If enabled, the agent should keep running until it believes it is "
        "done or a human interrupts it."
    )
    return bool(click.confirm("Install the continue-forever hook?", default=False))


def ask_goal(initial_value: str) -> str:
    result = str(
        click.prompt(
            "Goal",
            default=initial_value,
            show_default=bool(initial_value),
        )
    )
    if not result.strip():
        raise AutoevolveError("Goal is required for `Set up now`. Otherwise choose scaffold mode.")
    return result.strip()


def ask_metric_spec(initial_value: str) -> str:
    click.echo("Metric Format")
    click.echo(build_primary_metric_init_note())
    result = str(
        click.prompt(
            "Metric spec",
            default=initial_value or PRIMARY_METRIC_SPEC_EXAMPLE,
            show_default=bool(initial_value),
        )
    )
    if not result.strip():
        raise AutoevolveError(
            "Metric is required for `Set up now`. Otherwise choose scaffold mode."
        )
    try:
        parse_primary_metric_spec(result.strip())
    except ValueError as error:
        raise AutoevolveError(str(error)) from error
    return result.strip()


def ask_metric_description(initial_value: str) -> str:
    return str(
        click.prompt(
            "Metric description (optional)",
            default=initial_value,
            show_default=bool(initial_value),
        )
    ).strip()


def ask_constraints(initial_value: str) -> str:
    return str(
        click.prompt(
            "Constraints or non-goals",
            default=initial_value,
            show_default=bool(initial_value),
        )
    ).strip()


def ask_validation(initial_value: str) -> str:
    result = str(
        click.prompt(
            "Validation",
            default=initial_value,
            show_default=bool(initial_value),
        )
    )
    if not result.strip():
        raise AutoevolveError(
            "Validation is required for `Set up now`. Otherwise choose scaffold mode."
        )
    return result.strip()


def confirm_write(repo_root: str, skip_prompt: bool) -> None:
    click.echo(f"Repository\n{repo_root}")
    if skip_prompt:
        return
    if not click.confirm("Initialize autoevolve in this repository?", default=True):
        raise SystemExit(0)


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


def _read_if_exists(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_continue_hook_files(
    repo_root: str, harness: Harness, overwrite_by_default: bool
) -> list[str]:
    written: list[str] = []
    for file_spec in get_harness_spec(harness).continue_hook_files:
        existing_text = _read_if_exists(resolve_repo_path(repo_root, file_spec.path))
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
    mode: str | None = None,
    goal: str | None = None,
    metric: str | None = None,
    metric_description: str | None = None,
    constraints: str | None = None,
    validation: str | None = None,
    continue_hook: bool = False,
    yes: bool = False,
) -> None:
    repo_root = find_repo_root(os.getcwd())

    confirm_write(repo_root, yes)

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
    has_existing_problem = os.path.exists(existing_problem_path)
    keep_existing_problem = (
        has_existing_problem
        and not has_explicit_problem_inputs(
            mode,
            goal,
            metric,
            metric_description,
            constraints,
            validation,
        )
        and (True if yes else choose_keep_existing_problem())
    )

    selected_mode = None if keep_existing_problem else (mode or choose_setup_mode("now"))
    selected_goal = ""
    selected_metric = ""
    selected_metric_description = ""
    selected_constraints = ""
    selected_validation = ""

    if selected_mode == "now":
        selected_goal = require_filled_value(goal or ask_goal(""), "goal")
        selected_metric = require_filled_value(metric or ask_metric_spec(""), "metric")
        try:
            parse_primary_metric_spec(selected_metric)
        except ValueError as error:
            raise AutoevolveError(str(error)) from error
        selected_metric_description = (
            metric_description
            if metric_description is not None
            else (ask_metric_description("") if metric is None else "")
        )
        selected_constraints = constraints if constraints is not None else ask_constraints("")
        selected_validation = require_filled_value(
            validation or ask_validation(""),
            "validation",
        )

    problem_template = (
        None
        if keep_existing_problem
        else build_problem_template(
            ProblemTemplateOptions(
                constraints=selected_constraints,
                goal=selected_goal,
                metric=selected_metric,
                metric_description=selected_metric_description,
                validation=selected_validation,
            )
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
        review_lines.append(
            "Mode: "
            f"{'Set up now' if selected_mode == 'now' else 'Scaffold and finish with my agent'}"
        )
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
    if selected_mode == "scaffold":
        print_post_init_summary(
            repo_root,
            written_files,
            "ask your agent to finish setup.",
            build_setup_handoff_prompt(),
        )
        return

    print_post_init_summary(
        repo_root,
        written_files,
        "ask your agent to verify setup and begin the experiment loop.",
        build_loop_handoff_prompt(),
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
            f"Missing prompt file. Expected {ROOT_FILES.autoevolve} or a "
            "supported harness skill file."
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
