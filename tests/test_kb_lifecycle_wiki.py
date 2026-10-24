"""Wiki indexing and cleanup obey knowledge-base lifecycle admission."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

import rag.bm25_store as bm25_store
import rag.vector_store as vector_store
from rag.api import app
from rag.kb_lifecycle import KBLifecycleError, kb_mutation
from rag.models import Chunk
from rag.user_db import UserDB
from rag.wiki.indexer import WikiIndexer, enqueue_wiki_reindex_job
from rag.wiki.store import WikiStore


@pytest.fixture
def wiki_lifecycle_env(tmp_path, monkeypatch):
    db = UserDB(tmp_path / "users.db")
    db.create_kb_metadata("kb_wiki_delete", "Wiki Delete", 1, "public")
    db.create_kb_metadata("kb_wiki_keep", "Wiki Keep", 1, "public")
    store = WikiStore(tmp_path / "wiki.db")
    vector_store._client = QdrantClient(":memory:")
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    indexer = WikiIndexer(
        wiki_store=store,
        embed_fn=lambda texts: [[0.05] * 1024 for _ in texts],
        user_db=db,
    )
    try:
        yield db, store, indexer
    finally:
        vector_store.close_client()
        store.close()
        db.close()


def _points(kb_id: str):
    client = vector_store._get_client()
    if not client.collection_exists(kb_id):
        return []
    records, _ = client.scroll(collection_name=kb_id, limit=100, with_payload=True)
    return records


def test_indexer_requires_user_db_for_custom_kb_writes(tmp_path, monkeypatch):
    store = WikiStore(tmp_path / "wiki.db")
    vector_store._client = QdrantClient(":memory:")
    page = store.create_page("kb_unmanaged", "Unmanaged", "# content\nCustom KB text.")
    indexer = WikiIndexer(
        wiki_store=store,
        embed_fn=lambda texts: [[0.05] * 1024 for _ in texts],
    )
    try:
        with pytest.raises(KBLifecycleError):
            indexer.sync_page(page.id)
        assert _points("kb_unmanaged") == []
    finally:
        vector_store.close_client()
        store.close()


def test_indexer_factory_binds_the_requested_user_db(tmp_path, monkeypatch):
    import rag.wiki.api as wiki_api

    store = WikiStore(tmp_path / "wiki.db")
    deleting_db = UserDB(tmp_path / "deleting-users.db")
    active_db = UserDB(tmp_path / "active-users.db")
    kb_id = "kb_factory_binding"
    deleting_db.create_kb_metadata(kb_id, "Factory Binding", 1, "public")
    active_db.create_kb_metadata(kb_id, "Factory Binding", 1, "public")
    page = store.create_page(kb_id, "Factory page", "# Factory\nBound database check.")
    import rag.embedder as embedder

    monkeypatch.setattr(embedder, "embed", lambda texts: [[0.05] * 1024 for _ in texts])
    monkeypatch.setattr(wiki_api, "get_wiki_store", lambda: store)
    monkeypatch.setattr(wiki_api, "_wiki_indexer", None)
    vector_store._client = QdrantClient(":memory:")
    try:
        with kb_mutation(deleting_db, kb_id, mode="delete") as permit:
            permit.begin_delete()

        deleting_indexer = wiki_api.get_wiki_indexer(deleting_db)
        with pytest.raises(KBLifecycleError):
            deleting_indexer.sync_page(page.id)

        active_indexer = wiki_api.get_wiki_indexer(active_db)
        assert active_indexer is not deleting_indexer
        assert active_indexer.sync_page(page.id) == 1
    finally:
        vector_store.close_client()
        store.close()
        deleting_db.close()
        active_db.close()


def test_wiki_api_mutations_fail_before_wiki_write_when_kb_is_busy(
    tmp_path, monkeypatch
):
    db = app.state.user_db
    kb_id = "kb_wiki_busy"
    db.create_kb_metadata(kb_id, "Busy Wiki", 1, "public")
    store = WikiStore(tmp_path / "busy-wiki.db")
    vector_store._client = QdrantClient(":memory:")
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", str(tmp_path / "busy-bm25.db"))
    indexer = WikiIndexer(
        wiki_store=store,
        user_db=db,
        embed_fn=lambda texts: [[0.05] * 1024 for _ in texts],
    )
    import rag.wiki.api as wiki_api

    monkeypatch.setattr(wiki_api, "get_wiki_store", lambda: store)
    monkeypatch.setattr(wiki_api, "_wiki_indexer", indexer)
    client = TestClient(app)
    try:
        page = store.create_page(kb_id, "Existing", "# Original\nOriginal revision.")
        indexer.sync_page(page.id)
        point_count = len(_points(kb_id))

        with kb_mutation(db, kb_id):
            create = client.post(
                "/wiki/pages",
                json={"kb_id": kb_id, "title": "Blocked create", "content": "No write."},
            )
            update = client.put(
                f"/wiki/pages/{page.id}",
                json={"content": "Blocked update."},
            )
            rollback = client.post(
                f"/wiki/pages/{page.id}/rollback",
                json={"revision_num": 1},
            )
            delete = client.delete(f"/wiki/pages/{page.id}")

        assert [response.status_code for response in (create, update, rollback, delete)] == [
            409,
            409,
            409,
            409,
        ]
        assert store.list_pages(kb_id=kb_id) == [page]
        assert len(store.get_revisions(page.id)) == 1
        assert len(_points(kb_id)) == point_count
    finally:
        vector_store.close_client()
        store.close()


def test_remove_kb_content_cancels_retries_and_preserves_other_kb_and_regular_points(
    wiki_lifecycle_env,
):
    db, store, indexer = wiki_lifecycle_env
    doomed = store.create_page(
        "kb_wiki_delete", "Original title", "# Original\nFirst revision content [[Neighbor]]."
    )
    store.update_page(doomed.id, title="Renamed title", content="# Latest\nLatest wiki content.")
    neighbor = store.create_page("kb_wiki_delete", "Neighbor", "# Neighbor\nKeep this page.")
    retained = store.create_page("kb_wiki_keep", "Retained", "# Retained\nOther KB content.")
    for page in (doomed, neighbor, retained):
        indexer.sync_page(page.id)

    # Ordinary document point shares the KB collection but must survive the Wiki sweep.
    vector_store._ensure_collection_name("kb_wiki_delete")
    vector_store.add_to_collection(
        collection_name="kb_wiki_delete",
        chunks=[Chunk(text="ordinary document", doc_name="Original title", chunk_index=0)],
        embeddings=[[0.2] * 1024],
        document_id=9001,
        document_version_id=1,
    )

    assert enqueue_wiki_reindex_job(
        db, doomed.id, doomed.current_revision_id, kb_id="kb_wiki_delete"
    )
    assert enqueue_wiki_reindex_job(
        db, neighbor.id, neighbor.current_revision_id, kb_id="kb_wiki_delete"
    )
    db.enqueue_latest_index_job(doomed.id, 1, "material-hash", 0)

    with kb_mutation(db, "kb_wiki_delete", mode="delete"):
        indexer.remove_kb_content("kb_wiki_delete", db=db)

    assert store.list_pages(kb_id="kb_wiki_delete") == []
    assert store.get_page(doomed.id) is None
    assert store.get_page(neighbor.id) is None
    assert [page.id for page in store.list_pages(kb_id="kb_wiki_keep")] == [retained.id]
    with store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM wiki_revisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM wiki_links").fetchone()[0] == 0

    remaining = _points("kb_wiki_delete")
    assert len(remaining) == 1
    assert remaining[0].payload["document_id"] == 9001
    assert remaining[0].payload.get("chunk_kind") != "wiki"
    assert len(_points("kb_wiki_keep")) == 1

    jobs = db._conn.execute(
        "SELECT document_id, status FROM document_index_jobs "
        "WHERE job_scope = 'wiki' AND document_id IN (?, ?) ORDER BY document_id",
        (doomed.id, neighbor.id),
    ).fetchall()
    assert [(int(row["document_id"]), row["status"]) for row in jobs] == [
        (doomed.id, "cancelled"),
        (neighbor.id, "cancelled"),
    ]
    material_job = db._conn.execute(
        "SELECT status FROM document_index_jobs WHERE document_id = ? AND job_scope = 'material'",
        (doomed.id,),
    ).fetchone()
    assert material_job["status"] == "queued"


def test_remove_kb_content_keeps_wiki_identity_when_vector_cleanup_fails(
    wiki_lifecycle_env, monkeypatch
):
    db, store, indexer = wiki_lifecycle_env
    page = store.create_page("kb_wiki_delete", "Retry cleanup", "# Keep\nIdentity until retry.")
    indexer.sync_page(page.id)
    client = vector_store._get_client()
    original_delete = client.delete

    def fail_delete(*args, **kwargs):
        raise RuntimeError("temporary qdrant failure")

    monkeypatch.setattr(client, "delete", fail_delete)
    with kb_mutation(db, "kb_wiki_delete", mode="delete"), pytest.raises(
        RuntimeError, match="temporary qdrant failure"
    ):
        indexer.remove_kb_content("kb_wiki_delete", db=db)

    assert store.get_page(page.id) is not None
    assert len(store.get_revisions(page.id)) == 1
    assert len(_points("kb_wiki_delete")) == 1
    monkeypatch.setattr(client, "delete", original_delete)


def test_retry_enqueue_refuses_a_kb_tombstone(wiki_lifecycle_env):
    db, store, _indexer = wiki_lifecycle_env
    page = store.create_page("kb_wiki_delete", "Tombstone", "# Retry\nMust not queue.")
    with kb_mutation(db, "kb_wiki_delete", mode="delete") as permit:
        permit.begin_delete()
        assert not enqueue_wiki_reindex_job(
            db, page.id, page.current_revision_id, kb_id="kb_wiki_delete"
        )

    count = db._conn.execute(
        "SELECT COUNT(*) FROM document_index_jobs WHERE job_scope = 'wiki' AND document_id = ?",
        (page.id,),
    ).fetchone()[0]
    assert count == 0


def test_kb_wiki_cleanup_only_targets_wiki_chunk_kind(wiki_lifecycle_env):
    db, store, indexer = wiki_lifecycle_env
    page = store.create_page("kb_wiki_delete", "Shared", "# Wiki\nWiki payload.")
    indexer.sync_page(page.id)
    vector_store._ensure_collection_name("kb_wiki_delete")
    regular_id = "00000000-0000-4000-8000-000000000009"
    vector_store._get_client().upsert(
        collection_name="kb_wiki_delete",
        points=[
            PointStruct(
                id=regular_id,
                vector=[0.2] * 1024,
                payload={
                    "doc_name": page.title,
                    "document_id": 9002,
                    "chunk_index": 0,
                    "text": "ordinary payload",
                    "chunk_kind": "text",
                },
            )
        ],
    )

    with kb_mutation(db, "kb_wiki_delete", mode="delete"):
        indexer.remove_kb_content("kb_wiki_delete", db=db)

    assert [point.id for point in _points("kb_wiki_delete")] == [regular_id]
