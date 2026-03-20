from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RootFiles:
    autoevolve: str = "AUTOEVOLVE.md"
    experiment: str = "EXPERIMENT.json"
    journal: str = "JOURNAL.md"
    problem: str = "PROBLEM.md"


ROOT_FILES = RootFiles()
MANAGED_EXPERIMENT_BRANCH_PREFIX = "autoevolve/"
MANAGED_WORKTREE_ROOT = str(Path.home() / ".autoevolve" / "worktrees")


def format_home_relative_path(path: str | os.PathLike[str]) -> str:
    expanded_path = Path(path).expanduser()
    home_path = Path.home()
    try:
        relative_path = expanded_path.relative_to(home_path)
    except ValueError:
        return str(expanded_path)

    relative_text = relative_path.as_posix()
    if not relative_text:
        return "~"
    return f"~/{relative_text}"
