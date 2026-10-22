"""Task 5 (layers 4-5): the candidate endpoints, the percentage rollout, and the split.

Layer 4 opens the online path to a question-set candidate **only** through an
audited administrator action: ``POST /candidates/{id}/canary`` registers the
candidate question set, advances the candidate through
``awaiting_verification → approved_for_canary → canary`` and writes the live
percentage into the rollout controller; ``POST /candidates/{id}/rollback``
unregisters it, zeroes the percentage and moves the candidate to ``rolled_back``
inside one step. The two reads expose the stored signals and the five-layer gate
trail, never the question text a payload carries.

The controller splits by request key with a stable hash, so one request always
lands in one bucket (log attribution) and a percentage of zero never registers
anything (the online path is the current question set). The pipeline tests build
a real ``RAGPipeline`` around an injected Jev service to prove the seam: with a
rollout injected the candidate instructions reach the SDK, without one the
static set does.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from rag.auth import create_token
from rag.evolution import api as evolution_api
from rag.evolution.candidate_store import CandidateStore
from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.models import EvolutionStatus, FailureObservation
from rag.evolution.rollout import (
    CandidateRollout,
    bucket,
    candidate_question_set_version,
    rollout,
)
from rag.evolution.store import FailureTraceStore
from rag.jev.client import JevClient
from rag.jev.questions import (
    PREFLIGHT_QUESTION_SET_VERSION,
    build_preflight_questions,
    question_set_registry,
)
from rag.jev.service import JevDecisionService
from rag.pipeline import RAGPipeline
from rag.user_db import UserDB

PREFIX = "/knowledge-operations/evolution"
CANDIDATES_URL = f"{PREFIX}/candidates"
CANDIDATE_INTENT_INSTRUCTIONS = "判断用户问题的主要意图；只依据问题本身与知识库能力摘要。"


# ------------------------------------------------------------------
# Fixtures: the same private app the phase-3 API tests use.
# ------------------------------------------------------------------


@pytest.fixture
def evolution_client(tmp_path, monkeypatch):
    """A private app that mounts only the evolution router."""
    db = UserDB(str(tmp_path / "users.db"))
    app = FastAPI()
    app.state.user_db = db
    app.include_router(evolution_api.router)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "evolution_db_path", str(tmp_path / "evolution.db"))
    monkeypatch.setattr(settings, "evolution_candidate_canary_percent", 100)
    with TestClient(app) as client:
        yield client
    evolution_api.close_store()
    db.close()


@pytest.fixture(autouse=True)
def isolated_rollout():
    """The controller is a process-wide singleton: every test starts and ends clean."""
    for entry in rollout.snapshot():
        rollout.deactivate(entry.candidate_id)
    yield
    for entry in rollout.snapshot():
        rollout.deactivate(entry.candidate_id)


@pytest.fixture
def user_headers(evolution_client) -> dict:
    db = evolution_client.app.state.user_db
    user_id = db.create_user("alice", "Password-123!")
    return _token(db, user_id)


@pytest.fixture
def admin_headers(evolution_client) -> dict:
    db = evolution_client.app.state.user_db
    user_id = db.create_user("admin", "Password-123!")
    db.set_user_admin(user_id, True)
    return _token(db, user_id)


def _token(db: UserDB, user_id: int) -> dict:
    row = db.get_user_by_id(user_id)
    token = create_token({"user_id": user_id, "username": row["username"], "security_version": 1})
    return {"Authorization": f"Bearer {token}"}


def _admin_id(client: TestClient) -> str:
    row = client.app.state.user_db.get_user_by_username("admin")
    return str(row["id"])


def sample_payload() -> dict:
    return {
        "intent": {"instructions": CANDIDATE_INTENT_INSTRUCTIONS},
        "complexity": {},
        "needs_query_rewrite": {},
    }


def sample_candidate(**kw) -> JevQuestionSetCandidate:
    payload = sample_payload()
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    data = dict(
        artifact_type="jev_question_set",
        target="preflight",
        payload=payload,
        rationale="intent 判据在结构化查询边界与线上分歧集中",
        expected_effect="结构化查询意图判定更稳定",
        rollback_ref={
            "question_set_version": PREFLIGHT_QUESTION_SET_VERSION,
            "payload_sha256": digest,
        },
        confidence="medium",
    )
    data.update(kw)
    return JevQuestionSetCandidate(**data)


def _approved_cluster(store: FailureTraceStore, key: str) -> tuple[int, int]:
    """One cluster approved for candidates, built with existing store methods only."""
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
        member["id"], cluster["id"], failure_stage="retrieval",
        root_cause_feature="recall_miss", now=now,
    )
    admitted = store.promote_cluster(
        cluster["id"], new_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        pool="candidate_pool", member_status=EvolutionStatus.CLUSTERED.value, now=now,
    )
    assert admitted is not None
    store.decide_cluster(
        admitted["id"], new_status=EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        pool="candidate_pool", member_status=EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        now=now,
    )
    return admitted["id"], member["id"]


def _add_candidate(store: FailureTraceStore, *, status: str | None = None, key: str) -> int:
    """Persist one candidate, optionally advanced past ``candidate_testing``.

    A candidate that has left ``candidate_testing`` carries the two measurements layer 4 requires
    (C-05): a passed layer-2 benchmark entry — written by the move itself — and a layer-3 shadow
    entry recorded as evidence. The canary endpoint reads those entries, so a fixture without them
    would be describing a candidate the endpoint must now refuse.
    """
    cluster_id, member_id = _approved_cluster(store, key)
    candidates = CandidateStore(store)
    candidate_id = candidates.add_candidate(
        member_id, cluster_id, sample_candidate(),
        prompt_version="candidate-v1", model="test-model",
    )
    if status is not None:
        candidates.advance_candidate(
            candidate_id, status, actor_id="gate-2", note="层 2/3 通过",
            gate="benchmark", gate_detail="路线准确率不降",
        )
        candidates.record_gate_entry(
            candidate_id,
            actor_id="gate-3",
            note="层 3 影子对比样本",
            gate="shadow",
            result="passed",
            gate_detail="影子对比 24 次，路由一致率 1.0",
            gate_metrics={"samples": 24},
        )
    return candidate_id


def _store() -> FailureTraceStore:
    """A separate writer handle on the test database (the API keeps its own)."""
    return FailureTraceStore(settings.evolution_db_path)


@pytest.fixture
def candidate_ready(evolution_client) -> int:
    """One candidate in ``awaiting_verification``: layers 1-3 concluded."""
    store = _store()
    try:
        return _add_candidate(
            store, status=EvolutionStatus.AWAITING_VERIFICATION.value, key="approved-one"
        )
    finally:
        store.close()


@pytest.fixture
def candidate_testing(evolution_client) -> int:
    """One fresh candidate still inside ``candidate_testing``."""
    store = _store()
    try:
        return _add_candidate(store, key="approved-one")
    finally:
        store.close()


@pytest.fixture
def second_candidate_ready(evolution_client, candidate_ready) -> int:
    """A second verified candidate, used for the one-live-rollout rule."""
    store = _store()
    try:
        return _add_candidate(
            store, status=EvolutionStatus.AWAITING_VERIFICATION.value, key="approved-two"
        )
    finally:
        store.close()


def _candidate_status(candidate_id: int) -> str:
    store = _store()
    try:
        return CandidateStore(store).get_candidate(candidate_id)["status"]
    finally:
        store.close()


def _candidate_audit(candidate_id: int) -> list[dict]:
    store = _store()
    try:
        return CandidateStore(store).list_audit(candidate_id)
    finally:
        store.close()


def _candidate_gate_log(candidate_id: int) -> list[dict]:
    store = _store()
    try:
        return CandidateStore(store).get_candidate(candidate_id)["gate_log"]
    finally:
        store.close()


def _canary(client: TestClient, candidate_id: int, percent: int, headers: dict):
    return client.post(
        f"{CANDIDATES_URL}/{candidate_id}/canary", json={"percent": percent}, headers=headers
    )


def _rollback(client: TestClient, candidate_id: int, headers: dict):
    return client.post(f"{CANDIDATES_URL}/{candidate_id}/rollback", headers=headers)


# ------------------------------------------------------------------
# The reads.
# ------------------------------------------------------------------


def test_candidates_reads_empty_without_creating_the_database(
    evolution_client, admin_headers, tmp_path
):
    db_file = tmp_path / "evolution.db"
    assert not db_file.exists()

    page = evolution_client.get(CANDIDATES_URL, headers=admin_headers).json()
    assert page == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert evolution_client.get(f"{CANDIDATES_URL}/1", headers=admin_headers).status_code == 404
    assert _canary(evolution_client, 1, 10, admin_headers).status_code == 404
    assert _rollback(evolution_client, 1, admin_headers).status_code == 404

    assert not db_file.exists()


def test_every_candidate_endpoint_is_administrator_only(
    evolution_client, user_headers, admin_headers, candidate_ready
):
    requests = (
        ("get", CANDIDATES_URL, None),
        ("get", f"{CANDIDATES_URL}/{candidate_ready}", None),
        ("post", f"{CANDIDATES_URL}/{candidate_ready}/canary", {"percent": 10}),
        ("post", f"{CANDIDATES_URL}/{candidate_ready}/rollback", None),
    )
    for method, url, body in requests:
        response = getattr(evolution_client, method)(
            url, headers=user_headers, **({} if body is None else {"json": body})
        )
        assert response.status_code == 403, url
    assert evolution_client.get(f"{CANDIDATES_URL}/{candidate_ready}").status_code == 401
    assert evolution_client.get(CANDIDATES_URL, headers=admin_headers).status_code == 200


def test_candidates_list_filters_by_status_and_pages(
    evolution_client, admin_headers, candidate_ready
):
    page = evolution_client.get(CANDIDATES_URL, headers=admin_headers).json()
    assert page["total"] == 1
    item = page["items"][0]
    assert set(item) >= {
        "id", "observation_id", "cluster_id", "target", "status", "pool", "confidence",
        "prompt_version", "model", "rationale", "expected_effect", "payload_summary",
        "rollback_ref", "canary_percent", "created_at", "updated_at",
    }
    assert item["id"] == candidate_ready
    assert item["target"] == "preflight"
    assert item["status"] == "awaiting_verification"
    assert item["confidence"] == "medium"
    assert item["canary_percent"] == 0
    assert item["payload_summary"]["question_count"] == 3
    assert item["payload_summary"]["overridden"] == 1
    # The gate trail lives on the detail response, not on every row.
    assert "gate_log" not in item

    by_status = evolution_client.get(
        CANDIDATES_URL, headers=admin_headers, params={"status": "candidate_testing"}
    ).json()
    assert by_status["items"] == []
    assert evolution_client.get(
        CANDIDATES_URL, headers=admin_headers, params={"status": "not-a-status"}
    ).status_code == 400

    paged = evolution_client.get(
        CANDIDATES_URL, headers=admin_headers, params={"page": 2, "page_size": 1}
    ).json()
    assert paged == {"items": [], "total": 1, "page": 2, "page_size": 1}


def test_candidate_detail_reports_the_gate_trail_and_the_audit(
    evolution_client, admin_headers, candidate_ready
):
    detail = evolution_client.get(
        f"{CANDIDATES_URL}/{candidate_ready}", headers=admin_headers
    ).json()

    assert [entry["gate"] for entry in detail["gate_log"]] == ["scope", "benchmark", "shadow"]
    assert detail["gate_log"][1]["result"] == "passed"
    assert detail["gate_log"][1]["detail"] == "路线准确率不降"
    assert detail["gate_log"][2]["metrics"] == {"samples": 24}
    assert [row["action"] for row in detail["audit"]] == ["generate", "advance", "advance"]
    assert detail["audit"][0]["actor_id"] == "test-model"
    assert detail["rollback_ref"]["question_set_version"] == PREFLIGHT_QUESTION_SET_VERSION
    summary = detail["payload_summary"]
    assert [question["question_id"] for question in summary["questions"]] == [
        "complexity", "intent", "needs_query_rewrite",
    ]
    assert summary["questions"][1]["fields"] == ["instructions"]


def test_candidate_serialization_never_carries_the_payload_text(
    evolution_client, admin_headers, candidate_ready
):
    body = evolution_client.get(
        f"{CANDIDATES_URL}/{candidate_ready}", headers=admin_headers
    ).text
    assert CANDIDATE_INTENT_INSTRUCTIONS not in body
    assert CANDIDATE_INTENT_INSTRUCTIONS not in evolution_client.get(
        CANDIDATES_URL, headers=admin_headers
    ).text


def test_candidate_detail_on_an_unknown_candidate_is_not_found(
    evolution_client, admin_headers, candidate_ready
):
    assert evolution_client.get(f"{CANDIDATES_URL}/999", headers=admin_headers).status_code == 404


# ------------------------------------------------------------------
# Layer 4: the audited canary action.
# ------------------------------------------------------------------


def test_canary_registers_the_question_set_and_advances_the_candidate(
    evolution_client, admin_headers, candidate_ready
):
    response = _canary(evolution_client, candidate_ready, 30, admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "canary"
    assert body["canary_percent"] == 30
    version = candidate_question_set_version(PREFLIGHT_QUESTION_SET_VERSION, candidate_ready)
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION, version}
    assert question_set_registry()["preflight"][version](chunk_ids=None)["intent"].instructions == (
        CANDIDATE_INTENT_INSTRUCTIONS
    )
    assert rollout.percent_for(candidate_ready) == 30

    audit = _candidate_audit(candidate_ready)
    assert [row["action"] for row in audit] == [
        "generate", "advance", "advance", "advance", "canary",
    ]
    assert audit[-1]["actor_id"] == _admin_id(evolution_client)
    assert "canary_percent=30" in audit[-1]["note"]
    # 层 2（benchmark）与层 3（shadow）的结论都在轨迹里；canary 是这两条之后的动作。
    assert [entry["gate"] for entry in _candidate_gate_log(candidate_ready)] == [
        "scope", "benchmark", "shadow", "canary",
    ]
    assert _candidate_gate_log(candidate_ready)[-1]["detail"] == "percent=30"


def test_canary_rejects_a_percent_outside_the_unit_range(
    evolution_client, admin_headers, candidate_ready
):
    for percent in (-1, 101):
        assert _canary(evolution_client, candidate_ready, percent, admin_headers).status_code == 400

    assert _candidate_status(candidate_ready) == "awaiting_verification"
    assert rollout.percent_for(candidate_ready) == 0
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}
    assert [row["action"] for row in _candidate_audit(candidate_ready)] == [
        "generate", "advance", "advance",
    ]


def test_canary_refuses_a_percent_above_the_configured_ceiling(
    evolution_client, admin_headers, candidate_ready, monkeypatch
):
    monkeypatch.setattr(settings, "evolution_candidate_canary_percent", 10)
    response = _canary(evolution_client, candidate_ready, 30, admin_headers)

    assert response.status_code == 400
    assert "evolution_candidate_canary_percent" in response.json()["detail"]
    assert _candidate_status(candidate_ready) == "awaiting_verification"
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


def test_canary_on_an_unknown_candidate_is_not_found(evolution_client, admin_headers):
    assert _canary(evolution_client, 999, 10, admin_headers).status_code == 404


def test_canary_before_the_verification_layers_is_a_conflict(
    evolution_client, admin_headers, candidate_testing
):
    response = _canary(evolution_client, candidate_testing, 10, admin_headers)

    assert response.status_code == 409
    assert _candidate_status(candidate_testing) == "candidate_testing"
    assert [row["action"] for row in _candidate_audit(candidate_testing)] == ["generate"]
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


def test_canary_refuses_while_another_candidate_is_live(
    evolution_client, admin_headers, candidate_ready, second_candidate_ready
):
    assert _canary(evolution_client, candidate_ready, 10, admin_headers).status_code == 200

    response = _canary(evolution_client, second_candidate_ready, 10, admin_headers)

    assert response.status_code == 409
    assert "正在灰度中" in response.json()["detail"]
    assert _candidate_status(second_candidate_ready) == "awaiting_verification"
    assert rollout.percent_for(second_candidate_ready) == 0


def test_canary_twice_only_updates_the_percentage(
    evolution_client, admin_headers, candidate_ready
):
    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200
    second = _canary(evolution_client, candidate_ready, 60, admin_headers)

    assert second.status_code == 200
    assert second.json()["status"] == "canary"
    assert second.json()["canary_percent"] == 60
    assert rollout.percent_for(candidate_ready) == 60
    assert [row["action"] for row in _candidate_audit(candidate_ready)] == [
        "generate", "advance", "advance", "advance", "canary", "canary",
    ]
    assert "canary_percent=60" in _candidate_audit(candidate_ready)[-1]["note"]
    # A percentage change is not a gate layer: the trail keeps exactly one canary entry.
    assert [entry["gate"] for entry in _candidate_gate_log(candidate_ready)] == [
        "scope", "benchmark", "shadow", "canary",
    ]


# ------------------------------------------------------------------
# C-05 (2026-10-22): the canary endpoint reads evidence, not just status.
# ------------------------------------------------------------------


def _code_patch_row(store: FailureTraceStore, key: str) -> int:
    """One stored code-patch candidate, advanced past testing (a row of the other artifact type)."""
    from rag.evolution.code_patches import CodePatchCandidate

    cluster_id, member_id = _approved_cluster(store, key)
    candidates = CandidateStore(store)
    candidate_id = candidates.add_candidate(
        member_id,
        cluster_id,
        CodePatchCandidate(
            artifact_type="code_patch",
            layer="reranking",
            paths=("rag/reranker.py",),
            diff="--- a/rag/reranker.py\n+++ b/rag/reranker.py\n@@ -1 +1,2 @@\n x = 1\n+# probe\n",
            test_paths=("tests/test_reranker.py",),
            rationale="同表另一制品",
            expected_effect="无行为变化",
            rollback_ref={"base_commit": "0" * 40},
            confidence="low",
        ),
    )
    candidates.advance_candidate(
        candidate_id,
        EvolutionStatus.AWAITING_VERIFICATION.value,
        actor_id="gate",
        note="sandbox passed",
        gate="sandbox",
        gate_detail="tests passed",
    )
    return candidate_id


def test_canary_asserts_the_artifact_type_first(evolution_client, admin_headers):
    """The table serves two artifact types; the question-set canary must refuse the other one.

    A code patch has no percentage canary (there is no second instance to serve a share of
    traffic), so posting one here would register a ``target``/``payload`` pair that is NULL for
    that row — the exact shape the split exists to keep apart.
    """
    store = _store()
    try:
        patch_id = _code_patch_row(store, "code-patch-row")
    finally:
        store.close()

    response = _canary(evolution_client, patch_id, 10, admin_headers)

    assert response.status_code == 404
    assert _candidate_status(patch_id) == "awaiting_verification"
    assert rollout.percent_for(patch_id) == 0
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


def test_canary_refuses_a_candidate_whose_layer_two_never_passed(evolution_client, admin_headers):
    """``awaiting_verification`` is a claim; the benchmark entry is the evidence behind it."""
    store = _store()
    try:
        cluster_id, member_id = _approved_cluster(store, "no-benchmark")
        candidates = CandidateStore(store)
        candidate_id = candidates.add_candidate(member_id, cluster_id, sample_candidate())
        # 层 3 有样本，但层 2 没有结论：状态到了，证据没到。
        candidates.advance_candidate(
            candidate_id,
            EvolutionStatus.AWAITING_VERIFICATION.value,
            actor_id="gate",
            note="advanced without a benchmark conclusion",
        )
        candidates.record_gate_entry(
            candidate_id,
            actor_id="gate-3",
            note="shadow sampled",
            gate="shadow",
            result="passed",
            gate_detail="24 comparisons",
            gate_metrics={"samples": 24},
        )
    finally:
        store.close()

    response = _canary(evolution_client, candidate_id, 10, admin_headers)

    assert response.status_code == 409
    assert "层 2" in response.json()["detail"]
    assert _candidate_status(candidate_id) == "awaiting_verification"
    assert rollout.percent_for(candidate_id) == 0
    assert [row["action"] for row in _candidate_audit(candidate_id)].count("canary") == 0


def test_canary_refuses_a_candidate_with_no_shadow_samples(evolution_client, admin_headers):
    """No layer-3 comparison means the canary would be the first measurement, on live traffic."""
    store = _store()
    try:
        cluster_id, member_id = _approved_cluster(store, "no-shadow")
        candidates = CandidateStore(store)
        candidate_id = candidates.add_candidate(member_id, cluster_id, sample_candidate())
        candidates.advance_candidate(
            candidate_id,
            EvolutionStatus.AWAITING_VERIFICATION.value,
            actor_id="gate",
            note="benchmark passed",
            gate="benchmark",
            gate_detail="route delta +0.0pp",
        )
    finally:
        store.close()

    response = _canary(evolution_client, candidate_id, 10, admin_headers)

    assert response.status_code == 409
    assert "层 3" in response.json()["detail"]
    assert "0 次" in response.json()["detail"]
    assert _candidate_status(candidate_id) == "awaiting_verification"
    assert rollout.percent_for(candidate_id) == 0


def test_canary_refuses_a_shadow_entry_that_sampled_nothing(evolution_client, admin_headers):
    """An entry claiming ``samples=0`` is the same absence as no entry at all."""
    store = _store()
    try:
        cluster_id, member_id = _approved_cluster(store, "zero-shadow")
        candidates = CandidateStore(store)
        candidate_id = candidates.add_candidate(member_id, cluster_id, sample_candidate())
        candidates.advance_candidate(
            candidate_id,
            EvolutionStatus.AWAITING_VERIFICATION.value,
            actor_id="gate",
            note="benchmark passed",
            gate="benchmark",
            gate_detail="route delta +0.0pp",
        )
        candidates.record_gate_entry(
            candidate_id,
            actor_id="gate-3",
            note="shadow ran but sampled nothing",
            gate="shadow",
            result="passed",
            gate_detail="0 comparisons",
            gate_metrics={"samples": 0},
        )
    finally:
        store.close()

    assert _canary(evolution_client, candidate_id, 10, admin_headers).status_code == 409
    assert rollout.percent_for(candidate_id) == 0


def test_a_canary_write_failure_leaves_no_live_registration(
    evolution_client, admin_headers, candidate_ready, monkeypatch
):
    """The live registration happens before the status write, so a refused write leaves no canary.

    The old order wrote the status first and registered last: a failure between the two left a
    candidate recorded as "in canary" that served nothing, and nothing could tell the difference
    from a healthy one. Now the registration is compensated when the write is refused.
    """

    def refuse(self, *args, **kwargs):
        raise ValueError("store refused the move")

    monkeypatch.setattr(CandidateStore, "advance_candidate", refuse)

    response = _canary(evolution_client, candidate_ready, 30, admin_headers)

    assert response.status_code == 409
    assert "非法状态转移" in response.json()["detail"]
    assert rollout.percent_for(candidate_ready) == 0
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


# ------------------------------------------------------------------
# C-06 (2026-10-22): the configured ceiling is a kill switch, not a hint.
# ------------------------------------------------------------------


def test_restore_caps_a_persisted_canary_at_the_configured_ceiling(
    evolution_client, admin_headers, candidate_ready, monkeypatch
):
    """Lowering ``evolution_candidate_canary_percent`` reaches a canary that is already live.

    The endpoint already refused a share above the cap, but the reconstruction from the audit
    trail did not, so a lowered cap changed nothing until the next administrator action. The cap
    now applies on every reconcile — and the correction is written to the trail, so it is durable
    and visible rather than a silent traffic reduction.
    """
    assert _canary(evolution_client, candidate_ready, 60, admin_headers).status_code == 200
    rollout.deactivate(candidate_ready)  # a fresh process holds no live state
    monkeypatch.setattr(settings, "evolution_candidate_canary_percent", 20)

    store = _store()
    try:
        rollout.restore(CandidateStore(store))
        assert rollout.percent_for(candidate_ready) == 20
        version = candidate_question_set_version(PREFLIGHT_QUESTION_SET_VERSION, candidate_ready)
        assert version in question_set_registry()["preflight"]
        audit = _candidate_audit(candidate_ready)
        assert audit[-1]["action"] == "canary"
        assert "canary_percent=20" in audit[-1]["note"]
        assert "上限" in audit[-1]["note"]
        assert "60" in audit[-1]["note"]

        # Idempotent: the second reconcile reads the capped value and writes nothing more.
        recorded = len(audit)
        rollout.restore(CandidateStore(store))
        assert rollout.percent_for(candidate_ready) == 20
        assert len(_candidate_audit(candidate_ready)) == recorded
    finally:
        store.close()


def test_a_zero_ceiling_stops_serving_without_rewriting_the_trail(
    evolution_client, admin_headers, candidate_ready, monkeypatch
):
    """Ceiling zero is the incident setting: serve nothing, and keep what the admin asked for.

    Writing ``canary_percent=0`` into the trail would make the clamp permanent — raising the cap
    again would then restore nothing. The zero cap is a configuration state, so it is logged and
    applied to the live share only.
    """
    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200
    rollout.deactivate(candidate_ready)
    monkeypatch.setattr(settings, "evolution_candidate_canary_percent", 0)
    before = len(_candidate_audit(candidate_ready))

    page = evolution_client.get(CANDIDATES_URL, headers=admin_headers).json()  # reconcile via the API

    assert page["items"][0]["canary_percent"] == 0
    assert rollout.percent_for(candidate_ready) == 0
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}
    assert len(_candidate_audit(candidate_ready)) == before, "the zero cap must not rewrite the trail"


# ------------------------------------------------------------------
# Layer 5: the instant, audited rollback.
# ------------------------------------------------------------------


def test_rollback_unregisters_the_question_set_and_zeroes_the_percentage(
    evolution_client, admin_headers, candidate_ready
):
    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200

    response = _rollback(evolution_client, candidate_ready, admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rolled_back"
    assert body["canary_percent"] == 0
    assert rollout.percent_for(candidate_ready) == 0
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}
    audit = _candidate_audit(candidate_ready)
    assert [row["action"] for row in audit][-1] == "rollback"
    assert audit[-1]["actor_id"] == _admin_id(evolution_client)
    assert audit[-1]["note"].endswith("canary_percent=0")


def test_rollback_is_idempotent(evolution_client, admin_headers, candidate_ready):
    _canary(evolution_client, candidate_ready, 30, admin_headers)
    assert _rollback(evolution_client, candidate_ready, admin_headers).status_code == 200

    second = _rollback(evolution_client, candidate_ready, admin_headers)

    assert second.status_code == 200
    assert second.json()["status"] == "rolled_back"
    assert [row["action"] for row in _candidate_audit(candidate_ready)].count("rollback") == 1
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


def test_rollback_before_canary_is_a_conflict(evolution_client, admin_headers, candidate_testing):
    response = _rollback(evolution_client, candidate_testing, admin_headers)

    assert response.status_code == 409
    assert _candidate_status(candidate_testing) == "candidate_testing"
    assert [row["action"] for row in _candidate_audit(candidate_testing)] == ["generate"]


# ------------------------------------------------------------------
# The controller itself: bucketing, defaults and persistence.
# ------------------------------------------------------------------


def test_should_use_candidate_is_deterministic_and_bounded_by_the_percentage():
    controller = CandidateRollout()
    controller.activate(7, "preflight", sample_payload(), PREFLIGHT_QUESTION_SET_VERSION, 50)
    keys = [f"request-{index}" for index in range(200)]
    first = [controller.should_use_candidate(key) for key in keys]
    try:
        assert first == [controller.should_use_candidate(key) for key in keys]
        assert any(first) and not all(first)
        assert 60 <= sum(first) <= 140
        assert controller.should_use_candidate("") is False
    finally:
        controller.deactivate(7)

    assert not any(controller.should_use_candidate(key) for key in keys)


def test_the_request_key_scope_is_isolated_between_concurrent_requests():
    """Two in-flight requests must not overwrite each other's canary key."""
    keys = [f"request-{index}" for index in range(1000)]
    inside = next(key for key in keys if bucket(key) < 50)
    outside = next(key for key in keys if bucket(key) >= 50)
    controller = CandidateRollout()
    controller.activate(7, "preflight", sample_payload(), PREFLIGHT_QUESTION_SET_VERSION, 50)
    served: dict[str, str] = {}
    try:
        def serve(key: str) -> None:
            with controller.request_key(key):
                time.sleep(0.01)  # let the other thread publish its own key
                served[key] = controller.provide("preflight", None)[1]

        threads = [threading.Thread(target=serve, args=(key,)) for key in (inside, outside)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert served[inside] == candidate_question_set_version(PREFLIGHT_QUESTION_SET_VERSION, 7)
        assert served[outside] == PREFLIGHT_QUESTION_SET_VERSION
    finally:
        controller.deactivate(7)


def test_zero_percent_registers_nothing():
    controller = CandidateRollout()
    controller.activate(7, "preflight", sample_payload(), PREFLIGHT_QUESTION_SET_VERSION, 0)
    try:
        questions, version = controller.provide("preflight", None)
        assert version == PREFLIGHT_QUESTION_SET_VERSION
        assert questions["intent"].instructions == (
            build_preflight_questions()["intent"].instructions
        )
        assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}
    finally:
        controller.deactivate(7)


def test_the_provider_serves_the_overlay_only_inside_the_request_scope():
    controller = CandidateRollout()
    controller.activate(7, "preflight", sample_payload(), PREFLIGHT_QUESTION_SET_VERSION, 100)
    version = candidate_question_set_version(PREFLIGHT_QUESTION_SET_VERSION, 7)
    try:
        with controller.request_key("request-1"):
            questions, served_version = controller.provide("preflight", None)
        assert served_version == version
        assert questions["intent"].instructions == CANDIDATE_INTENT_INSTRUCTIONS

        # Outside the published key the online path stays on the current set.
        questions, served_version = controller.provide("preflight", None)
        assert served_version == PREFLIGHT_QUESTION_SET_VERSION
        assert questions["intent"].instructions != CANDIDATE_INTENT_INSTRUCTIONS
    finally:
        controller.deactivate(7)


def test_restore_rebuilds_a_persisted_canary_after_a_restart(
    evolution_client, admin_headers, candidate_ready
):
    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200
    rollout.deactivate(candidate_ready)  # a fresh process holds no live state
    assert rollout.percent_for(candidate_ready) == 0
    version = candidate_question_set_version(PREFLIGHT_QUESTION_SET_VERSION, candidate_ready)
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}

    store = _store()
    try:
        rollout.restore(CandidateStore(store))
    finally:
        store.close()

    assert rollout.percent_for(candidate_ready) == 30
    assert version in question_set_registry()["preflight"]


def test_restore_drops_a_rolled_back_candidate(evolution_client, admin_headers, candidate_ready):
    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200
    assert _rollback(evolution_client, candidate_ready, admin_headers).status_code == 200

    store = _store()
    try:
        rollout.restore(CandidateStore(store))
    finally:
        store.close()

    assert rollout.percent_for(candidate_ready) == 0
    assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}


# ------------------------------------------------------------------
# The startup hook: the API must restore through a CandidateStore.
# ------------------------------------------------------------------


def test_startup_restore_rebuilds_a_live_canary(monkeypatch, evolution_client, admin_headers, candidate_ready):
    """The lifespan hook restores through a ``CandidateStore`` over the trace store."""
    from rag.api import restore_question_set_rollout

    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200
    rollout.deactivate(candidate_ready)  # a fresh process holds no live state

    monkeypatch.setattr(settings, "evolution_candidate_enabled", True)
    try:
        restore_question_set_rollout()
        assert rollout.percent_for(candidate_ready) == 30
        version = candidate_question_set_version(PREFLIGHT_QUESTION_SET_VERSION, candidate_ready)
        assert version in question_set_registry()["preflight"]
    finally:
        rollout.deactivate(candidate_ready)


def test_startup_restore_is_a_noop_without_the_candidate_switch(
    monkeypatch, evolution_client, admin_headers, candidate_ready
):
    from rag.api import restore_question_set_rollout

    assert _canary(evolution_client, candidate_ready, 30, admin_headers).status_code == 200
    rollout.deactivate(candidate_ready)

    monkeypatch.setattr(settings, "evolution_candidate_enabled", False)
    try:
        restore_question_set_rollout()
        assert rollout.percent_for(candidate_ready) == 0
        assert set(question_set_registry()["preflight"]) == {PREFLIGHT_QUESTION_SET_VERSION}
    finally:
        rollout.deactivate(candidate_ready)


# ------------------------------------------------------------------
# The pipeline seam: an injected rollout serves the candidate set.
# ------------------------------------------------------------------


def preflight_answers() -> dict:
    return {
        "intent": SimpleNamespace(choice="retrieval_qa", confidence=0.94),
        "complexity": SimpleNamespace(choice="simple", confidence=0.91),
        "needs_query_rewrite": SimpleNamespace(noul=0.08),
    }


class RecordingTypesafeClient:
    def __init__(self):
        self.questions_sent: dict = {}
        self.answers = preflight_answers()

    def system_one(self, state, questions, model=None):
        self.questions_sent = dict(questions)
        return SimpleNamespace(answers=self.answers, model="jev-latest")


def build_pipeline(jev_service, *, rollout=None):
    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls:
        retriever_cls.return_value.retrieve.return_value = []
        reranker_cls.return_value.rerank.side_effect = lambda _query, chunks: chunks
        pipeline = RAGPipeline(
            "test.txt",
            session_id="s1",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            jev_service=jev_service,
            question_set_rollout=rollout,
        )
    return pipeline


def _service_with(client: RecordingTypesafeClient, controller=None):
    provider = None if controller is None else controller.provide
    service = JevDecisionService(client=JevClient(client=client, question_set_provider=provider))
    return service


def test_the_pipeline_serves_the_candidate_question_set_inside_the_bucket(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    controller = CandidateRollout()
    controller.activate(
        7, "preflight", sample_payload(), PREFLIGHT_QUESTION_SET_VERSION, 100
    )
    client = RecordingTypesafeClient()
    try:
        pipeline = build_pipeline(_service_with(client, controller), rollout=controller)
        with patch("rag.pipeline.generate", return_value="answer"):
            pipeline.query("年假有几天")
        assert client.questions_sent["intent"].instructions == CANDIDATE_INTENT_INSTRUCTIONS
    finally:
        controller.deactivate(7)


def test_the_pipeline_keeps_the_default_question_set_without_a_rollout(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    client = RecordingTypesafeClient()
    pipeline = build_pipeline(_service_with(client), rollout=None)
    with patch("rag.pipeline.generate", return_value="answer"):
        pipeline.query("年假有几天")
    assert client.questions_sent["intent"].instructions == (
        build_preflight_questions()["intent"].instructions
    )


def test_the_pipeline_keeps_the_default_set_for_requests_outside_the_bucket(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    controller = CandidateRollout()
    controller.activate(
        7, "preflight", sample_payload(), PREFLIGHT_QUESTION_SET_VERSION, 0
    )
    client = RecordingTypesafeClient()
    try:
        pipeline = build_pipeline(_service_with(client, controller), rollout=controller)
        with patch("rag.pipeline.generate", return_value="answer"):
            pipeline.query("年假有几天")
        assert client.questions_sent["intent"].instructions != CANDIDATE_INTENT_INSTRUCTIONS
    finally:
        controller.deactivate(7)
