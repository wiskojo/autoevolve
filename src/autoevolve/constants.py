from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RootFiles:
    autoevolve: str = "AUTOEVOLVE.md"
    experiment: str = "EXPERIMENT.json"
    journal: str = "JOURNAL.md"
    problem: str = "PROBLEM.md"


ROOT_FILES = RootFiles()

AUTOEVOLVE_HOME_DIRNAME = ".autoevolve"
AUTOEVOLVE_HOME_DISPLAY_ROOT = f"~/{AUTOEVOLVE_HOME_DIRNAME}"
MANAGED_WORKTREE_DIRNAME = "worktrees"
MANAGED_WORKTREE_DISPLAY_ROOT = f"{AUTOEVOLVE_HOME_DISPLAY_ROOT}/{MANAGED_WORKTREE_DIRNAME}"
MANAGED_WORKTREE_ROOT = os.path.join(
    os.path.expanduser("~"),
    AUTOEVOLVE_HOME_DIRNAME,
    MANAGED_WORKTREE_DIRNAME,
)

HARNESS_PATHS = {
    "claude": os.path.join(".claude", "skills", "autoevolve", "SKILL.md"),
    "codex": os.path.join(".codex", "skills", "autoevolve", "SKILL.md"),
    "gemini": os.path.join(".gemini", "skills", "autoevolve", "SKILL.md"),
    "other": ROOT_FILES.autoevolve,
}

SUPPORTED_HARNESSES = tuple(HARNESS_PATHS.keys())
