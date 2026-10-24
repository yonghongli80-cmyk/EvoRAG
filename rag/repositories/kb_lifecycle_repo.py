"""Persistence operations for durable KB lifecycle tombstones."""

from __future__ import annotations

from typing import Any

from rag.repositories.base import BaseRepository


class KBLifecycleRepository(BaseRepository):
    """Read lifecycle state without conflating it with KB authorization."""

    def get(self, kb_id: str) -> dict[str, Any] | None:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT kb_id, state, delete_token, generation, requested_at, deleted_at, last_error_code "
                "FROM kb_lifecycle WHERE kb_id = ?",
                (kb_id,),
            ).fetchone()
        return dict(row) if row is not None else None
