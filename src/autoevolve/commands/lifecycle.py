from __future__ import annotations

import os
import shutil
from typing import Any

import click

from autoevolve.commands.shared import (
    MANAGED_EXPERIMENT_BRANCH_PREFIX,
    build_experiment_stub,
    build_journal_stub,
    delete_managed_experiment_branch_if_present,
    describe_worktree_for_removal,
    find_repo_worktree_by_path,
    get_managed_experiment_name,
    is_managed_experiment_branch,
    is_managed_worktree_path,
    list_autoevolve_branches,
    list_repo_worktrees,
    normalize_managed_experiment_name,
    parse_ref_value,
    resolve_git_path,
    resolve_managed_worktree_path,
    resolve_new_experiment_base_ref,
    validate_managed_branch_name,
)
from autoevolve.constants import MANAGED_WORKTREE_ROOT, ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root, run_git, run_git_with_git_dir
from autoevolve.models import CleanOptions, StartOptions
from autoevolve.utils import parse_experiment_json, read_text_file, resolve_repo_path, short_sha


def parse_start_options(args: list[str]) -> StartOptions:
    from_ref = ""
    name = ""
    summary = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--from":
            from_ref = parse_ref_value("--from", args[index + 1] if index + 1 < len(args) else None)
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for start.')
        if not name:
            name = token.strip()
            index += 1
            continue
        if not summary:
            summary = token.strip()
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for start.')

    if not name:
        raise AutoevolveError(
            "start requires an experiment name and summary, for example: "
            'autoevolve start tune-thresholds "Try a tighter threshold sweep"'
        )
    if not summary:
        raise AutoevolveError(
            "start requires an experiment summary, for example: "
            'autoevolve start tune-thresholds "Try a tighter threshold sweep"'
        )
    return StartOptions(from_ref=from_ref, name=name, summary=summary)


def parse_record_args(args: list[str]) -> None:
    if args:
        raise AutoevolveError(f'Unexpected argument "{args[0]}" for record.')


def parse_clean_options(args: list[str]) -> CleanOptions:
    force = False
    name = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-f", "--force"}:
            force = True
            index += 1
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for clean.')
        if not name:
            name = token.strip()
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for clean.')
    return CleanOptions(force=force, name=name)


def run_start(args: list[str]) -> None:
    options = parse_start_options(args)
    repo_root = find_repo_root(os.getcwd())
    base_ref = resolve_new_experiment_base_ref(repo_root, options.from_ref)
    branch_name = f"{MANAGED_EXPERIMENT_BRANCH_PREFIX}{options.name}"
    worktree_path = resolve_managed_worktree_path(options.name)
    validate_managed_branch_name(repo_root, branch_name)
    if any(branch["name"] == branch_name for branch in list_autoevolve_branches(repo_root)):
        raise AutoevolveError(f'Branch "{branch_name}" already exists.')
    if os.path.exists(worktree_path):
        raise AutoevolveError(f"Worktree path already exists: {worktree_path}")
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    run_git(
        repo_root,
        ["worktree", "add", "-b", branch_name, worktree_path, base_ref["sha"]],
    )
    with open(
        resolve_repo_path(worktree_path, ROOT_FILES.journal), "w", encoding="utf-8"
    ) as handle:
        handle.write(build_journal_stub(options.name))
    with open(
        resolve_repo_path(worktree_path, ROOT_FILES.experiment), "w", encoding="utf-8"
    ) as handle:
        handle.write(build_experiment_stub(options.summary))
    click.echo(f"Branch: {branch_name}")
    click.echo(f"Base: {base_ref['ref']}")
    click.echo(f"Path: {worktree_path}")


def run_record(args: list[str]) -> None:
    parse_record_args(args)
    repo_root = find_repo_root(os.getcwd())
    branch_name = run_git(repo_root, ["branch", "--show-current"]).strip()
    if not branch_name:
        raise AutoevolveError("record requires an attached branch.")
    if not is_managed_experiment_branch(branch_name):
        raise AutoevolveError(
            "record only works on managed autoevolve experiment branches "
            f"({MANAGED_EXPERIMENT_BRANCH_PREFIX}<name>)."
        )
    managed_root = os.path.realpath(os.path.abspath(MANAGED_WORKTREE_ROOT))
    resolved_repo_root = os.path.realpath(os.path.abspath(repo_root))
    if resolved_repo_root != managed_root and not resolved_repo_root.startswith(
        f"{managed_root}{os.sep}"
    ):
        raise AutoevolveError(
            f"record must be run from a managed autoevolve worktree under {managed_root}."
        )
    git_dir = resolve_git_path(repo_root, "--git-dir")
    common_git_dir = resolve_git_path(repo_root, "--git-common-dir")
    if git_dir == common_git_dir:
        raise AutoevolveError("record refuses to remove the primary worktree.")
    journal_text = read_text_file(repo_root, ROOT_FILES.journal).strip()
    experiment_text = read_text_file(repo_root, ROOT_FILES.experiment)
    parsed_experiment = parse_experiment_json(experiment_text)
    experiment_name = get_managed_experiment_name(branch_name)
    if journal_text == build_journal_stub(experiment_name).strip():
        raise AutoevolveError(f"Replace the {ROOT_FILES.journal} stub before committing.")
    if not run_git(repo_root, ["status", "--porcelain"]).strip():
        raise AutoevolveError("No changes to commit.")
    commit_message = next(
        (line.strip() for line in parsed_experiment.summary.splitlines() if line.strip()),
        "",
    )
    if not commit_message:
        raise AutoevolveError(f"{ROOT_FILES.experiment} summary must not be empty.")
    run_git(repo_root, ["add", "."])
    run_git(repo_root, ["commit", "-m", commit_message])
    commit_sha = run_git(repo_root, ["rev-parse", "HEAD"]).strip()
    run_git_with_git_dir(
        os.path.expanduser("~"),
        common_git_dir,
        ["worktree", "remove", resolved_repo_root],
    )
    click.echo(f"Committed {branch_name} at {short_sha(commit_sha)}.")
    click.echo(f"Removed worktree: {resolved_repo_root}")


def run_clean(args: list[str]) -> None:
    options = parse_clean_options(args)
    repo_root = find_repo_root(os.getcwd())
    target_worktrees: list[dict[str, Any]] = []
    target_experiment_name = ""
    if options.name:
        target_experiment_name = normalize_managed_experiment_name(options.name)
        target_worktree = find_repo_worktree_by_path(
            repo_root, resolve_managed_worktree_path(target_experiment_name)
        )
        if (
            target_worktree is None
            or target_worktree["isPrimary"]
            or not is_managed_worktree_path(target_worktree["path"])
        ):
            raise AutoevolveError(
                "No managed experiment worktree named "
                f'"{target_experiment_name}" found for this repository.'
            )
        target_worktrees = [target_worktree]
    else:
        target_worktrees = [
            worktree
            for worktree in list_repo_worktrees(repo_root)
            if not worktree["isPrimary"] and is_managed_worktree_path(worktree["path"])
        ]
    if not target_worktrees:
        click.echo("No managed worktrees to clean.")
        return
    blocked_worktrees = [
        worktree for worktree in target_worktrees if worktree["isMissing"] or worktree["dirty"]
    ]
    if not options.force and blocked_worktrees:
        reason = (
            "Refusing to remove a dirty or missing linked worktree without --force:"
            if len(blocked_worktrees) == 1
            else "Refusing to remove dirty or missing linked worktrees without --force:"
        )
        raise AutoevolveError(
            reason
            + "\n"
            + "\n".join(
                f"  {describe_worktree_for_removal(worktree)}" for worktree in blocked_worktrees
            )
        )
    common_git_dir = resolve_git_path(repo_root, "--git-common-dir")
    target_branches = [worktree["branch"] for worktree in target_worktrees]
    pruned_missing_worktrees = False
    for worktree in target_worktrees:
        if worktree["isMissing"]:
            if os.path.exists(worktree["path"]):
                shutil.rmtree(worktree["path"], ignore_errors=True)
            if not pruned_missing_worktrees:
                run_git_with_git_dir(
                    os.path.expanduser("~"),
                    common_git_dir,
                    ["worktree", "prune", "--expire", "now"],
                )
                pruned_missing_worktrees = True
            continue
        remove_args = ["worktree", "remove"]
        if options.force or worktree["dirty"]:
            remove_args.append("--force")
        remove_args.append(worktree["path"])
        run_git_with_git_dir(os.path.expanduser("~"), common_git_dir, remove_args)
    for branch_name in target_branches:
        delete_managed_experiment_branch_if_present(common_git_dir, branch_name)
    click.echo(
        "Removed "
        f"{len(target_worktrees)} linked worktree"
        f"{'s' if len(target_worktrees) != 1 else ''} for this repository."
    )
    if target_experiment_name:
        click.echo(f"Experiment: {target_experiment_name}")
    for worktree in target_worktrees:
        click.echo(f"  {describe_worktree_for_removal(worktree)}")


__all__ = ["run_clean", "run_record", "run_start"]
