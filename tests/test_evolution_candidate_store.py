"""Task 1: the candidate store, its state machine, and its audit trail.

``candidates`` is the phase-4 sibling of ``diagnosis_drafts``: it persists a
question-set candidate, records the five gate layers in ``gate_log``, and moves
the candidate only through ``EVOLUTION_TRANSITIONS``. Every advance writes its
audit row first and then the new status, in one transaction, so a rejected
transition leaves neither an audit row nor a gate entry behind.
"""

import sqlite3
import time

import pytest

from rag.evolution.candidate_store import CandidateStore
from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.models import (
    EvolutionStatus,
    FailureObservation,
    ObservationPool,
)
from rag.evolution.store import FailureTraceStore
from rag.jev.questions import PREFLIGHT_QUESTION_SET_VERSION, build_preflight_questions


def trace_store(tmp_path) -> FailureTraceStore:
    return FailureTraceStore(tmp_path / "evolution.db")


def make_approved_cluster(store: FailureTraceStore, key: str = "approved-1"):
    """One cluster promoted to ``approved_for_candidate`` with one member observation.

    Only existing store methods are used, so this mirrors what the clusterer and
    the audited admin decision produce in production.
    """
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key=key,
        kb_id="kb1",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    member = store.add_observation(
        FailureObservation(
            kb_id="kb1",
            failure_stage="retrieval",
            question="年假额度怎么算",
            route="rag",
            chunk_hashes=("a" * 64,),
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
    admitted = store.promote_cluster(
        cluster["id"],
        new_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.CLUSTERED.value,
        now=now,
    )
    decided = store.decide_cluster(
        admitted["id"],
        new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool=ObservationPool.CANDIDATE_POOL.value,
        member_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        now=now,
    )
    return decided, member


def sample_candidate(**kw) -> JevQuestionSetCandidate:
    """A candidate over the current preflight question set with one small edit."""
    base = build_preflight_questions()
    payload = {
        "intent": {"instructions": "判断用户问题的主要意图；只依据问题本身和提供的知识库能力摘要。"},
        "complexity": {},
        "needs_query_rewrite": {},
    }
    data = dict(
        artifact_type="jev_question_set",
        target="preflight",
        payload=payload,
        rationale="intent 判据在结构化查询边界与线上分歧集中",
        expected_effect="结构化查询意图判定更稳定",
        rollback_ref={
            "question_set_version": PREFLIGHT_QUESTION_SET_VERSION,
            "payload_sha256": "b" * 64,
        },
        confidence="medium",
    )
    data.update(kw)
    candidate = JevQuestionSetCandidate(**data)
    assert all(question_id in base for question_id in payload)
    return candidate


def seeded(tmp_path):
    """``(trace_store, candidate_store, cluster, member)`` ready for writes."""
    trace = trace_store(tmp_path)
    cluster, member = make_approved_cluster(trace)
    return trace, CandidateStore(trace), cluster, member


def table_columns(store: CandidateStore, table: str) -> set[str]:
    return {row["name"] for row in store._conn.execute(f"PRAGMA table_info({table})")}


# ------------------------------------------------------------------
# Writing and reading candidates.
# ------------------------------------------------------------------


def test_add_candidate_persists_the_contract_and_the_scope_gate(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate = sample_candidate()

    candidate_id = store.add_candidate(
        member["id"],
        cluster["id"],
        candidate,
        prompt_version="candidate-v1",
        model="deepseek/deepseek-v4.1-flash",
    )

    assert isinstance(candidate_id, int)
    stored = store.get_candidate(candidate_id)
    assert stored is not None
    assert stored["observation_id"] == member["id"]
    assert stored["cluster_id"] == cluster["id"]
    assert stored["artifact_type"] == "jev_question_set"
    assert stored["target"] == "preflight"
    assert stored["status"] == EvolutionStatus.CANDIDATE_TESTING.value
    assert stored["pool"] == ObservationPool.CANDIDATE_POOL.value
    assert stored["payload"] == candidate.payload
    assert stored["rollback_ref"] == candidate.rollback_ref
    assert stored["rationale"] == candidate.rationale
    assert stored["expected_effect"] == candidate.expected_effect
    assert stored["confidence"] == "medium"
    assert stored["prompt_version"] == "candidate-v1"
    assert stored["model"] == "deepseek/deepseek-v4.1-flash"
    assert stored["created_at"] > 0 and stored["updated_at"] > 0

    scope_gate = stored["gate_log"][0]
    assert scope_gate["gate"] == "scope"
    assert scope_gate["result"] == "accepted"
    assert scope_gate["detail"] == "jev_question_set/preflight questions=3"
    assert scope_gate["created_at"] > 0
    assert len(stored["gate_log"]) == 1


def test_add_candidate_writes_a_generate_audit_row(tmp_path):
    _, store, cluster, member = seeded(tmp_path)

    candidate_id = store.add_candidate(
        member["id"], cluster["id"], sample_candidate(), model="deepseek/deepseek-v4.1-flash"
    )

    rows = store.list_audit(candidate_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "generate"
    assert rows[0]["actor_id"] == "deepseek/deepseek-v4.1-flash"
    assert rows[0]["candidate_id"] == candidate_id
    assert rows[0]["created_at"] > 0


def test_add_candidate_rejects_unknown_observation_and_cluster(tmp_path):
    trace, store, cluster, member = seeded(tmp_path)
    with pytest.raises(KeyError):
        store.add_candidate(9999, cluster["id"], sample_candidate())
    with pytest.raises(KeyError):
        store.add_candidate(member["id"], 9999, sample_candidate())
    assert store.list_candidates() == []


def test_add_candidate_defaults_prompt_version_and_model(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    stored = store.get_candidate(candidate_id)
    assert stored["prompt_version"] == ""
    assert stored["model"] == ""
    assert store.list_audit(candidate_id)[0]["actor_id"] == "generator"


def test_get_candidate_returns_none_for_an_unknown_id(tmp_path):
    _, store, _, _ = seeded(tmp_path)
    assert store.get_candidate(4242) is None


def test_list_candidates_filters_by_status_and_limit(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    first = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    second = store.add_candidate(member["id"], cluster["id"], sample_candidate(confidence="low"))
    store.advance_candidate(first, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="worker", note="层2通过")

    assert [row["id"] for row in store.list_candidates()] == [second, first]
    assert [row["id"] for row in store.list_candidates(limit=1)] == [second]
    assert [row["id"] for row in store.list_candidates(status=EvolutionStatus.CANDIDATE_TESTING.value)] == [second]
    assert [
        row["id"] for row in store.list_candidates(status=EvolutionStatus.AWAITING_VERIFICATION.value)
    ] == [first]
    assert store.list_candidates(status=EvolutionStatus.STABLE.value) == []


def test_candidates_for_observation_scopes_to_one_observation(tmp_path):
    trace, store, cluster, member = seeded(tmp_path)
    other_member = trace.add_observation(
        FailureObservation(kb_id="kb1", failure_stage="retrieval", question="第二个问题", route="rag")
    )
    first = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    second = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    store.add_candidate(other_member["id"], cluster["id"], sample_candidate())
    cluster_level = store.add_candidate(None, cluster["id"], sample_candidate())

    assert [row["id"] for row in store.candidates_for_observation(member["id"])] == [first, second]
    assert [row["id"] for row in store.candidates_for_observation(other_member["id"])] != []
    assert store.candidates_for_observation(cluster_level) == []


# ------------------------------------------------------------------
# State machine, gate log, and audit.
# ------------------------------------------------------------------


def test_advance_candidate_moves_through_the_transition_table(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], sample_candidate())

    advanced = store.advance_candidate(
        candidate_id,
        EvolutionStatus.AWAITING_VERIFICATION.value,
        actor_id="worker",
        note="层2 基准: 20/20 用例路由一致",
        gate="benchmark",
        gate_detail="routing accuracy delta +0pp, top5 delta -0.4pp",
    )

    assert advanced["status"] == EvolutionStatus.AWAITING_VERIFICATION.value
    gate_log = advanced["gate_log"]
    assert [entry["gate"] for entry in gate_log] == ["scope", "benchmark"]
    assert gate_log[1]["result"] == "passed"
    assert gate_log[1]["detail"] == "routing accuracy delta +0pp, top5 delta -0.4pp"
    assert gate_log[1]["created_at"] >= gate_log[0]["created_at"]

    audit_rows = store.list_audit(candidate_id)
    assert [row["action"] for row in audit_rows] == ["generate", "advance"]
    assert audit_rows[1]["actor_id"] == "worker"
    assert audit_rows[1]["note"] == "层2 基准: 20/20 用例路由一致"
    assert audit_rows[1]["created_at"] <= advanced["updated_at"]


def test_advance_candidate_records_rejections_rollbacks_and_canary(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    rejected = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    rolled = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    canaried = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    accepted = store.add_candidate(member["id"], cluster["id"], sample_candidate())

    store.advance_candidate(
        rejected, EvolutionStatus.REJECTED.value, actor_id="admin", note="层2 命中回归",
        gate="benchmark", gate_detail="routing accuracy -3pp",
    )
    store.advance_candidate(rolled, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="worker", note="ok")
    store.advance_candidate(rolled, EvolutionStatus.APPROVED_FOR_CANARY.value, actor_id="admin", note="ok")
    store.advance_candidate(rolled, EvolutionStatus.CANARY.value, actor_id="admin", note="灰度 5%")
    store.advance_candidate(rolled, EvolutionStatus.ROLLED_BACK.value, actor_id="admin", note="已回滚")
    store.advance_candidate(canaried, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="worker", note="ok")
    store.advance_candidate(
        canaried, EvolutionStatus.APPROVED_FOR_CANARY.value, actor_id="admin", note="批准灰度", gate="admin"
    )
    store.advance_candidate(canaried, EvolutionStatus.CANARY.value, actor_id="admin", note="灰度 5%")
    store.advance_candidate(accepted, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="worker", note="ok")
    store.advance_candidate(accepted, EvolutionStatus.APPROVED_FOR_CANARY.value, actor_id="admin", note="ok")
    store.advance_candidate(accepted, EvolutionStatus.CANARY.value, actor_id="admin", note="灰度 100%")
    store.advance_candidate(accepted, EvolutionStatus.STABLE.value, actor_id="admin", note="观察期通过")

    assert [row["action"] for row in store.list_audit(rejected)] == ["generate", "reject"]
    assert [row["action"] for row in store.list_audit(rolled)] == [
        "generate",
        "advance",
        "advance",
        "canary",
        "rollback",
    ]
    assert [row["action"] for row in store.list_audit(canaried)] == [
        "generate",
        "advance",
        "advance",
        "canary",
    ]
    assert [row["action"] for row in store.list_audit(accepted)] == [
        "generate",
        "advance",
        "advance",
        "canary",
        "accept",
    ]
    failed_gate = store.get_candidate(rejected)["gate_log"][1]
    assert failed_gate["result"] == "failed"
    assert failed_gate["gate"] == "benchmark"


def test_advance_candidate_appends_no_gate_entry_without_a_gate_name(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], sample_candidate())

    store.advance_candidate(
        candidate_id, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="worker", note="层2通过"
    )

    assert len(store.get_candidate(candidate_id)["gate_log"]) == 1
    assert store.get_candidate(candidate_id)["status"] == EvolutionStatus.AWAITING_VERIFICATION.value


def test_illegal_transition_raises_and_writes_nothing(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    before = store.get_candidate(candidate_id)

    for illegal in (
        EvolutionStatus.STABLE.value,
        EvolutionStatus.CANARY.value,
        EvolutionStatus.APPROVED_FOR_CANARY.value,
        EvolutionStatus.OBSERVED.value,
    ):
        with pytest.raises(ValueError, match="invalid transition"):
            store.advance_candidate(candidate_id, illegal, actor_id="worker", note="越级")

    after = store.get_candidate(candidate_id)
    assert after["status"] == before["status"] == EvolutionStatus.CANDIDATE_TESTING.value
    assert after["gate_log"] == before["gate_log"]
    assert [row["action"] for row in store.list_audit(candidate_id)] == ["generate"]


def test_advance_candidate_on_an_unknown_id_raises_key_error(tmp_path):
    _, store, _, _ = seeded(tmp_path)
    with pytest.raises(KeyError):
        store.advance_candidate(4242, EvolutionStatus.REJECTED.value, actor_id="admin", note="不存在")


def test_advance_candidate_rejects_a_terminal_state_transition(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    store.advance_candidate(candidate_id, EvolutionStatus.REJECTED.value, actor_id="admin", note="拒绝")

    with pytest.raises(ValueError, match="invalid transition"):
        store.advance_candidate(candidate_id, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="worker", note="复活")


def test_audit_action_whitelist_is_enforced(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    candidate_id = store.add_candidate(member["id"], cluster["id"], sample_candidate())

    for action in ("generate", "advance", "reject", "canary", "rollback", "accept"):
        assert store.add_audit("worker", action, candidate_id, f"白名单 {action}") > 0

    before = len(store.list_audit(candidate_id))
    for illegal in ("delete", "APPROVE", "", "approve_for_candidate", "advance "):
        with pytest.raises(ValueError, match="invalid audit action"):
            store.add_audit("worker", illegal, candidate_id, "非法")

    assert len(store.list_audit(candidate_id)) == before


def test_list_audit_is_chronological_and_candidate_scoped(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    first = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    second = store.add_candidate(member["id"], cluster["id"], sample_candidate())

    assert [row["candidate_id"] for row in store.list_audit()] == [first, second]
    assert [row["candidate_id"] for row in store.list_audit(first)] == [first]
    assert store.list_audit(4242) == []


def test_candidates_without_gate_result_skips_gated_candidates(tmp_path):
    _, store, cluster, member = seeded(tmp_path)
    gated = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    pending_one = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    pending_two = store.add_candidate(member["id"], cluster["id"], sample_candidate())
    verified = store.add_candidate(member["id"], cluster["id"], sample_candidate())

    store.advance_candidate(
        gated,
        EvolutionStatus.AWAITING_VERIFICATION.value,
        actor_id="worker",
        note="层2通过",
        gate="benchmark",
    )
    store.advance_candidate(verified, EvolutionStatus.REJECTED.value, actor_id="admin", note="层2回归", gate="benchmark")

    remaining = store.candidates_without_gate_result("benchmark", limit=10)
    assert [row["id"] for row in remaining] == [pending_one, pending_two]
    assert [row["id"] for row in store.candidates_without_gate_result("benchmark", limit=1)] == [pending_one]
    # A gate nothing has run yet returns every candidate still in testing.
    assert len(store.candidates_without_gate_result("shadow", limit=10)) == 2


# ------------------------------------------------------------------
# Connection reuse, DDL, and migration.
# ------------------------------------------------------------------


def test_candidate_store_reuses_the_trace_store_connection_and_lock(tmp_path):
    trace = trace_store(tmp_path)
    store = CandidateStore(trace)

    assert store._conn is trace._conn
    assert store._lock is trace._lock
    # A close must never take the shared connection down with it.
    store.close()
    cluster, _ = make_approved_cluster(trace)
    assert cluster["status"] == EvolutionStatus.APPROVED_FOR_CANDIDATE.value


def test_candidate_store_creates_its_own_connection_without_a_trace_store(tmp_path):
    db_path = tmp_path / "evolution.db"
    trace = trace_store(tmp_path)
    cluster, member = make_approved_cluster(trace)
    trace_connection = trace._conn
    trace.close()

    store = CandidateStore(db_path=db_path)

    assert store._conn is not trace_connection
    assert store._lock is not trace._lock
    candidate_id = store.add_candidate(
        member["id"], cluster["id"], sample_candidate(), prompt_version="candidate-v1", model="m"
    )
    assert store.get_candidate(candidate_id)["status"] == EvolutionStatus.CANDIDATE_TESTING.value
    store.close()
    reopened = CandidateStore(db_path=db_path)
    assert reopened.get_candidate(candidate_id)["status"] == EvolutionStatus.CANDIDATE_TESTING.value


def test_candidate_table_has_the_documented_columns_and_indexes(tmp_path):
    trace = trace_store(tmp_path)
    store = CandidateStore(trace)

    assert table_columns(store, "candidates") == {
        "id",
        "observation_id",
        "cluster_id",
        "artifact_type",
        "target",
        "payload",
        "status",
        "pool",
        "gate_log",
        "prompt_version",
        "model",
        "rollback_ref",
        "rationale",
        "expected_effect",
        "confidence",
        "diff",
        "layer",
        "test_paths",
        "created_at",
        "updated_at",
    }
    indexes = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'candidates'"
        )
    }
    assert {"idx_candidates_cluster", "idx_candidates_status", "idx_candidates_observation"} <= indexes


def test_old_shape_candidates_table_gains_the_missing_columns(tmp_path):
    """Migration: a pre-phase-4 ``candidates`` table opens and is altered in place.

    The database keeps its phase-3 tables (a candidate still points at a real
    cluster); only the candidate table is replaced by an old-shape one that
    lacks the newer columns.
    """
    db_path = tmp_path / "evolution.db"
    trace = trace_store(tmp_path)
    cluster, _ = make_approved_cluster(trace)
    trace.close()
    now = time.time()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE IF EXISTS candidates")
        conn.execute(
            "CREATE TABLE candidates ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, cluster_id INTEGER NOT NULL, "
            "artifact_type TEXT NOT NULL, target TEXT NOT NULL, payload JSON NOT NULL, "
            "status TEXT NOT NULL, pool TEXT NOT NULL, rationale TEXT NOT NULL, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO candidates "
            "(cluster_id, artifact_type, target, payload, status, pool, rationale, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cluster["id"],
                "jev_question_set",
                "preflight",
                '{"intent": {}}',
                "candidate_testing",
                "candidate_pool",
                "旧格式候选",
                now,
                now,
            ),
        )

    store = CandidateStore(db_path=db_path)

    stored = store.get_candidate(1)
    assert stored is not None
    assert stored["gate_log"] == []
    assert stored["rollback_ref"] == {}
    assert stored["observation_id"] is None
    assert stored["confidence"] == "low"
    assert stored["rationale"] == "旧格式候选"
    assert store.add_candidate(None, cluster["id"], sample_candidate()) == 2


# ------------------------------------------------------------------
# Phase 5: code-patch candidates share the store and the state machine.
# ------------------------------------------------------------------


def sample_code_patch(**kw):
    """A minimal reranking-layer patch over the current repository state."""
    from rag.evolution.code_patches import CodePatchCandidate

    data = dict(
        artifact_type="code_patch",
        layer="reranking",
        paths=("rag/reranker.py",),
        diff=(
            "--- a/rag/reranker.py\n"
            "+++ b/rag/reranker.py\n"
            "@@ -1,1 +1,2 @@\n"
            " # reranker\n"
            "+# tightened filename collision penalty\n"
        ),
        test_paths=("tests/test_reranker.py",),
        rationale="词面撞名淹没候选，重排缺少文件名惩罚",
        expected_effect="无关文档不再凭借文件名相似度挤占前列",
        rollback_ref={"base_commit": "a" * 40},
        confidence="low",
    )
    data.update(kw)
    return CodePatchCandidate(**data)


def test_a_code_patch_candidate_round_trips_through_the_store(tmp_path):
    trace, store, cluster, member = seeded(tmp_path)

    candidate_id = store.add_candidate(
        member["id"], cluster["id"], sample_code_patch(), prompt_version="patch-v1", model="m"
    )
    row = store.get_candidate(candidate_id)

    assert row["artifact_type"] == "code_patch"
    assert row["layer"] == "reranking"
    assert row["target"] is None
    assert row["payload"] is None
    assert row["diff"].startswith("--- a/rag/reranker.py")
    assert row["test_paths"] == ["tests/test_reranker.py"]
    assert store.list_candidates()[0]["rollback_ref"]["base_commit"] == "a" * 40


def test_a_phase4_database_upgrades_to_the_code_patch_schema(tmp_path):
    """A pre-phase-5 table (CHECK-constrained, no patch columns) is rebuilt in place."""
    import json

    db_path = tmp_path / "evolution.db"
    trace = FailureTraceStore(db_path)
    cluster, member = make_approved_cluster(trace)
    old_candidate = sample_candidate()
    now = time.time()
    with trace._lock:
        trace._conn.execute(
            "CREATE TABLE candidates ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER, cluster_id INTEGER, "
            "artifact_type TEXT NOT NULL CHECK(artifact_type IN ('jev_question_set')), "
            "target TEXT NOT NULL CHECK(target IN ('preflight','passages')), "
            "payload JSON NOT NULL, status TEXT NOT NULL, pool TEXT NOT NULL, "
            "gate_log JSON NOT NULL DEFAULT '[]', prompt_version TEXT, model TEXT, "
            "rollback_ref JSON NOT NULL, rationale TEXT NOT NULL, expected_effect TEXT NOT NULL, "
            "confidence TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        trace._conn.execute(
            "INSERT INTO candidates (observation_id, cluster_id, artifact_type, target, payload, "
            "status, pool, gate_log, prompt_version, model, rollback_ref, rationale, "
            "expected_effect, confidence, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                member["id"], cluster["id"], "jev_question_set", "preflight",
                json.dumps(old_candidate.payload, ensure_ascii=False),
                "candidate_testing", "candidate_pool", "[]", "candidate-v1", "m",
                json.dumps(old_candidate.rollback_ref, ensure_ascii=False),
                old_candidate.rationale, old_candidate.expected_effect, "medium", now, now,
            ),
        )
        trace._conn.commit()
    trace.close()

    reopened = FailureTraceStore(db_path)
    store = CandidateStore(reopened)  # opening runs the migration
    rows = store.list_candidates()
    assert [row["artifact_type"] for row in rows] == ["jev_question_set"]
    assert rows[0]["target"] == "preflight"
    assert rows[0]["payload"] == old_candidate.payload

    patch_id = store.add_candidate(member["id"], cluster["id"], sample_code_patch())
    assert store.get_candidate(patch_id)["layer"] == "reranking"
    store.close()


def test_the_benchmark_gate_scan_skips_code_patch_candidates(tmp_path):
    """Layer 2 belongs to question sets; the sandbox gate owns code patches."""
    trace, store, cluster, member = seeded(tmp_path)
    store.add_candidate(member["id"], cluster["id"], sample_candidate())
    store.add_candidate(member["id"], cluster["id"], sample_code_patch())

    rows = store.candidates_without_gate_result("benchmark", limit=10)

    assert [row["artifact_type"] for row in rows] == ["jev_question_set"]
