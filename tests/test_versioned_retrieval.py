"""Task 4: version-aware vector payload and historical fallback."""

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from rag.models import Chunk
from rag.user_db import UserDB
from rag.versioned_retrieval import retrieve_current_then_history


def chunk(text, version_id=None, is_current=True, weight=1.0):
    return Chunk(
        text=text,
        doc_name="policy.pdf",
        chunk_index=0,
        document_id=7,
        document_version_id=version_id,
        version_no=version_id,
        is_current=is_current,
        historical_weight=weight,
    )


class FakeVersionRetriever:
    def __init__(self, current, history):
        self.current = current
        self.history = history

    def retrieve(self, query, top_k=5, doc_name=None, tags=None, allowed_version_ids=None, validity_statuses=None, allowed_wiki_revision_ids=None):
        if allowed_version_ids and 2 in allowed_version_ids:
            return list(self.current)[:top_k]
        return list(self.history)[:top_k]


USER = {"id": 1, "username": "alice", "permission_level": 1}
ADMIN = {"id": 99, "username": "root", "permission_level": 0, "is_admin": True}


class _FakeDB:
    def get_accessible_doc_names(self, kb_id, user_id, user_level=1):
        return ["policy.pdf"]

    def get_document_permission(self, name, kb_id):
        return {
            "id": 7,
            "doc_name": name,
            "kb_id": kb_id,
            "owner_id": 1,
            "current_version_id": 2,
        }

    def get_document_permission_by_id(self, document_id):
        return self.get_document_permission("policy.pdf", "rag_docs")

    def list_document_permissions_all(self):
        return [{"id": 7}]

    def list_document_versions(self, document_id):
        return [
            {"id": 2, "status": "current", "validity_status": "valid"},
            {"id": 1, "status": "historical", "validity_status": "valid"},
        ]


DB = _FakeDB()


class _NoVisibleVersionsDB:
    def get_accessible_doc_names(self, kb_id, user_id, user_level=1):
        return []


class _FailIfCalledRetriever:
    def retrieve(self, *_args, **_kwargs):
        raise AssertionError("unrestricted retrieval must not run")


def test_no_visible_versions_returns_empty_without_unrestricted_retrieval():
    result = retrieve_current_then_history(
        _FailIfCalledRetriever(),
        "policy",
        top_k=3,
        user=USER,
        user_db=_NoVisibleVersionsDB(),
        kb_id="rag_docs",
    )

    assert result == []


class _RecordingRetriever:
    def __init__(self):
        self.allowed_version_ids = []

    def retrieve(self, query, top_k=5, doc_name=None, tags=None, allowed_version_ids=None, validity_statuses=None, allowed_wiki_revision_ids=None):
        self.allowed_version_ids.append(allowed_version_ids)
        if allowed_version_ids and 2 in allowed_version_ids:
            return [chunk("current", version_id=2, is_current=True)]
        return [chunk("history", version_id=1, is_current=False)]


def test_admin_uses_current_then_historical_version_scopes():
    """Admins see all legal versions while still preferring the current version."""
    retriever = _RecordingRetriever()

    result = retrieve_current_then_history(
        retriever, "policy", top_k=3, user=ADMIN, user_db=DB, kb_id="rag_docs"
    )

    assert [item.text for item in result] == ["current", "history"]
    assert retriever.allowed_version_ids == [{2}, {1}]


def test_non_admin_without_visible_versions_still_returns_empty():
    retriever = _RecordingRetriever()

    result = retrieve_current_then_history(
        retriever, "policy", top_k=3, user=USER, user_db=_NoVisibleVersionsDB(), kb_id="rag_docs"
    )

    assert result == []
    assert retriever.allowed_version_ids == []


def test_history_only_fills_missing_current_results():
    current = [chunk("current", version_id=2, is_current=True)]
    history = [
        chunk("old-a", version_id=1, is_current=False),
        chunk("old-b", version_id=1, is_current=False),
    ]
    retriever = FakeVersionRetriever(current=current, history=history)

    result = retrieve_current_then_history(
        retriever, "policy", top_k=3, user=USER, user_db=DB, kb_id="rag_docs"
    )

    assert [item.text for item in result] == ["current", "old-a", "old-b"]
    assert result[0].historical_weight == 1.0
    assert result[1].historical_weight == 0.35


def test_sufficient_current_results_do_not_fetch_history():
    current = [
        chunk("a", version_id=2, is_current=True),
        chunk("b", version_id=2, is_current=True),
        chunk("c", version_id=2, is_current=True),
    ]
    history = [chunk("old", version_id=1, is_current=False)]
    retriever = FakeVersionRetriever(current=current, history=history)

    result = retrieve_current_then_history(
        retriever, "policy", top_k=3, user=USER, user_db=DB, kb_id="rag_docs"
    )

    assert [item.text for item in result] == ["a", "b", "c"]
    assert all(item.historical_weight == 1.0 for item in result)


@pytest.fixture
def mock_qdrant(tmp_path, monkeypatch):
    import rag.vector_store as vs

    client = QdrantClient(path=str(tmp_path / "qdrant"))
    client.create_collection(
        collection_name="kb_test",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    monkeypatch.setattr(vs, "_client", client)
    return vs


def test_version_payload_round_trips_through_qdrant(mock_qdrant):
    from rag.vector_store import add_to_collection, search_collection

    original = Chunk(
        text="Leave policy",
        doc_name="policy.md",
        chunk_index=2,
        doc_permission_id=7,
        document_id=7,
        document_version_id=11,
        version_no=3,
        is_current=True,
        validity_status="valid",
        section_title="Annual leave",
        updated_at=1000.0,
    )

    add_to_collection("kb_test", [original], [[0.1, 0.2]])
    result = search_collection(
        "kb_test",
        [0.1, 0.2],
        top_k=1,
        allowed_version_ids={11},
        validity_statuses={"valid"},
    )

    assert result[0] == original


@pytest.fixture
def versioned_retrieval(tmp_path):
    import rag.vector_store as vs

    db = UserDB(str(tmp_path / "users.db"))
    user_id = db.create_user("owner", "Password-123!")
    db._conn.execute(
        "INSERT INTO document_permissions (id, doc_name, kb_id, owner_id) "
        "VALUES (7, 'policy.pdf', 'kb_test', ?)",
        (user_id,),
    )
    db._conn.commit()
    client = QdrantClient(path=str(tmp_path / "qdrant"))
    client.create_collection(
        collection_name="kb_test",
        vectors_config=VectorParams(size=2, distance=Distance.COSINE),
    )
    vs._client = client

    class Fixture:
        def seed_sqlite_current_version(self, document_id, version_id):
            db._conn.execute(
                "INSERT INTO document_versions "
                "(id, document_id, version_no, original_filename, file_path, content_hash, uploaded_by, status, validity_status) "
                "VALUES (?, ?, 1, 'policy.pdf', ?, 'h', ?, 'current', 'valid')",
                (version_id, document_id, str(tmp_path / "policy.pdf"), user_id),
            )
            db._conn.execute(
                "UPDATE document_permissions SET current_version_id = ? WHERE id = ?",
                (version_id, document_id),
            )
            db._conn.commit()

        def seed_vector(self, version_id, is_current):
            from rag.vector_store import add_to_collection

            payload_chunk = Chunk(
                text=f"v{version_id}",
                doc_name="policy.pdf",
                chunk_index=0,
                document_id=7,
                document_version_id=version_id,
                version_no=version_id,
                is_current=is_current,
            )
            add_to_collection(
                "kb_test", [payload_chunk], [[0.1, 0.2]]
            )

        def retrieve_current(self, document_id):
            from rag.vector_store import search_collection

            perm = db.get_document_permission_by_id(document_id)
            return search_collection(
                "kb_test",
                [0.1, 0.2],
                top_k=5,
                allowed_version_ids={perm["current_version_id"]},
                validity_statuses={"valid"},
            )

    return Fixture()


def test_stale_is_current_flags_cannot_change_sqlite_selected_results(versioned_retrieval):
    versioned_retrieval.seed_sqlite_current_version(document_id=7, version_id=11)
    versioned_retrieval.seed_vector(version_id=10, is_current=True)
    versioned_retrieval.seed_vector(version_id=11, is_current=False)

    result = versioned_retrieval.retrieve_current(document_id=7)

    assert [item.document_version_id for item in result] == [11]
