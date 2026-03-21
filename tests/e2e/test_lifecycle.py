import json

from inline_snapshot import snapshot

from tests.e2e.conftest import RepoFixture


def test_start_and_record(repo: RepoFixture) -> None:
    repo.init_other()
    worktree = repo.home / ".autoevolve" / "worktrees" / "tune-thresholds"

    start = repo.run("start", "tune-thresholds", "Try a tighter threshold sweep")
    assert repo.normalize(start.stdout, worktree) == snapshot(
        """\
Branch: autoevolve/tune-thresholds
Base: main
Path: <PATH_1>
"""
    )

    (worktree / "JOURNAL.md").write_text(
        "# tune-thresholds\n\nTried a tighter threshold sweep.\n",
        encoding="utf-8",
    )
    (worktree / "EXPERIMENT.json").write_text(
        json.dumps(
            {
                "summary": "Tighter threshold sweep improved the benchmark.",
                "metrics": {"benchmark_score": 0.91, "runtime_sec": 1.03},
                "references": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (worktree / "src" / "ranker.py").write_text(
        'def score_candidate(features):\n    return features["freshness"]\n',
        encoding="utf-8",
    )

    record = repo.run("record", cwd=worktree)
    assert repo.normalize(record.stdout, worktree) == snapshot(
        """\
Committed autoevolve/tune-thresholds at <SHA_1>.
Removed worktree: <PATH_1>
"""
    )


def test_clean(repo: RepoFixture) -> None:
    repo.init_other()
    worktree = repo.home / ".autoevolve" / "worktrees" / "stale-run"
    repo.run("start", "stale-run", "Try a stale experiment cleanup path")
    result = repo.run("clean", "--force")
    assert repo.normalize(result.stdout, worktree) == snapshot(
        """\
Removed 1 linked worktree for this repository.
  <PATH_1> (autoevolve/stale-run, dirty, <SHA_1>)
"""
    )


def test_clean_no_worktrees(repo: RepoFixture) -> None:
    repo.init_other()
    result = repo.run("clean")
    assert result.stdout == snapshot("No managed worktrees to clean.\n")


def test_clean_requires_force_for_dirty_worktree(repo: RepoFixture) -> None:
    repo.init_other()
    worktree = repo.home / ".autoevolve" / "worktrees" / "dirty-run"
    repo.run("start", "dirty-run", "Try cleaning without force")
    result = repo.run("clean", expect_failure=True)
    normalized = repo.normalize(result.stderr, worktree)
    assert normalized == snapshot(
        """\
Refusing to remove a dirty or missing linked worktree without --force:
  <PATH_1> (autoevolve/dirty-run, dirty, <SHA_1>)
"""
    )


def test_clean_named_worktree_not_found(repo: RepoFixture) -> None:
    repo.init_other()
    result = repo.run("clean", "missing-run", expect_failure=True)
    assert result.stderr == snapshot(
        'No managed experiment worktree named "missing-run" found for this repository.\n'
    )


def test_record_from_primary_worktree_fails(repo: RepoFixture) -> None:
    repo.init_other()
    result = repo.run("record", expect_failure=True)
    assert result.stderr == snapshot(
        "record only works on managed autoevolve experiment branches (autoevolve/<name>).\n"
    )


def test_record_requires_replacing_journal_stub(repo: RepoFixture) -> None:
    repo.init_other()
    worktree = repo.home / ".autoevolve" / "worktrees" / "stub-run"
    repo.run("start", "stub-run", "Leave the journal stub in place")
    (worktree / "src" / "ranker.py").write_text(
        'def score_candidate(features):\n    return features["relevance"]\n',
        encoding="utf-8",
    )

    result = repo.run("record", cwd=worktree, expect_failure=True)
    assert result.stderr == snapshot("Replace the JOURNAL.md stub before committing.\n")


def test_start_from_explicit_ref(history_repo: RepoFixture) -> None:
    worktree = history_repo.home / ".autoevolve" / "worktrees" / "from-branch"
    result = history_repo.run(
        "start",
        "from-branch",
        "Branch from a prior experiment",
        "--from",
        "island-a/balanced-v2",
    )
    assert history_repo.normalize(result.stdout, worktree) == snapshot(
        """\
Branch: autoevolve/from-branch
Base: island-a/balanced-v2
Path: <PATH_1>
"""
    )


def test_record_requires_experiment_file(repo: RepoFixture) -> None:
    repo.init_other()
    worktree = repo.home / ".autoevolve" / "worktrees" / "missing-experiment"
    repo.run("start", "missing-experiment", "Delete the experiment file")
    (worktree / "JOURNAL.md").write_text("# missing-experiment\n\nDone.\n", encoding="utf-8")
    (worktree / "EXPERIMENT.json").unlink()
    (worktree / "src" / "ranker.py").write_text(
        'def score_candidate(features):\n    return features["cost"]\n',
        encoding="utf-8",
    )

    result = repo.run("record", cwd=worktree, expect_failure=True)
    assert result.stderr == snapshot("record requires both JOURNAL.md and EXPERIMENT.json.\n")
