"""Task 4: read-only evolution observation API.

The phase-2 API surface is administrator-only and read-only: four GET endpoints
under ``/knowledge-operations/evolution`` and nothing that can move an
observation or a cluster through the state machine. The tests mount the router
on a private app (``rag.api`` mounts it once in production) and assert the
authorization boundary, the pagination shape, and that serialization carries
stored signals only — never question text or chunk text.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rag.auth import create_token
from rag.evolution import api as evolution_api
from rag.evolution.models import FailureObservation
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
    assert "POST" not in {m for path in body["endpoints"] for m in path["methods"]}
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
