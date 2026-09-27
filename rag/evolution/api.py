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

import threading
import time
from collections.abc import Mapping
from pathlib import Path

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
from rag.evolution.models import EVOLUTION_TRANSITIONS, EvolutionStatus, ObservationPool
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
)

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
    return {
        "gate": str(entry.get("gate", "")),
        "result": str(entry.get("result", "")),
        "detail": str(entry.get("detail", "")),
        "created_at": float(entry.get("created_at") or 0.0),
    }


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


def _reconcile_rollout(store: CandidateStore) -> None:
    """Rebuild live rollout state from the persisted trail before acting on it."""
    rollout.restore(store)


def _require_candidate(store: CandidateStore, candidate_id: int) -> dict:
    row = store.get_candidate(candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    return row


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
    actor_id = str(user["id"])
    percent = payload.percent
    current = row["status"]

    if current == EvolutionStatus.CANARY.value:
        # Already live: a percentage change is its own audited action.
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
            raise HTTPException(status_code=404, detail="候选不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=f"非法状态转移：{error}") from error
    else:
        raise HTTPException(
            status_code=409, detail=f"候选状态 {current} 不允许灰度（需 awaiting_verification 或 canary）"
        )

    # The live effect goes last: every audited write above it already
    # succeeded, so a failure here leaves the trail truthful and no traffic.
    rollback_ref = row["rollback_ref"] if isinstance(row["rollback_ref"], Mapping) else {}
    rollout.activate(
        candidate_id,
        row["target"],
        row["payload"],
        str(rollback_ref.get("question_set_version", "")),
        percent,
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
}


class CodePatchDecision(BaseModel):
    """One administrator review decision; the note is audit evidence for it."""

    model_config = ConfigDict(extra="forbid")

    # ``str`` plus an explicit check instead of ``Literal``: an unknown action is
    # a 400 naming the supported set, not FastAPI's generic 422 — the same shape
    # the phase-3 decision endpoint and the question-set canary use.
    action: str
    note: str = ""


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
    exactly what a reviewer needs to see before deciding about it.
    """
    if not isinstance(gate_log, list):
        return []
    return [
        _serialize_gate_entry(entry)
        for entry in gate_log
        if isinstance(entry, Mapping) and entry.get("gate") == SANDBOX_GATE
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
    payload["audit"] = [
        _serialize_candidate_audit(entry) for entry in store.list_audit(row["id"])
    ]
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
    command = (
        f"git -C {REPO_ROOT} apply --3way {_PATCH_EXPORT_SUBDIR}/candidate-{int(candidate_id)}.patch"
    )
    return f"{command} && python -m pytest {verification}" if verification else command


def _patch_file_text(candidate_id: int, candidate: dict, apply_command: str) -> str:
    """The exported file: a header comment, then the diff body verbatim.

    The comment carries what a reviewer needs at the moment they apply it — the
    layer, the base commit the diff was generated against, the touched files, the
    verification targets and the apply command. ``git apply`` skips leading
    comment lines, so the file stays a valid patch.
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
    ]
    diff = str(candidate.get("diff") or "")
    if diff and not diff.endswith("\n"):
        diff += "\n"
    return "\n".join(header) + "\n" + diff


def _export_patch(candidate: dict) -> tuple[Path, str]:
    """Write the exported patch file and return its path and the apply command.

    Exporting is the entire effect of an approval: the directory is created when
    it is missing, the file is written, and nothing else in the repository moves.
    A row without a diff body cannot be exported, so the endpoint refuses it
    before this runs (an empty ``.patch`` would be worse than no export).
    """
    candidate_id = int(candidate["id"])
    test_paths = [str(path) for path in candidate.get("test_paths") or []]
    apply_command = _apply_command(candidate_id, test_paths)
    export_path = _patch_export_path(candidate_id)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    export_path.write_text(
        _patch_file_text(candidate_id, candidate, apply_command), encoding="utf-8"
    )
    return export_path, apply_command


def _decision_note(
    action: str, note: str, export_path: Path | None, apply_command: str
) -> str:
    """The audit note: the administrator's words, plus what an approval produced.

    ``advance_candidate`` writes this note as the audit row of the same
    transaction that moves the status, so the trail always names the exported
    file and the command a human has to run afterwards — and for the two other
    decisions it stays exactly what the administrator typed.
    """
    if action != "approve_for_apply" or export_path is None:
        return note
    detail = f"导出补丁 {export_path}；应用命令：{apply_command}"
    return f"{detail}；备注：{note}" if note else detail


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
    items = [_serialize_code_candidate(row) for row in rows]
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
    if payload.action == "approve_for_apply":
        if not str(row.get("diff") or "").strip():
            raise HTTPException(status_code=409, detail="补丁缺少 diff 正文，拒绝导出")
        export_path, apply_command = _export_patch(row)
    note = _decision_note(payload.action, payload.note, export_path, apply_command)
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
