"""Tests for execution tracker."""
import json
import sqlite3

from rag.tracker import ExecutionTrace, ExecutionTracker, ToolCall


def test_ensure_table_creates_execution_logs(tmp_path):
    """ExecutionTracker should create execution_logs table on init."""
    db_path = str(tmp_path / "test.db")
    ExecutionTracker(db_path=db_path)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='execution_logs'").fetchall()
    conn.close()
    assert len(rows) == 1


def test_save_inserts_record(tmp_path):
    """save() should insert a trace into execution_logs."""
    db_path = str(tmp_path / "test.db")
    tracker = ExecutionTracker(db_path=db_path)
    trace = ExecutionTrace(
        question="测试问题",
        route="rag",
        answer="测试回答",
        total_ms=123.4,
        tool_calls=[],
    )
    tracker.save(trace)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT * FROM execution_logs").fetchall()
    conn.close()
    assert len(rows) == 1


def test_get_recent_returns_saved_traces(tmp_path):
    """get_recent() should return saved traces with correct fields."""
    db_path = str(tmp_path / "test.db")
    tracker = ExecutionTracker(db_path=db_path)
    trace = ExecutionTrace(
        question="什么是 Raft？",
        route="rag",
        answer="Raft 是一致性协议",
        total_ms=500.0,
        tool_calls=[ToolCall(tool_name="retrieve", input="Raft", output="文档片段", duration_ms=200.0)],
    )
    tracker.save(trace)
    results = tracker.get_recent(limit=10)
    assert len(results) == 1
    assert results[0]["question"] == "什么是 Raft？"
    assert results[0]["route"] == "rag"
    assert results[0]["answer"] == "Raft 是一致性协议"
    assert results[0]["total_ms"] == 500.0


def test_get_recent_empty(tmp_path):
    """get_recent() should return empty list when no records."""
    db_path = str(tmp_path / "test.db")
    tracker = ExecutionTracker(db_path=db_path)
    results = tracker.get_recent()
    assert results == []


def test_decision_metadata_allowlist_keeps_both_stage_versions(tmp_path):
    """The sanitizer keeps the passages version next to the preflight one.

    A key that is not allowlisted is dropped before persistence, so a
    passages-stage version that only exists in the in-memory trace would still be
    lost in the stored observation.
    """
    db_path = str(tmp_path / "test.db")
    tracker = ExecutionTracker(db_path=db_path)
    trace = ExecutionTrace(
        question="年假几天",
        route="rag",
        answer="7 天",
        total_ms=10.0,
        decision_metadata={
            "question_set_version": "preflight-v1",
            "passages_question_set_version": "passages-v1+candidate-3",
            "question": "must not be persisted",
        },
    )
    tracker.save(trace)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT details FROM execution_logs LIMIT 1").fetchone()
    conn.close()
    metadata = json.loads(row[0])["decision_metadata"]
    assert metadata["question_set_version"] == "preflight-v1"
    assert metadata["passages_question_set_version"] == "passages-v1+candidate-3"
    assert "question" not in metadata


def test_tool_calls_serialized_as_json(tmp_path):
    """Tool calls should be serialized as JSON in details column."""
    db_path = str(tmp_path / "test.db")
    tracker = ExecutionTracker(db_path=db_path)
    trace = ExecutionTrace(
        question="计算增长率",
        route="agent",
        answer="增长率为 12.36%",
        total_ms=800.0,
        tool_calls=[
            ToolCall(tool_name="calculate", input="(1200-1068)/1068*100", output="12.36", duration_ms=50.0),
            ToolCall(tool_name="sql_query", input="SELECT * FROM sales", output="3 rows", duration_ms=100.0),
        ],
    )
    tracker.save(trace)
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT details FROM execution_logs LIMIT 1").fetchone()
    conn.close()
    details = json.loads(row[0])
    tool_calls = details["tool_calls"]
    assert len(tool_calls) == 2
    assert tool_calls[0]["tool"] == "calculate"
    assert tool_calls[0]["input"] == "(1200-1068)/1068*100"
    assert tool_calls[0]["output"] == "12.36"
    assert tool_calls[0]["ms"] == 50.0
    assert tool_calls[1]["tool"] == "sql_query"

def test_tracker_persists_only_finite_known_stage_timings_and_bounded_outcome(tmp_path):
    db_path = str(tmp_path / "timings.db")
    tracker = ExecutionTracker(db_path=db_path)
    tracker.save(
        ExecutionTrace(
            question="q",
            route="rag",
            stage_timings={
                "preparation_ms": 12,
                "jev_preflight_ms": 1,
                "jev_passages_ms": 2,
                "rerank_ms": 3.5,
                "generation_ms": 4,
                "unknown_ms": 99,
                "negative_ms": -1,
                "infinite_ms": float("inf"),
                "nan_ms": float("nan"),
                "boolean_ms": True,
                "text_ms": "7",
            },
            decision_metadata={"execution_outcome": "degraded", "unknown": "drop"},
        )
    )
    tracker.save(
        ExecutionTrace(
            question="q2",
            route="rag",
            stage_timings={"generation_ms": 5},
            decision_metadata={"execution_outcome": "arbitrary"},
        )
    )

    rows = tracker.get_recent()
    first = json.loads(rows[1]["details"])
    second = json.loads(rows[0]["details"])
    assert first["stage_timings"] == {
        "preparation_ms": 12.0,
        "jev_preflight_ms": 1.0,
        "jev_passages_ms": 2.0,
        "rerank_ms": 3.5,
        "generation_ms": 4.0,
    }
    assert first["decision_metadata"]["execution_outcome"] == "degraded"
    assert "execution_outcome" not in second["decision_metadata"]
    tracker.close()


def test_tracker_sqlite_schema_stays_unchanged_with_observation_fields(tmp_path):
    db_path = str(tmp_path / "schema.db")
    tracker = ExecutionTracker(db_path=db_path)
    tracker.save(ExecutionTrace(question="q", route="rag", stage_timings={"generation_ms": 1}))
    columns = [row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(execution_logs)")]
    assert columns == ["id", "timestamp", "question", "route", "answer", "total_ms", "details"]
    tracker.close()
