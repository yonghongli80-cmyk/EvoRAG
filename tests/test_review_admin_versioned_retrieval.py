from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from rag.user_db import UserDB
from rag.versioned_retrieval import _visible_version_scopes, retrieve_current_then_history

ADMIN = {"id": 900, "username": "root", "permission_level": 0, "is_admin": True}


@dataclass
class Candidate:
    text: str
    document_version_id: int
    validity_status: str = "valid"
    chunk_kind: str = "document"
    score: float = 0.0
    historical_weight: float = 1.0
    is_current: bool = True


class ScopedRetriever:
    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = []

    def retrieve(
        self, query, top_k=5, doc_name=None, tags=None, weights=None,
        allowed_version_ids=None, validity_statuses=None, allowed_wiki_revision_ids=None,
    ):
        self.calls.append((allowed_version_ids, allowed_wiki_revision_ids, validity_statuses))
        matches = []
        for candidate in self.candidates:
            allowed = allowed_wiki_revision_ids if candidate.chunk_kind == "wiki" else allowed_version_ids
            if allowed is not None and candidate.document_version_id not in allowed:
                continue
            if validity_statuses and candidate.validity_status not in validity_statuses:
                continue
            matches.append(candidate)
        return sorted(matches, key=lambda item: item.score, reverse=True)[:top_k]


class WikiCurrentRevision:
    def list_pages(self, kb_id=None, limit=10000):
        return [SimpleNamespace(current_revision_id=3)]


def _insert_version(db, document_id, version_id, version_no, status, user_id):
    db._conn.execute(
        "INSERT INTO document_versions "
        "(id, document_id, version_no, original_filename, file_path, content_hash, uploaded_by, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (version_id, document_id, version_no, "policy.pdf", f"/tmp/{version_id}", f"h{version_id}", user_id, status),
    )


@pytest.fixture
def version_db(tmp_path):
    db = UserDB(str(tmp_path / "versioned-users.sqlite3"))
    owner_id = db.create_user("document-owner", "Password-123!")
    other_id = db.create_user("other-owner", "Password-456!")
    active = db.create_document_permission("active.pdf", "kb_test", owner_id)
    other_kb = db.create_document_permission("elsewhere.pdf", "other_kb", owner_id)
    recycled = db.create_document_permission("recycled.pdf", "kb_test", owner_id)
    purged = db.create_document_permission("purged.pdf", "kb_test", owner_id)
    other_owner_doc = db.create_document_permission("private.pdf", "kb_test", other_id)

    for version_id, number, status in [
        (1, 1, "historical"), (2, 2, "current"), (3, 3, "staging"),
        (4, 4, "indexing"), (5, 5, "failed"), (6, 6, "recycle_pending"),
        (7, 7, "purged"),
    ]:
        _insert_version(db, active, version_id, number, status, owner_id)
    db._conn.execute("UPDATE document_permissions SET current_version_id = 2 WHERE id = ?", (active,))
    db._conn.execute(
        "UPDATE document_versions SET validity_status = 'confirmed_outdated' WHERE id IN (3, 5)"
    )

    _insert_version(db, other_kb, 20, 1, "current", owner_id)
    db._conn.execute("UPDATE document_permissions SET current_version_id = 20 WHERE id = ?", (other_kb,))

    _insert_version(db, recycled, 30, 1, "recycle_pending", owner_id)
    db._conn.execute(
        "UPDATE document_permissions SET current_version_id = 30, deleted_at = 123 WHERE id = ?",
        (recycled,),
    )

    _insert_version(db, purged, 40, 1, "purged", owner_id)
    db._conn.execute(
        "UPDATE document_permissions SET purge_state = 'purged_by_retention_policy' WHERE id = ?",
        (purged,),
    )

    _insert_version(db, other_owner_doc, 19, 1, "historical", other_id)
    _insert_version(db, other_owner_doc, 21, 2, "current", other_id)
    db._conn.execute(
        "UPDATE document_permissions SET current_version_id = 21 WHERE id = ?",
        (other_owner_doc,),
    )
    db._conn.commit()
    yield db, owner_id
    db.close()


def test_admin_scopes_include_all_live_kb_documents_but_only_retrievable_versions(version_db):
    db, _owner_id = version_db

    current, historical = _visible_version_scopes(
        db, ADMIN, "kb_test", wiki_store=WikiCurrentRevision()
    )

    assert current.documents == {2, 21}
    assert historical.documents == {1, 19}
    assert current.wiki == {3}
    assert historical.wiki == set()
    assert not ({3, 4, 5, 6, 7, 20, 30, 40} & (current.documents | historical.documents))


def test_admin_retrieval_uses_current_scope_before_high_scoring_history(version_db):
    db, _owner_id = version_db
    retriever = ScopedRetriever([
        Candidate("old v1", 1, score=1.0),
        Candidate("current v2", 2, score=0.1),
        Candidate("staging", 3, score=10.0),
        Candidate("failed", 5, validity_status="valid", score=100.0),
        Candidate("recycled", 30, score=100.0),
    ])

    result = retrieve_current_then_history(
        retriever, "policy", top_k=1, user=ADMIN, user_db=db, kb_id="kb_test"
    )

    assert [item.text for item in result] == ["current v2"]
    assert result[0].historical_weight == 1.0
    assert retriever.calls == [({2, 21}, set(), None)]


def test_sqlite_current_scope_normalizes_stale_vector_flags(version_db):
    db, _owner_id = version_db
    retriever = ScopedRetriever([
        Candidate(
            "current with stale vector flags", 2,
            validity_status="confirmed_outdated", is_current=False, score=1.0,
        ),
    ])

    result = retrieve_current_then_history(
        retriever, "policy", top_k=1, user=ADMIN, user_db=db, kb_id="kb_test"
    )

    assert len(result) == 1
    assert result[0].is_current is True
    assert result[0].validity_status == "valid"


def test_non_admin_still_sees_only_acl_documents_and_their_history(version_db):
    db, owner_id = version_db
    user = {"id": owner_id, "username": "document-owner", "permission_level": 1}

    current, historical = _visible_version_scopes(db, user, "kb_test")

    assert current.documents == {2}
    assert historical.documents == {1}


@pytest.mark.parametrize("is_admin", [False, True])
def test_confirmed_outdated_current_pointer_is_only_retrievable_as_history(version_db, is_admin):
    db, owner_id = version_db
    db.set_version_validity(2, "confirmed_outdated")
    user = ADMIN if is_admin else {"id": owner_id, "username": "document-owner", "permission_level": 1}
    current, historical = _visible_version_scopes(db, user, "kb_test")
    retriever = ScopedRetriever([
        Candidate("stale vector metadata for v2", 2, validity_status="valid", score=0.1),
        Candidate("old v1", 1, score=1.0),
        Candidate("staging marked outdated", 3, validity_status="confirmed_outdated", score=10.0),
        Candidate("failed marked outdated", 5, validity_status="confirmed_outdated", score=100.0),
    ])

    result = retrieve_current_then_history(
        retriever, "policy", top_k=3, user=user, user_db=db, kb_id="kb_test"
    )

    assert 2 not in current.documents
    assert 2 in historical.documents
    assert {3, 5}.isdisjoint(current.documents | historical.documents)
    stale = next(item for item in result if item.text == "stale vector metadata for v2")
    assert stale.historical_weight < 1.0
    assert stale.is_current is False
    assert stale.validity_status == "confirmed_outdated"
    assert all(item.text not in {"staging marked outdated", "failed marked outdated"} for item in result)
