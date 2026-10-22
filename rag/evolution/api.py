"""Evolution observation API: phase-2 reads plus phase-3 admin decisions.

Mounted once by ``rag.api`` inside the knowledge-operations area. Every endpoint
is administrator-only. Phase 2 observes failures and stays read-only; phase 3
(Task 5) opens the first writes, so an administrator can read a diagnosis draft
and confirm a root cause before a cluster may reach the candidate pool. Phase 4
appends the candidate lifecycle (Task 5 of the candidate plan): two reads over
the candidates and the five-layer trail, and two audited actions — a percentage
canary (layer 4) and an instant rollback (layer 5).

Phase 5 adds the code-patch half of the same table (Task 4 of the code-patch
plan). A code patch has no percentage canary — there is no second instance to
serve a share of traffic — so layer 4 degrades to an administrator review of the
diff: ``POST /code-candidates/{id}/decision`` exports
``artifacts/patches/candidate-<id>.patch`` (a header comment with the apply
command and the base commit, then the diff verbatim), writes the audit row and
advances the candidate to ``approved_for_canary``, which for a code patch means
"awaiting a human apply". The endpoint never runs ``git apply``: the working tree
is only read, exactly as ``validate_patch`` reads it.

Serialization carries stored signals only (hashes, scores, versions, enums,
counts, and timestamps); question text and chunk text never leave the store. The
one exception is the draft payload: the permission-filtered diagnoser output an
administrator must read to judge a root cause, redacted for credential-shaped
keys before it leaves ``evolution.db``. A candidate payload is summarized
instead (which questions it overrides and how much text it moves) — the
generation evidence an administrator needs for the rollout decision is the gate
trail, the rationale and the expected effect. A code-patch candidate carries its
diff body only on the detail response, because that body *is* the file content a
reviewer must read to decide; the list rows report the layer, the touched paths,
the verification targets and what the sandbox concluded instead. Every state
change runs through the store (``promote_cluster`` for admission,
``decide_cluster`` for an admin decision, ``advance_candidate`` for the candidate
lifecycle) and is preceded by an audit row.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import get_args

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from config import settings
from rag.api_security import redact_source_config, require_admin, require_user
from rag.evolution.candidate_store import CandidateStore
from rag.evolution.candidates import PayloadBoundaryError, validate_payload

# ``code_patches`` owns diff parsing and is the authority layer 1 validated a
# candidate's ``paths`` against; ``candidate_store`` already borrows private
# helpers from ``store`` the same way, and the diff headers are the only record
# of which files a stored patch touches (``paths`` is not a column).
from rag.evolution.code_patches import _diff_header_paths
from rag.evolution.metrics import compute_metrics
from rag.evolution.models import (
    EVOLUTION_TRANSITIONS,
    DiagnosisDraft,
    EvolutionStatus,
    ObservationPool,
)
from rag.evolution.rollout import PERCENT_NOTE_KEY, percent_note, rollout
from rag.evolution.store import FailureTraceStore
from rag.jev.questions import build_preflight_questions

router = APIRouter(prefix="/knowledge-operations/evolution", tags=["evolution"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

_STATUSES = tuple(status.value for status in EvolutionStatus)
_POOLS = tuple(pool.value for pool in ObservationPool)

# Endpoint inventory returned by GET /state-machine so the page renders the real
# surface from the same source the router registers. Phase 3 adds the diagnosis
# read endpoint and the single audited decision endpoint; the invariant that no
# other state-changing method exists is pinned by the API tests. Phase 4 appends
# the four candidate endpoints below — they are deliberately *not* in this
# inventory, because ``test_evolution_api.py`` pins the phase-3 surface and the
# candidate lifecycle is documented (and exercised) through its own tests.
_ENDPOINTS = (
    {"path": "/knowledge-operations/evolution/observations", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters/{cluster_id}", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters/{cluster_id}/diagnosis", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/state-machine", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/metrics", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/clusters/{cluster_id}/decision", "methods": ("POST",)},
    # The later phases' read surfaces belong in the administrator's inventory too:
    # without them the list documents the diagnosis tab while the candidates and
    # patches tabs stay undiscoverable from it. Their write endpoints (canary,
    # rollback, review decisions) stay out so the invariant this list pins — that
    # the only state-changing method in the *diagnosis* surface is the audited
    # decision — remains literally true; those are documented in
    # docs/evolution-candidates.md and docs/evolution-code-patches.md.
    {"path": "/knowledge-operations/evolution/candidates", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/code-candidates", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/code-generation-queue", "methods": ("GET",)},
    {"path": "/knowledge-operations/evolution/gap-tickets", "methods": ("GET",)},
)

#: 工单状态（表里是自由文本，但读取端只认这两个已知值，未知值由 `_filter_value` 拒掉）。
GAP_TICKET_STATUSES: tuple[str, ...] = ("open", "content_added")

_STATE_MACHINE_NOTE = (
    "进入候选池由聚类准入规则自动完成；管理员审核诊断草案后显式决策"
    "（approve_for_candidate / reject / needs_more_evidence），每次决策都写审计行。"
)

_store_lock = threading.Lock()
_store: FailureTraceStore | None = None
_store_path: str | None = None
# Phase 4: the candidate store shares the trace store's connection and lock, so
# it is cached per path the same way and dropped by ``close_store``.
_candidate_store: CandidateStore | None = None
_candidate_store_path: str | None = None

# Candidate rows are paged in Python like every other list endpoint, so the
# query asks for a bounded scan instead of the whole table.
MAX_CANDIDATE_ROWS = 1000


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
    global _store, _store_path, _candidate_store, _candidate_store_path
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
        _store_path = None
        # The candidate store shares the trace connection and never closes it,
        # so dropping the reference is all the cleanup it needs.
        _candidate_store = None
        _candidate_store_path = None


def _open_candidate_store() -> CandidateStore | None:
    """The candidate store on the shared connection, or ``None`` when nothing was recorded.

    ``CandidateStore`` reuses the trace store's connection, row factory and lock
    (a candidate always lives next to the observations it came from) and closes
    nothing it does not own, so the shutdown path stays the single
    ``close_store()``.
    """
    global _candidate_store, _candidate_store_path
    store = _open_store()
    if store is None:
        return None
    path = str(settings.evolution_db_path)
    with _store_lock:
        if _candidate_store is None or _candidate_store_path != path:
            _candidate_store = CandidateStore(store)
            _candidate_store_path = path
        return _candidate_store


def _admin_user(user: dict = Depends(require_user)) -> dict:
    """Every evolution endpoint is administrator-only, reads and decisions alike."""
    return require_admin(user)


# Design section 8: the three administrator decisions, each mapped onto one of
# the ``awaiting_diagnosis_review`` transitions. ``pool`` stays
# ``candidate_pool`` on purpose — a rejected or evidence-short cluster is never
# returned to the observation queue, because both of those statuses have no
# outgoing transition and the next clustering round would try to re-admit one.
# ``member_status`` must be a legal successor of every status a member can hold
# when the decision lands. Admission writes ``clustered``, but a member attached
# after admission stays ``observed`` (``attach_cluster_member`` keeps it queued),
# so approve targets ``clustered`` — reachable from both and a no-op for already
# clustered members — instead of the unreachable ``awaiting_diagnosis_review``.
# reject and needs_more_evidence are reachable from both statuses directly.
DECISION_TARGETS: dict[str, dict[str, str]] = {
    "approve_for_candidate": {
        "new_status": EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
        "pool": ObservationPool.CANDIDATE_POOL.value,
        "member_status": EvolutionStatus.CLUSTERED.value,
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


#: Layers an administrator may confirm; read from the diagnosis contract so the
#: API, the diagnoser and the patch generator share one taxonomy.
CONFIRMABLE_LAYERS: tuple[str, ...] = tuple(
    get_args(DiagnosisDraft.model_fields["suggested_layer"].annotation)
)


class DiagnosisDecision(BaseModel):
    """One administrator decision; the note is audit evidence for the transition."""

    model_config = ConfigDict(extra="forbid")

    # ``str`` plus an explicit check instead of ``Literal``: an unknown action is
    # a 400 naming the supported set, not FastAPI's generic 422.
    action: str
    note: str = ""
    # The administrator's confirmed root-cause layer (design section 6/8). It is
    # optional: without it the diagnoser's own suggestion stands, and the
    # code-patch generator refuses a layer the confirmed diagnosis does not name.
    confirmed_layer: str | None = None


def _confirm_cluster_layer(*, cluster_id: int, payload: DiagnosisDecision, user: dict) -> dict:
    """Record the administrator's confirmed root-cause layer without moving status.

    The root-cause confirmation and the state transition are separate judgments
    (design sections 6/8): the layer may be confirmed before or after the
    approval, and the code-patch generator reads it as the authority. Only the
    two review states accept a confirmation; the write is audited first.
    """
    layer = (payload.confirmed_layer or "").strip()
    if layer not in CONFIRMABLE_LAYERS:
        raise HTTPException(
            status_code=400,
            detail=f"无效的确认根因层级：{payload.confirmed_layer}（支持 {' / '.join(CONFIRMABLE_LAYERS)}）",
        )
    store = _open_store()
    cluster = None if store is None else _find_cluster(store, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="聚类不存在")
    if cluster["status"] not in (
        EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value,
        EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
    ):
        raise HTTPException(status_code=409, detail="该聚类当前不接受根因层级确认")
    store.add_audit(
        str(user["id"]),
        # The phase-3 audit table whitelists three decision actions; the layer
        # confirmation is part of the same approval judgment, and the note names
        # exactly what happened.
        "approve_for_candidate",
        cluster_id,
        f"确认根因层级：{layer}；备注：{payload.note}" if payload.note else f"确认根因层级：{layer}",
    )
    updated = store.set_confirmed_layer(cluster_id, confirmed_layer=layer, now=time.time())
    members = store.list_observations(cluster_id)
    return _serialize_cluster(updated, members)


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
        "confirmed_layer": row.get("confirmed_layer"),
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
    if payload.action == "confirm_layer":
        return _confirm_cluster_layer(cluster_id=cluster_id, payload=payload, user=user)
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的决策动作：{payload.action}（支持 {' / '.join(sorted(DECISION_TARGETS))}）",
        )
    if payload.confirmed_layer is not None and payload.confirmed_layer not in CONFIRMABLE_LAYERS:
        raise HTTPException(
            status_code=400,
            detail=f"无效的确认根因层级：{payload.confirmed_layer}（支持 {' / '.join(CONFIRMABLE_LAYERS)}）",
        )
    store = _open_store()
    cluster = None if store is None else _find_cluster(store, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="聚类不存在")
    if cluster["status"] != EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value:
        raise HTTPException(status_code=409, detail="该聚类已不在诊断审核状态")
    audit_note = payload.note
    if payload.confirmed_layer:
        audit_note = f"{audit_note}；确认根因层级：{payload.confirmed_layer}".strip("；")
    store.add_audit(str(user["id"]), payload.action, cluster_id, audit_note)
    try:
        updated = store.decide_cluster(
            cluster_id,
            new_status=target["new_status"],
            pool=target["pool"],
            member_status=target["member_status"],
            now=time.time(),
            confirmed_layer=payload.confirmed_layer,
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
        # 可确认的根因层级：由草稿模型的 Literal **派生**，前端据此渲染下拉框，
        # 避免两边各写一份清单而漂移。
        "confirmable_layers": list(CONFIRMABLE_LAYERS),
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


# ----------------------------------------------------------------------
# Phase 4 (Task 5): the candidate lifecycle.
#
# A candidate question set goes live only through an audited canary action and
# comes back off in one audited rollback. Both writes reconcile the in-memory
# rollout controller with the persisted trail first, so a restarted API process
# resumes (or drops) exactly what the administrator left behind, and the
# transition legality is checked before a single row is written.
#
# Phase 5 (Task 4 of the code-patch plan) continues below it: the code-patch
# review endpoints, which share this table, state machine and audit trail but
# have no percentage rollout, sit between the canary and the rollback below.
# ----------------------------------------------------------------------


def _summarize_payload(payload: Mapping) -> dict:
    """Which questions a candidate overrides, and how much text it moves.

    The payload text itself stays in ``evolution.db``: the API carries stored
    signals only, and the gate trail plus the rationale and expected effect are
    what a rollout decision is taken on. Overridden keys are reported per
    question so the summary still shows a surgical edit versus a rewrite.
    """
    questions = []
    overridden = 0
    if not isinstance(payload, Mapping):
        return {"question_count": 0, "overridden": 0, "questions": []}
    for question_id in sorted(payload):
        entry = payload[question_id] if isinstance(payload[question_id], Mapping) else {}
        fields = sorted(key for key in ("instructions", "criteria") if key in entry)
        if fields:
            overridden += 1
        questions.append(
            {
                "question_id": str(question_id),
                "fields": fields,
                "instructions_chars": len(entry["instructions"]) if "instructions" in entry else 0,
                "criteria_entries": len(entry.get("criteria") or {}),
            }
        )
    return {"question_count": len(questions), "overridden": overridden, "questions": questions}


def _serialize_candidate(row: dict, *, canary_percent: int = 0) -> dict:
    """One candidate: stored signals, a payload summary and the live percentage."""
    return {
        "id": row["id"],
        "observation_id": row["observation_id"],
        "cluster_id": row["cluster_id"],
        "artifact_type": row["artifact_type"],
        "target": row["target"],
        "status": row["status"],
        "pool": row["pool"],
        "confidence": row["confidence"],
        "prompt_version": row["prompt_version"],
        "model": row["model"],
        "rationale": row["rationale"],
        "expected_effect": row["expected_effect"],
        "rollback_ref": dict(row["rollback_ref"])
        if isinstance(row["rollback_ref"], Mapping)
        else {},
        "payload_summary": _summarize_payload(row["payload"]),
        "canary_percent": int(canary_percent),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _serialize_gate_entry(entry: dict) -> dict:
    """One entry of the five-layer trail: which layer, what it concluded, when."""
    payload = {
        "gate": str(entry.get("gate", "")),
        "result": str(entry.get("result", "")),
        "detail": str(entry.get("detail", "")),
        "created_at": float(entry.get("created_at") or 0.0),
    }
    # 沙箱写回的结构化指标（两侧 route/top-5/失败调用/时延）。老条目没有这个键，
    # 所以只在存在时透出，前端据此决定要不要画对照表。
    metrics = entry.get("metrics")
    if isinstance(metrics, dict):
        payload["metrics"] = metrics
    return payload


def _serialize_candidate_audit(row: dict) -> dict:
    """One candidate lifecycle audit row: who acted, what, on which candidate."""
    return {
        "id": row["id"],
        "actor_id": row["actor_id"],
        "action": row["action"],
        "candidate_id": row["candidate_id"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


class CanaryRequest(BaseModel):
    """One canary action: the share of requests that may use the candidate set."""

    model_config = ConfigDict(extra="forbid")

    # ``int`` plus an explicit range check instead of ``Field(le=100)``: an
    # out-of-range share is a 400 naming the boundary, not FastAPI's generic
    # 422 — the same shape the phase-3 decision endpoint uses for its action.
    percent: int


#: ``gate_log`` names of the layers a canary must see concluded. Layer 2 is the fixed benchmark
#: corpus (:data:`rag.evolution.candidate_gates.GATE_NAME`); layer 3 is the shadow comparison. They
#: are the two layers that measure a question set *before* it serves traffic, which is why the
#: canary endpoint reads their entries instead of trusting the status alone (C-05, 2026-10-22).
BENCHMARK_GATE = "benchmark"
SHADOW_GATE = "shadow"

#: How many recorded shadow comparisons count as "sampled enough". One comparison already proves
#: the layer ran; zero is the state this guard exists for — the endpoint used to accept a candidate
#: whose trail held nothing but its own payload, so a question set could serve live traffic with no
#: evidence behind it at all.
MIN_SHADOW_SAMPLES = 1


def _reconcile_rollout(store: CandidateStore) -> None:
    """Rebuild live rollout state from the persisted trail before acting on it."""
    rollout.restore(store)


def _require_candidate(store: CandidateStore, candidate_id: int) -> dict:
    row = store.get_candidate(candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    return row


def _gate_entries(row: Mapping) -> list[Mapping]:
    """The readable entries of one candidate's ``gate_log`` (malformed values dropped)."""
    entries = row.get("gate_log")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


def _gate_passed(entries: Sequence[Mapping], gate: str) -> bool:
    """Whether ``gate`` recorded a **passed** conclusion in this trail."""
    return any(
        str(entry.get("gate") or "") == gate and str(entry.get("result") or "") == "passed"
        for entry in entries
    )


def _shadow_samples(entry: Mapping) -> int:
    """How many comparisons one shadow entry says it recorded.

    ``record_gate_entry`` stores structured numbers under ``metrics``; a hand-written or legacy
    entry without them counts as zero, which is the conservative direction for this guard (an
    unreadable sample count is not evidence of sampling).
    """
    metrics = entry.get("metrics")
    if not isinstance(metrics, Mapping):
        return 0
    for key in ("samples", "sample_count"):
        value = metrics.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return 0


def _require_canaryable_payload(row: dict) -> None:
    """Refuse to serve a stored payload that no longer fits its base set.

    The generator validated the payload against the set it was generated from;
    a rollback reference points at that same version, so re-validating here is
    the last check before the candidate set reaches live traffic. Only the
    chunk-independent preflight target can be re-validated without a request.
    """
    if row["target"] != "preflight":
        return
    try:
        validate_payload("preflight", row["payload"], build_preflight_questions())
    except PayloadBoundaryError as error:
        raise HTTPException(
            status_code=409, detail=f"候选载荷越界，拒绝灰度：{error.reason}"
        ) from error


def _require_canary_entry_conditions(row: Mapping) -> None:
    """Refuse to expose a candidate whose layers 2 and 3 never concluded (C-05).

    ``awaiting_verification`` is a claim written by a runner; this reads the trail that is supposed
    to back it. Layer 2 needs a **passed** benchmark entry — a missing one, a failed one, or an
    inconclusive retry all mean the candidate has no measured comparison against the live question
    set. Layer 3 needs at least :data:`MIN_SHADOW_SAMPLES` recorded shadow comparisons, because
    that is the only evidence about how the two question sets answer the same live question.

    Both refusals are 409s that write nothing: the caller may run the missing layer and retry.
    """
    entries = _gate_entries(row)
    if not _gate_passed(entries, BENCHMARK_GATE):
        raise HTTPException(
            status_code=409,
            detail=(
                "层 2 基准门禁没有通过的记录：不能进入灰度"
                "（请先跑 benchmarks 对照，让 gate_log 里有 benchmark=passed）"
            ),
        )
    samples = sum(
        _shadow_samples(entry)
        for entry in entries
        if str(entry.get("gate") or "") == SHADOW_GATE
    )
    if samples < MIN_SHADOW_SAMPLES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"层 3 影子采样不足（{samples} 次，至少 {MIN_SHADOW_SAMPLES} 次）：不能进入灰度"
                "（无样本的灰度等于拿线上流量做第一次测量）"
            ),
        )


@router.get("/candidates", summary="候选问题集列表")
def list_candidates(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(_admin_user),
) -> dict:
    """Question-set candidates newest first, with the live canary percentage.

    A missing database means nothing was recorded yet, so the page reads empty
    without creating the file (the generation switch is off by default).

    Phase 5 widened one table to two artifact types, and this endpoint's
    contract is the question-set one: ``target`` and ``payload`` are NULL for a
    code patch, which the page (and its frontend) would read as a broken row.
    Each candidate list endpoint therefore serves exactly one artifact type —
    question sets here, code patches under ``/code-candidates`` — while both
    share the table, the state machine, the gate trail and the audit trail.
    """
    status = _filter_value(status, _STATUSES, "状态")
    store = _open_candidate_store()
    rows = [] if store is None else store.list_candidates(status=status, limit=MAX_CANDIDATE_ROWS)
    if store is not None:
        _reconcile_rollout(store)
    rows = [row for row in rows if row["artifact_type"] == QUESTION_SET_ARTIFACT_TYPE]
    items = [
        _serialize_candidate(row, canary_percent=rollout.percent_for(row["id"])) for row in rows
    ]
    return _page(items, page, page_size)


@router.get("/candidates/{candidate_id}", summary="候选详情与五层门禁轨迹")
def get_candidate(candidate_id: int, user: dict = Depends(_admin_user)) -> dict:
    """One candidate with its gate trail and its lifecycle audit rows.

    The trail is the five layers in order (scope, layer 2, layer 3, canary), so
    an administrator sees what the candidate passed before approving layer 4 and
    what the percentage was when it went live.
    """
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="观测数据库不存在")
    _reconcile_rollout(store)
    row = _require_candidate(store, candidate_id)
    payload = _serialize_candidate(row, canary_percent=rollout.percent_for(candidate_id))
    payload["gate_log"] = [_serialize_gate_entry(entry) for entry in row["gate_log"]]
    payload["audit"] = [
        _serialize_candidate_audit(entry) for entry in store.list_audit(candidate_id)
    ]
    return payload


@router.post("/candidates/{candidate_id}/canary", summary="按百分比灰度候选问题集")
def canary_candidate(
    candidate_id: int, payload: CanaryRequest, user: dict = Depends(_admin_user)
) -> dict:
    """Layer 4: register the candidate question set and roll it out by percentage.

    The action registers the candidate version in
    :mod:`rag.jev.questions` (a data registration next to the default set),
    advances ``awaiting_verification → approved_for_canary → canary`` and writes
    the percentage into the rollout controller; a candidate already in canary
    only changes its percentage. Requests inside the stable bucket
    ``hash(key) % 100 < percent`` are judged with the candidate set and carry
    its version in ``decision_metadata``; the rest keep the current set. A
    percentage of zero registers nothing, which is the default online
    behaviour. Only one candidate may be live at a time.

    Layer 4 is the first point at which a question set meets real traffic, so it
    refuses a candidate whose trail does not carry the two measurements that
    justify that (C-05, 2026-10-22): a **passed** layer-2 benchmark entry and at
    least one layer-3 shadow comparison. A row that merely *is* in
    ``awaiting_verification`` is a status claim; the entry conditions read the
    evidence. The live registration happens **before** the status is written, so
    a failed write cannot leave a candidate recorded as "in canary" while
    nothing serves it — and if the write does fail, the registration is removed
    again.
    """
    if not 0 <= payload.percent <= 100:
        raise HTTPException(status_code=400, detail="灰度百分比必须是 0..100 的整数")
    ceiling = settings.evolution_candidate_canary_percent
    if payload.percent > ceiling:
        raise HTTPException(
            status_code=400,
            detail=(
                f"请求的灰度百分比 {payload.percent} 超过配置上限 {ceiling}"
                "（evolution_candidate_canary_percent）"
            ),
        )
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="观测数据库不存在")
    _reconcile_rollout(store)
    row = _require_candidate(store, candidate_id)
    # 第一条断言：这张表两个制品共用，问题集的灰度端点只服务问题集候选。代码补丁没有
    # 百分比灰度（没有第二个实例可分流量），它的层 4 是 `/code-candidates/{id}/decision`。
    if row["artifact_type"] != QUESTION_SET_ARTIFACT_TYPE:
        raise HTTPException(status_code=404, detail="候选不存在")
    actor_id = str(user["id"])
    percent = payload.percent
    current = row["status"]
    rollback_ref = row["rollback_ref"] if isinstance(row["rollback_ref"], Mapping) else {}

    def activate() -> None:
        """Apply the live effect; never let a registration failure become a status move."""
        try:
            rollout.activate(
                candidate_id,
                row["target"],
                row["payload"],
                str(rollback_ref.get("question_set_version", "")),
                percent,
            )
        except (TypeError, ValueError) as error:
            # An unknown target or an unreadable payload is not something to serve.
            raise HTTPException(status_code=409, detail=f"候选载荷无法灰度：{error}") from error

    if current == EvolutionStatus.CANARY.value:
        # Already live: a percentage change is its own audited action. The registration comes
        # first for the same reason as the transition below — the trail must describe what serves.
        activate()
        store.add_audit(actor_id, "canary", candidate_id, percent_note(percent))
    elif current in (
        EvolutionStatus.AWAITING_VERIFICATION.value,
        EvolutionStatus.APPROVED_FOR_CANARY.value,
    ):
        live = [
            candidate
            for candidate in store.list_candidates(
                status=EvolutionStatus.CANARY.value, limit=MAX_CANDIDATE_ROWS
            )
            if candidate["id"] != candidate_id
        ]
        if live:
            raise HTTPException(
                status_code=409,
                detail=f"候选 #{live[0]['id']} 正在灰度中：同一时间只允许一个候选问题集在线",
            )
        _require_canaryable_payload(row)
        _require_canary_entry_conditions(row)
        activate()
        try:
            if current == EvolutionStatus.AWAITING_VERIFICATION.value:
                store.advance_candidate(
                    candidate_id,
                    EvolutionStatus.APPROVED_FOR_CANARY.value,
                    actor_id=actor_id,
                    note=f"管理员批准灰度 {percent}%",
                )
            store.advance_candidate(
                candidate_id,
                EvolutionStatus.CANARY.value,
                actor_id=actor_id,
                note=percent_note(percent),
                gate="canary",
                gate_detail=f"percent={percent}",
            )
        except KeyError as error:
            # The candidate disappeared between the read above and this write.
            rollout.deactivate(candidate_id)
            raise HTTPException(status_code=404, detail="候选不存在") from error
        except ValueError as error:
            rollout.deactivate(candidate_id)
            raise HTTPException(status_code=409, detail=f"非法状态转移：{error}") from error
    else:
        raise HTTPException(
            status_code=409, detail=f"候选状态 {current} 不允许灰度（需 awaiting_verification 或 canary）"
        )

    updated = store.get_candidate(candidate_id)
    return _serialize_candidate(updated, canary_percent=rollout.percent_for(candidate_id))


# ----------------------------------------------------------------------
# Phase 5 (Task 4): the code-patch review endpoints and the export.
#
# A code patch has no percentage canary — there is no second instance to serve a
# share of traffic — so layer 4 degrades to an administrator review of the diff
# (design §2). ``approve_for_apply`` is that whole layer: it exports
# ``artifacts/patches/candidate-<id>.patch``, writes the audit row (whose note
# carries the export path and the apply command) and advances the candidate to
# ``approved_for_canary``, which for a code patch means "awaiting a human apply".
# The endpoint never runs ``git apply``: nothing here writes to the working tree,
# reads it, or spawns git, exactly like ``validate_patch``'s check-only mode.
# ----------------------------------------------------------------------

#: The artifact types the two candidate surfaces serve. One table, two contracts:
#: question sets read ``target``/``payload``, code patches read ``layer``/``diff``.
QUESTION_SET_ARTIFACT_TYPE = "jev_question_set"
CODE_PATCH_ARTIFACT_TYPE = "code_patch"

#: Layers 2/3 of a code patch are the worktree sandbox (apply, target tests,
#: benchmark replay) and record their conclusion under this gate name; layer 4's
#: review writes under the other one. Both names are what the gate trail is read
#: by — the store only ever copies them into ``gate_log``.
SANDBOX_GATE = "sandbox"
REVIEW_GATE = "review"
#: 层 2 的第二个生产者：宿主上的 A/B（见 ``sandbox.HOST_AB_GATE_NAME``）。它写进同一条 gate_log，
#: 但**不移动状态**——测量是证据，决策仍是管理员那一步。审查台按门禁名分开渲染两者。
HOST_AB_GATE = "host_ab"

#: Where an approved patch is exported, and the repository the exported apply
#: command names. ``artifacts/`` is locally ignored, so an export is a working
#: document an administrator applies by hand — never a commit, never a merge.
REPO_ROOT = Path(__file__).resolve().parents[2]
PATCH_EXPORT_DIR = REPO_ROOT / "artifacts" / "patches"
_PATCH_EXPORT_SUBDIR = "artifacts/patches"

#: The three administrator decisions of layer 4, each mapped onto one of the
#: ``awaiting_verification`` transitions and onto the gate detail its review
#: entry records. ``approved_for_canary`` is the target for an approval because
#: the code-patch state machine reuses the question-set one (the plan Task 1
#: interfaces pin the transition, and no new status exists).
CODE_PATCH_DECISIONS: dict[str, dict[str, str]] = {
    "approve_for_apply": {
        "new_status": EvolutionStatus.APPROVED_FOR_CANARY.value,
        "gate_detail": "approved_for_apply",
    },
    "reject": {"new_status": EvolutionStatus.REJECTED.value, "gate_detail": "reject"},
    "needs_revision": {
        "new_status": EvolutionStatus.NEEDS_MORE_EVIDENCE.value,
        "gate_detail": "needs_revision",
    },
    # 合入提交的那条路。人不经过 apply 就把改动合进代码时（2026-10-12 的 #10：改动早在
    # bcb645a 里，候选行却停在 approved_for_canary 两周、一直占着聚类的准入名额），
    # 系统无从自动知道；能做的只有"让管理员把这件事说出来并留痕"。
    "close_out_merged": {
        "new_status": EvolutionStatus.STABLE.value,
        "gate_detail": "merged_by_commit",
    },
}


class CodePatchDecision(BaseModel):
    """One administrator review decision; the note is audit evidence for it."""

    model_config = ConfigDict(extra="forbid")

    # ``str`` plus an explicit check instead of ``Literal``: an unknown action is
    # a 400 naming the supported set, not FastAPI's generic 422 — the same shape
    # the phase-3 decision endpoint and the question-set canary use.
    action: str
    note: str = ""
    #: Only ``close_out_merged`` reads it: the commit that carried the change into the code, so the
    #: audit row names something a reader can open instead of "somewhere in history".
    merged_commit: str = ""


def _diff_paths(diff: str) -> list[str]:
    """Every file a stored diff touches, once, through the boundary module's parser.

    ``_diff_header_paths`` reports both sides of a modified file (``a/…`` and
    ``b/…``), which is one file to a reviewer, so deduplicate in diff order.
    """
    paths: list[str] = []
    for path in _diff_header_paths(diff):
        if path not in paths:
            paths.append(path)
    return paths


def _sandbox_summary(gate_log: object) -> list[dict]:
    """The sandbox conclusions of layers 2/3, lifted out of the gate trail.

    A patch that never reached the sandbox summarizes to an empty list, which is
    exactly what a reviewer needs to see before deciding about it. Only the ``sandbox`` gate
    qualifies: the host A/B writes the same layer's comparison on a different surface and gets its
    own summary, so the panel can say which of the two produced a number instead of merging them.
    """
    if not isinstance(gate_log, list):
        return []
    return [
        _serialize_gate_entry(entry)
        for entry in gate_log
        if isinstance(entry, Mapping) and entry.get("gate") == SANDBOX_GATE
    ]


def _host_ab_summary(gate_log: object) -> list[dict]:
    """The host A/B conclusions, lifted out of the same trail.

    Layer 2's real clean-versus-patched comparison happens on the host **after** an administrator
    applies the patch (the sandbox carries no credentials), so this is where the number a release
    decision leans on actually lives. Its entries never move a candidate's status.
    """
    if not isinstance(gate_log, list):
        return []
    return [
        _serialize_gate_entry(entry)
        for entry in gate_log
        if isinstance(entry, Mapping) and entry.get("gate") == HOST_AB_GATE
    ]


def _serialize_code_candidate(row: dict) -> dict:
    """One code-patch candidate: stored signals, paths and the sandbox conclusion.

    The diff body is deliberately absent — it is the file content a reviewer
    reads on the detail response, and the only place the frontend ever shows it.
    A list row needs the layer, the touched paths, the verification targets and
    what the sandbox concluded; ``paths`` comes from the diff headers because the
    candidate stores the diff, not a separate path list.
    """
    return {
        "id": row["id"],
        "observation_id": row["observation_id"],
        "cluster_id": row["cluster_id"],
        "artifact_type": row["artifact_type"],
        "layer": row["layer"],
        "paths": _diff_paths(str(row.get("diff") or "")),
        "test_paths": [str(path) for path in row.get("test_paths") or []],
        "status": row["status"],
        "pool": row["pool"],
        "confidence": row["confidence"],
        "prompt_version": row["prompt_version"],
        "model": row["model"],
        "rationale": row["rationale"],
        "expected_effect": row["expected_effect"],
        "rollback_ref": dict(row["rollback_ref"])
        if isinstance(row["rollback_ref"], Mapping)
        else {},
        "sandbox_summary": _sandbox_summary(row["gate_log"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _serialize_code_candidate_detail(row: dict, store: CandidateStore) -> dict:
    """One patch plus its diff body, its five-layer trail and its audit rows."""
    payload = _serialize_code_candidate(row)
    payload["diff"] = str(row.get("diff") or "")
    payload["gate_log"] = [_serialize_gate_entry(entry) for entry in row["gate_log"]]
    # 层 2 的两个生产者分开呈现：沙箱（应用 + 目标测试 + 固定回归网，回放按设计不量）
    # 与宿主 A/B（真实的干净侧 vs 打补丁侧）。合并成一个列表会让"这次数字是谁量出来的"消失。
    payload["sandbox_summary"] = _sandbox_summary(row["gate_log"])
    payload["host_ab_summary"] = _host_ab_summary(row["gate_log"])
    payload["audit"] = [
        _serialize_candidate_audit(entry) for entry in store.list_audit(row["id"])
    ]
    # 命令是给管理员**复制**的，端点从不执行它们（批准只导出文件）。命令只有导出后才可用，
    # 所以连导出与否一起给出来，前端才能如实呈现"先批准、再复制"。
    test_paths = [str(path) for path in (row.get("test_paths") or [])]
    export_path = _patch_export_path(int(row["id"]))
    payload["apply_command"] = _apply_command(int(row["id"]), test_paths)
    payload["revert_command"] = _revert_command(int(row["id"]))
    payload["export_path"] = str(export_path)
    payload["exported"] = export_path.is_file()
    return payload


def _patch_export_path(candidate_id: int) -> Path:
    """``artifacts/patches/candidate-<id>.patch`` under the export directory."""
    return PATCH_EXPORT_DIR / f"candidate-{int(candidate_id)}.patch"


def _apply_command(candidate_id: int, test_paths: list[str]) -> str:
    """The command an administrator runs by hand — this endpoint never runs it.

    ``--3way`` so a base commit that has since moved still resolves when the
    context matches, followed by the verification targets the sandbox already
    ran. The command is written into the exported header comment and the audit
    note, so the trail names the exact steps a human performed afterwards.
    """
    verification = " ".join(path for path in test_paths if path)
    # **不要**加 ``--3way``：导出的补丁不带 ``index`` 行（difflib 不产生），而 3-way 需要 blob
    # 哈希，实测会直接失败（"repository lacks the necessary blob"）⇒ 那条命令照着跑会坑人。
    command = (
        f"git -C {REPO_ROOT} apply {_PATCH_EXPORT_SUBDIR}/candidate-{int(candidate_id)}.patch"
    )
    return f"{command} && python -m pytest {verification}" if verification else command


def _revert_command(candidate_id: int) -> str:
    """The command that reverses the exported patch, for the human to run.

    Design §10 asks a release to be reversible without editing files in place.
    Producing the reverse command — never running it — is the part of that
    requirement this endpoint can honour: reversing an applied patch is a ``git
    apply -R`` of the same file, and the exported snapshot records it next to the
    apply command so a rollback does not depend on anyone remembering it.
    """
    return (
        f"git -C {REPO_ROOT} apply -R {_PATCH_EXPORT_SUBDIR}/candidate-{int(candidate_id)}.patch"
    )


def _snapshot_export_path(candidate_id: int) -> Path:
    """``artifacts/patches/candidate-<id>.snapshot.json`` under the export directory."""
    return PATCH_EXPORT_DIR / f"candidate-{int(candidate_id)}.snapshot.json"


def _patch_file_text(
    candidate_id: int, candidate: dict, apply_command: str, revert_command: str
) -> str:
    """The exported file: a header comment, then the diff body verbatim.

    The comment carries what a reviewer needs at the moment they apply it — the
    layer, the base commit the diff was generated against, the touched files, the
    verification targets and both commands (apply and its reverse). ``git apply``
    skips leading comment lines, so the file stays a valid patch.
    """
    rollback_ref = candidate.get("rollback_ref")
    base_commit = (
        str(rollback_ref.get("base_commit", ""))
        if isinstance(rollback_ref, Mapping)
        else ""
    )
    paths = _diff_paths(str(candidate.get("diff") or ""))
    test_paths = [str(path) for path in candidate.get("test_paths") or []]
    header = [
        f"# candidate-{int(candidate_id)}.patch — 进化补丁审核导出（approve_for_apply）",
        "# 系统永不自动应用本补丁；应用、跑测试与重启服务均由管理员按下面的命令手动执行。",
        f"# layer: {candidate.get('layer') or '—'}",
        f"# base_commit: {base_commit or '—'}",
        f"# files: {', '.join(paths) if paths else '—'}",
        f"# test_paths: {', '.join(test_paths) if test_paths else '—'}",
        f"# apply: {apply_command}",
        f"# revert: {revert_command}",
    ]
    diff = str(candidate.get("diff") or "")
    if diff and not diff.endswith("\n"):
        diff += "\n"
    return "\n".join(header) + "\n" + diff


def _snapshot_payload(
    candidate: dict, *, apply_command: str, revert_command: str
) -> dict:
    """The machine-readable companion of one export (design §10, minimal form).

    A full immutable version snapshot store does not exist and is not built here.
    What an administrator needs in order to roll a single applied patch back —
    the base commit, the exact commands, the verification targets and what the
    sandbox concluded — is written next to the patch instead, so the export is
    self-describing and a later rollback does not depend on this process.
    """
    rollback_ref = candidate.get("rollback_ref")
    base_commit = (
        str(rollback_ref.get("base_commit", ""))
        if isinstance(rollback_ref, Mapping)
        else ""
    )
    diff = str(candidate.get("diff") or "")
    return {
        "candidate_id": int(candidate["id"]),
        "artifact_type": candidate.get("artifact_type") or "",
        "layer": candidate.get("layer") or "",
        "base_commit": base_commit,
        "paths": _diff_paths(diff),
        "test_paths": [str(path) for path in candidate.get("test_paths") or []],
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "apply_command": apply_command,
        "revert_command": revert_command,
        "gate_summary": _sandbox_summary(candidate.get("gate_log")),
        "prompt_version": candidate.get("prompt_version") or "",
        "model": candidate.get("model") or "",
        "exported_at": time.time(),
    }


def _export_patch(candidate: dict) -> tuple[Path, str]:
    """Write the exported patch and its snapshot, and return the path plus the apply command.

    Exporting is the entire effect of an approval: the directory is created when
    it is missing, two files are written, and nothing else in the repository
    moves. A row without a diff body cannot be exported, so the endpoint refuses
    it before this runs (an empty ``.patch`` would be worse than no export).
    """
    candidate_id = int(candidate["id"])
    test_paths = [str(path) for path in candidate.get("test_paths") or []]
    apply_command = _apply_command(candidate_id, test_paths)
    revert_command = _revert_command(candidate_id)
    export_path = _patch_export_path(candidate_id)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(
        _patch_file_text(candidate_id, candidate, apply_command, revert_command),
        encoding="utf-8",
    )
    _snapshot_export_path(candidate_id).write_text(
        json.dumps(
            _snapshot_payload(
                candidate, apply_command=apply_command, revert_command=revert_command
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return export_path, apply_command


def _decision_note(
    action: str, note: str, export_path: Path | None, apply_command: str, *, merged_commit: str = ""
) -> str:
    """The audit note: the administrator's words, plus what the decision produced.

    ``advance_candidate`` writes this note as the audit row of the same
    transaction that moves the status, so the trail always names the exported
    file and the command a human has to run afterwards — and for the other
    decisions it stays exactly what the administrator typed. A close-out names
    the commit whose content the row is being closed against, because that sha
    is the thing a later reader has to open to check the claim.
    """
    if action == "close_out_merged":
        detail = f"改动已随提交 {merged_commit or '<未知>'} 落地，结案为 stable"
        return f"{detail}；备注：{note}" if note else detail
    if action != "approve_for_apply" or export_path is None:
        return note
    detail = f"导出补丁 {export_path}；应用命令：{apply_command}"
    return f"{detail}；备注：{note}" if note else detail


def _added_patch_lines(diff: str) -> list[str]:
    """The content lines a diff adds, headers excluded.

    ``patch_is_committed`` asks whether the paths a patch touches are clean relative to HEAD, and
    a file nobody ever touched is clean too — so "clean" alone cannot tell "this change is in a
    commit" from "this change is nowhere". The probe that tells them apart is the content itself:
    the lines the diff adds have to exist in the file for the claim to be true.
    """
    lines: list[str] = []
    for line in str(diff or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if not body.strip() or body.startswith("# "):
            continue
        lines.append(body)
    return lines


def _added_lines_present_in_tree(diff: str) -> tuple[bool, str]:
    """Is every content line the diff adds present in the working-tree files it names?

    A path that does not exist, or one where any added line is missing, fails: both mean the
    patch's content is not in the code, whatever ``git status`` says about the directory. Removal
    hunks are deliberately not checked — a patch whose lines are still there is "not reversed",
    which is all this guard needs to establish.
    """
    from rag.evolution.service_control import patch_paths

    paths = patch_paths(diff)
    if not paths:
        return False, "diff 里没有可核对的文件路径"
    added = _added_patch_lines(diff)
    if not added:
        return False, "diff 里没有可核对的新增内容行，无法证明改动已在代码里"
    for path in paths:
        target = REPO_ROOT / path
        if not target.is_file():
            return False, f"{path} 不在当前代码里"
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:  # pragma: no cover - unreadable source
            return False, f"{path} 无法读取（{type(error).__name__}）"
        for line in added:
            if line not in text:
                return False, f"{path} 里找不到补丁新增的内容行：{line.strip()[:60]}"
    return True, ""


def _merged_commit_for_closeout(row: dict, claimed: str) -> str:
    """Resolve the commit a close-out names, and refuse when the tree disagrees.

    A row that is closed for being merged has to be closed against something, and the claim has to
    survive two independent tests — because "not shipped" silently recorded as "accepted" is this
    path's dangerous failure mode:

    1. the patch must be **present** in the tree (``patch_state``) while the paths it touches are
       **clean relative to HEAD** (``patch_is_committed``): that combination is what a committed
       change looks like, and it is the same judgement rollback uses to send a committed patch to
       ``git revert``;
    2. the content the diff adds must actually be found in those files (this is the test that
       separates "committed" from "never changed at all", which step 1 cannot do on its own).

    The claimed sha is deliberately *not* trusted as the only evidence: a reader checking the row
    later wants a commit to open, so when the caller names one it is used, and otherwise the commit
    that last touched these paths is looked up. Either way the refusal names what to do instead.
    """
    from rag.evolution.service_control import patch_is_committed, patch_paths, patch_state

    diff = str(row.get("diff") or "")
    if not diff.strip():
        raise HTTPException(status_code=409, detail="该候选没有存储的 diff，无法核对是否已合入")
    state = patch_state(REPO_ROOT, diff)
    if state == "not_applied":
        raise HTTPException(
            status_code=409,
            detail=(
                "该补丁的改动并不在当前代码里，不能按“已合入”结案；"
                "请先应用它，或改用 reject / needs_revision"
            ),
        )
    if state == "conflict":
        raise HTTPException(
            status_code=409,
            detail="补丁与当前代码不再匹配（conflict），无法判定是否已合入；请人工核对后再决定",
        )
    paths = patch_paths(diff)
    if not patch_is_committed(REPO_ROOT, paths):
        raise HTTPException(
            status_code=409,
            detail=(
                "该补丁的改动还在工作树里、没有进入任何提交，不能按“已合入”结案；"
                "请先提交，或改用 apply 让系统记录它已上线"
            ),
        )
    present, why = _added_lines_present_in_tree(diff)
    if not present:
        raise HTTPException(
            status_code=409,
            detail=(
                f"核对未通过：{why}。“这些文件相对 HEAD 干净”本身不足以证明改动已合入"
                "（从未被改动的文件同样干净），所以此处按内容核对，未通过即拒绝结案"
            ),
        )
    resolved = _git_last_commit_for(paths)
    claimed = str(claimed or "").strip()
    return claimed or resolved


def _serialize_generation_queue_row(row: dict) -> dict:
    """One queue row: both layer authorities, the effective one, and the veto.

    A row is actionable when the effective layer is one an administrator may
    confirm. It is not actionable when nothing names a layer yet (no confirmation
    and no draft) — which is exactly the state in which spending a model call
    would be wasteful — or when the named layer is outside the taxonomy.
    """
    confirmed = str(row.get("confirmed_layer") or "").strip()
    suggested = str(row.get("draft_suggested_layer") or "").strip()
    effective = confirmed or suggested
    if confirmed:
        authority = "confirmed"
    elif suggested:
        authority = "draft"
    else:
        authority = ""
    if not effective:
        blocked: str | None = "缺少确定层级：请先确认根因层级，或等待该观测的诊断草案"
    elif effective not in CONFIRMABLE_LAYERS:
        blocked = f"层级 {effective} 不能用于代码补丁（支持 {' / '.join(CONFIRMABLE_LAYERS)}）"
    else:
        blocked = None
    return {
        "cluster_id": int(row["cluster_id"]),
        "cluster_status": row["cluster_status"],
        "observation_id": int(row["observation_id"]),
        "question": row["question"],
        "kb_id": row.get("observation_kb_id") or row.get("cluster_kb_id") or "",
        "failure_stage": row.get("cluster_stage") or row.get("observation_stage") or "",
        "root_cause_feature": row.get("root_cause_feature") or "",
        "confirmed_layer": confirmed or None,
        "draft_suggested_layer": suggested or None,
        "draft_prompt_version": row.get("draft_prompt_version") or None,
        "effective_layer": effective,
        "layer_authority": authority,
        "blocked_reason": blocked,
    }


@router.get("/code-generation-queue", summary="待生成代码补丁的观测清单（只读）")
def list_code_generation_queue(
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(_admin_user),
) -> dict:
    """Member observations a manual generation could act on, oldest first.

    Read-only by design: the phase-5 generator is deliberately outside the
    worker's round and no endpoint triggers it, so this list is what an
    administrator reads before triggering a generation by hand. Every row names
    the layer the generator would apply — the cluster's administrator-confirmed
    layer when one exists, otherwise the newest diagnosis draft's suggestion —
    plus ``blocked_reason`` when that layer is missing or is not one a code patch
    may address. Nothing here calls a model or writes anything.
    """
    store = _open_candidate_store()
    rows = (
        []
        if store is None
        else store.observations_awaiting_code_patch(limit=MAX_CANDIDATE_ROWS)
    )
    return _page([_serialize_generation_queue_row(row) for row in rows], page, page_size)


@router.get("/code-candidates", summary="代码补丁候选列表")
def list_code_candidates(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(_admin_user),
) -> dict:
    """Code-patch candidates newest first, with the sandbox conclusion per row.

    A missing database means nothing was recorded yet, so the page reads empty
    without creating the file. There is no percentage to report and no rollout to
    reconcile: layer 4 for a code patch is the review endpoint below, and the
    diff body never leaves the detail response.
    """
    status = _filter_value(status, _STATUSES, "状态")
    store = _open_candidate_store()
    rows = (
        []
        if store is None
        else [
            row
            for row in store.list_candidates(status=status, limit=MAX_CANDIDATE_ROWS)
            if row["artifact_type"] == CODE_PATCH_ARTIFACT_TYPE
        ]
    )
    # 近重复提示（确定性、只做提示从不拦截）：同一条改动的多个变体并排留在队列里，正是
    # "补丁堆成屎山"的来源，而队列本身不告诉评审这件事。
    from rag.evolution.overlap import group_by_overlap
    from rag.evolution.patch_generator import _layer_files

    keys, groups = group_by_overlap(
        rows, repo_root=REPO_ROOT, files=_layer_files(REPO_ROOT, "code")
    )
    items = []
    for row in rows:
        item = _serialize_code_candidate(row)
        key = keys.get(int(row["id"]), "")
        if key:
            item["overlap_key"] = key
            others = [other for other in groups.get(key, []) if other != int(row["id"])]
            if others:
                item["overlap_ids"] = others
        items.append(item)
    return _page(items, page, page_size)


@router.get("/code-candidates/{candidate_id}", summary="代码补丁详情、diff 全文与门禁轨迹")
def get_code_candidate(candidate_id: int, user: dict = Depends(_admin_user)) -> dict:
    """One patch with its diff, its five-layer trail and its lifecycle audit.

    The trail is where the whole evidence lives: ``scope`` (layer 1, written at
    generation), ``sandbox`` (layers 2/3 — the isolated worktree run) and, once an
    administrator decides, ``review`` (layer 4). A missing database and an unknown
    id both read as 404, and neither creates the file.
    """
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="观测数据库不存在")
    row = _require_candidate(store, candidate_id)
    return _serialize_code_candidate_detail(row, store)


@router.get("/gap-tickets", summary="内容缺口工单列表")
def list_gap_tickets(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(_admin_user),
) -> dict:
    """Knowledge-gap tickets newest first, read-only.

    A ticket is a request for content, not a candidate: it never enters the candidate pool and no
    gate reads it, which is why this surface has no write method at all. The only thing that moves
    a ticket is the worker's deterministic re-check after material has been ingested into that
    knowledge base — the administrator adds content through the materials pipeline, not here.
    """
    status = _filter_value(status, GAP_TICKET_STATUSES, "状态")
    store = _open_store()
    rows = [] if store is None else store.list_gap_tickets(status=status)
    items = [
        {
            "id": int(row["id"]),
            "cluster_id": int(row["cluster_id"]),
            "kb_id": str(row["kb_id"]),
            "status": str(row["status"]),
            "occurrences": int(row["occurrences"]),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            **(row.get("payload") or {}),
        }
        for row in rows
    ]
    return _page(items, page, page_size)


class GapTicketRemediateWikiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: int
    auto_recheck: bool = True


@router.post("/gap-tickets/{ticket_id}/remediate/wiki", summary="关联 Wiki 页面并触发闭环复测")
def remediate_gap_ticket_wiki(
    ticket_id: int,
    body: GapTicketRemediateWikiRequest,
    user: dict = Depends(_admin_user),
) -> dict:
    from rag.evolution.gap_remediation import GapRemediationService
    from rag.wiki.api import get_wiki_store

    store = _open_store()
    if store is None:
        raise HTTPException(status_code=404, detail="演进数据库未初始化")

    wiki_store = get_wiki_store()
    service = GapRemediationService(trace_store=store, wiki_store=wiki_store)

    try:
        link_res = service.link_ticket_to_wiki(
            ticket_id=ticket_id,
            page_id=body.page_id,
            author_id=user.get("username"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    recheck_res = None
    if body.auto_recheck:
        recheck_res = service.recheck_ticket(ticket_id=ticket_id)

    return {
        "ticket_id": ticket_id,
        "remediation": link_res["remediation"],
        "recheck": recheck_res,
    }


@router.get("/gap-tickets/{ticket_id}/remediation", summary="获取工单关联的补录资产详情")
def get_gap_ticket_remediation(
    ticket_id: int,
    user: dict = Depends(_admin_user),
) -> dict:
    from rag.evolution.gap_remediation import GapRemediationService
    from rag.wiki.api import get_wiki_store

    store = _open_store()
    if store is None:
        raise HTTPException(status_code=404, detail="演进数据库未初始化")

    wiki_store = get_wiki_store()
    service = GapRemediationService(trace_store=store, wiki_store=wiki_store)

    detail = service.get_ticket_remediation(ticket_id)
    if not detail:
        raise HTTPException(status_code=404, detail="未找到关联资产信息")
    return detail


class ApplyRequest(BaseModel):
    """One administrative action on an already-approved patch."""

    model_config = ConfigDict(extra="forbid")

    #: Restart the service once the guards pass (the helper waits for the port, starts it, polls
    #: /health and rolls the patch back if it never comes up).
    restart: bool = True
    note: str = ""


@contextmanager
def _apply_lease():
    """Hold the exclusive apply/rollback lease for one request (C-11, 2026-10-22).

    Applying and rolling back both move the working tree and then the candidate's status, so two of
    them interleaved — from two administrators, two browser tabs, or the API and a script — leave the
    tree and the trail describing different things. The lease is the same atomic file lock the host
    A/B uses (:mod:`rag.evolution.host_lock`), taken on its own file
    (:func:`rag.evolution.service_control.apply_lock_path`) because a host measurement may legitimately
    run beside an apply.

    A held lease is a **409**, not a wait: the operation is minutes long, and queueing a second one
    silently is worse than telling the caller someone else is doing it. It is also cross-process, so
    a CLI or a second API process is refused the same way.
    """
    from rag.evolution.host_lock import HostLocked, host_measurement_lock
    from rag.evolution.service_control import apply_lock_path

    try:
        with host_measurement_lock(apply_lock_path(REPO_ROOT)):
            yield
    except HostLocked as error:
        raise HTTPException(
            status_code=409,
            detail=f"另一个应用/回滚正在进行，请等它结束：{error}",
        ) from error


@router.post("/code-candidates/{candidate_id}/apply", summary="应用已批准的补丁，可选一键重启")
def apply_code_candidate(
    candidate_id: int, payload: ApplyRequest, user: dict = Depends(_admin_user)
) -> dict:
    """Apply an approved patch behind guards, then optionally restart the service.

    This does not soften "the system never merges on its own": it acts only on a candidate an
    administrator already approved, and only when a human clicks. What it adds is the safety the
    manual sequence lacks — the patch must apply cleanly, the application must still import, and the
    patch's own target tests must pass **before** anything is restarted. Any failure reverts the
    patch and leaves the running service untouched.

    The whole sequence runs under the apply lease (C-11): the status advance and the working-tree
    operations are serialized with every other apply/rollback, and the restart helper inherits the
    same lease so a second apply cannot start while the process is being swapped.

    The outcome is returned in the response and appended to ``artifacts/service-control.log``. The
    evolution audit table is deliberately not used: its action column is CHECK-constrained to the
    three diagnosis decisions, and rebuilding a table to record an operational event is not worth it.
    """
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="候选数据库不存在")
    with _apply_lease():
        return _apply_code_candidate_locked(store, candidate_id, payload, user)


def _apply_code_candidate_locked(
    store: CandidateStore, candidate_id: int, payload: ApplyRequest, user: dict
) -> dict:
    """The apply body, run inside the lease.

    The preconditions are read **after** the lease is taken, so the state this run acts on cannot be
    changed by another apply between the read and the write.
    """
    from rag.evolution.models import EvolutionStatus
    from rag.evolution.service_control import (
        apply_patch,
        import_smoke,
        log_line,
        patch_state,
        revert_patch,
        run_target_tests,
        service_log_path,
        spawn_restart_helper,
    )

    row = _require_candidate(store, candidate_id)
    if row["artifact_type"] != CODE_PATCH_ARTIFACT_TYPE:
        raise HTTPException(status_code=404, detail="候选不存在")
    # 已批准的补丁可以应用；**已在观察（canary）的也可以**——那就是"再点一次＝校验 + 重启"，
    # 而这个动作在 apply 现在会把状态推到 canary 之后，才真正可能被用到（此前 canary 只在
    # 回滚前置那一步短暂出现）。stable 不接受：接受过的补丁要走 git revert，不是再 apply。
    if row["status"] not in {
        EvolutionStatus.APPROVED_FOR_CANARY.value,
        EvolutionStatus.CANARY.value,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"只有已批准的补丁才能应用（当前状态：{row['status']}）",
        )
    diff = str(row.get("diff") or "")
    if not diff:
        raise HTTPException(status_code=409, detail="该候选没有存储的 diff")

    state = patch_state(REPO_ROOT, diff)
    if state == "conflict":
        raise HTTPException(status_code=409, detail="补丁与当前代码不再匹配（conflict）")

    steps: dict = {"patch_state": state}
    applied_now = state == "not_applied"
    if applied_now:
        try:
            apply_patch(REPO_ROOT, diff)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    def _revert_and_refuse(reason: str, detail: str, status_code: int = 409) -> None:
        if applied_now:
            try:
                revert_patch(REPO_ROOT, diff)
                log_line(
                    service_log_path(REPO_ROOT), f"candidate {candidate_id}: reverted ({reason})"
                )
            except RuntimeError as revert_error:  # pragma: no cover - needs a broken tree
                raise HTTPException(
                    status_code=500,
                    detail=f"{reason}；且回滚也失败：{revert_error}（需要人工处理）",
                ) from revert_error
        raise HTTPException(status_code=status_code, detail=detail)

    smoke_ok, smoke_why = import_smoke(REPO_ROOT)
    steps["import_smoke"] = smoke_ok
    if not smoke_ok:
        _revert_and_refuse("import_smoke", f"应然后无法导入：{smoke_why}")

    tests_ok, tests_why = run_target_tests(REPO_ROOT, tuple(row.get("test_paths") or ()))
    steps["target_tests"] = tests_ok
    if not tests_ok:
        _revert_and_refuse("target_tests", f"目标测试未通过：{tests_why}")

    # 状态先与事实对齐（与 rollback 的开头同一手法、方向相反）：补丁现在**确实**在运行代码里了，
    # 那就是"已上线、在观察"。此前 apply 不写这一步，于是"已应用"只存在于工作树与
    # service-control.log 里，队列上仍显示 approved_for_canary —— 时间一长就分不清哪些行已经
    # 落地、哪些还没，而 `_clusters_with_open_candidates` 正是按状态判断"这个聚类还有没有未决"。
    # rolled_back 侧也是同一个坑的另一面：回滚若不先写 canary，状态就到不了 rolled_back。
    if row["status"] != EvolutionStatus.CANARY.value:
        store.advance_candidate(
            candidate_id,
            EvolutionStatus.CANARY.value,
            actor_id=str(user["id"]),
            note=f"应用前置：补丁已进入工作树，状态与事实对齐（进入观察）｜{payload.note}".strip("｜"),
        )
    # 无论刚才有没有写、状态都是 canary：调用方据此知道"这一条现在说的是事实"。
    steps["status_after_apply"] = EvolutionStatus.CANARY.value

    restart_path = ""
    if payload.restart:
        restart_path = str(
            spawn_restart_helper(
                repo_root=REPO_ROOT,
                patch_file=_patch_export_path(int(row["id"])),
                candidate_id=int(row["id"]),
                test_scope=str(getattr(settings, "evolution_apply_test_scope", "full") or "full"),
                # 助手必须能停掉**当前**服务（否则端口一直被占，新代码永远起不来）。
                service_pid=os.getpid(),
            )
        )
        steps["restart_requested"] = True
    else:
        steps["restart_requested"] = False
        steps["warning"] = "未请求重启 ⇒ 全量测试没有跑；请自行确认后再上线"

    log_line(
        service_log_path(REPO_ROOT),
        f"candidate {candidate_id} applied by {user.get('username')} "
        f"(applied_now={applied_now}, restart={payload.restart}, note={payload.note[:60]!r})",
    )
    return {
        "applied": True,
        "applied_now": applied_now,
        "steps": steps,
        "restart_helper": restart_path,
        "apply_command": _apply_command(int(row["id"]), list(row.get("test_paths") or ())),
        "revert_command": _revert_command(int(row["id"])),
    }


def _git_last_commit_for(paths: tuple[str, ...]) -> str:
    """The short sha of the last commit touching ``paths``, or ``""`` when git cannot say.

    Only used to make a refusal actionable: a patch that is already committed is undone with
    ``git revert``, and naming the commit is the difference between "somewhere in history" and one
    command someone can paste.
    """
    if not paths:
        return ""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%h", "--", *paths],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


class RollbackRequest(BaseModel):
    """One administrative action on a patch that is already applied."""

    model_config = ConfigDict(extra="forbid")

    #: Restart the service once the guards pass. The helper runs the full suite first and, if the
    #: suite fails or the service never comes up, puts the patch back — the mirror of apply.
    restart: bool = True
    note: str = ""


@router.post("/code-candidates/{candidate_id}/rollback", summary="回滚已应用的补丁，可选一键重启")
def rollback_code_candidate(
    candidate_id: int, payload: RollbackRequest, user: dict = Depends(_admin_user)
) -> dict:
    """Undo an applied patch behind the same guards apply uses.

    The asymmetry this closes: a code patch has a one-click apply but no one-click rollback. Layer 5
    for a code patch is "reverse the change and restart", and the existing rollback endpoint only
    unregisters question-set rollouts, so the way back was a hand-typed command — the one step of the
    five layers with no supported path. The guards are symmetric on purpose: if the reverted tree does
    not import, or the patch's own target tests fail, the patch is **put back** and the service is left
    alone, so a rollback that would leave the code worse than before cannot land either.

    A patch whose change is already in a commit is refused rather than reversed: ``git apply -R`` would
    leave the working tree holding the inverse of a commit, and the honest way to undo a commit is
    ``git revert``. The refusal names the commit that last touched those paths.

    The whole sequence runs under the same apply lease as apply (C-11), and its restart helper
    inherits that lease — a rollback and an apply cannot interleave their tree operations or status
    moves.
    """
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="候选数据库不存在")
    with _apply_lease():
        return _rollback_code_candidate_locked(store, candidate_id, payload, user)


def _rollback_code_candidate_locked(
    store: CandidateStore, candidate_id: int, payload: RollbackRequest, user: dict
) -> dict:
    """The rollback body, run inside the lease (preconditions read under it)."""
    from rag.evolution.models import EvolutionStatus
    from rag.evolution.service_control import (
        apply_patch,
        import_smoke,
        log_line,
        patch_is_committed,
        patch_paths,
        patch_state,
        revert_patch,
        run_target_tests,
        service_log_path,
        spawn_restart_helper,
    )

    row = _require_candidate(store, candidate_id)
    if row["artifact_type"] != CODE_PATCH_ARTIFACT_TYPE:
        raise HTTPException(status_code=404, detail="候选不存在")
    allowed = {EvolutionStatus.APPROVED_FOR_CANARY.value, EvolutionStatus.CANARY.value}
    if row["status"] not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"只有已应用的补丁才能回滚（当前状态：{row['status']}）",
        )
    diff = str(row.get("diff") or "")
    if not diff:
        raise HTTPException(status_code=409, detail="该候选没有存储的 diff")

    state = patch_state(REPO_ROOT, diff)
    if state == "not_applied":
        raise HTTPException(status_code=409, detail="补丁当前并未应用，无需回滚")
    if state == "conflict":
        raise HTTPException(status_code=409, detail="补丁与当前代码不再匹配（conflict）")

    paths = patch_paths(diff)
    if patch_is_committed(REPO_ROOT, paths):
        last = _git_last_commit_for(paths)
        raise HTTPException(
            status_code=409,
            detail=(
                "该补丁的改动已经在提交里：请用 git revert "
                f"{last or '<sha>'} 回滚那次提交，而不是在工作树里反向应用"
            ),
        )

    steps: dict = {"patch_state": state}
    # 状态先与事实对齐：补丁是**已应用**的，那就是"已上线、在观察"。这一步也让 canary → rolled_back
    # 成为合法转移（approved_for_canary 不能直接到 rolled_back）。
    if row["status"] != EvolutionStatus.CANARY.value:
        store.advance_candidate(
            candidate_id,
            EvolutionStatus.CANARY.value,
            actor_id=str(user["id"]),
            note=f"回滚前置：确认补丁已应用，进入观察｜{payload.note}".strip("｜"),
        )

    try:
        revert_patch(REPO_ROOT, diff)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    def _restore_and_refuse(reason: str, detail: str, status_code: int = 409) -> None:
        try:
            apply_patch(REPO_ROOT, diff)
            log_line(
                service_log_path(REPO_ROOT),
                f"candidate {candidate_id}: rollback refused, patch put back ({reason})",
            )
        except RuntimeError as restore_error:  # pragma: no cover - needs a broken tree
            raise HTTPException(
                status_code=500,
                detail=f"{reason}；且恢复补丁也失败：{restore_error}（需要人工处理）",
            ) from restore_error
        raise HTTPException(status_code=status_code, detail=detail)

    smoke_ok, smoke_why = import_smoke(REPO_ROOT)
    steps["import_smoke"] = smoke_ok
    if not smoke_ok:
        _restore_and_refuse("import_smoke", f"回滚之后无法导入：{smoke_why}；已把补丁放回")

    tests_ok, tests_why = run_target_tests(REPO_ROOT, tuple(row.get("test_paths") or ()))
    steps["target_tests"] = tests_ok
    if not tests_ok:
        _restore_and_refuse("target_tests", f"回滚之后目标测试未通过：{tests_why}；已把补丁放回")

    updated = store.advance_candidate(
        candidate_id,
        EvolutionStatus.ROLLED_BACK.value,
        actor_id=str(user["id"]),
        note=f"回滚：反向应用 + 目标测试通过｜{payload.note}".strip("｜"),
    )

    restart_path = ""
    if payload.restart:
        restart_path = str(
            spawn_restart_helper(
                repo_root=REPO_ROOT,
                patch_file=_patch_export_path(int(row["id"])),
                candidate_id=int(row["id"]),
                test_scope=str(getattr(settings, "evolution_apply_test_scope", "full") or "full"),
                service_pid=os.getpid(),
                direction="revert",
            )
        )
        steps["restart_requested"] = True
    else:
        steps["restart_requested"] = False
        steps["warning"] = "未请求重启 ⇒ 全量测试没有跑，运行中的服务仍是旧代码"

    log_line(
        service_log_path(REPO_ROOT),
        f"candidate {candidate_id} rolled back by {user.get('username')} "
        f"(restart={payload.restart}, note={payload.note[:60]!r})",
    )
    return {
        "reverted": True,
        "status": updated["status"] if isinstance(updated, dict) else EvolutionStatus.ROLLED_BACK.value,
        "steps": steps,
        "restart_helper": restart_path,
        "revert_command": _revert_command(int(row["id"])),
        "apply_command": _apply_command(int(row["id"]), list(row.get("test_paths") or ())),
    }


class RegenerateRequest(BaseModel):
    """One administrative action on a patch the system already rolled back or rejected."""

    model_config = ConfigDict(extra="forbid")

    #: Which member observation the new patch is generated from. Empty means "the one the
    #: closed-out candidate came from", which is the reproduce-the-same-idea default.
    observation_id: int | None = None
    note: str = ""


#: 一个代码补丁候选可以"结案后重生"的状态（C-08，2026-10-22）。三者都是终态，也都意味着
#: 那条观测不再有任何未决补丁；重生是**新行**，不是复活旧行。
_REGENERABLE_STATUSES: frozenset[str] = frozenset(
    {
        EvolutionStatus.ROLLED_BACK.value,
        EvolutionStatus.REJECTED.value,
        EvolutionStatus.NEEDS_MORE_EVIDENCE.value,
    }
)


def _patch_generator():
    """The generator a regeneration calls; a seam so tests never reach a model.

    The timeout and format come from the same settings the worker's generation step uses, so
    an administrator-triggered regeneration and a round-generated candidate are the same thing
    built by the same code — the only difference is who asked.
    """
    from rag.evolution.patch_generator import PatchGenerator

    return PatchGenerator(
        timeout_seconds=float(getattr(settings, "evolution_patch_timeout_seconds", 600.0)),
        patch_format=str(getattr(settings, "evolution_patch_format", "diff") or "diff"),
    )


def _observation_for_regeneration(
    store, candidate: dict, cluster_id: int, requested: int | None
) -> int:
    """The member observation a regenerated patch is based on.

    ``observation_id`` is the argument's name in every generation the worker makes, so a
    regeneration that omitted it would produce a row nobody can join back to its evidence.
    The rolled-back candidate's own observation is the default: the same failure, re-diagnosed
    with the current draft and the current code. An explicit id must still be a member of the
    same cluster — a patch generated across clusters would attribute one failure's evidence to
    another's candidate.
    """
    observation_id = int(requested) if requested else candidate.get("observation_id")
    if observation_id is None:
        raise HTTPException(status_code=409, detail="该候选没有绑定观测，无法重新生成")
    observation = store.get_observation(int(observation_id))
    if observation is None:
        raise HTTPException(status_code=409, detail=f"观测 {int(observation_id)} 不存在，无法重新生成")
    if int(observation.get("cluster_id") or 0) != int(cluster_id):
        raise HTTPException(
            status_code=409,
            detail=f"观测 {int(observation_id)} 不属于聚类 {cluster_id}，无法重新生成",
        )
    return int(observation_id)


def _draft_for_regeneration(store, observation_id: int, layer: str) -> dict:
    """The newest diagnosis draft of ``observation_id``, when it names the requested layer.

    ``PatchGenerator`` refuses a draft whose ``suggested_layer`` disagrees with the layer it is
    asked for. Rather than hunt for an older draft that happens to match — which would hide a
    re-diagnosis that changed its mind — this returns the newest draft and refuses with that
    reason, without spending a model call that is already known to be refused.
    """
    newest = next(
        (
            draft
            for draft in store.list_drafts()  # newest first
            if draft.get("observation_id") is not None
            and int(draft["observation_id"]) == int(observation_id)
        ),
        None,
    )
    if newest is None:
        raise HTTPException(status_code=409, detail="该观测还没有诊断草案，无法重新生成")
    suggested = str((newest.get("payload") or {}).get("suggested_layer") or "").strip()
    if suggested != layer:
        shown = suggested or "（未给出）"
        raise HTTPException(
            status_code=409,
            detail=f"该观测最新的诊断草案建议层级为 {shown}，与聚类已确认的 {layer} 不一致；"
            "请先重新诊断，或确认层级",
        )
    return newest


@router.post(
    "/code-candidates/{candidate_id}/regenerate",
    summary="回滚/驳回后从同一聚类重新生成一条候选",
)
def regenerate_code_candidate(
    candidate_id: int, payload: RegenerateRequest, user: dict = Depends(_admin_user)
) -> dict:
    """Generate a **new** candidate from the cluster a rolled-back or rejected patch came from.

    ``rolled_back`` is a terminal status: the transition table has no way back to
    ``approved_for_canary``, so after a rollback the interface could not re-apply the same
    change (14.3 gap two; the drill needed an audited manual repair). This closes that gap the
    non-structural way: it does not reopen the rolled-back row, it produces a **new** candidate
    from the same cluster, which then walks the ordinary path — sandbox, administrator review,
    approval, apply. The cluster has to be still ``approved_for_candidate`` with a confirmed
    layer, which is the state it is in after a rollback by construction.

    The same entry serves a **rejected** or **needs_more_evidence** candidate (C-08,
    2026-10-22). Those are terminal too, and they are the normal outcome of a review; without this
    path, "administrator rejects an attempt" removed the observation from the generation queue for
    good (the queue's ``NOT EXISTS`` used to ignore status), so the failure it described could
    never be addressed again except by editing the database.

    Pile guard, same as the worker's: refuse while the cluster carries another open patch
    candidate, so regenerating cannot be used to stack variants of one idea side by side. A
    refusal from the generator itself is a 409 carrying its reason, never a silent success and
    never a row without its audit trail.
    """
    from rag.evolution.diagnoser import DiagnosisUnavailable
    from rag.evolution.patch_generator import PatchUnavailable
    from rag.evolution.worker import _clusters_with_open_candidates

    store = _open_store()
    candidates = _open_candidate_store()
    if store is None or candidates is None:
        raise HTTPException(status_code=404, detail="观测数据库不存在")
    row = _require_candidate(candidates, candidate_id)
    if row["artifact_type"] != CODE_PATCH_ARTIFACT_TYPE:
        raise HTTPException(status_code=404, detail="候选不存在")
    if row["status"] not in _REGENERABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "只有回滚过或被驳回的补丁才需要重新生成（当前状态："
                f"{row['status']}）；未决补丁请继续走原有路径"
            ),
        )

    cluster_id = int(row["cluster_id"])
    cluster = next(
        (item for item in store.list_clusters() if int(item["id"]) == cluster_id), None
    )
    if cluster is None:
        raise HTTPException(status_code=409, detail=f"聚类 {cluster_id} 不存在，无法重新生成")
    if cluster.get("status") != EvolutionStatus.APPROVED_FOR_CANDIDATE.value:
        raise HTTPException(
            status_code=409,
            detail=f"聚类 {cluster_id} 当前状态为 {cluster.get('status')}，不接受生成候选",
        )
    layer = str(cluster.get("confirmed_layer") or "").strip()
    if not layer:
        raise HTTPException(status_code=409, detail="该聚类还没有确认根因层级，无法重新生成")
    open_clusters = _clusters_with_open_candidates(candidates, CODE_PATCH_ARTIFACT_TYPE)
    if cluster_id in open_clusters:
        raise HTTPException(
            status_code=409,
            detail=f"聚类 {cluster_id} 已有未决的补丁候选：请先处理它，再重新生成",
        )

    observation_id = _observation_for_regeneration(
        store, row, cluster_id, payload.observation_id
    )
    draft = _draft_for_regeneration(store, observation_id, layer)

    generator = _patch_generator()
    try:
        candidate = generator.generate(
            cluster,
            [draft.get("payload") or {}],
            layer=layer,
            repo_root=REPO_ROOT,
            observation_id=observation_id,
            cluster_id=cluster_id,
            trace_store=store,
        )
    except (PatchUnavailable, DiagnosisUnavailable) as error:
        # 生成器的拒绝是**答案**，不是异常：把原因如实交回给管理员，且不写任何行。
        raise HTTPException(status_code=409, detail=f"生成被拒绝：{error}") from error

    new_id = candidates.add_candidate(
        observation_id,
        cluster_id,
        candidate,
        prompt_version=str(getattr(generator, "prompt_version", "")),
        model=str(getattr(generator, "model_name", "")),
    )
    candidates.add_audit(
        str(user["id"]),
        "advance",
        new_id,
        (
            f"结案后重新生成：由 {user.get('username')} 从候选 #{int(row['id'])}"
            f"（{row['status']}）"
            f"所在的聚类 {cluster_id} 重新生成，观测 {observation_id}，草案 {draft.get('id')}，"
            f"层级 {layer}｜{payload.note}"
        ).strip("｜"),
    )
    created = candidates.get_candidate(new_id)
    return _serialize_code_candidate_detail(created, candidates)


@router.post("/code-candidates/{candidate_id}/decision", summary="提交代码补丁审核决策")
def decide_code_candidate(
    candidate_id: int, payload: CodePatchDecision, user: dict = Depends(_admin_user)
) -> dict:
    """Layer 4 for a code patch: one audited administrator decision.

    The order is fixed and it is what makes the trail truthful. An unknown action
    is a 400 before anything is read; an unknown candidate or a missing database
    is a 404; an illegal transition (a candidate already decided, or one that
    never passed the sandbox) is a 409 — all three write nothing, because
    ``advance_candidate`` validates inside its transaction and the audit row
    follows the validation, never precedes it. An approval exports the patch
    first, so the audit note can name the exported file and the apply command in
    the same row that records the status move.

    Approving never applies: the endpoint reads the stored diff, writes it under
    ``artifacts/patches/`` and stops. ``git apply``, the tests and the restart
    belong to the administrator, at the command this endpoint records.
    """
    target = CODE_PATCH_DECISIONS.get(payload.action)
    if target is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的审核动作：{payload.action}"
                f"（支持 {' / '.join(sorted(CODE_PATCH_DECISIONS))}）"
            ),
        )
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="观测数据库不存在")
    row = _require_candidate(store, candidate_id)
    # 表是两个制品共用的：这个端点只对代码补丁做层 4 审核。问题集候选的层 4 是 canary，
    # 一条问题集的 payload 被当成补丁决策，状态机会给它开一条它没走过的路（C-05）。
    if row["artifact_type"] != CODE_PATCH_ARTIFACT_TYPE:
        raise HTTPException(status_code=404, detail="候选不存在")
    new_status = target["new_status"]
    if new_status not in EVOLUTION_TRANSITIONS.get(row["status"], ()):
        # Checked here as well as inside the store so the 409 leaves no trace:
        # a rejected decision must not be able to write an audit row.
        raise HTTPException(
            status_code=409,
            detail=f"补丁状态 {row['status']} 不允许 {payload.action}（目标 {new_status}）",
        )
    export_path: Path | None = None
    apply_command = ""
    merged_commit = ""
    if payload.action == "approve_for_apply":
        if not str(row.get("diff") or "").strip():
            raise HTTPException(status_code=409, detail="补丁缺少 diff 正文，拒绝导出")
        export_path, apply_command = _export_patch(row)
    elif payload.action == "close_out_merged":
        merged_commit = _merged_commit_for_closeout(row, payload.merged_commit)
    note = _decision_note(
        payload.action, payload.note, export_path, apply_command, merged_commit=merged_commit
    )
    try:
        updated = store.advance_candidate(
            candidate_id,
            new_status,
            actor_id=str(user["id"]),
            note=note,
            gate=REVIEW_GATE,
            gate_detail=target["gate_detail"],
        )
    except KeyError as error:
        # The candidate disappeared between the read above and this write.
        raise HTTPException(status_code=404, detail="候选不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=f"非法状态转移：{error}") from error
    return _serialize_code_candidate_detail(updated, store)


@router.post("/candidates/{candidate_id}/rollback", summary="一键回滚候选灰度")
def rollback_candidate(candidate_id: int, user: dict = Depends(_admin_user)) -> dict:
    """Layer 5: unregister the candidate question set and zero its percentage.

    Rollback takes effect before the audited transition runs, so traffic is back
    on the current question set even if the move itself is refused; a candidate
    outside canary/stable is refused with a 409 and nothing is written. A
    second rollback of an already rolled-back candidate is an idempotent no-op.
    """
    store = _open_candidate_store()
    if store is None:
        raise HTTPException(status_code=404, detail="观测数据库不存在")
    _reconcile_rollout(store)
    row = _require_candidate(store, candidate_id)
    if row["status"] == EvolutionStatus.ROLLED_BACK.value:
        # Idempotent: the registration is already gone and the percentage is 0.
        rollout.deactivate(candidate_id)
        return _serialize_candidate(row, canary_percent=0)
    if EvolutionStatus.ROLLED_BACK.value not in EVOLUTION_TRANSITIONS.get(row["status"], ()):
        raise HTTPException(
            status_code=409, detail=f"候选状态 {row['status']} 不可回滚（仅 canary / stable 可回滚）"
        )
    previous = rollout.percent_for(candidate_id)
    rollout.deactivate(candidate_id)
    try:
        updated = store.advance_candidate(
            candidate_id,
            EvolutionStatus.ROLLED_BACK.value,
            actor_id=str(user["id"]),
            note=(
                f"回滚：撤销候选问题集注册，灰度百分比置 0（原 {previous}%）"
                f"｜{PERCENT_NOTE_KEY}=0"
            ),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="候选不存在") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=f"非法状态转移：{error}") from error
    return _serialize_candidate(updated, canary_percent=rollout.percent_for(candidate_id))
