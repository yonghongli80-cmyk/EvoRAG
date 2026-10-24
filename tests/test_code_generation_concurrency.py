"""Code-patch admission must serialize against other generators across SQLite connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from rag.evolution.candidate_store import CandidateStore
from rag.evolution.code_patches import CodePatchCandidate
from rag.evolution.store import FailureTraceStore


def _patch(marker="generated"):
    return CodePatchCandidate(
        artifact_type="code_patch",
        layer="reranking",
        paths=("rag/reranker.py",),
        diff=f"--- a/rag/reranker.py\n+++ b/rag/reranker.py\n@@ -1 +1 @@\n-old\n+{marker}\n",
        test_paths=("tests/test_reranker.py",),
        rationale=marker,
        expected_effect="improve ranking",
        rollback_ref={"base_commit": "a" * 40},
        confidence="medium",
    )


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "evolution.db"
    trace = FailureTraceStore(path)
    first, second = CandidateStore(db_path=path), CandidateStore(db_path=path)
    cluster = trace.upsert_cluster(
        cluster_key="code-race", kb_id="kb-test", failure_stage="retrieval",
        root_cause_feature="recall_miss", seen_at=1.0,
    )
    try:
        yield trace, first, second, int(cluster["id"])
    finally:
        first.close()
        second.close()
        trace.close()


def _attempt(store, cluster_id, barrier, marker):
    barrier.wait(timeout=5)
    try:
        method = getattr(store, "add_generated_code_candidate", store.add_candidate)
        return ("created", method(None, cluster_id, _patch(marker), model="offline-test"))
    except Exception as error:
        if type(error).__name__ == "OpenCodeCandidateConflict":
            return ("conflict", None)
        raise


def test_two_connections_admit_only_one_open_code_patch_and_generation_audit(db):
    _, first, second, cluster_id = db
    barrier = Barrier(2)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(
            lambda args: _attempt(*args),
            [(first, cluster_id, barrier, "first"), (second, cluster_id, barrier, "second")],
        ))

    assert sorted(result[0] for result in results) == ["conflict", "created"]
    candidates = [
        row for row in first.list_candidates(limit=100)
        if row["cluster_id"] == cluster_id
    ]
    assert len(candidates) == 1
    assert [row["action"] for row in first.list_audit(candidates[0]["id"])] == ["generate"]


def test_generated_admission_rejects_non_code_patch_but_legacy_add_stays_compatible(db):
    _, first, _, cluster_id = db
    if not hasattr(first, "add_generated_code_candidate"):
        pytest.skip("RED proof above covers the absent admission guard")
    from types import SimpleNamespace

    question_candidate = SimpleNamespace(artifact_type="jev_question_set")
    with pytest.raises(ValueError, match="code_patch"):
        first.add_generated_code_candidate(None, cluster_id, question_candidate)


def test_different_cluster_and_terminal_candidate_do_not_block_generation(db):
    trace, first, _, cluster_id = db
    second_cluster = trace.upsert_cluster(
        cluster_key="other-cluster", kb_id="kb-test", failure_stage="retrieval",
        root_cause_feature="recall_miss", seen_at=1.0,
    )["id"]
    first.add_generated_code_candidate(None, cluster_id, _patch("one"))
    assert first.add_generated_code_candidate(None, int(second_cluster), _patch("other"))
    candidate_id = next(
        row["id"] for row in first.list_candidates(limit=10)
        if row["cluster_id"] == cluster_id
    )
    first.advance_candidate(candidate_id, "awaiting_verification", actor_id="test", note="check")
    first.advance_candidate(candidate_id, "rejected", actor_id="test", note="closed")
    assert first.add_generated_code_candidate(None, cluster_id, _patch("retry"))


def test_generation_audit_failure_rolls_back_candidate_and_audit(db, monkeypatch):
    _, first, _, cluster_id = db
    original = CandidateStore._insert_audit

    def fail_after_write(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("audit persistence interrupted")

    monkeypatch.setattr(CandidateStore, "_insert_audit", fail_after_write)
    with pytest.raises(RuntimeError, match="interrupted"):
        first.add_generated_code_candidate(None, cluster_id, _patch())
    assert first.list_candidates(limit=10) == []
    assert first.list_audit() == []
