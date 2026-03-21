import json
from collections import deque
from datetime import datetime
from pathlib import Path

from git.exc import BadName, GitCommandError

from autoevolve.git import find_repo_root, list_linked_worktrees, open_repo
from autoevolve.models.experiment import (
    ExperimentDocument,
    ExperimentRecord,
    ExperimentReference,
    ExperimentWorktree,
    Objective,
    ProblemSpec,
)
from autoevolve.models.lineage import LineageEdge, LineageGraph
from autoevolve.models.types import GraphDirection, GraphEdges, MetricValue
from autoevolve.problem import parse_problem_spec
from autoevolve.workspace import WORKTREE_ROOT

EXPERIMENT_FILE = "EXPERIMENT.json"
JOURNAL_FILE = "JOURNAL.md"
PROBLEM_FILE = "PROBLEM.md"


class ExperimentRepository:
    def __init__(self, cwd: str | Path = ".") -> None:
        self.root = find_repo_root(cwd)
        self.repo = open_repo(self.root)
        self._records: list[ExperimentRecord] | None = None

    def problem(self) -> ProblemSpec:
        problem_path = self.root / PROBLEM_FILE
        if not problem_path.exists():
            raise FileNotFoundError(f"Missing {PROBLEM_FILE}. Run autoevolve init first.")
        return parse_problem_spec(problem_path.read_text(encoding="utf-8"))

    def records(self) -> list[ExperimentRecord]:
        if self._records is None:
            records: list[ExperimentRecord] = []
            for sha in self._record_shas():
                record = self._load_record(sha)
                if record is not None:
                    records.append(record)
            self._records = records
        return list(self._records)

    def resolve_record(self, ref: str) -> ExperimentRecord:
        sha = self._resolve_commit(ref)
        record = self.record_by_sha(sha)
        if record is None:
            raise ValueError(f'"{ref}" does not resolve to a recorded experiment.')
        return record

    def record_by_sha(self, sha: str) -> ExperimentRecord | None:
        for record in self.records():
            if record.sha == sha:
                return record
        return self._load_record(sha)

    def active_worktrees(self) -> list[ExperimentWorktree]:
        managed_root = WORKTREE_ROOT.resolve()
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
                    is_managed=managed_root in item.path.parents,
                )
            )

        return worktrees

    def nearest_record(self, ref: str) -> ExperimentRecord | None:
        start = self._resolve_commit(ref)
        record_map = self._record_map()
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

    def previous_record(self, record: ExperimentRecord) -> ExperimentRecord | None:
        queue = deque(self._parents(record.sha))
        seen = {record.sha}

        while queue:
            sha = queue.popleft()
            if sha in seen:
                continue
            seen.add(sha)
            previous = self._record_map().get(sha)
            if previous is not None:
                return previous
            queue.extend(self._parents(sha))

        return None

    def recent_records(self, limit: int) -> list[ExperimentRecord]:
        return sorted(self.records(), key=lambda record: _sort_date(record.date), reverse=True)[
            :limit
        ]

    def best_records(self, objective: Objective, limit: int) -> list[ExperimentRecord]:
        numeric_records = [
            record
            for record in self.records()
            if _numeric_metric(record, objective.metric) is not None
        ]
        return sorted(numeric_records, key=lambda record: _best_key(record, objective))[:limit]

    def pareto_records(
        self,
        objectives: list[Objective],
        limit: int | None = None,
    ) -> list[ExperimentRecord]:
        candidates = [
            record
            for record in self.records()
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
        root = self.resolve_record(ref)
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

    def git_relationship(self, left: ExperimentRecord, right: ExperimentRecord) -> str:
        base = self._merge_base(left.sha, right.sha)
        left_parents = set(self._parents(left.sha))
        right_parents = set(self._parents(right.sha))
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

    def _record_shas(self) -> list[str]:
        output = self.repo.git.log("--all", "--format=%H", "--", EXPERIMENT_FILE)
        return list(dict.fromkeys(line for line in output.splitlines() if line))

    def _resolve_commit(self, ref: str) -> str:
        try:
            return self.repo.commit(ref).hexsha
        except (BadName, ValueError) as error:
            raise ValueError(f'Unknown ref "{ref}".') from error

    def _parents(self, ref: str) -> tuple[str, ...]:
        return tuple(parent.hexsha for parent in self.repo.commit(ref).parents)

    def _merge_base(self, left: str, right: str) -> str | None:
        base = self.repo.merge_base(left, right)
        if not base:
            return None
        return base[0].hexsha

    def _read_file_at_ref(self, ref: str, path: str) -> str | None:
        try:
            return str(self.repo.git.show(f"{ref}:{path}"))
        except GitCommandError:
            return None

    def _load_record(self, sha: str) -> ExperimentRecord | None:
        experiment_text = self._read_file_at_ref(sha, EXPERIMENT_FILE)
        journal_text = self._read_file_at_ref(sha, JOURNAL_FILE)
        if experiment_text is None or journal_text is None:
            return None
        commit = self.repo.commit(sha)
        return ExperimentRecord(
            sha=commit.hexsha,
            date=commit.committed_datetime.isoformat(),
            journal=journal_text,
            document=parse_experiment_document(experiment_text),
        )

    def _record_map(self) -> dict[str, ExperimentRecord]:
        return {record.sha: record for record in self.records()}

    def _backward_edges(self) -> dict[str, list[LineageEdge]]:
        record_map = self._record_map()
        edges: dict[str, list[LineageEdge]] = {}

        for record in self.records():
            for parent in self._parents(record.sha):
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


def _numeric_metric(record: ExperimentRecord, metric: str) -> float | None:
    value = record.document.metrics.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sort_date(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _best_key(record: ExperimentRecord, objective: Objective) -> tuple[float, datetime, str]:
    value = _numeric_metric(record, objective.metric)
    if value is None:
        raise ValueError(f'Metric "{objective.metric}" must be numeric.')
    ranked = value if objective.direction == "min" else -value
    return (ranked, _sort_date(record.date), record.sha)


def _pareto_key(
    record: ExperimentRecord, objectives: list[Objective]
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
    left: ExperimentRecord, right: ExperimentRecord, objectives: list[Objective]
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
