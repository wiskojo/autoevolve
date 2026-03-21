import json

from inline_snapshot import snapshot

from tests.e2e.conftest import RepoFixture


def test_recent_empty_repo(repo: RepoFixture) -> None:
    result = repo.run("recent")
    assert result.stdout == snapshot("No experiments found.\n")


def test_recent(history_repo: RepoFixture) -> None:
    result = history_repo.run("recent")
    assert history_repo.normalize(result.stdout) == snapshot(
        """\
sha	date	metrics	summary
<SHA_1>	2026-01-01T12:11:00+00:00	benchmark_score=0.918, runtime_sec=1.08	Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
<SHA_2>	2026-01-01T12:10:00+00:00	benchmark_score=0.913, runtime_sec=1.03	Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
<SHA_3>	2026-01-01T12:09:00+00:00	benchmark_score=0.821, runtime_sec=0.74	The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
<SHA_4>	2026-01-01T12:08:00+00:00	benchmark_score=0.901, runtime_sec=1.12	The stronger cost penalty crossed the 0.90 threshold but made validation slower.
<SHA_5>	2026-01-01T12:07:00+00:00	benchmark_score=0.894, runtime_sec=0.92	Premium guard was solid and balanced relevance against cheaper-case pressure.
<SHA_6>	2026-01-01T12:06:00+00:00	benchmark_score=0.887, runtime_sec=1.04	Prioritizing cheaper items improved the cheap-case fit without fully giving up stale recovery.
<SHA_7>	2026-01-01T12:05:00+00:00	benchmark_score=0.861, runtime_sec=0.8	A relevance-heavy mix helped somewhat and became the fastest variant to validate.
<SHA_8>	2026-01-01T12:04:00+00:00	benchmark_score=0.879, runtime_sec=0.96	Stale recovery helped and picked up some of the cheap-case gains from island A.
<SHA_9>	2026-01-01T12:03:00+00:00	benchmark_score=0.872, runtime_sec=1.01	Weight rebalance improved the benchmark noticeably at a small runtime cost.
<SHA_10>	2026-01-01T12:02:00+00:00	benchmark_score=0.842, runtime_sec=0.86	Premium clipping was only a minor improvement over baseline but stayed cheap to validate.
"""
    )


def test_recent_jsonl(history_repo: RepoFixture) -> None:
    result = history_repo.run("recent", "--limit", "2", "--format", "jsonl")
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert [row["date"] for row in rows] == [
        "2026-01-01T12:11:00+00:00",
        "2026-01-01T12:10:00+00:00",
    ]
    assert [row["summary"] for row in rows] == [
        "Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.",
        "Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.",
    ]
    assert rows[0]["metrics"] == {"benchmark_score": 0.918, "runtime_sec": 1.08}
    assert rows[1]["metrics"] == {"benchmark_score": 0.913, "runtime_sec": 1.03}
    assert rows[0]["references"] == [
        {
            "commit": rows[0]["references"][0]["commit"],
            "why": "borrowed the stale-case recovery heuristic idea from this experiment",
        },
        {
            "commit": rows[0]["references"][1]["commit"],
            "why": "borrowed the premium-guard weighting idea from this experiment",
        },
    ]
    assert rows[1]["references"] == [
        {
            "commit": rows[1]["references"][0]["commit"],
            "why": "borrowed the premium-guard idea from this experiment",
        }
    ]
    assert all(len(row["short_sha"]) == 7 for row in rows)
    assert all(len(row["sha"]) == 40 for row in rows)


def test_best(history_repo: RepoFixture) -> None:
    result = history_repo.run("best")
    assert history_repo.normalize(result.stdout) == snapshot(
        """\
sha	date	metrics	summary
<SHA_1>	2026-01-01T12:11:00+00:00	benchmark_score=0.918, runtime_sec=1.08	Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
<SHA_2>	2026-01-01T12:10:00+00:00	benchmark_score=0.913, runtime_sec=1.03	Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
<SHA_3>	2026-01-01T12:08:00+00:00	benchmark_score=0.901, runtime_sec=1.12	The stronger cost penalty crossed the 0.90 threshold but made validation slower.
<SHA_4>	2026-01-01T12:07:00+00:00	benchmark_score=0.894, runtime_sec=0.92	Premium guard was solid and balanced relevance against cheaper-case pressure.
<SHA_5>	2026-01-01T12:06:00+00:00	benchmark_score=0.887, runtime_sec=1.04	Prioritizing cheaper items improved the cheap-case fit without fully giving up stale recovery.
"""
    )


def test_best_min_metric(history_repo: RepoFixture) -> None:
    result = history_repo.run("best", "--min", "runtime_sec", "--limit", "3")
    assert history_repo.normalize(result.stdout) == snapshot(
        """\
sha	date	metrics	summary
<SHA_1>	2026-01-01T12:09:00+00:00	benchmark_score=0.821, runtime_sec=0.74	The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
<SHA_2>	2026-01-01T12:05:00+00:00	benchmark_score=0.861, runtime_sec=0.8	A relevance-heavy mix helped somewhat and became the fastest variant to validate.
<SHA_3>	2026-01-01T12:02:00+00:00	benchmark_score=0.842, runtime_sec=0.86	Premium clipping was only a minor improvement over baseline but stayed cheap to validate.
"""
    )


def test_best_rejects_conflicting_objectives(history_repo: RepoFixture) -> None:
    result = history_repo.run(
        "best",
        "--max",
        "benchmark_score",
        "--min",
        "runtime_sec",
        expect_failure=True,
    )
    assert "Use either --max <metric> or --min <metric>, not both." in result.stderr


def test_best_reports_missing_numeric_metric(history_repo: RepoFixture) -> None:
    result = history_repo.run("best", "--max", "unknown_metric")
    assert result.stdout == snapshot(
        'No experiments found with a numeric "unknown_metric" metric.\n'
    )


def test_pareto(history_repo: RepoFixture) -> None:
    result = history_repo.run("pareto", "--max", "benchmark_score", "--min", "runtime_sec")
    assert history_repo.normalize(result.stdout) == snapshot(
        """\
sha	date	metrics	summary
<SHA_1>	2026-01-01T12:11:00+00:00	benchmark_score=0.918, runtime_sec=1.08	Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
<SHA_2>	2026-01-01T12:10:00+00:00	benchmark_score=0.913, runtime_sec=1.03	Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
<SHA_3>	2026-01-01T12:07:00+00:00	benchmark_score=0.894, runtime_sec=0.92	Premium guard was solid and balanced relevance against cheaper-case pressure.
<SHA_4>	2026-01-01T12:05:00+00:00	benchmark_score=0.861, runtime_sec=0.8	A relevance-heavy mix helped somewhat and became the fastest variant to validate.
<SHA_5>	2026-01-01T12:09:00+00:00	benchmark_score=0.821, runtime_sec=0.74	The premium-heavy mix regressed against the earlier baselines despite being very fast to validate.
"""
    )


def test_pareto_requires_at_least_one_metric(history_repo: RepoFixture) -> None:
    result = history_repo.run("pareto", expect_failure=True)
    assert "pareto requires at least one metric" in result.stderr
