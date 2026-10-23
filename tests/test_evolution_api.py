"""Evolution observation API: phase-2 reads plus phase-3 admin decisions.

The phase-2 surface is administrator-only and read-only: four GET endpoints
under ``/knowledge-operations/evolution`` and nothing that can move an
observation or a cluster through the state machine. Phase 3 (Task 5) adds the
first writes — the diagnosis draft read-out and the three audited admin
decisions — both still administrator-only. The tests mount the router on a
private app (``rag.api`` mounts it once in production) and assert the
authorization boundary, the pagination shape, and that serialization carries
stored signals only, never question text, chunk text, or credentials.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.auth import create_token
from rag.evolution import api as evolution_api
from rag.evolution.candidate_store import CandidateStore
from rag.evolution.models import (
    EVOLUTION_TRANSITIONS,
    DiagnosisDraft,
    EvolutionStatus,
    FailureObservation,
    ObservationPool,
)
from rag.evolution.store import FailureTraceStore
from rag.user_db import UserDB

PREFIX = "/knowledge-operations/evolution"
CHUNK_HASH = "a" * 64


@pytest.fixture
def evolution_client(tmp_path, monkeypatch):
    """A private app that mounts only the read-only evolution router."""
    from config import settings

    db = UserDB(str(tmp_path / "users.db"))
    app = FastAPI()
    app.state.user_db = db
    app.include_router(evolution_api.router)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "evolution_db_path", str(tmp_path / "evolution.db"))
    # 运维日志改道由 conftest 的 isolated_service_control_log 统一负责（apply/rollback 用例
    # 都会写它，而那些行与真实运维记录长得一模一样）。
    with TestClient(app) as client:
        yield client
    evolution_api.close_store()
    db.close()


@pytest.fixture
def user_headers(evolution_client) -> dict:
    return _headers(evolution_client.app.state.user_db, "alice")


@pytest.fixture
def admin_headers(evolution_client) -> dict:
    return _headers(evolution_client.app.state.user_db, "admin", is_admin=True)


@pytest.fixture
def store_with_observation(evolution_client, monkeypatch) -> FailureTraceStore:
    """Two observations whose question text must never reach the API response."""
    from config import settings

    store = FailureTraceStore(settings.evolution_db_path)
    store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="retrieval", question="chunk-secret-text",
        route="rag", chunk_hashes=(CHUNK_HASH,), best_score=0.11,
        answerability_kind="insufficient_evidence",
        jev_question_set_version="passages-v1",
        decision_metadata={"question_set_version": "passages-v1", "jev_route": "rag"},
    ))
    store.add_observation(FailureObservation(
        kb_id="kb2", failure_stage="generation", question="chunk-secret-text",
        route="rag", chunk_hashes=(), best_score=0.62,
        answerability_kind="answered", feedback_kind="not_useful",
    ))
    store.close()
    return store


@pytest.fixture
def store_with_cluster(store_with_observation, monkeypatch) -> dict:
    """One cluster promoted into the candidate pool, with one clustered member."""
    from config import settings

    store = FailureTraceStore(settings.evolution_db_path)
    member = store.list_observations()[0]
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key="kb1|retrieval|recall_miss",
        kb_id="kb1",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    store.attach_cluster_member(
        member["id"], cluster["id"],
        failure_stage="retrieval", root_cause_feature="recall_miss", now=now,
    )
    store.refresh_cluster(
        cluster["id"], cutoff=now - 30 * 86400, priority="normal", now=now
    )
    promoted = store.promote_cluster(
        cluster["id"],
        new_status="awaiting_diagnosis_review",
        pool="candidate_pool",
        member_status="clustered",
        now=now,
    )
    store.close()
    return {"cluster_id": promoted["id"], "member_id": member["id"]}


def _headers(db: UserDB, username: str, *, is_admin: bool = False) -> dict:
    user_id = db.create_user(username, "Password-123!")
    if is_admin:
        db.set_user_admin(user_id, True)
    token = create_token({"user_id": user_id, "username": username, "security_version": 1})
    return {"Authorization": f"Bearer {token}"}


def test_observation_list_is_admin_only(evolution_client, user_headers, admin_headers):
    assert evolution_client.get(
        f"{PREFIX}/observations", headers=user_headers
    ).status_code == 403
    response = evolution_client.get(f"{PREFIX}/observations", headers=admin_headers)
    assert response.status_code == 200
    assert set(response.json()) >= {"items", "total", "page", "page_size"}


def test_every_endpoint_rejects_non_admins(evolution_client, user_headers):
    for path in ("/observations", "/clusters", "/clusters/1", "/state-machine", "/metrics"):
        response = evolution_client.get(f"{PREFIX}{path}", headers=user_headers)
        assert response.status_code == 403, path


def test_endpoints_require_authentication(evolution_client):
    for path in ("/observations", "/clusters", "/clusters/1", "/state-machine", "/metrics"):
        assert evolution_client.get(f"{PREFIX}{path}").status_code == 401, path


def test_phase_two_exposes_no_state_changing_method(evolution_client, admin_headers):
    """Phase 2 has no write endpoint: every mutation attempt is a 405."""
    for path in ("/observations", "/clusters", "/clusters/1", "/state-machine", "/metrics"):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(evolution_client, method)(
                f"{PREFIX}{path}", headers=admin_headers
            )
            assert response.status_code == 405, (path, method, response.status_code)


def test_state_machine_matches_design_section_seven(evolution_client, admin_headers):
    response = evolution_client.get(f"{PREFIX}/state-machine", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["transitions"]["observed"] == [
        "clustered", "rejected", "blocked", "needs_more_evidence",
    ]
    assert body["transitions"]["canary"] == [
        "stable", "rolled_back", "rejected", "blocked", "needs_more_evidence",
    ]
    # Phase 3 adds exactly one state-changing method: the audited decision endpoint.
    write_paths = {
        path["path"] for path in body["endpoints"] if "POST" in path["methods"]
    }
    assert write_paths == {f"{PREFIX}/clusters/{{cluster_id}}/decision"}
    # The later phases' read surfaces are listed too, so an administrator can find
    # them from the reference tab; their write endpoints stay documented elsewhere
    # (that keeps the single-write-method invariant above literally true).
    read_paths = {
        path["path"] for path in body["endpoints"] if path["methods"] == ["GET"]
    }
    assert {
        f"{PREFIX}/candidates",
        f"{PREFIX}/code-candidates",
        f"{PREFIX}/code-generation-queue",
        f"{PREFIX}/gap-tickets",  # 第三种制品（内容缺口工单）的只读面
    } <= read_paths
    assert body["transitions"] == {
        status: list(targets) for status, targets in evolution_api.EVOLUTION_TRANSITIONS.items()
    }


def test_cluster_detail_exposes_members_and_state(
    evolution_client, admin_headers, store_with_cluster
):
    cluster_id = store_with_cluster["cluster_id"]
    detail = evolution_client.get(
        f"{PREFIX}/clusters/{cluster_id}", headers=admin_headers
    ).json()
    assert detail["status"] == "awaiting_diagnosis_review"
    assert detail["pool"] == "candidate_pool"
    assert detail["member_count"] == 1
    assert [m["status"] for m in detail["members"]] == ["clustered"]
    assert [m["id"] for m in detail["members"]] == [store_with_cluster["member_id"]]


def test_unknown_cluster_is_not_found(evolution_client, admin_headers, store_with_observation):
    response = evolution_client.get(f"{PREFIX}/clusters/999", headers=admin_headers)
    assert response.status_code == 404


def test_observations_filter_and_page_in_the_api_layer(
    evolution_client, admin_headers, store_with_observation
):
    page = evolution_client.get(
        f"{PREFIX}/observations", headers=admin_headers, params={"page_size": 1}
    ).json()
    assert page["total"] == 2
    assert page["page"] == 1
    assert page["page_size"] == 1
    assert len(page["items"]) == 1
    assert page["items"][0]["id"] == 1

    by_status = evolution_client.get(
        f"{PREFIX}/observations", headers=admin_headers, params={"status": "observed"}
    ).json()
    assert by_status["total"] == 2

    by_kb = evolution_client.get(
        f"{PREFIX}/observations", headers=admin_headers, params={"kb_id": "kb2"}
    ).json()
    assert [item["kb_id"] for item in by_kb["items"]] == ["kb2"]


def test_response_never_contains_chunk_text(evolution_client, admin_headers, store_with_observation):
    body = evolution_client.get(f"{PREFIX}/observations", headers=admin_headers).text
    assert "chunk-secret-text" not in body
    assert "RAG_" not in body


def test_serialization_holds_stored_signals_only(
    evolution_client, admin_headers, store_with_observation
):
    item = evolution_client.get(
        f"{PREFIX}/observations", headers=admin_headers, params={"kb_id": "kb1"}
    ).json()["items"][0]
    assert "question" not in item
    assert item["failure_stage"] == "retrieval"
    assert item["chunk_hashes"] == [CHUNK_HASH]
    assert item["best_score"] == 0.11
    assert item["answerability_kind"] == "insufficient_evidence"
    assert item["jev_question_set_version"] == "passages-v1"
    assert set(item) == {
        "id", "cluster_id", "kb_id", "failure_stage", "root_cause_feature", "status",
        "pool", "priority", "route", "chunk_hashes", "adopted_evidence_hashes",
        "best_score", "answerability_kind", "confidence_level", "tool_names",
        "structured_query_kind", "exception_class", "feedback_kind",
        "jev_question_set_version", "decision_metadata", "total_ms",
        "created_at", "updated_at",
    }


def test_missing_database_returns_empty_pages_without_creating_it(
    evolution_client, admin_headers, tmp_path
):
    """The observation flag is off by default: reads must not create the store."""
    db_file = tmp_path / "evolution.db"
    assert not db_file.exists()

    observations = evolution_client.get(f"{PREFIX}/observations", headers=admin_headers).json()
    assert observations == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert evolution_client.get(f"{PREFIX}/clusters", headers=admin_headers).json()["items"] == []

    assert not db_file.exists()


def test_state_machine_view_is_pure_read(evolution_client, admin_headers, store_with_cluster):
    """Reading the reference view never rewrites stored rows."""
    first = evolution_client.get(f"{PREFIX}/clusters", headers=admin_headers).json()
    second = evolution_client.get(f"{PREFIX}/state-machine", headers=admin_headers).json()
    third = evolution_client.get(f"{PREFIX}/clusters", headers=admin_headers).json()
    assert first == third
    assert second["statuses"]
    assert [c["status"] for c in first["items"]] == ["awaiting_diagnosis_review"]


def test_metrics_empty_store_returns_zero_safe_payload_without_creating_it(
    evolution_client, admin_headers, tmp_path
):
    """Metrics must be zero-safe on a store that was never created."""
    db_file = tmp_path / "evolution.db"
    assert not db_file.exists()

    body = evolution_client.get(f"{PREFIX}/metrics", headers=admin_headers).json()

    assert body["counts"]["observations"] == 0
    assert body["counts"]["clusters"] == 0
    assert body["rates"]["problem_solve_rate"] is None
    assert body["definitions"]["problem_solve_rate"]["available_from_phase"] == 2
    assert not db_file.exists()


def test_metrics_reports_phase_two_payload_without_text(
    evolution_client, admin_headers, store_with_observation
):
    body = evolution_client.get(f"{PREFIX}/metrics", headers=admin_headers).json()

    assert body["window_days"] == 30
    assert body["counts"]["observations"] == 2
    assert body["counts"]["skipped"] == 0
    assert set(body["rates"]) >= {
        "problem_solve_rate", "generalization_rate", "wrong_answer_rate",
        "refusal_accuracy_rate", "avg_latency_ms",
    }
    assert "chunk-secret-text" not in evolution_client.get(
        f"{PREFIX}/metrics", headers=admin_headers
    ).text


# ------------------------------------------------------------------
# Task 5: the diagnosis read-out and the three audited admin decisions.
#
# A draft is the verbatim diagnoser output — permission-filtered evidence text
# an administrator reviews before confirming a root cause. It is stored JSON,
# so the assertions check both its field set and that no credential-shaped
# value survives serialization.
# ------------------------------------------------------------------

DIAGNOSIS_SECRET = "sk-evolution-diagnosis-sentinel"

# The frozen draft contract: nine design-section-6 fields plus the confidence
# and reproduction metadata the worker filters on.
DIAGNOSIS_PAYLOAD_FIELDS = {
    "failure_category", "stage", "first_divergence", "direct_evidence", "root_cause",
    "impact_scope", "suggested_layer", "reproduction_steps", "acceptance_criteria",
    "confidence", "reproducible", "evidence_refs",
}


def sample_draft(**kw) -> DiagnosisDraft:
    """A minimal valid draft; every design-section-6 field is required."""
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


def _store() -> FailureTraceStore:
    """A separate writer handle on the test database (the API keeps its own)."""
    from config import settings

    return FailureTraceStore(settings.evolution_db_path)


def _user_id(db: UserDB, username: str) -> int:
    row = db.get_user_by_username(username)
    assert row is not None
    return int(row["id"])


@pytest.fixture
def store_with_drafts(store_with_cluster) -> dict:
    """Two drafts for the reviewed cluster plus an audit row of another cluster."""
    cluster_id = store_with_cluster["cluster_id"]
    store = _store()
    first = store.add_draft(
        cluster_id, sample_draft(root_cause="第一轮：假设切分截断"),
        prompt_version="diagnosis-v1", model="deepseek/deepseek-v4.1-flash",
    )
    second = store.add_draft(
        cluster_id,
        sample_draft(root_cause="第二轮：假设切分截断", confidence="medium"),
        prompt_version="diagnosis-v2", model="deepseek/deepseek-v4.1-flash",
    )
    store.add_audit("admin-9", "reject", 999, "不属于这条聚类的备注")
    store.close()
    return {"cluster_id": cluster_id, "draft_ids": (first, second)}


def test_diagnosis_endpoints_are_admin_only(evolution_client, user_headers, admin_headers):
    diagnosis = f"{PREFIX}/clusters/1/diagnosis"
    decision = f"{PREFIX}/clusters/1/decision"
    assert evolution_client.get(diagnosis, headers=user_headers).status_code == 403
    assert evolution_client.post(
        decision, json={"action": "reject", "note": ""}, headers=user_headers
    ).status_code == 403
    assert evolution_client.get(diagnosis, headers=admin_headers).status_code == 200


def test_diagnosis_endpoints_require_authentication(evolution_client):
    assert evolution_client.get(f"{PREFIX}/clusters/1/diagnosis").status_code == 401
    assert evolution_client.post(
        f"{PREFIX}/clusters/1/decision", json={"action": "reject", "note": ""}
    ).status_code == 401


def test_diagnosis_returns_the_newest_cluster_draft_and_the_cluster_audit(
    evolution_client, admin_headers, store_with_drafts
):
    """Legacy cluster-level drafts surface as ``cluster_draft``; member_drafts still list members.

    The two drafts in this fixture are cluster-level (``observation_id`` NULL,
    the pre-phase-4 shape), so the newest is reported under ``cluster_draft`` and
    the cluster's single member appears under ``member_drafts`` with ``draft`` null
    until a member-level draft exists.
    """
    cluster_id = store_with_drafts["cluster_id"]
    store = _store()
    store.add_audit("admin-7", "needs_more_evidence", cluster_id, "缺少管理员确认标签")
    member_id = store.list_observations(cluster_id)[0]["id"]
    store.close()

    body = evolution_client.get(
        f"{PREFIX}/clusters/{cluster_id}/diagnosis", headers=admin_headers
    ).json()

    assert body["cluster_id"] == cluster_id
    assert body["cluster_draft"]["id"] == store_with_drafts["draft_ids"][1]
    assert body["cluster_draft"]["prompt_version"] == "diagnosis-v2"
    assert body["cluster_draft"]["model"] == "deepseek/deepseek-v4.1-flash"
    assert body["cluster_draft"]["reproducible"] is True
    assert set(body["cluster_draft"]) == {
        "id", "cluster_id", "payload", "prompt_version", "model", "reproducible", "created_at",
    }
    assert body["cluster_draft"]["payload"]["root_cause"] == "第二轮：假设切分截断"
    assert set(body["cluster_draft"]["payload"]) == DIAGNOSIS_PAYLOAD_FIELDS
    assert body["cluster_draft"]["payload"]["evidence_refs"] == ["a" * 64]
    # The undrafted member is listed, with its admin-visible question text.
    assert body["member_drafts"] == [
        {
            "observation_id": member_id,
            "question": "chunk-secret-text",
            "stage": "retrieval",
            "root_cause_feature": "recall_miss",
            "draft": None,
        }
    ]
    assert [(row["actor_id"], row["action"], row["note"]) for row in body["audit"]] == [
        ("admin-7", "needs_more_evidence", "缺少管理员确认标签"),
    ]
    assert "admin-9" not in evolution_client.get(
        f"{PREFIX}/clusters/{cluster_id}/diagnosis", headers=admin_headers
    ).text


def test_diagnosis_lists_member_drafts_with_the_question_and_each_draft(
    evolution_client, admin_headers, store_with_cluster
):
    """Phase-4 shape: member_drafts carry the question text an admin diagnoses from.

    The endpoint is administrator-only, so exposing the observation's question
    here is an intentional boundary (a diagnosis is meaningless without it); no
    other endpoint leaks question text. Each member reports its own draft or null.
    """
    cluster_id = store_with_cluster["cluster_id"]
    store = _store()
    drafted = store.list_observations(cluster_id)[0]
    store.add_observation(FailureObservation(
        kb_id="kb1", failure_stage="retrieval", question="second-member-question",
        route="rag", best_score=0.2, answerability_kind="insufficient_evidence",
    ))
    second = store.list_observations()[-1]
    store.attach_cluster_member(
        second["id"], cluster_id, failure_stage="retrieval",
        root_cause_feature="recall_miss", now=time.time(),
    )
    store.add_draft(
        cluster_id, sample_draft(root_cause="成员级：候选在池中被挤压"),
        observation_id=drafted["id"], prompt_version="diagnosis-v2", model="member-model",
    )
    store.close()

    body = evolution_client.get(
        f"{PREFIX}/clusters/{cluster_id}/diagnosis", headers=admin_headers
    ).json()

    entries = {entry["observation_id"]: entry for entry in body["member_drafts"]}
    assert set(entries) == {drafted["id"], second["id"]}
    with_draft = entries[drafted["id"]]
    assert with_draft["question"] == "chunk-secret-text"
    assert with_draft["stage"] == "retrieval"
    assert with_draft["root_cause_feature"] == "recall_miss"
    assert with_draft["draft"]["payload"]["root_cause"] == "成员级：候选在池中被挤压"
    assert set(with_draft["draft"]) == {
        "id", "cluster_id", "payload", "prompt_version", "model", "reproducible", "created_at",
    }
    # The sibling member has no draft yet and reports null, still with its question.
    without_draft = entries[second["id"]]
    assert without_draft["draft"] is None
    assert without_draft["question"] == "second-member-question"
    # A member-level draft is not the legacy cluster draft.
    assert body["cluster_draft"] is None


def test_diagnosis_audit_is_chronological_and_cluster_scoped(
    evolution_client, admin_headers, store_with_drafts
):
    cluster_id = store_with_drafts["cluster_id"]
    store = _store()
    first = store.add_audit("admin-7", "needs_more_evidence", cluster_id, "第一轮证据不足")
    second = store.add_audit("admin-7", "approve_for_candidate", cluster_id, "根因已确认")
    store.close()

    rows = evolution_client.get(
        f"{PREFIX}/clusters/{cluster_id}/diagnosis", headers=admin_headers
    ).json()["audit"]

    assert [row["id"] for row in rows] == [first, second]
    assert [row["action"] for row in rows] == ["needs_more_evidence", "approve_for_candidate"]
    assert set(rows[0]) == {"id", "actor_id", "action", "cluster_id", "note", "created_at"}


def test_diagnosis_without_a_draft_reads_empty(
    evolution_client, admin_headers, store_with_cluster
):
    body = evolution_client.get(
        f"{PREFIX}/clusters/{store_with_cluster['cluster_id']}/diagnosis", headers=admin_headers
    ).json()

    assert body == {
        "cluster_id": store_with_cluster["cluster_id"],
        "cluster_draft": None,
        "member_drafts": [
            {
                "observation_id": store_with_cluster["member_id"],
                "question": "chunk-secret-text",
                "stage": "retrieval",
                "root_cause_feature": "recall_miss",
                "draft": None,
            }
        ],
        "audit": [],
    }


def test_diagnosis_on_a_missing_database_reads_empty_without_creating_it(
    evolution_client, admin_headers, tmp_path
):
    db_file = tmp_path / "evolution.db"
    assert not db_file.exists()

    body = evolution_client.get(f"{PREFIX}/clusters/3/diagnosis", headers=admin_headers).json()

    assert body == {"cluster_id": 3, "cluster_draft": None, "member_drafts": [], "audit": []}
    assert not db_file.exists()


def test_diagnosis_payload_never_carries_credentials(
    evolution_client, admin_headers, store_with_drafts, monkeypatch
):
    from config import settings

    monkeypatch.setattr(settings, "deepseek_api_key", DIAGNOSIS_SECRET)
    body = evolution_client.get(
        f"{PREFIX}/clusters/{store_with_drafts['cluster_id']}/diagnosis", headers=admin_headers
    ).text

    assert DIAGNOSIS_SECRET not in body
    assert "RAG_" not in body


def test_serialize_draft_redacts_secret_shaped_payload_keys():
    payload = sample_draft().model_dump(mode="json")
    payload["connection_string"] = "Server=db;Password=hunter2"
    payload["nested"] = [{"api_key": "sk-should-never-leave"}]

    serialized = evolution_api._serialize_draft({
        "id": 1,
        "cluster_id": 2,
        "prompt_version": "diagnosis-v1",
        "model": "deepseek/deepseek-v4.1-flash",
        "reproducible": True,
        "created_at": 1.0,
        "payload": payload,
    })

    assert serialized["payload"]["connection_string"] == "[REDACTED]"
    assert serialized["payload"]["nested"][0]["api_key"] == "[REDACTED]"
    assert serialized["payload"]["root_cause"] == payload["root_cause"]
    assert set(serialized["payload"]) == DIAGNOSIS_PAYLOAD_FIELDS | {"connection_string", "nested"}


def test_decision_rejects_an_unknown_action(evolution_client, admin_headers, store_with_cluster):
    cluster_id = store_with_cluster["cluster_id"]
    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "approve", "note": "拼写错误"},
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert _store().list_audit(cluster_id) == []


def test_decision_on_an_unknown_cluster_is_not_found(
    evolution_client, admin_headers, store_with_observation
):
    response = evolution_client.post(
        f"{PREFIX}/clusters/999/decision",
        json={"action": "reject", "note": "聚类不存在"},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert _store().list_audit(999) == []


def test_decision_on_a_missing_database_is_not_found_without_creating_it(
    evolution_client, admin_headers, tmp_path
):
    db_file = tmp_path / "evolution.db"
    assert not db_file.exists()

    response = evolution_client.post(
        f"{PREFIX}/clusters/1/decision",
        json={"action": "reject", "note": ""},
        headers=admin_headers,
    )

    assert response.status_code == 404
    assert not db_file.exists()


def test_decision_outside_diagnosis_review_is_a_conflict_without_audit(
    evolution_client, admin_headers, store_with_observation
):
    store = _store()
    cluster = store.upsert_cluster(
        cluster_key="kb1|retrieval|recall_miss",
        kb_id="kb1",
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=time.time(),
    )
    store.close()

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster['id']}/decision",
        json={"action": "reject", "note": "尚未进入诊断审核"},
        headers=admin_headers,
    )

    assert response.status_code == 409
    assert _store().list_audit(cluster["id"]) == []


def test_decision_targets_map_every_action_onto_legal_transitions():
    targets = evolution_api.DECISION_TARGETS

    assert set(targets) == {"approve_for_candidate", "reject", "needs_more_evidence"}
    for action, target in targets.items():
        assert target["new_status"] in EVOLUTION_TRANSITIONS[
            EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value
        ], action
        # A member can be ``clustered`` (admitted with the cluster) or still
        # ``observed`` (attached after admission), so both must reach the target.
        for source in (EvolutionStatus.OBSERVED.value, EvolutionStatus.CLUSTERED.value):
            assert (
                target["member_status"] == source
                or target["member_status"] in EVOLUTION_TRANSITIONS[source]
            ), (action, source)
        assert target["pool"] in tuple(pool.value for pool in ObservationPool), action


def test_an_accepted_decision_writes_an_audit_row_and_advances_the_cluster(
    evolution_client, admin_headers, store_with_cluster
):
    """The audit row precedes the transition, so the decision stays traceable."""
    cluster_id = store_with_cluster["cluster_id"]
    admin_id = _user_id(evolution_client.app.state.user_db, "admin")

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "approve_for_candidate", "note": "根因已确认"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved_for_candidate"
    rows = _store().list_audit(cluster_id)
    assert [(row["actor_id"], row["action"], row["note"]) for row in rows] == [
        (str(admin_id), "approve_for_candidate", "根因已确认"),
    ]


def test_approval_survives_a_member_attached_after_admission(
    evolution_client, admin_headers, store_with_cluster
):
    """A late member stays ``observed``; the decision must still be legal for it."""
    from rag.evolution.models import FailureObservation

    store = _store()
    cluster_id = store_with_cluster["cluster_id"]
    late = store.add_observation(
        FailureObservation(
            kb_id="kb1", failure_stage="retrieval", question="late-member",
            route="rag", chunk_hashes=(), best_score=0.4,
            answerability_kind="insufficient_evidence",
        )
    )
    store.attach_cluster_member(
        late["id"], cluster_id,
        failure_stage="retrieval", root_cause_feature="recall_miss", now=time.time(),
    )
    store.close()

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "approve_for_candidate", "note": "迟加入成员不影响批准"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved_for_candidate"
    statuses = {member["id"]: member["status"] for member in response.json()["members"]}
    assert statuses[late["id"]] == "clustered"


@pytest.mark.parametrize(
    "action,expected_status,expected_member_status",
    [
        ("approve_for_candidate", "approved_for_candidate", "clustered"),
        ("reject", "rejected", "rejected"),
        ("needs_more_evidence", "needs_more_evidence", "needs_more_evidence"),
    ],
)
def test_admin_decision_advances_the_cluster_and_is_audited(
    evolution_client,
    admin_headers,
    store_with_cluster,
    action,
    expected_status,
    expected_member_status,
):
    cluster_id = store_with_cluster["cluster_id"]
    admin_id = _user_id(evolution_client.app.state.user_db, "admin")

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": action, "note": f"{action} 备注"},
        headers=admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == cluster_id
    assert body["status"] == expected_status
    assert body["pool"] == "candidate_pool"
    assert [member["status"] for member in body["members"]] == [expected_member_status]
    rows = _store().list_audit(cluster_id)
    assert [(row["actor_id"], row["action"], row["note"]) for row in rows] == [
        (str(admin_id), action, f"{action} 备注"),
    ]


def test_a_second_decision_is_a_conflict_and_writes_no_second_audit_row(
    evolution_client, admin_headers, store_with_cluster
):
    cluster_id = store_with_cluster["cluster_id"]
    decision = f"{PREFIX}/clusters/{cluster_id}/decision"
    payload = {"action": "approve_for_candidate", "note": "根因已确认"}

    first = evolution_client.post(decision, json=payload, headers=admin_headers)
    second = evolution_client.post(decision, json=payload, headers=admin_headers)

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(_store().list_audit(cluster_id)) == 1


def test_a_decision_records_the_administrator_confirmed_layer(
    evolution_client, admin_headers, store_with_cluster
):
    """The confirmed layer is the human authority a code patch is generated for."""
    cluster_id = store_with_cluster["cluster_id"]

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={
            "action": "approve_for_candidate",
            "note": "回放数据证实检索分数区分度低、撞名块淹没候选",
            "confirmed_layer": "reranking",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["confirmed_layer"] == "reranking"
    rows = _store().list_audit(cluster_id)
    assert "reranking" in rows[-1]["note"]


def test_an_unknown_confirmed_layer_is_rejected_without_an_audit_row(
    evolution_client, admin_headers, store_with_cluster
):
    cluster_id = store_with_cluster["cluster_id"]

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "approve_for_candidate", "note": "x", "confirmed_layer": "not-a-layer"},
        headers=admin_headers,
    )

    assert response.status_code == 400
    assert _store().list_audit(cluster_id) == []


def test_the_layer_can_be_confirmed_after_the_approval(
    evolution_client, admin_headers, store_with_cluster
):
    """Confirming the root-cause layer is a separate judgment from the approval."""
    cluster_id = store_with_cluster["cluster_id"]
    approved = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "approve_for_candidate", "note": "根因已确认"},
        headers=admin_headers,
    )
    assert approved.status_code == 200

    response = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={
            "action": "confirm_layer",
            "note": "回放数据证实分数区分度低、撞名块淹没候选",
            "confirmed_layer": "reranking",
        },
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert response.json()["confirmed_layer"] == "reranking"
    notes = [row["note"] for row in _store().list_audit(cluster_id)]
    assert any("reranking" in note for note in notes)


def test_confirming_a_layer_needs_a_layer_and_a_reviewable_cluster(
    evolution_client, admin_headers, store_with_cluster
):
    cluster_id = store_with_cluster["cluster_id"]

    missing = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "confirm_layer", "note": "x"},
        headers=admin_headers,
    )
    assert missing.status_code == 400

    rejected = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "reject", "note": "非缺陷"},
        headers=admin_headers,
    )
    assert rejected.status_code == 200
    after_reject = evolution_client.post(
        f"{PREFIX}/clusters/{cluster_id}/decision",
        json={"action": "confirm_layer", "note": "x", "confirmed_layer": "reranking"},
        headers=admin_headers,
    )
    assert after_reject.status_code == 409


# ------------------------------------------------------------------
# 队列 7：审查台需要的两样东西——**可复制的命令**与**工单只读面**。
# ------------------------------------------------------------------


def _seed_code_patch_candidate(
    store, *, marker: str = "审查台测试用的注释行", observation_id: int | None = None
) -> int:
    """一份最小但合法的代码补丁候选（diff/测试路径/base_commit 都过边界校验）。"""
    import subprocess
    from pathlib import Path

    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.code_patches import CodePatchCandidate

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
    ).stdout.strip()
    diff = (
        "diff --git a/rag/pipeline.py b/rag/pipeline.py\n"
        "--- a/rag/pipeline.py\n"
        "+++ b/rag/pipeline.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import asyncio\n"
        "+import json\n"
        f"+# {marker}\n"
        " import logging\n"
    )
    candidate = CodePatchCandidate(
        artifact_type="code_patch",
        layer="code",
        paths=("rag/pipeline.py",),
        diff=diff,
        test_paths=("tests/test_pipeline.py",),
        rationale="审查台用例",
        expected_effect="无行为变化",
        rollback_ref={"base_commit": head},
        confidence="low",
    )
    return CandidateStore(store).add_candidate(
        observation_id, 1, candidate, prompt_version="patch-v3", model="scripted"
    )


def test_code_candidate_detail_exposes_copy_only_commands(
    evolution_client, admin_headers, store_with_cluster
):
    """命令必须**结构化**给出并可复制；端点从不执行它，且如实标出"还没导出"。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _seed_code_patch_candidate(store)

    detail = evolution_client.get(
        f"{PREFIX}/code-candidates/{candidate_id}", headers=admin_headers
    ).json()

    assert f"candidate-{candidate_id}.patch" in detail["apply_command"]
    assert detail["apply_command"].startswith("git -C ")
    # 刻意不带 --3way：导出的补丁没有 index 行，三路合并必然失败（2026-10-11 实测）。
    assert "--3way" not in detail["apply_command"]
    assert "pytest tests/test_pipeline.py" in detail["apply_command"]
    assert detail["revert_command"].endswith(f"apply -R artifacts/patches/candidate-{candidate_id}.patch")
    assert detail["export_path"].endswith(f"candidate-{candidate_id}.patch")
    assert detail["exported"] is False  # 还没批准 ⇒ 文件不存在，前端据此提示"先批准"
    store.close()


def test_gap_tickets_endpoint_lists_what_the_worker_drafted(
    evolution_client, admin_headers
):
    """工单只读面：把 worker 起草的工单（含缺失词与复测）如实读出来。"""
    import time

    from rag.evolution.gaps import build_ticket

    store = FailureTraceStore(settings_path())
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key="kb1|generation|gap",
        kb_id="kb1",
        failure_stage="generation",
        root_cause_feature="undetermined",
        seen_at=now,
    )
    observations = [
        store.get_observation(
            store.add_observation(
                FailureObservation(
                    kb_id="kb1",
                    failure_stage="generation",
                    question="R50 电池卖多少钱",
                    route="fixed",
                    answerability_kind="insufficient_evidence",
                )
            )["id"]
        )
    ]
    ticket_id = store.add_gap_ticket(
        build_ticket(cluster, observations, missing_terms=("电池", "价格"), probed_questions=1, now=now)
    )

    body = evolution_client.get(f"{PREFIX}/gap-tickets", headers=admin_headers).json()

    assert body["total"] == 1
    item = body["items"][0]
    assert item["id"] == ticket_id
    assert item["kb_id"] == "kb1"
    assert item["status"] == "open"
    assert item["question_samples"] == ["R50 电池卖多少钱"]
    assert item["missing_terms"] == ["电池", "价格"]
    assert item["recheck"] is None
    # 状态筛选按白名单校验：未知值 400（读取端不猜自由文本）
    assert evolution_client.get(
        f"{PREFIX}/gap-tickets", params={"status": "whatever"}, headers=admin_headers
    ).status_code == 400
    store.close()


def settings_path() -> str:
    from config import settings

    return str(settings.evolution_db_path)


def test_gate_entry_serialisation_passes_metrics_through_but_only_when_present():
    """结构化指标只在存在时透出（老条目没有这个键 ⇒ 前端据此不画对照表）。"""
    entry = {
        "gate": "sandbox",
        "result": "passed",
        "detail": "沙箱结论",
        "created_at": 1.0,
        "metrics": {"applied": True, "verdict": {"passed": True}},
    }

    payload = evolution_api._serialize_gate_entry(entry)
    assert payload["metrics"]["verdict"]["passed"] is True

    legacy = dict(entry)
    legacy.pop("metrics")
    assert "metrics" not in evolution_api._serialize_gate_entry(legacy)


def test_host_ab_entries_get_their_own_summary_and_stay_out_of_the_sandbox_one(
    evolution_client, admin_headers, store_with_cluster
):
    """层 2 的两个生产者分开呈现：宿主 A/B 有数字，就不能被算成沙箱的结论。

    此前宿主 A/B 只写一个 artifacts JSON，审查台一无所知；回流之后它进同一条 gate_log，
    但**必须**能被分辨出来 —— 合并渲染会让"这个数字是谁量出来的"消失。
    """
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.sandbox import HOST_AB_GATE_NAME, HostAbReport, record_host_ab_outcome

    store = FailureTraceStore(settings_path())
    candidate_id = _seed_code_patch_candidate(store)
    candidates = CandidateStore(store)
    verdict = {
        "cases": 20,
        "sides": {
            "jev": {
                "baseline": {"route_accuracy": 1.0, "top5_hit_rate": 1.0, "failed_calls": 0, "prep_p50_ms": 900},
                "patched": {"route_accuracy": 1.0, "top5_hit_rate": 0.98, "failed_calls": 0, "prep_p50_ms": 850},
                "route_delta_pp": 0.0,
                "top5_delta_pp": -2.0,
                "failed_calls_delta": 0,
                "passed": True,
            }
        },
        "route_delta_pp": 0.0,
        "top5_delta_pp": -2.0,
        "thresholds": {"route_drop_max_pp": 0.0, "top5_drop_max_pp": 2.0},
        "passed": True,
        "verdict": "passed: Jev side route delta +0.0pp (limit +0.0pp), top-5 delta -2.0pp (limit -2.0pp) over 20 cases",
    }
    record_host_ab_outcome(
        candidates,
        HostAbReport(
            candidate_id=candidate_id,
            kb_id="rag_docs",
            runs=2,
            verdict=verdict,
            duration_s=12.5,
            worktrees=("clean", "patched"),
        ),
        actor_id="admin:cli",
        note="宿主 A/B 对照（层 2）",
    )

    detail = evolution_client.get(
        f"{PREFIX}/code-candidates/{candidate_id}", headers=admin_headers
    ).json()

    assert len(detail["host_ab_summary"]) == 1
    entry = detail["host_ab_summary"][0]
    assert entry["gate"] == HOST_AB_GATE_NAME
    assert entry["result"] == "passed"
    assert entry["metrics"]["runs"] == 2
    assert entry["metrics"]["verdict"]["passed"] is True
    # 两侧数字都在（审查台据此画对照表）
    assert entry["metrics"]["verdict"]["sides"]["jev"]["patched"]["top5_hit_rate"] == 0.98
    # 沙箱那份摘要**不得**把宿主条目算进去（反之亦然）
    assert detail["sandbox_summary"] == []
    # 一次测量只追加证据：候选状态没有被这件事推动
    assert detail["status"] == "candidate_testing"
    assert [row["gate"] for row in detail["gate_log"]] == ["scope", HOST_AB_GATE_NAME]
    store.close()


def test_host_ab_summary_says_when_no_comparison_was_produced(
    evolution_client, admin_headers, store_with_cluster
):
    """没有可比对照 ⇒ 显式记 inconclusive，别让"没有数字"看起来像"没有变差"。"""
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.sandbox import HostAbReport, record_host_ab_outcome

    store = FailureTraceStore(settings_path())
    candidate_id = _seed_code_patch_candidate(store)
    record_host_ab_outcome(
        CandidateStore(store),
        HostAbReport(candidate_id=candidate_id, kb_id="rag_docs", runs=2, verdict=None, error="replay_exit_1"),
        actor_id="admin:cli",
    )

    detail = evolution_client.get(
        f"{PREFIX}/code-candidates/{candidate_id}", headers=admin_headers
    ).json()

    entry = detail["host_ab_summary"][0]
    assert entry["result"] == "inconclusive"
    assert entry["metrics"]["verdict_unavailable"] is True
    assert "did not produce a verdict" in entry["detail"]
    assert detail["status"] == "candidate_testing"
    store.close()


def test_state_machine_exposes_the_confirmable_layers(evolution_client, admin_headers):
    """可确认层级由后端**派生**并透出，前端据此渲染下拉框（不各写一份清单）。"""
    body = evolution_client.get(f"{PREFIX}/state-machine", headers=admin_headers).json()

    assert body["confirmable_layers"] == list(evolution_api.CONFIRMABLE_LAYERS)
    assert "prompt" in body["confirmable_layers"]

# ------------------------------------------------------------------
# 一键应用（可选重启）：门禁更严，不是更松。
# ------------------------------------------------------------------


def _approved_patch(store) -> int:
    """把一份种子补丁推进到"已批准"（应用端点只接受这个状态）。"""
    from rag.evolution.candidate_store import CandidateStore

    candidate_id = _seed_code_patch_candidate(store)
    candidates = CandidateStore(store)
    for status in ("awaiting_verification", "approved_for_canary"):
        candidates.advance_candidate(candidate_id, status, actor_id="test", note="seed")
    return candidate_id


def _fake_control(monkeypatch, **overrides):
    """把 service_control 的几个动作换成替身（测试**绝不**动真树、绝不重启）。"""
    import rag.evolution.service_control as control

    calls: dict = {"apply": 0, "revert": 0, "restart": 0}
    state = overrides.get("state", "not_applied")
    smoke = overrides.get("smoke", (True, ""))
    tests = overrides.get("tests", (True, ""))

    monkeypatch.setattr(control, "patch_state", lambda *a, **k: state)
    monkeypatch.setattr(control, "patch_paths", lambda diff: ("rag/reranker.py",))
    monkeypatch.setattr(
        control, "patch_is_committed", lambda *a, **k: overrides.get("committed", False)
    )
    monkeypatch.setattr(control, "apply_patch", lambda *a, **k: calls.__setitem__("apply", calls["apply"] + 1))
    monkeypatch.setattr(control, "revert_patch", lambda *a, **k: calls.__setitem__("revert", calls["revert"] + 1))
    monkeypatch.setattr(control, "import_smoke", lambda *a, **k: smoke)
    monkeypatch.setattr(control, "run_target_tests", lambda *a, **k: tests)
    def _spawn(**kwargs):
        calls["restart"] += 1
        calls["spawn_kwargs"] = kwargs
        return "helper.py"

    monkeypatch.setattr(control, "spawn_restart_helper", _spawn)
    return calls


def test_apply_refuses_a_patch_that_is_not_approved(
    evolution_client, admin_headers, store_with_cluster
):
    store = FailureTraceStore(settings_path())
    candidate_id = _seed_code_patch_candidate(store)  # 停在 candidate_testing

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply", headers=admin_headers, json={"restart": False}
    )

    assert response.status_code == 409
    assert "只有已批准" in response.json()["detail"]
    store.close()


def test_apply_reverts_and_does_not_restart_when_the_app_cannot_import(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """最关键的一条：冒烟失败 ⇒ **还原补丁**且**不重启**（服务保持原样）。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch, smoke=(False, "ImportError: boom"))

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply", headers=admin_headers, json={"restart": True}
    )

    assert response.status_code == 409
    assert "无法导入" in response.json()["detail"]
    assert calls["apply"] == 1 and calls["revert"] == 1
    assert calls["restart"] == 0  # 绝不重启一个起不来的服务
    store.close()


def test_apply_restarts_only_after_every_guard_passes(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch)

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply", headers=admin_headers, json={"restart": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True and body["applied_now"] is True
    assert body["steps"]["import_smoke"] is True and body["steps"]["target_tests"] is True
    assert calls["apply"] == 1 and calls["revert"] == 0 and calls["restart"] == 1
    # 助手必须拿到"该停哪个进程"，否则它只会干等端口、新代码永远起不来。
    assert int(calls["spawn_kwargs"]["service_pid"]) > 0
    assert calls["spawn_kwargs"]["test_scope"] in {"full", "target"}
    assert "--3way" not in body["apply_command"]
    # **状态要跟着事实走**：补丁现在确实在运行代码里 ⇒ 状态是 canary（在观察），
    # 而不是继续显示 approved_for_canary。2026-10-12 演练发现：apply 此前不写这一步，
    # 于是"已应用"只活在工作树与 service-control.log 里，队列上分不清哪些行已落地。
    assert body["steps"]["status_after_apply"] == "canary"
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "canary"
    store.close()


def test_apply_leaves_an_already_canary_patch_where_it_is(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """已应用的补丁再点一次是"校验 + 重启"：状态已是 canary 就不再写一次转移（幂等）。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    from rag.evolution.candidate_store import CandidateStore

    CandidateStore(store).advance_candidate(candidate_id, "canary", actor_id="test", note="seed")
    _fake_control(monkeypatch)
    before = len(CandidateStore(store).list_audit(candidate_id))

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply", headers=admin_headers, json={"restart": True}
    )

    assert response.status_code == 200
    assert response.json()["steps"]["status_after_apply"] == "canary"
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "canary"
    assert len(CandidateStore(store).list_audit(candidate_id)) == before, "状态没变就不该多写审计"
    store.close()


# ------------------------------------------------------------------
# 合入之后关闭候选行：人手合进提交的那条路（#10 占了聚类准入名额两周）。
# ------------------------------------------------------------------


def _fake_committed(monkeypatch, *, state="applied", committed=True, content_present=True):
    """把"补丁在代码里、且这些路径相对 HEAD 干净"这套判据换成替身（测试不动真树）。

    ``content_present`` 单独留一个开关：**路径干净 ≠ 改动真的在代码里**（从未被改动的文件也干净），
    所以结案还有第二道内容核对；那条守卫在下面单独用真实现验。
    """
    import rag.evolution.service_control as control

    monkeypatch.setattr(control, "patch_state", lambda *a, **k: state)
    monkeypatch.setattr(control, "patch_paths", lambda diff: ("rag/pipeline.py",))
    monkeypatch.setattr(control, "patch_is_committed", lambda *a, **k: committed)
    monkeypatch.setattr(
        evolution_api, "_added_lines_present_in_tree", lambda diff: (content_present, "" if content_present else "核对失败")
    )


def test_close_out_merged_refuses_when_the_paths_are_clean_but_the_content_is_not_there(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """**本轮真机验证抓到的坑**：`git status` 干净可能只是"这个文件从没被改过"。

    所以"已合入"不能只靠"路径干净"判定；内容对不上就必须拒绝 —— 否则会把"根本没上线"
    写成"已接受"。这里让前两道判据都放行、只让**真的**内容核对跑，用假 diff 触发拒绝。
    """
    import rag.evolution.service_control as control

    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    monkeypatch.setattr(control, "patch_state", lambda *a, **k: "applied")
    monkeypatch.setattr(control, "patch_paths", lambda diff: ("rag/pipeline.py",))
    monkeypatch.setattr(control, "patch_is_committed", lambda *a, **k: True)
    # 前两道判据全放行，只让**真的**内容核对跑：这份 diff 的新增行在文件里并不存在。
    candidates = CandidateStore(store)
    row = candidates.get_candidate(candidate_id)
    missing_line = "+import 这个模块在代码里并不存在_xyz"
    candidates._conn.execute(
        "UPDATE candidates SET diff = ? WHERE id = ?",
        (row["diff"].replace("+import json", missing_line), int(candidate_id)),
    )
    candidates._conn.commit()

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/decision",
        headers=admin_headers,
        json={"action": "close_out_merged", "note": "按路径干净结案"},
    )

    assert response.status_code == 409
    assert "核对未通过" in response.json()["detail"]
    assert "找不到补丁新增的内容行" in response.json()["detail"]
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "approved_for_canary"
    store.close()


def test_added_lines_present_in_tree_accepts_real_content_and_rejects_a_clean_untouched_file():
    """内容核对的正反两面：真在文件里的行通过；一个**从没被改过**的文件必须被拒。"""
    accepted, _ = evolution_api._added_lines_present_in_tree(
        "diff --git a/rag/pipeline.py b/rag/pipeline.py\n"
        "--- a/rag/pipeline.py\n"
        "+++ b/rag/pipeline.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import asyncio\n"
        "+import json\n"
    )
    assert accepted is True

    rejected, why = evolution_api._added_lines_present_in_tree(
        "diff --git a/rag/pipeline.py b/rag/pipeline.py\n"
        "--- a/rag/pipeline.py\n"
        "+++ b/rag/pipeline.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import asyncio\n"
        "+def 这个函数在代码里并不存在_xyz():\n"
    )
    assert rejected is False and "找不到补丁新增的内容行" in why

    missing, missing_why = evolution_api._added_lines_present_in_tree(
        "diff --git a/rag/does_not_exist_at_all.py b/rag/does_not_exist_at_all.py\n"
        "--- a/rag/does_not_exist_at_all.py\n"
        "+++ b/rag/does_not_exist_at_all.py\n"
        "@@ -1 +1 @@\n"
        "+something\n"
    )
    assert missing is False and "不在当前代码里" in missing_why


def test_close_out_merged_records_the_commit_and_releases_the_cluster(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """改动已随提交落地 ⇒ 结案为 stable（终态），备注里点名那次提交，聚类随之释放。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    _fake_committed(monkeypatch)
    monkeypatch.setattr(evolution_api, "_git_last_commit_for", lambda paths: "abc1234")

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/decision",
        headers=admin_headers,
        json={"action": "close_out_merged", "note": "人手合进提交了", "merged_commit": "abc1234"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stable"
    audit = body["audit"][-1]
    assert audit["action"] == "accept"
    assert "abc1234" in audit["note"] and "人手合进提交了" in audit["note"]
    # stable 是终态 ⇒ 这一行的"未决"身份没了，聚类重新可生成。
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "stable"
    store.close()


def test_close_out_merged_refuses_when_the_change_is_only_in_the_working_tree(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """改动还在工作树里（没进提交）⇒ 拒绝结案：`git apply -R` 会留下"某次提交的逆"那种状态。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    _fake_committed(monkeypatch, committed=False)

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/decision",
        headers=admin_headers,
        json={"action": "close_out_merged", "note": "先结案再说"},
    )

    assert response.status_code == 409
    assert "没有进入任何提交" in response.json()["detail"]
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "approved_for_canary", "拒绝不得改状态"
    store.close()


def test_close_out_merged_refuses_when_the_change_is_not_in_the_code(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """改动根本不在代码里 ⇒ 拒绝：不能把"没上线"写成"已接受"。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    _fake_committed(monkeypatch, state="not_applied")

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/decision",
        headers=admin_headers,
        json={"action": "close_out_merged", "note": "结案"},
    )

    assert response.status_code == 409
    assert "并不在当前代码里" in response.json()["detail"]
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "approved_for_canary"
    store.close()


def test_close_out_merged_is_not_allowed_before_approval(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """未批准的候选不能结案（转移表不允许）—— 结案是"已经走到上线那一步"的收尾。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _seed_code_patch_candidate(store)  # candidate_testing
    _fake_committed(monkeypatch)

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/decision",
        headers=admin_headers,
        json={"action": "close_out_merged", "note": "结案"},
    )

    assert response.status_code == 409
    assert "不允许" in response.json()["detail"]
    store.close()

# ------------------------------------------------------------------
# 一键回滚：apply 的镜像，守卫是对称的。
# ------------------------------------------------------------------


def test_rollback_refuses_a_patch_that_is_not_applied(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    _fake_control(monkeypatch, state="not_applied")

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/rollback", headers=admin_headers, json={"restart": False}
    )

    assert response.status_code == 409
    assert "并未应用" in response.json()["detail"]
    store.close()


def test_rollback_refuses_a_patch_whose_change_is_committed(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """已提交的补丁要用 git revert：反向应用会留下"某次提交的逆"，谁看都糊涂。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    _fake_control(monkeypatch, state="applied", committed=True)

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/rollback", headers=admin_headers, json={"restart": False}
    )

    assert response.status_code == 409
    assert "git revert" in response.json()["detail"]
    store.close()


def test_rollback_puts_the_patch_back_when_the_reverted_tree_cannot_import(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """守卫是对称的：回滚之后导入不了 ⇒ **把补丁放回**并拒绝，绝不留下更坏的状态。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch, state="applied", smoke=(False, "ImportError: boom"))

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/rollback", headers=admin_headers, json={"restart": False}
    )

    assert response.status_code == 409
    assert "已把补丁放回" in response.json()["detail"]
    assert calls["revert"] == 1 and calls["apply"] == 1, "必须反向一次再放回一次"
    assert calls["restart"] == 0
    store.close()


def test_rollback_reverts_restarts_and_records_the_status(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch, state="applied")

    response = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/rollback", headers=admin_headers, json={"restart": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reverted"] is True
    assert body["status"] == "canary"
    assert body["steps"]["operation_status"] == "pending_restart_health_check"
    assert body["steps"]["status_after_restart"] == "rolled_back"
    assert body["steps"]["import_smoke"] is True and body["steps"]["target_tests"] is True
    assert calls["revert"] == 1 and calls["apply"] == 0
    assert calls["restart"] == 1
    # 助手必须知道这是"回滚"，失败时才应当把补丁**重新应用**而不是再反向一次。
    assert calls["spawn_kwargs"]["direction"] == "revert"
    from rag.evolution.service_control import clear_operation_marker

    operation = calls["spawn_kwargs"]["operation"]
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "canary"
    clear_operation_marker(operation["marker_path"], operation["operation_id"])
    store.close()


# ------------------------------------------------------------------
# C-11 (2026-10-22)：apply/rollback 串行在同一个租约上。
# ------------------------------------------------------------------


def _apply_lease_path():
    """The lease path the endpoints use under the suite's redirected log directory."""
    from rag.evolution.service_control import apply_lock_path

    return apply_lock_path(evolution_api.REPO_ROOT)


def test_apply_refuses_while_another_run_holds_the_lease(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """同一个租约被占用时，第二个 apply 直接 409，而不是与它并行改工作树。

    并行的后果是工作树与候选状态互相矛盾：一个请求刚把补丁应用、另一个把它反向，而两边的审计
    都写着"成功"。拒绝是**跨进程**的（同一份锁文件），所以脚本与第二个 API 进程同样被挡住。
    """
    from rag.evolution.host_lock import host_measurement_lock

    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch)

    with host_measurement_lock(_apply_lease_path()):
        response = evolution_client.post(
            f"{PREFIX}/code-candidates/{candidate_id}/apply",
            headers=admin_headers,
            json={"restart": True},
        )

    assert response.status_code == 409
    assert "另一个应用/回滚正在进行" in response.json()["detail"]
    assert calls["apply"] == 0 and calls["restart"] == 0, "被租约挡下时一步都不许走"
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "approved_for_canary"

    # 租约释放后同一条请求可以正常执行：拒绝没有留下半截状态，也不是死锁。
    released = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply",
        headers=admin_headers,
        json={"restart": True},
    )
    assert released.status_code == 200
    store.close()


def test_rollback_refuses_while_another_run_holds_the_lease(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    from rag.evolution.host_lock import host_measurement_lock

    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch, state="applied")

    with host_measurement_lock(_apply_lease_path()):
        response = evolution_client.post(
            f"{PREFIX}/code-candidates/{candidate_id}/rollback",
            headers=admin_headers,
            json={"restart": False},
        )

    assert response.status_code == 409
    assert "另一个应用/回滚正在进行" in response.json()["detail"]
    assert calls["revert"] == 0
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "approved_for_canary"
    store.close()


def test_apply_releases_the_lease_when_a_guard_refuses(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    """失败路径也必须释放租约：否则一次失败的 apply 会把后续操作永久锁在门外。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    calls = _fake_control(monkeypatch, smoke=(False, "ImportError: boom"))
    lock_path = _apply_lease_path()

    first = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply",
        headers=admin_headers,
        json={"restart": True},
    )
    assert first.status_code == 409
    assert calls["apply"] == 1 and calls["revert"] == 1
    assert not lock_path.exists(), "拒绝之后租约必须已经释放"

    second = evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply",
        headers=admin_headers,
        json={"restart": True},
    )
    assert second.status_code == 409
    assert calls["apply"] == 2, "同一个租约可以再次取得（不是被泄漏）"
    store.close()


def test_every_successful_apply_leaves_the_lease_free(
    evolution_client, admin_headers, monkeypatch, store_with_cluster
):
    store = FailureTraceStore(settings_path())
    candidate_id = _approved_patch(store)
    _fake_control(monkeypatch)
    lock_path = _apply_lease_path()

    assert evolution_client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/apply",
        headers=admin_headers,
        json={"restart": True},
    ).status_code == 200

    assert not lock_path.exists()
    store.close()


# ------------------------------------------------------------------
# 回滚之后从同一聚类重新生成（14.3 缺口二：rolled_back 是终态，接口层面没法再应用）。
# 修法 ①：不回滚状态机，改从聚类产出一条**新**候选，让它重新走一遍原有路径。
# ------------------------------------------------------------------


def _rolled_back_patch(
    store,
    cluster_id: int,
    member_id: int,
    *,
    marker: str = "旧补丁",
    statuses: tuple[str, ...] | None = None,
) -> int:
    """把一份种子补丁推进到终态（默认 ``rolled_back``：回滚后重新生成的前态）。

    ``statuses`` 让同一个构造器产出别的终态（``rejected`` / ``needs_more_evidence``）：C-08 之后
    这两种结案同样是"重新生成"的合法入口。

    同时把聚类推进到"已批准生成候选"并写下一条层级一致的成员草案——这正是回滚/驳回之后
    聚类所处的状态（结案只动候选，不动聚类）。
    """
    import time

    from rag.evolution.candidate_store import CandidateStore

    candidate_id = _seed_code_patch_candidate(store, marker=marker, observation_id=member_id)
    candidates = CandidateStore(store)
    path = statuses or (
        "awaiting_verification",
        "approved_for_canary",
        "canary",
        "rolled_back",
    )
    for status in path:
        candidates.advance_candidate(candidate_id, status, actor_id="test", note="seed")
    store.decide_cluster(
        cluster_id,
        new_status="approved_for_candidate",
        pool="candidate_pool",
        member_status="clustered",
        now=time.time(),
        confirmed_layer="code",
    )
    store.add_draft(
        cluster_id,
        sample_draft(suggested_layer="code", stage="generation"),
        observation_id=member_id,
        prompt_version="diagnosis-v10",
        model="scripted",
    )
    return candidate_id


def _fake_patch_generator(monkeypatch, *, markers=("新补丁",)):
    """把生成器换成替身：测试**绝不**调模型（真调用要几十秒且不确定）。"""
    import itertools

    import rag.evolution.api as api_module
    from rag.evolution.code_patches import CodePatchCandidate

    seen: dict = {"calls": 0, "clusters": [], "drafts": [], "layers": [], "repo_roots": []}
    circle = itertools.cycle(markers)

    class _FakeGenerator:
        prompt_version = "patch-v5"
        model_name = "scripted"

        def generate(self, cluster, drafts, *, layer, repo_root, observation_id, cluster_id, trace_store):
            seen["calls"] += 1
            seen["clusters"].append(cluster)
            seen["drafts"].append(drafts)
            seen["layers"].append(layer)
            seen["repo_roots"].append(repo_root)
            seen["observation_id"] = observation_id
            return CodePatchCandidate(
                artifact_type="code_patch",
                layer=layer,
                paths=("rag/pipeline.py",),
                diff=_seed_diff(next(circle)),
                test_paths=("tests/test_pipeline.py",),
                rationale="回滚后重新生成（替身）",
                expected_effect="无行为变化",
                rollback_ref={"base_commit": "0" * 40},
                confidence="low",
            )

    monkeypatch.setattr(api_module, "_patch_generator", lambda: _FakeGenerator())
    return seen


def _seed_diff(marker: str) -> str:
    """一份含**真实存在的内容行**的 diff（`import json` 就在 rag/pipeline.py 里）。

    只加注释行的 diff 过不了结案的内容核对（注释行被有意排除），所以这里必须带一行真实代码。
    """
    return (
        "diff --git a/rag/pipeline.py b/rag/pipeline.py\n"
        "--- a/rag/pipeline.py\n"
        "+++ b/rag/pipeline.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import asyncio\n"
        "+import json\n"
        f"+# {marker}\n"
        " import logging\n"
    )


def _regenerate(client, admin_headers, candidate_id: int, **body):
    return client.post(
        f"{PREFIX}/code-candidates/{candidate_id}/regenerate",
        headers=admin_headers,
        json=body,
    )


def test_regenerate_after_rollback_creates_a_new_pending_candidate(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """终态 rolled_back 不可复活 ⇒ 从同一聚类产出**新**候选，并留下"谁因何重生成"的审计。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)
    seen = _fake_patch_generator(monkeypatch)

    response = _regenerate(
        evolution_client, admin_headers, rolled_back_id, note="回滚后重生成演练"
    )

    assert response.status_code == 200
    body = response.json()
    new_id = body["id"]
    assert new_id != rolled_back_id
    assert body["status"] == "candidate_testing"
    assert body["cluster_id"] == cluster_id
    assert body["observation_id"] == member_id
    # 新候选必须**重新走**原有路径，而不是被当作已批准。
    assert body["status"] not in {"approved_for_canary", "canary", "stable"}
    assert body["prompt_version"] == "patch-v5" and body["model"] == "scripted"
    assert "新补丁" in body["diff"]

    # 生成器拿到的是**同一聚类 + 该观测最新且层级一致的草案**（不是空参数、不是别的聚类）。
    assert seen["calls"] == 1
    assert int(seen["clusters"][0]["id"]) == cluster_id
    assert seen["layers"] == ["code"]
    assert seen["observation_id"] == member_id
    assert seen["drafts"][0][0]["suggested_layer"] == "code"

    # 旧候选仍是终态（修法 ① 不回滚状态机的语义），新的那条带审计。
    candidates = CandidateStore(store)
    assert candidates.get_candidate(rolled_back_id)["status"] == "rolled_back"
    created = candidates.get_candidate(new_id)
    assert created["status"] == "candidate_testing"
    assert created["observation_id"] == member_id
    audit = candidates.list_audit(new_id)
    assert [entry["action"] for entry in audit] == ["generate", "advance"]
    assert "重新生成" in audit[-1]["note"]
    assert f"#{rolled_back_id}" in audit[-1]["note"]
    store.close()


def test_regenerate_refuses_while_the_cluster_has_an_open_candidate(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """堆放守卫同样生效：聚类还有未决候选时不许再生成，否则一个想法又能并排堆几份。"""
    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)
    seen = _fake_patch_generator(monkeypatch)
    first = _regenerate(evolution_client, admin_headers, rolled_back_id)
    assert first.status_code == 200  # 第一条新候选现在是 candidate_testing（未决）
    assert seen["calls"] == 1
    seen["calls"] = 0

    second = _regenerate(evolution_client, admin_headers, rolled_back_id)

    assert second.status_code == 409
    assert "已有未决" in second.json()["detail"]
    assert seen["calls"] == 0, "被守卫拦下时绝不调用模型"
    store.close()


def test_regenerate_allows_when_other_candidate_is_stable(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """已合入的 stable 补丁不再算作未决：聚类里有已结案补丁时，回滚候选依然允许重新生成。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)

    # 模拟聚类中同时存在一条已结案合入的 stable 候选
    candidates = CandidateStore(store)
    stable_id = _seed_code_patch_candidate(store, marker="已结案补丁", observation_id=member_id)
    for status in ("awaiting_verification", "approved_for_canary", "stable"):
        candidates.advance_candidate(stable_id, status, actor_id="admin", note="merged")

    seen = _fake_patch_generator(monkeypatch)
    response = _regenerate(evolution_client, admin_headers, rolled_back_id)
    assert response.status_code == 200
    assert seen["calls"] == 1
    store.close()



@pytest.mark.parametrize(
    "final_status,path",
    [
        ("rejected", ("awaiting_verification", "rejected")),
        ("needs_more_evidence", ("awaiting_verification", "needs_more_evidence")),
    ],
)
def test_regenerate_after_a_rejection_creates_a_new_candidate(
    evolution_client, admin_headers, store_with_cluster, monkeypatch, final_status, path
):
    """C-08：被驳回/要求补证据也是终态，同样需要一条"重新生成"的路。

    上一次驳回只是**结案**，不是"这个失败以后不许再处理"；入口仍然是同一聚类 + 同一观测的
    一条新候选，旧行原样留在终态（审计可追）。
    """
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    closed_id = _rolled_back_patch(store, cluster_id, member_id, statuses=path)
    seen = _fake_patch_generator(monkeypatch)

    response = _regenerate(
        evolution_client, admin_headers, closed_id, note=f"{final_status} 后重新生成"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] != closed_id
    assert body["status"] == "candidate_testing"
    assert body["observation_id"] == member_id
    assert seen["calls"] == 1

    candidates = CandidateStore(store)
    assert candidates.get_candidate(closed_id)["status"] == final_status
    audit = candidates.list_audit(body["id"])
    assert "重新生成" in audit[-1]["note"]
    assert final_status in audit[-1]["note"]
    store.close()


def test_regenerate_refuses_a_candidate_that_was_not_rolled_back(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """别的状态走原有路径：这个端点只补'回滚之后'那一段。"""
    store = FailureTraceStore(settings_path())
    candidate_id = _seed_code_patch_candidate(store)  # 停在 candidate_testing
    seen = _fake_patch_generator(monkeypatch)

    response = _regenerate(evolution_client, admin_headers, candidate_id)

    assert response.status_code == 409
    assert "只有回滚过" in response.json()["detail"]
    assert seen["calls"] == 0
    store.close()


def test_regenerate_refuses_when_the_cluster_has_no_confirmed_layer(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """没有确认层级就没有可生成的层级（人给的那条权威不能由端点自己猜）。

    ``set_confirmed_layer`` 只接受字符串（``None`` 会写成 "None"），所以这里直接用一条 SQL
    把列清空——端点读的就是这一列，而"层级未确认"正是真实系统里聚类刚被批准时的状态。
    """
    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)
    store._conn.execute(
        "UPDATE failure_clusters SET confirmed_layer = NULL WHERE id = ?", (int(cluster_id),)
    )
    store._conn.commit()
    seen = _fake_patch_generator(monkeypatch)

    response = _regenerate(evolution_client, admin_headers, rolled_back_id)

    assert response.status_code == 409
    assert "确认根因层级" in response.json()["detail"]
    assert seen["calls"] == 0
    store.close()


def test_regenerate_refuses_a_draft_that_disagrees_with_the_confirmed_layer(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """最新草案说别的层级 ⇒ 当场拒绝并说明，而不是烧一次注定被拒的模型调用。"""
    import time

    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)
    store.add_draft(
        cluster_id,
        sample_draft(suggested_layer="reranking", stage="retrieval"),
        observation_id=member_id,
        prompt_version="diagnosis-v10",
        model="scripted",
    )
    store.set_confirmed_layer(cluster_id, confirmed_layer="code", now=time.time())
    seen = _fake_patch_generator(monkeypatch)

    response = _regenerate(evolution_client, admin_headers, rolled_back_id)

    assert response.status_code == 409
    assert "reranking" in response.json()["detail"] and "code" in response.json()["detail"]
    assert seen["calls"] == 0
    store.close()


def test_regenerate_refuses_an_observation_from_another_cluster(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """跨簇观测会把一次失败的证据算到另一条候选头上 ⇒ 显式 id 也必须是本簇成员。"""
    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)
    seen = _fake_patch_generator(monkeypatch)

    response = _regenerate(evolution_client, admin_headers, rolled_back_id, observation_id=999999)

    assert response.status_code == 409
    assert "不存在" in response.json()["detail"]
    assert seen["calls"] == 0
    store.close()


def test_regenerate_reports_a_generator_refusal_without_writing_a_row(
    evolution_client, admin_headers, store_with_cluster, monkeypatch
):
    """生成器的拒绝是**答案**：409 带原因，且库里不留半条候选。"""
    import rag.evolution.api as api_module
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.patch_generator import PatchUnavailable

    store = FailureTraceStore(settings_path())
    cluster_id = store_with_cluster["cluster_id"]
    member_id = store_with_cluster["member_id"]
    rolled_back_id = _rolled_back_patch(store, cluster_id, member_id)

    class _RefusingGenerator:
        prompt_version = "patch-v5"
        model_name = "scripted"

        def generate(self, *args, **kwargs):
            raise PatchUnavailable("timeout", "provider timed out")

    monkeypatch.setattr(api_module, "_patch_generator", lambda: _RefusingGenerator())

    response = _regenerate(evolution_client, admin_headers, rolled_back_id)

    assert response.status_code == 409
    assert "timeout" in response.json()["detail"]
    candidates = CandidateStore(store)
    assert [row["id"] for row in candidates.list_candidates()] == [rolled_back_id]
    store.close()
