"""Unit tests for Wiki Ingestion & Hybrid Indexing Pipeline."""

from __future__ import annotations

import pytest
from qdrant_client import QdrantClient

import rag.bm25_store as bm25_store
import rag.retriever as retriever_module
import rag.vector_store as vector_store
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
    chunker = MarkdownASTChunker(max_chunk_chars=300, min_chunk_chars=40)
    indexer = WikiIndexer(wiki_store=wiki_store, chunker=chunker, embed_fn=fake_embed)

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

