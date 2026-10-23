"""Regression coverage for evolution candidate lifecycle ownership and compensation."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from rag.auth import create_token
from rag.evolution import api as evolution_api
from rag.evolution import service_control
from rag.evolution.candidate_store import CandidateStore
from rag.evolution.service_control import (
    arm_operation_marker,
    create_operation_artifact,
    operation_marker_path,
    verify_operation_artifact,
)
from rag.evolution.store import FailureTraceStore
from rag.user_db import UserDB


@pytest.fixture
def lifecycle_client(tmp_path, monkeypatch):
    evolution_api.close_store()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "evolution_db_path", str(tmp_path / "evolution.db"))
    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("review-admin", "Password-123!")
    db.set_user_admin(user_id, True)
    token = create_token({"user_id": user_id, "username": "review-admin", "security_version": 1})
    headers = {"Authorization": f"Bearer {token}"}
    app = FastAPI()
    app.state.user_db = db
    app.include_router(evolution_api.router)
    with TestClient(app) as client:
        yield client, headers
    evolution_api.close_store()
    db.close()


def _seed_code_patch(store: FailureTraceStore, status: str, *, diff: str | None = None, path: str = "rag/pipeline.py") -> int:
    from rag.evolution.code_patches import CodePatchCandidate

    cluster = store.upsert_cluster(
        cluster_key="kb|retrieval|review-lifecycle",
        kb_id="kb",
        failure_stage="retrieval",
        root_cause_feature="review-lifecycle",
        seen_at=time.time(),
    )
    candidate = CodePatchCandidate(
        artifact_type="code_patch",
        layer="code",
        paths=(path,),
        diff=diff or (
            "diff --git a/rag/pipeline.py b/rag/pipeline.py\n"
            "--- a/rag/pipeline.py\n"
            "+++ b/rag/pipeline.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import asyncio\n"
            "+# review lifecycle fixture\n"
            " import logging\n"
        ),
        test_paths=("tests/test_pipeline.py",),
        rationale="lifecycle regression",
        expected_effect="no behavior change",
        rollback_ref={"base_commit": "test"},
        confidence="low",
    )
    candidates = CandidateStore(store)
    candidate_id = candidates.add_candidate(
        None, cluster["id"], candidate, prompt_version="test", model="test"
    )
    path = ("awaiting_verification",) if status == "awaiting_verification" else (
        "awaiting_verification",
        "approved_for_canary",
    )
    if status in {"canary", "rolled_back"}:
        path += ("canary",)
    if status == "rolled_back":
        path += ("rolled_back",)
    for next_status in path:
        candidates.advance_candidate(candidate_id, next_status, actor_id="seed", note="seed")
    return candidate_id


@pytest.mark.parametrize("status", ["canary", "rolled_back"])
def test_question_set_rollback_rejects_code_patch_without_state_or_audit_changes(
    lifecycle_client, status
):
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, status)
    candidates = CandidateStore(store)
    before = candidates.get_candidate(candidate_id)
    audit_before = candidates.list_audit(candidate_id)

    response = client.post(
        f"/knowledge-operations/evolution/candidates/{candidate_id}/rollback",
        headers=headers,
    )

    assert response.status_code == 404
    assert candidates.get_candidate(candidate_id)["status"] == status
    assert candidates.list_audit(candidate_id) == audit_before
    assert candidates.get_candidate(candidate_id)["gate_log"] == before["gate_log"]
    store.close()


def test_operation_snapshot_is_hash_checked_and_marker_reserves_the_handoff(tmp_path):
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha=1\n+beta=1\n"
    operation = create_operation_artifact(
        tmp_path,
        diff,
        candidate_id=7,
        database_path=tmp_path / "evolution.db",
        direction="apply",
        expected_status="canary",
        compensation_status="rolled_back",
        actor_id="admin:7",
        applied_now=True,
    )

    assert Path(operation["patch_file"]).read_text(encoding="utf-8") == diff
    assert verify_operation_artifact(operation) is True
    arm_operation_marker(operation)
    assert operation_marker_path(tmp_path).is_file()
    Path(operation["patch_file"]).chmod(0o644)
    Path(operation["patch_file"]).write_text(diff + "# tampered\n", encoding="utf-8")
    assert verify_operation_artifact(operation) is False


def test_live_code_patch_does_not_block_question_set_canary(
    lifecycle_client, monkeypatch
):
    from rag.evolution.candidates import JevQuestionSetCandidate
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    code_id = _seed_code_patch(store, "canary")
    cluster = store.upsert_cluster(
        cluster_key="kb|generation|question-set-review",
        kb_id="kb",
        failure_stage="generation",
        root_cause_feature="question-set-review",
        seen_at=time.time(),
    )
    question = JevQuestionSetCandidate(
        artifact_type="jev_question_set",
        target="preflight",
        payload={"q1": {"instructions": "adjusted"}},
        rationale="test",
        expected_effect="test",
        rollback_ref={"question_set_version": "v1", "payload_sha256": "a" * 64},
        confidence="low",
    )
    candidates = CandidateStore(store)
    question_id = candidates.add_candidate(
        None, cluster["id"], question, prompt_version="test", model="test"
    )
    candidates.advance_candidate(
        question_id, "awaiting_verification", actor_id="seed", note="seed"
    )
    monkeypatch.setattr(settings, "evolution_candidate_canary_percent", 100)
    monkeypatch.setattr(evolution_api, "_require_canaryable_payload", lambda row: None)
    monkeypatch.setattr(evolution_api, "_require_canary_entry_conditions", lambda row: None)

    try:
        response = client.post(
            f"/knowledge-operations/evolution/candidates/{question_id}/canary",
            headers=headers,
            json={"percent": 10},
        )
        assert response.status_code == 200, response.text
        assert candidates.get_candidate(code_id)["status"] == "canary"
        assert candidates.get_candidate(question_id)["status"] == "canary"
    finally:
        evolution_api.rollout.deactivate(question_id)
        store.close()


def test_code_patch_decisions_cannot_change_applied_candidate(lifecycle_client):
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "canary")
    candidates = CandidateStore(store)
    before = candidates.get_candidate(candidate_id)
    audit_before = candidates.list_audit(candidate_id)

    response = client.post(
        f"/knowledge-operations/evolution/code-candidates/{candidate_id}/decision",
        headers=headers,
        json={"action": "reject", "note": "should not mutate live patch"},
    )

    assert response.status_code == 409
    assert candidates.get_candidate(candidate_id)["status"] == "canary"
    assert candidates.list_audit(candidate_id) == audit_before
    assert candidates.get_candidate(candidate_id)["gate_log"] == before["gate_log"]
    store.close()


@pytest.mark.parametrize("action", ["approve_for_apply", "reject"])
@pytest.mark.parametrize("blocker", ["lease", "marker"])
def test_code_patch_decision_refuses_during_apply_handoff_without_audit_change(
    lifecycle_client, tmp_path, monkeypatch, action, blocker
):
    from rag.evolution.host_lock import host_measurement_lock

    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "awaiting_verification")
    candidates = CandidateStore(store)
    before = candidates.get_candidate(candidate_id)
    audit_before = candidates.list_audit(candidate_id)
    monkeypatch.setattr(evolution_api, "REPO_ROOT", tmp_path)

    operation = None
    if blocker == "marker":
        operation = create_operation_artifact(
            tmp_path,
            before["diff"],
            candidate_id=candidate_id,
            database_path=settings.evolution_db_path,
            direction="apply",
            expected_status="canary",
            compensation_status="rolled_back",
            actor_id="admin:review",
            applied_now=True,
        )
        arm_operation_marker(operation)

    def submit():
        return client.post(
            f"/knowledge-operations/evolution/code-candidates/{candidate_id}/decision",
            headers=headers,
            json={"action": action, "note": "must be serialized with apply"},
        )

    try:
        if blocker == "lease":
            with host_measurement_lock(service_control.apply_lock_path(tmp_path)):
                response = submit()
        else:
            response = submit()
        assert response.status_code == 409
        assert candidates.get_candidate(candidate_id)["status"] == "awaiting_verification"
        assert candidates.list_audit(candidate_id) == audit_before
        assert candidates.get_candidate(candidate_id)["gate_log"] == before["gate_log"]
    finally:
        if operation is not None:
            service_control.clear_operation_marker(
                operation["marker_path"], operation["operation_id"]
            )
        store.close()


def test_closeout_is_refused_while_restart_helper_owns_handoff_marker(
    lifecycle_client, tmp_path, monkeypatch
):
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "canary")
    candidates = CandidateStore(store)
    audit_before = candidates.list_audit(candidate_id)
    monkeypatch.setattr(evolution_api, "REPO_ROOT", tmp_path)
    operation = create_operation_artifact(
        tmp_path,
        candidates.get_candidate(candidate_id)["diff"],
        candidate_id=candidate_id,
        database_path=settings.evolution_db_path,
        direction="revert",
        expected_status="canary",
        compensation_status="canary",
        actor_id="admin:review",
        applied_now=False,
    )
    arm_operation_marker(operation)

    try:
        response = client.post(
            f"/knowledge-operations/evolution/code-candidates/{candidate_id}/decision",
            headers=headers,
            json={"action": "close_out_merged", "note": "do not race helper"},
        )
        assert response.status_code == 409
        assert candidates.get_candidate(candidate_id)["status"] == "canary"
        assert candidates.list_audit(candidate_id) == audit_before
    finally:
        service_control.clear_operation_marker(
            operation["marker_path"], operation["operation_id"]
        )
        store.close()


def _helper_fixture(tmp_path, diff: str):
    repo = tmp_path / "repo"
    (repo / "rag").mkdir(parents=True)
    target = repo / "rag" / "demo.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    db_path = tmp_path / "helper-evolution.db"
    store = FailureTraceStore(db_path)
    cluster = store.upsert_cluster(
        cluster_key="kb|retrieval|helper",
        kb_id="kb",
        failure_stage="retrieval",
        root_cause_feature="helper",
        seen_at=time.time(),
    )
    from rag.evolution.code_patches import CodePatchCandidate

    candidate = CodePatchCandidate(
        artifact_type="code_patch",
        layer="code",
        paths=("rag/demo.py",),
        diff=diff,
        test_paths=("test_demo.py",),
        rationale="helper test",
        expected_effect="helper test",
        rollback_ref={"base_commit": "test"},
        confidence="low",
    )
    candidates = CandidateStore(store)
    candidate_id = candidates.add_candidate(
        None, cluster["id"], candidate, prompt_version="test", model="test"
    )
    for status in ("awaiting_verification", "approved_for_canary", "canary"):
        candidates.advance_candidate(candidate_id, status, actor_id="seed", note="seed")
    return repo, db_path, store, candidates, candidate_id, target


def _run_helper_source(monkeypatch, command: dict, **overrides):
    namespace = {"__name__": "evolution_helper_test"}
    monkeypatch.setattr(sys, "argv", ["helper", json.dumps(command)])
    exec(compile(service_control.HELPER_SOURCE, "helper-under-test", "exec"), namespace)
    namespace.update(overrides)
    return namespace


def test_helper_failure_reverses_new_apply_and_audits_rolled_back(
    tmp_path, monkeypatch
):
    diff = (
        "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n"
        "-alpha = 1\n+beta = 1\n"
    )
    repo, db_path, store, candidates, candidate_id, target = _helper_fixture(tmp_path, diff)
    service_control.apply_patch(repo, diff)
    operation = create_operation_artifact(
        repo,
        diff,
        candidate_id=candidate_id,
        database_path=db_path,
        direction="apply",
        expected_status="canary",
        compensation_status="rolled_back",
        actor_id="admin:review",
        applied_now=True,
    )
    arm_operation_marker(operation)
    command = {
        **operation,
        "root": str(repo),
        "log": str(tmp_path / "helper.log"),
        "lock": str(service_control.apply_lock_path(repo)),
        "lock_wait": 0,
        "candidate_id": candidate_id,
        "test_scope": "full",
        "timeout": 1,
    }
    helper = _run_helper_source(
        monkeypatch, command,
        full_suite_passes=lambda: False,
        stop_service=lambda: pytest.fail("must not restart after a failed suite"),
    )

    helper["main"]()

    assert "alpha = 1" in target.read_text(encoding="utf-8")
    assert candidates.get_candidate(candidate_id)["status"] == "rolled_back"
    assert "helper补偿" in candidates.list_audit(candidate_id)[-1]["note"]
    assert not Path(operation["marker_path"]).exists()
    store.close()


def test_helper_failed_suite_keeps_a_preexisting_canary_patch(tmp_path, monkeypatch):
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    repo, db_path, store, candidates, candidate_id, target = _helper_fixture(tmp_path, diff)
    service_control.apply_patch(repo, diff)
    operation = create_operation_artifact(
        repo, diff, candidate_id=candidate_id, database_path=db_path,
        direction="apply", expected_status="canary", compensation_status="rolled_back",
        actor_id="admin:review", applied_now=False,
    )
    arm_operation_marker(operation)
    command = {
        **operation, "root": str(repo), "log": str(tmp_path / "helper.log"),
        "lock": str(service_control.apply_lock_path(repo)), "lock_wait": 0,
        "candidate_id": candidate_id, "test_scope": "full", "timeout": 1,
    }
    helper = _run_helper_source(
        monkeypatch, command, full_suite_passes=lambda: False,
        stop_service=lambda: pytest.fail("must not stop service after failed suite"),
    )

    helper["main"]()

    assert "beta = 1" in target.read_text(encoding="utf-8")
    assert candidates.get_candidate(candidate_id)["status"] == "canary"
    assert not Path(operation["marker_path"]).exists()
    store.close()


def test_helper_start_failure_compensates_then_restarts_previous_service(tmp_path, monkeypatch):
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    repo, db_path, store, candidates, candidate_id, target = _helper_fixture(tmp_path, diff)
    service_control.apply_patch(repo, diff)
    operation = create_operation_artifact(
        repo, diff, candidate_id=candidate_id, database_path=db_path,
        direction="apply", expected_status="canary", compensation_status="rolled_back",
        actor_id="admin:review", applied_now=True,
    )
    arm_operation_marker(operation)
    starts = {"count": 0}

    def start():
        starts["count"] += 1
        if starts["count"] == 1:
            raise OSError("new service did not spawn")
        return types.SimpleNamespace(poll=lambda: 0)

    command = {
        **operation, "root": str(repo), "log": str(tmp_path / "helper.log"),
        "lock": str(service_control.apply_lock_path(repo)), "lock_wait": 0,
        "candidate_id": candidate_id, "test_scope": "target", "timeout": 1,
    }
    helper = _run_helper_source(
        monkeypatch, command, stop_service=lambda: True, port_free=lambda: True,
        start=start, healthy=lambda: True,
    )

    helper["main"]()

    assert starts["count"] == 2
    assert "alpha = 1" in target.read_text(encoding="utf-8")
    assert candidates.get_candidate(candidate_id)["status"] == "rolled_back"
    assert not Path(operation["marker_path"]).exists()
    store.close()


def test_helper_refuses_a_tampered_snapshot_and_keeps_the_marker(tmp_path, monkeypatch):
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    repo, db_path, store, candidates, candidate_id, target = _helper_fixture(tmp_path, diff)
    service_control.apply_patch(repo, diff)
    operation = create_operation_artifact(
        repo,
        diff,
        candidate_id=candidate_id,
        database_path=db_path,
        direction="apply",
        expected_status="canary",
        compensation_status="rolled_back",
        actor_id="admin:review",
        applied_now=True,
    )
    arm_operation_marker(operation)
    Path(operation["patch_file"]).chmod(0o644)
    Path(operation["patch_file"]).write_text(diff + "# changed\n", encoding="utf-8")
    command = {
        **operation,
        "root": str(repo),
        "log": str(tmp_path / "helper.log"),
        "lock": str(service_control.apply_lock_path(repo)),
        "lock_wait": 0,
        "candidate_id": candidate_id,
        "test_scope": "full",
        "timeout": 1,
    }
    helper = _run_helper_source(
        monkeypatch, command,
        run=lambda: pytest.fail("must stop on hash mismatch"),
    )

    helper["main"]()

    assert "beta = 1" in target.read_text(encoding="utf-8")
    assert candidates.get_candidate(candidate_id)["status"] == "canary"
    assert Path(operation["marker_path"]).is_file()
    store.close()


def test_helper_finalizes_rollback_only_after_healthy_service(tmp_path, monkeypatch):
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    repo, db_path, store, candidates, candidate_id, target = _helper_fixture(tmp_path, diff)
    service_control.apply_patch(repo, diff)
    service_control.revert_patch(repo, diff)
    operation = create_operation_artifact(
        repo,
        diff,
        candidate_id=candidate_id,
        database_path=db_path,
        direction="revert",
        expected_status="canary",
        compensation_status="canary",
        actor_id="admin:review",
        applied_now=False,
    )
    arm_operation_marker(operation)
    command = {
        **operation,
        "success_status": "rolled_back",
        "root": str(repo),
        "log": str(tmp_path / "helper.log"),
        "lock": str(service_control.apply_lock_path(repo)),
        "lock_wait": 0,
        "candidate_id": candidate_id,
        "test_scope": "target",
        "timeout": 1,
    }
    helper = _run_helper_source(
        monkeypatch,
        command,
        stop_service=lambda: True,
        port_free=lambda: True,
        start=lambda: types.SimpleNamespace(poll=lambda: 0),
        healthy=lambda: True,
    )

    helper["main"]()

    assert "alpha = 1" in target.read_text(encoding="utf-8")
    assert candidates.get_candidate(candidate_id)["status"] == "rolled_back"
    assert "helper完成" in candidates.list_audit(candidate_id)[-1]["note"]
    assert not Path(operation["marker_path"]).exists()
    store.close()


def test_failed_helper_compensation_keeps_marker_and_does_not_continue(
    tmp_path, monkeypatch
):
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    repo, db_path, store, candidates, candidate_id, target = _helper_fixture(tmp_path, diff)
    service_control.apply_patch(repo, diff)
    operation = create_operation_artifact(
        repo,
        diff,
        candidate_id=candidate_id,
        database_path=db_path,
        direction="apply",
        expected_status="canary",
        compensation_status="rolled_back",
        actor_id="admin:review",
        applied_now=True,
    )
    arm_operation_marker(operation)
    command = {
        **operation,
        "root": str(repo),
        "log": str(tmp_path / "helper.log"),
        "lock": str(service_control.apply_lock_path(repo)),
        "lock_wait": 0,
        "candidate_id": candidate_id,
        "test_scope": "full",
        "timeout": 1,
    }
    helper = _run_helper_source(
        monkeypatch,
        command,
        full_suite_passes=lambda: False,
        stop_service=lambda: pytest.fail("must stop after failed compensation"),
    )
    helper["subprocess"] = types.SimpleNamespace(
        run=lambda *args, **kwargs: types.SimpleNamespace(returncode=1, stdout="", stderr="apply failed")
    )

    helper["main"]()

    assert "beta = 1" in target.read_text(encoding="utf-8")
    assert candidates.get_candidate(candidate_id)["status"] == "canary"
    assert Path(operation["marker_path"]).is_file()
    assert "compensation failed" in (tmp_path / "helper.log").read_text(encoding="utf-8")
    store.close()


def test_apply_marker_blocks_requests_until_helper_takes_the_lease(
    lifecycle_client, monkeypatch
):
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "approved_for_canary")
    calls = {"restart": 0}
    monkeypatch.setattr(service_control, "patch_state", lambda *args, **kwargs: "not_applied")
    monkeypatch.setattr(service_control, "apply_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(service_control, "revert_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(service_control, "import_smoke", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(service_control, "run_target_tests", lambda *args, **kwargs: (True, ""))

    def spawn(**kwargs):
        calls["restart"] += 1
        calls["operation"] = kwargs["operation"]
        return "helper.py"

    monkeypatch.setattr(service_control, "spawn_restart_helper", spawn)
    try:
        response = client.post(
            f"/knowledge-operations/evolution/code-candidates/{candidate_id}/apply",
            headers=headers,
            json={"restart": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["steps"]["operation_status"] == "pending_restart_health_check"
        assert CandidateStore(store).get_candidate(candidate_id)["status"] == "canary"
        operation = calls["operation"]
        assert Path(operation["patch_file"]).is_file()
        assert operation["database_path"] == str(Path(settings.evolution_db_path).resolve())
        assert Path(operation["marker_path"]).is_file()

        blocked = client.post(
            f"/knowledge-operations/evolution/code-candidates/{candidate_id}/apply",
            headers=headers,
            json={"restart": True},
        )
        assert blocked.status_code == 409
        assert calls["restart"] == 1
    finally:
        if calls.get("operation"):
            service_control.clear_operation_marker(
                calls["operation"]["marker_path"], calls["operation"]["operation_id"]
            )
        store.close()


def test_helper_spawn_failure_compensates_new_apply_and_persists_terminal_status(
    lifecycle_client, monkeypatch
):
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "approved_for_canary")
    calls = {"apply": 0, "revert": 0}
    monkeypatch.setattr(service_control, "patch_state", lambda *args, **kwargs: "not_applied")
    monkeypatch.setattr(
        service_control,
        "apply_patch",
        lambda *args, **kwargs: calls.__setitem__("apply", calls["apply"] + 1),
    )
    monkeypatch.setattr(
        service_control,
        "revert_patch",
        lambda *args, **kwargs: calls.__setitem__("revert", calls["revert"] + 1),
    )
    monkeypatch.setattr(service_control, "import_smoke", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(service_control, "run_target_tests", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(
        service_control,
        "spawn_restart_helper",
        lambda **kwargs: (_ for _ in ()).throw(OSError("helper cannot start")),
    )

    response = client.post(
        f"/knowledge-operations/evolution/code-candidates/{candidate_id}/apply",
        headers=headers,
        json={"restart": True},
    )

    assert response.status_code == 500
    assert calls == {"apply": 1, "revert": 1}
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "rolled_back"
    assert not service_control.operation_marker_path(evolution_api.REPO_ROOT).exists()
    assert "补偿应用" in CandidateStore(store).list_audit(candidate_id)[-1]["note"]
    store.close()


def test_apply_spawn_failure_restores_real_git_tree_from_verified_snapshot(
    lifecycle_client, tmp_path, monkeypatch
):
    client, headers = lifecycle_client
    repo = tmp_path / "repo"
    (repo / "rag").mkdir(parents=True)
    target = repo / "rag" / "demo.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    monkeypatch.setattr(evolution_api, "REPO_ROOT", repo)
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "approved_for_canary", diff=diff, path="rag/demo.py")
    monkeypatch.setattr(service_control, "import_smoke", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(service_control, "run_target_tests", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(
        service_control, "spawn_restart_helper",
        lambda **kwargs: (_ for _ in ()).throw(OSError("helper cannot start")),
    )

    response = client.post(
        f"/knowledge-operations/evolution/code-candidates/{candidate_id}/apply",
        headers=headers,
        json={"restart": True},
    )

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "alpha = 1\n"
    assert service_control.patch_state(repo, diff) == "not_applied"
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "rolled_back"
    store.close()


def test_apply_artifact_write_failure_does_not_mutate_real_git_tree(
    lifecycle_client, tmp_path, monkeypatch
):
    client, headers = lifecycle_client
    repo = tmp_path / "repo"
    (repo / "rag").mkdir(parents=True)
    target = repo / "rag" / "demo.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    monkeypatch.setattr(evolution_api, "REPO_ROOT", repo)
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "approved_for_canary", diff=diff, path="rag/demo.py")
    monkeypatch.setattr(
        service_control, "create_operation_artifact",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("artifact disk failure")),
    )

    response = client.post(
        f"/knowledge-operations/evolution/code-candidates/{candidate_id}/apply",
        headers=headers,
        json={"restart": True},
    )

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "alpha = 1\n"
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "approved_for_canary"
    store.close()


def test_rollback_spawn_failure_restores_real_git_tree_from_verified_snapshot(
    lifecycle_client, tmp_path, monkeypatch
):
    client, headers = lifecycle_client
    repo = tmp_path / "repo"
    (repo / "rag").mkdir(parents=True)
    target = repo / "rag" / "demo.py"
    target.write_text("alpha = 1\n", encoding="utf-8")
    for args in (
        ("init", "-q"),
        ("add", "-A"),
        ("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    diff = "--- a/rag/demo.py\n+++ b/rag/demo.py\n@@ -1 +1 @@\n-alpha = 1\n+beta = 1\n"
    service_control.apply_patch(repo, diff)
    monkeypatch.setattr(evolution_api, "REPO_ROOT", repo)
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "canary", diff=diff, path="rag/demo.py")
    monkeypatch.setattr(
        service_control, "spawn_restart_helper",
        lambda **kwargs: (_ for _ in ()).throw(OSError("helper cannot start")),
    )
    monkeypatch.setattr(service_control, "import_smoke", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(service_control, "run_target_tests", lambda *args, **kwargs: (True, ""))

    response = client.post(
        f"/knowledge-operations/evolution/code-candidates/{candidate_id}/rollback",
        headers=headers,
        json={"restart": True},
    )

    assert response.status_code == 500
    assert target.read_text(encoding="utf-8") == "beta = 1\n"
    assert service_control.patch_state(repo, diff) == "applied"
    assert CandidateStore(store).get_candidate(candidate_id)["status"] == "canary"
    store.close()


def test_restart_requested_rollback_stays_canary_until_helper_health(
    lifecycle_client, monkeypatch
):
    client, headers = lifecycle_client
    store = FailureTraceStore(settings.evolution_db_path)
    candidate_id = _seed_code_patch(store, "canary")
    captured = {}
    monkeypatch.setattr(service_control, "patch_state", lambda *args, **kwargs: "applied")
    monkeypatch.setattr(service_control, "patch_paths", lambda diff: ("rag/pipeline.py",))
    monkeypatch.setattr(service_control, "patch_is_committed", lambda *args, **kwargs: False)
    monkeypatch.setattr(service_control, "revert_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(service_control, "apply_patch", lambda *args, **kwargs: None)
    monkeypatch.setattr(service_control, "import_smoke", lambda *args, **kwargs: (True, ""))
    monkeypatch.setattr(service_control, "run_target_tests", lambda *args, **kwargs: (True, ""))

    def spawn(**kwargs):
        captured.update(kwargs)
        return "helper.py"

    monkeypatch.setattr(service_control, "spawn_restart_helper", spawn)
    try:
        response = client.post(
            f"/knowledge-operations/evolution/code-candidates/{candidate_id}/rollback",
            headers=headers,
            json={"restart": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["steps"]["operation_status"] == "pending_restart_health_check"
        assert response.json()["status"] == "canary"
        assert response.json()["steps"]["status_after_restart"] == "rolled_back"
        assert CandidateStore(store).get_candidate(candidate_id)["status"] == "canary"
        assert captured["direction"] == "revert"
        assert captured["operation"]["success_status"] == "rolled_back"
    finally:
        if captured:
            service_control.clear_operation_marker(
                captured["operation"]["marker_path"], captured["operation"]["operation_id"]
            )
        store.close()
