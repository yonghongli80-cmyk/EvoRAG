"""Task 1: diagnosis drafts and the admin audit trail (design sections 6 and 8).

A draft is persisted verbatim (JSON payload + prompt version + model) and is
never a confirmed conclusion; the audit trail records every admin decision with
actor, action, cluster, and note.
"""

import time

import pytest

from rag.evolution.models import (
    DiagnosisDraft,
    EvolutionStatus,
    ObservationPool,
)
from rag.evolution.store import FailureTraceStore


def sample_draft(**kw):
    """A minimal valid ``DiagnosisDraft``; every field is required."""
    base = dict(
        failure_category="recall_miss",
        stage="retrieval",
        first_divergence="重检索未命中目标块",
        direct_evidence="失败与重检索的块哈希集合均缺少 h1",
        root_cause="假设：切分把目标段落截断",
        impact_scope="kb1 内年假类问题",
        suggested_layer="chunking",
        reproduction_steps="以原问题重检索并核对块哈希",
        acceptance_criteria="重检索命中 h1 且答案引用证据",
        confidence="low",
        reproducible=True,
        evidence_refs=("a" * 64,),
    )
    base.update(kw)
    return DiagnosisDraft(**base)


def make_cluster(store, *, key, status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value):
    """Insert one cluster straight into ``status`` (no clusterer needed)."""
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key=key,
        kb_id="kb1",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    if status == EvolutionStatus.CLUSTERED.value:
        return cluster
    return store.promote_cluster(
        cluster["id"],
        new_status=status,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=now,
    )


def test_add_draft_round_trips_payload_and_metadata(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")
    payload = sample_draft()

    draft_id = store.add_draft(
        cluster["id"], payload, prompt_version="diagnosis-v1", model="deepseek/deepseek-v4.1-flash"
    )

    assert isinstance(draft_id, int)
    stored = store.get_draft(draft_id)
    assert stored is not None
    assert stored["cluster_id"] == cluster["id"]
    assert stored["prompt_version"] == "diagnosis-v1"
    assert stored["model"] == "deepseek/deepseek-v4.1-flash"
    assert stored["reproducible"] is True
    assert stored["payload"] == payload.model_dump(mode="json")
    assert stored["created_at"] > 0
    assert store.get_draft(draft_id + 999) is None

    second = store.add_draft(
        cluster["id"],
        sample_draft(reproducible=False, confidence="high"),
        prompt_version="diagnosis-v2",
        model="deepseek/deepseek-v4-flash-fast",
    )
    re_drafted = store.get_draft(second)
    assert re_drafted["prompt_version"] == "diagnosis-v2"
    assert re_drafted["reproducible"] is False
    assert draft_id != second


def test_add_draft_rejects_an_unknown_cluster(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    with pytest.raises(KeyError):
        store.add_draft(999, sample_draft(), prompt_version="diagnosis-v1", model="m")


def test_list_drafts_filters_by_cluster_newest_first(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    first_cluster = make_cluster(store, key="awaiting-1")
    second_cluster = make_cluster(store, key="awaiting-2")
    older = store.add_draft(first_cluster["id"], sample_draft(), prompt_version="v1", model="m")
    newer = store.add_draft(
        first_cluster["id"], sample_draft(root_cause="第二轮诊断"), prompt_version="v2", model="m"
    )
    other = store.add_draft(second_cluster["id"], sample_draft(), prompt_version="v1", model="m")

    rows = store.list_drafts(first_cluster["id"])

    assert [row["id"] for row in rows] == [newer, older]
    assert [row["prompt_version"] for row in rows] == ["v2", "v1"]
    assert rows[0]["payload"]["root_cause"] == "第二轮诊断"
    assert [row["id"] for row in store.list_drafts(second_cluster["id"])] == [other]
    assert len(store.list_drafts()) == 3


def test_clusters_awaiting_diagnosis_without_draft_filters_and_limits(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    oldest_ready = make_cluster(store, key="awaiting-1")
    still_clustered = make_cluster(store, key="still-clustered", status=EvolutionStatus.CLUSTERED.value)
    newer_ready = make_cluster(store, key="awaiting-2")
    rejected = make_cluster(store, key="rejected", status=EvolutionStatus.REJECTED.value)
    drafted = make_cluster(store, key="awaiting-3")
    store.add_draft(drafted["id"], sample_draft(), prompt_version="v1", model="m")

    rows = store.clusters_awaiting_diagnosis_without_draft(limit=10)

    assert [row["id"] for row in rows] == [oldest_ready["id"], newer_ready["id"]]
    assert {row["cluster_key"] for row in rows} == {"awaiting-1", "awaiting-2"}
    assert all(row["status"] == EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value for row in rows)
    assert all(row["pool"] == ObservationPool.CANDIDATE_POOL.value for row in rows)
    assert "member_count" in rows[0]
    skipped = {still_clustered["id"], rejected["id"], drafted["id"]}
    assert not skipped.intersection(row["id"] for row in rows)
    assert [row["id"] for row in store.clusters_awaiting_diagnosis_without_draft(limit=1)] == [
        oldest_ready["id"]
    ]


def test_add_audit_appends_rows_and_rejects_unknown_actions(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")
    other = make_cluster(store, key="awaiting-2")

    first = store.add_audit("admin-1", "needs_more_evidence", cluster["id"], "缺少管理员确认标签")
    second = store.add_audit("admin-2", "approve_for_candidate", cluster["id"], "")
    third = store.add_audit("admin-3", "reject", other["id"], "非缺陷")

    rows = store.list_audit(cluster["id"])

    assert [row["id"] for row in rows] == [first, second]
    assert [row["actor_id"] for row in rows] == ["admin-1", "admin-2"]
    assert [row["action"] for row in rows] == ["needs_more_evidence", "approve_for_candidate"]
    assert [row["cluster_id"] for row in rows] == [cluster["id"], cluster["id"]]
    assert [row["note"] for row in rows] == ["缺少管理员确认标签", ""]
    assert rows[0]["created_at"] <= rows[1]["created_at"]
    assert [row["id"] for row in store.list_audit(other["id"])] == [third]
    assert len(store.list_audit()) == 3
    for illegal in ("approve", "APPROVE_FOR_CANDIDATE", "", "needs_more_evidence ", "delete"):
        with pytest.raises(ValueError):
            store.add_audit("admin-1", illegal, cluster["id"], "typo")
    assert len(store.list_audit()) == 3


def test_decide_cluster_advances_a_candidate_pool_cluster(tmp_path):
    """Admin decisions act on clusters already in the candidate pool.

    ``promote_cluster`` is a no-op outside the observation queue, but every
    ``awaiting_diagnosis_review`` cluster lives in the candidate pool, so the
    explicit decision path needs its own transition entry point.
    """
    from rag.evolution.models import FailureObservation

    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1", status=EvolutionStatus.CLUSTERED.value)
    member = store.add_observation(
        FailureObservation(kb_id="kb1", question="q", route="rag")
    )
    store.attach_cluster_member(
        member["id"],
        cluster["id"],
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        now=time.time(),
    )
    admitted = store.promote_cluster(
        cluster["id"],
        new_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=time.time(),
    )
    assert admitted["pool"] == ObservationPool.CANDIDATE_POOL.value

    decided = store.decide_cluster(
        cluster["id"],
        new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        now=time.time(),
    )

    assert decided["status"] == EvolutionStatus.APPROVED_FOR_CANDIDATE.value
    assert store.get_observation(member["id"])["status"] == EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value


def test_decide_cluster_rejects_illegal_transitions(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")

    with pytest.raises(ValueError, match="invalid transition"):
        store.decide_cluster(
            cluster["id"],
            new_status=EvolutionStatus.STABLE.value,
            pool=ObservationPool.CANDIDATE_POOL.value,
            member_status=EvolutionStatus.CLUSTERED.value,
            now=time.time(),
        )

    assert store.list_clusters()[0]["status"] == EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value
