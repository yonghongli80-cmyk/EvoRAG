"""Tests for ACL-01: Wiki chunk permission passthrough in filter_chunks_by_permission."""

from __future__ import annotations

import pytest

from rag.models import Chunk
from rag.org_auth.repository import OrgRepository
from rag.permissions import filter_chunks_by_permission
from rag.user_db import UserDB


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test_acl.db")
    udb = UserDB(path)
    yield udb
    udb.close()


def _make_user(db: UserDB, username: str, is_admin: bool = False) -> dict:
    uid = db.create_user(username, "pwd123")
    if is_admin:
        db.set_user_admin(uid, True)
    return db.get_user_by_id(uid)


def test_filter_chunks_allows_wiki_when_user_has_kb_view(db):
    """ACL-01: Non-admin users with KB view access must retain wiki chunks."""
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")

    kb_id = "kb_wiki_shared"
    # Create KB with public scope so viewer has view rights
    db.create_kb_metadata(kb_id=kb_id, name="Test KB", owner_id=owner["id"], scope="public")

    # Regular private doc belonging to alice
    db.create_document_permission("alice_private.pdf", kb_id, owner["id"], is_public=False)

    wiki_chunk = Chunk(
        text="Wiki content on caching strategies",
        doc_name="Caching Strategies",
        chunk_index=0,
        chunk_kind="wiki",
        document_id=1,
        document_version_id=1,
    )
    private_file_chunk = Chunk(
        text="Alice private financial data",
        doc_name="alice_private.pdf",
        chunk_index=0,
        document_id=2,
        document_version_id=2,
    )

    result = filter_chunks_by_permission(
        db,
        kb_id,
        [wiki_chunk, private_file_chunk],
        viewer,
    )

    # Bob has KB view access, so wiki_chunk must be retained, but alice_private.pdf must be dropped!
    doc_names = [c.doc_name for c in result]
    assert "Caching Strategies" in doc_names
    assert "alice_private.pdf" not in doc_names
    assert len(result) == 1


def test_filter_chunks_denies_wiki_when_user_lacks_kb_view(db):
    """ACL-01: Non-admin users without KB view access must drop wiki chunks."""
    owner = _make_user(db, "alice")
    outsider = _make_user(db, "charlie")

    kb_id = "kb_wiki_secret"
    # Private KB owned by alice, not shared with charlie
    db.create_kb_metadata(kb_id=kb_id, name="Secret KB", owner_id=owner["id"], scope="private")

    wiki_chunk = Chunk(
        text="Secret corporate roadmap",
        doc_name="Secret Roadmap",
        chunk_index=0,
        chunk_kind="wiki",
        document_id=10,
        document_version_id=10,
    )

    result = filter_chunks_by_permission(
        db,
        kb_id,
        [wiki_chunk],
        outsider,
    )

    # Charlie has no KB access, so wiki chunk must be filtered out
    assert result == []


def test_filter_chunks_allows_wiki_in_org_policy_kb(db):
    """ACL-01: Users with org authorization on an org policy KB must retain wiki chunks."""
    member_id = db.create_user("member", "Password1")
    member = db.get_user_by_id(member_id)

    repo = OrgRepository(db)
    repo.create_unit("ENG", None, "Engineering", "team")
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P_ENG',?,'工程师','Tech')", (member_id,))
        conn.execute("INSERT INTO org_memberships VALUES('P_ENG','ENG',1)")
        conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('kb_eng','ENG','organization')")

    wiki_chunk = Chunk(
        text="Engineering onboarding guide",
        doc_name="Onboarding Guide",
        chunk_index=0,
        chunk_kind="wiki",
        document_id=55,
        document_version_id=55,
    )

    result = filter_chunks_by_permission(
        db,
        "kb_eng",
        [wiki_chunk],
        member,
    )

    assert len(result) == 1
    assert result[0].doc_name == "Onboarding Guide"
