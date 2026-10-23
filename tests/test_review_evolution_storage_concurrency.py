"""Cross-connection state and evidence must survive real SQLite interleavings."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from rag.evolution.candidate_store import CandidateStore
from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.code_patches import CodePatchCandidate
from rag.evolution.models import FailureObservation
from rag.evolution.store import FailureTraceStore
from rag.evolution.worker import (
    _clusters_with_open_candidates,
    _eligible_patch_items,
    _eligible_question_set_items,
)


def _seed(trace, store, key):
    cluster = trace.upsert_cluster(
        cluster_key=key, kb_id="kb_probe", failure_stage="retrieval",
        root_cause_feature="recall_miss", seen_at=1.0,
    )
    candidate = CodePatchCandidate(
        artifact_type="code_patch", layer="reranking", paths=("rag/reranker.py",),
        diff="--- a/rag/reranker.py\n+++ b/rag/reranker.py\n@@ -1 +1 @@\n-old\n+new\n",
        test_paths=("tests/test_reranker.py",), rationale="probe",
        expected_effect="probe", rollback_ref={"base_commit": "a" * 40},
        confidence="medium",
    )
    return store.add_candidate(None, cluster["id"], candidate), cluster["id"]


@pytest.fixture
def stores(tmp_path):
    path = tmp_path / "evolution.db"
    trace = FailureTraceStore(path)
    first, second = CandidateStore(db_path=path), CandidateStore(db_path=path)
    try:
        yield trace, first, second
    finally:
        first.close()
        second.close()
        trace.close()


def _interleave(monkeypatch, first, first_call, second_call):
    reached, release, second_started = Event(), Event(), Event()
    original = CandidateStore._insert_audit

    def pause_first(self, *args, **kwargs):
        if self is first:
            reached.set()
            assert release.wait(5), "test did not release first writer"
        return original(self, *args, **kwargs)

    def run_second():
        second_started.set()
        return second_call()

    monkeypatch.setattr(CandidateStore, "_insert_audit", pause_first)
    with ThreadPoolExecutor(2) as pool:
        one = pool.submit(first_call)
        try:
            assert reached.wait(5)
            two = pool.submit(run_second)
            assert second_started.wait(5)
            # Let an unprotected writer finish; a correct writer waits for SQLite.
            try:
                second_result = two.result(timeout=0.25)
            except TimeoutError:
                second_result = None
        finally:
            release.set()
        first_result = one.result(timeout=5)
        if second_result is None:
            second_result = two.result(timeout=5)
    return first_result, second_result


def test_same_transition_is_accepted_only_once_across_connections(stores, monkeypatch):
    trace, first, second = stores
    candidate_id, _ = _seed(trace, first, "transition")

    def advance(store):
        try:
            store.advance_candidate(candidate_id, "awaiting_verification", actor_id="test", note="advance")
        except ValueError:
            return "refused"
        return "accepted"

    outcomes = _interleave(monkeypatch, first, lambda: advance(first), lambda: advance(second))
    assert sorted(outcomes) == ["accepted", "refused"]
    assert len(first.list_audit(candidate_id)) == 2  # generation plus one advance


def test_gate_evidence_is_not_lost_across_connections(stores, monkeypatch):
    trace, first, second = stores
    candidate_id, _ = _seed(trace, first, "evidence")

    def record(store, gate):
        store.record_gate_entry(
            candidate_id, actor_id="test", note=gate, gate=gate,
            result="passed", gate_detail="independent evidence",
        )
        return gate

    _interleave(monkeypatch, first, lambda: record(first, "one"), lambda: record(second, "two"))
    gates = {entry["gate"] for entry in first.get_candidate(candidate_id)["gate_log"]}
    assert gates == {"scope", "one", "two"}


def test_failed_audit_rolls_back_generation_and_does_not_poison_later_writes(stores, monkeypatch):
    trace, first, _ = stores
    original = CandidateStore._insert_audit

    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("audit persistence interrupted")

    with monkeypatch.context() as patcher:
        patcher.setattr(CandidateStore, "_insert_audit", fail_after_write)
        with pytest.raises(RuntimeError, match="interrupted"):
            _seed(trace, first, "failed")
    assert first.list_candidates() == []
    assert first.list_audit() == []
    candidate_id, _ = _seed(trace, first, "successful")
    assert [row["id"] for row in first.list_candidates()] == [candidate_id]


@pytest.mark.parametrize(
    "operation",
    ["advance", "gate", "inconclusive", "audit"],
)
def test_audit_failure_rolls_back_every_candidate_mutation(stores, monkeypatch, operation):
    trace, first, _ = stores
    candidate_id, _ = _seed(trace, first, f"atomic-{operation}")
    before = first.get_candidate(candidate_id)
    audit_count = len(first.list_audit(candidate_id))
    original = CandidateStore._insert_audit

    def fail_after_insert(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("audit interrupted")

    monkeypatch.setattr(CandidateStore, "_insert_audit", fail_after_insert)
    with pytest.raises(RuntimeError, match="interrupted"):
        if operation == "advance":
            first.advance_candidate(
                candidate_id, "awaiting_verification", actor_id="test", note="advance"
            )
        elif operation == "gate":
            first.record_gate_entry(
                candidate_id,
                actor_id="test",
                note="gate",
                gate="benchmark",
                result="passed",
                gate_detail="evidence",
            )
        elif operation == "inconclusive":
            first.record_inconclusive_gate(
                candidate_id,
                actor_id="test",
                note="retry",
                gate="benchmark",
                gate_detail="provider unavailable",
            )
        else:
            first.add_audit("test", "advance", candidate_id, "manual")

    after = first.get_candidate(candidate_id)
    assert after["status"] == before["status"]
    assert after["gate_log"] == before["gate_log"]
    assert len(first.list_audit(candidate_id)) == audit_count


def test_open_cluster_guard_does_not_lose_older_candidates_after_fifty_rows(stores):
    trace, first, _ = stores
    _, old_cluster = _seed(trace, first, "old-open")
    for index in range(50):
        candidate_id, _ = _seed(trace, first, f"finished-{index}")
        first.advance_candidate(candidate_id, "rejected", actor_id="test", note="done")
    assert old_cluster in _clusters_with_open_candidates(first, "code_patch")


class _OneApprovedObservation:
    def __init__(self, cluster_id, observation_id, layer):
        self.cluster = {
            "id": cluster_id,
            "status": "approved_for_candidate",
            "confirmed_layer": layer,
        }
        self.observation = {"id": observation_id, "question": "question"}
        self.layer = layer

    def list_drafts(self):
        return [
            {
                "observation_id": self.observation["id"],
                "payload": {"suggested_layer": self.layer},
            }
        ]

    def list_clusters(self):
        return [self.cluster]

    def list_observations(self, cluster_id=None):
        return [self.observation]


def _fill_finished_candidate_history(trace, candidate_store, artifact_type):
    cluster = trace.upsert_cluster(
        cluster_key=f"history-{artifact_type}",
        kb_id="kb_probe",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=1.0,
    )
    observations = []
    for index in range(51):
        observation = trace.add_observation(
            FailureObservation(kb_id="kb_probe", question=f"q{index}", route="rag")
        )
        observations.append(observation)

    if artifact_type == "code_patch":
        target = CodePatchCandidate(
            artifact_type="code_patch", layer="reranking", paths=("rag/a.py",),
            diff="--- a/rag/a.py\n+++ b/rag/a.py\n@@ -1 +1 @@\n-a\n+b\n",
            test_paths=("tests/test_a.py",), rationale="probe", expected_effect="probe",
            rollback_ref={"base_commit": "a" * 40}, confidence="medium",
        )
        layer = "reranking"
    else:
        target = JevQuestionSetCandidate(
            artifact_type="jev_question_set", target="passages",
            payload={"c1": {"instructions": "specific question"}},
            rationale="probe", expected_effect="probe",
            rollback_ref={"question_set_version": "v1", "payload_sha256": "0" * 64},
            confidence="low",
        )
        layer = "prompt"

    for observation in observations:
        candidate_id = candidate_store.add_candidate(
            observation["id"], cluster["id"], target
        )
        candidate_store.advance_candidate(
            candidate_id, "rejected", actor_id="test", note="finished"
        )
    return cluster["id"], observations[0]["id"], layer


def test_old_patch_observation_is_not_requeued_after_fifty_newer_candidates(stores):
    trace, first, _ = stores
    cluster_id, observation_id, layer = _fill_finished_candidate_history(
        trace, first, "code_patch"
    )

    eligible = _eligible_patch_items(
        _OneApprovedObservation(cluster_id, observation_id, layer), first
    )

    assert eligible == []


def test_old_question_set_observation_is_not_requeued_after_fifty_newer_candidates(stores):
    trace, first, _ = stores
    cluster_id, observation_id, layer = _fill_finished_candidate_history(
        trace, first, "jev_question_set"
    )

    eligible = _eligible_question_set_items(
        _OneApprovedObservation(cluster_id, observation_id, layer), first
    )

    assert eligible == []
