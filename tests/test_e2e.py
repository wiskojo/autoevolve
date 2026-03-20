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

from autoevolve.prompt import build_protocol_prompt
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
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "python-playground"


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
        assert base_ref is not None
        run_git(repo_path, ["checkout", base_ref])
        run_git(repo_path, ["checkout", "-b", with_prefix(experiment.branch)])
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
    assert re.search(r"Harness: other", result.stdout)
    assert re.search(r"Files: PROBLEM\.md, AUTOEVOLVE\.md", result.stdout)
    assert re.search(r"Autoevolve initialized\.", result.stdout)
    assert re.search(r"Repository: ", result.stdout)
    assert re.search(r"Files written:", result.stdout)
    assert re.search(r"- PROBLEM\.md", result.stdout)
    assert re.search(r"- AUTOEVOLVE\.md", result.stdout)
    assert re.search(
        r"Next: ask your agent to verify setup and begin the experiment loop\.",
        result.stdout,
    )
    assert re.search(r"Start autoevolve\.", result.stdout)
    assert Path(repo_path, "PROBLEM.md").exists()
    assert Path(repo_path, "AUTOEVOLVE.md").exists()

    validate = run(["validate"], cwd=repo_path)
    assert "repository matches the autoevolve protocol" in validate.stdout
    assert "No current experiment record found" in validate.stdout

    top_help = run([], cwd=repo_path)
    assert "Human:" in top_help.stdout
    assert "Lifecycle:" in top_help.stdout
    assert "Inspect:" in top_help.stdout
    assert "Analytics:" in top_help.stdout
    assert re.search(
        r"start\s+Create a managed experiment branch and worktree\.",
        top_help.stdout,
    )
    assert re.search(
        r"record\s+Validate, commit, and remove the current managed worktree\.",
        top_help.stdout,
    )
    assert re.search(r"list\s+List recent experiments\.", top_help.stdout)
    assert re.search(r"recent\s+Return the most recent experiments\.", top_help.stdout)
    assert "update" not in top_help.stdout

    legacy_experiments = run(["experiments"], cwd=repo_path, expect_failure=True)
    assert 'unknown command "experiments"' in legacy_experiments.stderr

    list_help = run(["list", "--help"], cwd=repo_path)
    assert re.search(r"^autoevolve list\n\nList recent experiments\.\n\nUsage:", list_help.stdout)
    assert re.search(r"most recent recorded experiments", list_help.stdout, re.I)
    assert re.search(
        r"--limit <n>\s+Number of experiments to show\. Default: 10\.",
        list_help.stdout,
        re.I,
    )

    best_help = run(["best", "--help"], cwd=repo_path)
    assert re.search(
        r"^autoevolve best\n\nReturn the top experiments for one objective\.\n\nUsage:",
        best_help.stdout,
    )
    assert "format <tsv|jsonl>" in best_help.stdout
    assert "primary metric from PROBLEM.md" in best_help.stdout

    recent_help = run(["recent", "--help"], cwd=repo_path)
    assert re.search(
        r"^autoevolve recent\n\nShow the most recent recorded experiments\.\n\nUsage:",
        recent_help.stdout,
    )
    assert "format <tsv|jsonl>" in recent_help.stdout

    pareto_help = run(["pareto", "--help"], cwd=repo_path)
    assert "autoevolve pareto" in pareto_help.stdout
    assert "Pareto frontier for the selected objectives" in pareto_help.stdout

    graph_help = run(["graph", "--help"], cwd=repo_path)
    assert "autoevolve graph <ref>" in graph_help.stdout
    assert "--depth <n|all>" in graph_help.stdout


def test_other_scaffold_init() -> None:
    repo_path = init_repo_from_fixture()
    result = run(["init", "other", "--mode", "scaffold", "--yes"], cwd=repo_path)
    assert "Harness: other" in result.stdout
    assert "Files: PROBLEM.md, AUTOEVOLVE.md" in result.stdout
    assert "Autoevolve initialized." in result.stdout
    assert "Repository: " in result.stdout
    assert "Files written:" in result.stdout
    assert "Next: ask your agent to finish setup." in result.stdout
    assert "Follow the setup instructions for autoevolve." in result.stdout


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
    assert "Harness: other" in result.stdout
    assert "Problem: Keep existing PROBLEM.md" in result.stdout
    assert "Files: keep PROBLEM.md, write AUTOEVOLVE.md" in result.stdout
    assert "Files written:" in result.stdout
    assert "- AUTOEVOLVE.md" in result.stdout
    assert Path(repo_path, "PROBLEM.md").read_text(encoding="utf-8") == existing_problem


def test_legacy_commands_removed() -> None:
    repo_path = init_repo_from_fixture()
    for command in ["log", "update", "lineage", "results", "search"]:
        result = run([command], cwd=repo_path, expect_failure=True)
        assert f'unknown command "{command}"' in result.stderr


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
    assert re.search(
        (
            r"## Metric\nmax benchmark_score\n\nHigher is better\. Computed by "
            r"python3 scripts/validate\.py\."
        ),
        problem_text,
    )
    validate = run(["validate"], cwd=repo_path)
    assert "repository matches the autoevolve protocol" in validate.stdout


def test_protocol_prompt_lifecycle_guidance() -> None:
    prompt = build_protocol_prompt()
    assert "autoevolve start <name> <summary> [--from <ref>]" in prompt
    assert "autoevolve record" in prompt
    assert "autoevolve clean" in prompt
    assert "autoevolve recent" in prompt
    assert "autoevolve best" in prompt
    assert "autoevolve compare" in prompt
    assert "Faithfully record the metrics produced by this experiment commit itself." in prompt
    assert (
        "`metrics` should be a truthful record of what this experiment achieved when evaluated."
        in prompt
    )
    assert "keep each subagent scoped to one experiment or one clear checkpoint" in prompt
    assert (
        "continue it as a sequence of committed experiments rather than one giant uncommitted run"
        in prompt
    )
    assert (
        "continue exploring outward through committed experiments rather "
        "than one long-lived uncommitted session"
    ) in prompt
    assert "managed worktrees under `~/.autoevolve/worktrees`" in prompt
    assert "Do not scatter files across `/tmp`, `/private`, cache directories" in prompt


def test_synthetic_branches_inspect_and_analytics() -> None:
    repo_path = init_repo_from_fixture()
    init_other_now(repo_path)
    commit_all(repo_path, "Initialize autoevolve")
    main_branch = current_branch(repo_path)
    commit_by_branch = populate_synthetic_branches(repo_path)
    experiment_list = run(["list"], cwd=repo_path)
    experiment_blocks = re.split(r"\n\n+", experiment_list.stdout.strip())
    assert len(experiment_blocks) == 10
    assert re.search(
        r"^[0-9a-f]{7}  2026-01-01T12:11:00(?:Z|\+00:00)  Record cross/hybrid-final experiment",
        experiment_blocks[0],
        re.M,
    )
    assert "summary: Hybrid final is the best synthetic experiment" in experiment_blocks[0]
    assert "metrics: benchmark_score=0.918, runtime_sec=1.08" in experiment_blocks[0]
    assert re.search(
        r"journal: (Hypothesis: )?Cross-pollinate the strongest island A, B, and C ideas",
        experiment_blocks[0],
    )
    assert "Record island-a/baseline experiment" not in experiment_list.stdout

    limited_list = run(["list", "--limit", "2"], cwd=repo_path)
    limited_blocks = re.split(r"\n\n+", limited_list.stdout.strip())
    assert len(limited_blocks) == 2
    assert "Record cross/hybrid-final experiment" in limited_blocks[0]
    assert "Record island-a/balanced-v2 experiment" in limited_blocks[1]

    bogus_list = run(["list", "--bogus"], cwd=repo_path, expect_failure=True)
    assert 'Unknown option "--bogus" for list' in bogus_list.stderr

    legacy_list_selectors = run(["list", "--text", "premium"], cwd=repo_path, expect_failure=True)
    assert 'Unknown option "--text" for list' in legacy_list_selectors.stderr

    recent = run(["recent"], cwd=repo_path)
    recent_lines = recent.stdout.strip().splitlines()
    assert len(recent_lines) == 11
    assert recent_lines[0] == "sha\tdate\tsubject\ttips\tmetrics\tsummary"
    assert re.search(
        r"^.{7}\t2026-01-01T12:11:00(?:Z|\+00:00)\tRecord cross/hybrid-final experiment\t",
        recent_lines[1],
    )
    assert re.search(
        r"^.{7}\t2026-01-01T12:10:00(?:Z|\+00:00)\tRecord island-a/balanced-v2 experiment\t",
        recent_lines[2],
    )

    recent_json = run(["recent", "--limit", "2", "--format", "jsonl"], cwd=repo_path)
    recent_json_records = [
        json.loads(line) for line in recent_json.stdout.strip().splitlines() if line
    ]
    assert len(recent_json_records) == 2
    assert len(recent_json_records[0]["short_sha"]) == 7
    assert re.search(r"Record cross/hybrid-final experiment", recent_json_records[0]["subject"])
    assert isinstance(recent_json_records[0]["tips"], list)

    bogus_recent = run(["recent", "--bogus"], cwd=repo_path, expect_failure=True)
    assert 'Unknown option "--bogus" for recent' in bogus_recent.stderr

    bogus_best = run(["best", "--bogus"], cwd=repo_path, expect_failure=True)
    assert 'Unknown option "--bogus" for best' in bogus_best.stderr

    default_best = run(["best"], cwd=repo_path)
    assert re.search(r"^sha\tdate\tsubject\ttips\tmetrics\tsummary$", default_best.stdout, re.M)
    assert "Record cross/hybrid-final experiment" in default_best.stdout

    best = run(["best", "--max", "benchmark_score"], cwd=repo_path)
    best_lines = best.stdout.strip().splitlines()
    assert len(best_lines) == 6
    assert best_lines[0] == "sha\tdate\tsubject\ttips\tmetrics\tsummary"
    assert "Record cross/hybrid-final experiment" in best_lines[1]
    assert "Record island-a/balanced-v2 experiment" in best_lines[2]

    best_json = run(
        ["best", "--max", "benchmark_score", "--limit", "2", "--format", "jsonl"],
        cwd=repo_path,
    )
    best_json_records = [json.loads(line) for line in best_json.stdout.strip().splitlines() if line]
    assert len(best_json_records) == 2
    assert len(best_json_records[0]["short_sha"]) == 7
    assert isinstance(best_json_records[0]["metrics"]["benchmark_score"], float)

    legacy_best_selectors = run(["best", "--active"], cwd=repo_path, expect_failure=True)
    assert 'Unknown option "--active" for best' in legacy_best_selectors.stderr

    fastest = run(["best", "--min", "runtime_sec", "--limit", "1"], cwd=repo_path)
    assert "Record island-c/overfit-premium experiment" in fastest.stdout
    assert "runtime_sec=0.74" in fastest.stdout

    pareto = run(["pareto", "--max", "benchmark_score", "--min", "runtime_sec"], cwd=repo_path)
    pareto_lines = pareto.stdout.strip().splitlines()
    assert len(pareto_lines) == 6
    assert pareto_lines[0] == "sha\tdate\tsubject\ttips\tmetrics\tsummary"
    assert "Record cross/hybrid-final experiment" in pareto.stdout
    assert "Record island-a/balanced-v2 experiment" in pareto.stdout
    assert "Record island-c/relevance-lean experiment" in pareto.stdout
    assert "Record island-a/cheap-priority experiment" not in pareto.stdout

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
    assert any(record["metrics"]["benchmark_score"] == 0.918 for record in pareto_json_records)

    legacy_pareto_selectors = run(
        ["pareto", "--where", "benchmark_score > 0.89"],
        cwd=repo_path,
        expect_failure=True,
    )
    assert 'Unknown option "--where" for pareto' in legacy_pareto_selectors.stderr

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
    assert re.search(r"root: .*Record cross/hybrid-final experiment", graph.stdout)
    assert "mode: edges=all direction=backward depth=all" in graph.stdout
    assert re.search(r"git  .* -> .*", graph.stdout)
    assert re.search(r"reference  .* -> .*", graph.stdout)

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

    default_graph = run(["graph", "cross/hybrid-final"], cwd=repo_path)
    assert "mode: edges=all direction=backward depth=3" in default_graph.stdout

    run_git(repo_path, ["checkout", "cross/hybrid-final"])
    status = run(["status"], cwd=repo_path)
    assert "metric: max benchmark_score" in status.stdout
    assert re.search(r"experiments: \d+ recorded \(0 ongoing\)", status.stdout)
    assert re.search(r"best: [0-9a-f]{7}  benchmark_score=0\.918  \(.+\)", status.stdout)
    assert re.search(
        r"recent trend: [+-][^ ]+ over last 5 recorded experiments \([0-9]+[a-z]+ span\)",
        status.stdout,
    )
    assert re.search(r"ongoing experiments \(managed worktrees\):\n  \(none\)", status.stdout)

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
    assert re.search(r"left:  .*Record island-a/balanced-v2 experiment", compare.stdout)
    assert re.search(r"right: .*Record cross/hybrid-final experiment", compare.stdout)
    assert re.search(r"git:   direct_parent_of_right", compare.stdout)
    assert "changed paths:" in compare.stdout
    assert "  M  EXPERIMENT.json" in compare.stdout
    assert "benchmark_score: 0.913 -> 0.918" in compare.stdout
    assert "parent deltas:" in compare.stdout
    assert "runtime_sec: 1.03 -> 1.08" in compare.stdout

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
    assert "# JOURNAL.md" in show.stdout
    assert "Recorded the baseline benchmark before island-specific exploration" in show.stdout
    assert "# EXPERIMENT.json" in show.stdout
    assert '"runtime_sec": 0.91' in show.stdout

    show_best = run(["show", "cross/hybrid-final"], cwd=repo_path)
    assert "Cross Hybrid Final" in show_best.stdout
    assert "0.918" in show_best.stdout
    assert "borrowed the stale-case recovery heuristic idea" in show_best.stdout
    assert "borrowed the premium-guard weighting idea" in show_best.stdout

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
    assert re.search(
        rf"best: {re.escape(first_best_sha[:7])}  benchmark_score=0\.918  \(.+\)",
        status.stdout,
    )


def test_managed_experiment_commands() -> None:
    repo_path = init_repo_from_fixture()
    init_other_now(repo_path)
    commit_all(repo_path, "Initialize autoevolve")
    populate_synthetic_branches(repo_path)

    temp_home = tempfile.mkdtemp(prefix="autoevolve-home-")
    seed_branch = "autoevolve/seed"
    run_git(repo_path, ["branch", seed_branch, "cross/hybrid-final"])

    start_help = run(["start", "--help"], cwd=repo_path)
    assert "autoevolve start <name> <summary> [--from <ref>]" in start_help.stdout
    assert "managed worktree under ~/.autoevolve/worktrees" in start_help.stdout
    assert "Managed branches are created under autoevolve/<name>" in start_help.stdout

    record_help = run(["record", "--help"], cwd=repo_path)
    assert "removes the current managed worktree" in record_help.stdout

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
    assert re.search(rf"Path: {re.escape(str(from_main_path))}", from_main.stdout)
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
    assert "Branch: autoevolve/trial-run" in created.stdout
    assert "Base: autoevolve/seed" in created.stdout
    assert re.search(rf"Path: {re.escape(str(worktree_path))}", created.stdout)
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
    assert re.search(r"Committed autoevolve/trial-run at [0-9a-f]{7}\.", committed.stdout)
    assert re.search(
        rf"Removed worktree: {re.escape(str(resolved_worktree_path))}", committed.stdout
    )
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
    assert "start requires an experiment name and summary" in missing_name.stderr

    missing_summary = run(
        ["start", "summary-missing"],
        cwd=repo_path,
        env={"HOME": temp_home},
        expect_failure=True,
    )
    assert "start requires an experiment summary" in missing_summary.stderr

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
    result = run(
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
    assert f"Harness: {harness}" in result.stdout
    assert skill_path in result.stdout
    skill_text = Path(repo_path, skill_path).read_text(encoding="utf-8")
    assert re.search(r"^---\nname: autoevolve\ndescription: ", skill_text)
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
        result = run(
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
        assert "Continue hook: enabled" in result.stdout
        for path in paths:
            assert Path(repo_path, path).exists()
        if harness == "claude":
            settings = json.loads(
                Path(repo_path, ".claude/settings.json").read_text(encoding="utf-8")
            )
            assert settings["hooks"]["Stop"][0]["hooks"][0]["type"] == "command"
            assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == expected_command
        elif harness == "gemini":
            settings = json.loads(
                Path(repo_path, ".gemini/settings.json").read_text(encoding="utf-8")
            )
            assert settings["hooks"]["AfterAgent"][0]["hooks"][0]["type"] == "command"
            assert settings["hooks"]["AfterAgent"][0]["hooks"][0]["name"] == "autoevolve-continue"
            assert settings["hooks"]["AfterAgent"][0]["hooks"][0]["command"] == expected_command
        else:
            config_text = Path(repo_path, ".codex/config.toml").read_text(encoding="utf-8")
            assert "[features]" in config_text
            assert "codex_hooks = true" in config_text
            hooks = json.loads(Path(repo_path, ".codex/hooks.json").read_text(encoding="utf-8"))
            assert hooks["hooks"]["Stop"][0]["hooks"][0]["type"] == "command"
            assert hooks["hooks"]["Stop"][0]["hooks"][0]["command"] == expected_command


def test_packaging_smoke() -> None:
    pyproject_text = Path(PROJECT_ROOT, "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'build-backend = "hatchling\.build"', pyproject_text)
    assert re.search(r"autoevolve = \"autoevolve\.cli:main\"", pyproject_text)
    assert "click" in pyproject_text
    assert "GitPython" in pyproject_text
    assert "pytest" in pyproject_text
    assert "ruff" in pyproject_text
    assert "mypy" in pyproject_text
