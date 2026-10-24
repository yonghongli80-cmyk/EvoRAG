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

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from rag.api_security import require_doc_edit, require_kb_edit
from rag.generator import generate_structured
from rag.kb_lifecycle import assert_kb_available_in_tx
from rag.org_auth.models import OrgRole
from rag.org_auth.repository import OrgRepository, is_org_policy
from rag.org_auth.service import OrgAuthorizationService
from rag.repositories.material_repo import ensure_organization_batch_item_columns
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
    """Authorized, editable, non-deleted documents with a current version.

    The document/current-version rows are loaded in one query and edit
    authorization is resolved once per knowledge base instead of once per
    document (D observation: locator/organizer N+1).  This is the batched form
    of :func:`rag.api_security.require_doc_edit`.
    """
    rows = db.material_repo.list_material_documents_with_current_version()
    documents_by_kb: dict[str, list[dict]] = {}
    for row in rows:
        documents_by_kb.setdefault(row["kb_id"], []).append(row)

    user_id = int(user["id"])
    candidates: dict[int, dict] = {}
    for kb_id, documents in documents_by_kb.items():
        policy = OrgRepository(db).get_kb_policy(kb_id)
        if is_org_policy(policy):
            if not OrgAuthorizationService(db).authorize(
                user_id, kb_id, OrgRole.MANAGE
            ).allowed:
                continue
            for document in documents:
                if not document.get("protected"):
                    candidates[int(document["id"])] = document
            continue
        if user.get("is_admin"):
            for document in documents:
                if not document.get("protected"):
                    candidates[int(document["id"])] = document
            continue
        document_ids = [int(document["id"]) for document in documents]
        shared_ids = db.get_shared_doc_ids_for_user(user_id, document_ids)
        edit_shared_ids = {
            document_id
            for document_id in shared_ids
            if db.is_document_shared(document_id, user_id, permission="edit")
        }
        for document in documents:
            if document.get("protected"):
                continue
            document_id = int(document["id"])
            if int(document["owner_id"]) == user_id or (
                document.get("scope") == "shared" and document_id in edit_shared_ids
            ):
                candidates[document_id] = document
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
        # The D-10 rollback payload lives in columns added by a compatibility
        # step for databases created before the fix (see material_repo).
        self._rollback_payload_columns_ready = False

    def _ensure_rollback_payload_columns(self) -> None:
        if self._rollback_payload_columns_ready:
            return
        with self.db._lock:
            ensure_organization_batch_item_columns(self.db._conn)
            self.db._conn.commit()
        self._rollback_payload_columns_ready = True

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
        try:
            if doc["kb_id"] != "rag_docs":
                require_kb_edit(self.db, doc["kb_id"], actor)
            require_doc_edit(self.db, doc["doc_name"], doc["kb_id"], actor)
        except HTTPException:
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

        self._ensure_rollback_payload_columns()

        items = self.db.list_organization_items(proposal_id)
        selected = [item for item in items if item["id"] in selected_item_ids and item["status"] == "executable"]
        authorized_ids: set[int] = set()
        for item in selected:
            document = self.db.get_document_permission_by_id(item["document_id"])
            if document is None:
                continue
            try:
                if document["kb_id"] != "rag_docs":
                    require_kb_edit(self.db, document["kb_id"], actor)
                require_doc_edit(
                    self.db, document["doc_name"], document["kb_id"], actor
                )
            except HTTPException:
                continue
            authorized_ids.add(int(item["document_id"]))

        status = "applied"
        with self.db._lock, self.db.transaction(immediate=True):
            batch_id = self.db._create_organization_batch_in_tx(proposal_id, actor["id"], idempotency_key)
            batch_items = []
            for item in selected:
                applied = self._apply_item_in_tx(item, authorized_ids)
                row_id = self._record_batch_item_in_tx(
                    batch_id,
                    item["document_id"],
                    applied["before_path"],
                    applied["after_path"],
                    applied["before_version"],
                    applied["after_version"],
                    applied["status"],
                    applied["reason_code"],
                    before_tags_json=applied["before_tags_json"],
                    after_tags_json=applied["after_tags_json"],
                    before_document_type=applied["before_document_type"],
                    after_document_type=applied["after_document_type"],
                )
                batch_items.append(
                    OrganizationBatchItem(id=row_id, document_id=item["document_id"], status=applied["status"], reason_code=applied["reason_code"])
                )
            if batch_items:
                applied_count = sum(1 for i in batch_items if i.status == "applied")
                if applied_count == 0:
                    status = "conflict"
                elif applied_count != len(batch_items):
                    status = "partial"
            if status != "applied":
                self.db._conn.execute(
                    "UPDATE material_organization_batches SET status=? WHERE id=?",
                    (status, batch_id),
                )
        return OrganizationBatchResult(
            batch_id=batch_id, proposal_id=proposal_id, status=status, items=tuple(batch_items)
        )

    def _apply_item_in_tx(self, item: dict, authorized_ids: set[int]) -> dict:
        """Apply one item, returning the real status plus the rollback payload."""
        doc = self.db._conn.execute(
            "SELECT id, doc_name, kb_id, owner_id, scope, metadata_version, deleted_at, "
            "logical_path, tags_json, document_type "
            "FROM document_permissions WHERE id = ?",
            (item["document_id"],),
        ).fetchone()
        if doc is None:
            return self._conflict("document_missing", None, None)
        assert_kb_available_in_tx(self.db._conn, str(doc["kb_id"]))
        if doc["deleted_at"] is not None:
            return self._conflict("deleted", doc["logical_path"], doc["tags_json"], doc["document_type"])
        if int(doc["id"]) not in authorized_ids:
            return self._conflict("acl", doc["logical_path"], doc["tags_json"], doc["document_type"])
        before_version = doc["metadata_version"]
        after_path = item["target_path"]
        after_type = item["document_type"]
        current_tags = set(json.loads(doc["tags_json"] or "[]"))
        removed_tags = set(json.loads(item["remove_tags"] or "[]"))
        added_tags = set(json.loads(item["add_tags"] or "[]"))
        after_tags = sorted(current_tags - removed_tags | added_tags)
        after_tags_json = json.dumps(after_tags, ensure_ascii=False)
        updated = self.db._conn.execute(
            "UPDATE document_permissions "
            "SET logical_path=?, tags_json=?, document_type=?, metadata_version=metadata_version+1 "
            "WHERE id=? AND metadata_version=? AND deleted_at IS NULL",
            (after_path, after_tags_json, after_type, doc["id"], before_version),
        )
        if updated.rowcount != 1:
            return self._conflict(
                "version_changed",
                doc["logical_path"],
                doc["tags_json"],
                doc["document_type"],
                before_version=before_version,
            )
        return {
            "status": "applied",
            "reason_code": "",
            "before_path": doc["logical_path"],
            "after_path": after_path,
            "before_version": before_version,
            "after_version": before_version + 1,
            "before_tags_json": doc["tags_json"] or "[]",
            "after_tags_json": after_tags_json,
            "before_document_type": doc["document_type"] or "",
            "after_document_type": after_type or "",
        }

    @staticmethod
    def _conflict(
        reason_code: str,
        before_path: str | None,
        before_tags_json: str | None,
        before_document_type: str | None = None,
        *,
        before_version: int = 0,
    ) -> dict:
        return {
            "status": "metadata_conflict" if reason_code == "version_changed" else "permission_conflict",
            "reason_code": reason_code,
            "before_path": before_path,
            "after_path": None,
            "before_version": before_version,
            "after_version": before_version,
            "before_tags_json": before_tags_json,
            "after_tags_json": None,
            "before_document_type": before_document_type,
            "after_document_type": None,
        }

    def _record_batch_item_in_tx(
        self,
        batch_id,
        document_id,
        before,
        after,
        before_version,
        after_version,
        status,
        reason_code,
        *,
        before_tags_json=None,
        after_tags_json=None,
        before_document_type=None,
        after_document_type=None,
    ):
        cur = self.db._conn.execute(
            "INSERT INTO material_organization_batch_items "
            "(batch_id, document_id, before_logical_path, after_logical_path, "
            "before_metadata_version, after_metadata_version, status, reason_code, "
            "before_tags_json, after_tags_json, before_document_type, after_document_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id, document_id, before or "", after or "",
                before_version, after_version, status, reason_code,
                before_tags_json, after_tags_json,
                before_document_type, after_document_type,
            ),
        )
        return int(cur.lastrowid)

    def rollback(self, batch_id: int, actor: dict, idempotency_key: str = "") -> OrganizationBatchResult:
        batch = self.db.get_organization_batch_by_id(batch_id)
        if batch is None:
            raise OrganizationError("batch_missing")
        if not (actor.get("is_admin") or batch["actor_id"] == actor["id"]):
            raise OrganizationError("no_rollback_permission")
        self._ensure_rollback_payload_columns()
        batch_items = self.db.list_organization_batch_items(batch_id)
        with self.db._lock, self.db.transaction(immediate=True):
            items = []
            for row in batch_items:
                if row["status"] == "rolled_back":
                    items.append(OrganizationBatchItem(id=row["id"], document_id=row["document_id"], status="rolled_back", reason_code=""))
                    continue
                if row["status"] != "applied":
                    # Never applied by this batch (primary conflict), so there is
                    # nothing to undo; keep the original status visible.
                    items.append(OrganizationBatchItem(id=row["id"], document_id=row["document_id"], status=row["status"], reason_code=row["reason_code"]))
                    continue
                current = self.db._conn.execute(
                    "SELECT metadata_version, logical_path, kb_id FROM document_permissions WHERE id=?",
                    (row["document_id"],),
                ).fetchone()
                if current is not None:
                    assert_kb_available_in_tx(self.db._conn, str(current["kb_id"]))
                if current is None or current["metadata_version"] != row["after_metadata_version"]:
                    items.append(OrganizationBatchItem(id=row["id"], document_id=row["document_id"], status="metadata_conflict", reason_code="changed_since"))
                    continue
                if row["before_tags_json"] is None:
                    # Batch recorded before the D-10 payload columns existed.
                    self.db._conn.execute(
                        "UPDATE document_permissions "
                        "SET logical_path=?, metadata_version=? "
                        "WHERE id=? AND metadata_version=?",
                        (row["before_logical_path"], row["before_metadata_version"], row["document_id"], row["after_metadata_version"]),
                    )
                else:
                    self.db._conn.execute(
                        "UPDATE document_permissions "
                        "SET logical_path=?, tags_json=?, document_type=?, metadata_version=? "
                        "WHERE id=? AND metadata_version=?",
                        (
                            row["before_logical_path"],
                            row["before_tags_json"],
                            row["before_document_type"] or "",
                            row["before_metadata_version"],
                            row["document_id"],
                            row["after_metadata_version"],
                        ),
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
