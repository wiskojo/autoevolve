import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e.experiments import (
    EXPERIMENTS,
    build_experiment_object,
    build_journal_text,
    resolve_references,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures" / "playground"
AUTOEVOLVE_BIN = (PROJECT_ROOT / ".venv" / "bin" / "autoevolve").resolve()
HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
AGE_RE = re.compile(r"\b(?:in \d+(?:y|mo|w|d|h|m)|\d+(?:y|mo|w|d|h|m) ago|just now)\b")


@dataclass
class RepoFixture:
    root: Path
    home: Path

    def run(
        self,
        *args: str,
        expect_failure: bool = False,
        input_text: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(AUTOEVOLVE_BIN), *args],
            cwd=cwd or self.root,
            env=self.env,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        if expect_failure:
            assert result.returncode != 0, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def git(self, *args: str, cwd: Path | None = None, **env: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.root,
            env={**self.env, **env},
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    def normalize(self, text: str, *paths: Path) -> str:
        normalized = text
        all_paths = [*paths, self.root, self.home]
        for index, path in enumerate(all_paths, start=1):
            normalized = normalized.replace(str(path.resolve()), f"<PATH_{index}>")
            normalized = normalized.replace(str(path), f"<PATH_{index}>")
        sha_map: dict[str, str] = {}

        def replace_sha(match: re.Match[str]) -> str:
            sha = match.group(0)
            label = sha_map.setdefault(sha, f"<SHA_{len(sha_map) + 1}>")
            return label

        normalized = HEX_RE.sub(replace_sha, normalized)
        normalized = AGE_RE.sub("<AGE>", normalized)
        return re.sub(r"[ \t]+\n", "\n", normalized)

    def write_problem(self) -> None:
        (self.root / "PROBLEM.md").write_text(
            """# Problem

## Goal
Improve the Python ranking heuristic.

## Metric
max benchmark_score

## Constraints
Keep the project dependency-free.

## Validation
python3 scripts/validate.py
""",
            encoding="utf-8",
        )

    def commit_all(self, message: str, date: str = "2026-01-01T11:00:00Z") -> None:
        self.git("add", ".")
        self.git(
            "commit",
            "-m",
            message,
            GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_DATE=date,
        )

    def init_other(self) -> None:
        self.run("init", "--harness", "other", "--yes")
        self.write_problem()

    def managed_worktree_path(self, name: str) -> Path:
        return self.home / ".autoevolve" / "worktrees" / name

    def populate_history(self) -> dict[str, str]:
        main_ref_name = self.git("branch", "--show-current").strip()
        commits: dict[str, str] = {}
        for experiment in EXPERIMENTS:
            base_ref = experiment.base or main_ref_name
            experiment_ref = experiment.name
            self.git("checkout", base_ref)
            self.git("checkout", "-b", experiment_ref)
            base_commit = commits.get(experiment.base) if experiment.base else None
            references = resolve_references(experiment, commits)
            (self.root / "JOURNAL.md").write_text(
                build_journal_text(experiment, base_commit, references),
                encoding="utf-8",
            )
            (self.root / "EXPERIMENT.json").write_text(
                json.dumps(build_experiment_object(experiment, references), indent=2) + "\n",
                encoding="utf-8",
            )
            (self.root / "src" / "ranker.py").write_text(
                "def score_candidate(features):\n"
                f'    freshness = features["freshness"] * {experiment.weights.freshness}\n'
                f'    relevance = features["relevance"] * {experiment.weights.relevance}\n'
                f'    affordability = (1 - features["cost"]) * {experiment.weights.affordability}\n'
                "    return round(freshness + relevance + affordability, 3)\n",
                encoding="utf-8",
            )
            self.commit_all(f"Record {experiment.name} experiment", experiment.date)
            commits[experiment.name] = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", main_ref_name)
        return commits

    @property
    def env(self) -> dict[str, str]:
        pythonpath = str(SRC_ROOT)
        existing = os.environ.get("PYTHONPATH")
        if existing:
            pythonpath = os.pathsep.join([pythonpath, existing])
        return {
            **os.environ,
            "HOME": str(self.home),
            "PYTHONPATH": pythonpath,
        }


@pytest.fixture
def repo(tmp_path: Path) -> RepoFixture:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    shutil.copytree(FIXTURE_ROOT, root)
    home.mkdir()
    fixture = RepoFixture(root=root, home=home)
    fixture.git("init", "-b", "main")
    fixture.git("config", "user.name", "Autoevolve Tests")
    fixture.git("config", "user.email", "tests@example.com")
    fixture.git("add", ".")
    fixture.git("commit", "-m", "Initial fixture")
    return fixture


@pytest.fixture
def history_repo(repo: RepoFixture) -> RepoFixture:
    repo.init_other()
    repo.commit_all("Initialize autoevolve")
    repo.populate_history()
    return repo


@pytest.fixture
def history_repo_with_ongoing(history_repo: RepoFixture) -> RepoFixture:
    history_repo.run(
        "start",
        "alpha-branch",
        "Continue from the best recorded experiment.",
        "--from",
        "cross/hybrid-final",
    )
    alpha_path = history_repo.managed_worktree_path("alpha-branch")
    (alpha_path / "EXPERIMENT.json").write_text(
        json.dumps(
            {
                "summary": "Alpha branch is exploring the strongest recorded lineage.",
                "metrics": {},
                "references": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    history_repo.run(
        "start",
        "main-fork",
        "Fork directly from main without a recorded ancestor.",
        "--from",
        "main",
    )
    main_path = history_repo.managed_worktree_path("main-fork")
    (main_path / "EXPERIMENT.json").write_text(
        json.dumps(
            {
                "summary": "Main fork is still defining its first real experiment.",
                "metrics": {},
                "references": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return history_repo
