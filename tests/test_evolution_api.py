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
