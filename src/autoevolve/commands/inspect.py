from __future__ import annotations

import json
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cmp_to_key

import click

from autoevolve.commands.shared import (
    apply_limit,
    build_tip_map,
    get_experiment_records,
    get_managed_experiment_name,
    get_record_numeric_metric_value,
    is_managed_experiment_branch,
    list_autoevolve_branches,
    list_repo_worktrees,
    try_read_file_at_ref,
)
from autoevolve.constants import ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import (
    find_repo_root,
    get_current_branch_label,
    get_head_sha,
    is_checkout_dirty,
    resolve_ref,
    run_git,
    try_git,
)
from autoevolve.models import (
    ExperimentRecord,
    GraphDirection,
    GraphEdges,
    MetricValue,
    PrimaryMetricSpec,
    WorktreeInfo,
)
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.utils import (
    file_exists,
    format_metric_pairs,
    format_metric_summary,
    is_number,
    parse_experiment_json,
    parse_iso_datetime,
    read_text_file,
    short_sha,
)


@dataclass(frozen=True)
class CurrentRecordState:
    kind: str
    problems: list[str]


@dataclass(frozen=True)
class TipStatusEntry:
    sha: str
    branches: list[str]
    date: str | None
    subject: str
    summary: str | None
    metrics: dict[str, MetricValue] | None
    problems: list[str]
    primary_metric_value: int | float | None

    @property
    def short_sha(self) -> str:
        return short_sha(self.sha)


@dataclass(frozen=True)
class RecentTrend:
    delta: float
    sample_size: int
    span_ms: int


@dataclass(frozen=True)
class WorktreeCounts:
    clean: int
    dirty: int
    missing: int
    total: int


@dataclass(frozen=True)
class ChangedPath:
    status: str
    path: str
    previous_path: str | None = None


@dataclass(frozen=True)
class MetricDelta:
    left: MetricValue
    right: MetricValue
    delta: float | None


@dataclass(frozen=True)
class ReferenceDiff:
    common: list[str]
    left_only: list[str]
    right_only: list[str]


@dataclass(frozen=True)
class GitRelationship:
    relationship: str
    merge_base: str | None
    shared_parents: list[str]


@dataclass(frozen=True)
class LineageEdge:
    kind: str
    source: str
    target: str
    why: str | None = None


@dataclass(frozen=True)
class LineageGraph:
    node_order: list[str]
    edges: list[LineageEdge]


def format_worktree_state(worktree: WorktreeInfo) -> str:
    labels = [worktree.branch or "(detached HEAD)"]
    if worktree.is_current:
        labels.append("current")
    if worktree.is_primary:
        labels.append("primary")
    if worktree.is_managed_experiment:
        labels.append("managed")
    elif not worktree.is_primary:
        labels.append("unmanaged")
    labels.append("missing" if worktree.is_missing else "dirty" if worktree.dirty else "clean")
    return f"{worktree.path} [{', '.join(labels)}] @ {worktree.short_head}"


def format_managed_worktree_line(worktree: WorktreeInfo) -> str:
    branch_name = worktree.branch
    name = (
        get_managed_experiment_name(branch_name)
        if branch_name and is_managed_experiment_branch(branch_name)
        else os.path.basename(worktree.path)
    )
    state = "missing" if worktree.is_missing else "dirty" if worktree.dirty else "clean"
    return f"{name} @ {worktree.short_head} ({state})"


def summarize_worktree_counts(worktrees: list[WorktreeInfo]) -> WorktreeCounts:
    dirty = len([worktree for worktree in worktrees if worktree.dirty])
    missing = len([worktree for worktree in worktrees if worktree.is_missing])
    return WorktreeCounts(
        clean=len(worktrees) - dirty - missing,
        dirty=dirty,
        missing=missing,
        total=len(worktrees),
    )


def format_relative_time(iso_date: str) -> str:
    target = parse_iso_datetime(iso_date)
    if target is None:
        return ""
    now = datetime.now(timezone.utc)
    target_utc = target.astimezone(timezone.utc)
    delta_ms = (target_utc - now).total_seconds() * 1000
    abs_ms = abs(delta_ms)
    if abs_ms < 60_000:
        return "just now"
    units = [
        ("y", 365 * 24 * 60 * 60 * 1000),
        ("mo", 30 * 24 * 60 * 60 * 1000),
        ("w", 7 * 24 * 60 * 60 * 1000),
        ("d", 24 * 60 * 60 * 1000),
        ("h", 60 * 60 * 1000),
        ("m", 60 * 1000),
    ]
    for label, millis in units:
        if abs_ms >= millis:
            value = round(abs_ms / millis)
            return f"{value}{label} ago" if delta_ms < 0 else f"in {value}{label}"
    return "just now"


def format_signed_number(value: float) -> str:
    rounded = float(f"{value:.6g}")
    prefix = "+" if rounded >= 0 else ""
    return f"{prefix}{rounded}"


def format_experiment_summary(
    sha: str,
    date: str,
    summary: str | None,
    metrics: dict[str, MetricValue] | None,
    primary_metric: PrimaryMetricSpec | None,
    extra_label: str = "",
) -> str:
    metric_summary = ""
    if primary_metric is not None and metrics is not None:
        primary_value = metrics.get(primary_metric.metric)
        if is_number(primary_value):
            metric_summary = f"{primary_metric.metric}={json.dumps(primary_value)}"
    elif metrics is not None:
        metric_summary = format_metric_summary(metrics)
    detail_parts = [part for part in [extra_label, format_relative_time(date)] if part]
    detail = f"  ({', '.join(detail_parts)})" if detail_parts else ""
    return f"{short_sha(sha)}{f'  {metric_summary}' if metric_summary else ''}{detail}"


def truncate_status_summary(summary: str, max_length: int = 120) -> str:
    compact = re.sub(r"\s+", " ", summary).strip()
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


def format_recent_experiment_line(
    record: ExperimentRecord, primary_metric: PrimaryMetricSpec | None
) -> str:
    summary = truncate_status_summary(record.parsed.summary) if record.parsed else ""
    summary_suffix = f" | {summary}" if summary else ""
    return f"{format_experiment_summary(record.sha, record.date, record.parsed.summary if record.parsed else None, record.parsed.metrics if record.parsed else None, primary_metric)}{summary_suffix}"


def find_project_best_experiment_record(
    records: list[ExperimentRecord], primary_metric: PrimaryMetricSpec | None
) -> ExperimentRecord | None:
    if primary_metric is None:
        return None
    candidates = [
        record
        for record in records
        if record.parsed
        and record.parsed.metrics
        and is_number(record.parsed.metrics.get(primary_metric.metric))
    ]
    if not candidates:
        return None

    def sort_key(record: ExperimentRecord) -> tuple[int | float, str, str]:
        metric_value = get_record_numeric_metric_value(record, primary_metric.metric)
        if metric_value is None:
            raise AutoevolveError(f'Metric "{primary_metric.metric}" must be numeric for ranking.')
        ranked_value = metric_value if primary_metric.direction == "min" else -metric_value
        return (ranked_value, record.date, record.sha)

    return sorted(candidates, key=sort_key)[0]


def find_recent_experiment_records(
    records: list[ExperimentRecord], limit: int
) -> list[ExperimentRecord]:
    return sorted(records, key=lambda record: record.date, reverse=True)[:limit]


def build_recent_trend(
    records: list[ExperimentRecord], primary_metric: PrimaryMetricSpec | None
) -> RecentTrend | None:
    if primary_metric is None:
        return None
    sample = find_recent_experiment_records(
        [
            record
            for record in records
            if record.parsed
            and record.parsed.metrics
            and is_number(record.parsed.metrics.get(primary_metric.metric))
        ],
        5,
    )
    if len(sample) < 2:
        return None
    newest = sample[0]
    oldest = sample[-1]
    newest_value = get_record_numeric_metric_value(newest, primary_metric.metric)
    oldest_value = get_record_numeric_metric_value(oldest, primary_metric.metric)
    if not is_number(newest_value) or not is_number(oldest_value):
        return None
    newest_date = parse_iso_datetime(newest.date)
    oldest_date = parse_iso_datetime(oldest.date)
    span_ms = 0
    if newest_date and oldest_date:
        span_ms = max(
            0,
            int(
                (
                    newest_date.astimezone(timezone.utc) - oldest_date.astimezone(timezone.utc)
                ).total_seconds()
                * 1000
            ),
        )
    return RecentTrend(
        delta=float(newest_value - oldest_value),
        sample_size=len(sample),
        span_ms=span_ms,
    )


def format_duration_ms(duration_ms: int) -> str:
    if duration_ms <= 0:
        return "0m"
    units = [
        ("y", 365 * 24 * 60 * 60 * 1000),
        ("mo", 30 * 24 * 60 * 60 * 1000),
        ("w", 7 * 24 * 60 * 60 * 1000),
        ("d", 24 * 60 * 60 * 1000),
        ("h", 60 * 60 * 1000),
        ("m", 60 * 1000),
    ]
    for label, millis in units:
        if duration_ms >= millis:
            return f"{round(duration_ms / millis)}{label}"
    return "0m"


def _build_primary_metric_problems(
    metrics: dict[str, MetricValue] | None, primary_metric: PrimaryMetricSpec | None
) -> list[str]:
    if primary_metric is None:
        return []
    if metrics is None or primary_metric.metric not in metrics:
        return [f'missing primary metric "{primary_metric.metric}"']
    value = metrics[primary_metric.metric]
    if not is_number(value):
        return [f'primary metric "{primary_metric.metric}" is not numeric']
    return []


def _build_tip_status_entry(
    sha: str,
    branches: list[str],
    date: str | None,
    subject: str,
    summary: str | None,
    metrics: dict[str, MetricValue] | None,
    primary_metric: PrimaryMetricSpec | None,
    problems: list[str],
) -> TipStatusEntry:
    primary_metric_value = None
    if primary_metric is not None and metrics is not None:
        candidate = metrics.get(primary_metric.metric)
        if is_number(candidate):
            primary_metric_value = candidate
    return TipStatusEntry(
        sha=sha,
        branches=branches,
        date=date,
        subject=subject,
        summary=summary,
        metrics=metrics,
        problems=problems,
        primary_metric_value=primary_metric_value,
    )


def inspect_current_record_state(
    repo_root: str, primary_metric: PrimaryMetricSpec | None
) -> CurrentRecordState:
    has_journal = file_exists(repo_root, ROOT_FILES.journal)
    has_experiment = file_exists(repo_root, ROOT_FILES.experiment)
    problems: list[str] = []

    if not has_journal and not has_experiment:
        return CurrentRecordState(
            "missing",
            [f"no current experiment record; add {ROOT_FILES.journal} and {ROOT_FILES.experiment}"],
        )

    if not has_journal or not has_experiment:
        if not has_journal:
            problems.append(f"missing {ROOT_FILES.journal}")
        if not has_experiment:
            problems.append(f"missing {ROOT_FILES.experiment}")
        return CurrentRecordState("incomplete", problems)

    journal_text = read_text_file(repo_root, ROOT_FILES.journal).strip()
    if not journal_text:
        problems.append(f"{ROOT_FILES.journal} is empty")

    try:
        parsed_experiment = parse_experiment_json(read_text_file(repo_root, ROOT_FILES.experiment))
        problems.extend(_build_primary_metric_problems(parsed_experiment.metrics, primary_metric))
    except AutoevolveError as error:
        problems.append(f"invalid {ROOT_FILES.experiment}: {error}")

    if problems:
        return CurrentRecordState("invalid", problems)
    return CurrentRecordState("recorded", [])


def _get_commit_metadata(repo_root: str, ref: str) -> tuple[str, str]:
    output = run_git(repo_root, ["show", "-s", "--format=%cI%x09%s", ref]).strip()
    parts = output.split("\t", 1)
    if len(parts) != 2 or not parts[0]:
        raise AutoevolveError(f"Unexpected git show output: {output}")
    return parts[0], parts[1]


def inspect_active_tip_entry(
    repo_root: str,
    sha: str,
    branches: list[str],
    record_map: dict[str, ExperimentRecord],
    primary_metric: PrimaryMetricSpec | None,
) -> tuple[TipStatusEntry, str]:
    record = record_map.get(sha)
    if record is not None:
        problems: list[str] = []
        if not record.journal_text.strip():
            problems.append(f"{ROOT_FILES.journal} is empty")
        if record.parse_error:
            problems.append(f"invalid {ROOT_FILES.experiment}: {record.parse_error}")
        else:
            problems.extend(
                _build_primary_metric_problems(
                    record.parsed.metrics if record.parsed else None, primary_metric
                )
            )
        kind = "ok" if not problems else "invalid"
        return (
            _build_tip_status_entry(
                sha,
                branches,
                record.date,
                record.subject,
                record.parsed.summary if record.parsed else None,
                record.parsed.metrics if record.parsed else None,
                primary_metric,
                problems,
            ),
            kind,
        )

    journal_text = try_read_file_at_ref(repo_root, sha, ROOT_FILES.journal)
    experiment_text = try_read_file_at_ref(repo_root, sha, ROOT_FILES.experiment)
    missing_problems: list[str] = []
    if journal_text is None and experiment_text is None:
        missing_problems.append(
            f"tip does not contain {ROOT_FILES.journal} or {ROOT_FILES.experiment}"
        )
    else:
        if journal_text is None:
            missing_problems.append(f"missing {ROOT_FILES.journal} at branch tip")
        if experiment_text is None:
            missing_problems.append(f"missing {ROOT_FILES.experiment} at branch tip")
    date, subject = _get_commit_metadata(repo_root, sha)
    return (
        _build_tip_status_entry(
            sha,
            branches,
            date,
            subject,
            None,
            None,
            primary_metric,
            missing_problems,
        ),
        "missing",
    )


def _compare_tip_entries_by_metric(
    left: TipStatusEntry, right: TipStatusEntry, primary_metric: PrimaryMetricSpec
) -> int:
    left_value = left.primary_metric_value
    right_value = right.primary_metric_value
    if left_value is None and right_value is None:
        left_date = left.date or ""
        right_date = right.date or ""
        if right_date > left_date:
            return 1
        if right_date < left_date:
            return -1
        return 0
    if left_value is None:
        return 1
    if right_value is None:
        return -1
    if left_value == right_value:
        if (right.date or "") > (left.date or ""):
            return 1
        if (right.date or "") < (left.date or ""):
            return -1
        return 0
    if primary_metric.direction == "min":
        return -1 if left_value < right_value else 1
    return -1 if left_value > right_value else 1


def sort_tip_entries(
    entries: list[TipStatusEntry], primary_metric: PrimaryMetricSpec | None
) -> list[TipStatusEntry]:
    def compare(left: TipStatusEntry, right: TipStatusEntry) -> int:
        if primary_metric is not None:
            metric_comparison = _compare_tip_entries_by_metric(left, right, primary_metric)
            if metric_comparison != 0:
                return metric_comparison
        left_date = left.date or ""
        right_date = right.date or ""
        if left_date and right_date and left_date != right_date:
            if right_date > left_date:
                return 1
            return -1
        left_branch = left.branches[0] if left.branches else ""
        right_branch = right.branches[0] if right.branches else ""
        if left_branch < right_branch:
            return -1
        if left_branch > right_branch:
            return 1
        return 0

    return sorted(entries, key=cmp_to_key(compare))


def _get_parents(repo_root: str, ref: str) -> list[str]:
    output = run_git(repo_root, ["rev-list", "--parents", "-n", "1", ref]).strip()
    if not output:
        return []
    return output.split(" ")[1:]


def code_diff_pathspec_args() -> list[str]:
    return [
        "--",
        ".",
        f":(exclude){ROOT_FILES.experiment}",
        f":(exclude){ROOT_FILES.journal}",
    ]


def find_git_experiment_ancestor(
    repo_root: str, starting_sha: str, experiment_shas: set[str]
) -> str | None:
    current: str | None = starting_sha
    while current is not None:
        if current in experiment_shas:
            return current
        parents = _get_parents(repo_root, current)
        current = parents[0] if parents else None
    return None


def format_experiment_line(record: ExperimentRecord) -> str:
    tips = f" [{', '.join(record.tip_branches)}]" if record.tip_branches else ""
    details: list[str] = []
    if record.parsed:
        metrics = format_metric_pairs(record.parsed.metrics)
        if metrics:
            details.append(metrics)
        if record.parsed.summary:
            details.append(record.parsed.summary)
    elif record.parse_error:
        details.append(f"invalid EXPERIMENT.json: {record.parse_error}")
    suffix = f" - {' | '.join(details)}" if details else ""
    return f"{short_sha(record.sha)}  {record.date}  {record.subject}{tips}{suffix}"


def build_git_parent_map(repo_root: str, records: list[ExperimentRecord]) -> dict[str, list[str]]:
    experiment_shas = {record.sha for record in records}
    parent_map: dict[str, list[str]] = {}
    for record in records:
        parents = _get_parents(repo_root, record.sha)
        compressed_parents: list[str] = []
        for parent in parents:
            ancestor = find_git_experiment_ancestor(repo_root, parent, experiment_shas)
            if ancestor and ancestor not in compressed_parents:
                compressed_parents.append(ancestor)
        parent_map[record.sha] = compressed_parents
    return parent_map


def build_git_child_map(parent_map: dict[str, list[str]]) -> dict[str, list[str]]:
    child_map: dict[str, list[str]] = {}
    for child, parents in parent_map.items():
        for parent in parents:
            child_map.setdefault(parent, []).append(child)
    return child_map


def build_incoming_reference_map(
    records: list[ExperimentRecord],
) -> dict[str, list[tuple[str, str]]]:
    incoming_map: dict[str, list[tuple[str, str]]] = {}
    for record in records:
        for reference in (record.parsed.references or []) if record.parsed else []:
            incoming_map.setdefault(reference.commit, []).append((record.sha, reference.why))
    return incoming_map


def _get_merge_base(repo_root: str, left_sha: str, right_sha: str) -> str | None:
    return try_git(repo_root, ["merge-base", left_sha, right_sha]) or None


def describe_git_relationship(
    repo_root: str,
    left_sha: str,
    right_sha: str,
    git_parent_map: dict[str, list[str]],
) -> GitRelationship:
    if left_sha == right_sha:
        return GitRelationship("same", left_sha, [])

    right_parents = git_parent_map.get(right_sha, [])
    if left_sha in right_parents:
        return GitRelationship("direct_parent_of_right", left_sha, [])

    left_parents = git_parent_map.get(left_sha, [])
    if right_sha in left_parents:
        return GitRelationship("direct_parent_of_left", right_sha, [])

    shared_parents = sorted(parent for parent in left_parents if parent in right_parents)
    if shared_parents:
        return GitRelationship(
            "sibling", _get_merge_base(repo_root, left_sha, right_sha), shared_parents
        )

    merge_base = _get_merge_base(repo_root, left_sha, right_sha)
    if merge_base == left_sha:
        return GitRelationship("left_ancestor_of_right", merge_base, [])
    if merge_base == right_sha:
        return GitRelationship("right_ancestor_of_left", merge_base, [])
    return GitRelationship("diverged", merge_base, [])


def build_changed_paths(repo_root: str, left_sha: str, right_sha: str) -> list[ChangedPath]:
    output = run_git(
        repo_root,
        ["diff", "--name-status", left_sha, right_sha, *code_diff_pathspec_args()],
    ).strip()
    if not output:
        return []
    changed_paths: list[ChangedPath] = []
    for line in output.splitlines():
        parts = line.split("\t")
        status = parts[0] if parts else ""
        first_path = parts[1] if len(parts) > 1 else ""
        second_path = parts[2] if len(parts) > 2 else None
        if not status or not first_path:
            raise AutoevolveError(f"Unexpected git diff --name-status output: {line}")
        if status.startswith(("R", "C")):
            changed_paths.append(
                ChangedPath(
                    status=status,
                    path=second_path or first_path,
                    previous_path=first_path if second_path else None,
                )
            )
            continue
        changed_paths.append(ChangedPath(status=status, path=first_path))
    return changed_paths


def build_metric_diff(left: ExperimentRecord, right: ExperimentRecord) -> dict[str, MetricDelta]:
    metric_names = set(left.parsed.metrics.keys() if left.parsed and left.parsed.metrics else [])
    if right.parsed and right.parsed.metrics:
        metric_names.update(right.parsed.metrics.keys())
    diff: dict[str, MetricDelta] = {}
    for metric in sorted(metric_names):
        left_value = (
            left.parsed.metrics.get(metric) if left.parsed and left.parsed.metrics else None
        )
        right_value = (
            right.parsed.metrics.get(metric) if right.parsed and right.parsed.metrics else None
        )
        diff[metric] = MetricDelta(
            left=left_value,
            right=right_value,
            delta=right_value - left_value
            if is_number(left_value) and is_number(right_value)
            else None,
        )
    return diff


def build_reference_diff(left: ExperimentRecord, right: ExperimentRecord) -> ReferenceDiff:
    left_references = left.parsed.references if left.parsed and left.parsed.references else []
    right_references = right.parsed.references if right.parsed and right.parsed.references else []
    left_commits = {reference.commit for reference in left_references}
    right_commits = {reference.commit for reference in right_references}
    return ReferenceDiff(
        common=sorted(left_commits & right_commits),
        left_only=sorted(left_commits - right_commits),
        right_only=sorted(right_commits - left_commits),
    )


def format_metric_value(value: MetricValue) -> str:
    return "null" if value is None else json.dumps(value)


def read_primary_metric(repo_root: str) -> PrimaryMetricSpec | None:
    if not file_exists(repo_root, ROOT_FILES.problem):
        return None
    try:
        return parse_problem_primary_metric(read_text_file(repo_root, ROOT_FILES.problem))
    except ValueError:
        return None


def print_tip_entries(title: str, entries: list[TipStatusEntry]) -> None:
    if not entries:
        return
    click.echo(title)
    for entry in entries:
        click.echo(
            f"  {', '.join(entry.branches)} @ {entry.short_sha}: {'; '.join(entry.problems)}"
        )
    click.echo("")


def print_log_record(record: ExperimentRecord) -> None:
    summary = (
        f"invalid EXPERIMENT.json: {record.parse_error}"
        if record.parse_error
        else record.parsed.summary
        if record.parsed
        else "(none)"
    )
    metrics = (
        f"invalid EXPERIMENT.json: {record.parse_error}"
        if record.parse_error
        else format_metric_pairs(record.parsed.metrics if record.parsed else None) or "(none)"
    )
    journal_text = record.journal_text.strip() or "(none)"
    click.echo(f"commit {record.sha}")
    click.echo(f"Date:    {record.date}")
    if record.tip_branches:
        click.echo(f"Tips:    {', '.join(record.tip_branches)}")
    click.echo(f"Subject: {record.subject}")
    click.echo(f"Summary: {summary}")
    click.echo("Metrics:")
    if record.parse_error or metrics == "(none)":
        click.echo(f"  {metrics}")
    else:
        for name, value in (record.parsed.metrics or {}).items() if record.parsed else ():
            click.echo(f"  {name}: {json.dumps(value)}")
    click.echo("")
    click.echo("Journal:")
    for line in journal_text.splitlines():
        if not line:
            continue
        click.echo(f"  {line}")


def format_show_experiment_section(record: ExperimentRecord) -> str:
    if record.parse_error:
        return f"invalid: {record.parse_error}"
    if record.parsed is None:
        return "(none)"

    lines = [f"summary: {record.parsed.summary or '(none)'}", "metrics:"]
    if not record.parsed.metrics:
        lines.append("  (none)")
    else:
        for name, value in record.parsed.metrics.items():
            lines.append(f"  {name}: {json.dumps(value)}")

    lines.append("references:")
    if not record.parsed.references:
        lines.append("  (none)")
    else:
        for reference in record.parsed.references:
            lines.append(f"  {short_sha(reference.commit)}: {reference.why}")
    return "\n".join(lines)


def collect_graph(
    repo_root: str,
    records: list[ExperimentRecord],
    starting_sha: str,
    edge_mode: GraphEdges,
    traversal_direction: GraphDirection,
    max_depth: int | None,
) -> LineageGraph:
    record_map = {record.sha: record for record in records}
    git_parent_map = build_git_parent_map(repo_root, records)
    git_child_map = build_git_child_map(git_parent_map)
    incoming_reference_map = build_incoming_reference_map(records)
    include_git_edges = edge_mode in {"all", "git"}
    include_reference_edges = edge_mode in {"all", "references"}
    queue = deque([(0, starting_sha)])
    node_order: list[str] = []
    seen_depth: dict[str, int] = {}
    edge_keys: set[str] = set()
    edge_list: list[LineageEdge] = []

    while queue:
        current_depth, sha = queue.popleft()
        previous_depth = seen_depth.get(sha)
        if previous_depth is not None and previous_depth <= current_depth:
            continue
        seen_depth[sha] = current_depth
        node_order.append(sha)

        if max_depth is not None and current_depth >= max_depth:
            continue

        def enqueue(target_sha: str, next_depth: int) -> None:
            known_depth = seen_depth.get(target_sha)
            if known_depth is not None and known_depth <= next_depth:
                return
            queue.append((next_depth, target_sha))

        def add_edge(edge: LineageEdge) -> None:
            edge_key = f"{edge.kind}:{edge.source}:{edge.target}:{edge.why or ''}"
            if edge_key in edge_keys:
                return
            edge_keys.add(edge_key)
            edge_list.append(edge)

        if include_git_edges and traversal_direction in {"backward", "both"}:
            for parent_sha in git_parent_map.get(sha, []):
                add_edge(LineageEdge(kind="git", source=sha, target=parent_sha))
                enqueue(parent_sha, current_depth + 1)

        if include_git_edges and traversal_direction in {"forward", "both"}:
            for child_sha in git_child_map.get(sha, []):
                add_edge(LineageEdge(kind="git", source=child_sha, target=sha))
                enqueue(child_sha, current_depth + 1)

        if include_reference_edges and traversal_direction in {"backward", "both"}:
            record = record_map.get(sha)
            for reference in (record.parsed.references or []) if record and record.parsed else []:
                add_edge(
                    LineageEdge(
                        kind="reference",
                        source=sha,
                        target=reference.commit,
                        why=reference.why,
                    )
                )
                enqueue(reference.commit, current_depth + 1)

        if include_reference_edges and traversal_direction in {"forward", "both"}:
            for source, why in incoming_reference_map.get(sha, []):
                add_edge(
                    LineageEdge(
                        kind="reference",
                        source=source,
                        target=sha,
                        why=why,
                    )
                )
                enqueue(source, current_depth + 1)

    return LineageGraph(node_order=node_order, edges=edge_list)


def run_status() -> None:
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    branches = list_autoevolve_branches(repo_root)
    tip_map = build_tip_map(branches)
    record_map = {record.sha: record for record in records}
    worktrees = list_repo_worktrees(repo_root)
    primary_metric = read_primary_metric(repo_root)

    active_recorded_tips: list[TipStatusEntry] = []
    active_tips_missing_record: list[TipStatusEntry] = []
    active_tips_needing_attention: list[TipStatusEntry] = []
    for sha, tip_branches in tip_map.items():
        entry, kind = inspect_active_tip_entry(
            repo_root, sha, tip_branches, record_map, primary_metric
        )
        if kind == "ok":
            active_recorded_tips.append(entry)
        elif kind == "missing":
            active_tips_missing_record.append(entry)
        else:
            active_tips_needing_attention.append(entry)

    current_record_state = inspect_current_record_state(repo_root, primary_metric)
    head_sha = get_head_sha(repo_root)
    nearest_experiment_ancestor = find_git_experiment_ancestor(
        repo_root, head_sha, {record.sha for record in records}
    )
    nearest_experiment = record_map.get(nearest_experiment_ancestor or "")
    managed_worktrees = [
        worktree
        for worktree in worktrees
        if not worktree.is_primary and worktree.is_managed_experiment
    ]
    unmanaged_worktrees = [
        worktree
        for worktree in worktrees
        if not worktree.is_primary and not worktree.is_managed_experiment
    ]
    worktree_counts = summarize_worktree_counts(managed_worktrees)
    best_experiment = find_project_best_experiment_record(records, primary_metric)
    recent_trend = build_recent_trend(records, primary_metric)
    recent_records = find_recent_experiment_records(records, 5)

    click.echo("checkout:")
    click.echo(f"  branch: {get_current_branch_label(repo_root)}")
    click.echo(f"  head: {short_sha(head_sha)}")
    click.echo(f"  dirty: {'yes' if is_checkout_dirty(repo_root) else 'no'}")
    click.echo(f"  state: {current_record_state.kind}")
    if nearest_experiment is not None:
        click.echo(f"  nearest experiment ancestor: {short_sha(nearest_experiment.sha)}")
    if current_record_state.problems:
        click.echo("  problems:")
        for problem in current_record_state.problems:
            click.echo(f"    - {problem}")
    click.echo("")

    click.echo("project:")
    if primary_metric is not None:
        click.echo(f"  metric: {primary_metric.raw}")
    click.echo(f"  experiments: {len(records)} recorded ({worktree_counts.total} ongoing)")
    if best_experiment is not None:
        click.echo(
            "  best: "
            f"{format_experiment_summary(best_experiment.sha, best_experiment.date, best_experiment.parsed.summary if best_experiment.parsed else None, best_experiment.parsed.metrics if best_experiment.parsed else None, primary_metric)}"
        )
    if recent_trend is not None:
        click.echo(
            "  recent trend: "
            f"{format_signed_number(recent_trend.delta)} "
            f"over last {recent_trend.sample_size} recorded experiments "
            f"({format_duration_ms(recent_trend.span_ms)} span)"
        )
    click.echo("")

    click.echo("latest experiments:")
    if not recent_records:
        click.echo("  (none)")
    else:
        for record in recent_records:
            click.echo(f"  {format_recent_experiment_line(record, primary_metric)}")
    click.echo("")
    click.echo("ongoing experiments (managed worktrees):")
    if not managed_worktrees:
        click.echo("  (none)")
    else:
        for worktree in managed_worktrees:
            click.echo(f"  {format_managed_worktree_line(worktree)}")
    click.echo("")
    print_tip_entries(
        "tip branches needing attention:",
        sort_tip_entries(active_tips_needing_attention, None),
    )
    print_tip_entries(
        "tip branches missing experiment records:",
        sort_tip_entries(active_tips_missing_record, None),
    )
    if unmanaged_worktrees:
        click.echo("other linked worktrees:")
        for worktree in unmanaged_worktrees:
            click.echo(f"  {format_worktree_state(worktree)}")
        click.echo("")


def run_log(limit: int = 10) -> None:
    repo_root = find_repo_root(os.getcwd())
    records = apply_limit(
        sorted(
            get_experiment_records(repo_root),
            key=lambda record: record.date,
            reverse=True,
        ),
        limit,
    )
    if not records:
        click.echo("No experiments found.")
        return
    for index, record in enumerate(records):
        if index > 0:
            click.echo("")
        print_log_record(record)


def run_lineage(
    ref: str,
    edges: GraphEdges = "all",
    direction: GraphDirection = "backward",
    depth: int | None = 3,
) -> None:
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    record_map = {record.sha: record for record in records}
    starting_sha = resolve_ref(repo_root, ref)
    starting_record = record_map.get(starting_sha)
    if starting_record is None:
        raise AutoevolveError(f"{ref} is not an experiment commit.")
    graph = collect_graph(repo_root, records, starting_sha, edges, direction, depth)
    click.echo(f"root: {short_sha(starting_sha)}  {starting_record.subject}")
    click.echo(
        f"mode: edges={edges} direction={direction} depth={depth if depth is not None else 'all'}"
    )
    click.echo("")
    click.echo("nodes:")
    for sha in graph.node_order:
        record = record_map.get(sha)
        label = (
            f"{short_sha(sha)}  {record.subject}"
            if record is not None and record.subject
            else f"{short_sha(sha)}  [not an experiment commit]"
        )
        click.echo(f"  {label}")
    click.echo("")
    click.echo("edges:")
    if not graph.edges:
        click.echo("  (none)")
        return
    for edge in graph.edges:
        suffix = f" - {edge.why}" if edge.why else ""
        click.echo(f"  {edge.kind}  {short_sha(edge.source)} -> {short_sha(edge.target)}{suffix}")


def run_compare(left_ref: str, right_ref: str) -> None:
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    left_sha = resolve_ref(repo_root, left_ref)
    right_sha = resolve_ref(repo_root, right_ref)
    record_map = {record.sha: record for record in records}
    left_record = record_map.get(left_sha)
    right_record = record_map.get(right_sha)
    if left_record is None:
        raise AutoevolveError(f"{left_ref} is not an experiment commit.")
    if right_record is None:
        raise AutoevolveError(f"{right_ref} is not an experiment commit.")
    git_parent_map = build_git_parent_map(repo_root, records)
    git_relationship = describe_git_relationship(repo_root, left_sha, right_sha, git_parent_map)
    metric_diff = build_metric_diff(left_record, right_record)
    reference_diff = build_reference_diff(left_record, right_record)
    changed_paths = build_changed_paths(repo_root, left_sha, right_sha)
    diffstat = run_git(
        repo_root,
        ["diff", "--shortstat", left_sha, right_sha, *code_diff_pathspec_args()],
    ).strip()
    diff_text = run_git(
        repo_root, ["diff", left_sha, right_sha, *code_diff_pathspec_args()]
    ).rstrip()
    click.echo(f"left:  {format_experiment_line(left_record)}")
    click.echo(f"right: {format_experiment_line(right_record)}")
    git_details: list[str] = []
    if git_relationship.merge_base:
        git_details.append(f"merge-base {short_sha(git_relationship.merge_base.strip())}")
    if git_relationship.shared_parents:
        suffix = "" if len(git_relationship.shared_parents) == 1 else "s"
        git_details.append(
            f"shared experiment parent{suffix} "
            f"{', '.join(short_sha(sha) for sha in git_relationship.shared_parents)}"
        )
    git_suffix = f" ({'; '.join(git_details)})" if git_details else ""
    click.echo(f"git:   {git_relationship.relationship}{git_suffix}")
    if diffstat:
        click.echo(f"diff:  {diffstat}")
    click.echo("")
    click.echo("changed paths:")
    if not changed_paths:
        click.echo("  (none)")
    else:
        for changed_path in changed_paths:
            if changed_path.previous_path:
                click.echo(
                    f"  {changed_path.status}  {changed_path.previous_path} -> {changed_path.path}"
                )
            else:
                click.echo(f"  {changed_path.status}  {changed_path.path}")
    click.echo("")
    click.echo("metrics:")
    metric_names = list(metric_diff.keys())
    if not metric_names:
        click.echo("  (none)")
    else:
        for metric, entry in metric_diff.items():
            delta_text = f" ({entry.delta:+g})" if entry.delta is not None else ""
            click.echo(
                f"  {metric}: {format_metric_value(entry.left)} "
                f"-> {format_metric_value(entry.right)}{delta_text}"
            )
    click.echo("")
    click.echo("references:")
    click.echo(
        "  common: "
        + (
            ", ".join(short_sha(commit) for commit in reference_diff.common)
            if reference_diff.common
            else "(none)"
        )
    )
    click.echo(
        "  left only: "
        + (
            ", ".join(short_sha(commit) for commit in reference_diff.left_only)
            if reference_diff.left_only
            else "(none)"
        )
    )
    click.echo(
        "  right only: "
        + (
            ", ".join(short_sha(commit) for commit in reference_diff.right_only)
            if reference_diff.right_only
            else "(none)"
        )
    )
    click.echo("")
    click.echo(f"left summary:  {left_record.parsed.summary if left_record.parsed else '(none)'}")
    click.echo(f"right summary: {right_record.parsed.summary if right_record.parsed else '(none)'}")
    click.echo("")
    click.echo("code diff:")
    if not diff_text:
        click.echo("  (none)")
        return
    click.echo(diff_text)


def run_show(ref: str) -> None:
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    record_map = {record.sha: record for record in records}
    sha = resolve_ref(repo_root, ref)
    record = record_map.get(sha)
    if record is None:
        raise AutoevolveError(f"{ref} is not an experiment commit.")
    journal = try_read_file_at_ref(repo_root, ref, ROOT_FILES.journal)
    experiment_text = try_read_file_at_ref(repo_root, ref, ROOT_FILES.experiment)
    if journal is None and experiment_text is None:
        raise AutoevolveError(
            f"{ref} does not contain {ROOT_FILES.journal} or {ROOT_FILES.experiment}"
        )
    diff_base: str | None = None
    for parent_sha in build_git_parent_map(repo_root, records).get(sha, []):
        diff_base = parent_sha
        break
    if diff_base is None:
        parents = _get_parents(repo_root, sha)
        diff_base = parents[0] if parents else None
    diff_text = (
        None
        if diff_base is None
        else run_git(repo_root, ["diff", diff_base, sha, *code_diff_pathspec_args()]).rstrip()
    )

    sections = [
        ("journal", journal.rstrip() if journal is not None else None),
        ("experiment", format_show_experiment_section(record)),
        ("code diff", diff_text or "(none)"),
    ]
    rendered_sections = [(label, content) for label, content in sections if content is not None]
    for index, (label, content) in enumerate(rendered_sections):
        click.echo(f"{label}:")
        for line in content.splitlines():
            click.echo(f"  {line}" if line else "")
        if index < len(rendered_sections) - 1:
            click.echo("")
