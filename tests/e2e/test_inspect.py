from inline_snapshot import snapshot

from tests.e2e.conftest import RepoFixture


def test_status_lists_unmanaged_linked_worktrees(repo: RepoFixture) -> None:
    repo.init_other()
    other_worktree = repo.home / "scratch"
    repo.git("worktree", "add", "-b", "scratch", str(other_worktree))
    (other_worktree / "README.md").write_text("dirty\n", encoding="utf-8")

    result = repo.run("status")
    normalized = repo.normalize(result.stdout, other_worktree)
    assert "other linked worktrees:" in normalized
    assert "<PATH_1> [scratch, unmanaged, dirty]" in normalized


def test_status(history_repo: RepoFixture) -> None:
    history_repo.git("checkout", "cross/hybrid-final")
    result = history_repo.run("status")
    assert history_repo.normalize(result.stdout) == snapshot(
        """\
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
"""
    )


def test_log_empty_repo(repo: RepoFixture) -> None:
    result = repo.run("log")
    assert result.stdout == snapshot("No experiments found.\n")


def test_log(history_repo: RepoFixture) -> None:
    result = history_repo.run("log", "--limit", "2")
    assert history_repo.normalize(result.stdout) == snapshot(
        """\
commit <SHA_1>
date: 2026-01-01T12:11:00+00:00
experiment:
  summary: Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
  metrics:
    benchmark_score: 0.918
    runtime_sec: 1.08
journal:
  # Cross Hybrid Final

  Hypothesis: Cross-pollinate the strongest island A, B, and C ideas without doing a formal git merge.

  Lineage:
  - git parent: island-a/balanced-v2 @ <SHA_2>

  References:
  - <SHA_3>: borrowed the stale-case recovery heuristic idea from this experiment
  - <SHA_4>: borrowed the premium-guard weighting idea from this experiment

  Validation:
  - python3 scripts/validate.py

  Outcome:
  - Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.

commit <SHA_2>
date: 2026-01-01T12:10:00+00:00
experiment:
  summary: Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
  metrics:
    benchmark_score: 0.913
    runtime_sec: 1.03
journal:
  # Island A Balanced v2

  Hypothesis: Preserve the cost gains from island A while borrowing island C's premium guard.

  Lineage:
  - git parent: island-a/cost-penalty @ <SHA_5>

  References:
  - <SHA_4>: borrowed the premium-guard idea from this experiment

  Validation:
  - python3 scripts/validate.py

  Outcome:
  - Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
"""
    )


def test_show_compare_and_lineage(history_repo: RepoFixture) -> None:
    show = history_repo.run("show", "island-a/baseline")
    assert history_repo.normalize(show.stdout) == snapshot(
        """\
experiment:
  summary: Recorded the baseline benchmark before island-specific exploration.
  metrics:
    benchmark_score: 0.838
    runtime_sec: 0.91
  references:
    (none)

journal:
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

code diff:
  (none)
"""
    )

    compare = history_repo.run("compare", "island-a/balanced-v2", "cross/hybrid-final")
    assert history_repo.normalize(compare.stdout) == snapshot(
        """\
left:  <SHA_1>  2026-01-01T12:10:00+00:00 - benchmark_score=0.913, runtime_sec=1.03 | Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
right: <SHA_2>  2026-01-01T12:11:00+00:00 - benchmark_score=0.918, runtime_sec=1.08 | Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
git:   direct_parent_of_right (merge-base <SHA_1>)
diff:  1 file changed, 3 insertions(+), 3 deletions(-)

changed paths:
  M  src/ranker.py

metrics:
  benchmark_score: 0.913 -> 0.918 (+0.005)
  runtime_sec: 1.03 -> 1.08 (+0.05)

references:
  common: <SHA_3>
  left only: (none)
  right only: <SHA_4>

summaries:
  left: Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
  right: Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.

code diff:
  diff --git a/src/ranker.py b/src/ranker.py
  index <SHA_5>..<SHA_6> 100644
  --- a/src/ranker.py
  +++ b/src/ranker.py
  @@ -1,5 +1,5 @@
   def score_candidate(features):
  -    freshness = features["freshness"] * 0.36
  -    relevance = features["relevance"] * 0.44
  -    affordability = (1 - features["cost"]) * 0.2
  +    freshness = features["freshness"] * 0.37
  +    relevance = features["relevance"] * 0.42
  +    affordability = (1 - features["cost"]) * 0.21
       return round(freshness + relevance + affordability, 3)
"""
    )

    lineage = history_repo.run(
        "lineage",
        "cross/hybrid-final",
        "--edges",
        "all",
        "--direction",
        "backward",
        "--depth",
        "all",
    )
    assert history_repo.normalize(lineage.stdout) == snapshot(
        """\
root: <SHA_1>  Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
mode: edges=all direction=backward depth=all

nodes:
  <SHA_1>  Hybrid final is the best synthetic experiment and explicitly combines ideas from multiple islands.
  <SHA_2>  Balanced v2 combined island A's score gains with island C's premium guard and became the best single-island result.
  <SHA_3>  Stale recovery helped and picked up some of the cheap-case gains from island A.
  <SHA_4>  Premium guard was solid and balanced relevance against cheaper-case pressure.
  <SHA_5>  The stronger cost penalty crossed the 0.90 threshold but made validation slower.
  <SHA_6>  Freshness boost nudged the benchmark upward with a slightly faster validation run.
  <SHA_7>  Weight rebalance improved the benchmark noticeably at a small runtime cost.
  <SHA_8>  A relevance-heavy mix helped somewhat and became the fastest variant to validate.
  <SHA_9>  Prioritizing cheaper items improved the cheap-case fit without fully giving up stale recovery.
  <SHA_10>  Recorded the baseline benchmark before island-specific exploration.
  <SHA_11>  Premium clipping was only a minor improvement over baseline but stayed cheap to validate.

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
  reference  <SHA_8> -> <SHA_6> - kept the freshness behavior from that earlier experiment in mind while leaning harder on relevance
  git  <SHA_9> -> <SHA_7>
  reference  <SHA_9> -> <SHA_3> - checked the affordability shift against the stale-recovery experiment
  git  <SHA_11> -> <SHA_10>
"""
    )


def test_lineage_rejects_invalid_depth(history_repo: RepoFixture) -> None:
    result = history_repo.run(
        "lineage",
        "cross/hybrid-final",
        "--depth",
        "0",
        expect_failure=True,
    )
    assert "Depth must be a positive integer or 'all'." in result.stderr
