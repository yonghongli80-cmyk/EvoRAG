"""Task 4: the code-patch review endpoints and the administrator export.

Layer 4 does not exist for a code patch (design §2): there is no second instance
to serve a percentage of traffic, so the review **is** the release gate. These
tests pin that gate through the API:

* the two reads expose the stored signals, the diff body, the five-layer trail
  and the audit rows — and the list read deliberately drops the diff body, which
  is file content a reviewer opens on the detail response;
* ``approve_for_apply`` **exports** ``artifacts/patches/candidate-<id>.patch``
  (a header comment with the apply command and the base commit, then the diff)
  and advances the candidate to ``approved_for_canary`` — the endpoint never
  runs ``git apply``, so the main work tree is byte-for-byte unchanged, which the
  tests pin with ``git status --porcelain`` plus the target file's own content;
* ``reject`` and ``needs_revision`` are the other two audited moves;
* 403 (member) / 401 (anonymous) / 400 (unknown action) / 404 (unknown candidate,
  missing database) / 409 (illegal transition, duplicated decision) write no
  audit row, so no state change can exist without a trail and no trail can exist
  without one.

The last test pins the phase-4 compatibility split: ``GET /candidates`` keeps
serving question sets only (its frontend contract reads ``target``/``payload``,
both NULL for a code patch), while the new ``/code-candidates`` reads serve the
patch rows.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings
from rag.auth import create_token
from rag.evolution import api as evolution_api
from rag.evolution.candidate_store import CandidateStore
from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.code_patches import CodePatchCandidate
from rag.evolution.models import EvolutionStatus, FailureObservation
from rag.evolution.store import FailureTraceStore
from rag.user_db import UserDB

PREFIX = "/knowledge-operations/evolution"
CANDIDATES_URL = f"{PREFIX}/candidates"
CODE_CANDIDATES_URL = f"{PREFIX}/code-candidates"
GENERATION_QUEUE_URL = f"{PREFIX}/code-generation-queue"

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_FILE = REPO_ROOT / "rag" / "reranker.py"

BASE_COMMIT = "a" * 40
# One small surgical diff over the only file the reranking layer may touch. It is
# stored verbatim (the store does not validate) and never applied: the review
# endpoint exports it, and the test pins the target file's own bytes.
PATCH_DIFF = (
    "--- a/rag/reranker.py\n"
    "+++ b/rag/reranker.py\n"
    "@@ -1,3 +1,4 @@\n"
    " def rerank(query, chunks):\n"
    "     return chunks\n"
    "+    # candidate tweak: score spread below the collision margin\n"
)


# ------------------------------------------------------------------
# Fixtures: the private app and the stored candidates.
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
def export_dir(tmp_path, monkeypatch):
    """Export into a temporary directory; the default is the repo's artifacts/."""
    directory = tmp_path / "patches"
    monkeypatch.setattr(evolution_api, "PATCH_EXPORT_DIR", directory)
    return directory


def _headers(db: UserDB, username: str, *, is_admin: bool = False) -> dict:
    user_id = db.create_user(username, "Password-123!")
    if is_admin:
        db.set_user_admin(user_id, True)
    token = create_token({"user_id": user_id, "username": username, "security_version": 1})
    return {"Authorization": f"Bearer {token}"}


def _admin_id(client: TestClient) -> str:
    row = client.app.state.user_db.get_user_by_username("admin")
    return str(row["id"])


def sample_patch(**kw) -> CodePatchCandidate:
    data = dict(
        artifact_type="code_patch",
        layer="reranking",
        paths=("rag/reranker.py",),
        diff=PATCH_DIFF,
        test_paths=("tests/test_reranker.py",),
        rationale="重排序分数区分度低，词面撞名淹没真实候选",
        expected_effect="同义候选排在撞名候选之前",
        rollback_ref={"base_commit": BASE_COMMIT},
        confidence="medium",
    )
    data.update(kw)
    return CodePatchCandidate(**data)


def sample_question_set(**kw) -> JevQuestionSetCandidate:
    """A phase-4 question-set candidate, for the ``GET /candidates`` split."""
    payload = {
        "intent": {"instructions": "判断用户问题的主要意图；只依据问题本身与知识库能力摘要。"},
        "complexity": {},
        "needs_query_rewrite": {},
    }
    data = dict(
        artifact_type="jev_question_set",
        target="preflight",
        payload=payload,
        rationale="intent 判据在结构化查询边界与线上分歧集中",
        expected_effect="结构化查询意图判定更稳定",
        rollback_ref={"question_set_version": "preflight-v1", "payload_sha256": "c" * 64},
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
        root_cause_feature="ranking_error",
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
        member["id"], cluster["id"],
        failure_stage="retrieval", root_cause_feature="ranking_error", now=now,
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


def _add_candidate(store: FailureTraceStore, candidate, key: str, *, status: str | None):
    """Persist one candidate (patch or question set), optionally advanced."""
    cluster_id, member_id = _approved_cluster(store, key)
    candidates = CandidateStore(store)
    candidate_id = candidates.add_candidate(
        member_id, cluster_id, candidate, prompt_version="patch-v1", model="test-model",
    )
    if status is not None:
        candidates.advance_candidate(
            candidate_id, status, actor_id="gate-sandbox", note="沙箱：apply 通过，目标测试通过",
            gate="sandbox", gate_detail="applied=True tests_passed=True",
        )
    return candidate_id


@pytest.fixture
def patch_ready(evolution_client) -> int:
    """One code patch in ``awaiting_verification``: layers 1-3 concluded."""
    store = FailureTraceStore(settings.evolution_db_path)
    try:
        return _add_candidate(
            store, sample_patch(), "patch-one", status=EvolutionStatus.AWAITING_VERIFICATION.value
        )
    finally:
        store.close()


@pytest.fixture
def patch_testing(evolution_client) -> int:
    """One fresh patch still inside ``candidate_testing`` (the sandbox never ran)."""
    store = FailureTraceStore(settings.evolution_db_path)
    try:
        return _add_candidate(store, sample_patch(), "patch-two", status=None)
    finally:
        store.close()


def _store() -> FailureTraceStore:
    """A separate writer handle on the test database (the API keeps its own)."""
    return FailureTraceStore(settings.evolution_db_path)


def _candidate(candidate_id: int) -> dict:
    store = _store()
    try:
        return CandidateStore(store).get_candidate(candidate_id)
    finally:
        store.close()


def _candidate_audit(candidate_id: int) -> list[dict]:
    store = _store()
    try:
        return CandidateStore(store).list_audit(candidate_id)
    finally:
        store.close()


def _decision(client: TestClient, candidate_id: int, action: str, headers: dict, note: str = ""):
    return client.post(
        f"{CODE_CANDIDATES_URL}/{candidate_id}/decision",
        json={"action": action, "note": note},
        headers=headers,
    )


def _worktree_state() -> list[str]:
    """``git status --porcelain`` of the main work tree, pinned by Task 4."""
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.splitlines()


# ------------------------------------------------------------------
# The two reads.
# ------------------------------------------------------------------


def test_code_candidates_list_reports_the_patch_without_the_diff(
    evolution_client, admin_headers, patch_ready
):
    page = evolution_client.get(CODE_CANDIDATES_URL, headers=admin_headers).json()

    assert page["total"] == 1
    item = page["items"][0]
    assert set(item) >= {
        "id", "observation_id", "cluster_id", "artifact_type", "layer", "paths",
        "test_paths", "status", "pool", "confidence", "prompt_version", "model",
        "rationale", "expected_effect", "rollback_ref", "sandbox_summary",
        "created_at", "updated_at",
    }
    assert item["id"] == patch_ready
    assert item["artifact_type"] == "code_patch"
    assert item["layer"] == "reranking"
    # ``paths`` is not a stored column: the diff headers are the authority layer 1
    # validated against, and they are what the list row reports.
    assert item["paths"] == ["rag/reranker.py"]
    assert item["test_paths"] == ["tests/test_reranker.py"]
    assert item["status"] == "awaiting_verification"
    assert item["confidence"] == "medium"
    assert item["rollback_ref"]["base_commit"] == BASE_COMMIT
    # The sandbox conclusion of layers 2/3 is summarized on the row; the trail and
    # the audit list stay on the detail response.
    assert [entry["gate"] for entry in item["sandbox_summary"]] == ["sandbox"]
    assert item["sandbox_summary"][0]["detail"] == "applied=True tests_passed=True"
    # The diff body is file content: never on a list row.
    assert "diff" not in item
    assert PATCH_DIFF not in evolution_client.get(CODE_CANDIDATES_URL, headers=admin_headers).text

    by_status = evolution_client.get(
        CODE_CANDIDATES_URL, headers=admin_headers, params={"status": "candidate_testing"}
    ).json()
    assert by_status["items"] == []
    assert evolution_client.get(
        CODE_CANDIDATES_URL, headers=admin_headers, params={"status": "not-a-status"}
    ).status_code == 400

    paged = evolution_client.get(
        CODE_CANDIDATES_URL, headers=admin_headers, params={"page": 2, "page_size": 1}
    ).json()
    assert paged == {"items": [], "total": 1, "page": 2, "page_size": 1}


def test_code_candidate_detail_carries_the_diff_the_trail_and_the_audit(
    evolution_client, admin_headers, patch_ready
):
    detail = evolution_client.get(
        f"{CODE_CANDIDATES_URL}/{patch_ready}", headers=admin_headers
    ).json()

    assert detail["diff"] == PATCH_DIFF
    assert detail["paths"] == ["rag/reranker.py"]
    assert detail["test_paths"] == ["tests/test_reranker.py"]
    assert [entry["gate"] for entry in detail["gate_log"]] == ["scope", "sandbox"]
    assert detail["gate_log"][0]["detail"] == "code_patch/reranking files=1"
    assert detail["gate_log"][1]["result"] == "passed"
    assert [row["action"] for row in detail["audit"]] == ["generate", "advance"]
    assert detail["audit"][0]["actor_id"] == "test-model"
    assert detail["rationale"] == "重排序分数区分度低，词面撞名淹没真实候选"
    assert detail["expected_effect"] == "同义候选排在撞名候选之前"


def test_code_candidate_detail_on_an_unknown_patch_is_not_found(
    evolution_client, admin_headers, patch_ready
):
    assert evolution_client.get(f"{CODE_CANDIDATES_URL}/999", headers=admin_headers).status_code == 404


# ------------------------------------------------------------------
# The authorization boundary (every evolution endpoint is admin-only).
# ------------------------------------------------------------------


def test_every_code_candidate_endpoint_is_administrator_only(
    evolution_client, user_headers, admin_headers, patch_ready
):
    requests = (
        ("get", CODE_CANDIDATES_URL, None),
        ("get", f"{CODE_CANDIDATES_URL}/{patch_ready}", None),
        ("post", f"{CODE_CANDIDATES_URL}/{patch_ready}/decision", {"action": "approve_for_apply"}),
    )
    for method, url, body in requests:
        response = getattr(evolution_client, method)(
            url, headers=user_headers, **({} if body is None else {"json": body})
        )
        assert response.status_code == 403, url
    assert evolution_client.get(CODE_CANDIDATES_URL).status_code == 401
    assert evolution_client.get(f"{CODE_CANDIDATES_URL}/{patch_ready}").status_code == 401
    assert evolution_client.post(
        f"{CODE_CANDIDATES_URL}/{patch_ready}/decision", json={"action": "reject"}
    ).status_code == 401
    assert evolution_client.get(CODE_CANDIDATES_URL, headers=admin_headers).status_code == 200


# ------------------------------------------------------------------
# approve_for_apply: export only, never apply.
# ------------------------------------------------------------------


def test_approve_exports_the_patch_and_advances_without_touching_the_work_tree(
    evolution_client, admin_headers, patch_ready, export_dir
):
    before_tree = _worktree_state()
    before_target = TARGET_FILE.read_bytes()

    response = _decision(
        evolution_client, patch_ready, "approve_for_apply", admin_headers,
        note="基准回放已复核，撞名问题由重排序分数间距修复",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved_for_canary"
    assert body["diff"] == PATCH_DIFF
    # The main work tree is untouched: no apply, no commit, no branch switch.
    assert _worktree_state() == before_tree
    assert TARGET_FILE.read_bytes() == before_target
    assert "candidate tweak" not in TARGET_FILE.read_text(encoding="utf-8")

    exported = export_dir / f"candidate-{patch_ready}.patch"
    assert exported.is_file()
    content = exported.read_text(encoding="utf-8")
    # The header comment carries the apply command and the base commit; the diff
    # body follows it verbatim.
    assert content.startswith("#")
    assert BASE_COMMIT in content
    assert f"candidate-{patch_ready}.patch" in content
    assert "git" in content and "apply --3way" in content
    assert "python -m pytest tests/test_reranker.py" in content
    assert content.endswith(PATCH_DIFF)

    audit = _candidate_audit(patch_ready)
    assert [row["action"] for row in audit] == ["generate", "advance", "advance"]
    assert audit[-1]["actor_id"] == _admin_id(evolution_client)
    # The audit note names the export path and the apply command the administrator
    # runs by hand, plus the note they typed.
    assert str(exported) in audit[-1]["note"]
    assert "apply --3way" in audit[-1]["note"]
    assert "基准回放已复核" in audit[-1]["note"]

    trail = _candidate(patch_ready)["gate_log"]
    assert [entry["gate"] for entry in trail] == ["scope", "sandbox", "review"]
    assert trail[-1]["detail"] == "approved_for_apply"
    assert trail[-1]["result"] == "passed"
    # The candidate keeps its diff: exporting never consumed or rewrote it.
    assert _candidate(patch_ready)["status"] == "approved_for_canary"


def test_approve_creates_the_export_directory_when_it_is_missing(
    evolution_client, admin_headers, patch_ready, tmp_path, monkeypatch
):
    nested = tmp_path / "artifacts" / "patches"
    monkeypatch.setattr(evolution_api, "PATCH_EXPORT_DIR", nested)

    assert _decision(evolution_client, patch_ready, "approve_for_apply", admin_headers).status_code == 200
    assert (nested / f"candidate-{patch_ready}.patch").is_file()


def test_the_default_export_directory_is_the_repository_artifacts_path():
    """``artifacts/patches`` is the documented, locally ignored export location."""
    assert evolution_api.PATCH_EXPORT_DIR == REPO_ROOT / "artifacts" / "patches"


def test_approve_refuses_a_patch_whose_diff_body_is_missing(
    evolution_client, admin_headers, export_dir
):
    """A row without a diff cannot be exported: refuse instead of writing an empty file."""
    store = _store()
    try:
        candidate_id = _add_candidate(
            store, sample_patch(diff=""), "patch-empty",
            status=EvolutionStatus.AWAITING_VERIFICATION.value,
        )
    finally:
        store.close()

    response = _decision(evolution_client, candidate_id, "approve_for_apply", admin_headers)

    assert response.status_code == 409
    assert not (export_dir / f"candidate-{candidate_id}.patch").exists()
    assert [row["action"] for row in _candidate_audit(candidate_id)] == ["generate", "advance"]
    assert _candidate(candidate_id)["status"] == "awaiting_verification"


# ------------------------------------------------------------------
# reject and needs_revision.
# ------------------------------------------------------------------


def test_reject_moves_the_patch_to_rejected_and_writes_no_export(
    evolution_client, admin_headers, patch_ready, export_dir
):
    response = _decision(
        evolution_client, patch_ready, "reject", admin_headers, note="补丁只改注释，未触及分数间距"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert not list(export_dir.glob("*.patch"))
    audit = _candidate_audit(patch_ready)
    assert [row["action"] for row in audit] == ["generate", "advance", "reject"]
    assert audit[-1]["note"] == "补丁只改注释，未触及分数间距"
    trail = _candidate(patch_ready)["gate_log"]
    assert [entry["gate"] for entry in trail] == ["scope", "sandbox", "review"]
    assert trail[-1]["detail"] == "reject"
    assert trail[-1]["result"] == "failed"


def test_needs_revision_moves_the_patch_to_needs_more_evidence(
    evolution_client, admin_headers, patch_ready
):
    response = _decision(
        evolution_client, patch_ready, "needs_revision", admin_headers, note="缺少基准回放证据"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_more_evidence"
    audit = _candidate_audit(patch_ready)
    # ``needs_more_evidence`` has no better home in the audit whitelist, so it
    # stays "advance" and the note carries the reason.
    assert [row["action"] for row in audit] == ["generate", "advance", "advance"]
    assert audit[-1]["note"] == "缺少基准回放证据"
    trail = _candidate(patch_ready)["gate_log"]
    assert trail[-1]["gate"] == "review"
    assert trail[-1]["result"] == "inconclusive"


# ------------------------------------------------------------------
# The 400 / 404 / 409 boundaries: no audit row for a refused decision.
# ------------------------------------------------------------------


def test_an_unknown_action_is_rejected_before_anything_is_written(
    evolution_client, admin_headers, patch_ready
):
    response = _decision(evolution_client, patch_ready, "ship_it", admin_headers)

    assert response.status_code == 400
    assert "approve_for_apply" in response.json()["detail"]
    assert _candidate(patch_ready)["status"] == "awaiting_verification"
    assert [row["action"] for row in _candidate_audit(patch_ready)] == ["generate", "advance"]


def test_a_decision_on_an_unknown_patch_is_not_found(evolution_client, admin_headers, patch_ready):
    for action in ("approve_for_apply", "reject", "needs_revision"):
        assert _decision(evolution_client, 999, action, admin_headers).status_code == 404
    assert _candidate_audit(999) == []
    assert _candidate(patch_ready)["status"] == "awaiting_verification"


def test_a_second_decision_on_a_decided_patch_is_a_conflict(
    evolution_client, admin_headers, patch_ready, export_dir
):
    assert _decision(evolution_client, patch_ready, "approve_for_apply", admin_headers).status_code == 200

    second = _decision(evolution_client, patch_ready, "approve_for_apply", admin_headers)

    assert second.status_code == 409
    assert "approved_for_canary" in second.json()["detail"]
    # Nothing more was written: one export, one extra audit row, one review gate.
    assert list(export_dir.glob("*.patch")) == [export_dir / f"candidate-{patch_ready}.patch"]
    assert [row["action"] for row in _candidate_audit(patch_ready)] == ["generate", "advance", "advance"]
    assert len(_candidate(patch_ready)["gate_log"]) == 3


def test_a_patch_that_never_passed_the_sandbox_cannot_be_approved(
    evolution_client, admin_headers, patch_testing
):
    response = _decision(evolution_client, patch_testing, "approve_for_apply", admin_headers)

    assert response.status_code == 409
    assert _candidate(patch_testing)["status"] == "candidate_testing"
    assert [row["action"] for row in _candidate_audit(patch_testing)] == ["generate"]


def test_a_missing_database_reads_empty_and_is_never_created(
    evolution_client, admin_headers, tmp_path
):
    db_file = tmp_path / "evolution.db"
    assert not db_file.exists()

    page = evolution_client.get(CODE_CANDIDATES_URL, headers=admin_headers).json()
    assert page == {"items": [], "total": 0, "page": 1, "page_size": 20}
    assert evolution_client.get(
        f"{CODE_CANDIDATES_URL}/1", headers=admin_headers
    ).status_code == 404
    assert _decision(evolution_client, 1, "approve_for_apply", admin_headers).status_code == 404

    assert not db_file.exists()


# ------------------------------------------------------------------
# The phase-4 compatibility split: question sets and code patches.
# ------------------------------------------------------------------


def test_the_two_candidate_lists_never_mix_artifact_types(evolution_client, admin_headers):
    store = _store()
    try:
        patch_id = _add_candidate(
            store, sample_patch(), "patch-split", status=EvolutionStatus.AWAITING_VERIFICATION.value
        )
        _add_candidate(store, sample_question_set(), "set-split", status=None)
    finally:
        store.close()

    question_sets = evolution_client.get(CANDIDATES_URL, headers=admin_headers).json()
    assert question_sets["total"] == 1
    item = question_sets["items"][0]
    assert item["artifact_type"] == "jev_question_set"
    assert item["target"] == "preflight"
    assert item["payload_summary"]["question_count"] == 3
    assert "diff" not in item
    assert "layer" not in item

    patches = evolution_client.get(CODE_CANDIDATES_URL, headers=admin_headers).json()
    assert patches["total"] == 1
    assert patches["items"][0]["id"] == patch_id
    assert patches["items"][0]["layer"] == "reranking"
    # The question-set endpoint stays a question-set contract: a patch row (NULL
    # target/payload) would break it, so its body never mentions the diff.
    assert PATCH_DIFF not in evolution_client.get(CANDIDATES_URL, headers=admin_headers).text


# ------------------------------------------------------------------
# The read-only generation queue: what a manual trigger would act on.
# ------------------------------------------------------------------


def test_the_generation_queue_is_admin_only_and_names_the_missing_layer(
    evolution_client, admin_headers, user_headers
):
    """待生成清单：管理员可读，成员 403、匿名 401；未定层级时给出原因。

    Phase 5's generator is not in the worker's round, so this list is the only
    admin-facing surface of the queue; it stays read-only and calls no model.
    """
    store = _store()
    try:
        cluster_id, observation_id = _approved_cluster(store, "queue-admin")
    finally:
        store.close()

    assert evolution_client.get(GENERATION_QUEUE_URL).status_code == 401
    assert evolution_client.get(GENERATION_QUEUE_URL, headers=user_headers).status_code == 403

    response = evolution_client.get(GENERATION_QUEUE_URL, headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["observation_id"] == observation_id
    assert item["cluster_id"] == cluster_id
    assert item["question"] == "年假额度怎么算"
    assert item["confirmed_layer"] is None
    assert item["draft_suggested_layer"] is None
    assert item["effective_layer"] == ""
    assert item["layer_authority"] == ""
    assert "层级" in item["blocked_reason"]


def test_the_queue_reports_the_confirmed_layer_as_the_effective_one(
    evolution_client, admin_headers
):
    store = _store()
    try:
        cluster_id, _ = _approved_cluster(store, "queue-confirmed")
        store.set_confirmed_layer(cluster_id, confirmed_layer="reranking", now=time.time())
    finally:
        store.close()

    item = evolution_client.get(GENERATION_QUEUE_URL, headers=admin_headers).json()["items"][0]

    assert item["confirmed_layer"] == "reranking"
    assert item["effective_layer"] == "reranking"
    assert item["layer_authority"] == "confirmed"
    assert item["blocked_reason"] is None


def test_an_observation_that_already_has_a_patch_leaves_the_queue(
    evolution_client, admin_headers
):
    store = _store()
    try:
        _add_candidate(store, sample_patch(), "queue-patched", status=None)
    finally:
        store.close()

    body = evolution_client.get(GENERATION_QUEUE_URL, headers=admin_headers).json()

    assert body["total"] == 0
    assert body["items"] == []


# ------------------------------------------------------------------
# The exported snapshot: the revert path is part of the export.
# ------------------------------------------------------------------


def _real_comment_diff(path: Path) -> str:
    """A diff that really applies to ``path`` at HEAD (git's own unified format)."""
    import difflib

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    edited = [*lines, "# evolution candidate: tightened filename collision penalty\n"]
    relative = path.relative_to(REPO_ROOT).as_posix()
    return "".join(difflib.unified_diff(lines, edited, fromfile=f"a/{relative}", tofile=f"b/{relative}"))


def test_the_exported_patch_carries_a_revert_command_that_really_reverses_it(
    evolution_client, admin_headers, export_dir, tmp_path
):
    """批准导出同时给出 apply 与 revert，且 revert 在隔离 worktree 里验证过。

    回退不做成"自动 git 操作"：导出的是**可复制的命令**加一份机器可读快照，
    由人执行；这条测试证明那两条命令在真实仓库的一次 worktree 上互为逆操作，
    而主工作树始终未被触碰。
    """
    import json

    real_diff = _real_comment_diff(TARGET_FILE)
    target_text = TARGET_FILE.read_text(encoding="utf-8")
    before = _worktree_state()
    store = _store()
    try:
        candidate_id = _add_candidate(
            store,
            sample_patch(diff=real_diff),
            "revert-one",
            status=EvolutionStatus.AWAITING_VERIFICATION.value,
        )
    finally:
        store.close()

    response = _decision(evolution_client, candidate_id, "approve_for_apply", admin_headers, note="导出验证")
    assert response.status_code == 200

    patch_file = export_dir / f"candidate-{candidate_id}.patch"
    snapshot_file = export_dir / f"candidate-{candidate_id}.snapshot.json"
    text = patch_file.read_text(encoding="utf-8")
    assert "# revert: " in text
    snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
    assert snapshot["base_commit"] == BASE_COMMIT
    assert snapshot["layer"] == "reranking"
    assert snapshot["test_paths"] == ["tests/test_reranker.py"]
    assert snapshot["apply_command"] in text
    assert snapshot["revert_command"] in text

    worktree = tmp_path / "revert-worktree"
    subprocess.run(
        ["git", "worktree", "add", str(worktree), "--detach", "HEAD"],
        cwd=str(REPO_ROOT), check=True, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    try:
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", str(patch_file)],
            cwd=str(worktree), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        assert applied.returncode == 0, applied.stderr
        assert (worktree / "rag" / "reranker.py").read_text(encoding="utf-8") != target_text

        reverted = subprocess.run(
            ["git", "apply", "-R", "--whitespace=nowarn", str(patch_file)],
            cwd=str(worktree), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        assert reverted.returncode == 0, reverted.stderr
        assert (worktree / "rag" / "reranker.py").read_text(encoding="utf-8") == target_text
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=120,
        )

    # The red line still holds: approving never touched the main work tree.
    assert _worktree_state() == before
