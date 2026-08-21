"""Tests for eval module."""
import json
from unittest.mock import MagicMock, patch

from rag.models import Chunk


def test_load_dataset(tmp_path):
    """load_dataset should parse JSONL into list of dicts."""
    from rag.eval import load_dataset

    path = tmp_path / "test.jsonl"
    path.write_text(
        '{"question": "Q1", "expected_keywords": ["A"]}\n'
        '{"question": "Q2", "expected_keywords": ["B", "C"]}\n',
        encoding="utf-8",
    )
    result = load_dataset(str(path))
    assert len(result) == 2
    assert result[0]["question"] == "Q1"
    assert result[0]["expected_keywords"] == ["A"]
    assert result[1]["expected_keywords"] == ["B", "C"]


def test_load_dataset_empty(tmp_path):
    """load_dataset on empty file should return empty list."""
    from rag.eval import load_dataset

    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    result = load_dataset(str(path))
    assert result == []


def test_evaluate_hit():
    """evaluate should mark hit=True when keyword found in answer."""
    from rag.eval import evaluate
    from rag.pipeline import QueryResult

    mock_pipeline = MagicMock()
    mock_pipeline.query.return_value = QueryResult(
        answer="NovaRegistry 使用 Raft 一致性协议",
        context=[Chunk(text="chunk", doc_name="doc", chunk_index=0)],
        sources=[{"doc_name": "doc", "chunk_index": 0}],
    )

    dataset = [{"question": "NovaRegistry 使用什么一致性协议？", "expected_keywords": ["Raft"]}]
    results = evaluate(mock_pipeline, dataset)

    assert len(results) == 1
    assert results[0].hit is True
    assert results[0].question == "NovaRegistry 使用什么一致性协议？"


def test_evaluate_miss():
    """evaluate should mark hit=False when keyword not in answer."""
    from rag.eval import evaluate
    from rag.pipeline import QueryResult

    mock_pipeline = MagicMock()
    mock_pipeline.query.return_value = QueryResult(
        answer="我不确定具体协议",
        context=[],
        sources=[],
    )

    dataset = [{"question": "NovaRegistry 使用什么一致性协议？", "expected_keywords": ["Raft"]}]
    results = evaluate(mock_pipeline, dataset)

    assert len(results) == 1
    assert results[0].hit is False


def test_compute_metrics():
    """compute_metrics should calculate hit_rate, avg_latency, pass/fail."""
    from rag.eval import EvalResult, compute_metrics

    results = [
        EvalResult(question="Q1", expected_keywords=["A"], answer="A found", hit=True, sources=[], latency_ms=100),
        EvalResult(question="Q2", expected_keywords=["B"], answer="no match", hit=False, sources=[], latency_ms=200),
        EvalResult(question="Q3", expected_keywords=["C"], answer="C found", hit=True, sources=[], latency_ms=300),
    ]
    metrics = compute_metrics(results)

    assert metrics["total"] == 3
    assert metrics["pass"] == 2
    assert metrics["fail"] == 1
    assert abs(metrics["hit_rate"] - 2 / 3) < 0.001
    assert metrics["avg_latency_ms"] == 200.0


def test_compute_metrics_empty():
    """compute_metrics on empty list should return zeros."""
    from rag.eval import compute_metrics

    metrics = compute_metrics([])
    assert metrics["total"] == 0
    assert metrics["hit_rate"] == 0
    assert metrics["avg_latency_ms"] == 0
    assert metrics["pass"] == 0
    assert metrics["fail"] == 0


@patch("rag.pipeline.RAGPipeline")
def test_cli_main(mock_pipeline_cls, tmp_path):
    """CLI main() should run evaluation and write history file."""
    from rag.eval import main
    from rag.pipeline import QueryResult

    # Prepare dataset file
    dataset_path = tmp_path / "eval.jsonl"
    dataset_path.write_text(
        '{"question": "Q1", "expected_keywords": ["A"]}\n',
        encoding="utf-8",
    )

    # Mock pipeline
    mock_pipeline = MagicMock()
    mock_pipeline.query.return_value = QueryResult(
        answer="A is correct", context=[], sources=[],
    )
    mock_pipeline_cls.return_value = mock_pipeline

    history_path = tmp_path / "history.jsonl"

    with patch("sys.argv", ["eval", "--dataset", str(dataset_path), "--file", "test.txt", "--history", str(history_path)]):
        main()

    # Verify history file written
    assert history_path.exists()
    with open(history_path, encoding="utf-8") as f:
        entry = json.loads(f.readline())
    assert entry["total"] == 1
    assert entry["pass"] == 1
    assert entry["hit_rate"] == 1.0
    assert "timestamp" in entry


def test_compute_metrics_includes_p95():
    from rag.eval import EvalResult, compute_metrics

    results = [
        EvalResult("q1", ["k1"], "a1", True, [], 100.0),
        EvalResult("q2", ["k2"], "a2", True, [], 200.0),
        EvalResult("q3", ["k3"], "a3", False, [], 500.0),
    ]
    metrics = compute_metrics(results)
    assert "p95_latency_ms" in metrics
    assert metrics["hit_rate"] == 2 / 3


def test_save_bad_case(tmp_path):
    from rag.eval import EvalResult, save_bad_case

    path = str(tmp_path / "bad_cases.jsonl")
    result = EvalResult("test question", ["keyword"], "wrong answer", False, [], 100.0)
    save_bad_case(result, path)

    with open(path) as f:
        entry = json.loads(f.readline())
    assert entry["question"] == "test question"
    assert entry["hit"] is False
