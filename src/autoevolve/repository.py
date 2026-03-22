import json
from collections import deque
from datetime import datetime
from pathlib import Path

from git.exc import BadName

from autoevolve.git import (
    find_repo_root,
    list_experiment_commits,
    list_linked_worktrees,
    open_repo,
    read_text_blob,
    read_text_blobs,
)
from autoevolve.models.experiment import (
    ExperimentDetail,
    ExperimentDocument,
    ExperimentIndexEntry,
    ExperimentReference,
    ExperimentWorktree,
    Objective,
    ProblemSpec,
)
from autoevolve.models.git import GitCommit
from autoevolve.models.lineage import LineageEdge, LineageGraph
from autoevolve.models.types import GraphDirection, GraphEdges, MetricValue
from autoevolve.problem import parse_problem_spec

EXPERIMENT_FILE = "EXPERIMENT.json"
JOURNAL_FILE = "JOURNAL.md"
PROBLEM_FILE = "PROBLEM.md"
WORKTREE_ROOT = Path.home() / ".autoevolve" / "worktrees"
WORKTREE_ROOT_DISPLAY = "~/.autoevolve/worktrees"


def _worktree_root() -> Path:
    return Path.home() / ".autoevolve" / "worktrees"


def _is_managed_worktree_path(path: Path) -> bool:
    resolved = path.resolve()
    dynamic_root = _worktree_root().resolve()
    if resolved == dynamic_root or dynamic_root in resolved.parents:
        return True
    parent = resolved.parent
    return parent.name == "worktrees" and parent.parent.name == ".autoevolve"


class ExperimentRepository:
    def __init__(self, cwd: str | Path = ".") -> None:
        self.root = find_repo_root(cwd)
        self.repo = open_repo(self.root)
        self._index: list[ExperimentIndexEntry] | None = None
        self._index_by_sha: dict[str, ExperimentIndexEntry] = {}
        self._details_by_sha: dict[str, ExperimentDetail] = {}
        self._parents_by_sha: dict[str, tuple[str, ...]] = {}
        self._problem: ProblemSpec | None = None

    def problem(self) -> ProblemSpec:
        if self._problem is None:
            problem_path = self.root / PROBLEM_FILE
            if not problem_path.exists():
                raise FileNotFoundError(f"Missing {PROBLEM_FILE}. Run autoevolve init first.")
            self._problem = parse_problem_spec(problem_path.read_text(encoding="utf-8"))
        return self._problem

    def index(self) -> list[ExperimentIndexEntry]:
        if self._index is None:
            self._index = self._build_index(list_experiment_commits(self.repo, EXPERIMENT_FILE))
        return list(self._index)

    def recent_index(self, limit: int) -> list[ExperimentIndexEntry]:
        return self._build_index(list_experiment_commits(self.repo, EXPERIMENT_FILE, limit=limit))

    def detail(self, ref: str) -> ExperimentDetail:
        sha = self._resolve_commit(ref)
        cached = self._details_by_sha.get(sha)
        if cached is not None:
            return cached
        detail = self._load_detail(sha)
        if detail is None:
            raise ValueError(f'"{ref}" does not resolve to a recorded experiment.')
        self._details_by_sha[sha] = detail
        return detail

    def resolve_index(self, ref: str) -> ExperimentIndexEntry:
        sha = self._resolve_commit(ref)
        entry = self.record_by_sha(sha)
        if entry is None:
            raise ValueError(f'"{ref}" does not resolve to a recorded experiment.')
        return entry

    def record_by_sha(self, sha: str) -> ExperimentIndexEntry | None:
        cached = self._index_by_sha.get(sha)
        if cached is not None:
            return cached
        if self._index is not None:
            return None
        entry = self._load_index_entry(sha)
        if entry is not None:
            self._index_by_sha[sha] = entry
            self._parents_by_sha[sha] = entry.parents
        return entry

    def active_worktrees(self) -> list[ExperimentWorktree]:
        worktrees: list[ExperimentWorktree] = []

        for item in list_linked_worktrees(self.repo):
            is_missing = not item.path.exists()
            dirty = False if is_missing else open_repo(item.path).is_dirty(untracked_files=True)
            worktrees.append(
                ExperimentWorktree(
                    name=item.path.name,
                    path=item.path,
                    branch=item.branch,
                    head=item.head,
                    dirty=dirty,
                    is_missing=is_missing,
                    is_current=item.is_current,
                    is_primary=item.is_primary,
                    is_managed=_is_managed_worktree_path(item.path),
                )
            )

        return worktrees

    def nearest_record(self, ref: str) -> ExperimentIndexEntry | None:
        start = self._resolve_commit(ref)
        record_map = self._index_map()
        queue = deque([start])
        seen = {start}

        while queue:
            sha = queue.popleft()
            record = record_map.get(sha)
            if record is not None:
                return record
            for parent in self._parents(sha):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)

        return None

    def previous_record(self, record: ExperimentIndexEntry) -> ExperimentIndexEntry | None:
        queue = deque(record.parents)
        seen = {record.sha}
        record_map = self._index_map()

        while queue:
            sha = queue.popleft()
            if sha in seen:
                continue
            seen.add(sha)
            previous = record_map.get(sha)
            if previous is not None:
                return previous
            queue.extend(self._parents(sha))

        return None

    def best_records(self, objective: Objective, limit: int) -> list[ExperimentIndexEntry]:
        numeric_records = [
            record
            for record in self.index()
            if _numeric_metric(record, objective.metric) is not None
        ]
        return sorted(numeric_records, key=lambda record: _best_key(record, objective))[:limit]

    def pareto_records(
        self,
        objectives: list[Objective],
        limit: int | None = None,
    ) -> list[ExperimentIndexEntry]:
        candidates = [
            record
            for record in self.index()
            if all(
                _numeric_metric(record, objective.metric) is not None for objective in objectives
            )
        ]
        frontier = [
            candidate
            for candidate in candidates
            if not any(
                other.sha != candidate.sha and _dominates(other, candidate, objectives)
                for other in candidates
            )
        ]
        ranked = sorted(frontier, key=lambda record: _pareto_key(record, objectives))
        return ranked if limit is None else ranked[:limit]

    def lineage(
        self,
        ref: str,
        *,
        edges: GraphEdges,
        direction: GraphDirection,
        depth: int | None,
    ) -> LineageGraph:
        root = self.resolve_index(ref)
        backward = self._backward_edges()
        forward = self._forward_edges(backward)
        queue = deque([(root.sha, 0)])
        seen = {root.sha}
        node_order = [root.sha]
        edge_set: set[tuple[str, str, str, str | None]] = set()

        while queue:
            sha, distance = queue.popleft()
            if depth is not None and distance >= depth:
                continue
            candidates: list[LineageEdge] = []
            if direction in {GraphDirection.BACKWARD, GraphDirection.BOTH}:
                candidates.extend(backward.get(sha, ()))
            if direction in {GraphDirection.FORWARD, GraphDirection.BOTH}:
                candidates.extend(forward.get(sha, ()))
            for edge in candidates:
                if edges is not GraphEdges.ALL and edge.kind != edges.value.rstrip("s"):
                    continue
                edge_key = (edge.kind, edge.source, edge.target, edge.why)
                if edge_key not in edge_set:
                    edge_set.add(edge_key)
                next_sha = edge.target if edge.source == sha else edge.source
                if next_sha in seen:
                    continue
                seen.add(next_sha)
                node_order.append(next_sha)
                queue.append((next_sha, distance + 1))

        graph_edges = tuple(
            LineageEdge(kind=kind, source=source, target=target, why=why)
            for kind, source, target, why in edge_set
            if source in seen and target in seen
        )
        return LineageGraph(root=root, node_order=tuple(node_order), edges=graph_edges)

    def git_relationship(self, left: ExperimentIndexEntry, right: ExperimentIndexEntry) -> str:
        base = self._merge_base(left.sha, right.sha)
        left_parents = set(left.parents)
        right_parents = set(right.parents)
        if right.sha in left_parents:
            return f"direct_parent_of_left (merge-base {right.sha[:7]})"
        if left.sha in right_parents:
            return f"direct_parent_of_right (merge-base {left.sha[:7]})"
        if base is None:
            return "unrelated"
        if base in left_parents and base in right_parents:
            return f"sibling (merge-base {base[:7]})"
        if base == left.sha:
            return f"left_ancestor_of_right (merge-base {base[:7]})"
        if base == right.sha:
            return f"right_ancestor_of_left (merge-base {base[:7]})"
        return f"share_merge_base {base[:7]}"

    def _resolve_commit(self, ref: str) -> str:
        try:
            return self.repo.commit(ref).hexsha
        except (BadName, ValueError) as error:
            raise ValueError(f'Unknown ref "{ref}".') from error

    def _parents(self, ref: str) -> tuple[str, ...]:
        cached = self._parents_by_sha.get(ref)
        if cached is not None:
            return cached
        parents = tuple(parent.hexsha for parent in self.repo.commit(ref).parents)
        self._parents_by_sha[ref] = parents
        return parents

    def _merge_base(self, left: str, right: str) -> str | None:
        base = self.repo.merge_base(left, right)
        if not base:
            return None
        return base[0].hexsha

    def _build_index(self, commits: list[GitCommit]) -> list[ExperimentIndexEntry]:
        texts = read_text_blobs(self.repo, [commit.sha for commit in commits], EXPERIMENT_FILE)
        entries: list[ExperimentIndexEntry] = []
        self._index_by_sha = {}
        for commit in commits:
            experiment_text = texts.get(commit.sha)
            if experiment_text is None:
                continue
            entry = ExperimentIndexEntry(
                sha=commit.sha,
                date=commit.date,
                parents=commit.parents,
                document=parse_experiment_document(experiment_text),
            )
            entries.append(entry)
            self._index_by_sha[entry.sha] = entry
            self._parents_by_sha[entry.sha] = entry.parents
        return entries

    def _load_index_entry(self, sha: str) -> ExperimentIndexEntry | None:
        experiment_text = read_text_blob(self.repo, sha, EXPERIMENT_FILE)
        if experiment_text is None:
            return None
        commit = self.repo.commit(sha)
        return ExperimentIndexEntry(
            sha=commit.hexsha,
            date=commit.committed_datetime.isoformat(),
            parents=tuple(parent.hexsha for parent in commit.parents),
            document=parse_experiment_document(experiment_text),
        )

    def _load_detail(self, sha: str) -> ExperimentDetail | None:
        experiment_text = read_text_blob(self.repo, sha, EXPERIMENT_FILE)
        journal_text = read_text_blob(self.repo, sha, JOURNAL_FILE)
        if experiment_text is None or journal_text is None:
            return None
        return ExperimentDetail(experiment_text=experiment_text, journal=journal_text)

    def _index_map(self) -> dict[str, ExperimentIndexEntry]:
        if self._index is None:
            self.index()
        return self._index_by_sha

    def _backward_edges(self) -> dict[str, list[LineageEdge]]:
        record_map = self._index_map()
        edges: dict[str, list[LineageEdge]] = {}

        for record in self.index():
            for parent in record.parents:
                if parent in record_map:
                    edges.setdefault(record.sha, []).append(
                        LineageEdge(kind="git", source=record.sha, target=parent)
                    )
            for reference in record.document.references:
                if reference.commit in record_map:
                    edges.setdefault(record.sha, []).append(
                        LineageEdge(
                            kind="reference",
                            source=record.sha,
                            target=reference.commit,
                            why=reference.why,
                        )
                    )

        return edges

    def _forward_edges(
        self, backward: dict[str, list[LineageEdge]]
    ) -> dict[str, list[LineageEdge]]:
        forward: dict[str, list[LineageEdge]] = {}
        for edges in backward.values():
            for edge in edges:
                forward.setdefault(edge.target, []).append(edge)
        return forward


def parse_experiment_document(text: str) -> ExperimentDocument:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(str(error)) from error
    if not isinstance(value, dict):
        raise ValueError("EXPERIMENT.json must contain a JSON object.")

    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError('EXPERIMENT.json must contain a non-empty string field "summary".')

    metrics_raw = value.get("metrics", {})
    if not isinstance(metrics_raw, dict):
        raise ValueError('EXPERIMENT.json field "metrics" must be an object.')
    metrics: dict[str, MetricValue] = {}
    for key, metric_value in metrics_raw.items():
        if not isinstance(key, str):
            raise ValueError('EXPERIMENT.json field "metrics" keys must be strings.')
        if not _is_metric_value(metric_value):
            raise ValueError(
                f'EXPERIMENT.json field "metrics.{key}" must be a string, number, boolean, or null.'
            )
        metrics[key] = metric_value

    references_raw = value.get("references", [])
    if not isinstance(references_raw, list):
        raise ValueError('EXPERIMENT.json field "references" must be an array.')
    references: list[ExperimentReference] = []
    for index, entry in enumerate(references_raw):
        if not isinstance(entry, dict):
            raise ValueError(f'EXPERIMENT.json field "references[{index}]" must be an object.')
        commit = entry.get("commit")
        why = entry.get("why")
        if not isinstance(commit, str) or not commit.strip():
            raise ValueError(
                f'EXPERIMENT.json field "references[{index}].commit" must be a non-empty string.'
            )
        if not isinstance(why, str) or not why.strip():
            raise ValueError(
                f'EXPERIMENT.json field "references[{index}].why" must be a non-empty string.'
            )
        references.append(ExperimentReference(commit=commit, why=why))

    return ExperimentDocument(summary=summary, metrics=metrics, references=tuple(references))


def _is_metric_value(value: object) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _numeric_metric(record: ExperimentIndexEntry, metric: str) -> float | None:
    value = record.document.metrics.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sort_date(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _best_key(record: ExperimentIndexEntry, objective: Objective) -> tuple[float, datetime, str]:
    value = _numeric_metric(record, objective.metric)
    if value is None:
        raise ValueError(f'Metric "{objective.metric}" must be numeric.')
    ranked = value if objective.direction == "min" else -value
    return (ranked, _sort_date(record.date), record.sha)


def _pareto_key(
    record: ExperimentIndexEntry, objectives: list[Objective]
) -> tuple[float | datetime | str, ...]:
    values: list[float | datetime | str] = []
    for objective in objectives:
        metric = _numeric_metric(record, objective.metric)
        if metric is None:
            raise ValueError(f'Metric "{objective.metric}" must be numeric.')
        values.append(metric if objective.direction == "min" else -metric)
    values.append(_sort_date(record.date))
    values.append(record.sha)
    return tuple(values)


def _dominates(
    left: ExperimentIndexEntry, right: ExperimentIndexEntry, objectives: list[Objective]
) -> bool:
    strictly_better = False
    for objective in objectives:
        left_value = _numeric_metric(left, objective.metric)
        right_value = _numeric_metric(right, objective.metric)
        if left_value is None or right_value is None:
            return False
        if objective.direction == "max":
            if left_value < right_value:
                return False
            if left_value > right_value:
                strictly_better = True
        else:
            if left_value > right_value:
                return False
            if left_value < right_value:
                strictly_better = True
    return strictly_better
