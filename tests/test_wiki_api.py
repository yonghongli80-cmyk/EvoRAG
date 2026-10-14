"""Unit tests for Hypertext Wiki API Endpoints and Gap Remediation Route."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

import rag.bm25_store as bm25_store
import rag.retriever as retriever_module
import rag.vector_store as vector_store
from rag.api import app
from rag.evolution.gaps import KnowledgeGapTicket
from rag.evolution.store import FailureTraceStore
from rag.wiki.indexer import WikiIndexer
from rag.wiki.store import WikiStore


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    wiki_db = str(tmp_path / "wiki.db")
    evo_db = str(tmp_path / "evolution.db")
    qdrant_dir = str(tmp_path / "qdrant")
    bm25_db = str(tmp_path / "bm25.db")

    vector_store._client = QdrantClient(path=qdrant_dir)
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", bm25_db)
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", bm25_db)
    monkeypatch.setattr(retriever_module, "embed", lambda texts: [[0.05] * 1024 for _ in texts])

    store = WikiStore(db_path=wiki_db)
    indexer = WikiIndexer(
        wiki_store=store,
        embed_fn=lambda texts: [[0.05] * 1024 for _ in texts],
    )
    trace_store = FailureTraceStore(db_path=evo_db)

    # Wire up test overrides
    import rag.wiki.api as wiki_api_module
    monkeypatch.setattr(wiki_api_module, "_wiki_store", store)
    monkeypatch.setattr(wiki_api_module, "_wiki_indexer", indexer)

    import rag.evolution.api as evo_api_module
    monkeypatch.setattr(evo_api_module, "_open_store", lambda: trace_store)

    # Disable auth requirement in development test mode
    from config import settings
    monkeypatch.setattr(settings, "auth_enabled", False)

    client = TestClient(app)

    yield {
        "client": client,
        "wiki_store": store,
        "trace_store": trace_store,
        "indexer": indexer,
    }

    try:
        if vector_store._client is not None:
            vector_store._client.close()
    except Exception:
        pass
    vector_store._client = None
    store.close()


def test_wiki_page_crud_lifecycle(api_client):
    client: TestClient = api_client["client"]

    # 1. Create page
    create_resp = client.post(
        "/wiki/pages",
        json={
            "kb_id": "kb_prod",
            "title": "故障恢复指南",
            "content": "# 第一章 快速止血\n执行核心切换动作。\n\n# 第二章 数据补录\n参考 [[备份策略]] 进行修复。",
            "tags": ["dr", "sop"],
        },
    )
    assert create_resp.status_code == 201
    created_data = create_resp.json()
    assert created_data["page"]["title"] == "故障恢复指南"
    assert created_data["chunks_indexed"] >= 1
    page_id = created_data["page"]["id"]

    # 2. Get page details
    detail_resp = client.get(f"/wiki/pages/{page_id}")
    assert detail_resp.status_code == 200
    detail_data = detail_resp.json()
    assert detail_data["page"]["id"] == page_id
    assert len(detail_data["outgoing_links"]) == 1
    assert detail_data["outgoing_links"][0]["target_title"] == "备份策略"

    # 3. List pages
    list_resp = client.get("/wiki/pages", params={"kb_id": "kb_prod", "tag": "dr"})
    assert list_resp.status_code == 200
    pages = list_resp.json()
    assert len(pages) == 1
    assert pages[0]["id"] == page_id

    # 4. Update page
    update_resp = client.put(
        f"/wiki/pages/{page_id}",
        json={
            "content": "# 第一章 全自动止血\n升级为脚本化全自动一键止血。",
            "comment": "升级自动化流程",
        },
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["page"]["current_revision_id"] == 2

    # 5. Check history
    history_resp = client.get(f"/wiki/pages/{page_id}/history")
    assert history_resp.status_code == 200
    history = history_resp.json()
    assert len(history) == 2
    assert history[0]["revision_num"] == 1
    assert history[1]["revision_num"] == 2

    # 6. Rollback to revision 1
    rollback_resp = client.post(
        f"/wiki/pages/{page_id}/rollback",
        json={"revision_num": 1},
    )
    assert rollback_resp.status_code == 200
    assert "执行核心切换动作" in rollback_resp.json()["page"]["content"]

    # 7. Delete page
    del_resp = client.delete(f"/wiki/pages/{page_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # 8. Verify 404 after deletion
    get_after = client.get(f"/wiki/pages/{page_id}")
    assert get_after.status_code == 404


def test_evolution_gap_ticket_wiki_remediation_endpoint(api_client):
    client: TestClient = api_client["client"]
    wiki_store: WikiStore = api_client["wiki_store"]
    trace_store: FailureTraceStore = api_client["trace_store"]

    # Create cluster and gap ticket
    cluster = trace_store.upsert_cluster(
        cluster_key="kb_prod|generation|gap",
        kb_id="kb_prod",
        failure_stage="generation",
        root_cause_feature="undetermined",
        seen_at=100.0,
    )
    ticket = KnowledgeGapTicket(
        cluster_id=cluster["id"],
        kb_id="kb_prod",
        question_samples=("BGP 路由震荡怎么处理？",),
        occurrences=4,
        first_seen=50.0,
        last_seen=100.0,
        missing_terms=("bgp", "路由震荡"),
        probed_questions=1,
        rationale="Missing BGP SOP",
        created_at=100.0,
        status="open",
    )
    ticket_id = trace_store.add_gap_ticket(ticket)

    # Create wiki page for remediation
    wiki_page = wiki_store.create_page(
        kb_id="kb_prod",
        title="BGP路由维护SOP",
        content="# BGP 路由震荡处置规范\n按此流程处理 BGP 路由震荡故障。",
    )

    # Call remediation endpoint
    resp = client.post(
        f"/knowledge-operations/evolution/gap-tickets/{ticket_id}/remediate/wiki",
        json={"page_id": wiki_page.id, "auto_recheck": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticket_id"] == ticket_id
    assert data["remediation"]["type"] == "wiki"
    assert data["remediation"]["asset_id"] == wiki_page.id

    # Get remediation detail
    rem_detail = client.get(f"/knowledge-operations/evolution/gap-tickets/{ticket_id}/remediation")
    assert rem_detail.status_code == 200
    assert rem_detail.json()["page_title"] == "BGP路由维护SOP"
