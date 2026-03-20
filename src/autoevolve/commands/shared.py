from __future__ import annotations

import json
import os
import re
import shutil
from collections import deque
from datetime import datetime, timezone
from functools import cmp_to_key
from typing import Any, TypeVar, cast

import click

from autoevolve.constants import MANAGED_WORKTREE_ROOT, ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import (
    find_repo_root,
    resolve_path_if_present,
    run_git,
    run_git_with_git_dir,
    try_git,
    try_git_with_git_dir,
)
from autoevolve.models import (
    BestOptions,
    CleanOptions,
    CompareOptions,
    ExperimentDocument,
    ExperimentRecord,
    GraphDirection,
    GraphEdges,
    GraphOptions,
    ListOptions,
    MetricDirection,
    MetricValue,
    Objective,
    ObjectOutputFormat,
    ParetoOptions,
    PrimaryMetricSpec,
    RecentOptions,
    SetOutputFormat,
    ShowOptions,
    StartOptions,
    StatusOptions,
)
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.utils import (
    extract_excerpt,
    file_exists,
    format_metric_pairs,
    format_metric_summary,
    has_experiment_files,
    is_number,
    parse_experiment_json,
    read_text_file,
    resolve_repo_path,
    short_sha,
)

MANAGED_EXPERIMENT_BRANCH_PREFIX = "autoevolve/"
JOURNAL_STUB_NOTE = "TODO: fill this in once you're done with your experiment."
FormatT = TypeVar("FormatT", bound=str)


def get_record_metrics(record: ExperimentRecord) -> dict[str, MetricValue] | None:
    if record.parsed is None:
        return None
    return record.parsed.metrics


def get_record_references(record: ExperimentRecord) -> list[Any]:
    if record.parsed is None or record.parsed.references is None:
        return []
    return record.parsed.references


def get_record_metric_value(record: ExperimentRecord, metric: str) -> MetricValue:
    metrics = get_record_metrics(record)
    if metrics is None:
        return None
    return metrics.get(metric)


def get_record_numeric_metric_value(record: ExperimentRecord, metric: str) -> int | float | None:
    value = get_record_metric_value(record, metric)
    if not is_number(value):
        return None
    return value


def parse_history(repo_root: str, relative_path: str) -> list[dict[str, str]]:
    try:
        output = run_git(
            repo_root,
            ["log", "--all", "--format=%H%x09%cI%x09%s", "--", relative_path],
        )
    except AutoevolveError as error:
        if "does not have any commits yet" in str(error):
            return []
        raise

    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        sha, date, subject = (line.split("\t", 2) + ["", "", ""])[:3]
        if not sha or not date:
            raise AutoevolveError(f"Unexpected git log output: {line}")
        entries.append({"sha": sha, "date": date, "subject": subject})
    return entries


def list_autoevolve_branches(repo_root: str) -> list[dict[str, str]]:
    try:
        output = run_git(
            repo_root,
            [
                "for-each-ref",
                "refs/heads",
                "--format=%(refname:short)%09%(objectname)%09%(subject)",
            ],
        )
    except AutoevolveError as error:
        if "does not have any commits yet" in str(error):
            return []
        raise

    branches: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        name, sha, subject = (line.split("\t", 2) + ["", "", ""])[:3]
        if not name or not sha:
            raise AutoevolveError(f"Unexpected git ref output: {line}")
        branches.append({"name": name, "sha": sha, "subject": subject})
    return branches


def build_tip_map(branches: list[dict[str, str]]) -> dict[str, list[str]]:
    tip_map: dict[str, list[str]] = {}
    for branch in branches:
        tip_map.setdefault(branch["sha"], []).append(branch["name"])
    return tip_map


def try_read_file_at_ref(repo_root: str, ref: str, relative_path: str) -> str | None:
    return try_git(repo_root, ["show", f"{ref}:{relative_path}"])


def get_experiment_records(repo_root: str) -> list[ExperimentRecord]:
    tip_map = build_tip_map(list_autoevolve_branches(repo_root))
    entries = parse_history(repo_root, ROOT_FILES.experiment)
    records: list[ExperimentRecord] = []

    for entry in entries:
        journal_text = try_read_file_at_ref(repo_root, entry["sha"], ROOT_FILES.journal)
        experiment_text = try_read_file_at_ref(repo_root, entry["sha"], ROOT_FILES.experiment)
        if journal_text is None or experiment_text is None:
            continue

        parsed: ExperimentDocument | None = None
        parse_error: str | None = None
        try:
            parsed = parse_experiment_json(experiment_text)
        except AutoevolveError as error:
            parse_error = str(error)

        records.append(
            ExperimentRecord(
                sha=entry["sha"],
                date=entry["date"],
                subject=entry["subject"],
                experiment_text=experiment_text,
                journal_text=journal_text,
                parsed=parsed,
                parse_error=parse_error,
                tip_branches=tip_map.get(entry["sha"], []),
            )
        )
    return records


def get_commit_metadata(repo_root: str, ref: str) -> dict[str, str]:
    output = run_git(repo_root, ["show", "-s", "--format=%cI%x09%s", ref]).strip()
    parts = output.split("\t", 1)
    if len(parts) != 2 or not parts[0]:
        raise AutoevolveError(f"Unexpected git show output: {output}")
    return {"date": parts[0], "subject": parts[1]}


def get_head_sha(repo_root: str) -> str:
    return run_git(repo_root, ["rev-parse", "HEAD"]).strip()


def get_current_branch_label(repo_root: str) -> str:
    branch = run_git(repo_root, ["branch", "--show-current"]).strip()
    return branch or "(detached HEAD)"


def is_checkout_dirty(repo_root: str) -> bool:
    return bool(run_git(repo_root, ["status", "--porcelain"]).strip())


def build_primary_metric_problems(
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


def build_tip_status_entry(
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
        problems.extend(build_primary_metric_problems(parsed_experiment.metrics, primary_metric))
    except AutoevolveError as error:
        problems.append(f"invalid {ROOT_FILES.experiment}: {error}")

    if problems:
        return {"kind": "invalid", "problems": problems}
    return {"kind": "recorded", "problems": []}


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
                build_primary_metric_problems(
                    record.parsed.metrics if record.parsed else None, primary_metric
                )
            )
        kind = "ok" if not problems else "invalid"
        return (
            build_tip_status_entry(
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
    metadata = get_commit_metadata(repo_root, sha)
    return (
        build_tip_status_entry(
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


def compare_tip_entries_by_metric(
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
            metric_comparison = compare_tip_entries_by_metric(left, right, primary_metric)
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


def build_experiment_object_for_output(record: ExperimentRecord) -> dict[str, Any]:
    return {
        "sha": record.sha,
        "short_sha": short_sha(record.sha),
        "date": record.date,
        "subject": record.subject,
        "tips": record.tip_branches,
        "summary": record.parsed.summary if record.parsed else None,
        "metrics": record.parsed.metrics if record.parsed else None,
        "references": (
            [
                {"commit": reference.commit, "why": reference.why}
                for reference in (record.parsed.references or [])
            ]
            if record.parsed and record.parsed.references is not None
            else None
        ),
        "parse_error": record.parse_error,
        "journal_excerpt": extract_excerpt(record.journal_text),
    }


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


def collect_metric_names(records: list[ExperimentRecord]) -> set[str]:
    metric_names: set[str] = set()
    for record in records:
        if record.parsed and record.parsed.metrics:
            metric_names.update(record.parsed.metrics.keys())
    return metric_names


def format_known_metrics(metric_names: set[str]) -> str:
    return ", ".join(sorted(metric_names)) if metric_names else "none yet"


def normalize_metric_field_name(field: str) -> str:
    if field.startswith("metrics."):
        metric_name = field[len("metrics.") :]
        if not metric_name:
            raise AutoevolveError("Metric fields must use metrics.<name>.")
        return metric_name
    return field


def validate_metric_name(metric: str, metric_names: set[str], flag_name: str) -> str:
    normalized = normalize_metric_field_name(metric)
    if metric_names and normalized not in metric_names:
        raise AutoevolveError(
            f'{flag_name} unknown metric "{normalized}". Known metrics: '
            f"{format_known_metrics(metric_names)}"
        )
    return normalized


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


def parse_positive_integer(flag_name: str, raw_value: str | None) -> int:
    try:
        parsed = int(raw_value or "")
    except ValueError as error:
        raise AutoevolveError(
            f'{flag_name} expects a positive integer, received "{raw_value or ""}"'
        ) from error
    if parsed <= 0:
        raise AutoevolveError(
            f'{flag_name} expects a positive integer, received "{raw_value or ""}"'
        )
    return parsed


def parse_format(flag_name: str, raw_value: str | None, formats: tuple[FormatT, ...]) -> FormatT:
    format_value = (raw_value or "").strip()
    if format_value not in formats:
        raise AutoevolveError(f"{flag_name} expects one of {', '.join(formats)}")
    return cast(FormatT, format_value)


def parse_ref_value(flag_name: str, raw_value: str | None) -> str:
    ref = (raw_value or "").strip()
    if not ref:
        raise AutoevolveError(f"{flag_name} expects a git ref")
    return ref


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


def parse_recent_options(args: list[str]) -> RecentOptions:
    output_format: SetOutputFormat = "tsv"
    limit = 10
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("tsv", "jsonl"),
            )
            index += 2
            continue
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for recent.')
        raise AutoevolveError(f'Unexpected argument "{token}" for recent.')
    return RecentOptions(format=output_format, limit=limit)


def parse_best_options(args: list[str]) -> BestOptions:
    direction: MetricDirection | None = None
    output_format: SetOutputFormat = "tsv"
    limit = 5
    metric = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("tsv", "jsonl"),
            )
            index += 2
            continue
        if token in {"--min", "--max"}:
            metric = args[index + 1] if index + 1 < len(args) else ""
            if not metric:
                raise AutoevolveError(f"{token} expects a metric name")
            if direction is not None:
                raise AutoevolveError(
                    "best accepts exactly one objective. Use either "
                    "--max <metric> or --min <metric>."
                )
            direction = cast(MetricDirection, token[2:])
            index += 2
            continue
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for best.')
        raise AutoevolveError(f'Unexpected argument "{token}" for best.')
    return BestOptions(direction=direction, format=output_format, limit=limit, metric=metric)


def parse_pareto_options(args: list[str]) -> ParetoOptions:
    output_format: SetOutputFormat = "tsv"
    limit: int | None = None
    objectives: list[Objective] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"--max", "--min"}:
            metric = args[index + 1] if index + 1 < len(args) else ""
            if not metric:
                raise AutoevolveError(f"{token} expects a metric name")
            objectives.append(Objective(direction=cast(MetricDirection, token[2:]), metric=metric))
            index += 2
            continue
        if token == "--format":
            output_format = parse_format(
                "--format",
                args[index + 1] if index + 1 < len(args) else None,
                ("tsv", "jsonl"),
            )
            index += 2
            continue
        if token == "--limit":
            limit = parse_positive_integer(
                "--limit", args[index + 1] if index + 1 < len(args) else None
            )
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for pareto.')
        raise AutoevolveError(f'Unexpected argument "{token}" for pareto.')
    if not objectives:
        raise AutoevolveError(
            "pareto requires at least one objective, for example: "
            "--max primary_metric --min runtime_sec"
        )
    return ParetoOptions(format=output_format, limit=limit, objectives=objectives)


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


def parse_start_options(args: list[str]) -> StartOptions:
    from_ref = ""
    name = ""
    summary = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--from":
            from_ref = parse_ref_value("--from", args[index + 1] if index + 1 < len(args) else None)
            index += 2
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for start.')
        if not name:
            name = token.strip()
            index += 1
            continue
        if not summary:
            summary = token.strip()
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for start.')

    if not name:
        raise AutoevolveError(
            "start requires an experiment name and summary, for example: "
            'autoevolve start tune-thresholds "Try a tighter threshold '
            'sweep"'
        )
    if not summary:
        raise AutoevolveError(
            "start requires an experiment summary, for example: "
            'autoevolve start tune-thresholds "Try a tighter threshold '
            'sweep"'
        )
    return StartOptions(from_ref=from_ref, name=name, summary=summary)


def parse_record_args(args: list[str]) -> None:
    if args:
        raise AutoevolveError(f'Unexpected argument "{args[0]}" for record.')


def parse_clean_options(args: list[str]) -> CleanOptions:
    force = False
    name = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-f", "--force"}:
            force = True
            index += 1
            continue
        if token.startswith("-"):
            raise AutoevolveError(f'Unknown option "{token}" for clean.')
        if not name:
            name = token.strip()
            index += 1
            continue
        raise AutoevolveError(f'Unexpected argument "{token}" for clean.')
    return CleanOptions(force=force, name=name)


def normalize_managed_experiment_name(name: str) -> str:
    trimmed = name.strip()
    if trimmed.startswith(MANAGED_EXPERIMENT_BRANCH_PREFIX):
        return trimmed[len(MANAGED_EXPERIMENT_BRANCH_PREFIX) :]
    return trimmed


def is_managed_worktree_path(worktree_path: str) -> bool:
    root = resolve_path_if_present(MANAGED_WORKTREE_ROOT)
    resolved_worktree_path = resolve_path_if_present(worktree_path)
    return resolved_worktree_path.startswith(f"{root}{os.sep}")


def apply_limit(records: list[Any], limit: int | None) -> list[Any]:
    if not limit:
        return records
    return records[:limit]


def resolve_best_objective(
    repo_root: str, options: BestOptions, metric_names: set[str]
) -> Objective:
    if options.direction is not None:
        return Objective(
            direction=options.direction,
            metric=validate_metric_name(
                options.metric,
                metric_names,
                f"--{options.direction}",
            ),
        )

    if not file_exists(repo_root, ROOT_FILES.problem):
        raise AutoevolveError(
            "best requires an explicit objective, or a valid PROBLEM.md primary metric."
        )

    try:
        primary_metric = parse_problem_primary_metric(read_text_file(repo_root, ROOT_FILES.problem))
    except Exception as error:
        raise AutoevolveError(
            "best requires an explicit objective, or a valid PROBLEM.md primary metric."
        ) from error

    return Objective(
        direction=primary_metric.direction,
        metric=validate_metric_name(
            primary_metric.metric,
            metric_names,
            f"--{primary_metric.direction}",
        ),
    )


def validate_pareto_objectives(options: ParetoOptions, metric_names: set[str]) -> list[Objective]:
    return [
        Objective(
            direction=objective.direction,
            metric=validate_metric_name(
                objective.metric,
                metric_names,
                f"--{objective.direction}",
            ),
        )
        for objective in options.objectives
    ]


def resolve_ref(repo_root: str, ref: str) -> str:
    return run_git(repo_root, ["rev-parse", "--verify", ref]).strip()


def resolve_git_path(repo_root: str, rev_parse_flag: str) -> str:
    return os.path.abspath(
        os.path.join(repo_root, run_git(repo_root, ["rev-parse", rev_parse_flag]).strip())
    )


def is_managed_experiment_branch(branch_name: str) -> bool:
    return branch_name.startswith(MANAGED_EXPERIMENT_BRANCH_PREFIX)


def get_managed_experiment_name(branch_name: str) -> str:
    return branch_name[len(MANAGED_EXPERIMENT_BRANCH_PREFIX) :]


def build_journal_stub(name: str) -> str:
    return f"# {name}\n\n{JOURNAL_STUB_NOTE}\n"


def build_experiment_stub(summary: str) -> str:
    return f"{json.dumps({'summary': summary, 'metrics': {}, 'references': []}, indent=2)}\n"


def validate_managed_branch_name(repo_root: str, branch_name: str) -> None:
    try:
        run_git(repo_root, ["check-ref-format", f"refs/heads/{branch_name}"])
    except AutoevolveError as error:
        raise AutoevolveError(
            f'"{branch_name}" is not a valid managed experiment branch name.'
        ) from error


def resolve_managed_worktree_path(experiment_name: str) -> str:
    root = os.path.abspath(MANAGED_WORKTREE_ROOT)
    worktree_path = os.path.abspath(os.path.join(root, experiment_name))
    if worktree_path == root or not worktree_path.startswith(f"{root}{os.sep}"):
        raise AutoevolveError(f'"{experiment_name}" is not a valid experiment name.')
    return worktree_path


def delete_managed_experiment_branch_if_present(
    common_git_dir: str, branch_name: str | None
) -> None:
    if not branch_name or not is_managed_experiment_branch(branch_name):
        return
    exists = try_git_with_git_dir(
        os.path.expanduser("~"),
        common_git_dir,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
    )
    if exists is None:
        return
    run_git_with_git_dir(os.path.expanduser("~"), common_git_dir, ["branch", "-D", branch_name])


def parse_worktree_branch(raw_branch: str) -> str:
    prefix = "refs/heads/"
    return raw_branch[len(prefix) :] if raw_branch.startswith(prefix) else raw_branch


def list_repo_worktree_entries(repo_root: str) -> list[dict[str, Any]]:
    output = run_git(repo_root, ["worktree", "list", "--porcelain"]).strip()
    if not output:
        return []

    current_worktree_path = resolve_path_if_present(repo_root)
    primary_worktree_path = resolve_path_if_present(
        os.path.dirname(resolve_git_path(repo_root, "--git-common-dir"))
    )

    entries: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\r?\n", output):
        if not block:
            continue
        lines = [line for line in block.splitlines() if line]
        worktree_line = next((line for line in lines if line.startswith("worktree ")), None)
        head_line = next((line for line in lines if line.startswith("HEAD ")), None)
        branch_line = next((line for line in lines if line.startswith("branch ")), None)
        if worktree_line is None or head_line is None:
            raise AutoevolveError(f"Unexpected git worktree output: {block}")
        worktree_path = worktree_line[len("worktree ") :]
        resolved_worktree_path = resolve_path_if_present(worktree_path)
        branch = parse_worktree_branch(branch_line[len("branch ") :]) if branch_line else None
        head = head_line[len("HEAD ") :]
        entries.append(
            {
                "branch": branch,
                "isCurrent": resolved_worktree_path == current_worktree_path,
                "isPrimary": resolved_worktree_path == primary_worktree_path,
                "path": resolved_worktree_path,
                "head": head,
                "shortHead": short_sha(head),
            }
        )
    return entries


def is_missing_worktree_error(error: Exception) -> bool:
    message = str(error)
    return "not a git repository" in message or "cannot change to" in message


def inspect_repo_worktree_state(worktree_path: str) -> dict[str, Any]:
    if not os.path.exists(worktree_path):
        return {"dirty": None, "isMissing": True}
    try:
        return {"dirty": is_checkout_dirty(worktree_path), "isMissing": False}
    except AutoevolveError as error:
        if is_missing_worktree_error(error):
            return {"dirty": None, "isMissing": True}
        raise


def inspect_repo_worktree(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        **entry,
        **inspect_repo_worktree_state(entry["path"]),
        "isManagedExperiment": bool(
            entry["branch"] and is_managed_experiment_branch(entry["branch"])
        ),
    }


def list_repo_worktrees(repo_root: str) -> list[dict[str, Any]]:
    return [inspect_repo_worktree(entry) for entry in list_repo_worktree_entries(repo_root)]


def find_repo_worktree_by_path(repo_root: str, target_path: str) -> dict[str, Any] | None:
    target_resolved_path = resolve_path_if_present(target_path)
    for candidate in list_repo_worktree_entries(repo_root):
        if candidate["path"] == target_resolved_path:
            return inspect_repo_worktree(candidate)
    return None


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
    name = (
        get_managed_experiment_name(worktree["branch"])
        if worktree["branch"] and is_managed_experiment_branch(worktree["branch"])
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


def _parse_iso_date(iso_date: str) -> datetime | None:
    if not iso_date:
        return None
    try:
        if iso_date.endswith("Z"):
            return datetime.fromisoformat(iso_date[:-1] + "+00:00")
        return datetime.fromisoformat(iso_date)
    except ValueError:
        return None


def format_relative_time(iso_date: str) -> str:
    target = _parse_iso_date(iso_date)
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
    direction = primary_metric.direction
    metric_name = primary_metric.metric

    def sort_key(record: ExperimentRecord) -> tuple[int | float, str, str]:
        metric_value = get_record_numeric_metric_value(record, metric_name)
        if metric_value is None:
            raise AutoevolveError(f'Metric "{metric_name}" must be numeric for ranking.')
        ranked_value = metric_value if direction == "min" else -metric_value
        return (ranked_value, record.date, record.sha)

    return sorted(
        candidates,
        key=sort_key,
    )[0]


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
    newest_date = _parse_iso_date(newest.date)
    oldest_date = _parse_iso_date(oldest.date)
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


def describe_worktree_for_removal(worktree: dict[str, Any]) -> str:
    state = "missing" if worktree["isMissing"] else "dirty" if worktree["dirty"] else "clean"
    return (
        f"{worktree['path']} ({worktree['branch'] or '(detached HEAD)'}, "
        f"{state}, {worktree['shortHead']})"
    )


def resolve_new_experiment_base_ref(repo_root: str, explicit_base_ref: str) -> dict[str, str]:
    current_branch = run_git(repo_root, ["branch", "--show-current"]).strip()
    ref = explicit_base_ref or current_branch or "HEAD"
    return {"ref": ref, "sha": resolve_ref(repo_root, ref)}


def get_parents(repo_root: str, ref: str) -> list[str]:
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
        parents = get_parents(repo_root, current)
        current = parents[0] if parents else None
    return None


def build_git_parent_map(repo_root: str, records: list[ExperimentRecord]) -> dict[str, list[str]]:
    experiment_shas = {record.sha for record in records}
    parent_map: dict[str, list[str]] = {}
    for record in records:
        parents = get_parents(repo_root, record.sha)
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


def compare_metric_records(
    left: ExperimentRecord, right: ExperimentRecord, metric: str, direction: str
) -> int:
    left_value = left.parsed.metrics.get(metric) if left.parsed and left.parsed.metrics else None
    right_value = (
        right.parsed.metrics.get(metric) if right.parsed and right.parsed.metrics else None
    )
    if not is_number(left_value) or not is_number(right_value):
        raise AutoevolveError(f'Metric "{metric}" must be numeric for ranking.')
    if left_value == right_value:
        if right.date > left.date:
            return 1
        if right.date < left.date:
            return -1
        return 0
    if direction == "min":
        return -1 if left_value < right_value else 1
    return -1 if left_value > right_value else 1


def get_merge_base(repo_root: str, left_sha: str, right_sha: str) -> str | None:
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
            "mergeBase": get_merge_base(repo_root, left_sha, right_sha),
            "relationship": "sibling",
            "sharedParents": shared_parents,
        }

    merge_base = get_merge_base(repo_root, left_sha, right_sha)
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
    left_commits = {reference.commit for reference in get_record_references(left)}
    right_commits = {reference.commit for reference in get_record_references(right)}
    return {
        "common": sorted(left_commits & right_commits),
        "leftOnly": sorted(left_commits - right_commits),
        "rightOnly": sorted(right_commits - left_commits),
    }


def format_metric_value(value: Any) -> str:
    return "null" if value is None else json.dumps(value)


def dominates(
    candidate: ExperimentRecord,
    challenger: ExperimentRecord,
    objectives: list[Objective],
) -> bool:
    strictly_better = False
    for objective in objectives:
        candidate_value = get_record_numeric_metric_value(candidate, objective.metric)
        challenger_value = get_record_numeric_metric_value(challenger, objective.metric)
        if not is_number(candidate_value) or not is_number(challenger_value):
            return False
        if objective.direction == "max":
            if candidate_value < challenger_value:
                return False
            if candidate_value > challenger_value:
                strictly_better = True
        else:
            if candidate_value > challenger_value:
                return False
            if candidate_value < challenger_value:
                strictly_better = True
    return strictly_better


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


def print_set_record(record: ExperimentRecord, output_format: str) -> None:
    if output_format == "jsonl":
        click.echo(json.dumps(build_experiment_object_for_output(record)))
        return
    click.echo(format_experiment_tsv_row(record))


def sanitize_tsv_field(value: str) -> str:
    return re.sub(r"\r?\n+", " ", value.replace("\t", " ")).strip()


def format_experiment_tsv_row(record: ExperimentRecord) -> str:
    fields = [
        short_sha(record.sha),
        record.date,
        sanitize_tsv_field(record.subject),
        sanitize_tsv_field(",".join(record.tip_branches)),
        sanitize_tsv_field(
            format_metric_pairs(record.parsed.metrics if record.parsed else None) or ""
        ),
        sanitize_tsv_field(record.parsed.summary if record.parsed else record.parse_error or ""),
    ]
    return "\t".join(fields)


def print_set_header(output_format: str) -> None:
    if output_format == "tsv":
        click.echo("sha\tdate\tsubject\ttips\tmetrics\tsummary")


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


def _primary_metric_object(payload: dict[str, Any] | None) -> PrimaryMetricSpec | None:
    if payload is None:
        return None
    return PrimaryMetricSpec(
        direction=payload["direction"], metric=payload["metric"], raw=payload["raw"]
    )


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


def run_recent(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    options = parse_recent_options(args)
    records = apply_limit(
        sorted(
            get_experiment_records(repo_root),
            key=lambda record: record.date,
            reverse=True,
        ),
        options.limit,
    )
    if not records:
        if options.format != "jsonl":
            click.echo("No experiments found.")
        return
    print_set_header(options.format)
    for record in records:
        print_set_record(record, options.format)


def run_best(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    all_records = get_experiment_records(repo_root)
    options = parse_best_options(args)
    objective = resolve_best_objective(repo_root, options, collect_metric_names(all_records))
    records = [
        record
        for record in all_records
        if record.parsed
        and record.parsed.metrics
        and is_number(get_record_metric_value(record, objective.metric))
    ]

    def best_key(record: ExperimentRecord) -> tuple[int | float, int]:
        metric_value = get_record_numeric_metric_value(record, objective.metric)
        if metric_value is None:
            raise AutoevolveError(f'Metric "{objective.metric}" must be numeric for ranking.')
        ranked_value = metric_value if objective.direction == "min" else -metric_value
        return (ranked_value, -_sort_date_value(record.date))

    records = sorted(records, key=best_key)[: options.limit]
    if not records:
        if options.format != "jsonl":
            click.echo(f'No experiments found with a numeric "{objective.metric}" metric.')
        return
    print_set_header(options.format)
    for record in records:
        print_set_record(record, options.format)


def _sort_date_value(date: str) -> int:
    parsed = _parse_iso_date(date)
    return int(parsed.timestamp()) if parsed else 0


def run_pareto(args: list[str]) -> None:
    repo_root = find_repo_root(os.getcwd())
    all_records = get_experiment_records(repo_root)
    options = parse_pareto_options(args)
    objectives = validate_pareto_objectives(options, collect_metric_names(all_records))
    candidates = [
        record
        for record in all_records
        if all(
            record.parsed
            and record.parsed.metrics
            and is_number(get_record_metric_value(record, objective.metric))
            for objective in objectives
        )
    ]
    if not candidates:
        if options.format != "jsonl":
            click.echo(
                "No experiments found with numeric metrics for the requested Pareto objectives."
            )
        return
    frontier = [
        candidate
        for candidate in candidates
        if not any(
            other.sha != candidate.sha and dominates(other, candidate, objectives)
            for other in candidates
        )
    ]

    def pareto_key(record: ExperimentRecord) -> tuple[Any, ...]:
        values: list[Any] = []
        for objective in objectives:
            metric_value = get_record_numeric_metric_value(record, objective.metric)
            if metric_value is None:
                raise AutoevolveError(f'Metric "{objective.metric}" must be numeric for ranking.')
            values.append(metric_value if objective.direction == "min" else -metric_value)
        values.append(-_sort_date_value(record.date))
        return tuple(values)

    records = apply_limit(sorted(frontier, key=pareto_key), options.limit)
    print_set_header(options.format)
    for record in records:
        print_set_record(record, options.format)


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


def run_start(args: list[str]) -> None:
    options = parse_start_options(args)
    repo_root = find_repo_root(os.getcwd())
    base_ref = resolve_new_experiment_base_ref(repo_root, options.from_ref)
    branch_name = f"{MANAGED_EXPERIMENT_BRANCH_PREFIX}{options.name}"
    worktree_path = resolve_managed_worktree_path(options.name)
    validate_managed_branch_name(repo_root, branch_name)
    if any(branch["name"] == branch_name for branch in list_autoevolve_branches(repo_root)):
        raise AutoevolveError(f'Branch "{branch_name}" already exists.')
    if os.path.exists(worktree_path):
        raise AutoevolveError(f"Worktree path already exists: {worktree_path}")
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    run_git(
        repo_root,
        ["worktree", "add", "-b", branch_name, worktree_path, base_ref["sha"]],
    )
    with open(
        resolve_repo_path(worktree_path, ROOT_FILES.journal), "w", encoding="utf-8"
    ) as handle:
        handle.write(build_journal_stub(options.name))
    with open(
        resolve_repo_path(worktree_path, ROOT_FILES.experiment), "w", encoding="utf-8"
    ) as handle:
        handle.write(build_experiment_stub(options.summary))
    click.echo(f"Branch: {branch_name}")
    click.echo(f"Base: {base_ref['ref']}")
    click.echo(f"Path: {worktree_path}")


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


def run_record(args: list[str]) -> None:
    parse_record_args(args)
    repo_root = find_repo_root(os.getcwd())
    branch_name = run_git(repo_root, ["branch", "--show-current"]).strip()
    if not branch_name:
        raise AutoevolveError("record requires an attached branch.")
    if not is_managed_experiment_branch(branch_name):
        raise AutoevolveError(
            "record only works on managed autoevolve experiment branches "
            f"({MANAGED_EXPERIMENT_BRANCH_PREFIX}<name>)."
        )
    managed_root = os.path.realpath(os.path.abspath(MANAGED_WORKTREE_ROOT))
    resolved_repo_root = os.path.realpath(os.path.abspath(repo_root))
    if resolved_repo_root != managed_root and not resolved_repo_root.startswith(
        f"{managed_root}{os.sep}"
    ):
        raise AutoevolveError(
            f"record must be run from a managed autoevolve worktree under {managed_root}."
        )
    git_dir = resolve_git_path(repo_root, "--git-dir")
    common_git_dir = resolve_git_path(repo_root, "--git-common-dir")
    if git_dir == common_git_dir:
        raise AutoevolveError("record refuses to remove the primary worktree.")
    journal_text = read_text_file(repo_root, ROOT_FILES.journal).strip()
    experiment_text = read_text_file(repo_root, ROOT_FILES.experiment)
    parsed_experiment = parse_experiment_json(experiment_text)
    experiment_name = get_managed_experiment_name(branch_name)
    if journal_text == build_journal_stub(experiment_name).strip():
        raise AutoevolveError(f"Replace the {ROOT_FILES.journal} stub before committing.")
    if not run_git(repo_root, ["status", "--porcelain"]).strip():
        raise AutoevolveError("No changes to commit.")
    commit_message = next(
        (line.strip() for line in parsed_experiment.summary.splitlines() if line.strip()),
        "",
    )
    if not commit_message:
        raise AutoevolveError(f"{ROOT_FILES.experiment} summary must not be empty.")
    run_git(repo_root, ["add", "."])
    run_git(repo_root, ["commit", "-m", commit_message])
    commit_sha = run_git(repo_root, ["rev-parse", "HEAD"]).strip()
    run_git_with_git_dir(
        os.path.expanduser("~"),
        common_git_dir,
        ["worktree", "remove", resolved_repo_root],
    )
    click.echo(f"Committed {branch_name} at {short_sha(commit_sha)}.")
    click.echo(f"Removed worktree: {resolved_repo_root}")


def run_clean(args: list[str]) -> None:
    options = parse_clean_options(args)
    repo_root = find_repo_root(os.getcwd())
    target_worktrees: list[dict[str, Any]] = []
    target_experiment_name = ""
    if options.name:
        target_experiment_name = normalize_managed_experiment_name(options.name)
        target_worktree = find_repo_worktree_by_path(
            repo_root, resolve_managed_worktree_path(target_experiment_name)
        )
        if (
            target_worktree is None
            or target_worktree["isPrimary"]
            or not is_managed_worktree_path(target_worktree["path"])
        ):
            raise AutoevolveError(
                "No managed experiment worktree named "
                f'"{target_experiment_name}" found for this repository.'
            )
        target_worktrees = [target_worktree]
    else:
        target_worktrees = [
            worktree
            for worktree in list_repo_worktrees(repo_root)
            if not worktree["isPrimary"] and is_managed_worktree_path(worktree["path"])
        ]
    if not target_worktrees:
        click.echo("No managed worktrees to clean.")
        return
    blocked_worktrees = [
        worktree for worktree in target_worktrees if worktree["isMissing"] or worktree["dirty"]
    ]
    if not options.force and blocked_worktrees:
        reason = (
            "Refusing to remove a dirty or missing linked worktree without --force:"
            if len(blocked_worktrees) == 1
            else "Refusing to remove dirty or missing linked worktrees without --force:"
        )
        raise AutoevolveError(
            reason
            + "\n"
            + "\n".join(
                f"  {describe_worktree_for_removal(worktree)}" for worktree in blocked_worktrees
            )
        )
    common_git_dir = resolve_git_path(repo_root, "--git-common-dir")
    target_branches = [worktree["branch"] for worktree in target_worktrees]
    pruned_missing_worktrees = False
    for worktree in target_worktrees:
        if worktree["isMissing"]:
            if os.path.exists(worktree["path"]):
                shutil.rmtree(worktree["path"], ignore_errors=True)
            if not pruned_missing_worktrees:
                run_git_with_git_dir(
                    os.path.expanduser("~"),
                    common_git_dir,
                    ["worktree", "prune", "--expire", "now"],
                )
                pruned_missing_worktrees = True
            continue
        remove_args = ["worktree", "remove"]
        if options.force or worktree["dirty"]:
            remove_args.append("--force")
        remove_args.append(worktree["path"])
        run_git_with_git_dir(os.path.expanduser("~"), common_git_dir, remove_args)
    for branch_name in target_branches:
        delete_managed_experiment_branch_if_present(common_git_dir, branch_name)
    click.echo(
        "Removed "
        f"{len(target_worktrees)} linked worktree"
        f"{'s' if len(target_worktrees) != 1 else ''} for this repository."
    )
    if target_experiment_name:
        click.echo(f"Experiment: {target_experiment_name}")
    for worktree in target_worktrees:
        click.echo(f"  {describe_worktree_for_removal(worktree)}")


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


def run_validate() -> None:
    repo_root = find_repo_root(os.getcwd())
    problems: list[str] = []
    primary_metric = None
    if not file_exists(repo_root, ROOT_FILES.problem):
        problems.append(f"Missing {ROOT_FILES.problem}. Run autoevolve init first.")
    else:
        try:
            primary_metric = parse_problem_primary_metric(
                read_text_file(repo_root, ROOT_FILES.problem)
            )
        except Exception as error:
            problems.append(str(error))
    from autoevolve.utils import find_prompt_files

    if not find_prompt_files(repo_root):
        problems.append(
            f"Missing prompt file. Expected {ROOT_FILES.autoevolve} or a "
            "supported harness skill file."
        )

    if has_experiment_files(repo_root):
        has_journal = file_exists(repo_root, ROOT_FILES.journal)
        has_experiment = file_exists(repo_root, ROOT_FILES.experiment)
        if not has_journal or not has_experiment:
            problems.append(
                "Current checkout must contain both "
                f"{ROOT_FILES.journal} and {ROOT_FILES.experiment} when "
                "one is present."
            )
        if has_journal:
            journal_text = read_text_file(repo_root, ROOT_FILES.journal).strip()
            if not journal_text:
                problems.append(f"{ROOT_FILES.journal} must not be empty.")
        if has_experiment:
            try:
                parsed_experiment = parse_experiment_json(
                    read_text_file(repo_root, ROOT_FILES.experiment)
                )
                if primary_metric:
                    experiment_metrics = parsed_experiment.metrics
                    has_required_metric = (
                        experiment_metrics is not None
                        and primary_metric.metric in experiment_metrics
                    )
                    if not has_required_metric:
                        problems.append(
                            f"{ROOT_FILES.experiment} must record the "
                            f'primary metric "{primary_metric.metric}" '
                            f"declared in {ROOT_FILES.problem} "
                            f"({primary_metric.raw}). You can record "
                            "additional metrics too, but this one is "
                            "required."
                        )
            except AutoevolveError as error:
                problems.append(str(error))
    if problems:
        for problem in problems:
            click.echo(f"FAIL: {problem}")
        raise SystemExit(1)
    click.echo("OK: repository matches the autoevolve protocol.")
    if not has_experiment_files(repo_root):
        click.echo(
            "No current experiment record found. Add "
            f"{ROOT_FILES.journal} and {ROOT_FILES.experiment} in the "
            "first experiment commit."
        )
