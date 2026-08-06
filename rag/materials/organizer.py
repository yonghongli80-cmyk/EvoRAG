"""Typed, bounded organization proposals with optimistic concurrency.

The backend supplies only the authorized candidate document set and the
allowed target paths/types/tags. The model output is validated item by item;
unknown candidates, illegal paths, unknown types/tags, ACL failures, deleted
documents, and sub-threshold confidence are rejected before any mutation.
Confirmation applies each executable item with a ``metadata_version`` CAS and
records before/after values for rollback. Partial conflicts never roll back
unrelated successful items, and rollback never overwrites a later manual change.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from rag.generator import generate_structured
from rag.user_db import UserDB

MIN_CONFIDENCE = 0.70
PROPOSAL_TTL_SECONDS = 15 * 60


class OrganizationError(Exception):
    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        super().__init__(f"organization rejected: {reason_code}: {detail}")


class OrganizationInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: Annotated[str, Field(min_length=1, max_length=1000)]
    document_ids: tuple[int, ...] = ()


class OrganizationItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    document_id: int
    target_path: str = ""
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()
    document_type: str = ""
    reason: str = ""
    confidence: float = 0.0


class OrganizationProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: tuple[OrganizationItemModel, ...] = ()


@dataclass(frozen=True)
class OrganizationPreviewItem:
    id: int
    document_id: int
    target_path: str
    document_type: str
    status: str
    reason_code: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class OrganizationPreview:
    id: int
    items: tuple[OrganizationPreviewItem, ...]

    @property
    def executable_items(self) -> tuple[OrganizationPreviewItem, ...]:
        return tuple(item for item in self.items if item.status == "executable")

    @property
    def rejected_items(self) -> tuple[OrganizationPreviewItem, ...]:
        return tuple(item for item in self.items if item.status == "rejected")


@dataclass(frozen=True)
class OrganizationBatchItem:
    id: int
    document_id: int
    status: str
    reason_code: str


@dataclass(frozen=True)
class OrganizationBatchResult:
    batch_id: int
    proposal_id: int
    status: str
    items: tuple[OrganizationBatchItem, ...]


def _safe_target_path(path: str) -> bool:
    return bool(path) and not path.startswith("/") and ".." not in path and "\x00" not in path


def _instruction_hash(instruction: OrganizationInstruction) -> str:
    payload = json.dumps(
        [instruction.text, sorted(instruction.document_ids)], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_candidate_docs(db: UserDB, user: dict) -> dict[int, dict]:
    """Authorized, editable, non-deleted documents with a current version."""
    candidates = {}
    for doc in db.list_document_permissions_all():
        full = db.get_document_permission_by_id(doc["id"])
        if full is None or full.get("deleted_at") or full["current_version_id"] is None:
            continue
        can_edit = (
            user.get("is_admin")
            or full["owner_id"] == user["id"]
            or (
                full.get("scope") == "shared"
                and db.is_document_shared(full["id"], user["id"], permission="edit")
            )
        )
        if can_edit:
            candidates[full["id"]] = full
    return candidates


class OrganizationService:
    def __init__(
        self,
        db: UserDB,
        *,
        generate: Callable[[list[dict], type], object] | None = None,
        candidate_docs: Callable[[UserDB, dict], dict[int, dict]] | None = None,
        allowed_types: set[str] | None = None,
        allowed_tags: set[str] | None = None,
    ):
        self.db = db
        self.generate = generate or generate_structured
        self.candidate_docs = candidate_docs or _default_candidate_docs
        self.allowed_types = allowed_types
        self.allowed_tags = allowed_tags

    def preview(self, instruction: OrganizationInstruction, actor: dict) -> OrganizationPreview:
        candidates = self.candidate_docs(self.db, actor)
        messages = [
            {
                "role": "user",
                "content": (
                    f"{instruction.text}\n可操作的文档 ID: {','.join(map(str, candidates))}"
                ),
            }
        ]
        raw = self.generate(messages, OrganizationProposalModel)
        model_proposal = (
            raw
            if isinstance(raw, OrganizationProposalModel)
            else OrganizationProposalModel.model_validate(raw)
        )

        proposal_id = self.db.create_organization_proposal(
            actor["id"], _instruction_hash(instruction), time.time() + PROPOSAL_TTL_SECONDS
        )
        items = []
        for item in model_proposal.items:
            item_id = self.db.add_organization_item(
                proposal_id,
                item.document_id,
                item.target_path,
                json.dumps(list(item.add_tags), ensure_ascii=False),
                json.dumps(list(item.remove_tags), ensure_ascii=False),
                item.document_type,
                item.reason,
                item.confidence,
            )
            reason = self._validate_item(item, candidates, actor)
            status = "rejected" if reason else "executable"
            self.db.set_organization_item_status(item_id, status, reason or "")
            items.append(
                OrganizationPreviewItem(
                    id=item_id,
                    document_id=item.document_id,
                    target_path=item.target_path,
                    document_type=item.document_type,
                    status=status,
                    reason_code=reason or "",
                    reason=item.reason,
                    confidence=item.confidence,
                )
            )
        return OrganizationPreview(id=proposal_id, items=tuple(items))

    def _validate_item(
        self, item: OrganizationItemModel, candidates: dict[int, dict], actor: dict
    ) -> str:
        doc = candidates.get(item.document_id)
        if doc is None:
            return "unknown_candidate"
        if not _safe_target_path(item.target_path):
            return "illegal_path"
        if self.allowed_types is not None and item.document_type not in self.allowed_types:
            return "unknown_type"
        if self.allowed_tags is not None and (
            not set(item.add_tags) <= self.allowed_tags
            or not set(item.remove_tags) <= self.allowed_tags
        ):
            return "unknown_tag"
        if item.confidence < MIN_CONFIDENCE:
            return "low_confidence"
        if not (
            actor.get("is_admin")
            or doc["owner_id"] == actor["id"]
            or (
                doc.get("scope") == "shared"
                and self.db.is_document_shared(doc["id"], actor["id"], permission="edit")
            )
        ):
            return "permission_conflict"
        return ""

    def confirm(
        self,
        proposal_id: int,
        selected_item_ids: tuple[int, ...],
        actor: dict,
        expected_version: int | None = None,
        idempotency_key: str = "",
    ) -> OrganizationBatchResult:
        prior = self.db.get_organization_batch(proposal_id, idempotency_key)
        if prior is not None:
            return self._batch_result(prior["id"], prior["status"])

        proposal = self.db.get_organization_proposal(proposal_id)
        if proposal is None:
            raise OrganizationError("proposal_missing")
        if proposal["status"] != "pending":
            raise OrganizationError("proposal_not_pending")
        if time.time() > proposal["expires_at"]:
            raise OrganizationError("proposal_expired")
        if not (actor.get("is_admin") or proposal["actor_id"] == actor["id"]):
            raise OrganizationError("no_confirm_permission")

        items = self.db.list_organization_items(proposal_id)
        selected = [item for item in items if item["id"] in selected_item_ids and item["status"] == "executable"]

        with self.db._lock, self.db.transaction(immediate=True):
            batch_id = self.db._create_organization_batch_in_tx(proposal_id, actor["id"], idempotency_key)
            batch_items = []
            for item in selected:
                status, reason_code, before, after, before_version, after_version = self._apply_item_in_tx(item, actor)
                row_id = self._record_batch_item_in_tx(
                    batch_id, item["document_id"], before, after, before_version, after_version, status, reason_code
                )
                batch_items.append(
                    OrganizationBatchItem(id=row_id, document_id=item["document_id"], status=status, reason_code=reason_code)
                )
            if batch_items and all(i.status != "applied" for i in batch_items):
                self.db._conn.execute(
                    "UPDATE material_organization_batches SET status='conflict' WHERE id=?",
                    (batch_id,),
                )
        return OrganizationBatchResult(
            batch_id=batch_id, proposal_id=proposal_id, status="applied", items=tuple(batch_items)
        )

    def _apply_item_in_tx(self, item: dict, actor: dict):
        doc = self.db._conn.execute(
            "SELECT id, doc_name, kb_id, owner_id, scope, metadata_version, deleted_at, "
            "logical_path, tags_json, document_type "
            "FROM document_permissions WHERE id = ?",
            (item["document_id"],),
        ).fetchone()
        if doc is None:
            return "permission_conflict", "document_missing", None, None, 0, 0
        if doc["deleted_at"] is not None:
            return "permission_conflict", "deleted", doc["logical_path"], None, 0, 0
        can_edit = (
            actor.get("is_admin")
            or doc["owner_id"] == actor["id"]
            or (
                doc["scope"] == "shared"
                and self.db.is_document_shared(doc["id"], actor["id"], permission="edit")
            )
        )
        if not can_edit:
            return "permission_conflict", "acl", doc["logical_path"], None, 0, 0
        before_version = doc["metadata_version"]
        after_path = item["target_path"]
        after_type = item["document_type"]
        current_tags = set(json.loads(doc["tags_json"] or "[]"))
        removed_tags = set(json.loads(item["remove_tags"] or "[]"))
        added_tags = set(json.loads(item["add_tags"] or "[]"))
        after_tags = sorted(current_tags - removed_tags | added_tags)
        updated = self.db._conn.execute(
            "UPDATE document_permissions "
            "SET logical_path=?, tags_json=?, document_type=?, metadata_version=metadata_version+1 "
            "WHERE id=? AND metadata_version=? AND deleted_at IS NULL",
            (after_path, json.dumps(after_tags, ensure_ascii=False), after_type, doc["id"], before_version),
        )
        if updated.rowcount != 1:
            return "metadata_conflict", "version_changed", doc["logical_path"], None, before_version, before_version
        return "applied", "", doc["logical_path"], after_path, before_version, before_version + 1

    def _record_batch_item_in_tx(self, batch_id, document_id, before, after, before_version, after_version, status, reason_code):
        cur = self.db._conn.execute(
            "INSERT INTO material_organization_batch_items "
            "(batch_id, document_id, before_logical_path, after_logical_path, "
            "before_metadata_version, after_metadata_version, status, reason_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, document_id, before or "", after or "", before_version, after_version, status, reason_code),
        )
        return int(cur.lastrowid)

    def rollback(self, batch_id: int, actor: dict, idempotency_key: str = "") -> OrganizationBatchResult:
        batch = self.db.get_organization_batch_by_id(batch_id)
        if batch is None:
            raise OrganizationError("batch_missing")
        if not (actor.get("is_admin") or batch["actor_id"] == actor["id"]):
            raise OrganizationError("no_rollback_permission")
        batch_items = self.db.list_organization_batch_items(batch_id)
        with self.db._lock, self.db.transaction(immediate=True):
            items = []
            for row in batch_items:
                if row["status"] == "rolled_back":
                    items.append(OrganizationBatchItem(id=row["id"], document_id=row["document_id"], status="rolled_back", reason_code=""))
                    continue
                current = self.db._conn.execute(
                    "SELECT metadata_version, logical_path FROM document_permissions WHERE id=?",
                    (row["document_id"],),
                ).fetchone()
                if current is None or current["metadata_version"] != row["after_metadata_version"]:
                    items.append(OrganizationBatchItem(id=row["id"], document_id=row["document_id"], status="metadata_conflict", reason_code="changed_since"))
                    continue
                self.db._conn.execute(
                    "UPDATE document_permissions "
                    "SET logical_path=?, metadata_version=? "
                    "WHERE id=? AND metadata_version=?",
                    (row["before_logical_path"], row["before_metadata_version"], row["document_id"], row["after_metadata_version"]),
                )
                self.db._conn.execute(
                    "UPDATE material_organization_batch_items SET status='rolled_back' WHERE id=?",
                    (row["id"],),
                )
                items.append(OrganizationBatchItem(id=row["id"], document_id=row["document_id"], status="rolled_back", reason_code=""))
        return OrganizationBatchResult(batch_id=batch_id, proposal_id=batch["proposal_id"], status="rolled_back", items=tuple(items))

    def _batch_result(self, batch_id: int, status: str) -> OrganizationBatchResult:
        batch = self.db.get_organization_batch_by_id(batch_id)
        proposal_id = batch["proposal_id"] if batch else 0
        rows = self.db.list_organization_batch_items(batch_id)
        return OrganizationBatchResult(
            batch_id=batch_id,
            proposal_id=proposal_id,
            status=status,
            items=tuple(
                OrganizationBatchItem(id=r["id"], document_id=r["document_id"], status=r["status"], reason_code=r["reason_code"])
                for r in rows
            ),
        )
