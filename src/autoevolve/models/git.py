from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitWorktree:
    path: Path
    branch: str | None
    head: str
    is_current: bool
    is_primary: bool


@dataclass(frozen=True)
class GitChangedPath:
    status: str
    path: str
    previous_path: str | None = None


@dataclass(frozen=True)
class GitDiff:
    patch: str
    shortstat: str
    changed_paths: tuple[GitChangedPath, ...]
