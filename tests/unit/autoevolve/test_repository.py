import pytest

from autoevolve.repository import parse_experiment_document


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
