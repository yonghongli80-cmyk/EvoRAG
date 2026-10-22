"""Integration tests for Hypertext Wiki with Evidence Bundle and Versioned Retrieval."""

from __future__ import annotations

from qdrant_client import QdrantClient

import rag.vector_store as vector_store
from rag.reliability.bundle import build_evidence_bundle
from rag.wiki.chunker import MarkdownASTChunker, WikiChunk
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore


def test_wiki_chunk_version_metadata_and_evidence_bundle():
    """RET-01: WikiChunk must retain document_version_id and version_no for EvidenceBundle."""
    wc = WikiChunk(
        text="[Wiki: System Architecture]\n\nCore distributed knowledge engine.",
        raw_text="Core distributed knowledge engine.",
        section_path="System Architecture",
        heading_level=1,
        links=(),
        kb_id="kb_prod",
        page_id=42,
        chunk_index=0,
        metadata={"title": "System Architecture"},
    )

    # Calling to_chunk with document_version_id
    chunk = wc.to_chunk(document_version_id=101, version_no=3)
    assert chunk.document_id == 42
    assert chunk.document_version_id == 101
    assert chunk.version_no == 3
    assert chunk.chunk_kind == "wiki"

    # Must satisfy EvidenceBundle gate without raising ValueError
    bundle = build_evidence_bundle(
        question="What is the architecture?",
        chunks=[chunk],
        kb_id="kb_prod",
        acl_checked=True,
    )
    assert len(bundle.current_evidence) == 1
    assert bundle.current_evidence[0].document_id == 42
    assert bundle.current_evidence[0].document_version_id == 101
    assert bundle.current_evidence[0].version_no == 3


def test_wiki_indexer_syncs_version_metadata_to_chunks_and_qdrant(tmp_path, monkeypatch):
    """RET-01: WikiIndexer must inject page.current_revision_id into indexed chunks."""
    qdrant_dir = str(tmp_path / "qdrant")
    wiki_file = str(tmp_path / "wiki.db")

    vector_store._client = QdrantClient(path=qdrant_dir)
    store = WikiStore(db_path=wiki_file)
    chunker = MarkdownASTChunker(max_chunk_chars=300, min_chunk_chars=40)

    def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]

    indexer = WikiIndexer(wiki_store=store, chunker=chunker, embed_fn=fake_embed)

    try:
        page = store.create_page(
            kb_id="kb_ret_test",
            title="Database Sharding",
            content="# Database Sharding\n\nHorizontal partitioning details.",
        )
        assert page.current_revision_id > 0

        indexed_count = indexer.sync_page(page.id)
        assert indexed_count > 0

        # Query points from vector store to verify payload document_version_id
        client = vector_store._get_client()
        points, _ = client.scroll(
            collection_name="kb_ret_test",
            limit=10,
            with_payload=True,
        )
        assert len(points) == indexed_count
        for pt in points:
            assert pt.payload.get("document_id") == page.id
            assert pt.payload.get("document_version_id") == page.current_revision_id
            assert pt.payload.get("chunk_kind") == "wiki"
    finally:
        vector_store.close_client()
        store.close()


def test_visible_current_version_ids_includes_wiki_revisions(tmp_path):
    """RET-02: _visible_current_version_ids must include Wiki page revisions when user has view rights."""
    from rag.user_db import UserDB
    from rag.versioned_retrieval import _visible_current_version_ids

    user_db = UserDB(str(tmp_path / "user.db"))
    wiki_store = WikiStore(db_path=str(tmp_path / "wiki.db"))

    try:
        alice_id = user_db.create_user("alice", "pwd123")
        bob_id = user_db.create_user("bob", "pwd123")
        bob = user_db.get_user_by_id(bob_id)

        # B-02: a legacy ``scope='public'`` KB no longer grants view to another
        # account; bob gets view through the organization model instead.
        kb_id = "kb_wiki_versions"
        user_db.create_kb_metadata(
            kb_id=kb_id, name="Wiki KB", owner_id=alice_id, scope="private"
        )
        with user_db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO org_units(code,parent_code,name,kind) "
                "VALUES('ENG',NULL,'Engineering','team')"
            )
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES('P_BOB',?,'Bob','工程师')",
                (bob_id,),
            )
            conn.execute("INSERT INTO org_memberships VALUES('P_BOB','ENG',1)")
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
                "VALUES(?,'ENG','organization')",
                (kb_id,),
            )

        page = wiki_store.create_page(
            kb_id=kb_id,
            title="System Topo",
            content="# System Topology\n\nHigh availability setup.",
        )
        assert page.current_revision_id > 0

        # Bob is an organization member -> his allowed version IDs include the revision.
        version_ids = _visible_current_version_ids(user_db, bob, kb_id, wiki_store=wiki_store)
        assert version_ids is not None
        assert page.current_revision_id in version_ids

        # If a KB is private and bob has no access, wiki revisions must NOT be visible
        private_kb = "kb_private_wiki"
        user_db.create_kb_metadata(kb_id=private_kb, name="Private KB", owner_id=alice_id, scope="private")
        priv_page = wiki_store.create_page(
            kb_id=private_kb,
            title="Secret Strategy",
            content="# Secret Strategy\n\nConfidential.",
        )
        priv_version_ids = _visible_current_version_ids(user_db, bob, private_kb, wiki_store=wiki_store)
        assert priv_version_ids is not None
        assert priv_page.current_revision_id not in priv_version_ids
    finally:
        user_db.close()
        wiki_store.close()


def test_visible_current_version_ids_uses_default_store(tmp_path, monkeypatch):
    """RET-02 Regression: _visible_current_version_ids must resolve default wiki_store when None is passed."""
    import rag.wiki.store as wiki_store_mod
    from rag.user_db import UserDB
    from rag.versioned_retrieval import _visible_current_version_ids

    user_db = UserDB(str(tmp_path / "user.db"))
    store = wiki_store_mod.WikiStore(db_path=str(tmp_path / "wiki.db"))
    monkeypatch.setattr(wiki_store_mod, "_default_wiki_store", store)

    try:
        user_id = user_db.create_user("charlie", "pwd123")
        user = user_db.get_user_by_id(user_id)
        kb_id = "kb_wiki_default"
        user_db.create_kb_metadata(kb_id=kb_id, name="Public Wiki", owner_id=user_id, scope="public")

        page = store.create_page(
            kb_id=kb_id,
            title="Overview",
            content="# System Overview",
        )
        assert page.current_revision_id > 0

        # Call WITHOUT passing wiki_store parameter
        version_ids = _visible_current_version_ids(user_db, user, kb_id)
        assert version_ids is not None
        assert page.current_revision_id in version_ids
    finally:
        user_db.close()
        store.close()


