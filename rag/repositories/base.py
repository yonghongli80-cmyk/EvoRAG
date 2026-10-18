"""Base repository defining shared database access primitives."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rag.db.connection import ConnectionManager
    from rag.user_db import UserDB


class VersionConflict(Exception):
    """Raised when a version operation cannot proceed due to state conflict."""


class BaseRepository:
    """Base repository for all domain-specific data operations."""

    def __init__(self, connection_manager: ConnectionManager, db: UserDB | None = None) -> None:
        self.cm = connection_manager
        self.db = db

    @contextmanager
    def read_connection(self):
        """Yield a thread-local read-only SQLite connection."""
        with self.cm.read_connection() as conn:
            yield conn

    @contextmanager
    def write_connection(self):
        """Yield the exclusive write SQLite connection with write lock."""
        with self.cm.write_connection() as conn:
            yield conn

    @contextmanager
    def transaction(self, immediate: bool = False):
        """Yield the write connection inside a single transaction."""
        with self.cm.transaction(immediate=immediate) as conn:
            yield conn


def citations_hash(citations: list[dict[str, Any]] | None) -> str:
    """Return deterministic SHA256 hex digest for structured citations."""
    if not citations:
        return ""
    payload = json.dumps(citations, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_json_text(value: Any) -> str:
    """Return canonical, sorted JSON string for stable hashes and equality."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def serialize_reliability(
    confidence_level: str | None,
    confidence_reasons: list[str] | None,
    answerability_kind: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Normalize and validate message reliability metadata fields."""
    reasons_json: str | None = None
    if confidence_reasons is not None:
        reasons_json = json.dumps(confidence_reasons, ensure_ascii=False)
    return confidence_level, reasons_json, answerability_kind
