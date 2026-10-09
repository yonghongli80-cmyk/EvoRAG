"""Task 1: bounded, non-blocking recording of ACL-filtered failure observations."""

import json
import time

import pytest
from pydantic import ValidationError

from rag.evolution.models import DiagnosticDetail, FailureObservation, NonEvolvableReason
from rag.evolution.recorder import FailureRecorder
from rag.evolution.store import FailureTraceStore
from rag.tracker import ExecutionTrace


def trace(**kw):
    base = dict(question="q", route="rag", answer="a", total_ms=10.0)
    base.update(kw)
    return ExecutionTrace(**base)


def evolvable_detail(**kw):
    base = dict(kb_id="kb1", failure_stage="retrieval", chunk_hashes=("a" * 64,), best_score=0.11)
    base.update(kw)
    return DiagnosticDetail(**base)


def test_submit_never_blocks_on_store(tmp_path):
    class BlockingStore(FailureTraceStore):
        def add_observation(self, observation):
            time.sleep(0.5)
            return super().add_observation(observation)

    recorder = FailureRecorder(BlockingStore(tmp_path / "e.db"))
    started = time.perf_counter()
    assert recorder.submit(trace(), evolvable_detail()) is True
    assert time.perf_counter() - started < 0.05
    recorder.close()


def test_full_queue_drops_instead_of_blocking(tmp_path):
    class BlockingStore(FailureTraceStore):
        def add_observation(self, observation):
            time.sleep(0.3)
            return super().add_observation(observation)

    recorder = FailureRecorder(BlockingStore(tmp_path / "e.db"), max_queue=1)
    assert recorder.submit(trace(), evolvable_detail()) is True
    assert recorder.submit(trace(), evolvable_detail()) is False
    recorder.close()


def test_blocked_and_transient_requests_never_become_observations(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    recorder = FailureRecorder(store)
    assert recorder.submit(trace(route="blocked"), evolvable_detail()) is True  # logged as skip
    detail = evolvable_detail(non_evolvable_reason=NonEvolvableReason.TRANSIENT_EXTERNAL)
    assert recorder.submit(trace(), detail) is True  # logged as skip
    recorder.close(wait=True)
    assert store.count_by_status() == {}
    skips = store.count_skips_by_reason()
    assert skips["permission_denied"] == 1
    assert skips["transient_external"] == 1


def test_persisted_row_has_no_chunk_text_or_secret(tmp_path):
    store = FailureTraceStore(tmp_path / "e.db")
    recorder = FailureRecorder(store)
    t = trace(decision_metadata={
        "question_set_version": "passages-v1",
        "passages_question_set_version": "passages-v1+candidate-3",
        "jev_route": "rag",
        "secret_like": "secret-test-key",
    })
    recorder.submit(t, evolvable_detail(adopted_evidence_hashes=("b" * 64,)))
    recorder.close(wait=True)
    row = store.get_observation(1)
    blob = json.dumps(row, ensure_ascii=False)
    assert "secret-test-key" not in blob
    assert row["question"] == "q"  # question text is diagnosable, already stored by tracker
    assert row["stage"] == "retrieval" and row["kb_id"] == "kb1"
    assert row["details_decision_metadata"]["question_set_version"] == "passages-v1"
    # A passages canary stays attributable in the stored observation, not only in
    # the in-memory trace: the allowlist has to carry the passages version too.
    assert row["details_decision_metadata"]["passages_question_set_version"] == "passages-v1+candidate-3"


def test_diagnostic_detail_rejects_unknown_fields():
    """extra="forbid" keeps secrets and chunk text out of the store by construction."""
    with pytest.raises(ValidationError):
        evolvable_detail(secret_like="secret-test-key")


def test_failure_observation_defaults_to_the_observation_queue():
    observation = FailureObservation(kb_id="kb1", failure_stage="retrieval", question="q", route="rag")
    assert observation.status == "observed"
    assert observation.pool == "observation_queue"
    assert observation.priority == "normal"


def test_the_exception_frame_round_trips_through_the_store(tmp_path):
    """帧要能存能读：它是补丁生成器选文件的事实依据。"""
    store = FailureTraceStore(tmp_path / "e.db")

    row = store.add_observation(
        FailureObservation(
            kb_id="kb1",
            question="q",
            route="error",
            exception_class="ValueError",
            exception_frame="rag/reliability/bundle.py:29:_require_versioned",
        )
    )

    assert row["details_exception_frame"] == "rag/reliability/bundle.py:29:_require_versioned"
    assert store.get_observation(row["id"])["details_exception_frame"] == row["details_exception_frame"]
    store.close()


def test_a_database_written_before_the_frame_column_gains_it(tmp_path):
    """既有库用 ADD COLUMN 补列（旧行读回 NULL），不重建表——重建会删掉已审核的历史。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE failure_observations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, kb_id TEXT NOT NULL, stage TEXT NOT NULL, "
        "root_cause_feature TEXT, status TEXT NOT NULL, pool TEXT NOT NULL, "
        "priority TEXT NOT NULL, question TEXT NOT NULL, route TEXT NOT NULL, cluster_id INTEGER, "
        "details_answerability_kind TEXT, details_confidence_level TEXT, "
        "details_chunk_hashes TEXT NOT NULL DEFAULT '[]', details_best_score REAL, "
        "details_adopted_evidence_hashes TEXT NOT NULL DEFAULT '[]', "
        "details_tool_names TEXT NOT NULL DEFAULT '[]', details_structured_query_kind TEXT, "
        "details_exception_class TEXT, details_feedback_kind TEXT, "
        "details_jev_question_set_version TEXT, "
        "details_decision_metadata TEXT NOT NULL DEFAULT '{}', total_ms REAL NOT NULL DEFAULT 0, "
        "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO failure_observations (kb_id, stage, status, pool, priority, question, route, "
        "created_at, updated_at) VALUES ('kb1', 'unknown', 'observed', 'observation_queue', "
        "'normal', 'old', 'rag', 1.0, 1.0)"
    )
    connection.commit()
    connection.close()

    store = FailureTraceStore(db)

    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(failure_observations)")}
    assert "details_exception_frame" in columns
    assert store.list_observations()[0]["question"] == "old"  # 旧行还在
    store.close()
