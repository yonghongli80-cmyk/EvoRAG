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
from rag.evolution.candidates import JevQuestionSetCandidate
from rag.evolution.code_patches import ARTIFACT_TYPE as CODE_PATCH_ARTIFACT_TYPE
from rag.evolution.code_patches import CodePatchCandidate
from rag.evolution.models import EVOLUTION_TRANSITIONS, EvolutionStatus, ObservationPool

# The phase-3 ``_sql_in``/``_enum_values`` helpers are the single definition of
# the CHECK-constraint style used across this database; reusing them keeps the
# two tables' enum lists from drifting apart.
from rag.evolution.store import FailureTraceStore, _enum_values, _sql_in

#: 一个候选"还占着这条观测"的状态：仍在测试、待验证、已批准灰度、灰度中。只有这些状态让
#: 观测留在未决集合里；``rejected`` / ``needs_more_evidence`` / ``rolled_back`` / ``stable``
#: 都是**已结案**的尝试，不能永久把观测挡在生成队列外（C-08，2026-10-22）。此前队列的
#: ``NOT EXISTS`` 不看状态，于是一次驳回＝这条观测再也不会被生成——驳回成了不可撤销的终局。
OPEN_CANDIDATE_STATUSES: tuple[str, ...] = (
    EvolutionStatus.CANDIDATE_TESTING.value,
    EvolutionStatus.AWAITING_VERIFICATION.value,
    EvolutionStatus.APPROVED_FOR_CANARY.value,
    EvolutionStatus.CANARY.value,
)
_OPEN_STATUS_PLACEHOLDERS = ", ".join("?" for _ in OPEN_CANDIDATE_STATUSES)

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

#: 未决轮次在 ``gate_log`` 里记的结果值。它**不算门禁结果**：候选仍在
#: ``candidate_testing``，下一轮扫描会再次给它机会（基础设施故障不该烧掉候选）。
_INCONCLUSIVE_GATE_RESULT = "inconclusive"

#: 一个"追加证据"的门禁条目可以写的结果值（见 :meth:`CandidateStore.record_gate_entry`）。
#: ``accepted`` **不在**其中：它是层 1 生成时的产物边界结论，后来跑的任何测量都不能替生成门禁
#: 说"通过" —— 证据可以量出失败，不能伪造准入。
GATE_EVIDENCE_RESULTS: frozenset[str] = frozenset({"passed", "failed", "inconclusive", "blocked"})

_STATUS_IN = _sql_in("status", _enum_values(EvolutionStatus))
_POOL_IN = _sql_in("pool", _enum_values(ObservationPool))
_CONFIDENCE_IN = _sql_in("confidence", ("low", "medium", "high"))

# Phase-5 schema: ``artifact_type`` widens to ``jev_question_set | code_patch``.
# The CHECK constraints on artifact_type/target are gone on purpose — the Pydantic
# ``Literal`` contracts already enforce them, and a CHECK would force a table
# rebuild for every future artifact. ``target``/``payload`` stay question-set
# columns and are NULL for code patches, which carry ``diff``/``layer``/
# ``test_paths`` instead. The migration below rebuilds a phase-4 table in place.
_CANDIDATES_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER,
    cluster_id INTEGER,
    artifact_type TEXT NOT NULL,
    target TEXT,
    payload JSON,
    status TEXT NOT NULL {_STATUS_IN},
    pool TEXT NOT NULL {_POOL_IN},
    gate_log JSON NOT NULL DEFAULT '[]',
    prompt_version TEXT,
    model TEXT,
    rollback_ref JSON NOT NULL,
    rationale TEXT NOT NULL,
    expected_effect TEXT NOT NULL,
    confidence TEXT NOT NULL {_CONFIDENCE_IN},
    diff TEXT,
    layer TEXT,
    test_paths JSON,
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
    "diff": "TEXT",
    "layer": "TEXT",
    "test_paths": "JSON",
}

# Phase-5 rebuild: the phase-4 table CHECK-constrained artifact_type to
# ``jev_question_set`` and forced NOT NULL on target/payload. Neither can be
# altered in place, so a table that still declares ``target`` NOT NULL (the
# phase-4 shape, after the column migrations above have added every missing
# column) is rebuilt in one transaction (SQLite DDL is transactional): create
# the phase-5 shape, copy 1:1, drop, rename, recreate the indexes. Question-set
# rows survive verbatim, defaults included.
_CANDIDATES_PHASE5_DDL = (
    "CREATE TABLE candidates_phase5 ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, observation_id INTEGER, cluster_id INTEGER, "
    "artifact_type TEXT NOT NULL, target TEXT, payload JSON, status TEXT NOT NULL, "
    "pool TEXT NOT NULL, gate_log JSON NOT NULL DEFAULT '[]', prompt_version TEXT, "
    "model TEXT, rollback_ref JSON NOT NULL, rationale TEXT NOT NULL, "
    "expected_effect TEXT NOT NULL, confidence TEXT NOT NULL, diff TEXT, layer TEXT, "
    "test_paths JSON, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
)
_CANDIDATES_COPY_COLUMNS = (
    "id", "observation_id", "cluster_id", "artifact_type", "target", "payload",
    "status", "pool", "gate_log", "prompt_version", "model", "rollback_ref",
    "rationale", "expected_effect", "confidence", "diff", "layer", "test_paths",
    "created_at", "updated_at",
)
_PATCH_COLUMNS = ("diff", "layer", "test_paths")

_CANDIDATE_JSON_COLUMNS = ("payload", "gate_log", "rollback_ref", "test_paths")


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
    """Persistence for evolution candidates; content-light by construction.

    Phase 4 stored Jev question-set candidates; phase 5 widened the table to
    code-patch candidates (``diff``/``layer``/``test_paths`` instead of
    ``target``/``payload``). Both artifacts share the state machine, the gate log
    and the audit trail.
    """

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
            self._rebuild_phase4_candidates_table()
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

    def _insert_audit(
        self, actor_id: str, action: str, candidate_id: int, note: str, now: float | None = None
    ) -> int:
        """Append one audit row without committing; the caller owns the transaction.

        The whitelist check runs before the INSERT, so an unknown action raises
        ``ValueError`` and writes nothing even inside a larger transaction.
        ``now`` is passed by callers that already sampled one clock so the audit
        timestamp can never drift past the transaction it belongs to (the Windows
        timer granularity makes two ``time.time()`` calls observable).
        """
        if action not in _AUDIT_ACTIONS:
            raise ValueError(f"invalid audit action: {action}")
        cursor = self._conn.execute(
            "INSERT INTO evolution_candidate_audit (actor_id, action, candidate_id, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(actor_id), str(action), int(candidate_id), str(note), time.time() if now is None else now),
        )
        return int(cursor.lastrowid)

    def add_candidate(
        self,
        observation_id: int | None,
        cluster_id: int,
        candidate: JevQuestionSetCandidate | CodePatchCandidate,
        *,
        prompt_version: str = "",
        model: str = "",
    ) -> int:
        """Persist one candidate (question set or code patch) and return its id.

        A candidate starts in ``candidate_testing`` inside the candidate pool and
        carries the layer-1 (scope) gate as its first ``gate_log`` entry, plus a
        ``generate`` audit row. Both writes share one transaction with the
        candidate row, so a candidate can never exist without its gate and audit
        history. Question-set payloads are validated by the generator through
        :func:`~rag.evolution.candidates.validate_payload` and every gate re-runs
        it; code patches are bounded by
        :func:`~rag.evolution.code_patches.validate_patch` the same way.
        """
        now = time.time()
        is_patch = candidate.artifact_type == "code_patch"
        if is_patch:
            scope_detail = (
                f"{candidate.artifact_type}/{candidate.layer} files={len(candidate.paths)}"
            )
            note = (
                f"generated {candidate.layer} patch from base_commit="
                f"{candidate.rollback_ref.get('base_commit', '')}"
            )
        else:
            scope_detail = (
                f"{candidate.artifact_type}/{candidate.target} "
                f"questions={len(candidate.payload)}"
            )
            note = (
                f"generated {candidate.target} candidate from question_set_version="
                f"{candidate.rollback_ref.get('question_set_version', '')}"
            )
        gate_log = [
            {
                "gate": "scope",
                "result": "accepted",
                "detail": scope_detail,
                "created_at": now,
            }
        ]
        with self._lock:
            self._require_cluster(int(cluster_id))
            if observation_id is not None:
                self._require_observation(int(observation_id))
            cursor = self._conn.execute(
                "INSERT INTO candidates ("
                "observation_id, cluster_id, artifact_type, target, payload, status, pool, "
                "gate_log, prompt_version, model, rollback_ref, rationale, expected_effect, "
                "confidence, diff, layer, test_paths, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    None if observation_id is None else int(observation_id),
                    int(cluster_id),
                    candidate.artifact_type,
                    None if is_patch else candidate.target,
                    None if is_patch else json.dumps(candidate.payload, ensure_ascii=False),
                    EvolutionStatus.CANDIDATE_TESTING.value,
                    ObservationPool.CANDIDATE_POOL.value,
                    json.dumps(gate_log, ensure_ascii=False),
                    str(prompt_version),
                    str(model),
                    json.dumps(candidate.rollback_ref, ensure_ascii=False),
                    candidate.rationale,
                    candidate.expected_effect,
                    candidate.confidence,
                    candidate.diff if is_patch else None,
                    candidate.layer if is_patch else None,
                    json.dumps(list(candidate.test_paths), ensure_ascii=False) if is_patch else None,
                    now,
                    now,
                ),
            )
            candidate_id = int(cursor.lastrowid)
            # The audit row needs the new candidate id, so it follows the
            # INSERT inside the same transaction and precedes any advance.
            self._insert_audit(str(model) or "generator", "generate", candidate_id, note, now)
            self._conn.commit()
        return candidate_id

    def record_inconclusive_gate(
        self, id: int, *, actor_id: str, note: str, gate: str, gate_detail: str
    ) -> dict:
        """Record a gate attempt that could not decide, without moving the candidate.

        An inconclusive round is usually infrastructure — the provider timed out on the
        baseline side — so rejecting the candidate would blame it for a fault it did not
        cause and burn the artifact. The candidate therefore stays in
        ``candidate_testing`` and the attempt is written to ``gate_log`` with
        ``result="inconclusive"``: visible to an operator, and explicitly *not* a gate
        result, so :meth:`candidates_without_gate_result` still offers it for a retry.

        The audit action is ``advance`` because the audit whitelist has no better home
        for "the gate ran and did not conclude"; the note carries the reason.
        """
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (int(id),)
            ).fetchone()
            if row is None:
                raise KeyError("candidate not found")
            candidate_id = int(row["id"])
            self._insert_audit(str(actor_id), _DEFAULT_AUDIT_ACTION, candidate_id, str(note), now)
            gate_log = _decode_json(row["gate_log"])
            if not isinstance(gate_log, list):
                gate_log = []
            gate_log.append(
                {
                    "gate": str(gate),
                    "result": _INCONCLUSIVE_GATE_RESULT,
                    "detail": "" if gate_detail is None else str(gate_detail),
                    "created_at": now,
                }
            )
            self._conn.execute(
                "UPDATE candidates SET gate_log = ? WHERE id = ?",
                (json.dumps(gate_log, ensure_ascii=False), candidate_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return _decode_candidate_row(updated)

    def record_gate_entry(
        self,
        id: int,
        *,
        actor_id: str,
        note: str,
        gate: str,
        result: str,
        gate_detail: str,
        gate_metrics: dict | None = None,
    ) -> dict:
        """Append one gate entry as **evidence**, without moving the candidate's status.

        Some measurements are evidence for a decision rather than a decision themselves. Layer 2's
        host A/B is the case that forced this method (2026-10-12): the script ran the comparison and
        wrote its verdict into an artifact file, so the review panel never showed it and the
        administrator judged from something outside the system. Recording it has to be possible
        without pretending the candidate moved — the status stays whatever the decision it belongs
        to left it at.

        ``result`` is checked against :data:`GATE_EVIDENCE_RESULTS` because the entry is *not*
        allowed to claim the layer-1 ``accepted``: nothing that ran afterwards can accept a
        candidate on the generation gate's behalf. The audit row is ``advance``-shaped, as in
        :meth:`record_inconclusive_gate`, and the note carries the reason.
        """
        result = str(result or "").strip()
        if result not in GATE_EVIDENCE_RESULTS:
            raise ValueError(
                f"unknown gate evidence result: {result!r} (expected one of {sorted(GATE_EVIDENCE_RESULTS)})"
            )
        if not str(gate or "").strip():
            raise ValueError("a gate entry needs a gate name")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (int(id),)
            ).fetchone()
            if row is None:
                raise KeyError("candidate not found")
            candidate_id = int(row["id"])
            self._insert_audit(str(actor_id), _DEFAULT_AUDIT_ACTION, candidate_id, str(note), now)
            gate_log = _decode_json(row["gate_log"])
            if not isinstance(gate_log, list):
                gate_log = []
            entry = {
                "gate": str(gate),
                "result": result,
                "detail": "" if gate_detail is None else str(gate_detail),
                "created_at": now,
            }
            if gate_metrics:
                entry["metrics"] = gate_metrics
            gate_log.append(entry)
            self._conn.execute(
                "UPDATE candidates SET gate_log = ?, updated_at = ? WHERE id = ?",
                (json.dumps(gate_log, ensure_ascii=False), now, candidate_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return _decode_candidate_row(updated)

    def advance_candidate(
        self,
        id: int,
        new_status: str,
        *,
        actor_id: str,
        note: str,
        gate: str | None = None,
        gate_detail: str | None = None,
        gate_metrics: dict | None = None,
    ) -> dict:
        """Move one candidate through ``EVOLUTION_TRANSITIONS`` and return the new row.

        The order inside the single transaction is fixed: read the row, validate
        the transition (illegal ⇒ ``ValueError`` and nothing is written), write
        the audit row, append the optional gate entry, then update the status.
        So an audit row can never exist for a move that did not happen, and a
        gate entry can never be missing from a move that did.

        ``gate``/``gate_detail`` record which of the five layers this move
        represents, and ``gate_metrics`` carries the numbers behind that conclusion (the sandbox's
        clean-versus-patched comparison, for instance) so a review panel can compare instead of
        parsing prose; ``result`` is derived from the target status (``failed`` for
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
                now,
            )
            gate_log = _decode_json(row["gate_log"])
            if not isinstance(gate_log, list):
                gate_log = []
            if gate is not None:
                entry = {
                    "gate": str(gate),
                    "result": _GATE_RESULT_BY_STATUS.get(new_status, _DEFAULT_GATE_RESULT),
                    "detail": "" if gate_detail is None else str(gate_detail),
                    "created_at": now,
                }
                # 结构化指标随条目一起落库（JSON 列，老条目没有这个键 ⇒ 向前兼容）。
                if gate_metrics:
                    entry["metrics"] = gate_metrics
                gate_log.append(entry)
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

    def observations_awaiting_code_patch(self, limit: int) -> list[dict]:
        """Approved clusters' member observations that have no **open** code patch yet.

        The phase-5 generator runs outside the worker's round by design, so this
        read-only query is where the queue is visible before an administrator
        triggers a generation by hand: one row per member observation of a cluster
        whose status is ``approved_for_candidate`` and which no **open** ``code_patch``
        candidate mentions yet. Each row carries both layer authorities the
        generator weighs — the cluster's administrator-confirmed
        ``confirmed_layer`` and the newest draft's own ``suggested_layer`` — and
        the prompt version that draft was produced with, so the list is auditable
        without opening the cluster detail.

        "Open" is :data:`OPEN_CANDIDATE_STATUSES` on purpose (C-08, 2026-10-22): a candidate an
        administrator **rejected** (or asked for more evidence on) is a concluded attempt, not a
        reservation. Excluding the observation for the rest of its life made a review decision
        permanent — the queue said "nothing to generate" while the failure it described was still
        happening.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT o.id AS observation_id, o.question AS question, "
                "       o.status AS observation_status, o.kb_id AS observation_kb_id, "
                "       o.stage AS observation_stage, "
                "       c.id AS cluster_id, c.status AS cluster_status, "
                "       c.kb_id AS cluster_kb_id, c.failure_stage AS cluster_stage, "
                "       c.root_cause_feature AS root_cause_feature, "
                "       c.confirmed_layer AS confirmed_layer, "
                "       (SELECT json_extract(d.payload, '$.suggested_layer') "
                "          FROM diagnosis_drafts d WHERE d.observation_id = o.id "
                "         ORDER BY d.id DESC LIMIT 1) AS draft_suggested_layer, "
                "       (SELECT d.prompt_version FROM diagnosis_drafts d "
                "         WHERE d.observation_id = o.id "
                "         ORDER BY d.id DESC LIMIT 1) AS draft_prompt_version "
                "FROM failure_observations o "
                "JOIN failure_clusters c ON c.id = o.cluster_id "
                "WHERE c.status = ? "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM candidates p WHERE p.artifact_type = ? "
                "        AND p.observation_id = o.id"
                f"        AND p.status IN ({_OPEN_STATUS_PLACEHOLDERS})"
                "  ) "
                "ORDER BY o.id ASC LIMIT ?",
                (
                    EvolutionStatus.APPROVED_FOR_CANDIDATE.value,
                    CODE_PATCH_ARTIFACT_TYPE,
                    *OPEN_CANDIDATE_STATUSES,
                    int(limit),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def candidates_without_gate_result(self, gate_name: str, limit: int) -> list[dict]:
        """Candidates still in ``candidate_testing`` that ``gate_name`` never ran on.

        Drives a gate round: the runner takes this list, evaluates the gate, and
        reports the outcome back through :meth:`advance_candidate`, so a gate
        never runs twice on the same candidate. A candidate already advanced
        past testing is excluded, because its remaining layers happened later.

        The benchmark gate serves question sets only — code patches are validated
        by the sandbox gate instead — so the scan filters by artifact type.

        A gate entry whose result is ``inconclusive`` does **not** count as a result: that
        round could not decide (an infrastructure fault, typically), so the candidate is
        offered again until a round actually concludes.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM candidates WHERE status = ? AND artifact_type = ? "
                "AND NOT EXISTS ("
                "    SELECT 1 FROM json_each(candidates.gate_log) AS entry "
                "    WHERE json_extract(entry.value, '$.gate') = ? "
                "      AND json_extract(entry.value, '$.result') != ?"
                ") "
                "ORDER BY id ASC LIMIT ?",
                (
                    EvolutionStatus.CANDIDATE_TESTING.value,
                    "jev_question_set",
                    str(gate_name),
                    _INCONCLUSIVE_GATE_RESULT,
                    int(limit),
                ),
            ).fetchall()
        return [_decode_candidate_row(row) for row in rows]

    def _rebuild_phase4_candidates_table(self) -> None:
        """Rebuild a phase-4 ``candidates`` table (CHECK + NOT NULL target) in place.

        ``PRAGMA table_info`` still reports ``notnull`` for ``target`` after the
        column migrations, which is exactly the phase-4 shape that blocks a code
        patch (NULL target). SQLite DDL is transactional, so the rebuild either
        completes whole or not at all; a phase-5 table (nullable target) is left
        untouched.
        """
        info = {
            row["name"]: row for row in self._conn.execute("PRAGMA table_info(candidates)")
        }
        if not info.get("target") or not info["target"]["notnull"]:
            return
        columns = ", ".join(_CANDIDATES_COPY_COLUMNS)
        self._conn.execute("DROP TABLE IF EXISTS candidates_phase5")
        self._conn.execute(_CANDIDATES_PHASE5_DDL)
        self._conn.execute(
            f"INSERT INTO candidates_phase5 ({columns}) SELECT {columns} FROM candidates"
        )
        self._conn.execute("DROP TABLE candidates")
        self._conn.execute("ALTER TABLE candidates_phase5 RENAME TO candidates")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_cluster ON candidates(cluster_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_observation ON candidates(observation_id)"
        )
        self._conn.commit()

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


__all__ = ["OPEN_CANDIDATE_STATUSES", "CandidateStore"]
