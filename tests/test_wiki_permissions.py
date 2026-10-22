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


def test_filter_chunks_allows_wiki_for_the_legacy_kb_owner(db):
    """ACL-01/B-02: the legacy owner keeps KB view; wiki chunks follow it while
    file chunks still need the personal document ACL."""
    owner = _make_user(db, "alice")
    other = _make_user(db, "carol")

    kb_id = "kb_wiki_shared"
    db.create_kb_metadata(kb_id=kb_id, name="Test KB", owner_id=owner["id"], scope="public")

    # Regular private docs: one owned by the viewer, one owned by somebody else.
    db.create_document_permission("alice_private.pdf", kb_id, owner["id"], is_public=False)
    db.create_document_permission("carol_private.pdf", kb_id, other["id"], is_public=False)

    wiki_chunk = Chunk(
        text="Wiki content on caching strategies",
        doc_name="Caching Strategies",
        chunk_index=0,
        chunk_kind="wiki",
        document_id=1,
        document_version_id=1,
    )
    own_file_chunk = Chunk(
        text="Alice private financial data",
        doc_name="alice_private.pdf",
        chunk_index=0,
        document_id=2,
        document_version_id=2,
    )
    other_file_chunk = Chunk(
        text="Carol private roadmap",
        doc_name="carol_private.pdf",
        chunk_index=0,
        document_id=3,
        document_version_id=3,
    )

    result = filter_chunks_by_permission(
        db,
        kb_id,
        [wiki_chunk, own_file_chunk, other_file_chunk],
        owner,
    )

    doc_names = [c.doc_name for c in result]
    assert "Caching Strategies" in doc_names
    assert "alice_private.pdf" in doc_names
    assert "carol_private.pdf" not in doc_names
    assert len(result) == 2


def test_filter_chunks_denies_wiki_on_a_public_legacy_kb_for_non_owners(db):
    """B-02: ``scope='public'`` no longer grants KB view to another account."""
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")

    kb_id = "kb_wiki_public"
    db.create_kb_metadata(kb_id=kb_id, name="Public KB", owner_id=owner["id"], scope="public")
    wiki_chunk = Chunk(
        text="Public wiki content",
        doc_name="Public Wiki",
        chunk_index=0,
        chunk_kind="wiki",
        document_id=7,
        document_version_id=7,
    )

    assert filter_chunks_by_permission(db, kb_id, [wiki_chunk], viewer) == []


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
