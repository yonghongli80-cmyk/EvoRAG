"""Evolution observation API: phase-2 reads plus phase-3 admin decisions.

Mounted once by ``rag.api`` inside the knowledge-operations area. Every endpoint
is administrator-only. Phase 2 observes failures and stays read-only; phase 3
(Task 5) opens the first writes, so an administrator can read a diagnosis draft
and confirm a root cause before a cluster may reach the candidate pool.

Serialization carries stored signals only (hashes, scores, versions, enums,
counts, and timestamps); question text and chunk text never leave the store. The
one exception is the draft payload: the permission-filtered diagnoser output an
administrator must read to judge a root cause, redacted for credential-shaped
keys before it leaves ``evolution.db``. Every state change runs through the store
(``promote_cluster`` for admission, ``decide_cluster`` for an admin decision) and
is preceded by an audit row.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from config import settings
from rag.api_security import redact_source_config, require_admin, require_user
from rag.evolution.metrics import compute_metrics
from rag.evolution.models import EVOLUTION_TRANSITIONS, EvolutionStatus, ObservationPool
from rag.evolution.store import FailureTraceStore

router = APIRouter(prefix="/knowledge-operations/evolution", tags=["evolution"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

_STATUSES = tuple(status.value for status in EvolutionStatus)
_POOLS = tuple(pool.value for pool in ObservationPool)

# Endpoint inventory returned by GET /state-machine so the page renders the real
# surface from the same source the router registers. Phase 3 adds the diagnosis
# read endpoint and the single audited decision endpoint; the invariant that no
# other state-changing method exists is pinned by the API tests.
_ENDPOINTS = (
    {"path": "/knowledge-operations/evolution/observations", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters/{cluster_id}", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters/{cluster_id}/diagnosis", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/state-machine", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/metrics", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters/{cluster_id}/decision", "methods": ("POST",)},
)

_STATE_MACHINE_NOTE = (
    "进入候选池由聚类准入规则自动完成；管理员审核诊断草案后显式决策"
    "（approve_for_candidate / reject / needs_more_evidence），每次决策都写审计行。"
)

_store_lock = threading.Lock()
_store: FailureTraceStore | None = None
_store_path: str | None = None


def _open_store() -> FailureTraceStore | None:
    """Return the shared read-only store, or ``None`` when nothing was recorded.

    The recorder and the worker own ``evolution.db``; a read request must never
    create it. A missing file therefore means "no observations yet", which is
    the default deployment (``RAG_EVOLUTION_ENABLED=false``). The connection is
    cached per path so a reconfigured database path reopens the store.
    """
    global _store, _store_path
    path = str(settings.evolution_db_path)
    if not Path(path).exists():
        return None
    with _store_lock:
        if _store is None or _store_path != path:
            if _store is not None:
                _store.close()
            _store = FailureTraceStore(path)
            _store_path = path
        return _store


def close_store() -> None:
    """Release the read-only connection (shutdown and test isolation)."""
    global _store, _store_path
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
        _store_path = None


def _admin_user(user: dict = Depends(require_user)) -> dict:
    """Every evolution endpoint is administrator-only, reads and decisions alike."""
    return require_admin(user)


# Design section 8: the three administrator decisions, each mapped onto one of
# the ``awaiting_diagnosis_review`` transitions. ``pool`` stays
# ``candidate_pool`` on purpose — a rejected or evidence-short cluster is never
# returned to the observation queue, because both of those statuses have no
# outgoing transition and the next clustering round would try to re-admit one.
# ``member_status`` must be a legal successor of the status admission wrote
# (``clustered``), otherwise ``promote_cluster`` rejects the whole move.
DECISION_TARGETS: dict[str, dict[str, str]] = {
    "approve_for_candidate": {
        "new_status": EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        "pool": ObservationPool.CANDIDATE_POOL.value,
        "member_status": EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
    },
    "reject": {
        "new_status": EvolutionStatus.REJECTED.value,
        "pool": ObservationPool.CANDIDATE_POOL.value,
        "member_status": EvolutionStatus.REJECTED.value,
    },
    "needs_more_evidence": {
        "new_status": EvolutionStatus.NEEDS_MORE_EVIDENCE.value,
        "pool": ObservationPool.CANDIDATE_POOL.value,
        "member_status": EvolutionStatus.NEEDS_MORE_EVIDENCE.value,
    },
}


class DiagnosisDecision(BaseModel):
    """One administrator decision; the note is audit evidence for the transition."""

    model_config = ConfigDict(extra="forbid")

    # ``str`` plus an explicit check instead of ``Literal``: an unknown action is
    # a 400 naming the supported set, not FastAPI's generic 422.
    action: str
    note: str = ""


def _filter_value(value: str | None, allowed: tuple[str, ...], label: str) -> str | None:
    if value is None or value in allowed:
        return value
    raise HTTPException(status_code=400, detail=f"无效的{label}筛选值")


def _page(items: list[dict], page: int, page_size: int) -> dict:
    """Slice in Python like ``operations_api``: the store returns every row ordered by id."""
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "total": len(items),
        "page": page,
        "page_size": page_size,
    }


def _serialize_observation(row: dict) -> dict:
    """Expose stored signals only: hashes, scores, versions, enums, and times.

    The question text is deliberately absent — it stays in ``execution_logs``,
    where an administrator reads it together with the full trace. Raw chunk text
    was never stored, and decision metadata is the tracker's sanitized
    whitelist, so nothing here can carry document content or credentials.
    """
    return {
        "id": row["id"],
        "cluster_id": row["cluster_id"],
        "kb_id": row["kb_id"],
        "failure_stage": row["stage"],
        "root_cause_feature": row["root_cause_feature"],
        "status": row["status"],
        "pool": row["pool"],
        "priority": row["priority"],
        "route": row["route"],
        "chunk_hashes": list(row["details_chunk_hashes"]),
        "adopted_evidence_hashes": list(row["details_adopted_evidence_hashes"]),
        "best_score": row["details_best_score"],
        "answerability_kind": row["details_answerability_kind"],
        "confidence_level": row["details_confidence_level"],
        "tool_names": list(row["details_tool_names"]),
        "structured_query_kind": row["details_structured_query_kind"],
        "exception_class": row["details_exception_class"],
        "feedback_kind": row["details_feedback_kind"],
        "jev_question_set_version": row["details_jev_question_set_version"],
        "decision_metadata": dict(row["details_decision_metadata"]),
        "total_ms": row["total_ms"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _serialize_cluster(row: dict, members: list[dict] | None = None) -> dict:
    payload = {
        "id": row["id"],
        "cluster_key": row["cluster_key"],
        "kb_id": row["kb_id"],
        "failure_stage": row["failure_stage"],
        "root_cause_feature": row["root_cause_feature"],
        "pool": row["pool"],
        "status": row["status"],
        "priority": row["priority"],
        "member_count": row["member_count"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if members is not None:
        payload["members"] = [_serialize_observation(member) for member in members]
    return payload


def _serialize_draft(row: dict) -> dict:
    """One stored draft: metadata plus the redacted, verbatim payload.

    The payload is the diagnoser's frozen output — the evidence an administrator
    reviews before confirming a root cause, and the only place where text beyond
    hashes and enums reaches the API. It is redacted for credential-shaped keys
    (``redact_source_config``), so a model that echoed a connection string or an
    API key cannot leak it through this endpoint.
    """
    return {
        "id": row["id"],
        "cluster_id": row["cluster_id"],
        "prompt_version": row["prompt_version"],
        "model": row["model"],
        "reproducible": bool(row["reproducible"]),
        "created_at": row["created_at"],
        "payload": redact_source_config(dict(row["payload"])),
    }


def _serialize_audit(row: dict) -> dict:
    """One audit row: who decided what, on which cluster, and why."""
    return {
        "id": row["id"],
        "actor_id": row["actor_id"],
        "action": row["action"],
        "cluster_id": row["cluster_id"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _find_cluster(store: FailureTraceStore, cluster_id: int) -> dict | None:
    """One cluster row, or ``None`` when the id is unknown."""
    for row in store.list_clusters():
        if row["id"] == cluster_id:
            return row
    return None


@router.get("/observations", summary="失败观察列表")
def list_observations(
    status: str | None = Query(None),
    pool: str | None = Query(None),
    kb_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(_admin_user),
) -> dict:
    """Diagnosable failures across knowledge bases; filtering and paging here."""
    status = _filter_value(status, _STATUSES, "状态")
    pool = _filter_value(pool, _POOLS, "池")
    store = _open_store()
    rows = [] if store is None else store.list_observations()
    if status is not None:
        rows = [row for row in rows if row["status"] == status]
    if pool is not None:
        rows = [row for row in rows if row["pool"] == pool]
    if kb_id is not None:
        rows = [row for row in rows if row["kb_id"] == kb_id]
    items = [_serialize_observation(row) for row in rows]
    return _page(items, page, page_size)


@router.get("/clusters", summary="聚类列表")
def list_clusters(
    pool: str | None = Query(None),
    status: str | None = Query(None),
    kb_id: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(_admin_user),
) -> dict:
    """Observation-queue and candidate-pool clusters, oldest first."""
    pool = _filter_value(pool, _POOLS, "池")
    status = _filter_value(status, _STATUSES, "状态")
    store = _open_store()
    rows = [] if store is None else store.list_clusters()
    if pool is not None:
        rows = [row for row in rows if row["pool"] == pool]
    if status is not None:
        rows = [row for row in rows if row["status"] == status]
    if kb_id is not None:
        rows = [row for row in rows if row["kb_id"] == kb_id]
    items = [_serialize_cluster(row) for row in rows]
    return _page(items, page, page_size)


@router.get("/clusters/{cluster_id}", summary="聚类详情")
def get_cluster(cluster_id: int, user: dict = Depends(_admin_user)) -> dict:
    """One cluster with its member observations (stored signals only)."""
    store = _open_store()
    if store is not None:
        row = _find_cluster(store, cluster_id)
        if row is not None:
            members = store.list_observations(cluster_id=cluster_id)
            return _serialize_cluster(row, members)
    raise HTTPException(status_code=404, detail="聚类不存在")


@router.get("/clusters/{cluster_id}/diagnosis", summary="诊断草案与决策记录")
def get_diagnosis(cluster_id: int, user: dict = Depends(_admin_user)) -> dict:
    """The cluster-level legacy draft, every member draft, and the audit trail.

    Phase 4 drafts one diagnosis per member observation, because a cluster key
    mixes distinct root causes (``docs/evolution-diagnosis.md`` §5). The response
    therefore reports ``cluster_draft`` (the newest legacy cluster-level draft,
    or ``null``) plus ``member_drafts`` — one entry per member observation with
    its question, stage, and root-cause feature, and its own draft or ``null``.

    ``member_drafts`` carries each observation's question text. This is an
    intentional boundary change: a diagnosis is meaningless without the question
    an admin is judging a root cause for, and this endpoint is already
    administrator-only (``require_admin``), so the question text is visible to
    administrators and to no one else. A missing database or an unknown cluster
    reads as an empty diagnosis instead of an error: a cluster whose members are
    not yet drafted is a normal state (the worker drafts on a later round), so
    the page renders "no draft" rather than a failure.
    """
    store = _open_store()
    if store is None:
        return {"cluster_id": cluster_id, "cluster_draft": None, "member_drafts": [], "audit": []}
    drafts = store.list_drafts(cluster_id)  # newest first; carries observation_id
    members = store.list_observations(cluster_id)  # oldest first
    audit = store.list_audit(cluster_id)
    # The legacy cluster-level draft (observation_id NULL) that predates phase 4;
    # the newest such row is the one under review.
    cluster_draft = next((row for row in drafts if row["observation_id"] is None), None)
    # Newest member draft per observation (drafts are newest first, so the first
    # occurrence wins).
    member_draft_by_observation: dict[int, dict] = {}
    for row in drafts:
        observation_id = row["observation_id"]
        if observation_id is not None and observation_id not in member_draft_by_observation:
            member_draft_by_observation[observation_id] = row
    return {
        "cluster_id": cluster_id,
        "cluster_draft": None if cluster_draft is None else _serialize_draft(cluster_draft),
        "member_drafts": [
            {
                "observation_id": member["id"],
                "question": member["question"],
                "stage": member["stage"],
                "root_cause_feature": member["root_cause_feature"],
                "draft": _serialize_draft(member_draft_by_observation[member["id"]])
                if member["id"] in member_draft_by_observation
                else None,
            }
            for member in members
        ],
        "audit": [_serialize_audit(row) for row in audit],
    }


@router.post("/clusters/{cluster_id}/decision", summary="提交诊断决策")
def submit_decision(
    cluster_id: int, payload: DiagnosisDecision, user: dict = Depends(_admin_user)
) -> dict:
    """Record one administrator decision and move the cluster through the machine.

    The decision is still **cluster-level** (unchanged): a single
    ``approve_for_candidate`` / ``reject`` / ``needs_more_evidence`` covers the
    whole cluster and every member observation, even though phase 4 drafts a
    diagnosis per member. The audit row is written before the transition runs,
    so no state change can exist without a trace: an audit row without a state
    change is a visible no-op, while the reverse would be an unaudited
    transition. Admission writes go through ``store.promote_cluster``; this
    decision writes through ``store.decide_cluster``, which applies the same
    transition table to a cluster that already lives in the candidate pool.
    """
    target = DECISION_TARGETS.get(payload.action)
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的决策动作：{payload.action}（支持 {' / '.join(sorted(DECISION_TARGETS))}）",
        )
    store = _open_store()
    cluster = None if store is None else _find_cluster(store, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="聚类不存在")
    if cluster["status"] != EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value:
        raise HTTPException(status_code=409, detail="该聚类已不在诊断审核状态")
    store.add_audit(str(user["id"]), payload.action, cluster_id, payload.note)
    try:
        updated = store.decide_cluster(
            cluster_id,
            new_status=target["new_status"],
            pool=target["pool"],
            member_status=target["member_status"],
            now=time.time(),
        )
    except KeyError as exc:
        # The cluster disappeared between the read above and this write.
        raise HTTPException(status_code=404, detail="聚类不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=f"非法状态转移：{exc}") from exc
    return _serialize_cluster(updated, store.list_observations(cluster_id=cluster_id))


@router.get("/state-machine", summary="状态机参考")
def state_machine(user: dict = Depends(_admin_user)) -> dict:
    """Design section 7 transitions plus the phase-2 read-only endpoint list."""
    return {
        "statuses": list(_STATUSES),
        "pools": list(_POOLS),
        "transitions": EVOLUTION_TRANSITIONS,
        "endpoints": [
            {"path": endpoint["path"], "methods": list(endpoint["methods"])}
            for endpoint in _ENDPOINTS
        ],
        "note": _STATE_MACHINE_NOTE,
    }


@router.get("/metrics", summary="观测指标")
def metrics(user: dict = Depends(_admin_user)) -> dict:
    """Phase-2 observation metrics computed from stored signals only.

    A missing database means nothing was ever recorded, so the payload is
    computed against a private in-memory store instead of creating the file.
    """
    store = _open_store()
    temporary = store is None
    if store is None:
        store = FailureTraceStore(":memory:")
    try:
        return compute_metrics(store)
    finally:
        if temporary:
            store.close()
