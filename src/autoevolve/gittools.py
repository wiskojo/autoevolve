from __future__ import annotations

import os
from collections.abc import Iterable

from git import Git, Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from autoevolve.errors import AutoevolveError


def find_repo_root(cwd: str) -> str:
    try:
        repo = Repo(cwd, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError) as error:
        raise AutoevolveError("not inside a git repository") from error
    if repo.working_tree_dir is None:
        raise AutoevolveError("not inside a git repository")
    return os.fspath(repo.working_tree_dir)


def open_repo(repo_root: str) -> Repo:
    return Repo(repo_root)


def _clean_error_message(error: GitCommandError) -> str:
    stderr = (getattr(error, "stderr", "") or "").strip()
    stdout = (getattr(error, "stdout", "") or "").strip()
    if stderr:
        return stderr
    if stdout:
        return stdout
    return str(error)


def run_git(repo_root: str, args: Iterable[str], cwd: str | None = None) -> str:
    git = Git(cwd or repo_root)
    command = ["git", *list(args)]
    try:
        result = git.execute(
            command,
            with_extended_output=False,
            as_process=False,
            stdout_as_string=True,
        )
    except GitCommandError as error:
        raise AutoevolveError(_clean_error_message(error)) from error
    return result if isinstance(result, str) else str(result)


def try_git(repo_root: str, args: Iterable[str], cwd: str | None = None) -> str | None:
    git = Git(cwd or repo_root)
    command = ["git", *list(args)]
    try:
        result = git.execute(
            command,
            with_extended_output=False,
            as_process=False,
            stdout_as_string=True,
        )
    except GitCommandError:
        return None
    return result if isinstance(result, str) else str(result)


def run_git_with_git_dir(working_dir: str, git_dir: str, args: Iterable[str]) -> str:
    command = [f"--git-dir={git_dir}", *list(args)]
    return run_git(working_dir, command, cwd=working_dir)


def try_git_with_git_dir(working_dir: str, git_dir: str, args: Iterable[str]) -> str | None:
    command = [f"--git-dir={git_dir}", *list(args)]
    return try_git(working_dir, command, cwd=working_dir)


def resolve_path_if_present(target_path: str) -> str:
    if not os.path.exists(target_path):
        return os.path.abspath(target_path)
    return os.path.realpath(target_path)
