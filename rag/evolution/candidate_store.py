"""SQLite persistence for question-set evolution candidates (phase 4).

``candidates`` is the phase-4 sibling of ``diagnosis_drafts``: it stores one
candidate, the five gate layers it has passed in ``gate_log``, and the audit
trail of every state change. The store follows the phase-3 pattern exactly:

* one lock per database, held for every read and write (``rag/user_db.py``
  discipline), so the worker, the API, and the gate runners can share a file;
* every state change is a single transaction that validates first and writes
  nothing when the transition is illegal;
* ``PRAGMA table_info`` adds missing columns in place, so a database written
  before a column existed opens without losing history.

Connection reuse: :class:`CandidateStore` takes the phase-3
:class:`~rag.evolution.store.FailureTraceStore` and shares its connection and
lock, because a candidate always lives next to the observations and clusters it
was generated from. Passing ``db_path`` (or nothing, for the configured
database) opens an independent connection instead; only a connection this store
owns is closed by :meth:`close`.

Audit trail: candidate lifecycle actions (generate / advance / reject / canary /
rollback / accept) go into ``evolution_candidate_audit`` rather than the
phase-3 ``evolution_audit`` table, whose ``action`` CHECK constraint is limited
to the three administrator decisions (approve_for_candidate / reject /
needs_more_evidence) and would refuse them. The two tables stay side by side in
``evolution.db`` with the same actor/action/note shape, so an administrator
still reads one file for the whole history.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from config import settings
from rag.evolution.candidates import ARTIFACT_TYPE, TARGETS, JevQuestionSetCandidate
from rag.evolution.models import EVOLUTION_TRANSITIONS, EvolutionStatus, ObservationPool

# The phase-3 ``_sql_in``/``_enum_values`` helpers are the single definition of
# the CHECK-constraint style used across this database; reusing them keeps the
# two tables' enum lists from drifting apart.
from rag.evolution.store import FailureTraceStore, _enum_values, _sql_in

# Design section 8 actions for the candidate lifecycle; anything else is
# rejected before the INSERT, and the CHECK constraint is the second gate.
_AUDIT_ACTIONS = ("generate", "advance", "reject", "canary", "rollback", "accept")
_AUDIT_ACTION_IN = _sql_in("action", _AUDIT_ACTIONS)

# Which audit action a status move records. Stable and rolled back are terminal
# outcomes ("accept"/"rollback"), canary is the rollout action; a blocked or
# needs-more-evidence move stays "advance" because the whitelist has no better
# home for it, and its note carries the reason.
_AUDIT_ACTION_BY_STATUS = {
    EvolutionStatus.CANARY.value: "canary",
    EvolutionStatus.ROLLED_BACK.value: "rollback",
    EvolutionStatus.REJECTED.value: "reject",
    EvolutionStatus.STABLE.value: "accept",
}
_DEFAULT_AUDIT_ACTION = "advance"

# What a gate entry records when a move carries a gate result. "needs_more_
# evidence" never concludes a gate, so it stays inconclusive rather than failed.
_GATE_RESULT_BY_STATUS = {
    EvolutionStatus.REJECTED.value: "failed",
    EvolutionStatus.BLOCKED.value: "blocked",
    EvolutionStatus.NEEDS_MORE_EVIDENCE.value: "inconclusive",
}
_DEFAULT_GATE_RESULT = "passed"

_STATUS_IN = _sql_in("status", _enum_values(EvolutionStatus))
_POOL_IN = _sql_in("pool", _enum_values(ObservationPool))
_ARTIFACT_IN = _sql_in("artifact_type", (ARTIFACT_TYPE,))
_TARGET_IN = _sql_in("target", TARGETS)
_CONFIDENCE_IN = _sql_in("confidence", ("low", "medium", "high"))

_CANDIDATES_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER,
    cluster_id INTEGER,
    artifact_type TEXT NOT NULL {_ARTIFACT_IN},
    target TEXT NOT NULL {_TARGET_IN},
    payload JSON NOT NULL,
    status TEXT NOT NULL {_STATUS_IN},
    pool TEXT NOT NULL {_POOL_IN},
    gate_log JSON NOT NULL DEFAULT '[]',
    prompt_version TEXT,
    model TEXT,
    rollback_ref JSON NOT NULL,
    rationale TEXT NOT NULL,
    expected_effect TEXT NOT NULL,
    confidence TEXT NOT NULL {_CONFIDENCE_IN},
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""

_CANDIDATE_AUDIT_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS evolution_candidate_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL {_AUDIT_ACTION_IN},
    candidate_id INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
)
"""

# Phase-4 migration: a ``candidates`` table written before a column existed is
# altered in place instead of dropped, exactly like the phase-3
# ``diagnosis_drafts.observation_id`` migration. Every default is a constant
# because SQLite refuses a non-constant default on ADD COLUMN.
_CANDIDATE_COLUMN_MIGRATIONS: dict[str, str] = {
    "observation_id": "INTEGER",
    "cluster_id": "INTEGER",
    "artifact_type": "TEXT NOT NULL DEFAULT 'jev_question_set'",
    "target": "TEXT NOT NULL DEFAULT 'preflight'",
    "payload": "JSON NOT NULL DEFAULT '{}'",
    "status": "TEXT NOT NULL DEFAULT 'candidate_testing'",
    "pool": "TEXT NOT NULL DEFAULT 'candidate_pool'",
    "gate_log": "JSON NOT NULL DEFAULT '[]'",
    "prompt_version": "TEXT",
    "model": "TEXT",
    "rollback_ref": "JSON NOT NULL DEFAULT '{}'",
    "rationale": "TEXT NOT NULL DEFAULT ''",
    "expected_effect": "TEXT NOT NULL DEFAULT ''",
    "confidence": "TEXT NOT NULL DEFAULT 'low'",
    "created_at": "REAL NOT NULL DEFAULT 0",
    "updated_at": "REAL NOT NULL DEFAULT 0",
}

_CANDIDATE_JSON_COLUMNS = ("payload", "gate_log", "rollback_ref")


def _decode_json(value: object) -> object:
    """Decode a stored JSON column; a dict/list stays as it is."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _decode_candidate_row(row: sqlite3.Row) -> dict:
    """One candidate row with its JSON columns decoded, like ``add_candidate`` returns it."""
    decoded = dict(row)
    for column in _CANDIDATE_JSON_COLUMNS:
        decoded[column] = _decode_json(decoded.get(column))
    return decoded


class CandidateStore:
    """Persistence for question-set candidates; content-light by construction."""

    def __init__(self, store: FailureTraceStore | None = None, *, db_path=None):
        if store is not None:
            # Reuse the phase-3 connection, its lock and its row factory: the
            # recorder, worker and API already serialize through this lock, and
            # a second connection to the same file would only need another one.
            self._conn = store._conn
            self._lock = store._lock
            self._owns_connection = False
        else:
            self._conn = sqlite3.connect(
                str(db_path or settings.evolution_db_path),
                check_same_thread=False,
                timeout=30.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._lock = threading.Lock()
            self._owns_connection = True
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create ``candidates``/``evolution_candidate_audit`` and their indexes."""
        with self._lock:
            self._conn.execute(_CANDIDATES_TABLE_SQL)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(candidates)")}
            for column, definition in _CANDIDATE_COLUMN_MIGRATIONS.items():
                if column not in columns:
                    self._conn.execute(
                        f"ALTER TABLE candidates ADD COLUMN {column} {definition}"
                    )
            self._conn.execute(_CANDIDATE_AUDIT_TABLE_SQL)
            # The worker's "gated yet?" scan and the admin's per-cluster list
            # both filter candidates; the audit list joins on candidate_id.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_cluster ON candidates(cluster_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candidates_observation ON candidates(observation_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evolution_candidate_audit_candidate "
                "ON evolution_candidate_audit(candidate_id)"
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Writing candidates.
    # ------------------------------------------------------------------

    def _require_cluster(self, cluster_id: int) -> None:
        row = self._conn.execute(
            "SELECT id FROM failure_clusters WHERE id = ?", (int(cluster_id),)
        ).fetchone()
        if row is None:
            raise KeyError("cluster not found")

    def _require_observation(self, observation_id: int) -> None:
        row = self._conn.execute(
            "SELECT id FROM failure_observations WHERE id = ?", (int(observation_id),)
        ).fetchone()
        if row is None:
            raise KeyError("observation not found")

    def _insert_audit(self, actor_id: str, action: str, candidate_id: int, note: str) -> int:
        """Append one audit row without committing; the caller owns the transaction.

        The whitelist check runs before the INSERT, so an unknown action raises
        ``ValueError`` and writes nothing even inside a larger transaction.
        """
        if action not in _AUDIT_ACTIONS:
            raise ValueError(f"invalid audit action: {action}")
        cursor = self._conn.execute(
            "INSERT INTO evolution_candidate_audit (actor_id, action, candidate_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(actor_id), str(action), int(candidate_id), str(note), time.time()),
        )
        return int(cursor.lastrowid)

    def add_candidate(
        self,
        observation_id: int | None,
        cluster_id: int,
        candidate: JevQuestionSetCandidate,
        *,
        prompt_version: str = "",
        model: str = "",
    ) -> int:
        """Persist one candidate and return its id.

        A candidate starts in ``candidate_testing`` inside the candidate pool and
        carries the layer-1 (scope) gate as its first ``gate_log`` entry, plus a
        ``generate`` audit row. Both writes share one transaction with the
        candidate row, so a candidate can never exist without its gate and audit
        history. Payload validation itself lives in
        :func:`~rag.evolution.candidates.validate_payload`: the generator runs it
        before calling this method, and every gate re-runs it.
        """
        now = time.time()
        gate_log = [
            {
                "gate": "scope",
                "result": "accepted",
                "detail": (
                    f"{candidate.artifact_type}/{candidate.target} "
                    f"questions={len(candidate.payload)}"
                ),
                "created_at": now,
            }
        ]
        note = (
            f"generated {candidate.target} candidate from question_set_version="
            f"{candidate.rollback_ref.get('question_set_version', '')}"
        )
        with self._lock:
            self._require_cluster(int(cluster_id))
            if observation_id is not None:
                self._require_observation(int(observation_id))
            cursor = self._conn.execute(
                "INSERT INTO candidates ("
                "observation_id, cluster_id, artifact_type, target, payload, status, pool, "
                "gate_log, prompt_version, model, rollback_ref, rationale, expected_effect, "
                "confidence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    None if observation_id is None else int(observation_id),
                    int(cluster_id),
                    candidate.artifact_type,
                    candidate.target,
                    json.dumps(candidate.payload, ensure_ascii=False),
                    EvolutionStatus.CANDIDATE_TESTING.value,
                    ObservationPool.CANDIDATE_POOL.value,
                    json.dumps(gate_log, ensure_ascii=False),
                    str(prompt_version),
                    str(model),
                    json.dumps(candidate.rollback_ref, ensure_ascii=False),
                    candidate.rationale,
                    candidate.expected_effect,
                    candidate.confidence,
                    now,
                    now,
                ),
            )
            candidate_id = int(cursor.lastrowid)
            # The audit row needs the new candidate id, so it follows the
            # INSERT inside the same transaction and precedes any advance.
            self._insert_audit(str(model) or "generator", "generate", candidate_id, note)
            self._conn.commit()
        return candidate_id

    def advance_candidate(
        self,
        id: int,
        new_status: str,
        *,
        actor_id: str,
        note: str,
        gate: str | None = None,
        gate_detail: str | None = None,
    ) -> dict:
        """Move one candidate through ``EVOLUTION_TRANSITIONS`` and return the new row.

        The order inside the single transaction is fixed: read the row, validate
        the transition (illegal ⇒ ``ValueError`` and nothing is written), write
        the audit row, append the optional gate entry, then update the status.
        So an audit row can never exist for a move that did not happen, and a
        gate entry can never be missing from a move that did.

        ``gate``/``gate_detail`` record which of the five layers this move
        represents; ``result`` is derived from the target status (``failed`` for
        a rejection, ``blocked``/``inconclusive`` otherwise, ``passed`` for every
        advance). Without a ``gate`` name no gate entry is appended, so a plain
        administrative move leaves ``gate_log`` untouched.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (int(id),)
            ).fetchone()
            if row is None:
                raise KeyError("candidate not found")
            current = row["status"]
            if new_status not in EVOLUTION_TRANSITIONS.get(current, ()):
                raise ValueError(f"invalid transition: {current} -> {new_status}")
            candidate_id = int(row["id"])
            self._insert_audit(
                str(actor_id),
                _AUDIT_ACTION_BY_STATUS.get(new_status, _DEFAULT_AUDIT_ACTION),
                candidate_id,
                str(note),
            )
            gate_log = _decode_json(row["gate_log"])
            if not isinstance(gate_log, list):
                gate_log = []
            if gate is not None:
                gate_log.append(
                    {
                        "gate": str(gate),
                        "result": _GATE_RESULT_BY_STATUS.get(new_status, _DEFAULT_GATE_RESULT),
                        "detail": "" if gate_detail is None else str(gate_detail),
                        "created_at": now,
                    }
                )
            self._conn.execute(
                "UPDATE candidates SET status = ?, gate_log = ?, updated_at = ? WHERE id = ?",
                (
                    str(new_status),
                    json.dumps(gate_log, ensure_ascii=False),
                    now,
                    candidate_id,
                ),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return _decode_candidate_row(updated)

    # ------------------------------------------------------------------
    # Reading candidates.
    # ------------------------------------------------------------------

    def get_candidate(self, id: int) -> dict | None:
        """One candidate with its JSON columns decoded; ``None`` for an unknown id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (int(id),)
            ).fetchone()
        return None if row is None else _decode_candidate_row(row)

    def list_candidates(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        """Candidates newest first, optionally filtered to one status."""
        sql = "SELECT * FROM candidates"
        params: tuple = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (str(status),)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*params, int(limit))).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    def candidates_for_observation(self, observation_id: int) -> list[dict]:
        """Candidates generated for one member observation, oldest first.

        A candidate may also be cluster-level (``observation_id`` NULL); such a
        row never appears under a specific observation.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE observation_id = ? ORDER BY id ASC",
                (int(observation_id),),
            ).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    def candidates_without_gate_result(self, gate_name: str, limit: int) -> list[dict]:
        """Candidates still in ``candidate_testing`` that ``gate_name`` never ran on.

        Drives a gate round: the runner takes this list, evaluates the gate, and
        reports the outcome back through :meth:`advance_candidate`, so a gate
        never runs twice on the same candidate. A candidate already advanced
        past testing is excluded, because its remaining layers happened later.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE status = ? "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM json_each(candidates.gate_log) AS entry "
                "    WHERE json_extract(entry.value, '$.gate') = ?"
                ") "
                "ORDER BY id ASC LIMIT ?",
                (EvolutionStatus.CANDIDATE_TESTING.value, str(gate_name), int(limit)),
            ).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Audit trail.
    # ------------------------------------------------------------------

    def add_audit(self, actor_id: str, action: str, candidate_id: int, note: str) -> int:
        """Append one candidate lifecycle row to the audit trail; returns the row id.

        Only the six lifecycle actions are legal; an unknown action raises
        ``ValueError`` and writes nothing, and the CHECK constraint is the second
        gate for any other writer.
        """
        with self._lock:
            audit_id = self._insert_audit(actor_id, action, candidate_id, note)
            self._conn.commit()
        return audit_id

    def list_audit(self, candidate_id: int | None = None) -> list[dict]:
        """Audit rows in chronological order, optionally scoped to one candidate."""
        sql = "SELECT * FROM evolution_candidate_audit"
        params: tuple = ()
        if candidate_id is not None:
            sql += " WHERE candidate_id = ?"
            params = (int(candidate_id),)
        sql += " ORDER BY created_at ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        """Close a connection this store owns; a shared phase-3 one stays open.

        The recorder, worker and API keep using their own
        :class:`~rag.evolution.store.FailureTraceStore` after a candidate store
        is closed, which is what the API's shutdown path expects.
        """
        if self._owns_connection:
            self._conn.close()


__all__ = ["CandidateStore"]
