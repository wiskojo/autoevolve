from dataclasses import dataclass

VALIDATION_COMMAND = "python3 scripts/validate.py"


@dataclass(frozen=True)
class FixtureReference:
    name: str
    why: str


@dataclass(frozen=True)
class ExperimentWeights:
    affordability: float
    freshness: float
    relevance: float


@dataclass(frozen=True)
class FixtureExperiment:
    name: str
    date: str
    hypothesis: str
    score: float
    runtime_sec: float
    summary: str
    title: str
    weights: ExperimentWeights
    base: str | None = None
    references: tuple[FixtureReference, ...] = ()


EXPERIMENTS = [
    FixtureExperiment(
        name="island-a/baseline",
        base=None,
        references=(),
        date="2026-01-01T12:00:00Z",
        hypothesis="Capture the starting benchmark before splitting into island searches.",
        score=0.838,
        runtime_sec=0.91,
        summary="Recorded the baseline benchmark before island-specific exploration.",
        title="Island A Baseline",
        weights=ExperimentWeights(affordability=0.1, freshness=0.45, relevance=0.45),
    ),
    FixtureExperiment(
        name="island-b/boost-freshness",
        base="island-a/baseline",
        references=(),
        date="2026-01-01T12:01:00Z",
        hypothesis=(
            "Push freshness harder on a separate island to see if stale examples recover quickly."
        ),
        score=0.846,
        runtime_sec=0.88,
        summary=(
            "Freshness boost nudged the benchmark upward with a slightly faster validation run."
        ),
        title="Island B Boost Freshness",
        weights=ExperimentWeights(affordability=0.09, freshness=0.49, relevance=0.42),
    ),
    FixtureExperiment(
        name="island-c/clip-premium",
        base="island-a/baseline",
        references=(),
        date="2026-01-01T12:02:00Z",
        hypothesis=(
            "Reduce the freshness term on a third island to avoid overshooting premium examples."
        ),
        score=0.842,
        runtime_sec=0.86,
        summary=(
            "Premium clipping was only a minor improvement over baseline but "
            "stayed cheap to validate."
        ),
        title="Island C Clip Premium",
        weights=ExperimentWeights(affordability=0.1, freshness=0.41, relevance=0.49),
    ),
    FixtureExperiment(
        name="island-a/rebalance-weights",
        base="island-a/baseline",
        references=(),
        date="2026-01-01T12:03:00Z",
        hypothesis="Rebalance toward affordability on island A after the baseline split.",
        score=0.872,
        runtime_sec=1.01,
        summary="Weight rebalance improved the benchmark noticeably at a small runtime cost.",
        title="Island A Rebalance Weights",
        weights=ExperimentWeights(affordability=0.15, freshness=0.4, relevance=0.45),
    ),
    FixtureExperiment(
        name="island-b/stale-recovery",
        base="island-b/boost-freshness",
        references=(
            FixtureReference(
                name="island-a/rebalance-weights",
                why="borrowed the cheaper-case weighting intuition from this experiment",
            ),
        ),
        date="2026-01-01T12:04:00Z",
        hypothesis=(
            "Keep the freshness-heavy island but borrow the affordability intuition from island A."
        ),
        score=0.879,
        runtime_sec=0.96,
        summary="Stale recovery helped and picked up some of the cheap-case gains from island A.",
        title="Island B Stale Recovery",
        weights=ExperimentWeights(affordability=0.17, freshness=0.38, relevance=0.45),
    ),
    FixtureExperiment(
        name="island-c/relevance-lean",
        base="island-c/clip-premium",
        references=(
            FixtureReference(
                name="island-b/boost-freshness",
                why=(
                    "kept the freshness behavior from that earlier experiment in mind "
                    "while leaning harder on relevance"
                ),
            ),
        ),
        date="2026-01-01T12:05:00Z",
        hypothesis=(
            "Lean harder on relevance while keeping a reference to island B's freshness behavior."
        ),
        score=0.861,
        runtime_sec=0.8,
        summary="A relevance-heavy mix helped somewhat and became the fastest variant to validate.",
        title="Island C Relevance Lean",
        weights=ExperimentWeights(affordability=0.08, freshness=0.35, relevance=0.57),
    ),
    FixtureExperiment(
        name="island-a/cheap-priority",
        base="island-a/rebalance-weights",
        references=(
            FixtureReference(
                name="island-b/stale-recovery",
                why="checked the affordability shift against the stale-recovery experiment",
            ),
        ),
        date="2026-01-01T12:06:00Z",
        hypothesis=(
            "Push affordability further on island A while checking it against "
            "the stale-recovery experiment."
        ),
        score=0.887,
        runtime_sec=1.04,
        summary=(
            "Prioritizing cheaper items improved the cheap-case fit without "
            "fully giving up stale recovery."
        ),
        title="Island A Cheap Priority",
        weights=ExperimentWeights(affordability=0.19, freshness=0.36, relevance=0.45),
    ),
    FixtureExperiment(
        name="island-c/premium-guard",
        base="island-c/relevance-lean",
        references=(
            FixtureReference(
                name="island-a/rebalance-weights",
                why="used the cheaper-case signal from this run as a guardrail",
            ),
        ),
        date="2026-01-01T12:07:00Z",
        hypothesis=(
            "Pull island C back from over-indexing on relevance while "
            "borrowing island A's cheaper-case signal."
        ),
        score=0.894,
        runtime_sec=0.92,
        summary="Premium guard was solid and balanced relevance against cheaper-case pressure.",
        title="Island C Premium Guard",
        weights=ExperimentWeights(affordability=0.18, freshness=0.39, relevance=0.43),
    ),
    FixtureExperiment(
        name="island-a/cost-penalty",
        base="island-a/cheap-priority",
        references=(
            FixtureReference(
                name="island-b/stale-recovery",
                why="borrowed the stale-case recovery intuition from this experiment",
            ),
        ),
        date="2026-01-01T12:08:00Z",
        hypothesis=(
            "Increase the cost penalty on island A while keeping island B's stale recovery in mind."
        ),
        score=0.901,
        runtime_sec=1.12,
        summary="The stronger cost penalty crossed the 0.90 threshold but made validation slower.",
        title="Island A Cost Penalty",
        weights=ExperimentWeights(affordability=0.22, freshness=0.33, relevance=0.45),
    ),
    FixtureExperiment(
        name="island-c/overfit-premium",
        base="island-c/premium-guard",
        references=(
            FixtureReference(
                name="island-b/boost-freshness",
                why="leaned too hard on the premium-friendly freshness idea from that experiment",
            ),
        ),
        date="2026-01-01T12:09:00Z",
        hypothesis=(
            "Try an aggressive premium-heavy setting even if it risks overfitting the benchmark."
        ),
        score=0.821,
        runtime_sec=0.74,
        summary=(
            "The premium-heavy mix regressed against the earlier baselines "
            "despite being very fast to validate."
        ),
        title="Island C Overfit Premium",
        weights=ExperimentWeights(affordability=0.05, freshness=0.55, relevance=0.4),
    ),
    FixtureExperiment(
        name="island-a/balanced-v2",
        base="island-a/cost-penalty",
        references=(
            FixtureReference(
                name="island-c/premium-guard",
                why="borrowed the premium-guard idea from this experiment",
            ),
        ),
        date="2026-01-01T12:10:00Z",
        hypothesis=(
            "Preserve the cost gains from island A while borrowing island C's premium guard."
        ),
        score=0.913,
        runtime_sec=1.03,
        summary=(
            "Balanced v2 combined island A's score gains with island C's "
            "premium guard and became the best single-island result."
        ),
        title="Island A Balanced v2",
        weights=ExperimentWeights(affordability=0.2, freshness=0.36, relevance=0.44),
    ),
    FixtureExperiment(
        name="cross/hybrid-final",
        base="island-a/balanced-v2",
        references=(
            FixtureReference(
                name="island-b/stale-recovery",
                why=("borrowed the stale-case recovery heuristic idea from this experiment"),
            ),
            FixtureReference(
                name="island-c/premium-guard",
                why="borrowed the premium-guard weighting idea from this experiment",
            ),
        ),
        date="2026-01-01T12:11:00Z",
        hypothesis=(
            "Cross-pollinate the strongest island A, B, and C ideas without "
            "doing a formal git merge."
        ),
        score=0.918,
        runtime_sec=1.08,
        summary=(
            "Hybrid final is the best synthetic experiment and explicitly "
            "combines ideas from multiple islands."
        ),
        title="Cross Hybrid Final",
        weights=ExperimentWeights(affordability=0.21, freshness=0.37, relevance=0.42),
    ),
]


def resolve_references(
    experiment: FixtureExperiment, commit_by_name: dict[str, str]
) -> list[dict[str, str]]:
    resolved: list[dict[str, str]] = []
    for reference in experiment.references:
        reference_commit = commit_by_name.get(reference.name)
        if reference_commit is None:
            raise AssertionError(f'Unknown reference "{reference.name}" for "{experiment.name}"')
        resolved.append({"commit": reference_commit, "why": reference.why})
    return resolved


def build_journal_text(
    experiment: FixtureExperiment,
    base_commit: str | None,
    resolved_references: list[dict[str, str]],
) -> str:
    if experiment.base and not base_commit:
        raise AssertionError(f"Missing base commit for {experiment.name}")
    base_line = "- git parent: main"
    if experiment.base and base_commit:
        base_line = f"- git parent: {experiment.base} @ {base_commit[:7]}"
    reference_lines = (
        "- none"
        if not resolved_references
        else "\n".join(
            f"- {reference['commit'][:7]}: {reference['why']}" for reference in resolved_references
        )
    )
    return f"""# {experiment.title}

Hypothesis: {experiment.hypothesis}

Lineage:
{base_line}

References:
{reference_lines}

Validation:
- {VALIDATION_COMMAND}

Outcome:
- {experiment.summary}
"""


def build_experiment_object(
    experiment: FixtureExperiment, resolved_references: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "summary": experiment.summary,
        "metrics": {
            "benchmark_score": experiment.score,
            "runtime_sec": experiment.runtime_sec,
        },
        "references": resolved_references,
    }
