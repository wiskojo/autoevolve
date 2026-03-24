import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from autoevolve.models.git import GitChangedPath, GitCommit, GitDiff, GitWorktree


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


def list_experiment_commits(repo: Repo, path: str, limit: int | None = None) -> list[GitCommit]:
    args = ["log", "--all", "--format=%H%x09%cI%x09%P"]
    if limit is not None:
        args.append(f"-n{limit}")
    args.extend(["--", path])
    lines = _git(repo, *args).splitlines()
    commits: list[GitCommit] = []
    seen: set[str] = set()
    for line in lines:
        if not line:
            continue
        sha, date, parents = (line.split("\t", 2) + ["", ""])[:3]
        if sha in seen:
            continue
        seen.add(sha)
        commits.append(
            GitCommit(
                sha=sha,
                date=normalize_commit_date(date),
                parents=tuple(parent for parent in parents.split() if parent),
            )
        )
    return commits


def normalize_commit_date(value: str) -> str:
    return value[:-1] + "+00:00" if value.endswith("Z") else value


def read_text_blobs(repo: Repo, refs: Iterable[str], path: str) -> dict[str, str | None]:
    requested = list(dict.fromkeys(refs))
    if not requested:
        return {}
    command = [
        "git",
        "-C",
        str(Path(repo.working_tree_dir or ".").resolve()),
        "cat-file",
        "--batch",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write("".join(f"{ref}:{path}\n" for ref in requested).encode("utf-8"))
    process.stdin.close()

    result: dict[str, str | None] = {}
    for ref in requested:
        header = process.stdout.readline()
        if not header:
            raise RuntimeError("git cat-file returned incomplete output.")
        if header.endswith(b" missing\n"):
            result[ref] = None
            continue
        parts = header.rstrip(b"\n").split()
        if len(parts) != 3 or parts[1] != b"blob":
            raise RuntimeError(header.decode("utf-8", errors="replace").strip())
        size = int(parts[2])
        data = process.stdout.read(size)
        process.stdout.read(1)
        result[ref] = data.decode("utf-8")

    stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(stderr or "git cat-file failed.")
    return result


def read_text_blob(repo: Repo, ref: str, path: str) -> str | None:
    return read_text_blobs(repo, [ref], path).get(ref)


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
