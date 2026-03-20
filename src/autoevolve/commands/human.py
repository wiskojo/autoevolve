from __future__ import annotations

import os

import click

from autoevolve.constants import ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root
from autoevolve.init_cmd import run_init
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.utils import (
    file_exists,
    find_prompt_files,
    has_experiment_files,
    parse_experiment_json,
    read_text_file,
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


__all__ = ["run_init", "run_validate"]
