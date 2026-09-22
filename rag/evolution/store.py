"""Independent SQLite store for evolution observations, clusters, and skips.

The evolution database is separate from ``memory.db``/``users.db`` and holds only
hashes, scores, versions, and enums; it is shared by the API process and the
background worker through one lock, in the same pattern as ``rag/user_db.py``.
Deleting the file is the documented rollback, so no migration path is required.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from config import settings
from rag.evolution.models import (
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


def _decode_row(row: sqlite3.Row) -> dict:
    decoded = dict(row)
    for column in _JSON_COLUMNS:
        value = decoded.get(column)
        if isinstance(value, str):
            decoded[column] = json.loads(value)
    return decoded


class FailureTraceStore:
    """Persistence for the observation phase; content-free by construction."""

    def __init__(self, db_path=None):
        self._conn = sqlite3.connect(
            str(db_path or settings.evolution_db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        with self._lock:
            self._conn.execute(_OBSERVATIONS_TABLE_SQL)
            self._conn.execute(_CLUSTERS_TABLE_SQL)
            self._conn.execute(_SKIP_LOG_TABLE_SQL)
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

    def close(self) -> None:
        self._conn.close()
