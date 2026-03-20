from __future__ import annotations

import os
import re
from typing import Any

from autoevolve.constants import MANAGED_EXPERIMENT_BRANCH_PREFIX, ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.gittools import (
    is_checkout_dirty,
    resolve_git_path,
    resolve_path_if_present,
    run_git,
    try_git,
)
from autoevolve.models import ExperimentDocument, ExperimentRecord
from autoevolve.utils import extract_excerpt, is_number, parse_experiment_json, short_sha


def get_record_numeric_metric_value(record: ExperimentRecord, metric: str) -> int | float | None:
    value = record.parsed.metrics.get(metric) if record.parsed and record.parsed.metrics else None
    if not is_number(value):
        return None
    return value


def _parse_history(repo_root: str, relative_path: str) -> list[dict[str, str]]:
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
    entries = _parse_history(repo_root, ROOT_FILES.experiment)
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


def apply_limit(records: list[Any], limit: int | None) -> list[Any]:
    if not limit:
        return records
    return records[:limit]


def is_managed_experiment_branch(branch_name: str) -> bool:
    return branch_name.startswith(MANAGED_EXPERIMENT_BRANCH_PREFIX)


def get_managed_experiment_name(branch_name: str) -> str:
    return branch_name[len(MANAGED_EXPERIMENT_BRANCH_PREFIX) :]


def _parse_worktree_branch(raw_branch: str) -> str:
    prefix = "refs/heads/"
    return raw_branch[len(prefix) :] if raw_branch.startswith(prefix) else raw_branch


def _list_repo_worktree_entries(repo_root: str) -> list[dict[str, Any]]:
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
        branch = _parse_worktree_branch(branch_line[len("branch ") :]) if branch_line else None
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


def _is_missing_worktree_error(error: Exception) -> bool:
    message = str(error)
    return "not a git repository" in message or "cannot change to" in message


def _inspect_repo_worktree_state(worktree_path: str) -> dict[str, Any]:
    if not os.path.exists(worktree_path):
        return {"dirty": None, "isMissing": True}
    try:
        return {"dirty": is_checkout_dirty(worktree_path), "isMissing": False}
    except AutoevolveError as error:
        if _is_missing_worktree_error(error):
            return {"dirty": None, "isMissing": True}
        raise


def _inspect_repo_worktree(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        **entry,
        **_inspect_repo_worktree_state(entry["path"]),
        "isManagedExperiment": bool(
            entry["branch"] and is_managed_experiment_branch(entry["branch"])
        ),
    }


def list_repo_worktrees(repo_root: str) -> list[dict[str, Any]]:
    return [_inspect_repo_worktree(entry) for entry in _list_repo_worktree_entries(repo_root)]
