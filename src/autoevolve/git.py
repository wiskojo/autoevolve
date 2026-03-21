import os
from collections.abc import Iterable
from pathlib import Path

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from autoevolve.models.git import GitChangedPath, GitDiff, GitWorktree


def open_repo(cwd: str | Path = ".") -> Repo:
    try:
        return Repo(cwd, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError) as error:
        raise RuntimeError("Not inside a git repository.") from error


def find_repo_root(cwd: str | Path = ".") -> Path:
    root = open_repo(cwd).working_tree_dir
    if root is None:
        raise RuntimeError("Bare repositories are not supported.")
    return Path(root).resolve()


def list_linked_worktrees(repo: Repo, current_path: str | Path | None = None) -> list[GitWorktree]:
    current = Path(current_path or os.getcwd()).resolve()
    primary = None
    if repo.working_tree_dir is not None:
        common_dir = Path(repo.git.rev_parse("--git-common-dir").strip())
        if not common_dir.is_absolute():
            common_dir = Path(repo.working_tree_dir, common_dir)
        primary = common_dir.resolve().parent
    lines = _git(repo, "worktree", "list", "--porcelain").splitlines()
    worktrees: list[GitWorktree] = []
    entry: dict[str, str] = {}

    for line in [*lines, ""]:
        if not line:
            if "worktree" in entry and "HEAD" in entry:
                path = Path(entry["worktree"]).resolve()
                worktrees.append(
                    GitWorktree(
                        path=path,
                        branch=(
                            entry["branch"].removeprefix("refs/heads/")
                            if "branch" in entry
                            else None
                        ),
                        head=entry["HEAD"],
                        is_current=path == current,
                        is_primary=path == primary,
                    )
                )
            entry = {}
            continue
        key, _, value = line.partition(" ")
        entry[key] = value

    return worktrees


def diff(repo: Repo, left: str, right: str, *, exclude: Iterable[str] = ()) -> GitDiff:
    pathspec = tuple(f":(exclude){path}" for path in exclude)
    patch = _git(repo, "diff", left, right, "--", *pathspec)
    shortstat = _git(repo, "diff", "--shortstat", left, right, "--", *pathspec).strip()
    raw_paths = _git(repo, "diff", "--name-status", "-M", left, right, "--", *pathspec)
    changed_paths: list[GitChangedPath] = []

    for line in raw_paths.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            changed_paths.append(
                GitChangedPath(status=status, previous_path=parts[1], path=parts[2])
            )
            continue
        if len(parts) >= 2:
            changed_paths.append(GitChangedPath(status=status, path=parts[1]))

    return GitDiff(patch=patch, shortstat=shortstat, changed_paths=tuple(changed_paths))


def _git(repo: Repo, *args: str) -> str:
    try:
        return repo.git.execute(
            ["git", *args],
            with_extended_output=False,
            as_process=False,
            stdout_as_string=True,
        )
    except GitCommandError as error:
        message = (error.stderr or error.stdout or str(error)).strip()
        raise RuntimeError(message) from error
