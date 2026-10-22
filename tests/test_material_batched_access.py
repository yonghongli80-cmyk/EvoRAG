"""D observation: locator/organizer resolve access per KB, not per document."""

from __future__ import annotations

import pytest

from rag.materials.locator import MaterialLocator, _visible_documents
from rag.materials.models import MaterialLocationQuery
from rag.materials.organizer import _default_candidate_docs
from rag.user_db import UserDB


class _Fixture:
    def __init__(self, tmp_path):
        db = UserDB(str(tmp_path / "batch.db"))
        db.create_material_schema()
        self.db = db
        self.alice_id = db.create_user("batch-alice", "Password-123!")
        self.bob_id = db.create_user("batch-bob", "Password-123!")
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO org_units(code,parent_code,name,kind) "
                "VALUES('TEAM',NULL,'团队','team')"
            )
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES('P-BOB',?,'乙','专员')",
                (self.bob_id,),
            )
            conn.execute("INSERT INTO org_memberships VALUES('P-BOB','TEAM',1)")
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
                "VALUES('kb-team','TEAM','organization')"
            )
        self.private_doc = self._seed("private.pdf", owner=self.alice_id)
        self.public_doc = self._seed(
            "public.pdf", owner=self.alice_id, scope="public"
        )
        self.shared_doc = self._seed(
            "shared.pdf", owner=self.alice_id, scope="shared"
        )
        db.share_document(self.shared_doc, self.bob_id, self.alice_id, "view")
        self.shared_edit_doc = self._seed(
            "shared-edit.pdf", owner=self.alice_id, scope="shared"
        )
        db.share_document(self.shared_edit_doc, self.bob_id, self.alice_id, "edit")
        self.team_doc = self._seed(
            "team.pdf", owner=self.alice_id, kb_id="kb-team"
        )
        self.bob_doc = self._seed("bob.pdf", owner=self.bob_id)
        self.deleted_doc = self._seed("deleted.pdf", owner=self.alice_id)
        with db._lock:
            db._conn.execute(
                "UPDATE document_permissions SET deleted_at=1 WHERE id=?",
                (self.deleted_doc,),
            )
            db._conn.commit()
        # A staging-only document has no current version and must not surface.
        self.versionless_doc = db.create_document_permission(
            "versionless.pdf", "rag_docs", self.alice_id
        )

    def _seed(self, filename, owner, kb_id="rag_docs", scope="private"):
        document_id = self.db.create_document_permission(
            filename, kb_id, owner, scope=scope
        )
        version_id = self.db.create_document_version(
            document_id, filename, f"/tmp/{filename}", "hash", owner
        )
        self.db.activate_document_version(document_id, version_id, kb_id, filename)
        self.db.insert_material_search_fts(
            document_id, version_id, filename, "", "", "[]", "合同正文"
        )
        return document_id

    def user(self, user_id):
        return self.db.get_user_by_id(user_id)

    def close(self):
        self.db.close()


@pytest.fixture()
def batch_context(tmp_path):
    fixture = _Fixture(tmp_path)
    yield fixture
    fixture.close()


def test_visible_documents_match_the_per_document_acl(batch_context):
    f = batch_context

    bob_visible = _visible_documents(f.db, f.user(f.bob_id))

    assert set(bob_visible) == {
        f.public_doc,
        f.shared_doc,
        f.shared_edit_doc,
        f.bob_doc,
        f.team_doc,
    }
    assert bob_visible[f.public_doc]["current_filename"] == "public.pdf"
    assert bob_visible[f.public_doc]["current_version_no"] == 1

    # The organization KB needs TEAM membership, which Alice does not have.
    alice_visible = _visible_documents(f.db, f.user(f.alice_id))
    assert set(alice_visible) == {
        f.private_doc,
        f.public_doc,
        f.shared_doc,
        f.shared_edit_doc,
    }
    assert f.deleted_doc not in alice_visible
    assert f.versionless_doc not in alice_visible


def test_admin_visibility_includes_every_live_document(batch_context):
    f = batch_context
    admin_id = f.db.create_user("batch-admin", "Password-123!")
    f.db.set_user_admin(admin_id, True)

    visible = _visible_documents(f.db, f.user(admin_id))

    assert {f.private_doc, f.public_doc, f.team_doc, f.bob_doc} <= set(visible)
    assert f.deleted_doc not in visible
    assert f.versionless_doc not in visible


def test_localization_does_not_lookup_documents_one_by_one(
    batch_context, monkeypatch
):
    f = batch_context

    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("per-document lookup reintroduced the N+1")

    monkeypatch.setattr(f.db, "get_document_permission_by_id", _boom)
    monkeypatch.setattr(f.db, "get_current_document_version", _boom)

    results = MaterialLocator(f.db).locate(
        MaterialLocationQuery(text="合同正文"), f.user(f.bob_id)
    )

    # Public/shared/own/team documents are visible through the batch query, and
    # the FTS join resolved their current version without extra lookups.
    assert {result.document_id for result in results} == {
        f.public_doc,
        f.shared_doc,
        f.shared_edit_doc,
        f.bob_doc,
        f.team_doc,
    }


def test_organizer_candidates_use_batched_edit_acl(batch_context, monkeypatch):
    f = batch_context

    def _boom(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("per-document lookup reintroduced the N+1")

    monkeypatch.setattr(f.db, "get_document_permission_by_id", _boom)
    monkeypatch.setattr(f.db, "get_current_document_version", _boom)

    bob_candidates = _default_candidate_docs(f.db, f.user(f.bob_id))
    # Public/shared-view documents are not editable; shared-edit and own are.
    assert set(bob_candidates) == {f.shared_edit_doc, f.bob_doc}

    alice_candidates = _default_candidate_docs(f.db, f.user(f.alice_id))
    # The organization KB requires MANAGE, which a plain member lacks.
    assert f.team_doc not in alice_candidates
    assert {f.private_doc, f.public_doc, f.shared_doc} <= set(alice_candidates)
    assert f.deleted_doc not in alice_candidates
    assert f.versionless_doc not in alice_candidates
