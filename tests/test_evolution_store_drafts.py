"""Task 1: diagnosis drafts and the admin audit trail (design sections 6 and 8).

A draft is persisted verbatim (JSON payload + prompt version + model) and is
never a confirmed conclusion; the audit trail records every admin decision with
actor, action, cluster, and note.

Phase 4 granularity revision: the clustering key (kb × stage × root-cause
feature) mixes distinct root causes into one cluster, so a cluster-level single
draft can never match a mixed label set (``docs/evolution-diagnosis.md`` §5).
Drafts therefore move to the member-observation level: ``diagnosis_drafts``
gains a nullable ``observation_id`` (NULL = the legacy cluster-level shape),
and the worker drafts one diagnosis per member observation. These tests pin the
migration, the backward-compatible ``add_draft`` signature, and the
member-level "awaiting diagnosis without a draft" scan.
"""

import sqlite3
import time

import pytest

from rag.evolution.models import (
    DiagnosisDraft,
    EvolutionStatus,
    FailureObservation,
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


def make_awaiting_cluster_with_members(store, *, key, questions):
    """A cluster promoted into ``awaiting_diagnosis_review`` with member observations.

    Returns ``(cluster, members)`` where ``members`` is the stored observation
    rows (``id`` included), oldest first. Only existing store methods are used,
    so this mirrors what the recorder + clusterer produce in production.
    """
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key=key,
        kb_id="kb1",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    members = []
    for question in questions:
        member = store.add_observation(
            FailureObservation(
                kb_id="kb1",
                failure_stage="retrieval",
                question=question,
                route="rag",
                chunk_hashes=(),
                best_score=0.05,
                answerability_kind="insufficient_evidence",
            )
        )
        store.attach_cluster_member(
            member["id"],
            cluster["id"],
            failure_stage="retrieval",
            root_cause_feature="recall_miss",
            now=now,
        )
        members.append(member)
    promoted = store.promote_cluster(
        cluster["id"],
        new_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=now,
    )
    return promoted, members


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


def test_decide_cluster_records_the_administrator_confirmed_layer(tmp_path):
    """The human-confirmed root-cause layer persists on the cluster row.

    Design sections 6/8: the administrator confirms the layer a fix belongs to,
    which is the authority a code-patch candidate is generated against.
    """
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")

    decided = store.decide_cluster(
        cluster["id"],
        new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=time.time(),
        confirmed_layer="reranking",
    )

    assert decided["confirmed_layer"] == "reranking"
    assert store.list_clusters()[0]["confirmed_layer"] == "reranking"


def test_a_decision_without_a_confirmed_layer_leaves_it_null(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")

    decided = store.decide_cluster(
        cluster["id"],
        new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=time.time(),
    )

    assert decided["confirmed_layer"] is None


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


# ------------------------------------------------------------------
# Phase 4: member-observation-level drafts (docs/evolution-diagnosis.md §5).
#
# The clustering key mixes distinct root causes into one cluster, so a draft
# now belongs to a single member observation. ``diagnosis_drafts.observation_id``
# is nullable: NULL = the legacy cluster-level draft, an integer = a member draft.
# ------------------------------------------------------------------


def test_fresh_database_creates_the_observation_id_column(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(diagnosis_drafts)")}
    assert "observation_id" in columns
    store.close()


def test_add_draft_accepts_and_round_trips_an_observation_id(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster, members = make_awaiting_cluster_with_members(store, key="member-1", questions=["q1"])

    draft_id = store.add_draft(
        cluster["id"], sample_draft(), observation_id=members[0]["id"],
        prompt_version="diagnosis-v1", model="m",
    )

    stored = store.get_draft(draft_id)
    assert stored["cluster_id"] == cluster["id"]
    assert stored["observation_id"] == members[0]["id"]
    assert stored["payload"] == sample_draft().model_dump(mode="json")


def test_add_draft_without_observation_id_stays_a_legacy_cluster_draft(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")

    draft_id = store.add_draft(cluster["id"], sample_draft(), prompt_version="v1", model="m")

    assert store.get_draft(draft_id)["observation_id"] is None


def test_add_draft_rejects_an_unknown_observation(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = make_cluster(store, key="awaiting-1")
    with pytest.raises(KeyError):
        store.add_draft(cluster["id"], sample_draft(), observation_id=4242, prompt_version="v1", model="m")


def test_existing_old_shape_database_gains_the_column_with_null_legacy_drafts(tmp_path):
    """Migration: an old-shape ``diagnosis_drafts`` opens and is altered in place.

    A database written before phase 4 has no ``observation_id`` column; opening
    it must add the nullable column without dropping the stored cluster-level
    draft, whose ``observation_id`` then reads back as NULL.
    """
    db_path = tmp_path / "evolution.db"
    now = time.time()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE failure_clusters ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "cluster_key TEXT NOT NULL UNIQUE, kb_id TEXT NOT NULL,"
            "failure_stage TEXT NOT NULL, root_cause_feature TEXT, pool TEXT NOT NULL,"
            "status TEXT NOT NULL, priority TEXT NOT NULL, member_count INTEGER NOT NULL DEFAULT 0,"
            "first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,"
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE diagnosis_drafts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id INTEGER NOT NULL,"
            "payload JSON NOT NULL, prompt_version TEXT NOT NULL, model TEXT,"
            "reproducible INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO failure_clusters "
            "(cluster_key, kb_id, failure_stage, root_cause_feature, pool, status, priority, "
            "member_count, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("kb1|retrieval|recall_miss", "kb1", "retrieval", "recall_miss",
             ObservationPool.CANDIDATE_POOL.value, EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
             "normal", 0, now, now, now, now),
        )
        conn.execute(
            "INSERT INTO diagnosis_drafts (cluster_id, payload, prompt_version, model, reproducible, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (1, '{"root_cause":"legacy"}', "diagnosis-v1", "legacy-model", 1, now),
        )

    store = FailureTraceStore(db_path)

    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(diagnosis_drafts)")}
    assert "observation_id" in columns
    drafts = store.list_drafts()
    assert len(drafts) == 1
    assert drafts[0]["observation_id"] is None  # the legacy row is not a member draft
    assert drafts[0]["payload"]["root_cause"] == "legacy"
    assert drafts[0]["cluster_id"] == 1
    store.close()


def test_observations_awaiting_diagnosis_returns_undrafted_members_of_awaiting_clusters(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster, members = make_awaiting_cluster_with_members(
        store, key="awaiting-1", questions=["q-a1", "q-a2"]
    )

    result = store.observations_awaiting_diagnosis_without_draft(limit=10)

    assert [item["observation"]["id"] for item in result] == [members[0]["id"], members[1]["id"]]
    for item in result:
        assert item["cluster"]["id"] == cluster["id"]
        assert item["cluster"]["status"] == EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value
        assert item["observation"]["question"].startswith("q-a")


def test_observations_awaiting_diagnosis_excludes_drafted_members_only(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster, members = make_awaiting_cluster_with_members(
        store, key="awaiting-1", questions=["q-a1", "q-a2"]
    )
    store.add_draft(
        cluster["id"], sample_draft(), observation_id=members[0]["id"],
        prompt_version="v1", model="m",
    )

    result = store.observations_awaiting_diagnosis_without_draft(limit=10)

    # The drafted sibling is skipped; its cluster-mate still awaits a member draft.
    assert [item["observation"]["id"] for item in result] == [members[1]["id"]]


def test_observations_awaiting_diagnosis_ignores_non_awaiting_clusters(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    still_clustered = make_cluster(store, key="clustered-1", status=EvolutionStatus.CLUSTERED.value)
    store.add_observation(
        FailureObservation(kb_id="kb1", failure_stage="retrieval", question="q", route="rag")
    )
    store.attach_cluster_member(
        store.list_observations()[-1]["id"], still_clustered["id"],
        failure_stage="retrieval", root_cause_feature="recall_miss", now=time.time(),
    )
    _, members = make_awaiting_cluster_with_members(store, key="awaiting-1", questions=["q-a1"])

    result = store.observations_awaiting_diagnosis_without_draft(limit=10)

    assert [item["observation"]["id"] for item in result] == [members[0]["id"]]


def test_legacy_cluster_draft_does_not_block_member_drafts(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster, members = make_awaiting_cluster_with_members(store, key="awaiting-1", questions=["q-a1"])
    # A legacy cluster-level draft (observation_id NULL) must not suppress
    # the per-member draft the worker now owes this observation.
    store.add_draft(cluster["id"], sample_draft(), prompt_version="v1", model="m")

    result = store.observations_awaiting_diagnosis_without_draft(limit=10)

    assert [item["observation"]["id"] for item in result] == [members[0]["id"]]


def test_observations_awaiting_diagnosis_includes_members_of_approved_clusters(tmp_path):
    """已批准聚类里还没草案的成员仍然欠一份草案。

    候选是**按观测**生成的，生成器要求该观测自己有一份草案。所以"聚类已批准"不能
    停止 worker 继续给欠草案的成员出草案——否则这些成员永远产不出候选（配件那个聚类
    就是这样：26 个成员只有 1 份草案，其余 25 个再也不会被诊断）。
    """

    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster, members = make_awaiting_cluster_with_members(
        store, key="approved-1", questions=["q-a1", "q-a2"]
    )
    store.add_draft(
        cluster["id"],
        sample_draft(),
        observation_id=members[0]["id"],
        prompt_version="v1",
        model="m",
    )
    approved = store.decide_cluster(
        cluster["id"],
        new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=time.time(),
    )
    assert approved["status"] == EvolutionStatus.APPROVED_FOR_CANDIDATE.value

    result = store.observations_awaiting_diagnosis_without_draft(limit=10)

    # The already-drafted member is still skipped; its undrafted sibling is now owed one.
    assert [item["observation"]["id"] for item in result] == [members[1]["id"]]
    assert result[0]["cluster"]["status"] == EvolutionStatus.APPROVED_FOR_CANDIDATE.value


def test_observations_awaiting_diagnosis_respects_the_limit(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    _, members = make_awaiting_cluster_with_members(
        store, key="awaiting-1", questions=["q-a1", "q-a2", "q-a3"]
    )

    result = store.observations_awaiting_diagnosis_without_draft(limit=2)

    assert [item["observation"]["id"] for item in result] == [members[0]["id"], members[1]["id"]]


def test_a_gap_ticket_round_trips_and_never_touches_the_candidate_tables(tmp_path):
    """工单存自己的表：候选池的不变量（undetermined 不离开观察队列）不受它影响。"""
    import time

    from rag.evolution.gaps import build_ticket
    from rag.evolution.models import FailureObservation

    store = FailureTraceStore(tmp_path / "e.db")
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key="kb1|generation|gap", kb_id="kb1", failure_stage="generation",
        root_cause_feature="undetermined", seen_at=now,
    )
    row = store.add_observation(
        FailureObservation(kb_id="kb1", failure_stage="generation", question="R50 电池卖多少钱",
                           route="fixed", answerability_kind="insufficient_evidence")
    )
    store.attach_cluster_member(row["id"], cluster["id"], failure_stage="generation",
                                root_cause_feature="undetermined", now=now)
    ticket = build_ticket(cluster, [store.get_observation(row["id"])], missing_terms=("电池",), now=now)

    ticket_id = store.add_gap_ticket(ticket)

    assert ticket_id > 0
    stored = store.list_gap_tickets(cluster_id=cluster["id"])
    assert len(stored) == 1
    assert stored[0]["payload"]["missing_terms"] == ["电池"]
    assert stored[0]["status"] == "open"
    assert store.gap_ticket_cluster_ids() == {cluster["id"]}
    # 不变量：聚类仍在观察队列，没有草稿、没有候选
    assert store.list_clusters()[0]["pool"] == "observation_queue"
    assert store.list_drafts() == []
    store.close()
