"""Persistent material index jobs: enqueue, debounce-cancel, and lease claim.

Backing store lives on ``UserDB``; these module-level helpers keep the
material package a single import surface.
"""

from __future__ import annotations

from rag.user_db import UserDB


def enqueue_latest_index_job(
    db: UserDB, document_id: int, version_id: int, content_hash: str, not_before: float
) -> int:
    return db.enqueue_latest_index_job(document_id, version_id, content_hash, not_before)


def claim_material_jobs(
    db: UserDB, worker_id: str, now: float, limit: int, lease_seconds: int
) -> list[dict]:
    return db.claim_material_jobs(worker_id, now, limit, lease_seconds)
