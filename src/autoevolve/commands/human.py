from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import click

from autoevolve.constants import HARNESS_PATHS, ROOT_FILES, SUPPORTED_HARNESSES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root
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

CLAUDE_SETTINGS_PATH = os.path.join(".claude", "settings.json")
GEMINI_SETTINGS_PATH = os.path.join(".gemini", "settings.json")
CODEX_CONFIG_PATH = os.path.join(".codex", "config.toml")
CODEX_HOOKS_PATH = os.path.join(".codex", "hooks.json")

CLAUDE_CONTINUE_HOOK_COMMAND = "printf '%s\\n' 'Are you done? If not, continue.' >&2; exit 2"
GEMINI_CONTINUE_HOOK_COMMAND = "printf '%s\\n' 'Are you done? If not, continue.' >&2; exit 2"
CODEX_CONTINUE_HOOK_COMMAND = (
    "cat >/dev/null; printf '%s\\n' "
    '\'{"decision":"block","reason":"Are you done? If not, continue."}\''
)


@dataclass
class InitOptions:
    continue_hook: bool = False
    constraints: str | None = None
    goal: str | None = None
    harness: str | None = None
    metric: str | None = None
    metric_description: str | None = None
    mode: str | None = None
    validation: str | None = None
    yes: bool = False


def has_explicit_problem_inputs(options: InitOptions) -> bool:
    return any(
        value is not None
        for value in [
            options.mode,
            options.goal,
            options.metric,
            options.metric_description,
            options.constraints,
            options.validation,
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


def parse_init_options(args: list[str]) -> InitOptions:
    options = InitOptions()
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--") and options.harness is None:
            if token not in SUPPORTED_HARNESSES:
                raise AutoevolveError(f'Unsupported harness "{token}"')
            options.harness = token
            index += 1
            continue

        if token == "--harness":
            harness = args[index + 1] if index + 1 < len(args) else ""
            if harness not in SUPPORTED_HARNESSES:
                raise AutoevolveError(f'Unsupported harness "{harness}"')
            options.harness = harness
            index += 2
            continue

        if token == "--mode":
            mode = args[index + 1] if index + 1 < len(args) else ""
            if mode not in {"now", "scaffold"}:
                raise AutoevolveError(f'Unsupported init mode "{mode}"')
            options.mode = mode
            index += 2
            continue

        if token == "--yes":
            options.yes = True
            index += 1
            continue

        if token == "--continue-hook":
            options.continue_hook = True
            index += 1
            continue

        if token == "--goal":
            options.goal = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue

        if token == "--metric":
            options.metric = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue

        if token == "--metric-description":
            options.metric_description = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue

        if token == "--validation":
            options.validation = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue

        if token == "--constraints":
            options.constraints = args[index + 1] if index + 1 < len(args) else ""
            index += 2
            continue

        if token.startswith("--"):
            raise AutoevolveError(f'Unknown option "{token}" for init.')
        raise AutoevolveError(f'Unexpected argument "{token}" for init.')

    return options


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


def choose_harness(initial_value: str) -> str:
    prompt = "Which coding agent should autoevolve target?"
    choice = click.prompt(
        f"{prompt} [{'/'.join(SUPPORTED_HARNESSES)}]",
        type=click.Choice(SUPPORTED_HARNESSES),
        default=initial_value,
        show_choices=False,
    )
    return str(choice)


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


def supports_continue_hook(harness: str) -> bool:
    return harness in {"claude", "codex", "gemini"}


def choose_continue_hook(harness: str) -> bool:
    click.echo("Continue Forever Hook")
    click.echo(f"Install a {harness} stop hook that prevents early termination.")
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


def parse_json_object_file(existing_text: str | None) -> dict[str, Any]:
    if existing_text is None:
        return {}
    parsed = json.loads(existing_text)
    if not isinstance(parsed, dict):
        raise AutoevolveError("settings file must contain a JSON object.")
    return dict(parsed)


def append_hook_entry(
    hooks_value: object, event_name: str, hook_entry: dict[str, Any]
) -> dict[str, Any]:
    hooks = dict(hooks_value) if isinstance(hooks_value, dict) else {}
    existing_entries = (
        list(hooks.get(event_name, [])) if isinstance(hooks.get(event_name), list) else []
    )
    if all(entry != hook_entry for entry in existing_entries):
        existing_entries.append(hook_entry)
    hooks[event_name] = existing_entries
    return hooks


def build_claude_continue_hook_settings(existing_text: str | None) -> str:
    settings = parse_json_object_file(existing_text)
    hook_entry = {"hooks": [{"type": "command", "command": CLAUDE_CONTINUE_HOOK_COMMAND}]}
    settings["hooks"] = append_hook_entry(settings.get("hooks"), "Stop", hook_entry)
    return f"{json.dumps(settings, indent=2)}\n"


def build_gemini_continue_hook_settings(existing_text: str | None) -> str:
    settings = parse_json_object_file(existing_text)
    hook_entry = {
        "hooks": [
            {
                "name": "autoevolve-continue",
                "type": "command",
                "command": GEMINI_CONTINUE_HOOK_COMMAND,
            }
        ]
    }
    settings["hooks"] = append_hook_entry(settings.get("hooks"), "AfterAgent", hook_entry)
    return f"{json.dumps(settings, indent=2)}\n"


def build_codex_hooks(existing_text: str | None) -> str:
    hooks_document = parse_json_object_file(existing_text)
    hook_entry = {"hooks": [{"type": "command", "command": CODEX_CONTINUE_HOOK_COMMAND}]}
    hooks_document["hooks"] = append_hook_entry(hooks_document.get("hooks"), "Stop", hook_entry)
    return f"{json.dumps(hooks_document, indent=2)}\n"


def build_codex_config(existing_text: str | None) -> str:
    if existing_text is None or not existing_text.strip():
        return "[features]\ncodex_hooks = true\n"
    if "codex_hooks" in existing_text:
        updated = existing_text.replace("codex_hooks = false", "codex_hooks = true")
        return f"{updated.strip()}\n"
    if "[features]" in existing_text:
        updated = existing_text.replace("[features]", "[features]\ncodex_hooks = true", 1)
        return f"{updated.strip()}\n"
    return f"{existing_text.strip()}\n\n[features]\ncodex_hooks = true\n"


def get_continue_hook_files(harness: str) -> list[str]:
    if harness == "claude":
        return [CLAUDE_SETTINGS_PATH]
    if harness == "gemini":
        return [GEMINI_SETTINGS_PATH]
    if harness == "codex":
        return [CODEX_CONFIG_PATH, CODEX_HOOKS_PATH]
    return []


def _read_if_exists(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_continue_hook_files(
    repo_root: str, harness: str, overwrite_by_default: bool
) -> list[str]:
    if harness == "claude":
        existing_text = _read_if_exists(resolve_repo_path(repo_root, CLAUDE_SETTINGS_PATH))
        wrote = write_file_with_confirmation(
            repo_root,
            CLAUDE_SETTINGS_PATH,
            build_claude_continue_hook_settings(existing_text),
            overwrite_by_default,
        )
        return [CLAUDE_SETTINGS_PATH] if wrote else []

    if harness == "gemini":
        existing_text = _read_if_exists(resolve_repo_path(repo_root, GEMINI_SETTINGS_PATH))
        wrote = write_file_with_confirmation(
            repo_root,
            GEMINI_SETTINGS_PATH,
            build_gemini_continue_hook_settings(existing_text),
            overwrite_by_default,
        )
        return [GEMINI_SETTINGS_PATH] if wrote else []

    if harness == "codex":
        existing_config_text = _read_if_exists(resolve_repo_path(repo_root, CODEX_CONFIG_PATH))
        existing_hooks_text = _read_if_exists(resolve_repo_path(repo_root, CODEX_HOOKS_PATH))
        wrote_config = write_file_with_confirmation(
            repo_root,
            CODEX_CONFIG_PATH,
            build_codex_config(existing_config_text),
            overwrite_by_default,
        )
        wrote_hooks = write_file_with_confirmation(
            repo_root,
            CODEX_HOOKS_PATH,
            build_codex_hooks(existing_hooks_text),
            overwrite_by_default,
        )
        written: list[str] = []
        if wrote_config:
            written.append(CODEX_CONFIG_PATH)
        if wrote_hooks:
            written.append(CODEX_HOOKS_PATH)
        return written

    return []


def run_init(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    options = parse_init_options(args)

    confirm_write(repo_root, options.yes)

    harness = options.harness or choose_harness("claude")
    if options.continue_hook and not supports_continue_hook(harness):
        raise AutoevolveError(f'Continue hooks are not supported for harness "{harness}".')

    continue_hook = supports_continue_hook(harness) and (
        options.continue_hook or (not options.yes and choose_continue_hook(harness))
    )

    prompt_text = build_harness_prompt(harness)
    existing_problem_path = resolve_repo_path(repo_root, ROOT_FILES.problem)
    has_existing_problem = os.path.exists(existing_problem_path)
    keep_existing_problem = (
        has_existing_problem
        and not has_explicit_problem_inputs(options)
        and (True if options.yes else choose_keep_existing_problem())
    )

    mode = None if keep_existing_problem else (options.mode or choose_setup_mode("now"))
    goal = ""
    metric = ""
    metric_description = ""
    constraints = ""
    validation = ""

    if mode == "now":
        goal = require_filled_value(options.goal or ask_goal(""), "goal")
        metric = require_filled_value(options.metric or ask_metric_spec(""), "metric")
        try:
            parse_primary_metric_spec(metric)
        except ValueError as error:
            raise AutoevolveError(str(error)) from error
        metric_description = (
            options.metric_description
            if options.metric_description is not None
            else (ask_metric_description("") if options.metric is None else "")
        )
        constraints = (
            options.constraints if options.constraints is not None else ask_constraints("")
        )
        validation = require_filled_value(options.validation or ask_validation(""), "validation")

    problem_template = (
        None
        if keep_existing_problem
        else build_problem_template(
            ProblemTemplateOptions(
                constraints=constraints,
                goal=goal,
                metric=metric,
                metric_description=metric_description,
                validation=validation,
            )
        )
    )

    prompt_path = HARNESS_PATHS[harness]
    harness_extra_files = get_continue_hook_files(harness) if continue_hook else []
    planned_write_files = [prompt_path, *harness_extra_files]

    review_lines = [f"Harness: {harness}"]
    if continue_hook:
        review_lines.append("Continue hook: enabled")
    if keep_existing_problem:
        review_lines.append(f"Problem: Keep existing {ROOT_FILES.problem}")
        review_lines.append(
            f"Files: keep {ROOT_FILES.problem}, write {', '.join(planned_write_files)}"
        )
    else:
        review_lines.append(
            f"Mode: {'Set up now' if mode == 'now' else 'Scaffold and finish with my agent'}"
        )
        review_lines.append(f"Files: {', '.join([ROOT_FILES.problem, *planned_write_files])}")
    click.echo("Review")
    click.echo("\n".join(review_lines))

    if not options.yes and not click.confirm("Write these files?", default=True):
        raise SystemExit(0)

    wrote_problem = (
        False
        if problem_template is None
        else write_file_with_confirmation(
            repo_root,
            ROOT_FILES.problem,
            problem_template,
            options.yes,
        )
    )
    wrote_prompt = write_file_with_confirmation(
        repo_root,
        prompt_path,
        prompt_text,
        options.yes,
    )
    wrote_harness_extras = (
        write_continue_hook_files(repo_root, harness, options.yes) if continue_hook else []
    )

    written_files: list[str] = []
    if wrote_problem:
        written_files.append(ROOT_FILES.problem)
    if wrote_prompt:
        written_files.append(prompt_path)
    written_files.extend(wrote_harness_extras)

    click.echo("Autoevolve initialized.")
    if mode == "scaffold":
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
