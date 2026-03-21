from __future__ import annotations

import json
import os
import shutil

import click

from autoevolve.commands.shared import (
    get_managed_experiment_name,
    is_managed_experiment_branch,
    list_autoevolve_branches,
    list_repo_worktrees,
)
from autoevolve.constants import (
    MANAGED_EXPERIMENT_BRANCH_PREFIX,
    MANAGED_WORKTREE_ROOT,
    ROOT_FILES,
)
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import (
    find_repo_root,
    resolve_git_path,
    resolve_path_if_present,
    resolve_ref,
    run_git,
    run_git_with_git_dir,
    try_git_with_git_dir,
)
from autoevolve.models import WorktreeInfo
from autoevolve.utils import parse_experiment_json, read_text_file, resolve_repo_path, short_sha

JOURNAL_STUB_NOTE = "TODO: fill this in once you're done with your experiment."


def build_journal_stub(name: str) -> str:
    return f"# {name}\n\n{JOURNAL_STUB_NOTE}\n"


def build_experiment_stub(summary: str) -> str:
    return f"{json.dumps({'summary': summary, 'metrics': {}, 'references': []}, indent=2)}\n"


def normalize_managed_experiment_name(name: str) -> str:
    trimmed = name.strip()
    if trimmed.startswith(MANAGED_EXPERIMENT_BRANCH_PREFIX):
        return trimmed[len(MANAGED_EXPERIMENT_BRANCH_PREFIX) :]
    return trimmed


def is_managed_worktree_path(worktree_path: str) -> bool:
    root = resolve_path_if_present(MANAGED_WORKTREE_ROOT)
    resolved_worktree_path = resolve_path_if_present(worktree_path)
    return resolved_worktree_path.startswith(f"{root}{os.sep}")


def validate_managed_branch_name(repo_root: str, branch_name: str) -> None:
    try:
        run_git(repo_root, ["check-ref-format", f"refs/heads/{branch_name}"])
    except AutoevolveError as error:
        raise AutoevolveError(
            f'"{branch_name}" is not a valid managed experiment branch name.'
        ) from error


def resolve_managed_worktree_path(experiment_name: str) -> str:
    root = os.path.abspath(MANAGED_WORKTREE_ROOT)
    worktree_path = os.path.abspath(os.path.join(root, experiment_name))
    if worktree_path == root or not worktree_path.startswith(f"{root}{os.sep}"):
        raise AutoevolveError(f'"{experiment_name}" is not a valid experiment name.')
    return worktree_path


def describe_worktree_for_removal(worktree: WorktreeInfo) -> str:
    state = "missing" if worktree.is_missing else "dirty" if worktree.dirty else "clean"
    return (
        f"{worktree.path} ({worktree.branch or '(detached HEAD)'}, {state}, {worktree.short_head})"
    )


def delete_managed_experiment_branch_if_present(
    common_git_dir: str, branch_name: str | None
) -> None:
    if not branch_name or not is_managed_experiment_branch(branch_name):
        return
    exists = try_git_with_git_dir(
        os.path.expanduser("~"),
        common_git_dir,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
    )
    if exists is None:
        return
    run_git_with_git_dir(os.path.expanduser("~"), common_git_dir, ["branch", "-D", branch_name])


def run_start(name: str, summary: str, from_ref: str | None = None) -> None:
    repo_root = find_repo_root(os.getcwd())
    current_branch = run_git(repo_root, ["branch", "--show-current"]).strip()
    base_ref = from_ref or current_branch or "HEAD"
    base_sha = resolve_ref(repo_root, base_ref)
    branch_name = f"{MANAGED_EXPERIMENT_BRANCH_PREFIX}{name}"
    worktree_path = resolve_managed_worktree_path(name)
    validate_managed_branch_name(repo_root, branch_name)
    if any(branch.name == branch_name for branch in list_autoevolve_branches(repo_root)):
        raise AutoevolveError(f'Branch "{branch_name}" already exists.')
    if os.path.exists(worktree_path):
        raise AutoevolveError(f"Worktree path already exists: {worktree_path}")
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    run_git(repo_root, ["worktree", "add", "-b", branch_name, worktree_path, base_sha])
    with open(
        resolve_repo_path(worktree_path, ROOT_FILES.journal), "w", encoding="utf-8"
    ) as handle:
        handle.write(build_journal_stub(name))
    with open(
        resolve_repo_path(worktree_path, ROOT_FILES.experiment), "w", encoding="utf-8"
    ) as handle:
        handle.write(build_experiment_stub(summary))
    click.echo(f"Branch: {branch_name}")
    click.echo(f"Base: {base_ref}")
    click.echo(f"Path: {worktree_path}")


def run_record() -> None:
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


def run_clean(name: str | None = None, force: bool = False) -> None:
    repo_root = find_repo_root(os.getcwd())
    target_worktrees: list[WorktreeInfo] = []
    target_experiment_name = ""
    if name:
        target_experiment_name = normalize_managed_experiment_name(name)
        target_path = resolve_path_if_present(resolve_managed_worktree_path(target_experiment_name))
        target_worktree = next(
            (
                worktree
                for worktree in list_repo_worktrees(repo_root)
                if worktree.path == target_path
            ),
            None,
        )
        if (
            target_worktree is None
            or target_worktree.is_primary
            or not is_managed_worktree_path(target_worktree.path)
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
            if not worktree.is_primary and is_managed_worktree_path(worktree.path)
        ]
    if not target_worktrees:
        click.echo("No managed worktrees to clean.")
        return
    blocked_worktrees = [
        worktree for worktree in target_worktrees if worktree.is_missing or worktree.dirty
    ]
    if not force and blocked_worktrees:
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
    target_branches = [worktree.branch for worktree in target_worktrees]
    pruned_missing_worktrees = False
    for worktree in target_worktrees:
        if worktree.is_missing:
            if os.path.exists(worktree.path):
                shutil.rmtree(worktree.path, ignore_errors=True)
            if not pruned_missing_worktrees:
                run_git_with_git_dir(
                    os.path.expanduser("~"),
                    common_git_dir,
                    ["worktree", "prune", "--expire", "now"],
                )
                pruned_missing_worktrees = True
            continue
        remove_args = ["worktree", "remove"]
        if force or worktree.dirty:
            remove_args.append("--force")
        remove_args.append(worktree.path)
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
