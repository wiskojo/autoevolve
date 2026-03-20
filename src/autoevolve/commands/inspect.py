from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any

import click

from autoevolve.commands.shared import (
    apply_limit,
    build_changed_paths,
    build_experiment_object_for_output,
    build_experiment_output_by_sha,
    build_git_child_map,
    build_git_parent_map,
    build_incoming_reference_map,
    build_metric_diff,
    build_parent_metric_delta,
    build_reference_diff,
    build_tip_map,
    describe_git_relationship,
    find_git_experiment_ancestor,
    format_experiment_line,
    format_metric_value,
    get_current_branch_label,
    get_experiment_records,
    get_head_sha,
    get_managed_experiment_name,
    get_record_numeric_metric_value,
    inspect_active_tip_entry,
    inspect_current_record_state,
    is_checkout_dirty,
    is_managed_experiment_branch,
    list_autoevolve_branches,
    list_repo_worktrees,
    parse_format,
    parse_positive_integer,
    resolve_ref,
    sort_tip_entries,
    try_read_file_at_ref,
)
from autoevolve.constants import ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import find_repo_root, run_git
from autoevolve.models import (
    CompareOptions,
    ExperimentRecord,
    GraphDirection,
    GraphEdges,
    GraphOptions,
    ListOptions,
    ObjectOutputFormat,
    PrimaryMetricSpec,
    ShowOptions,
    StatusOptions,
)
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.utils import (
    extract_excerpt,
    file_exists,
    format_metric_pairs,
    format_metric_summary,
    is_number,
    parse_experiment_json,
    parse_iso_datetime,
    read_text_file,
    short_sha,
)


def parse_status_options(args: list[str]) -> StatusOptions:
    output_format: ObjectOutputFormat = "text"
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("text", "json"),
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for status.')
        raise AutoevolveError(f'Unexpected argument "{token}" for status.')
    return StatusOptions(format=output_format)


def parse_list_options(args: list[str]) -> ListOptions:
    limit = 10
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for list.')
        raise AutoevolveError(f'Unexpected argument "{token}" for list.')
    return ListOptions(limit=limit)


def parse_depth(raw_value: str | None) -> int | None:
    normalized = (raw_value or "").strip().lower()
    if normalized == "all":
        return None
    return parse_positive_integer("--depth", raw_value)


def parse_graph_options(args: list[str]) -> GraphOptions:
    depth: int | None = 3
    direction: GraphDirection = "backward"
    edges: GraphEdges = "all"
    output_format: ObjectOutputFormat = "text"
    ref = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--edges":
            edges = parse_format(
                "--edges",
                args[index + 1] if index + 1 < len(args) else None,
                ("git", "references", "all"),
            )
            index += 2
            continue
        if token == "--direction":
            direction = parse_format(
                "--direction",
                args[index + 1] if index + 1 < len(args) else None,
                ("backward", "forward", "both"),
            )
            index += 2
            continue
        if token == "--depth":
            depth = parse_depth(args[index + 1] if index + 1 < len(args) else None)
            index += 2
            continue
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("text", "json"),
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for graph.')
        if not ref:
            ref = token
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for graph.')
    return GraphOptions(
        depth=depth,
        direction=direction,
        edges=edges,
        format=output_format,
        ref=ref,
    )


def parse_compare_options(args: list[str]) -> CompareOptions:
    output_format: ObjectOutputFormat = "text"
    left_ref = ""
    patch = False
    right_ref = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("text", "json"),
            )
            index += 2
            continue
        if token == "--patch":
            patch = True
            index += 1
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for compare.')
        if not left_ref:
            left_ref = token
            index += 1
            continue
        if not right_ref:
            right_ref = token
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for compare.')
    return CompareOptions(
        format=output_format,
        left_ref=left_ref,
        patch=patch,
        right_ref=right_ref,
    )


def parse_show_options(args: list[str]) -> ShowOptions:
    output_format: ObjectOutputFormat = "text"
    ref = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("text", "json"),
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for show.')
        if not ref:
            ref = token
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for show.')
    return ShowOptions(format=output_format, ref=ref)


def format_worktree_state(worktree: dict[str, Any]) -> str:
    labels = [worktree["branch"] or "(detached HEAD)"]
    if worktree["isCurrent"]:
        labels.append("current")
    if worktree["isPrimary"]:
        labels.append("primary")
    if worktree["isManagedExperiment"]:
        labels.append("managed")
    elif not worktree["isPrimary"]:
        labels.append("unmanaged")
    labels.append("missing" if worktree["isMissing"] else "dirty" if worktree["dirty"] else "clean")
    return f"{worktree['path']} [{', '.join(labels)}] @ {worktree['shortHead']}"


def format_managed_worktree_line(worktree: dict[str, Any]) -> str:
    branch_name = worktree["branch"]
    name = (
        get_managed_experiment_name(branch_name)
        if branch_name and is_managed_experiment_branch(branch_name)
        else os.path.basename(worktree["path"])
    )
    state = "missing" if worktree["isMissing"] else "dirty" if worktree["dirty"] else "clean"
    return f"{name} @ {worktree['shortHead']} ({state})"


def summarize_worktree_counts(worktrees: list[dict[str, Any]]) -> dict[str, int]:
    dirty = len([worktree for worktree in worktrees if worktree["dirty"]])
    missing = len([worktree for worktree in worktrees if worktree["isMissing"]])
    return {
        "clean": len(worktrees) - dirty - missing,
        "dirty": dirty,
        "missing": missing,
        "total": len(worktrees),
    }


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
    record: dict[str, Any],
    primary_metric: PrimaryMetricSpec | None,
    extra_label: str = "",
) -> str:
    metric_summary = ""
    if (
        primary_metric is not None
        and record.get("metrics") is not None
        and is_number(record["metrics"].get(primary_metric.metric))
    ):
        metric_summary = (
            f"{primary_metric.metric}={json.dumps(record['metrics'][primary_metric.metric])}"
        )
    elif record.get("metrics") is not None:
        metric_summary = format_metric_summary(record["metrics"])
    detail_parts = [part for part in [extra_label, format_relative_time(record["date"])] if part]
    detail = f"  ({', '.join(detail_parts)})" if detail_parts else ""
    return f"{record['short_sha']}{f'  {metric_summary}' if metric_summary else ''}{detail}"


def truncate_status_summary(summary: str, max_length: int = 120) -> str:
    compact = re.sub(r"\s+", " ", summary).strip()
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


def format_recent_experiment_line(
    record: dict[str, Any], primary_metric: PrimaryMetricSpec | None
) -> str:
    summary = truncate_status_summary(record["summary"]) if record.get("summary") else ""
    summary_suffix = f" | {summary}" if summary else ""
    return f"{format_experiment_summary(record, primary_metric)}{summary_suffix}"


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
) -> dict[str, Any] | None:
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
    return {
        "delta": newest_value - oldest_value,
        "sampleSize": len(sample),
        "spanMs": span_ms,
    }


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


def build_status_output(repo_root: str, records: list[ExperimentRecord]) -> dict[str, Any]:
    branches = list_autoevolve_branches(repo_root)
    tip_map = build_tip_map(branches)
    record_map = {record.sha: record for record in records}
    worktrees = list_repo_worktrees(repo_root)
    primary_metric = None
    if file_exists(repo_root, ROOT_FILES.problem):
        try:
            primary_metric = parse_problem_primary_metric(
                read_text_file(repo_root, ROOT_FILES.problem)
            )
        except Exception:
            primary_metric = None

    active_recorded_tips: list[dict[str, Any]] = []
    active_tips_missing_record: list[dict[str, Any]] = []
    active_tips_needing_attention: list[dict[str, Any]] = []

    for sha, tip_branches in tip_map.items():
        inspected, kind = inspect_active_tip_entry(
            repo_root, sha, tip_branches, record_map, primary_metric
        )
        if kind == "ok":
            active_recorded_tips.append(inspected)
        elif kind == "missing":
            active_tips_missing_record.append(inspected)
        else:
            active_tips_needing_attention.append(inspected)

    head_sha = get_head_sha(repo_root)
    nearest_experiment_ancestor = find_git_experiment_ancestor(
        repo_root, head_sha, {record.sha for record in records}
    )
    managed_worktrees = [
        worktree
        for worktree in worktrees
        if not worktree["isPrimary"] and worktree["isManagedExperiment"]
    ]
    best_experiment = find_project_best_experiment_record(records, primary_metric)
    recent_experiment_records = find_recent_experiment_records(records, 5)
    latest_experiment = recent_experiment_records[0] if recent_experiment_records else None

    return {
        "activeRecordedTips": sort_tip_entries(active_recorded_tips, primary_metric),
        "activeTipsMissingRecord": sort_tip_entries(active_tips_missing_record, None),
        "activeTipsNeedingAttention": sort_tip_entries(active_tips_needing_attention, None),
        "checkout": {
            "branch": get_current_branch_label(repo_root),
            "currentRecordState": inspect_current_record_state(repo_root, primary_metric),
            "dirty": is_checkout_dirty(repo_root),
            "head": head_sha,
            "nearestExperimentAncestor": (
                build_experiment_object_for_output(record_map[nearest_experiment_ancestor])
                if nearest_experiment_ancestor in record_map
                else None
            ),
            "shortHead": short_sha(head_sha),
        },
        "primaryMetric": (
            {
                "direction": primary_metric.direction,
                "metric": primary_metric.metric,
                "raw": primary_metric.raw,
            }
            if primary_metric
            else None
        ),
        "project": {
            "bestExperiment": (
                build_experiment_object_for_output(best_experiment) if best_experiment else None
            ),
            "latestExperiment": (
                build_experiment_object_for_output(latest_experiment) if latest_experiment else None
            ),
            "localBranchCounts": {
                "invalid": len(active_tips_needing_attention),
                "missingRecord": len(active_tips_missing_record),
                "recorded": len(active_recorded_tips),
                "total": len(active_recorded_tips)
                + len(active_tips_missing_record)
                + len(active_tips_needing_attention),
            },
            "managedWorktreeCounts": summarize_worktree_counts(managed_worktrees),
            "recentExperiments": [
                build_experiment_object_for_output(record) for record in recent_experiment_records
            ],
            "recentTrend": build_recent_trend(records, primary_metric),
            "totalExperiments": len(records),
        },
        "worktrees": worktrees,
    }


def _primary_metric_object(payload: dict[str, Any] | None) -> PrimaryMetricSpec | None:
    if payload is None:
        return None
    return PrimaryMetricSpec(
        direction=payload["direction"], metric=payload["metric"], raw=payload["raw"]
    )


def print_status_output(status: dict[str, Any]) -> None:
    primary_metric = _primary_metric_object(status["primaryMetric"])
    managed_worktrees = [
        worktree
        for worktree in status["worktrees"]
        if not worktree["isPrimary"] and worktree["isManagedExperiment"]
    ]
    unmanaged_worktrees = [
        worktree
        for worktree in status["worktrees"]
        if not worktree["isPrimary"] and not worktree["isManagedExperiment"]
    ]

    click.echo("project:")
    if status["primaryMetric"]:
        click.echo(f"  metric: {status['primaryMetric']['raw']}")
    click.echo(
        f"  experiments: {status['project']['totalExperiments']} recorded "
        f"({status['project']['managedWorktreeCounts']['total']} ongoing)"
    )
    if status["project"]["bestExperiment"]:
        click.echo(
            "  best: "
            f"{format_experiment_summary(status['project']['bestExperiment'], primary_metric)}"
        )
    if status["project"]["recentTrend"]:
        click.echo(
            "  recent trend: "
            f"{format_signed_number(status['project']['recentTrend']['delta'])} "
            f"over last {status['project']['recentTrend']['sampleSize']} recorded experiments "
            f"({format_duration_ms(status['project']['recentTrend']['spanMs'])} span)"
        )
    click.echo("")

    click.echo("latest experiments:")
    if not status["project"]["recentExperiments"]:
        click.echo("  (none)")
    else:
        for record in status["project"]["recentExperiments"]:
            click.echo(f"  {format_recent_experiment_line(record, primary_metric)}")
    click.echo("")
    click.echo("ongoing experiments (managed worktrees):")
    if not managed_worktrees:
        click.echo("  (none)")
    else:
        for worktree in managed_worktrees:
            click.echo(f"  {format_managed_worktree_line(worktree)}")
    click.echo("")
    if unmanaged_worktrees:
        click.echo("other linked worktrees:")
        for worktree in unmanaged_worktrees:
            click.echo(f"  {format_worktree_state(worktree)}")
        click.echo("")


def format_list_metrics(record: ExperimentRecord) -> str:
    if record.parse_error:
        return f"invalid EXPERIMENT.json: {record.parse_error}"
    return format_metric_pairs(record.parsed.metrics if record.parsed else None) or "(none)"


def format_list_summary(record: ExperimentRecord) -> str:
    if record.parse_error:
        return f"invalid EXPERIMENT.json: {record.parse_error}"
    return record.parsed.summary if record.parsed else "(none)"


def format_list_journal_excerpt(record: ExperimentRecord) -> str:
    return extract_excerpt(record.journal_text) or "(none)"


def print_list_record(record: ExperimentRecord) -> None:
    click.echo(f"{short_sha(record.sha)}  {record.date}  {record.subject}")
    click.echo(f"  summary: {format_list_summary(record)}")
    click.echo(f"  metrics: {format_list_metrics(record)}")
    click.echo(f"  journal: {format_list_journal_excerpt(record)}")


def should_include_graph_edge(kind: str, mode: str) -> bool:
    return mode == "all" or mode == kind


def collect_graph(
    repo_root: str,
    records: list[ExperimentRecord],
    starting_sha: str,
    options: GraphOptions,
) -> dict[str, Any]:
    record_map = {record.sha: record for record in records}
    git_parent_map = build_git_parent_map(repo_root, records)
    git_child_map = build_git_child_map(git_parent_map)
    incoming_reference_map = build_incoming_reference_map(records)
    queue = deque([(0, starting_sha)])
    node_order: list[str] = []
    seen_depth: dict[str, int] = {}
    edge_keys: set[str] = set()
    edges: list[dict[str, Any]] = []

    while queue:
        depth, sha = queue.popleft()
        previous_depth = seen_depth.get(sha)
        if previous_depth is not None and previous_depth <= depth:
            continue
        seen_depth[sha] = depth
        node_order.append(sha)

        if options.depth is not None and depth >= options.depth:
            continue

        def enqueue(target_sha: str, next_depth: int) -> None:
            known_depth = seen_depth.get(target_sha)
            if known_depth is not None and known_depth <= next_depth:
                return
            queue.append((next_depth, target_sha))

        def add_edge(edge: dict[str, Any]) -> None:
            edge_key = f"{edge['kind']}:{edge['from']}:{edge['to']}:{edge.get('why', '')}"
            if edge_key in edge_keys:
                return
            edge_keys.add(edge_key)
            edges.append(edge)

        if should_include_graph_edge("git", options.edges) and options.direction in {
            "backward",
            "both",
        }:
            for parent_sha in git_parent_map.get(sha, []):
                add_edge({"from": sha, "kind": "git", "to": parent_sha})
                enqueue(parent_sha, depth + 1)

        if should_include_graph_edge("git", options.edges) and options.direction in {
            "forward",
            "both",
        }:
            for child_sha in git_child_map.get(sha, []):
                add_edge({"from": child_sha, "kind": "git", "to": sha})
                enqueue(child_sha, depth + 1)

        if should_include_graph_edge("reference", options.edges) and options.direction in {
            "backward",
            "both",
        }:
            record = record_map.get(sha)
            for reference in (record.parsed.references or []) if record and record.parsed else []:
                add_edge(
                    {
                        "from": sha,
                        "kind": "reference",
                        "to": reference.commit,
                        "why": reference.why,
                    }
                )
                enqueue(reference.commit, depth + 1)

        if should_include_graph_edge("reference", options.edges) and options.direction in {
            "forward",
            "both",
        }:
            for incoming in incoming_reference_map.get(sha, []):
                add_edge(
                    {
                        "from": incoming["from"],
                        "kind": "reference",
                        "to": sha,
                        "why": incoming["why"],
                    }
                )
                enqueue(incoming["from"], depth + 1)

    return {"edges": edges, "nodeOrder": node_order}


def run_status(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    status = build_status_output(repo_root, get_experiment_records(repo_root))
    options = parse_status_options(args)
    if options.format == "json":
        click.echo(json.dumps(status, indent=2))
        return
    print_status_output(status)


def run_list(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    options = parse_list_options(args)
    records = apply_limit(
        sorted(
            get_experiment_records(repo_root),
            key=lambda record: record.date,
            reverse=True,
        ),
        options.limit,
    )
    if not records:
        click.echo("No experiments found.")
        return
    for index, record in enumerate(records):
        if index > 0:
            click.echo("")
        print_list_record(record)


def run_graph(args: list[str]) -> None:
    options = parse_graph_options(args)
    if not options.ref:
        raise AutoevolveError("graph requires a git ref, for example: autoevolve graph HEAD")
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    record_map = {record.sha: record for record in records}
    starting_sha = resolve_ref(repo_root, options.ref)
    starting_record = record_map.get(starting_sha)
    if starting_record is None:
        raise AutoevolveError(f"{options.ref} is not an experiment commit.")
    graph = collect_graph(repo_root, records, starting_sha, options)
    nodes = [build_experiment_output_by_sha(sha, record_map) for sha in graph["nodeOrder"]]
    if options.format == "json":
        click.echo(
            json.dumps(
                {
                    "depth": options.depth,
                    "direction": options.direction,
                    "edges": graph["edges"],
                    "mode": options.edges,
                    "nodes": nodes,
                    "ref": options.ref,
                    "root": starting_sha,
                },
                indent=2,
            )
        )
        return
    click.echo(f"root: {short_sha(starting_sha)}  {starting_record.subject}")
    click.echo(
        "mode: "
        f"edges={options.edges} direction={options.direction} "
        f"depth={options.depth if options.depth is not None else 'all'}"
    )
    click.echo("")
    click.echo("nodes:")
    for node in nodes:
        label = (
            f"{node['short_sha']}  {node['subject']}"
            if node["subject"]
            else f"{node['short_sha']}  [not an experiment commit]"
        )
        click.echo(f"  {label}")
    click.echo("")
    click.echo("edges:")
    if not graph["edges"]:
        click.echo("  (none)")
        return
    for edge in graph["edges"]:
        suffix = f" - {edge['why']}" if edge.get("why") else ""
        click.echo(
            f"  {edge['kind']}  {short_sha(edge['from'])} -> {short_sha(edge['to'])}{suffix}"
        )


def run_compare(args: list[str]) -> None:
    options = parse_compare_options(args)
    if not options.left_ref or not options.right_ref:
        raise AutoevolveError(
            "compare requires two git refs, for example: autoevolve compare HEAD HEAD~1"
        )
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    record_map = {record.sha: record for record in records}
    left_sha = resolve_ref(repo_root, options.left_ref)
    right_sha = resolve_ref(repo_root, options.right_ref)
    left_record = record_map.get(left_sha)
    right_record = record_map.get(right_sha)
    if left_record is None:
        raise AutoevolveError(f"{options.left_ref} is not an experiment commit.")
    if right_record is None:
        raise AutoevolveError(f"{options.right_ref} is not an experiment commit.")
    git_parent_map = build_git_parent_map(repo_root, records)
    git_relationship = describe_git_relationship(repo_root, left_sha, right_sha, git_parent_map)
    metric_diff = build_metric_diff(left_record, right_record)
    reference_diff = build_reference_diff(left_record, right_record)
    changed_paths = build_changed_paths(repo_root, left_sha, right_sha)
    parent_deltas = {
        "left": build_parent_metric_delta(left_record, git_parent_map, record_map),
        "right": build_parent_metric_delta(right_record, git_parent_map, record_map),
    }
    diffstat = run_git(repo_root, ["diff", "--shortstat", left_sha, right_sha]).strip()
    patch = run_git(repo_root, ["diff", left_sha, right_sha]).rstrip() if options.patch else None
    if options.format == "json":
        click.echo(
            json.dumps(
                {
                    "changedPaths": changed_paths,
                    "diffstat": diffstat or None,
                    "git": git_relationship,
                    "left": build_experiment_object_for_output(left_record),
                    "metrics": metric_diff,
                    "patch": patch,
                    "parentDeltas": parent_deltas,
                    "references": reference_diff,
                    "right": build_experiment_object_for_output(right_record),
                },
                indent=2,
            )
        )
        return
    click.echo(f"left:  {format_experiment_line(left_record)}")
    click.echo(f"right: {format_experiment_line(right_record)}")
    git_details: list[str] = []
    if git_relationship["mergeBase"]:
        git_details.append(f"merge-base {short_sha(git_relationship['mergeBase'].strip())}")
    if git_relationship["sharedParents"]:
        suffix = "" if len(git_relationship["sharedParents"]) == 1 else "s"
        git_details.append(
            f"shared experiment parent{suffix} "
            f"{', '.join(short_sha(sha) for sha in git_relationship['sharedParents'])}"
        )
    git_suffix = f" ({'; '.join(git_details)})" if git_details else ""
    click.echo(f"git:   {git_relationship['relationship']}{git_suffix}")
    if diffstat:
        click.echo(f"diff:  {diffstat}")
    click.echo("")
    click.echo("changed paths:")
    if not changed_paths:
        click.echo("  (none)")
    else:
        for changed_path in changed_paths:
            if changed_path["previousPath"]:
                click.echo(
                    "  "
                    f"{changed_path['status']}  {changed_path['previousPath']} "
                    f"-> {changed_path['path']}"
                )
            else:
                click.echo(f"  {changed_path['status']}  {changed_path['path']}")
    click.echo("")
    click.echo("metrics:")
    metric_names = list(metric_diff.keys())
    if not metric_names:
        click.echo("  (none)")
    else:
        for metric in metric_names:
            entry = metric_diff[metric]
            delta_text = f" ({entry['delta']:+g})" if entry["delta"] is not None else ""
            click.echo(
                f"  {metric}: {format_metric_value(entry['left'])} "
                f"-> {format_metric_value(entry['right'])}{delta_text}"
            )
    click.echo("")
    click.echo("references:")
    click.echo(
        "  common: "
        + (
            ", ".join(short_sha(commit) for commit in reference_diff["common"])
            if reference_diff["common"]
            else "(none)"
        )
    )
    click.echo(
        "  left only: "
        + (
            ", ".join(short_sha(commit) for commit in reference_diff["leftOnly"])
            if reference_diff["leftOnly"]
            else "(none)"
        )
    )
    click.echo(
        "  right only: "
        + (
            ", ".join(short_sha(commit) for commit in reference_diff["rightOnly"])
            if reference_diff["rightOnly"]
            else "(none)"
        )
    )
    click.echo("")
    click.echo("parent deltas:")
    if not parent_deltas["left"] and not parent_deltas["right"]:
        click.echo("  (none)")
    else:
        for label in ("left", "right"):
            delta = parent_deltas[label]
            if not delta:
                continue
            click.echo(f"  {label} vs {short_sha(delta['parent'])}:")
            for metric in sorted(delta["metrics"].keys()):
                entry = delta["metrics"][metric]
                click.echo(
                    f"    {metric}: {entry['parent']} -> {entry['current']} ({entry['delta']:+g})"
                )
    click.echo("")
    click.echo(f"left summary:  {left_record.parsed.summary if left_record.parsed else '(none)'}")
    click.echo(f"right summary: {right_record.parsed.summary if right_record.parsed else '(none)'}")
    if options.patch:
        click.echo("")
        click.echo("patch:")
        if not patch:
            click.echo("  (none)")
            return
        click.echo(patch)


def run_show(args: list[str]) -> None:
    options = parse_show_options(args)
    if not options.ref:
        raise AutoevolveError("show requires a git ref, for example: autoevolve show HEAD")
    repo_root = find_repo_root(os.getcwd())
    journal = try_read_file_at_ref(repo_root, options.ref, ROOT_FILES.journal)
    experiment_text = try_read_file_at_ref(repo_root, options.ref, ROOT_FILES.experiment)
    if journal is None and experiment_text is None:
        raise AutoevolveError(
            f"{options.ref} does not contain {ROOT_FILES.journal} or {ROOT_FILES.experiment}"
        )
    if options.format == "json":
        parsed = parse_experiment_json(experiment_text) if experiment_text is not None else None
        click.echo(
            json.dumps(
                {
                    "ref": options.ref,
                    "journal": journal,
                    "experiment": (
                        {
                            "summary": parsed.summary,
                            "metrics": parsed.metrics,
                            "references": (
                                [
                                    {"commit": reference.commit, "why": reference.why}
                                    for reference in (parsed.references or [])
                                ]
                                if parsed.references is not None
                                else None
                            ),
                        }
                        if parsed
                        else None
                    ),
                },
                indent=2,
            )
        )
        return
    if journal is not None:
        click.echo(f"# {ROOT_FILES.journal}")
        click.echo(journal.rstrip())
    if experiment_text is not None:
        if journal is not None:
            click.echo("")
        click.echo(f"# {ROOT_FILES.experiment}")
        click.echo(experiment_text.rstrip())
