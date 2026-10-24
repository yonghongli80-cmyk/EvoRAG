"""Wiki vectors must remain scoped to page identity and current revision."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

import rag.vector_store as vector_store
from rag.kb_lifecycle import KBBusy
from rag.user_db import UserDB
from rag.wiki.chunker import MarkdownASTChunker
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore

KB_ID = "kb_wiki_identity_review"
EMBED_DIMENSION = 1024


@pytest.fixture
def wiki_index_harness(tmp_path, monkeypatch):
    vector_store._client = QdrantClient(":memory:")
    monkeypatch.setattr(vector_store.settings, "bm25_db_path", str(tmp_path / "bm25.db"))
    store = WikiStore(db_path=tmp_path / "wiki.db")
    user_db = UserDB(tmp_path / "users.db")
    store.lifecycle_db = user_db
    indexer = WikiIndexer(
        wiki_store=store,
        chunker=MarkdownASTChunker(max_chunk_chars=300, min_chunk_chars=30),
        embed_fn=lambda texts: [[0.05] * EMBED_DIMENSION for _ in texts],
        user_db=user_db,
    )
    try:
        yield store, indexer
    finally:
        vector_store.close_client()
        store.close()
        user_db.close()


def _content(label: str) -> str:
    return f"# {label}\n\nThis paragraph contains enough distinct text to create a searchable wiki chunk."


def _wiki_points(kb_id: str = KB_ID):
    client = vector_store._get_client()
    if not client.collection_exists(kb_id):
        return []
    points, _ = client.scroll(
        collection_name=kb_id,
        limit=1000,
        with_payload=True,
    )
    return [point for point in points if (point.payload or {}).get("chunk_kind") == "wiki"]


def _indexer(store: WikiStore, *, embed_fn=None) -> WikiIndexer:
    return WikiIndexer(
        wiki_store=store,
        chunker=MarkdownASTChunker(max_chunk_chars=300, min_chunk_chars=30),
        embed_fn=embed_fn or (lambda texts: [[0.05] * EMBED_DIMENSION for _ in texts]),
        user_db=store.lifecycle_db,
    )


def test_sync_after_title_reuse_preserves_other_page_vectors(wiki_index_harness):
    store, indexer = wiki_index_harness
    page1 = store.create_page(KB_ID, "A", _content("first page"))
    indexer.sync_page(page1.id)
    page1 = store.update_page(page1.id, title="B", content=_content("renamed first page"))
    indexer.sync_page(page1.id, old_title="A")
    page2 = store.create_page(KB_ID, "A", _content("second page"), slug="new-a")
    indexer.sync_page(page2.id)

    assert {point.payload["document_id"] for point in _wiki_points()} == {page1.id, page2.id}

    indexer.sync_page(page1.id)

    assert {point.payload["document_id"] for point in _wiki_points()} == {page1.id, page2.id}


def test_clearing_renamed_page_preserves_page_reusing_its_old_title(wiki_index_harness):
    store, indexer = wiki_index_harness
    page1 = store.create_page(KB_ID, "A", _content("first page"))
    indexer.sync_page(page1.id)
    page1 = store.update_page(page1.id, title="B", content="")
    page2 = store.create_page(KB_ID, "A", _content("second page"), slug="new-a")
    indexer.sync_page(page2.id)

    assert indexer.sync_page(page1.id) == 0

    remaining = _wiki_points()
    assert all(point.payload["document_id"] != page1.id for point in remaining)
    assert any(point.payload["document_id"] == page2.id for point in remaining)


def test_delete_renamed_page_removes_all_its_vectors(wiki_index_harness):
    store, indexer = wiki_index_harness
    page = store.create_page(KB_ID, "A", _content("first page"))
    indexer.sync_page(page.id)
    page = store.update_page(page.id, title="B", content=_content("renamed page"))

    assert indexer.delete_page_and_index(page.id)

    assert all(point.payload.get("document_id") != page.id for point in _wiki_points())
    assert store.get_page(page.id) is None


def test_deleting_wiki_page_preserves_ordinary_document_with_same_title(wiki_index_harness):
    store, indexer = wiki_index_harness
    page = store.create_page(KB_ID, "Shared title", _content("wiki content"))
    indexer.sync_page(page.id)
    client = vector_store._get_client()
    client.upsert(
        collection_name=KB_ID,
        points=[
            PointStruct(
                id="00000000-0000-4000-8000-000000000001",
                vector=[0.08] * EMBED_DIMENSION,
                payload={
                    "doc_name": page.title,
                    "document_id": 9001,
                    "chunk_index": 0,
                    "text": "ordinary document text",
                    "chunk_kind": "text",
                },
            )
        ],
    )

    indexer.delete_page(page.id, kb_id=page.kb_id, title=page.title)

    points, _ = client.scroll(collection_name=KB_ID, limit=100, with_payload=True)
    assert any(point.id == "00000000-0000-4000-8000-000000000001" for point in points)


def test_legacy_wiki_points_without_page_id_are_cleaned_when_title_is_unambiguous(wiki_index_harness):
    store, indexer = wiki_index_harness
    page = store.create_page(KB_ID, "A", _content("old title"))
    client = vector_store._get_client()
    vector_store._ensure_collection_name(KB_ID)
    client.upsert(
        collection_name=KB_ID,
        points=[
            PointStruct(
                id="00000000-0000-4000-8000-000000000002",
                vector=[0.07] * EMBED_DIMENSION,
                payload={"doc_name": "A", "chunk_index": 9, "chunk_kind": "wiki", "text": "legacy"},
            )
        ],
    )
    page = store.update_page(page.id, title="B", content=_content("new title"))

    indexer.sync_page(page.id)

    assert all(point.id != "00000000-0000-4000-8000-000000000002" for point in _wiki_points())


def test_legacy_title_cleanup_does_not_delete_reused_title_vectors(wiki_index_harness):
    store, indexer = wiki_index_harness
    page1 = store.create_page(KB_ID, "A", _content("old title"))
    page1 = store.update_page(page1.id, title="B", content=_content("renamed page"))
    page2 = store.create_page(KB_ID, "A", _content("new owner"), slug="new-a")
    indexer.sync_page(page2.id)
    client = vector_store._get_client()
    vector_store._ensure_collection_name(KB_ID)
    client.upsert(
        collection_name=KB_ID,
        points=[
            PointStruct(
                id="00000000-0000-4000-8000-000000000003",
                vector=[0.07] * EMBED_DIMENSION,
                payload={"doc_name": "A", "chunk_index": 9, "chunk_kind": "wiki", "text": "legacy"},
            )
        ],
    )
    indexer.sync_page(page1.id)

    assert any(point.id == "00000000-0000-4000-8000-000000000003" for point in _wiki_points())
    assert any(point.payload.get("document_id") == page2.id for point in _wiki_points())


def test_slow_old_revision_cannot_replace_new_revision_across_indexer_instances(wiki_index_harness):
    store, _ = wiki_index_harness
    page = store.create_page(KB_ID, "Versioned", _content("revision one unique"))
    second_store = WikiStore(db_path=store._path)
    second_store.lifecycle_db = store.lifecycle_db
    embed_started = Event()
    release_embed = Event()

    def slow_embed(texts: list[str]) -> list[list[float]]:
        embed_started.set()
        assert release_embed.wait(timeout=10)
        return [[0.05] * EMBED_DIMENSION for _ in texts]

    slow_indexer = _indexer(store, embed_fn=slow_embed)
    fast_indexer = _indexer(second_store)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            old_sync = executor.submit(slow_indexer.sync_page, page.id)
            assert embed_started.wait(timeout=5)
            newer = second_store.update_page(page.id, content=_content("revision two unique"))
            with pytest.raises(KBBusy):
                fast_indexer.sync_page(page.id)
            release_embed.set()
            old_sync.result(timeout=10)
            assert fast_indexer.sync_page(page.id) > 0

        points = [point for point in _wiki_points() if point.payload.get("document_id") == page.id]
        assert points
        assert {point.payload.get("document_version_id") for point in points} == {newer.current_revision_id}
        assert all("revision two unique" in point.payload.get("text", "") for point in points)
    finally:
        release_embed.set()
        second_store.close()


def test_delete_during_slow_embedding_cannot_reinsert_deleted_page(wiki_index_harness):
    store, _ = wiki_index_harness
    page = store.create_page(KB_ID, "Delete race", _content("revision before delete"))
    indexer_store = WikiStore(db_path=store._path)
    indexer_store.lifecycle_db = store.lifecycle_db
    embed_started = Event()
    release_embed = Event()

    def slow_embed(texts: list[str]) -> list[list[float]]:
        embed_started.set()
        assert release_embed.wait(timeout=10)
        return [[0.05] * EMBED_DIMENSION for _ in texts]

    slow_indexer = _indexer(store, embed_fn=slow_embed)
    delete_indexer = _indexer(indexer_store)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            old_sync = executor.submit(slow_indexer.sync_page, page.id)
            assert embed_started.wait(timeout=5)
            with pytest.raises(KBBusy):
                delete_indexer.delete_page_and_index(page.id)
            release_embed.set()
            old_sync.result(timeout=10)
            assert delete_indexer.delete_page_and_index(page.id)

        assert all(point.payload.get("document_id") != page.id for point in _wiki_points())
    finally:
        release_embed.set()
        indexer_store.close()


def test_failed_vector_cleanup_keeps_page_for_retry(wiki_index_harness, monkeypatch):
    store, indexer = wiki_index_harness
    page = store.create_page(KB_ID, "Retry delete", _content("retryable deletion"))
    indexer.sync_page(page.id)

    def fail_delete(_kb_id: str, _point_ids: list[str]) -> int:
        raise RuntimeError("temporary vector outage")

    monkeypatch.setattr(indexer, "_delete_point_ids", fail_delete)

    with pytest.raises(RuntimeError, match="temporary vector outage"):
        indexer.delete_page_and_index(page.id)

    assert store.get_page(page.id) is not None


def test_delete_permit_rejects_sync_on_independent_connection(wiki_index_harness, monkeypatch):
    store, _ = wiki_index_harness
    page = store.create_page(KB_ID, "Concurrent delete", _content("delete boundary"))
    base_indexer = _indexer(store)
    base_indexer.sync_page(page.id)
    other_store = WikiStore(db_path=store._path)
    other_store.lifecycle_db = store.lifecycle_db
    delete_indexer = _indexer(store)
    sync_indexer = _indexer(other_store)
    cleaned = Event()
    allow_delete = Event()
    sync_done = Event()
    original_delete = delete_indexer._delete_point_ids

    def pause_after_cleaning(kb_id: str, point_ids: list[str]) -> int:
        deleted = original_delete(kb_id, point_ids)
        cleaned.set()
        assert allow_delete.wait(timeout=5)
        return deleted

    monkeypatch.setattr(delete_indexer, "_delete_point_ids", pause_after_cleaning)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            deleting = executor.submit(delete_indexer.delete_page_and_index, page.id)
            assert cleaned.wait(timeout=5)

            def sync_and_mark() -> int:
                    try:
                        sync_indexer.sync_page(page.id)
                    except KBBusy:
                        return 0
                    finally:
                        sync_done.set()
                    return 1

            syncing = executor.submit(sync_and_mark)
            assert sync_done.wait(timeout=0.1)
            allow_delete.set()
            assert deleting.result(timeout=5)
            assert syncing.result(timeout=5) == 0

        assert store.get_page(page.id) is None
        assert all(point.payload.get("document_id") != page.id for point in _wiki_points())
    finally:
        allow_delete.set()
        other_store.close()


def test_renamed_page_unbinds_old_title_links_for_reused_title(wiki_index_harness):
    store, _ = wiki_index_harness
    original = store.create_page(KB_ID, "A", _content("original title"))
    reader = store.create_page(KB_ID, "Reader", "See [[A]] for details.")

    assert store.get_outgoing_links(reader.id) == [
        {"target_title": "A", "target_page_id": original.id}
    ]

    renamed = store.update_page(original.id, title="B")
    unbound = store.get_outgoing_links(reader.id)
    assert unbound == [{"target_title": "A", "target_page_id": None}]

    reused = store.create_page(KB_ID, "A", _content("replacement title"), slug="new-a")
    rebound = store.get_outgoing_links(reader.id)
    assert rebound == [{"target_title": "A", "target_page_id": reused.id}]
    assert renamed.title == "B"
