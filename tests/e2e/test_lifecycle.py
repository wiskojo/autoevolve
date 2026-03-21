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
