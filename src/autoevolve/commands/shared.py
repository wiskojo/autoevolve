from __future__ import annotations

import json
import os
import re
from functools import cmp_to_key
from typing import Any

from autoevolve.constants import MANAGED_WORKTREE_ROOT, ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import (
    resolve_path_if_present,
    run_git,
    run_git_with_git_dir,
    try_git,
    try_git_with_git_dir,
)
from autoevolve.models import (
    ExperimentDocument,
    ExperimentRecord,
    MetricDirection,
    Objective,
    PrimaryMetricSpec,
)
from autoevolve.problem import parse_problem_primary_metric
from autoevolve.utils import (
    extract_excerpt,
    file_exists,
    format_metric_pairs,
    is_number,
    parse_experiment_json,
    read_text_file,
    short_sha,
)

MANAGED_EXPERIMENT_BRANCH_PREFIX = "autoevolve/"
JOURNAL_STUB_NOTE = "TODO: fill this in once you're done with your experiment."


def get_record_references(record: ExperimentRecord) -> list[Any]:
    if record.parsed is None or record.parsed.references is None:
        return []
    return record.parsed.references


def get_record_numeric_metric_value(record: ExperimentRecord, metric: str) -> int | float | None:
    value = record.parsed.metrics.get(metric) if record.parsed and record.parsed.metrics else None
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
    repo_root: str,
    metric_names: set[str],
    direction: MetricDirection | None,
    metric: str | None,
) -> Objective:
    if direction is not None:
        return Objective(
            direction=direction,
            metric=validate_metric_name(
                metric or "",
                metric_names,
                f"--{direction}",
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


def validate_pareto_objectives(
    objectives: list[Objective], metric_names: set[str]
) -> list[Objective]:
    return [
        Objective(
            direction=objective.direction,
            metric=validate_metric_name(
                objective.metric,
                metric_names,
                f"--{objective.direction}",
            ),
        )
        for objective in objectives
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
