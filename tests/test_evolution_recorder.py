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
