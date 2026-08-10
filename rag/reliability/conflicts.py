"""Deterministic conflict candidates derived from visible evidence claims."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rag.user_db import UserDB


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class ConflictClaim:
    """One ACL-filtered factual claim with its supporting citation."""

    subject: str
    attribute: str
    value: str
    unit: str
    scope: str
    effective_at: str | None
    citation_id: int
    acl_checked: bool
    document_id: int | None = None
    document_version_id: int | None = None

    @property
    def subject_key(self) -> str:
        return _normalize(self.subject)

    @property
    def attribute_key(self) -> str:
        return _normalize(self.attribute)

    @property
    def unit_key(self) -> str:
        return _normalize(self.unit)

    @property
    def scope_key(self) -> str:
        return _normalize(self.scope)

    @property
    def normalized_value(self) -> str:
        return _normalize(self.value)


@dataclass(frozen=True)
class ConflictCandidate:
    """A pair of incompatible claims that requires an owner decision."""

    claims: tuple[ConflictClaim, ConflictClaim]
    citation_ids: tuple[int, int]


def scopes_overlap(left: str, right: str) -> bool:
    return left == "all" or right == "all" or left == right


def claims_conflict(left: ConflictClaim, right: ConflictClaim) -> bool:
    return (
        left.subject_key == right.subject_key
        and left.attribute_key == right.attribute_key
        and left.unit_key == right.unit_key
        and scopes_overlap(left.scope_key, right.scope_key)
        and left.normalized_value != right.normalized_value
    )


def detect_conflicts(claims: Sequence[ConflictClaim]) -> list[ConflictCandidate]:
    """Return deterministic conflict candidates from ACL-filtered claims only."""
    if any(not claim.acl_checked for claim in claims):
        raise ValueError("acl_checked claims required")
    candidates: list[ConflictCandidate] = []
    for index, left in enumerate(claims):
        for right in claims[index + 1 :]:
            if claims_conflict(left, right):
                candidates.append(
                    ConflictCandidate(
                        claims=(left, right),
                        citation_ids=tuple(sorted((left.citation_id, right.citation_id))),
                    )
                )
    return candidates


def _conflict_active_key(candidate: ConflictCandidate) -> str:
    left, _ = candidate.claims
    version_ids = sorted(
        claim.document_version_id for claim in candidate.claims if claim.document_version_id is not None
    )
    raw = json.dumps(
        {
            "subject": left.subject_key,
            "attribute": left.attribute_key,
            "unit": left.unit_key,
            "scope": left.scope_key,
            "document_version_ids": version_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def persist_conflict(user_db: UserDB, candidate: ConflictCandidate) -> dict[str, Any]:
    """Persist a visible candidate with a stable active key for idempotency."""
    left, _ = candidate.claims
    if any(
        claim.document_id is None or claim.document_version_id is None
        for claim in candidate.claims
    ):
        raise ValueError("conflict claims require document and version identifiers")
    return user_db.create_knowledge_conflict(
        active_key=_conflict_active_key(candidate),
        subject_key=left.subject_key,
        attribute_key=left.attribute_key,
        unit_key=left.unit_key,
        scope_key=left.scope_key,
        citation_ids=candidate.citation_ids,
        document_ids=tuple(int(claim.document_id) for claim in candidate.claims),
        document_version_ids=tuple(int(claim.document_version_id) for claim in candidate.claims),
    )


def resolve_conflict(
    user_db: UserDB,
    conflict_id: int,
    actor: dict[str, Any],
    resolution: str,
    note: str,
) -> dict[str, Any]:
    """Persist an owner/admin conflict decision without exposing document details."""
    try:
        actor_id = int(actor["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PermissionError("not authorized to resolve conflict") from exc

    resolved = user_db.resolve_knowledge_conflict(
        conflict_id,
        actor_id=actor_id,
        resolution=resolution,
        note=note,
    )
    if resolved is None:
        raise LookupError("conflict not found")
    return resolved
