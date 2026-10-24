"""Unit tests for Wiki Ingestion & Hybrid Indexing Pipeline."""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

import rag.bm25_store as bm25_store
import rag.retriever as retriever_module
import rag.vector_store as vector_store
from rag.user_db import UserDB
from rag.wiki.chunker import MarkdownASTChunker
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate Qdrant and SQLite BM25 storage in temporary test directory."""
    qdrant_dir = str(tmp_path / "qdrant")
    bm25_file = str(tmp_path / "bm25.db")
    wiki_file = str(tmp_path / "wiki.db")

    vector_store._client = QdrantClient(path=qdrant_dir)
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", bm25_file)
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", bm25_file)

    def fake_embed(texts: list[str]) -> list[list[float]]:
        # Deterministic 1024-dim embedding
        return [[float(len(t) % 100) / 100.0] * 1024 for t in texts]

    monkeypatch.setattr(retriever_module, "embed", fake_embed)
    retriever_module.invalidate_bm25_cache()

    wiki_store = WikiStore(db_path=wiki_file)
    user_db = UserDB(str(tmp_path / "users.db"))
    chunker = MarkdownASTChunker(max_chunk_chars=300, min_chunk_chars=40)
    indexer = WikiIndexer(
        wiki_store=wiki_store, chunker=chunker, embed_fn=fake_embed, user_db=user_db
    )

    yield {
        "wiki_store": wiki_store,
        "indexer": indexer,
        "chunker": chunker,
        "fake_embed": fake_embed,
        "collection": "kb_test_wiki",
    }

    try:
        if vector_store._client is not None:
            vector_store._client.close()
    except Exception:
        pass
    vector_store._client = None
    wiki_store.close()
    user_db.close()
    retriever_module.invalidate_bm25_cache()


def test_sync_new_page(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="网络排错指南",
        content="""# 网络诊断

## DNS 故障排查
遇到域名解析超时，请先检查本地 resolv.conf 配置。
确认默认网关与上游 DNS 地址可用。

## 路由丢包分析
使用 mtr 持续追踪核心交换机丢包率。
""",
        tags=["network", "sop"],
    )

    synced_chunks = indexer.sync_page(page.id)
    assert synced_chunks == 2

    # Verify points exist in Qdrant
    client = vector_store._get_client()
    records, _ = client.scroll(
        collection_name=kb_id,
        limit=10,
        with_payload=True,
    )
    assert len(records) == 2
    for r in records:
        assert r.payload["doc_name"] == "网络排错指南"
        assert r.payload["chunk_kind"] == "wiki"
        assert "resolv.conf" in r.payload["text"] or "丢包率" in r.payload["text"]


def test_sync_updated_page_cleans_stale_chunks(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="服务升级规范",
        content="""# 升级前置条件
第一阶段准备工作。

# 停机操作流程
第二阶段执行停机。

# 验收与复盘
第三阶段完成验收。
""",
    )

    count1 = indexer.sync_page(page.id)
    assert count1 == 3

    # Update page to have only 1 section
    wiki_store.update_page(
        page.id,
        content="""# 快速升级总览
全部合并为一键全自动滚动升级流程。
""",
    )

    count2 = indexer.sync_page(page.id)
    assert count2 == 1

    client = vector_store._get_client()
    records, _ = client.scroll(
        collection_name=kb_id,
        limit=10,
        with_payload=True,
    )
    assert len(records) == 1
    assert "快速升级总览" in records[0].payload["text"]
    assert "停机操作流程" not in records[0].payload["text"]


def test_delete_page_removes_from_index(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="临时草稿",
        content="""# 临时指引
本页面将被删除测试。
""",
    )
    indexer.sync_page(page.id)

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10)
    assert len(records) == 1

    # Now delete from index and store
    indexer.delete_page(page.id)
    wiki_store.delete_page(page.id)

    records_after, _ = client.scroll(collection_name=kb_id, limit=10)
    assert len(records_after) == 0


def test_reindex_all(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    wiki_store.create_page(
        kb_id=kb_id,
        title="页面一",
        content="# 第一章\n内容详情一。",
    )
    wiki_store.create_page(
        kb_id=kb_id,
        title="页面二",
        content="# 第二章\n内容详情二。",
    )

    summary = indexer.reindex_all(kb_id)
    assert summary["pages_synced"] == 2
    assert summary["chunks_indexed"] == 2

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10)
    assert len(records) == 2


def test_empty_page_cleans_index(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="清空测试",
        content="# 待清空\n原始有效文本。",
    )
    indexer.sync_page(page.id)

    wiki_store.update_page(page.id, content="   \n\n  ")
    synced = indexer.sync_page(page.id)
    assert synced == 0

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10)
    assert len(records) == 0


def test_rename_page_purges_old_title_vectors(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="旧标题A",
        content="# 第一章\n旧标题对应内容。",
    )
    indexer.sync_page(page.id)

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records) == 1
    assert records[0].payload["doc_name"] == "旧标题A"

    # Rename page to "新标题B"
    old_title = page.title
    wiki_store.update_page(page.id, title="新标题B")
    indexer.sync_page(page.id, old_title=old_title)

    records_after, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records_after) == 1
    assert records_after[0].payload["doc_name"] == "新标题B"

    # Verify no ghost points remain for "旧标题A"
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    old_records, _ = client.scroll(
        collection_name=kb_id,
        limit=10,
        scroll_filter=Filter(
            must=[FieldCondition(key="doc_name", match=MatchValue(value="旧标题A"))]
        ),
        with_payload=True,
    )
    assert len(old_records) == 0


def test_sync_failure_keeps_the_previous_revision_searchable(isolated_env, monkeypatch):
    """D-05: a failed re-index must not delete the chunks that were already working."""
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="回滚演练",
        content="# 第一节\n旧内容继续可检索。",
    )
    assert indexer.sync_page(page.id) == 1
    wiki_store.update_page(page.id, content="# 第二节\n新内容暂时写不进去。")

    def broken_embed(_texts):
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(indexer, "_embed_fn", broken_embed)
    with pytest.raises(RuntimeError):
        indexer.sync_page(page.id)

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records) == 1
    assert "旧内容" in records[0].payload["text"]

    monkeypatch.setattr(
        indexer, "_embed_fn", lambda texts: [[0.2] * 1024 for _ in texts]
    )
    assert indexer.sync_page(page.id) == 1

    records_after, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records_after) == 1
    assert "新内容" in records_after[0].payload["text"]
    assert "旧内容" not in records_after[0].payload["text"]


def test_add_failure_keeps_the_previous_revision_searchable(isolated_env, monkeypatch):
    """D-05: a failed vector write must leave the old revision in place."""
    import rag.wiki.indexer as indexer_module

    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="写入失败",
        content="# 第一节\n旧内容继续可检索。",
    )
    indexer.sync_page(page.id)
    wiki_store.update_page(page.id, content="# 第二节\n写入新块时失败。")

    def broken_add(*_args, **_kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(indexer_module, "add_to_collection", broken_add)
    with pytest.raises(RuntimeError):
        indexer.sync_page(page.id)

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records) == 1
    assert "旧内容" in records[0].payload["text"]


def test_resync_of_the_same_revision_does_not_duplicate_points(isolated_env):
    """The new write-then-delete ordering must stay idempotent for retries."""
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    page = wiki_store.create_page(
        kb_id=kb_id,
        title="重复索引",
        content="# 第一节\n同一修订重复同步。",
    )
    assert indexer.sync_page(page.id) == 1
    assert indexer.sync_page(page.id) == 1

    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records) == 1


def test_delete_wiki_page_preserves_identically_named_regular_doc(isolated_env):
    wiki_store: WikiStore = isolated_env["wiki_store"]
    indexer: WikiIndexer = isolated_env["indexer"]
    kb_id = isolated_env["collection"]

    from rag.models import Chunk
    vector_store._ensure_collection_name(kb_id)
    # 1. Insert a regular (non-wiki) document with doc_name="guide.md"
    reg_chunk = Chunk(text="Regular document content", doc_name="guide.md", chunk_index=0)
    vector_store.add_to_collection(
        collection_name=kb_id,
        chunks=[reg_chunk],
        embeddings=[[0.5] * 1024],
        document_id=999,
        document_version_id=1,
    )

    # 2. Insert a Wiki page with title="guide.md"
    page = wiki_store.create_page(
        kb_id=kb_id,
        title="guide.md",
        content="# Wiki Guide\nThis is a wiki page with identical title.",
    )
    indexer.sync_page(page.id)

    # Both exist
    client = vector_store._get_client()
    records, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(records) == 2

    # 3. Delete wiki page
    indexer.delete_page(page.id)

    # 4. Verify regular document still exists
    remaining, _ = client.scroll(collection_name=kb_id, limit=10, with_payload=True)
    assert len(remaining) == 1
    assert remaining[0].payload["doc_name"] == "guide.md"
    assert remaining[0].payload.get("chunk_kind") != "wiki"


