from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from dirty_equals import IsPartialDict, IsStr
from inline_snapshot import snapshot

from autoevolve.prompt import build_protocol_body
from tests.experiments import (
    EXPERIMENTS,
    build_experiment_object,
    build_journal_text,
    resolve_references,
    with_prefix,
)

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parent.parent
SRC_ROOT = PROJECT_ROOT / "src"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "playground"
HEX_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
AGE_RE = re.compile(r"\((?:just now|[0-9]+[a-z]+ ago)\)")


def normalize_text(text: str, *paths: str | Path, normalize_age: bool = False) -> str:
    normalized = text
    for index, raw_path in enumerate(paths, start=1):
        label = f"<PATH_{index}>"
        path_text = str(raw_path)
        candidates = [str(Path(raw_path).resolve()), path_text]
        if path_text.startswith("/var/"):
            candidates.append(f"/private{path_text}")
        for candidate in candidates:
            normalized = normalized.replace(candidate, label)

    sha_map: dict[str, str] = {}

    def replace_sha(match: re.Match[str]) -> str:
        sha = match.group(0)
        if sha not in sha_map:
            sha_map[sha] = f"<SHA_{len(sha_map) + 1}>"
        return sha_map[sha]

    normalized = HEX_RE.sub(replace_sha, normalized)
    if normalize_age:
        normalized = AGE_RE.sub("(<AGE>)", normalized)
    return normalized


def assert_click_error(stderr: str, message: str) -> None:
    assert f"Error: {message}" in stderr


def run(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    expect_failure: bool = False,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    existing_pythonpath = merged_env.get("PYTHONPATH")
    pythonpath_entries = [str(SRC_ROOT)]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    merged_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    if env:
        merged_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "autoevolve", *args],
        cwd=cwd,
        env=merged_env,
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


def run_git(cwd: str | Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def run_git_with_env(cwd: str | Path, args: list[str], extra_env: dict[str, str]) -> str:
    env = os.environ.copy()
    env.update(extra_env)
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def read_json_file(path: str | Path) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def branch_exists(repo_path: str | Path, branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stdout + result.stderr
    return result.returncode == 0


def current_branch(repo_path: str | Path) -> str:
    return run_git(repo_path, ["branch", "--show-current"]).strip()


def write_experiment(
    repo_path: str | Path,
    journal_text: str,
    experiment_json: dict[str, object],
    weights: tuple[float, float, float],
) -> None:
    Path(repo_path, "JOURNAL.md").write_text(journal_text, encoding="utf-8")
    Path(repo_path, "EXPERIMENT.json").write_text(
        json.dumps(experiment_json, indent=2) + "\n",
        encoding="utf-8",
    )
    Path(repo_path, "src", "ranker.py").write_text(
        "def score_candidate(features):\n"
        f'    freshness = features["freshness"] * {weights[1]}\n'
        f'    relevance = features["relevance"] * {weights[2]}\n'
        f'    affordability = (1 - features["cost"]) * {weights[0]}\n'
        "    return round(freshness + relevance + affordability, 3)\n",
        encoding="utf-8",
    )


def init_repo_from_fixture() -> str:
    temp_root = tempfile.mkdtemp(prefix="autoevolve-e2e-")
    repo_path = Path(temp_root) / "target"
    shutil.copytree(FIXTURE_PATH, repo_path)
    run_git(repo_path, ["init"])
    run_git(repo_path, ["config", "user.name", "Autoevolve E2E"])
    run_git(repo_path, ["config", "user.email", "autoevolve-e2e@example.com"])
    run_git(repo_path, ["add", "."])
    run_git(repo_path, ["commit", "-m", "Initial fixture"])
    return str(repo_path)


def commit_all(repo_path: str | Path, message: str, date: str = "2026-01-01T11:00:00Z") -> None:
    run_git(repo_path, ["add", "."])
    run_git_with_env(
        repo_path,
        ["commit", "-m", message],
        {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )


def init_other_now(repo_path: str | Path) -> None:
    run(
        [
            "init",
            "other",
            "--mode",
            "now",
            "--yes",
            "--goal",
            "Improve the Python ranking heuristic",
            "--validation",
            "python3 scripts/validate.py",
            "--metric",
            "max benchmark_score",
            "--constraints",
            "Keep the project dependency-free",
        ],
        cwd=repo_path,
    )


def populate_synthetic_branches(repo_path: str | Path) -> dict[str, str]:
    main_branch = current_branch(repo_path)
    commit_by_branch: dict[str, str] = {}
    for experiment in EXPERIMENTS:
        base_ref = with_prefix(experiment.base) if experiment.base else main_branch
        branch_name = with_prefix(experiment.branch)
        assert base_ref is not None
        assert branch_name is not None
        run_git(repo_path, ["checkout", base_ref])
        run_git(repo_path, ["checkout", "-b", branch_name])
        base_commit = commit_by_branch.get(experiment.base) if experiment.base else None
        resolved_references = resolve_references(experiment, commit_by_branch)
        write_experiment(
            repo_path,
            build_journal_text(experiment, base_commit, resolved_references),
            build_experiment_object(experiment, resolved_references),
            (
                experiment.weights.affordability,
                experiment.weights.freshness,
                experiment.weights.relevance,
            ),
        )
        commit_all(repo_path, f"Record {experiment.branch} experiment", experiment.date)
        commit_by_branch[experiment.branch] = run_git(repo_path, ["rev-parse", "HEAD"]).strip()
    run_git(repo_path, ["checkout", main_branch])
    return commit_by_branch


def test_other_init_and_help() -> None:
    repo_path = init_repo_from_fixture()
    result = run(
        [
            "init",
            "other",
            "--mode",
            "now",
            "--yes",
            "--goal",
            "Improve the Python ranking heuristic",
            "--validation",
            "python3 scripts/validate.py",
            "--metric",
            "max benchmark_score",
            "--constraints",
            "Keep the project dependency-free",
        ],
        cwd=repo_path,
    )
    assert normalize_text(result.stdout, repo_path) == snapshot("""\
Repository
<PATH_1>
Review
Harness: other
Mode: Set up now
Files: PROBLEM.md, AUTOEVOLVE.md
Autoevolve initialized.

Repository: <PATH_1>

Files written:
  - PROBLEM.md
  - AUTOEVOLVE.md

Next: ask your agent to verify setup and begin the experiment loop.

For example:
  Start autoevolve.
""")
    assert Path(repo_path, "PROBLEM.md").exists()
    assert Path(repo_path, "AUTOEVOLVE.md").exists()

    validate = run(["validate"], cwd=repo_path)
    assert validate.stdout == snapshot(
        "OK: repository matches the autoevolve protocol.\n"
        "No current experiment record found. Add JOURNAL.md and "
        "EXPERIMENT.json in the first experiment commit.\n"
    )

    top_help = run([], cwd=repo_path)
    assert top_help.stdout == snapshot("""\
Usage: autoevolve [OPTIONS] COMMAND [ARGS]...

  Git-backed experiment loops for coding agents.

Options:
  --help  Show this message and exit.

Human:
  init      Scaffold PROBLEM.md and agent instructions.
  validate  Validate that the repo is correctly initialized for autoevolve.

Lifecycle:
  start   Create a managed experiment branch and worktree.
  record  Validate, commit, and remove the current managed worktree.
  clean   Remove stale managed worktrees for this repository.

Inspect:
  status   Show the current experiment snapshot.
  list     List recent experiments.
  show     Show JOURNAL.md and EXPERIMENT.json for one ref.
  compare  Compare two experiment commits.
  graph    Traverse lineage around one ref.

Analytics:
  recent  Return the most recent experiments.
  best    Return the top experiments for one objective.
  pareto  Return the Pareto frontier for the selected objectives.

Examples:
  autoevolve init
  autoevolve start tune-thresholds "Try a tighter threshold sweep" --from 07f1844
  autoevolve record
  autoevolve list
  autoevolve recent --limit 5
  autoevolve best --max benchmark_score --limit 5

Run "autoevolve <command> --help" for command-specific details.
""")

    legacy_experiments = run(["experiments"], cwd=repo_path, expect_failure=True)
    assert_click_error(legacy_experiments.stderr, "No such command 'experiments'.")


def test_other_scaffold_init() -> None:
    repo_path = init_repo_from_fixture()
    result = run(["init", "other", "--mode", "scaffold", "--yes"], cwd=repo_path)
    assert normalize_text(result.stdout, repo_path) == snapshot("""\
Repository
<PATH_1>
Review
Harness: other
Mode: Scaffold and finish with my agent
Files: PROBLEM.md, AUTOEVOLVE.md
Autoevolve initialized.

Repository: <PATH_1>

Files written:
  - PROBLEM.md
  - AUTOEVOLVE.md

Next: ask your agent to finish setup.

For example:
  Follow the setup instructions for autoevolve.
""")


def test_keep_existing_problem_init() -> None:
    repo_path = init_repo_from_fixture()
    existing_problem = """# Problem

## Goal
Keep the current problem definition.

## Metric
max benchmark_score

## Constraints
- Keep this file unchanged.

## Validation
python3 scripts/validate.py
"""
    Path(repo_path, "PROBLEM.md").write_text(existing_problem, encoding="utf-8")
    result = run(["init", "other", "--yes"], cwd=repo_path)
    assert normalize_text(result.stdout, repo_path) == snapshot("""\
Repository
<PATH_1>
Review
Harness: other
Problem: Keep existing PROBLEM.md
Files: keep PROBLEM.md, write AUTOEVOLVE.md
Autoevolve initialized.

Repository: <PATH_1>

Files written:
  - AUTOEVOLVE.md

Next: ask your agent to verify setup and begin the experiment loop.

For example:
  Start autoevolve.
""")
    assert Path(repo_path, "PROBLEM.md").read_text(encoding="utf-8") == existing_problem


def test_legacy_commands_removed() -> None:
    repo_path = init_repo_from_fixture()
    for command in ["log", "update", "lineage", "results", "search"]:
        result = run([command], cwd=repo_path, expect_failure=True)
        assert_click_error(result.stderr, f"No such command '{command}'.")


def test_metric_protocol_validation() -> None:
    repo_path = init_repo_from_fixture()
    run(
        [
            "init",
            "other",
            "--mode",
            "now",
            "--yes",
            "--goal",
            "Set up a metric-driven repo",
            "--validation",
            "python3 scripts/validate.py",
            "--metric",
            "max benchmark_score",
            "--constraints",
            "",
        ],
        cwd=repo_path,
    )
    Path(repo_path, "PROBLEM.md").write_text(
        """# Problem

## Goal
Keep the current problem definition.

## Metric
benchmark_score

## Constraints
- Keep this file unchanged.

## Validation
python3 scripts/validate.py
""",
        encoding="utf-8",
    )
    invalid_problem = run(["validate"], cwd=repo_path, expect_failure=True)
    assert (
        'PROBLEM.md section "Metric" must start with "max <metric>" or "min <metric>"'
        in invalid_problem.stdout
    )

    missing_default_best = run(["best"], cwd=repo_path, expect_failure=True)
    assert (
        "best requires an explicit objective, or a valid PROBLEM.md primary metric."
        in missing_default_best.stderr
    )

    Path(repo_path, "PROBLEM.md").write_text(
        """# Problem

## Goal
Keep the current problem definition.

## Metric
max benchmark_score

## Constraints
- Keep this file unchanged.

## Validation
python3 scripts/validate.py
""",
        encoding="utf-8",
    )
    Path(repo_path, "JOURNAL.md").write_text(
        "# Notes\n\nTried a weaker metric payload.\n", encoding="utf-8"
    )
    Path(repo_path, "EXPERIMENT.json").write_text(
        json.dumps(
            {
                "summary": "Recorded the wrong metric set.",
                "metrics": {"runtime_sec": 1.23},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    missing_metric = run(["validate"], cwd=repo_path, expect_failure=True)
    assert (
        'EXPERIMENT.json must record the primary metric "benchmark_score" '
        "declared in PROBLEM.md (max benchmark_score)"
    ) in missing_metric.stdout


def test_metric_description_init() -> None:
    repo_path = init_repo_from_fixture()
    run(
        [
            "init",
            "other",
            "--mode",
            "now",
            "--yes",
            "--goal",
            "Set up a repo with metric notes",
            "--validation",
            "python3 scripts/validate.py",
            "--metric",
            "max benchmark_score",
            "--metric-description",
            "Higher is better. Computed by python3 scripts/validate.py.",
            "--constraints",
            "",
        ],
        cwd=repo_path,
    )
    problem_text = Path(repo_path, "PROBLEM.md").read_text(encoding="utf-8")
    assert (
        "## Metric\n"
        "max benchmark_score\n\n"
        "Higher is better. Computed by python3 scripts/validate.py."
    ) in problem_text
    run(["validate"], cwd=repo_path)


def test_protocol_prompt_lifecycle_guidance() -> None:
    prompt = build_protocol_body()
    assert "autoevolve start <name> <summary> [--from <ref>]" in prompt
    assert "autoevolve record" in prompt
    assert "autoevolve clean" in prompt
    assert "autoevolve recent" in prompt
    assert "autoevolve best" in prompt
    assert "autoevolve compare" in prompt


def test_synthetic_branches_inspect_and_analytics() -> None:
    repo_path = init_repo_from_fixture()
    init_other_now(repo_path)
    commit_all(repo_path, "Initialize autoevolve")
    main_branch = current_branch(repo_path)
    commit_by_branch = populate_synthetic_branches(repo_path)
    experiment_list = run(["list"], cwd=repo_path)
    assert normalize_text(experiment_list.stdout) == snapshot("""\
<SHA_1>  2026-01-01T12:11:00+00:00  Record cross/hybrid-final experiment
  summary: Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
  metrics: benchmark_score=0.918, runtime_sec=1.08
  journal: Hypothesis: Cross-pollinate the strongest island A, B, and C ideas without doing a formal git merge.

<SHA_2>  2026-01-01T12:10:00+00:00  Record island-a/balanced-v2 experiment
  summary: Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
  metrics: benchmark_score=0.913, runtime_sec=1.03
  journal: Hypothesis: Preserve the cost gains from island A while borrowing island C's premium guard.

<SHA_3>  2026-01-01T12:09:00+00:00  Record island-c/overfit-premium experiment
  summary: The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
  metrics: benchmark_score=0.821, runtime_sec=0.74
  journal: Hypothesis: Try an aggressive premium-heavy setting even if it risks overfitting the benchmark.

<SHA_4>  2026-01-01T12:08:00+00:00  Record island-a/cost-penalty experiment
  summary: The stronger cost penalty crossed the 0.90 threshold but made validation slower.
  metrics: benchmark_score=0.901, runtime_sec=1.12
  journal: Hypothesis: Increase the cost penalty on island A while keeping island B's stale recovery in mind.

<SHA_5>  2026-01-01T12:07:00+00:00  Record island-c/premium-guard experiment
  summary: Premium guard was solid and balanced relevance against cheaper-case pressure.
  metrics: benchmark_score=0.894, runtime_sec=0.92
  journal: Hypothesis: Pull island C back from over-indexing on relevance while borrowing island A's cheaper-case signal.

<SHA_6>  2026-01-01T12:06:00+00:00  Record island-a/cheap-priority experiment
  summary: Prioritizing cheaper items improved the cheap-case fit without fully giving up stale recovery.
  metrics: benchmark_score=0.887, runtime_sec=1.04
  journal: Hypothesis: Push affordability further on island A while checking it against the stale-recovery branch.

<SHA_7>  2026-01-01T12:05:00+00:00  Record island-c/relevance-lean experiment
  summary: A relevance-heavy mix helped somewhat and became the fastest branch to validate.
  metrics: benchmark_score=0.861, runtime_sec=0.8
  journal: Hypothesis: Lean harder on relevance while keeping a reference to island B's freshness behavior.

<SHA_8>  2026-01-01T12:04:00+00:00  Record island-b/stale-recovery experiment
  summary: Stale recovery helped and picked up some of the cheap-case gains from island A.
  metrics: benchmark_score=0.879, runtime_sec=0.96
  journal: Hypothesis: Keep the freshness-heavy island but borrow the affordability intuition from island A.

<SHA_9>  2026-01-01T12:03:00+00:00  Record island-a/rebalance-weights experiment
  summary: Weight rebalance improved the benchmark noticeably at a small runtime cost.
  metrics: benchmark_score=0.872, runtime_sec=1.01
  journal: Hypothesis: Rebalance toward affordability on island A after the baseline split.

<SHA_10>  2026-01-01T12:02:00+00:00  Record island-c/clip-premium experiment
  summary: Premium clipping was only a minor improvement over baseline but stayed cheap to validate.
  metrics: benchmark_score=0.842, runtime_sec=0.86
  journal: Hypothesis: Reduce the freshness term on a third island to avoid overshooting premium examples.
""")

    limited_list = run(["list", "--limit", "2"], cwd=repo_path)
    assert normalize_text(limited_list.stdout) == snapshot("""\
<SHA_1>  2026-01-01T12:11:00+00:00  Record cross/hybrid-final experiment
  summary: Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
  metrics: benchmark_score=0.918, runtime_sec=1.08
  journal: Hypothesis: Cross-pollinate the strongest island A, B, and C ideas without doing a formal git merge.

<SHA_2>  2026-01-01T12:10:00+00:00  Record island-a/balanced-v2 experiment
  summary: Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
  metrics: benchmark_score=0.913, runtime_sec=1.03
  journal: Hypothesis: Preserve the cost gains from island A while borrowing island C's premium guard.
""")

    bogus_list = run(["list", "--bogus"], cwd=repo_path, expect_failure=True)
    assert_click_error(bogus_list.stderr, "No such option: --bogus")

    legacy_list_selectors = run(["list", "--text", "premium"], cwd=repo_path, expect_failure=True)
    assert_click_error(legacy_list_selectors.stderr, "No such option: --text")

    recent = run(["recent"], cwd=repo_path)
    assert normalize_text(recent.stdout) == snapshot("""\
sha	date	subject	tips	metrics	summary
<SHA_1>	2026-01-01T12:11:00+00:00	Record cross/hybrid-final experiment	cross/hybrid-final	benchmark_score=0.918, runtime_sec=1.08	Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
<SHA_2>	2026-01-01T12:10:00+00:00	Record island-a/balanced-v2 experiment	island-a/balanced-v2	benchmark_score=0.913, runtime_sec=1.03	Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
<SHA_3>	2026-01-01T12:09:00+00:00	Record island-c/overfit-premium experiment	island-c/overfit-premium	benchmark_score=0.821, runtime_sec=0.74	The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
<SHA_4>	2026-01-01T12:08:00+00:00	Record island-a/cost-penalty experiment	island-a/cost-penalty	benchmark_score=0.901, runtime_sec=1.12	The stronger cost penalty crossed the 0.90 threshold but made validation slower.
<SHA_5>	2026-01-01T12:07:00+00:00	Record island-c/premium-guard experiment	island-c/premium-guard	benchmark_score=0.894, runtime_sec=0.92	Premium guard was solid and balanced relevance against cheaper-case pressure.
<SHA_6>	2026-01-01T12:06:00+00:00	Record island-a/cheap-priority experiment	island-a/cheap-priority	benchmark_score=0.887, runtime_sec=1.04	Prioritizing cheaper items improved the cheap-case fit without fully giving up stale recovery.
<SHA_7>	2026-01-01T12:05:00+00:00	Record island-c/relevance-lean experiment	island-c/relevance-lean	benchmark_score=0.861, runtime_sec=0.8	A relevance-heavy mix helped somewhat and became the fastest branch to validate.
<SHA_8>	2026-01-01T12:04:00+00:00	Record island-b/stale-recovery experiment	island-b/stale-recovery	benchmark_score=0.879, runtime_sec=0.96	Stale recovery helped and picked up some of the cheap-case gains from island A.
<SHA_9>	2026-01-01T12:03:00+00:00	Record island-a/rebalance-weights experiment	island-a/rebalance-weights	benchmark_score=0.872, runtime_sec=1.01	Weight rebalance improved the benchmark noticeably at a small runtime cost.
<SHA_10>	2026-01-01T12:02:00+00:00	Record island-c/clip-premium experiment	island-c/clip-premium	benchmark_score=0.842, runtime_sec=0.86	Premium clipping was only a minor improvement over baseline but stayed cheap to validate.
""")

    recent_json = run(["recent", "--limit", "2", "--format", "jsonl"], cwd=repo_path)
    recent_json_records = [
        json.loads(line) for line in recent_json.stdout.strip().splitlines() if line
    ]
    assert len(recent_json_records) == 2
    assert recent_json_records[0] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record cross/hybrid-final experiment",
        metrics=IsPartialDict(benchmark_score=0.918, runtime_sec=1.08),
    )
    assert recent_json_records[1] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record island-a/balanced-v2 experiment",
        metrics=IsPartialDict(benchmark_score=0.913, runtime_sec=1.03),
    )
    assert isinstance(recent_json_records[0]["tips"], list)

    bogus_recent = run(["recent", "--bogus"], cwd=repo_path, expect_failure=True)
    assert_click_error(bogus_recent.stderr, "No such option: --bogus")

    bogus_best = run(["best", "--bogus"], cwd=repo_path, expect_failure=True)
    assert_click_error(bogus_best.stderr, "No such option: --bogus")

    default_best = run(["best"], cwd=repo_path)
    best = run(["best", "--max", "benchmark_score"], cwd=repo_path)
    assert normalize_text(default_best.stdout) == normalize_text(best.stdout)
    assert normalize_text(best.stdout) == snapshot("""\
sha	date	subject	tips	metrics	summary
<SHA_1>	2026-01-01T12:11:00+00:00	Record cross/hybrid-final experiment	cross/hybrid-final	benchmark_score=0.918, runtime_sec=1.08	Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
<SHA_2>	2026-01-01T12:10:00+00:00	Record island-a/balanced-v2 experiment	island-a/balanced-v2	benchmark_score=0.913, runtime_sec=1.03	Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
<SHA_3>	2026-01-01T12:08:00+00:00	Record island-a/cost-penalty experiment	island-a/cost-penalty	benchmark_score=0.901, runtime_sec=1.12	The stronger cost penalty crossed the 0.90 threshold but made validation slower.
<SHA_4>	2026-01-01T12:07:00+00:00	Record island-c/premium-guard experiment	island-c/premium-guard	benchmark_score=0.894, runtime_sec=0.92	Premium guard was solid and balanced relevance against cheaper-case pressure.
<SHA_5>	2026-01-01T12:06:00+00:00	Record island-a/cheap-priority experiment	island-a/cheap-priority	benchmark_score=0.887, runtime_sec=1.04	Prioritizing cheaper items improved the cheap-case fit without fully giving up stale recovery.
""")

    best_json = run(
        ["best", "--max", "benchmark_score", "--limit", "2", "--format", "jsonl"],
        cwd=repo_path,
    )
    best_json_records = [json.loads(line) for line in best_json.stdout.strip().splitlines() if line]
    assert len(best_json_records) == 2
    assert best_json_records[0] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record cross/hybrid-final experiment",
        metrics=IsPartialDict(benchmark_score=0.918, runtime_sec=1.08),
    )
    assert best_json_records[1] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record island-a/balanced-v2 experiment",
        metrics=IsPartialDict(benchmark_score=0.913, runtime_sec=1.03),
    )

    legacy_best_selectors = run(["best", "--active"], cwd=repo_path, expect_failure=True)
    assert_click_error(legacy_best_selectors.stderr, "No such option: --active")

    fastest = run(["best", "--min", "runtime_sec", "--limit", "1"], cwd=repo_path)
    assert normalize_text(fastest.stdout) == snapshot("""\
sha	date	subject	tips	metrics	summary
<SHA_1>	2026-01-01T12:09:00+00:00	Record island-c/overfit-premium experiment	island-c/overfit-premium	benchmark_score=0.821, runtime_sec=0.74	The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
""")

    pareto = run(["pareto", "--max", "benchmark_score", "--min", "runtime_sec"], cwd=repo_path)
    assert normalize_text(pareto.stdout) == snapshot("""\
sha	date	subject	tips	metrics	summary
<SHA_1>	2026-01-01T12:11:00+00:00	Record cross/hybrid-final experiment	cross/hybrid-final	benchmark_score=0.918, runtime_sec=1.08	Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
<SHA_2>	2026-01-01T12:10:00+00:00	Record island-a/balanced-v2 experiment	island-a/balanced-v2	benchmark_score=0.913, runtime_sec=1.03	Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
<SHA_3>	2026-01-01T12:07:00+00:00	Record island-c/premium-guard experiment	island-c/premium-guard	benchmark_score=0.894, runtime_sec=0.92	Premium guard was solid and balanced relevance against cheaper-case pressure.
<SHA_4>	2026-01-01T12:05:00+00:00	Record island-c/relevance-lean experiment	island-c/relevance-lean	benchmark_score=0.861, runtime_sec=0.8	A relevance-heavy mix helped somewhat and became the fastest branch to validate.
<SHA_5>	2026-01-01T12:09:00+00:00	Record island-c/overfit-premium experiment	island-c/overfit-premium	benchmark_score=0.821, runtime_sec=0.74	The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
""")

    pareto_json = run(
        [
            "pareto",
            "--max",
            "benchmark_score",
            "--min",
            "runtime_sec",
            "--format",
            "jsonl",
        ],
        cwd=repo_path,
    )
    pareto_json_records = [
        json.loads(line) for line in pareto_json.stdout.strip().splitlines() if line
    ]
    assert len(pareto_json_records) == 5
    assert pareto_json_records[0] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record cross/hybrid-final experiment",
        metrics=IsPartialDict(benchmark_score=0.918, runtime_sec=1.08),
    )
    assert pareto_json_records[1] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record island-a/balanced-v2 experiment",
        metrics=IsPartialDict(benchmark_score=0.913, runtime_sec=1.03),
    )
    assert pareto_json_records[2] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record island-c/premium-guard experiment",
        metrics=IsPartialDict(benchmark_score=0.894, runtime_sec=0.92),
    )
    assert pareto_json_records[3] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record island-c/relevance-lean experiment",
        metrics=IsPartialDict(benchmark_score=0.861, runtime_sec=0.8),
    )
    assert pareto_json_records[4] == IsPartialDict(
        short_sha=IsStr(regex=r"[0-9a-f]{7}"),
        subject="Record island-c/overfit-premium experiment",
        metrics=IsPartialDict(benchmark_score=0.821, runtime_sec=0.74),
    )

    legacy_pareto_selectors = run(
        ["pareto", "--where", "benchmark_score > 0.89"],
        cwd=repo_path,
        expect_failure=True,
    )
    assert_click_error(legacy_pareto_selectors.stderr, "No such option: --where")

    graph = run(
        [
            "graph",
            "cross/hybrid-final",
            "--edges",
            "all",
            "--direction",
            "backward",
            "--depth",
            "all",
        ],
        cwd=repo_path,
    )
    assert normalize_text(graph.stdout) == snapshot("""\
root: <SHA_1>  Record cross/hybrid-final experiment
mode: edges=all direction=backward depth=all

nodes:
  <SHA_1>  Record cross/hybrid-final experiment
  <SHA_2>  Record island-a/balanced-v2 experiment
  <SHA_3>  Record island-b/stale-recovery experiment
  <SHA_4>  Record island-c/premium-guard experiment
  <SHA_5>  Record island-a/cost-penalty experiment
  <SHA_6>  Record island-b/boost-freshness experiment
  <SHA_7>  Record island-a/rebalance-weights experiment
  <SHA_8>  Record island-c/relevance-lean experiment
  <SHA_9>  Record island-a/cheap-priority experiment
  <SHA_10>  Record island-a/baseline experiment
  <SHA_11>  Record island-c/clip-premium experiment

edges:
  git  <SHA_1> -> <SHA_2>
  reference  <SHA_1> -> <SHA_3> - borrowed the stale-case recovery heuristic idea from this experiment
  reference  <SHA_1> -> <SHA_4> - borrowed the premium-guard weighting idea from this experiment
  git  <SHA_2> -> <SHA_5>
  reference  <SHA_2> -> <SHA_4> - borrowed the premium-guard idea from this experiment
  git  <SHA_3> -> <SHA_6>
  reference  <SHA_3> -> <SHA_7> - borrowed the cheaper-case weighting intuition from this experiment
  git  <SHA_4> -> <SHA_8>
  reference  <SHA_4> -> <SHA_7> - used the cheaper-case signal from this run as a guardrail
  git  <SHA_5> -> <SHA_9>
  reference  <SHA_5> -> <SHA_3> - borrowed the stale-case recovery intuition from this experiment
  git  <SHA_6> -> <SHA_10>
  git  <SHA_7> -> <SHA_10>
  git  <SHA_8> -> <SHA_11>
  reference  <SHA_8> -> <SHA_6> - kept the freshness behavior from this branch in mind while leaning harder on relevance
  git  <SHA_9> -> <SHA_7>
  reference  <SHA_9> -> <SHA_3> - checked the affordability shift against this stale-recovery branch
  git  <SHA_11> -> <SHA_10>
""")

    graph_json = run(
        [
            "graph",
            "cross/hybrid-final",
            "--edges",
            "all",
            "--direction",
            "backward",
            "--depth",
            "all",
            "--format",
            "json",
        ],
        cwd=repo_path,
    )
    graph_record = json.loads(graph_json.stdout)
    assert graph_record["root"] == commit_by_branch["cross/hybrid-final"]
    assert any(
        edge["kind"] == "git" and edge["to"] == commit_by_branch["island-a/balanced-v2"]
        for edge in graph_record["edges"]
    )
    assert any(
        edge["kind"] == "reference" and edge["to"] == commit_by_branch["island-c/premium-guard"]
        for edge in graph_record["edges"]
    )
    references_only_graph = run(
        [
            "graph",
            "cross/hybrid-final",
            "--edges",
            "references",
            "--direction",
            "backward",
            "--depth",
            "all",
            "--format",
            "json",
        ],
        cwd=repo_path,
    )
    references_only_record = json.loads(references_only_graph.stdout)
    assert references_only_record["mode"] == "references"
    assert references_only_record["edges"]
    assert all(edge["kind"] == "reference" for edge in references_only_record["edges"])
    assert any(
        edge["to"] == commit_by_branch["island-c/premium-guard"]
        for edge in references_only_record["edges"]
    )

    default_graph = run(["graph", "cross/hybrid-final"], cwd=repo_path)
    assert normalize_text(default_graph.stdout) == snapshot("""\
root: <SHA_1>  Record cross/hybrid-final experiment
mode: edges=all direction=backward depth=3

nodes:
  <SHA_1>  Record cross/hybrid-final experiment
  <SHA_2>  Record island-a/balanced-v2 experiment
  <SHA_3>  Record island-b/stale-recovery experiment
  <SHA_4>  Record island-c/premium-guard experiment
  <SHA_5>  Record island-a/cost-penalty experiment
  <SHA_6>  Record island-b/boost-freshness experiment
  <SHA_7>  Record island-a/rebalance-weights experiment
  <SHA_8>  Record island-c/relevance-lean experiment
  <SHA_9>  Record island-a/cheap-priority experiment
  <SHA_10>  Record island-a/baseline experiment
  <SHA_11>  Record island-c/clip-premium experiment

edges:
  git  <SHA_1> -> <SHA_2>
  reference  <SHA_1> -> <SHA_3> - borrowed the stale-case recovery heuristic idea from this experiment
  reference  <SHA_1> -> <SHA_4> - borrowed the premium-guard weighting idea from this experiment
  git  <SHA_2> -> <SHA_5>
  reference  <SHA_2> -> <SHA_4> - borrowed the premium-guard idea from this experiment
  git  <SHA_3> -> <SHA_6>
  reference  <SHA_3> -> <SHA_7> - borrowed the cheaper-case weighting intuition from this experiment
  git  <SHA_4> -> <SHA_8>
  reference  <SHA_4> -> <SHA_7> - used the cheaper-case signal from this run as a guardrail
  git  <SHA_5> -> <SHA_9>
  reference  <SHA_5> -> <SHA_3> - borrowed the stale-case recovery intuition from this experiment
  git  <SHA_6> -> <SHA_10>
  git  <SHA_7> -> <SHA_10>
  git  <SHA_8> -> <SHA_11>
  reference  <SHA_8> -> <SHA_6> - kept the freshness behavior from this branch in mind while leaning harder on relevance
""")

    run_git(repo_path, ["checkout", "cross/hybrid-final"])
    status = run(["status"], cwd=repo_path)
    assert normalize_text(status.stdout, normalize_age=True) == snapshot("""\
project:
  metric: max benchmark_score
  experiments: 12 recorded (0 ongoing)
  best: <SHA_1>  benchmark_score=0.918  (<AGE>)
  recent trend: +0.024 over last 5 recorded experiments (4m span)

latest experiments:
  <SHA_1>  benchmark_score=0.918  (<AGE>) | Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
  <SHA_2>  benchmark_score=0.913  (<AGE>) | Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
  <SHA_3>  benchmark_score=0.821  (<AGE>) | The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
  <SHA_4>  benchmark_score=0.901  (<AGE>) | The stronger cost penalty crossed the 0.90 threshold but made validation slower.
  <SHA_5>  benchmark_score=0.894  (<AGE>) | Premium guard was solid and balanced relevance against cheaper-case pressure.

ongoing experiments (managed worktrees):
  (none)

""")

    status_json = run(["status", "--format", "json"], cwd=repo_path)
    status_record = json.loads(status_json.stdout)
    assert status_record["checkout"]["branch"] == "cross/hybrid-final"
    assert status_record["checkout"]["dirty"] is False
    assert status_record["checkout"]["currentRecordState"]["kind"] == "recorded"
    assert (
        status_record["checkout"]["nearestExperimentAncestor"]["sha"]
        == commit_by_branch["cross/hybrid-final"]
    )
    assert status_record["activeRecordedTips"][0]["sha"] == commit_by_branch["cross/hybrid-final"]
    assert status_record["activeRecordedTips"][1]["sha"] == commit_by_branch["island-a/balanced-v2"]
    assert any(
        main_branch in entry["branches"] for entry in status_record["activeTipsMissingRecord"]
    )

    run_git(repo_path, ["checkout", main_branch])
    compare = run(["compare", "island-a/balanced-v2", "cross/hybrid-final"], cwd=repo_path)
    assert normalize_text(compare.stdout) == snapshot("""\
left:  <SHA_1>  2026-01-01T12:10:00+00:00  Record island-a/balanced-v2 experiment [island-a/balanced-v2] - benchmark_score=0.913, runtime_sec=1.03 | Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
right: <SHA_2>  2026-01-01T12:11:00+00:00  Record cross/hybrid-final experiment [cross/hybrid-final] - benchmark_score=0.918, runtime_sec=1.08 | Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
git:   direct_parent_of_right (merge-base <SHA_1>)
diff:  3 files changed, 17 insertions(+), 12 deletions(-)

changed paths:
  M  EXPERIMENT.json
  M  JOURNAL.md
  M  src/ranker.py

metrics:
  benchmark_score: 0.913 -> 0.918 (+0.005)
  runtime_sec: 1.03 -> 1.08 (+0.05)

references:
  common: <SHA_3>
  left only: (none)
  right only: <SHA_4>

parent deltas:
  left vs <SHA_5>:
    benchmark_score: 0.901 -> 0.913 (+0.012)
    runtime_sec: 1.12 -> 1.03 (-0.09)
  right vs <SHA_1>:
    benchmark_score: 0.913 -> 0.918 (+0.005)
    runtime_sec: 1.03 -> 1.08 (+0.05)

left summary:  Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
right summary: Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
""")

    compare_patch = run(
        ["compare", "island-a/balanced-v2", "cross/hybrid-final", "--patch"],
        cwd=repo_path,
    )
    assert "\npatch:\n" in compare_patch.stdout
    assert re.search(
        r"^diff --git a/EXPERIMENT\.json b/EXPERIMENT\.json", compare_patch.stdout, re.M
    )

    compare_json = run(
        ["compare", "island-a/balanced-v2", "cross/hybrid-final", "--format", "json"],
        cwd=repo_path,
    )
    compare_record = json.loads(compare_json.stdout)
    assert compare_record["git"]["relationship"] == "direct_parent_of_right"
    assert any(
        entry["path"] == "EXPERIMENT.json" and entry["status"] == "M"
        for entry in compare_record["changedPaths"]
    )
    assert abs(compare_record["metrics"]["benchmark_score"]["delta"] - 0.005) < 1e-9
    assert abs(compare_record["metrics"]["runtime_sec"]["delta"] - 0.05) < 1e-9
    assert (
        compare_record["parentDeltas"]["right"]["parent"]
        == commit_by_branch["island-a/balanced-v2"]
    )
    assert len(compare_record["references"]["rightOnly"]) == 1

    sibling_compare_json = run(
        [
            "compare",
            "island-b/boost-freshness",
            "island-c/clip-premium",
            "--format",
            "json",
        ],
        cwd=repo_path,
    )
    sibling_compare_record = json.loads(sibling_compare_json.stdout)
    assert sibling_compare_record["git"]["relationship"] == "sibling"
    assert sibling_compare_record["git"]["sharedParents"] == [commit_by_branch["island-a/baseline"]]

    Path(repo_path, "JOURNAL.md").write_text(
        "# Notes\n\nCurrent checkout is incomplete.\n", encoding="utf-8"
    )
    dirty_status_json = run(["status", "--format", "json"], cwd=repo_path)
    dirty_status_record = json.loads(dirty_status_json.stdout)
    assert dirty_status_record["checkout"]["dirty"] is True
    assert dirty_status_record["checkout"]["currentRecordState"]["kind"] == "incomplete"
    assert (
        "missing EXPERIMENT.json"
        in dirty_status_record["checkout"]["currentRecordState"]["problems"]
    )
    Path(repo_path, "JOURNAL.md").unlink()

    run_git(repo_path, ["checkout", "-b", "broken-tip"])
    Path(repo_path, "JOURNAL.md").write_text(
        "# Broken Tip\n\nThis branch records the wrong metric payload.\n",
        encoding="utf-8",
    )
    Path(repo_path, "EXPERIMENT.json").write_text(
        json.dumps(
            {
                "summary": "Broken tip omitted the declared primary metric.",
                "metrics": {"runtime_sec": 1.5},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    commit_all(repo_path, "Record broken tip experiment", "2026-01-01T12:12:00Z")
    run_git(repo_path, ["checkout", current_branch(repo_path)])

    invalid_status_json = run(["status", "--format", "json"], cwd=repo_path)
    invalid_status_record = json.loads(invalid_status_json.stdout)
    assert any(
        "broken-tip" in entry["branches"]
        and 'missing primary metric "benchmark_score"' in entry["problems"]
        for entry in invalid_status_record["activeTipsNeedingAttention"]
    )

    show = run(["show", "island-a/baseline"], cwd=repo_path)
    assert normalize_text(show.stdout) == snapshot("""\
# JOURNAL.md
# Island A Baseline

Hypothesis: Capture the starting benchmark before splitting into island searches.

Lineage:
- git parent: main

References:
- none

Validation:
- python3 scripts/validate.py

Outcome:
- Recorded the baseline benchmark before island-specific exploration.

# EXPERIMENT.json
{
  "summary": "Recorded the baseline benchmark before island-specific exploration.",
  "metrics": {
    "benchmark_score": 0.838,
    "runtime_sec": 0.91
  },
  "references": []
}
""")

    show_best = run(["show", "cross/hybrid-final"], cwd=repo_path)
    assert normalize_text(show_best.stdout) == snapshot("""\
# JOURNAL.md
# Cross Hybrid Final

Hypothesis: Cross-pollinate the strongest island A, B, and C ideas without doing a formal git merge.

Lineage:
- git parent: island-a/balanced-v2 @ <SHA_1>

References:
- <SHA_2>: borrowed the stale-case recovery heuristic idea from this experiment
- <SHA_3>: borrowed the premium-guard weighting idea from this experiment

Validation:
- python3 scripts/validate.py

Outcome:
- Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.

# EXPERIMENT.json
{
  "summary": "Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.",
  "metrics": {
    "benchmark_score": 0.918,
    "runtime_sec": 1.08
  },
  "references": [
    {
      "commit": "<SHA_4>",
      "why": "borrowed the stale-case recovery heuristic idea from this experiment"
    },
    {
      "commit": "<SHA_5>",
      "why": "borrowed the premium-guard weighting idea from this experiment"
    }
  ]
}
""")

    show_json = run(["show", "cross/hybrid-final", "--format", "json"], cwd=repo_path)
    show_record = json.loads(show_json.stdout)
    assert show_record["experiment"]["metrics"]["benchmark_score"] == 0.918
    assert len(show_record["experiment"]["references"]) == 2


def test_status_best_prefers_earliest_tie() -> None:
    repo_path = init_repo_from_fixture()
    init_other_now(repo_path)
    commit_all(repo_path, "Initialize autoevolve")

    def write_recorded_experiment(summary: str, score: float) -> None:
        Path(repo_path, "JOURNAL.md").write_text(
            f"# {summary}\n\nValidation:\n- python3 scripts/validate.py\n",
            encoding="utf-8",
        )
        Path(repo_path, "EXPERIMENT.json").write_text(
            json.dumps(
                {
                    "summary": summary,
                    "metrics": {"benchmark_score": score, "runtime_sec": 1.0},
                    "references": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    run_git(repo_path, ["checkout", "-b", "tie/first"])
    write_recorded_experiment("First tied best experiment.", 0.918)
    commit_all(repo_path, "Record tie/first experiment", "2026-01-01T12:00:00Z")
    first_best_sha = run_git(repo_path, ["rev-parse", "HEAD"]).strip()

    run_git(repo_path, ["checkout", "-b", "tie/later"])
    write_recorded_experiment("Later tied best experiment.", 0.918)
    commit_all(repo_path, "Record tie/later experiment", "2026-01-01T12:01:00Z")
    run_git(repo_path, ["checkout", "tie/later"])

    status = run(["status"], cwd=repo_path)
    assert f"best: {first_best_sha[:7]}  benchmark_score=0.918" in status.stdout


def test_managed_experiment_commands() -> None:
    repo_path = init_repo_from_fixture()
    init_other_now(repo_path)
    commit_all(repo_path, "Initialize autoevolve")
    populate_synthetic_branches(repo_path)

    temp_home = tempfile.mkdtemp(prefix="autoevolve-home-")
    seed_branch = "autoevolve/seed"
    run_git(repo_path, ["branch", seed_branch, "cross/hybrid-final"])

    from_main = run(
        [
            "start",
            "from-main",
            "Try the current branch as a seed.",
            "--from",
            current_branch(repo_path),
        ],
        cwd=repo_path,
        env={"HOME": temp_home},
    )
    from_main_path = Path(temp_home) / ".autoevolve" / "worktrees" / "from-main"
    assert normalize_text(from_main.stdout, from_main_path) == snapshot("""\
Branch: autoevolve/from-main
Base: main
Path: <PATH_1>
""")
    assert from_main_path.exists()
    run(["clean", "from-main", "--force"], cwd=repo_path, env={"HOME": temp_home})
    assert not from_main_path.exists()

    created = run(
        [
            "start",
            "trial-run",
            "Trial run starts from the managed seed branch.",
            "--from",
            seed_branch,
        ],
        cwd=repo_path,
        env={"HOME": temp_home},
    )
    worktree_path = Path(temp_home) / ".autoevolve" / "worktrees" / "trial-run"
    resolved_worktree_path = worktree_path.resolve()
    assert normalize_text(created.stdout, worktree_path) == snapshot("""\
Branch: autoevolve/trial-run
Base: autoevolve/seed
Path: <PATH_1>
""")
    assert worktree_path.exists()
    assert current_branch(worktree_path) == "autoevolve/trial-run"

    stub_journal = Path(worktree_path, "JOURNAL.md").read_text(encoding="utf-8")
    assert "TODO: fill this in once you're done with your experiment." in stub_journal
    stub_experiment = json.loads(Path(worktree_path, "EXPERIMENT.json").read_text(encoding="utf-8"))
    assert stub_experiment["summary"] == "Trial run starts from the managed seed branch."

    stub_commit = run(["record"], cwd=worktree_path, env={"HOME": temp_home}, expect_failure=True)
    assert "Replace the JOURNAL.md stub before committing." in stub_commit.stderr

    Path(worktree_path, "JOURNAL.md").write_text(
        "# trial-run\n\nMeasured the new thresholds and kept the faster scoring path.\n",
        encoding="utf-8",
    )
    Path(worktree_path, "EXPERIMENT.json").write_text(
        json.dumps(
            {
                "summary": "Trial run improves the benchmark with a small scoring tweak.",
                "metrics": {"benchmark_score": 0.919, "runtime_sec": 1.07},
                "references": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    Path(worktree_path, "src", "ranker.py").write_text(
        Path(worktree_path, "src", "ranker.py").read_text(encoding="utf-8")
        + "\n# trial-run tweak\n",
        encoding="utf-8",
    )
    committed = run(["record"], cwd=worktree_path, env={"HOME": temp_home})
    assert normalize_text(committed.stdout, resolved_worktree_path) == snapshot("""\
Committed autoevolve/trial-run at <SHA_1>.
Removed worktree: <PATH_1>
""")
    assert not worktree_path.exists()
    assert "trial-run" not in run_git(repo_path, ["worktree", "list"])
    assert (
        run_git(repo_path, ["log", "-1", "--pretty=%s", "autoevolve/trial-run"]).strip()
        == "Trial run improves the benchmark with a small scoring tweak."
    )


def test_managed_experiment_edge_cases_and_clean() -> None:
    repo_path = init_repo_from_fixture()
    init_other_now(repo_path)
    commit_all(repo_path, "Initialize autoevolve")
    populate_synthetic_branches(repo_path)

    temp_home = tempfile.mkdtemp(prefix="autoevolve-home-")
    original_branch = current_branch(repo_path)
    current_seed_branch = "autoevolve/current-seed"
    non_experiment_base_branch = "autoevolve/not-recorded"
    existing_branch = "autoevolve/existing-branch"

    missing_name = run(["start"], cwd=repo_path, env={"HOME": temp_home}, expect_failure=True)
    assert_click_error(missing_name.stderr, "Missing argument 'NAME'.")

    missing_summary = run(
        ["start", "summary-missing"],
        cwd=repo_path,
        env={"HOME": temp_home},
        expect_failure=True,
    )
    assert_click_error(missing_summary.stderr, "Missing argument 'SUMMARY'.")

    run_git(repo_path, ["branch", current_seed_branch, "cross/hybrid-final"])
    run_git(repo_path, ["branch", non_experiment_base_branch, original_branch])
    run_git(repo_path, ["branch", existing_branch, "cross/hybrid-final"])

    run_git(repo_path, ["checkout", current_seed_branch])
    Path(repo_path, "JOURNAL.md").write_text(
        Path(repo_path, "JOURNAL.md").read_text(encoding="utf-8")
        + "\nPrepared a unique seed commit for implicit-base coverage.\n",
        encoding="utf-8",
    )
    commit_all(
        repo_path,
        "Prepare current-seed branch for implicit-base coverage",
        "2026-01-15T12:00:00Z",
    )

    implicit_base = run(
        ["start", "implicit-base", "Use the current managed seed as the base."],
        cwd=repo_path,
        env={"HOME": temp_home},
    )
    implicit_path = Path(temp_home) / ".autoevolve" / "worktrees" / "implicit-base"
    assert "Base: autoevolve/current-seed" in implicit_base.stdout
    assert implicit_path.exists()

    cleaned_implicit = run(
        ["clean", "implicit-base", "--force"], cwd=repo_path, env={"HOME": temp_home}
    )
    assert "Removed 1 linked worktree for this repository." in cleaned_implicit.stdout
    assert not implicit_path.exists()

    invalid_name = run(
        [
            "start",
            "../escape",
            "Invalid path escape attempt.",
            "--from",
            current_seed_branch,
        ],
        cwd=repo_path,
        env={"HOME": temp_home},
        expect_failure=True,
    )
    assert "not a valid experiment name" in invalid_name.stderr

    non_experiment_base_sha = run_git(repo_path, ["rev-parse", non_experiment_base_branch]).strip()
    from_sha = run(
        [
            "start",
            "from-sha",
            "Start from an explicit commit SHA.",
            "--from",
            non_experiment_base_sha,
        ],
        cwd=repo_path,
        env={"HOME": temp_home},
    )
    assert f"Base: {non_experiment_base_sha}" in from_sha.stdout
    from_sha_path = Path(temp_home) / ".autoevolve" / "worktrees" / "from-sha"
    assert from_sha_path.exists()
    run(["clean", "from-sha", "--force"], cwd=repo_path, env={"HOME": temp_home})
    assert not from_sha_path.exists()

    branch_exists_result = run(
        [
            "start",
            "existing-branch",
            "Existing branch collision.",
            "--from",
            current_seed_branch,
        ],
        cwd=repo_path,
        env={"HOME": temp_home},
        expect_failure=True,
    )
    assert 'Branch "autoevolve/existing-branch" already exists.' in branch_exists_result.stderr

    conflict_path = Path(temp_home) / ".autoevolve" / "worktrees" / "path-conflict"
    conflict_path.mkdir(parents=True, exist_ok=True)
    path_conflict = run(
        [
            "start",
            "path-conflict",
            "Existing worktree path collision.",
            "--from",
            current_seed_branch,
        ],
        cwd=repo_path,
        env={"HOME": temp_home},
        expect_failure=True,
    )
    assert "Worktree path already exists:" in path_conflict.stderr

    run_git(repo_path, ["checkout", original_branch])

    non_managed_commit = run(
        ["record"], cwd=repo_path, env={"HOME": temp_home}, expect_failure=True
    )
    assert (
        "record only works on managed autoevolve experiment branches" in non_managed_commit.stderr
    )

    run_git(repo_path, ["checkout", current_seed_branch])
    primary_commit = run(["record"], cwd=repo_path, env={"HOME": temp_home}, expect_failure=True)
    assert "record must be run from a managed autoevolve worktree under" in primary_commit.stderr
    run_git(repo_path, ["checkout", original_branch])


@pytest.mark.parametrize(
    ("harness", "skill_path"),
    [
        ("claude", ".claude/skills/autoevolve/SKILL.md"),
        ("gemini", ".gemini/skills/autoevolve/SKILL.md"),
        ("codex", ".codex/skills/autoevolve/SKILL.md"),
    ],
)
def test_harness_init_variants(harness: str, skill_path: str) -> None:
    repo_path = init_repo_from_fixture()
    run(
        [
            "init",
            harness,
            "--mode",
            "now",
            "--yes",
            "--goal",
            f"Generate a {harness} adapter",
            "--validation",
            "python3 scripts/validate.py",
            "--metric",
            "max benchmark_score",
            "--constraints",
            "",
        ],
        cwd=repo_path,
    )
    skill_text = Path(repo_path, skill_path).read_text(encoding="utf-8")
    assert skill_text.startswith("---\nname: autoevolve\ndescription: ")
    assert "\n# Autoevolve Protocol\n" in skill_text


def test_continue_hooks() -> None:
    commands = {
        "claude": (
            [".claude/settings.json"],
            "printf '%s\\n' 'Are you done? If not, continue.' >&2; exit 2",
        ),
        "gemini": (
            [".gemini/settings.json"],
            "printf '%s\\n' 'Are you done? If not, continue.' >&2; exit 2",
        ),
        "codex": (
            [".codex/config.toml", ".codex/hooks.json"],
            (
                "cat >/dev/null; printf '%s\\n' "
                '\'{"decision":"block","reason":"Are you done? If not, '
                "continue.\"}'"
            ),
        ),
    }
    for harness, (paths, expected_command) in commands.items():
        repo_path = init_repo_from_fixture()
        run(
            [
                "init",
                harness,
                "--continue-hook",
                "--mode",
                "now",
                "--yes",
                "--goal",
                f"Generate a {harness} adapter",
                "--validation",
                "python3 scripts/validate.py",
                "--metric",
                "max benchmark_score",
                "--constraints",
                "",
            ],
            cwd=repo_path,
        )
        for path in paths:
            assert Path(repo_path, path).exists()
        if harness == "claude":
            settings = read_json_file(Path(repo_path, ".claude/settings.json"))
            assert settings == IsPartialDict(
                hooks=IsPartialDict(
                    Stop=[
                        IsPartialDict(
                            hooks=[IsPartialDict(type="command", command=expected_command)]
                        )
                    ]
                )
            )
        elif harness == "gemini":
            settings = read_json_file(Path(repo_path, ".gemini/settings.json"))
            assert settings == IsPartialDict(
                hooks=IsPartialDict(
                    AfterAgent=[
                        IsPartialDict(
                            hooks=[
                                IsPartialDict(
                                    type="command",
                                    name="autoevolve-continue",
                                    command=expected_command,
                                )
                            ]
                        )
                    ]
                )
            )
        else:
            config_text = Path(repo_path, ".codex/config.toml").read_text(encoding="utf-8")
            assert "[features]" in config_text
            assert "codex_hooks = true" in config_text
            hooks = read_json_file(Path(repo_path, ".codex/hooks.json"))
            assert hooks == IsPartialDict(
                hooks=IsPartialDict(
                    Stop=[
                        IsPartialDict(
                            hooks=[IsPartialDict(type="command", command=expected_command)]
                        )
                    ]
                )
            )
