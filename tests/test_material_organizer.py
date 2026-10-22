"""Task 9: typed organization preview, confirmation, and rollback."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from rag.materials.organizer import (
    OrganizationInstruction,
    OrganizationProposalModel,
    OrganizationService,
)
from rag.user_db import UserDB


class OrganizationFixture:
    def __init__(self, tmp_path):
        db = UserDB(str(tmp_path / "users.db"))
        db.create_material_schema()
        self.db = db
        self.alice_id = db.create_user("alice", "Password-123!")
        self.hr_id = db.create_user("hr", "Password-123!")
        self.model = mock.MagicMock()
        self.service = OrganizationService(db, generate=self._generate)
        self.contract_id = self.seed("合同.pdf", owner="alice", logical_path="未整理")
        self.model.return_value = {
            "items": [
                {
                    "document_id": self.contract_id,
                    "target_path": "入职材料",
                    "document_type": "onboarding",
                    "confidence": 0.9,
                    "reason": "整理合同",
                }
            ]
        }
        self.last_preview = None

    def _generate(self, messages, model):
        return OrganizationProposalModel.model_validate(self.model.return_value or {"items": []})

    def seed(
        self,
        filename,
        owner="alice",
        logical_path="",
        document_type="",
        tags_json="[]",
    ):
        owner_id = self.alice_id if owner == "alice" else self.hr_id
        doc_id = self.db.create_document_permission(filename, "kb-1", owner_id, 1, False)
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE document_permissions SET logical_path=?, document_type=?, tags_json=? WHERE id=?",
                (logical_path, document_type, tags_json, doc_id),
            )
            self.db._conn.commit()
        version_id = self.db.create_document_version(
            doc_id, filename, f"/tmp/{filename}", "h", owner_id
        )
        self.db.activate_document_version(doc_id, version_id, "kb-1", filename)
        return doc_id

    def alice(self):
        return {"id": self.alice_id, "username": "alice", "is_admin": False}

    def preview(self, text, user=None):
        user = user or self.alice()
        self.last_preview = self.service.preview(OrganizationInstruction(text=text), user)
        return self.last_preview

    def confirm(self, proposal_id, key, user=None):
        user = user or self.alice()
        selected = tuple(item.id for item in self.last_preview.executable_items)
        return self.service.confirm(proposal_id, selected, user, None, key)

    def confirm_preview(self, target_path):
        self.model.return_value = {
            "items": [
                {
                    "document_id": self.contract_id,
                    "target_path": target_path,
                    "confidence": 0.9,
                    "reason": "整理",
                }
            ]
        }
        proposal = self.service.preview(OrganizationInstruction(text="整理"), self.alice())
        selected = tuple(item.id for item in proposal.executable_items)
        batch = self.service.confirm(proposal.id, selected, self.alice(), None, "org-1")
        return SimpleNamespace(id=batch.batch_id, document_id=self.contract_id, items=batch.items)

    def manual_move(self, document_id, path):
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE document_permissions SET logical_path=?, metadata_version=metadata_version+1 "
                "WHERE id=?",
                (path, document_id),
            )
            self.db._conn.commit()

    def revoke_edit(self, document_id, username):
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE document_permissions SET owner_id=? WHERE id=?",
                (self.hr_id, document_id),
            )
            self.db._conn.commit()

    def rollback(self, batch_id, key):
        return self.service.rollback(batch_id, self.alice(), key)

    def path(self, document_id):
        return self.db.get_document_permission_by_id(document_id)["logical_path"]

    def snapshot(self):
        rows = self.db._conn.execute(
            "SELECT id, logical_path, document_type, metadata_version, tags_json "
            "FROM document_permissions"
        ).fetchall()
        return {
            r["id"]: (r["logical_path"], r["document_type"], r["metadata_version"], r["tags_json"])
            for r in rows
        }


@pytest.fixture
def organizer_fixture(tmp_path):
    return OrganizationFixture(tmp_path)


def test_model_cannot_add_file_outside_backend_candidates(organizer_fixture):
    f = organizer_fixture
    f.model.return_value = {"items": [{"document_id": 999, "target_path": "我的人事资料"}]}
    preview = f.preview("把合同放入入职材料", user=f.alice())
    assert preview.executable_items == ()
    assert preview.rejected_items[0].reason_code == "unknown_candidate"


def test_preview_does_not_change_metadata(organizer_fixture):
    f = organizer_fixture
    before = f.snapshot()
    f.preview("把今年合同放到入职材料", user=f.alice())
    assert f.snapshot() == before


def test_confirmation_is_idempotent_and_rechecks_acl(organizer_fixture):
    f = organizer_fixture
    proposal = f.preview("整理合同", user=f.alice())
    first = f.confirm(proposal.id, key="org-1")
    second = f.confirm(proposal.id, key="org-1")
    assert first.batch_id == second.batch_id
    f.revoke_edit(proposal.items[0].document_id, "alice")
    conflict = f.confirm(proposal.id, key="org-2")
    assert conflict.items[0].status == "permission_conflict"


def test_rollback_does_not_overwrite_later_manual_change(organizer_fixture):
    f = organizer_fixture
    batch = f.confirm_preview(target_path="入职材料")
    f.manual_move(batch.document_id, "已归档")
    result = f.rollback(batch.id, key="undo-1")
    assert result.items[0].status == "metadata_conflict"
    assert f.path(batch.document_id) == "已归档"


def test_confirmed_move_then_rollback_restores_previous_path(organizer_fixture):
    f = organizer_fixture
    batch = f.confirm_preview(target_path="入职材料")
    assert f.path(batch.document_id) == "入职材料"
    result = f.rollback(batch.id, key="undo-1")
    assert result.items[0].status == "rolled_back"
    assert f.path(batch.document_id) == "未整理"


# ---------------------------------------------------------------------------
# D-10: rollback restores tags/document_type and conflicts report real status
# ---------------------------------------------------------------------------


def _proposal_for(f, items):
    f.model.return_value = {"items": items}
    proposal = f.preview("整理", user=f.alice())
    selected = tuple(item.id for item in proposal.executable_items)
    return proposal, selected


def test_rollback_restores_tags_and_document_type(organizer_fixture):
    f = organizer_fixture
    doc_id = f.seed(
        "tech-stack.pdf",
        owner="alice",
        logical_path="未整理",
        document_type="contract",
        tags_json='["hr", "2026"]',
    )
    proposal, selected = _proposal_for(
        f,
        [
            {
                "document_id": doc_id,
                "target_path": "技术/架构",
                "document_type": "reference",
                "add_tags": ["tech"],
                "remove_tags": ["2026"],
                "confidence": 0.9,
                "reason": "整理",
            }
        ],
    )
    batch = f.service.confirm(proposal.id, selected, f.alice(), None, "org-tags")
    after = f.db.get_document_permission_by_id(doc_id)
    assert batch.status == "applied"
    assert after["logical_path"] == "技术/架构"
    assert after["document_type"] == "reference"
    assert after["tags_json"] == '["hr", "tech"]'

    result = f.rollback(batch.batch_id, key="undo-tags")
    restored = f.db.get_document_permission_by_id(doc_id)
    assert result.status == "rolled_back"
    assert restored["logical_path"] == "未整理"
    assert restored["document_type"] == "contract"
    assert restored["tags_json"] == '["hr", "2026"]'


def test_batch_conflict_returns_the_real_status(organizer_fixture):
    f = organizer_fixture
    proposal, selected = _proposal_for(
        f,
        [
            {
                "document_id": f.contract_id,
                "target_path": "入职材料",
                "confidence": 0.9,
            }
        ],
    )
    # Revoking edit access between preview and confirm makes the item conflict.
    f.revoke_edit(f.contract_id, "alice")

    result = f.service.confirm(proposal.id, selected, f.alice(), None, "org-conflict")

    assert result.status == "conflict"
    assert result.items[0].status == "permission_conflict"
    assert f.db.get_organization_batch_by_id(result.batch_id)["status"] == "conflict"
    assert f.path(f.contract_id) == "未整理"


def test_partial_batch_reports_partial_and_rolls_back_only_applied_items(
    organizer_fixture,
):
    f = organizer_fixture
    second_id = f.seed("员工手册.pdf", owner="alice", logical_path="未整理")
    proposal, selected = _proposal_for(
        f,
        [
            {
                "document_id": f.contract_id,
                "target_path": "入职材料",
                "document_type": "onboarding",
                "confidence": 0.9,
            },
            {
                "document_id": second_id,
                "target_path": "制度",
                "confidence": 0.9,
            },
        ],
    )
    f.revoke_edit(second_id, "alice")

    result = f.service.confirm(proposal.id, selected, f.alice(), None, "org-partial")

    assert result.status == "partial"
    statuses = {item.document_id: item.status for item in result.items}
    assert statuses == {f.contract_id: "applied", second_id: "permission_conflict"}
    assert f.db.get_organization_batch_by_id(result.batch_id)["status"] == "partial"
    assert f.path(f.contract_id) == "入职材料"

    rollback = f.rollback(result.batch_id, key="undo-partial")
    rollback_statuses = {item.document_id: item.status for item in rollback.items}
    # The conflicting item was never applied, so it is not "rolled back".
    assert rollback_statuses == {
        f.contract_id: "rolled_back",
        second_id: "permission_conflict",
    }
    assert f.path(f.contract_id) == "未整理"
    assert f.path(second_id) == "未整理"
