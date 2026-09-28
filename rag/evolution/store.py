"""Independent SQLite store for evolution observations, clusters, and skips.

The evolution database is separate from ``memory.db``/``users.db`` and holds only
hashes, scores, versions, and enums; it is shared by the API process and the
background worker through one lock, in the same pattern as ``rag/user_db.py``.
Deleting the file is the documented rollback, so no migration path is required.

The diagnosis phase (phase 3) adds two more content-light tables: verbatim
diagnosis drafts (JSON payload + prompt version + model) and the admin decision
audit trail. Both live only in ``evolution.db`` and never carry credentials.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from config import settings
from rag.evolution.models import (
    EVOLUTION_TRANSITIONS,
    DiagnosisDraft,
    EvolutionStatus,
    FailureObservation,
    FailureStage,
    NonEvolvableReason,
    ObservationPool,
)

_JSON_COLUMNS = (
    "details_chunk_hashes",
    "details_adopted_evidence_hashes",
    "details_tool_names",
    "details_decision_metadata",
)


def _enum_values(enum_cls) -> tuple[str, ...]:
    return tuple(member.value for member in enum_cls)


def _sql_in(column: str, values) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"CHECK({column} IN ({quoted}))"


_STATUS_IN = _sql_in("status", _enum_values(EvolutionStatus))
_POOL_IN = _sql_in("pool", _enum_values(ObservationPool))
_STAGE_IN = _sql_in("stage", _enum_values(FailureStage))
_CLUSTER_STAGE_IN = _sql_in("failure_stage", _enum_values(FailureStage))
_PRIORITY_IN = _sql_in("priority", ("normal", "high"))
_REASON_IN = _sql_in("reason", _enum_values(NonEvolvableReason))

# Design section 8: the three admin decisions that may ever be audited. They map
# one-to-one onto the ``awaiting_diagnosis_review`` transitions in
# ``EVOLUTION_TRANSITIONS``; anything else is rejected before the INSERT.
_AUDIT_ACTIONS = ("approve_for_candidate", "reject", "needs_more_evidence")
_AUDIT_ACTION_IN = _sql_in("action", _AUDIT_ACTIONS)

_OBSERVATIONS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS failure_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id TEXT NOT NULL,
    stage TEXT NOT NULL {_STAGE_IN},
    root_cause_feature TEXT,
    status TEXT NOT NULL {_STATUS_IN},
    pool TEXT NOT NULL {_POOL_IN},
    priority TEXT NOT NULL {_PRIORITY_IN},
    question TEXT NOT NULL,
    route TEXT NOT NULL,
    cluster_id INTEGER,
    details_answerability_kind TEXT,
    details_confidence_level TEXT,
    details_chunk_hashes TEXT NOT NULL DEFAULT '[]',
    details_best_score REAL,
    details_adopted_evidence_hashes TEXT NOT NULL DEFAULT '[]',
    details_tool_names TEXT NOT NULL DEFAULT '[]',
    details_structured_query_kind TEXT,
    details_exception_class TEXT,
    details_feedback_kind TEXT,
    details_jev_question_set_version TEXT,
    details_decision_metadata TEXT NOT NULL DEFAULT '{{}}',
    total_ms REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CLUSTERS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS failure_clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key TEXT NOT NULL UNIQUE,
    kb_id TEXT NOT NULL,
    failure_stage TEXT NOT NULL {_CLUSTER_STAGE_IN},
    root_cause_feature TEXT,
    pool TEXT NOT NULL {_POOL_IN},
    status TEXT NOT NULL {_STATUS_IN},
    priority TEXT NOT NULL {_PRIORITY_IN},
    member_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_SKIP_LOG_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS evolution_skip_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL {_REASON_IN},
    kb_id TEXT NOT NULL DEFAULT '',
    question TEXT NOT NULL DEFAULT '',
    route TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)
"""

# The draft payload is the frozen Pydantic draft serialized as JSON: the admin
# reads the model output verbatim, and a prompt-version bump keeps old drafts
# queryable. ``reproducible`` is duplicated out of the payload so the worker can
# filter on it without parsing JSON. ``observation_id`` scopes a draft to one
# member observation; NULL is the legacy cluster-level draft the mixed-key
# clustering produced before phase 4 (``docs/evolution-diagnosis.md`` §5). The
# cluster_id stays NOT NULL on member drafts because a decision still covers the
# whole cluster, so both draft levels remain joinable to their cluster.
_DIAGNOSIS_DRAFTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS diagnosis_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    observation_id INTEGER,
    payload JSON NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT,
    reproducible INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
)
"""

_EVOLUTION_AUDIT_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS evolution_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL {_AUDIT_ACTION_IN},
    cluster_id INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)
"""


def _decode_row(row: sqlite3.Row) -> dict:
    decoded = dict(row)
    for column in _JSON_COLUMNS:
        value = decoded.get(column)
        if isinstance(value, str):
            decoded[column] = json.loads(value)
    return decoded


def _decode_draft_row(row: sqlite3.Row) -> dict:
    decoded = dict(row)
    decoded["payload"] = json.loads(decoded["payload"])
    decoded["reproducible"] = bool(decoded["reproducible"])
    return decoded


class FailureTraceStore:
    """Persistence for the observation phase; content-free by construction."""

    def __init__(self, db_path=None):
        # Recorder, worker, and API each hold their own connection to the same file; a busy
        # timeout keeps concurrent rounds from failing with SQLITE_BUSY.
        self._conn = sqlite3.connect(
            str(db_path or settings.evolution_db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._lock:
            self._conn.execute(_OBSERVATIONS_TABLE_SQL)
            self._conn.execute(_CLUSTERS_TABLE_SQL)
            self._conn.execute(_SKIP_LOG_TABLE_SQL)
            self._conn.execute(_DIAGNOSIS_DRAFTS_TABLE_SQL)
            self._conn.execute(_EVOLUTION_AUDIT_TABLE_SQL)
            # Phase-4 migration: a database written before drafts became
            # member-level has a ``diagnosis_drafts`` table without the nullable
            # ``observation_id`` column. Add it in place so the stored cluster-level
            # drafts survive untouched (their observation_id reads back as NULL);
            # dropping and recreating would delete admin-reviewed history.
            draft_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(diagnosis_drafts)")
            }
            if "observation_id" not in draft_columns:
                self._conn.execute(
                    "ALTER TABLE diagnosis_drafts ADD COLUMN observation_id INTEGER"
                )
            # Phase-5 migration: the administrator's confirmed root-cause layer
            # (design section 6/8 — the human confirms the layer a fix belongs
            # to, which is what lets a code-patch candidate be generated from a
            # diagnosis whose draft suggested something else). Nullable: an
            # unconfirmed cluster keeps reading back NULL.
            cluster_columns = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(failure_clusters)")
            }
            if "confirmed_layer" not in cluster_columns:
                self._conn.execute(
                    "ALTER TABLE failure_clusters ADD COLUMN confirmed_layer TEXT"
                )
            # Membership lookups and the in-window member count both filter on cluster_id.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_failure_observations_cluster "
                "ON failure_observations(cluster_id)"
            )
            # The worker's "awaiting and still undrafted" scan and the read-only
            # diagnosis endpoint both filter drafts and audit rows on cluster_id.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diagnosis_drafts_cluster "
                "ON diagnosis_drafts(cluster_id)"
            )
            # The member-level "awaiting diagnosis without a draft" scan tests each
            # observation against the member drafts that point at it.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diagnosis_drafts_observation "
                "ON diagnosis_drafts(observation_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_audit_cluster "
                "ON evolution_audit(cluster_id)"
            )
            self._conn.commit()

    def add_observation(self, observation: FailureObservation) -> dict:
        """Persist one observation and return the stored row (``id`` included)."""
        values = (
            observation.kb_id,
            str(observation.failure_stage),
            observation.root_cause_feature,
            str(observation.status),
            str(observation.pool),
            str(observation.priority),
            observation.question,
            observation.route,
            observation.answerability_kind,
            observation.confidence_level,
            json.dumps(list(observation.chunk_hashes), ensure_ascii=False),
            observation.best_score,
            json.dumps(list(observation.adopted_evidence_hashes), ensure_ascii=False),
            json.dumps(list(observation.tool_names), ensure_ascii=False),
            observation.structured_query_kind,
            observation.exception_class,
            observation.feedback_kind,
            observation.jev_question_set_version,
            json.dumps(observation.decision_metadata, ensure_ascii=False),
            float(observation.total_ms),
            float(observation.created_at),
            float(observation.updated_at),
        )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO failure_observations ("
                "kb_id, stage, root_cause_feature, status, pool, priority, question, route, "
                "details_answerability_kind, details_confidence_level, details_chunk_hashes, "
                "details_best_score, details_adopted_evidence_hashes, details_tool_names, "
                "details_structured_query_kind, details_exception_class, details_feedback_kind, "
                "details_jev_question_set_version, details_decision_metadata, total_ms, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM failure_observations WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _decode_row(row)

    def log_skip(
        self,
        reason: NonEvolvableReason | str,
        *,
        question: str = "",
        route: str = "",
        kb_id: str = "",
    ) -> int:
        """Count one request that never becomes an observation; returns the row id."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO evolution_skip_log (reason, kb_id, question, route, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(reason), kb_id, question, route, time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_observation(self, observation_id: int) -> dict | None:
        """Return one stored observation with its JSON columns decoded."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM failure_observations WHERE id = ?", (observation_id,)
            ).fetchone()
        return None if row is None else _decode_row(row)

    def count_by_status(self) -> dict[str, int]:
        """Observation counts per status; empty when nothing has been recorded yet."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM failure_observations GROUP BY status"
            ).fetchall()
        return {row["status"]: int(row["cnt"]) for row in rows}

    def count_skips_by_reason(self) -> dict[str, int]:
        """Skip counts per reason; empty when nothing has been skipped."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT reason, COUNT(*) AS cnt FROM evolution_skip_log GROUP BY reason"
            ).fetchall()
        return {row["reason"]: int(row["cnt"]) for row in rows}

    # ------------------------------------------------------------------
    # Clustering and admission (Task 2); read-only listing (Tasks 4 and 5).
    # All state changes below are single-statement and run under the store
    # lock, so repeated clustering can never double-count a member.
    # ------------------------------------------------------------------

    def list_pending_observations(self, *, cutoff: float, limit: int) -> list[dict]:
        """Pending (observed, unattached) observations inside the window, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM failure_observations "
                "WHERE status = ? AND cluster_id IS NULL AND created_at >= ? "
                "ORDER BY id ASC LIMIT ?",
                (EvolutionStatus.OBSERVED.value, float(cutoff), int(limit)),
            ).fetchall()
        return [_decode_row(row) for row in rows]

    def count_stale_pending_observations(self, cutoff: float) -> int:
        """Pending observations outside the window; they stay in the observation queue."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM failure_observations "
                "WHERE status = ? AND cluster_id IS NULL AND created_at < ?",
                (EvolutionStatus.OBSERVED.value, float(cutoff)),
            ).fetchone()
        return int(row["cnt"])

    def upsert_cluster(
        self,
        *,
        cluster_key: str,
        kb_id: str,
        failure_stage: str,
        root_cause_feature: str,
        seen_at: float,
    ) -> dict:
        """Insert the cluster for this key or return the existing row (idempotent)."""
        now = float(seen_at)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO failure_clusters ("
                "cluster_key, kb_id, failure_stage, root_cause_feature, pool, status, "
                "priority, member_count, first_seen_at, last_seen_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'normal', 0, ?, ?, ?, ?)",
                (
                    cluster_key,
                    kb_id,
                    failure_stage,
                    root_cause_feature,
                    ObservationPool.OBSERVATION_QUEUE.value,
                    EvolutionStatus.CLUSTERED.value,
                    now,
                    now,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE cluster_key = ?", (cluster_key,)
            ).fetchone()
        return dict(row)

    def attach_cluster_member(
        self,
        observation_id: int,
        cluster_id: int,
        *,
        failure_stage: str,
        root_cause_feature: str,
        now: float,
    ) -> None:
        """Attach one observation to a cluster and backfill the authoritative signals.

        The observation stays ``observed`` until its cluster is admitted, so a
        single or unattached failure keeps waiting in the observation queue.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE failure_observations SET cluster_id = ?, stage = ?, "
                "root_cause_feature = ?, updated_at = ? WHERE id = ?",
                (int(cluster_id), failure_stage, root_cause_feature, float(now), int(observation_id)),
            )
            self._conn.commit()

    def refresh_cluster(self, cluster_id: int, *, cutoff: float, priority: str, now: float) -> dict:
        """Atomically recount in-window members and refresh priority/last activity."""
        with self._lock:
            self._conn.execute(
                "UPDATE failure_clusters SET "
                "member_count = (SELECT COUNT(*) FROM failure_observations o "
                "                 WHERE o.cluster_id = failure_clusters.id AND o.created_at >= ?), "
                "priority = ?, last_seen_at = ?, updated_at = ? WHERE id = ?",
                (float(cutoff), priority, float(now), float(now), int(cluster_id)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
        return dict(row)

    def promote_cluster(
        self,
        cluster_id: int,
        *,
        new_status: str,
        pool: str,
        member_status: str,
        now: float,
    ) -> dict | None:
        """Admit one cluster and its members atomically; ``None`` when already admitted.

        Both the cluster and its member observations move through the design
        section 7 transition table; an illegal transition raises ``ValueError``
        and nothing is written. Re-promoting an admitted cluster is a no-op.
        """
        with self._lock:
            cluster = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
            if cluster is None:
                raise KeyError("cluster not found")
            if cluster["pool"] != ObservationPool.OBSERVATION_QUEUE.value:
                return None
            current = cluster["status"]
            if new_status not in EVOLUTION_TRANSITIONS.get(current, ()):
                raise ValueError(f"invalid transition: {current} -> {new_status}")
            members = self._conn.execute(
                "SELECT id, status FROM failure_observations WHERE cluster_id = ?", (int(cluster_id),)
            ).fetchall()
            for member in members:
                status = member["status"]
                if status == member_status:
                    continue
                if member_status not in EVOLUTION_TRANSITIONS.get(status, ()):
                    raise ValueError(f"invalid transition: {status} -> {member_status}")
            self._conn.execute(
                "UPDATE failure_clusters SET status = ?, pool = ?, updated_at = ? WHERE id = ?",
                (new_status, pool, float(now), int(cluster_id)),
            )
            self._conn.execute(
                "UPDATE failure_observations SET status = ?, updated_at = ? "
                "WHERE cluster_id = ? AND status != ?",
                (member_status, float(now), int(cluster_id), member_status),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
        return dict(row)

    def list_clusters(self) -> list[dict]:
        """All clusters, oldest first; callers filter or page in Python (Tasks 4 and 5)."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM failure_clusters ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]

    def decide_cluster(
        self,
        cluster_id: int,
        *,
        new_status: str,
        pool: str,
        member_status: str,
        now: float,
        confirmed_layer: str | None = None,
    ) -> dict:
        """Apply an administrator decision to one cluster and its members atomically.

        ``promote_cluster`` admits clusters from the observation queue only, but every
        ``awaiting_diagnosis_review`` cluster already lives in the candidate pool, so an
        explicit decision needs this separate entry point. Both the cluster and its
        members still move through ``EVOLUTION_TRANSITIONS``; an illegal transition
        raises ``ValueError`` and nothing is written. ``confirmed_layer`` records the
        administrator's confirmed root-cause layer (design section 6/8) inside the same
        transaction, which is what allows a code-patch candidate afterwards.
        """
        with self._lock:
            cluster = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
            if cluster is None:
                raise KeyError("cluster not found")
            current = cluster["status"]
            if new_status not in EVOLUTION_TRANSITIONS.get(current, ()):
                raise ValueError(f"invalid transition: {current} -> {new_status}")
            members = self._conn.execute(
                "SELECT id, status FROM failure_observations WHERE cluster_id = ?", (int(cluster_id),)
            ).fetchall()
            for member in members:
                status = member["status"]
                if status == member_status:
                    continue
                if member_status not in EVOLUTION_TRANSITIONS.get(status, ()):
                    raise ValueError(f"invalid transition: {status} -> {member_status}")
            self._conn.execute(
                "UPDATE failure_clusters SET status = ?, pool = ?, updated_at = ? WHERE id = ?",
                (new_status, pool, float(now), int(cluster_id)),
            )
            if confirmed_layer is not None:
                self._conn.execute(
                    "UPDATE failure_clusters SET confirmed_layer = ?, updated_at = ? WHERE id = ?",
                    (str(confirmed_layer), float(now), int(cluster_id)),
                )
            self._conn.execute(
                "UPDATE failure_observations SET status = ?, updated_at = ? "
                "WHERE cluster_id = ? AND status != ?",
                (member_status, float(now), int(cluster_id), member_status),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
        return dict(row)

    def set_confirmed_layer(
        self, cluster_id: int, *, confirmed_layer: str, now: float
    ) -> dict:
        """Record the administrator's confirmed root-cause layer without moving status.

        Design sections 6/8 treat the root-cause confirmation and the state
        transition as separate judgments: an administrator may confirm the layer
        after approving the cluster, and the candidate generator reads it. The
        write is one UPDATE; the caller owns the audit row.
        """
        with self._lock:
            cluster = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
            if cluster is None:
                raise KeyError("cluster not found")
            self._conn.execute(
                "UPDATE failure_clusters SET confirmed_layer = ?, updated_at = ? WHERE id = ?",
                (str(confirmed_layer), float(now), int(cluster_id)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
        return dict(row)

    def list_observations(self, cluster_id: int | None = None) -> list[dict]:
        """All observations, or the members of one cluster, oldest first.

        JSON columns are decoded exactly like ``add_observation`` returns them,
        so the read-only API and the metric layer see identical rows.
        """
        sql = "SELECT * FROM failure_observations"
        params: tuple = ()
        if cluster_id is not None:
            sql += " WHERE cluster_id = ?"
            params = (int(cluster_id),)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode_row(row) for row in rows]

    def purge_observations_before(self, timestamp: float) -> int:
        """Delete observations created before ``timestamp``; returns the deleted count.

        Task 3's retention step calls this with the configured cutoff. Clusters
        are left untouched on purpose: the next clustering round recomputes each
        touched cluster's ``member_count`` from its surviving members, so purging
        can never silently promote or demote a cluster here.
        """
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM failure_observations WHERE created_at < ?", (float(timestamp),)
            )
            self._conn.commit()
            return int(cursor.rowcount)

    # ------------------------------------------------------------------
    # Diagnosis drafts and the admin audit trail (Task 1). Drafts are
    # append-only history: a cluster may be re-drafted under a new prompt
    # version, and the state machine still only moves through
    # ``promote_cluster`` after an audited admin decision.
    # ------------------------------------------------------------------

    def add_draft(
        self,
        cluster_id: int,
        draft: DiagnosisDraft,
        *,
        observation_id: int | None = None,
        prompt_version: str = "",
        model: str = "",
    ) -> int:
        """Persist one diagnosis draft; returns the draft id.

        The payload is the frozen draft serialized verbatim, so the admin sees
        exactly what the diagnoser produced. ``observation_id`` scopes the draft
        to one member observation (phase 4); ``None`` writes the legacy
        cluster-level shape and stays backward compatible with the phase-3
        callers. A draft never moves the state machine on its own: it is evidence
        for a human decision, audited by ``add_audit``.
        """
        with self._lock:
            cluster = self._conn.execute(
                "SELECT id FROM failure_clusters WHERE id = ?", (int(cluster_id),)
            ).fetchone()
            if cluster is None:
                raise KeyError("cluster not found")
            if observation_id is not None:
                observation = self._conn.execute(
                    "SELECT id FROM failure_observations WHERE id = ?", (int(observation_id),)
                ).fetchone()
                if observation is None:
                    raise KeyError("observation not found")
            cursor = self._conn.execute(
                "INSERT INTO diagnosis_drafts "
                "(cluster_id, observation_id, payload, prompt_version, model, reproducible, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(cluster_id),
                    None if observation_id is None else int(observation_id),
                    json.dumps(draft.model_dump(mode="json"), ensure_ascii=False),
                    str(prompt_version),
                    str(model),
                    1 if draft.reproducible else 0,
                    time.time(),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_draft(self, draft_id: int) -> dict | None:
        """One draft with its JSON payload decoded; ``None`` when the id is unknown."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM diagnosis_drafts WHERE id = ?", (int(draft_id),)
            ).fetchone()
        return None if row is None else _decode_draft_row(row)

    def list_drafts(self, cluster_id: int | None = None) -> list[dict]:
        """Drafts newest first, optionally filtered to one cluster.

        The read-only endpoint reads ``list_drafts(cluster_id)[0]`` as the draft
        under review; older drafts remain queryable as prompt-version history.
        """
        sql = "SELECT * FROM diagnosis_drafts"
        params: tuple = ()
        if cluster_id is not None:
            sql += " WHERE cluster_id = ?"
            params = (int(cluster_id),)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_decode_draft_row(row) for row in rows]

    def clusters_awaiting_diagnosis_without_draft(self, limit: int) -> list[dict]:
        """Clusters awaiting diagnosis review with no draft yet, oldest first.

        Feeds the worker's diagnosis round: a cluster that already has a draft
        is never re-drafted here, so repeated rounds stay inside the model-call
        budget.

        Phase 4 keeps this cluster-level view for backward compatibility, but the
        worker now drafts per member observation through
        :meth:`observations_awaiting_diagnosis_without_draft`, because a
        cluster-level draft cannot match the mixed root causes a cluster key
        gathers (``docs/evolution-diagnosis.md`` §5).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM failure_clusters "
                "WHERE status = ? "
                "AND id NOT IN (SELECT cluster_id FROM diagnosis_drafts) "
                "ORDER BY id ASC LIMIT ?",
                (EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def observations_awaiting_diagnosis_without_draft(self, limit: int) -> list[dict]:
        """Member observations that still owe a diagnosis, oldest first.

        Returns one ``{"observation": row, "cluster": row}`` pairing per member
        observation whose cluster is ``awaiting_diagnosis_review`` and which has
        no member-level draft (``diagnosis_drafts.observation_id`` pointing at
        it). A legacy cluster-level draft (``observation_id`` NULL) never blocks
        a member draft, so databases migrated from phase 3 still get their
        per-observation drafts. This drives the worker's phase-4 diagnosis step,
        replacing the cluster-level scan (design sections 5/6: a failure
        trajectory is the observation-level entity; the cluster is only the
        admission grouping).
        """
        with self._lock:
            observation_rows = self._conn.execute(
                "SELECT o.* FROM failure_observations o "
                "JOIN failure_clusters c ON c.id = o.cluster_id "
                "WHERE c.status = ? "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM diagnosis_drafts d WHERE d.observation_id = o.id"
                ") "
                "ORDER BY o.id ASC LIMIT ?",
                (EvolutionStatus.AWAITING_DIAGNOSIS_REVIEW.value, int(limit)),
            ).fetchall()
            if not observation_rows:
                return []
            cluster_ids = sorted({row["cluster_id"] for row in observation_rows})
            placeholders = ", ".join("?" for _ in cluster_ids)
            cluster_rows = self._conn.execute(
                f"SELECT * FROM failure_clusters WHERE id IN ({placeholders})",
                tuple(cluster_ids),
            ).fetchall()
        clusters_by_id = {row["id"]: dict(row) for row in cluster_rows}
        return [
            {"observation": _decode_row(row), "cluster": clusters_by_id[row["cluster_id"]]}
            for row in observation_rows
        ]

    def add_audit(self, actor_id: str, action: str, cluster_id: int, note: str) -> int:
        """Append one admin decision to the audit trail; returns the row id.

        Only the three design section 8 actions are legal. An unknown action
        raises ``ValueError`` and writes nothing; the CHECK constraint is the
        second gate for any other writer.
        """
        if action not in _AUDIT_ACTIONS:
            raise ValueError(f"invalid audit action: {action}")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO evolution_audit (actor_id, action, cluster_id, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(actor_id), str(action), int(cluster_id), str(note), time.time()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_audit(self, cluster_id: int | None = None) -> list[dict]:
        """Audit rows in chronological order, optionally filtered to one cluster."""
        sql = "SELECT * FROM evolution_audit"
        params: tuple = ()
        if cluster_id is not None:
            sql += " WHERE cluster_id = ?"
            params = (int(cluster_id),)
        sql += " ORDER BY created_at ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
