import json
from datetime import datetime, timezone
from typing import Annotated

import typer

from autoevolve.git import diff
from autoevolve.models.experiment import (
    ExperimentDocument,
    ExperimentRecord,
    ExperimentWorktree,
    Objective,
)
from autoevolve.models.git import GitChangedPath
from autoevolve.models.lineage import LineageEdge
from autoevolve.models.types import GraphDirection, GraphEdges, MetricValue
from autoevolve.repository import (
    EXPERIMENT_FILE,
    JOURNAL_FILE,
    ExperimentRepository,
)

app = typer.Typer()


@app.command(
    "status",
    rich_help_panel="Inspect",
    short_help="Show the current experiment status.",
    help=(
        "Show the current experiment status.\n\n"
        "status shows the checkout state, recent results, and managed worktrees "
        "for the current repository."
    ),
)
def status() -> None:
    repository = ExperimentRepository()
    records = repository.records()
    worktrees = repository.active_worktrees()
    managed = [
        worktree for worktree in worktrees if worktree.is_managed and not worktree.is_primary
    ]
    unmanaged = [
        worktree for worktree in worktrees if not worktree.is_managed and not worktree.is_primary
    ]
    lines = []
    lines.extend(_section("project", _project_lines(repository, records, managed)))
    lines.extend(_section("latest experiments", _recent_experiment_lines(repository)))
    lines.extend(
        _section(
            "ongoing experiments (managed worktrees)",
            _managed_worktree_lines(managed),
        )
    )
    if unmanaged:
        lines.extend(_section("other linked worktrees", _other_worktree_lines(unmanaged)))
    typer.echo("\n".join(lines).rstrip())


@app.command(
    "log",
    rich_help_panel="Inspect",
    short_help="Show experiment logs.",
    help=(
        "Show experiment logs.\n\n"
        "log shows the most recent recorded experiments with full metrics and "
        "JOURNAL.md content."
    ),
)
def log(
    limit: Annotated[int, typer.Option(min=1, help="Number of experiments to show.")] = 10,
) -> None:
    records = ExperimentRepository().recent_records(limit)
    if not records:
        typer.echo("No experiments found.")
        return
    typer.echo("\n\n".join(_render_log_record(record) for record in records))


@app.command(
    "show",
    rich_help_panel="Inspect",
    short_help="Show experiment details.",
    help=(
        "Show experiment details.\n\n"
        "show prints the experiment, journal, and the git diff from "
        "the previous experiment, or from the first parent commit when there "
        "is no earlier experiment ancestor."
    ),
)
def show(ref: str) -> None:
    repository = ExperimentRepository()
    record = repository.resolve_record(ref)
    previous = repository.previous_record(record)
    parents = repository.repo.commit(record.sha).parents
    base = previous.sha if previous is not None else (parents[0].hexsha if parents else None)
    patch = (
        diff(
            repository.repo,
            base,
            record.sha,
            exclude=(EXPERIMENT_FILE, JOURNAL_FILE),
        ).patch
        if base is not None
        else ""
    )
    lines = []
    lines.extend(_section("experiment", _experiment_lines(record.document)))
    lines.extend(_section("journal", record.journal.splitlines()))
    lines.extend(_section("code diff", patch.splitlines() if patch else []))
    typer.echo("\n".join(lines).rstrip())


@app.command(
    "compare",
    rich_help_panel="Inspect",
    short_help="Compare two experiments.",
    help=(
        "Compare two experiments.\n\n"
        "compare shows commit metadata, summaries, metrics, references, and "
        "the git diff between two recorded experiments."
    ),
)
def compare(left_ref: str, right_ref: str) -> None:
    repository = ExperimentRepository()
    left = repository.resolve_record(left_ref)
    right = repository.resolve_record(right_ref)
    comparison = diff(
        repository.repo,
        left.sha,
        right.sha,
        exclude=(EXPERIMENT_FILE, JOURNAL_FILE),
    )
    lines = [
        f"left:  {_record_header(left)}",
        f"right: {_record_header(right)}",
        f"git:   {repository.git_relationship(left, right)}",
        f"diff:  {comparison.shortstat or '(none)'}",
        "",
    ]
    lines.extend(_section("changed paths", _changed_path_lines(comparison.changed_paths)))
    lines.extend(_section("metrics", _metric_delta_lines(left, right)))
    lines.extend(_section("references", _reference_diff_lines(left, right)))
    lines.extend(
        _section(
            "summaries",
            [
                f"left: {left.document.summary}",
                f"right: {right.document.summary}",
            ],
        )
    )
    lines.extend(_section("code diff", comparison.patch.splitlines() if comparison.patch else []))
    typer.echo("\n".join(lines).rstrip())


@app.command(
    "lineage",
    rich_help_panel="Inspect",
    short_help="Show experiment lineage around one ref.",
    help=(
        "Show experiment lineage around one ref.\n\n"
        "lineage traverses git ancestry and recorded references around one "
        "experiment."
    ),
)
def lineage(
    ref: str,
    edges: Annotated[
        GraphEdges,
        typer.Option(help="Edge types to include."),
    ] = GraphEdges.ALL,
    direction: Annotated[
        GraphDirection,
        typer.Option(help="Traversal direction."),
    ] = GraphDirection.BACKWARD,
    depth: Annotated[
        str,
        typer.Option(help="Traversal depth. Use a positive integer or 'all'."),
    ] = "3",
) -> None:
    repository = ExperimentRepository()
    parsed_depth = _parse_depth(depth)
    graph = repository.lineage(ref, edges=edges, direction=direction, depth=parsed_depth)
    order = {sha: index for index, sha in enumerate(graph.node_order)}
    lines = [
        f"root: {graph.root.sha[:7]}  {graph.root.document.summary}",
        f"mode: edges={edges.value} direction={direction.value} depth={depth}",
        "",
    ]
    lines.extend(
        _section(
            "nodes",
            [
                f"{sha[:7]}  {record.document.summary}"
                for sha in graph.node_order
                if (record := repository.record_by_sha(sha)) is not None
            ],
        )
    )
    lines.extend(
        _section(
            "edges",
            [
                _render_lineage_edge(edge)
                for edge in sorted(
                    graph.edges,
                    key=lambda edge: (
                        order.get(edge.source, 0),
                        0 if edge.kind == "git" else 1,
                        order.get(edge.target, 0),
                    ),
                )
            ],
        )
    )
    typer.echo("\n".join(lines).rstrip())


def _project_lines(
    repository: ExperimentRepository,
    records: list[ExperimentRecord],
    managed: list[ExperimentWorktree],
) -> list[str]:
    lines = [f"experiments: {len(records)} recorded ({len(managed)} ongoing)"]
    try:
        problem = repository.problem()
    except (FileNotFoundError, ValueError):
        return lines
    lines.insert(0, f"metric: {problem.raw}")
    best = repository.best_records(Objective(problem.direction, problem.metric), limit=1)
    if best:
        value = best[0].document.metrics[problem.metric]
        lines.append(
            f"best: {best[0].sha[:7]}  {problem.metric}={json.dumps(value)}  "
            f"({_relative_time(best[0].date)})"
        )
    trend = _recent_trend(records, problem.metric)
    if trend is not None:
        delta, sample_size, span_ms = trend
        lines.append(
            f"recent trend: {_signed_number(delta)} over last {sample_size} recorded experiments "
            f"({_duration_ms(span_ms)} span)"
        )
    return lines


def _recent_experiment_lines(
    repository: ExperimentRepository,
) -> list[str]:
    recent = repository.recent_records(5)
    if not recent:
        return []
    try:
        problem = repository.problem()
    except (FileNotFoundError, ValueError):
        problem = None
    lines = []
    for record in recent:
        metrics = ""
        if problem is not None:
            value = record.document.metrics.get(problem.metric)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics = f"{problem.metric}={json.dumps(value)}"
        if not metrics:
            metrics = _metric_inline(record.document.metrics)
        detail = f"({_relative_time(record.date)})"
        summary = _truncate_summary(record.document.summary)
        line = f"{record.sha[:7]}"
        if metrics:
            line += f"  {metrics}"
        line += f"  {detail}"
        if summary:
            line += f" | {summary}"
        lines.append(line)
    return lines


def _managed_worktree_lines(worktrees: list[ExperimentWorktree]) -> list[str]:
    return [
        f"{worktree.name} @ {worktree.head[:7]} "
        f"({'missing' if worktree.is_missing else 'dirty' if worktree.dirty else 'clean'})"
        for worktree in worktrees
    ]


def _other_worktree_lines(worktrees: list[ExperimentWorktree]) -> list[str]:
    lines = []
    for worktree in worktrees:
        labels = [worktree.branch or "(detached HEAD)"]
        if worktree.is_current:
            labels.append("current")
        if worktree.is_primary:
            labels.append("primary")
        if worktree.is_managed:
            labels.append("managed")
        elif not worktree.is_primary:
            labels.append("unmanaged")
        labels.append("missing" if worktree.is_missing else "dirty" if worktree.dirty else "clean")
        lines.append(f"{worktree.path} [{', '.join(labels)}] @ {worktree.head[:7]}")
    return lines


def _render_log_record(record: ExperimentRecord) -> str:
    lines = [f"commit {record.sha[:7]}", f"date: {record.date}"]
    lines.extend(
        _section(
            "experiment",
            [
                f"summary: {record.document.summary}",
                "metrics:",
                *[f"  {line}" for line in _metric_lines(record.document.metrics)],
            ],
            blank_line=False,
        )
    )
    lines.extend(_section("journal", record.journal.splitlines(), blank_line=False))
    return "\n".join(lines)


def _record_header(record: ExperimentRecord) -> str:
    header = "  ".join([record.sha[:7], record.date])
    metrics = _metric_inline(record.document.metrics)
    summary = f"| {record.document.summary}"
    if metrics:
        return f"{header} - {metrics} {summary}"
    return f"{header} {summary}"


def _experiment_lines(document: ExperimentDocument) -> list[str]:
    lines = [f"summary: {document.summary}", "metrics:"]
    lines.extend(f"  {line}" for line in _metric_lines(document.metrics))
    lines.append("references:")
    if not document.references:
        lines.append("  (none)")
        return lines
    for reference in document.references:
        lines.append(f"  {reference.commit[:7]}: {reference.why}")
    return lines


def _metric_lines(metrics: dict[str, MetricValue]) -> list[str]:
    if not metrics:
        return ["(none)"]
    return [f"{name}: {json.dumps(value)}" for name, value in metrics.items()]


def _metric_delta_lines(left: ExperimentRecord, right: ExperimentRecord) -> list[str]:
    lines: list[str] = []
    for name in sorted(set(left.document.metrics) | set(right.document.metrics)):
        left_value = left.document.metrics.get(name)
        right_value = right.document.metrics.get(name)
        if (
            isinstance(left_value, (int, float))
            and not isinstance(left_value, bool)
            and isinstance(right_value, (int, float))
            and not isinstance(right_value, bool)
        ):
            delta = float(right_value) - float(left_value)
            lines.append(
                f"{name}: {json.dumps(left_value)} -> {json.dumps(right_value)} ({delta:+g})"
            )
            continue
        lines.append(f"{name}: {json.dumps(left_value)} -> {json.dumps(right_value)}")
    return lines


def _reference_diff_lines(left: ExperimentRecord, right: ExperimentRecord) -> list[str]:
    left_refs = {reference.commit for reference in left.document.references}
    right_refs = {reference.commit for reference in right.document.references}
    return [
        (
            "common: (none)"
            if not left_refs & right_refs
            else "common: " + ", ".join(commit[:7] for commit in sorted(left_refs & right_refs))
        ),
        (
            "left only: (none)"
            if not left_refs - right_refs
            else "left only: " + ", ".join(commit[:7] for commit in sorted(left_refs - right_refs))
        ),
        (
            "right only: (none)"
            if not right_refs - left_refs
            else "right only: " + ", ".join(commit[:7] for commit in sorted(right_refs - left_refs))
        ),
    ]


def _changed_path_lines(changed_paths: tuple[GitChangedPath, ...]) -> list[str]:
    lines: list[str] = []
    for item in changed_paths:
        if item.previous_path is None:
            lines.append(f"{item.status}  {item.path}")
        else:
            lines.append(f"{item.status}  {item.previous_path} -> {item.path}")
    return lines


def _render_lineage_edge(edge: LineageEdge) -> str:
    line = f"{edge.kind}  {edge.source[:7]} -> {edge.target[:7]}"
    if edge.why:
        line += f" - {edge.why}"
    return line


def _metric_inline(metrics: dict[str, MetricValue]) -> str:
    return ", ".join(f"{name}={json.dumps(value)}" for name, value in metrics.items())


def _relative_time(iso_date: str) -> str:
    target = _parse_date(iso_date)
    delta = datetime.now(timezone.utc) - target.astimezone(timezone.utc)
    seconds = abs(delta.total_seconds())
    if seconds < 60:
        return "just now"
    units = (
        ("y", 365 * 24 * 60 * 60),
        ("mo", 30 * 24 * 60 * 60),
        ("w", 7 * 24 * 60 * 60),
        ("d", 24 * 60 * 60),
        ("h", 60 * 60),
        ("m", 60),
    )
    for label, size in units:
        if seconds >= size:
            value = round(seconds / size)
            return f"{value}{label} ago" if delta.total_seconds() >= 0 else f"in {value}{label}"
    return "just now"


def _signed_number(value: float) -> str:
    rounded = float(f"{value:.6g}")
    return f"{rounded:+g}"


def _duration_ms(duration_ms: int) -> str:
    units = (
        ("d", 24 * 60 * 60 * 1000),
        ("h", 60 * 60 * 1000),
        ("m", 60 * 1000),
        ("s", 1000),
    )
    for label, size in units:
        if duration_ms >= size:
            return f"{round(duration_ms / size)}{label}"
    return "0s"


def _recent_trend(records: list[ExperimentRecord], metric: str) -> tuple[float, int, int] | None:
    sample = [
        record
        for record in sorted(records, key=lambda record: _parse_date(record.date), reverse=True)
        if isinstance(record.document.metrics.get(metric), (int, float))
        and not isinstance(record.document.metrics.get(metric), bool)
    ][:5]
    if len(sample) < 2:
        return None
    newest = sample[0].document.metrics[metric]
    oldest = sample[-1].document.metrics[metric]
    if not isinstance(newest, (int, float)) or isinstance(newest, bool):
        return None
    if not isinstance(oldest, (int, float)) or isinstance(oldest, bool):
        return None
    span_ms = int(
        (_parse_date(sample[0].date) - _parse_date(sample[-1].date)).total_seconds() * 1000
    )
    return float(newest) - float(oldest), len(sample), max(0, span_ms)


def _truncate_summary(summary: str, max_length: int = 120) -> str:
    compact = " ".join(summary.split())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 3].rstrip() + "..."


def _parse_date(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _parse_depth(value: str) -> int | None:
    text = value.strip().lower()
    if text == "all":
        return None
    try:
        parsed = int(text)
    except ValueError as error:
        raise typer.BadParameter("Depth must be a positive integer or 'all'.") from error
    if parsed <= 0:
        raise typer.BadParameter("Depth must be a positive integer or 'all'.")
    return parsed


def _section(title: str, body: list[str], *, blank_line: bool = True) -> list[str]:
    lines = [f"{title}:"]
    if not body:
        lines.append("  (none)")
    else:
        for line in body:
            lines.append(f"  {line}" if line else "")
    if blank_line:
        lines.append("")
    return lines
