from __future__ import annotations

import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from functools import cmp_to_key
from typing import Any

import click

from autoevolve.commands.shared import (
    apply_limit,
    build_experiment_object_for_output,
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
    ObjectOutputFormat,
    PrimaryMetricSpec,
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


def _build_primary_metric_problems(
    metrics: dict[str, Any] | None, primary_metric: PrimaryMetricSpec | None
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
    metrics: dict[str, Any] | None,
    primary_metric: PrimaryMetricSpec | None,
    problems: list[str],
) -> dict[str, Any]:
    primary_metric_value = None
    if primary_metric is not None and metrics is not None:
        candidate = metrics.get(primary_metric.metric)
        if is_number(candidate):
            primary_metric_value = candidate
    return {
        "branches": branches,
        "date": date,
        "metrics": metrics,
        "primaryMetricValue": primary_metric_value,
        "problems": problems,
        "sha": sha,
        "shortSha": short_sha(sha),
        "subject": subject,
        "summary": summary,
    }


def inspect_current_record_state(
    repo_root: str, primary_metric: PrimaryMetricSpec | None
) -> dict[str, Any]:
    has_journal = file_exists(repo_root, ROOT_FILES.journal)
    has_experiment = file_exists(repo_root, ROOT_FILES.experiment)
    problems: list[str] = []

    if not has_journal and not has_experiment:
        return {
            "kind": "missing",
            "problems": [
                (
                    "no current experiment record; add "
                    f"{ROOT_FILES.journal} and {ROOT_FILES.experiment}"
                )
            ],
        }

    if not has_journal or not has_experiment:
        if not has_journal:
            problems.append(f"missing {ROOT_FILES.journal}")
        if not has_experiment:
            problems.append(f"missing {ROOT_FILES.experiment}")
        return {"kind": "incomplete", "problems": problems}

    journal_text = read_text_file(repo_root, ROOT_FILES.journal).strip()
    if not journal_text:
        problems.append(f"{ROOT_FILES.journal} is empty")

    try:
        parsed_experiment = parse_experiment_json(read_text_file(repo_root, ROOT_FILES.experiment))
        problems.extend(_build_primary_metric_problems(parsed_experiment.metrics, primary_metric))
    except AutoevolveError as error:
        problems.append(f"invalid {ROOT_FILES.experiment}: {error}")

    if problems:
        return {"kind": "invalid", "problems": problems}
    return {"kind": "recorded", "problems": []}


def _get_commit_metadata(repo_root: str, ref: str) -> dict[str, str]:
    output = run_git(repo_root, ["show", "-s", "--format=%cI%x09%s", ref]).strip()
    parts = output.split("\t", 1)
    if len(parts) != 2 or not parts[0]:
        raise AutoevolveError(f"Unexpected git show output: {output}")
    return {"date": parts[0], "subject": parts[1]}


def inspect_active_tip_entry(
    repo_root: str,
    sha: str,
    branches: list[str],
    record_map: dict[str, ExperimentRecord],
    primary_metric: PrimaryMetricSpec | None,
) -> tuple[dict[str, Any], str]:
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
    metadata = _get_commit_metadata(repo_root, sha)
    return (
        _build_tip_status_entry(
            sha,
            branches,
            metadata["date"],
            metadata["subject"],
            None,
            None,
            primary_metric,
            missing_problems,
        ),
        "missing",
    )


def _compare_tip_entries_by_metric(
    left: dict[str, Any], right: dict[str, Any], primary_metric: PrimaryMetricSpec
) -> int:
    left_value = left.get("primaryMetricValue")
    right_value = right.get("primaryMetricValue")
    if left_value is None and right_value is None:
        left_date = left.get("date") or ""
        right_date = right.get("date") or ""
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
        if (right.get("date") or "") > (left.get("date") or ""):
            return 1
        if (right.get("date") or "") < (left.get("date") or ""):
            return -1
        return 0
    if primary_metric.direction == "min":
        return -1 if left_value < right_value else 1
    return -1 if left_value > right_value else 1


def sort_tip_entries(
    entries: list[dict[str, Any]], primary_metric: PrimaryMetricSpec | None
) -> list[dict[str, Any]]:
    def compare(left: dict[str, Any], right: dict[str, Any]) -> int:
        if primary_metric is not None:
            metric_comparison = _compare_tip_entries_by_metric(left, right, primary_metric)
            if metric_comparison != 0:
                return metric_comparison
        left_date = left.get("date") or ""
        right_date = right.get("date") or ""
        if left_date and right_date and left_date != right_date:
            if right_date > left_date:
                return 1
            return -1
        left_branch = left.get("branches", [""])[0] if left.get("branches") else ""
        right_branch = right.get("branches", [""])[0] if right.get("branches") else ""
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


def build_experiment_output_by_sha(
    sha: str, record_map: dict[str, ExperimentRecord]
) -> dict[str, Any]:
    record = record_map.get(sha)
    if record is not None:
        return build_experiment_object_for_output(record)
    return {
        "sha": sha,
        "short_sha": short_sha(sha),
        "date": "",
        "journal_excerpt": "",
        "metrics": None,
        "parse_error": "not an experiment commit",
        "references": None,
        "subject": "",
        "summary": None,
        "tips": [],
    }


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
) -> dict[str, list[dict[str, str]]]:
    incoming_map: dict[str, list[dict[str, str]]] = {}
    for record in records:
        for reference in (record.parsed.references or []) if record.parsed else []:
            incoming_map.setdefault(reference.commit, []).append(
                {"from": record.sha, "why": reference.why}
            )
    return incoming_map


def _get_merge_base(repo_root: str, left_sha: str, right_sha: str) -> str | None:
    return try_git(repo_root, ["merge-base", left_sha, right_sha]) or None


def describe_git_relationship(
    repo_root: str,
    left_sha: str,
    right_sha: str,
    git_parent_map: dict[str, list[str]],
) -> dict[str, Any]:
    if left_sha == right_sha:
        return {"mergeBase": left_sha, "relationship": "same", "sharedParents": []}

    right_parents = git_parent_map.get(right_sha, [])
    if left_sha in right_parents:
        return {
            "mergeBase": left_sha,
            "relationship": "direct_parent_of_right",
            "sharedParents": [],
        }

    left_parents = git_parent_map.get(left_sha, [])
    if right_sha in left_parents:
        return {
            "mergeBase": right_sha,
            "relationship": "direct_parent_of_left",
            "sharedParents": [],
        }

    shared_parents = sorted(parent for parent in left_parents if parent in right_parents)
    if shared_parents:
        return {
            "mergeBase": _get_merge_base(repo_root, left_sha, right_sha),
            "relationship": "sibling",
            "sharedParents": shared_parents,
        }

    merge_base = _get_merge_base(repo_root, left_sha, right_sha)
    if merge_base == left_sha:
        return {
            "mergeBase": merge_base,
            "relationship": "left_ancestor_of_right",
            "sharedParents": [],
        }
    if merge_base == right_sha:
        return {
            "mergeBase": merge_base,
            "relationship": "right_ancestor_of_left",
            "sharedParents": [],
        }
    return {"mergeBase": merge_base, "relationship": "diverged", "sharedParents": []}


def build_changed_paths(repo_root: str, left_sha: str, right_sha: str) -> list[dict[str, Any]]:
    output = run_git(repo_root, ["diff", "--name-status", left_sha, right_sha]).strip()
    if not output:
        return []
    changed_paths: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        status = parts[0] if parts else ""
        first_path = parts[1] if len(parts) > 1 else ""
        second_path = parts[2] if len(parts) > 2 else None
        if not status or not first_path:
            raise AutoevolveError(f"Unexpected git diff --name-status output: {line}")
        if status.startswith(("R", "C")):
            changed_paths.append(
                {
                    "path": second_path or first_path,
                    "previousPath": first_path if second_path else None,
                    "status": status,
                }
            )
            continue
        changed_paths.append({"path": first_path, "previousPath": None, "status": status})
    return changed_paths


def build_parent_metric_delta(
    record: ExperimentRecord,
    git_parent_map: dict[str, list[str]],
    record_map: dict[str, ExperimentRecord],
) -> dict[str, Any] | None:
    parents = git_parent_map.get(record.sha, [])
    if len(parents) != 1:
        return None
    parent_sha = parents[0]
    parent_record = record_map.get(parent_sha)
    if parent_record is None:
        return None
    metric_names = set(
        parent_record.parsed.metrics.keys()
        if parent_record.parsed and parent_record.parsed.metrics
        else []
    )
    if record.parsed and record.parsed.metrics:
        metric_names.update(record.parsed.metrics.keys())
    metrics: dict[str, Any] = {}
    for metric in metric_names:
        parent_value = (
            parent_record.parsed.metrics.get(metric)
            if parent_record.parsed and parent_record.parsed.metrics
            else None
        )
        current_value = (
            record.parsed.metrics.get(metric) if record.parsed and record.parsed.metrics else None
        )
        if not is_number(parent_value) or not is_number(current_value):
            continue
        metrics[metric] = {
            "current": current_value,
            "delta": current_value - parent_value,
            "parent": parent_value,
        }
    if not metrics:
        return None
    return {"metrics": metrics, "parent": parent_sha}


def build_metric_diff(left: ExperimentRecord, right: ExperimentRecord) -> dict[str, Any]:
    metric_names = set(left.parsed.metrics.keys() if left.parsed and left.parsed.metrics else [])
    if right.parsed and right.parsed.metrics:
        metric_names.update(right.parsed.metrics.keys())
    diff: dict[str, Any] = {}
    for metric in sorted(metric_names):
        left_value = (
            left.parsed.metrics.get(metric) if left.parsed and left.parsed.metrics else None
        )
        right_value = (
            right.parsed.metrics.get(metric) if right.parsed and right.parsed.metrics else None
        )
        diff[metric] = {
            "left": left_value,
            "right": right_value,
            "delta": right_value - left_value
            if is_number(left_value) and is_number(right_value)
            else None,
        }
    return diff


def build_reference_diff(left: ExperimentRecord, right: ExperimentRecord) -> dict[str, list[str]]:
    left_references = left.parsed.references if left.parsed and left.parsed.references else []
    right_references = right.parsed.references if right.parsed and right.parsed.references else []
    left_commits = {reference.commit for reference in left_references}
    right_commits = {reference.commit for reference in right_references}
    return {
        "common": sorted(left_commits & right_commits),
        "leftOnly": sorted(left_commits - right_commits),
        "rightOnly": sorted(right_commits - left_commits),
    }


def format_metric_value(value: Any) -> str:
    return "null" if value is None else json.dumps(value)


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


def print_status_output(status: dict[str, Any]) -> None:
    primary_metric_payload = status["primaryMetric"]
    primary_metric = (
        None
        if primary_metric_payload is None
        else PrimaryMetricSpec(
            direction=primary_metric_payload["direction"],
            metric=primary_metric_payload["metric"],
            raw=primary_metric_payload["raw"],
        )
    )
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


def print_list_record(record: ExperimentRecord) -> None:
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
    journal_excerpt = extract_excerpt(record.journal_text) or "(none)"
    click.echo(f"{short_sha(record.sha)}  {record.date}  {record.subject}")
    click.echo(f"  summary: {summary}")
    click.echo(f"  metrics: {metrics}")
    click.echo(f"  journal: {journal_excerpt}")


def collect_graph(
    repo_root: str,
    records: list[ExperimentRecord],
    starting_sha: str,
    edge_mode: GraphEdges,
    traversal_direction: GraphDirection,
    max_depth: int | None,
) -> dict[str, Any]:
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
    edge_list: list[dict[str, Any]] = []

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

        def add_edge(edge: dict[str, Any]) -> None:
            edge_key = f"{edge['kind']}:{edge['from']}:{edge['to']}:{edge.get('why', '')}"
            if edge_key in edge_keys:
                return
            edge_keys.add(edge_key)
            edge_list.append(edge)

        if include_git_edges and traversal_direction in {"backward", "both"}:
            for parent_sha in git_parent_map.get(sha, []):
                add_edge({"from": sha, "kind": "git", "to": parent_sha})
                enqueue(parent_sha, current_depth + 1)

        if include_git_edges and traversal_direction in {"forward", "both"}:
            for child_sha in git_child_map.get(sha, []):
                add_edge({"from": child_sha, "kind": "git", "to": sha})
                enqueue(child_sha, current_depth + 1)

        if include_reference_edges and traversal_direction in {"backward", "both"}:
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
                enqueue(reference.commit, current_depth + 1)

        if include_reference_edges and traversal_direction in {"forward", "both"}:
            for incoming in incoming_reference_map.get(sha, []):
                add_edge(
                    {
                        "from": incoming["from"],
                        "kind": "reference",
                        "to": sha,
                        "why": incoming["why"],
                    }
                )
                enqueue(incoming["from"], current_depth + 1)

    return {"edges": edge_list, "nodeOrder": node_order}


def run_status(output_format: ObjectOutputFormat = "text") -> None:
    repo_root = find_repo_root(os.getcwd())
    status = build_status_output(repo_root, get_experiment_records(repo_root))
    if output_format == "json":
        click.echo(json.dumps(status, indent=2))
        return
    print_status_output(status)


def run_list(limit: int = 10) -> None:
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
        print_list_record(record)


def run_graph(
    ref: str,
    edges: GraphEdges = "all",
    direction: GraphDirection = "backward",
    depth: int | None = 3,
    output_format: ObjectOutputFormat = "text",
) -> None:
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    record_map = {record.sha: record for record in records}
    starting_sha = resolve_ref(repo_root, ref)
    starting_record = record_map.get(starting_sha)
    if starting_record is None:
        raise AutoevolveError(f"{ref} is not an experiment commit.")
    graph = collect_graph(repo_root, records, starting_sha, edges, direction, depth)
    nodes = [build_experiment_output_by_sha(sha, record_map) for sha in graph["nodeOrder"]]
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "depth": depth,
                    "direction": direction,
                    "edges": graph["edges"],
                    "mode": edges,
                    "nodes": nodes,
                    "ref": ref,
                    "root": starting_sha,
                },
                indent=2,
            )
        )
        return
    click.echo(f"root: {short_sha(starting_sha)}  {starting_record.subject}")
    click.echo(
        f"mode: edges={edges} direction={direction} depth={depth if depth is not None else 'all'}"
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


def run_compare(
    left_ref: str,
    right_ref: str,
    output_format: ObjectOutputFormat = "text",
    patch: bool = False,
) -> None:
    repo_root = find_repo_root(os.getcwd())
    records = get_experiment_records(repo_root)
    record_map = {record.sha: record for record in records}
    left_sha = resolve_ref(repo_root, left_ref)
    right_sha = resolve_ref(repo_root, right_ref)
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
    parent_deltas = {
        "left": build_parent_metric_delta(left_record, git_parent_map, record_map),
        "right": build_parent_metric_delta(right_record, git_parent_map, record_map),
    }
    diffstat = run_git(repo_root, ["diff", "--shortstat", left_sha, right_sha]).strip()
    patch_text = run_git(repo_root, ["diff", left_sha, right_sha]).rstrip() if patch else None
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "changedPaths": changed_paths,
                    "diffstat": diffstat or None,
                    "git": git_relationship,
                    "left": build_experiment_object_for_output(left_record),
                    "metrics": metric_diff,
                    "patch": patch_text,
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
    if patch:
        click.echo("")
        click.echo("patch:")
        if not patch_text:
            click.echo("  (none)")
            return
        click.echo(patch_text)


def run_show(ref: str, output_format: ObjectOutputFormat = "text") -> None:
    repo_root = find_repo_root(os.getcwd())
    journal = try_read_file_at_ref(repo_root, ref, ROOT_FILES.journal)
    experiment_text = try_read_file_at_ref(repo_root, ref, ROOT_FILES.experiment)
    if journal is None and experiment_text is None:
        raise AutoevolveError(
            f"{ref} does not contain {ROOT_FILES.journal} or {ROOT_FILES.experiment}"
        )
    if output_format == "json":
        parsed = parse_experiment_json(experiment_text) if experiment_text is not None else None
        click.echo(
            json.dumps(
                {
                    "ref": ref,
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
