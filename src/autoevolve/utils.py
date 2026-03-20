from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, TypeGuard

from autoevolve.constants import HARNESS_PATHS, ROOT_FILES
from autoevolve.errors import AutoevolveError
from autoevolve.models import ExperimentDocument, ExperimentReference, MetricValue


def resolve_repo_path(repo_root: str, relative_path: str) -> str:
    return os.path.join(repo_root, relative_path)


def file_exists(repo_root: str, relative_path: str) -> bool:
    return os.path.exists(resolve_repo_path(repo_root, relative_path))


def read_text_file(repo_root: str, relative_path: str) -> str:
    with open(resolve_repo_path(repo_root, relative_path), encoding="utf-8") as handle:
        return handle.read()


def write_text_file(path: str, contents: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)


def is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def short_sha(sha: str) -> str:
    return sha[:7]


def extract_excerpt(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _is_metric_value(value: object) -> bool:
    return value is None or isinstance(value, bool) or is_number(value) or isinstance(value, str)


def _parse_metrics(value: object) -> dict[str, MetricValue]:
    if not isinstance(value, dict):
        raise AutoevolveError('EXPERIMENT.json field "metrics" must be an object when present')

    metrics: dict[str, MetricValue] = {}
    for key, metric_value in value.items():
        if not isinstance(key, str):
            raise AutoevolveError('EXPERIMENT.json field "metrics" keys must be strings')
        if not _is_metric_value(metric_value):
            raise AutoevolveError(
                f'EXPERIMENT.json field "metrics.{key}" must be a string, number, boolean, or null'
            )
        metrics[key] = metric_value
    return metrics


def _parse_references(value: object) -> list[ExperimentReference]:
    if not isinstance(value, list):
        raise AutoevolveError('EXPERIMENT.json field "references" must be an array when present')

    references: list[ExperimentReference] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise AutoevolveError(f'EXPERIMENT.json field "references[{index}]" must be an object')
        commit = entry.get("commit")
        why = entry.get("why")
        if not isinstance(commit, str) or not commit.strip():
            raise AutoevolveError(
                f'EXPERIMENT.json field "references[{index}].commit" must be a non-empty string'
            )
        if not isinstance(why, str) or not why.strip():
            raise AutoevolveError(
                f'EXPERIMENT.json field "references[{index}].why" must be a non-empty string'
            )
        references.append(ExperimentReference(commit=commit, why=why))
    return references


def parse_experiment_json(json_text: str) -> ExperimentDocument:
    try:
        value: Any = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise AutoevolveError(str(error)) from error
    if not isinstance(value, dict):
        raise AutoevolveError("EXPERIMENT.json must contain a JSON object")

    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise AutoevolveError('EXPERIMENT.json must contain a non-empty string field "summary"')

    metrics = None
    references = None
    if "metrics" in value:
        metrics = _parse_metrics(value["metrics"])
    if "references" in value:
        references = _parse_references(value["references"])
    return ExperimentDocument(summary=summary, metrics=metrics, references=references)


def parse_iso_datetime(iso_date: str) -> datetime | None:
    if not iso_date:
        return None
    try:
        if iso_date.endswith("Z"):
            return datetime.fromisoformat(iso_date[:-1] + "+00:00")
        return datetime.fromisoformat(iso_date)
    except ValueError:
        return None


def sort_iso_datetime_value(iso_date: str) -> int:
    parsed = parse_iso_datetime(iso_date)
    if parsed is None:
        return 0
    return int(parsed.timestamp())


def format_metric_summary(metrics: dict[str, MetricValue] | None) -> str:
    if not metrics:
        return ""
    entries = list(metrics.items())
    numeric_entry = next(
        ((name, value) for name, value in entries if is_number(value)),
        None,
    )
    chosen = numeric_entry or entries[0]
    name, value = chosen
    return f"{name}={json.dumps(value)}"


def format_metric_pairs(metrics: dict[str, MetricValue] | None) -> str:
    if not metrics:
        return ""
    return ", ".join(f"{name}={json.dumps(value)}" for name, value in metrics.items())


def find_prompt_files(repo_root: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for harness, relative_path in HARNESS_PATHS.items():
        if file_exists(repo_root, relative_path):
            matches.append({"harness": harness, "relative_path": relative_path})
    return matches


def has_experiment_files(repo_root: str) -> bool:
    return file_exists(repo_root, ROOT_FILES.journal) or file_exists(
        repo_root, ROOT_FILES.experiment
    )
