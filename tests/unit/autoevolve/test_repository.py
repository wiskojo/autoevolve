import shutil
from pathlib import Path

import pytest
from git import Repo

from autoevolve.git import read_text_blob as git_read_text_blob
from autoevolve.git import read_text_blobs as git_read_text_blobs
from autoevolve.models.experiment import Objective
from autoevolve.models.types import GraphDirection, GraphEdges
from autoevolve.repository import (
    EXPERIMENT_FILE,
    JOURNAL_FILE,
    ExperimentRepository,
    parse_experiment_document,
)
from tests.e2e.conftest import FIXTURE_ROOT, RepoFixture


def test_parse_experiment_document_parses_valid_document() -> None:
    document = parse_experiment_document(
        """{
  "summary": "Improve benchmark score.",
  "metrics": {
    "benchmark_score": 0.91,
    "passed": true,
    "note": "fast"
  },
  "references": [
    {
      "commit": "abc1234",
      "why": "borrowed a ranking heuristic"
    }
  ]
}"""
    )

    assert document.summary == "Improve benchmark score."
    assert document.metrics == {"benchmark_score": 0.91, "passed": True, "note": "fast"}
    assert len(document.references) == 1
    assert document.references[0].commit == "abc1234"
    assert document.references[0].why == "borrowed a ranking heuristic"


def test_parse_experiment_document_rejects_invalid_metric_type() -> None:
    with pytest.raises(ValueError, match='EXPERIMENT.json field "metrics.samples" must be'):
        parse_experiment_document(
            """{
  "summary": "Bad metrics",
  "metrics": {
    "samples": []
  },
  "references": []
}"""
        )


def test_parse_experiment_document_rejects_blank_reference_reason() -> None:
    with pytest.raises(ValueError, match='references\\[0\\]\\.why" must be a non-empty string'):
        parse_experiment_document(
            """{
  "summary": "Bad reference",
  "metrics": {},
  "references": [
    {
      "commit": "abc1234",
      "why": "   "
    }
  ]
}"""
        )


def test_recent_index_only_materializes_requested_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _history_repo(tmp_path)
    refs: list[list[str]] = []
    original = git_read_text_blobs

    def tracking_read_text_blobs(repo: Repo, shas: list[str], path: str) -> dict[str, str | None]:
        if path == EXPERIMENT_FILE:
            refs.append(list(shas))
        return original(repo, shas, path)

    monkeypatch.setattr("autoevolve.repository.read_text_blobs", tracking_read_text_blobs)
    records = ExperimentRepository(fixture.root).recent_index(5)
    assert len(records) == 5
    assert refs == [[record.sha for record in records]]


def test_read_only_queries_do_not_load_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _history_repo(tmp_path)
    journal_reads: list[tuple[str, str]] = []
    original = git_read_text_blob

    def tracking_read_text_blob(repo: Repo, ref: str, path: str) -> str | None:
        if path == JOURNAL_FILE:
            journal_reads.append((ref, path))
        return original(repo, ref, path)

    monkeypatch.setattr("autoevolve.repository.read_text_blob", tracking_read_text_blob)
    repository = ExperimentRepository(fixture.root)
    assert len(repository.recent_index(5)) == 5
    assert len(repository.best_records(Objective("max", "benchmark_score"), 3)) == 3
    assert len(repository.pareto_records([Objective("max", "benchmark_score")])) == 1
    assert repository.index()
    assert journal_reads == []


def test_detail_only_loads_selected_experiment_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _history_repo(tmp_path)
    latest_sha = fixture.git("log", "--all", "--format=%H", "--", EXPERIMENT_FILE).splitlines()[0]
    journal_reads: list[str] = []
    original = git_read_text_blob

    def tracking_read_text_blob(repo: Repo, ref: str, path: str) -> str | None:
        if path == JOURNAL_FILE:
            journal_reads.append(ref)
        return original(repo, ref, path)

    monkeypatch.setattr("autoevolve.repository.read_text_blob", tracking_read_text_blob)
    detail = ExperimentRepository(fixture.root).detail(latest_sha)
    assert detail.journal
    assert journal_reads == [latest_sha]


def test_previous_record_and_lineage_use_index_graph(tmp_path: Path) -> None:
    fixture = _history_repo(tmp_path)
    repository = ExperimentRepository(fixture.root)
    root = repository.resolve_index("cross/hybrid-final")
    previous = repository.previous_record(root)
    assert previous is not None
    assert previous.document.summary.startswith("Balanced v2 combined island A")

    graph = repository.lineage(
        "cross/hybrid-final",
        edges=GraphEdges.ALL,
        direction=GraphDirection.BACKWARD,
        depth=3,
    )
    assert graph.root.sha == root.sha
    assert previous.sha in graph.node_order
    assert any(edge.kind == "reference" for edge in graph.edges)


def _history_repo(tmp_path: Path) -> RepoFixture:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    shutil.copytree(FIXTURE_ROOT, root)
    home.mkdir()
    fixture = RepoFixture(root=root, home=home)
    fixture.git("init", "-b", "main")
    fixture.git("config", "user.name", "Autoevolve Tests")
    fixture.git("config", "user.email", "tests@example.com")
    fixture.git("add", ".")
    fixture.git("commit", "-m", "Initial fixture")
    fixture.init_other()
    fixture.commit_all("Initialize autoevolve")
    fixture.populate_history()
    return fixture
