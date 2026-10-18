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

    # Wire up test overrides：接缝是取 store 的**访问器**。
    # 以前这里 patch 的是模块全局 `_wiki_store`，而那个全局从未被赋值（死代码，2026-10-18 删除）。
    import rag.wiki.api as wiki_api_module
    monkeypatch.setattr(wiki_api_module, "get_wiki_store", lambda: store)
    monkeypatch.setattr(wiki_api_module, "_wiki_indexer", indexer)

    import rag.evolution.api as evo_api_module
    monkeypatch.setattr(evo_api_module, "_open_store", lambda: trace_store)

    # Disable auth requirement in development test mode
    from config import settings
    monkeypatch.setattr(settings, "auth_enabled", False)

    db = getattr(app.state, "user_db", None)
    if db and not db.get_kb_metadata("kb_prod"):
        db.create_kb_metadata("kb_prod", "Prod KB", 1, "public")

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


def test_wiki_api_fine_grained_permissions(isolated_api, make_user, monkeypatch, tmp_path):
    import rag.wiki.api as wiki_api_module
    from rag.wiki.indexer import WikiIndexer
    from rag.wiki.store import WikiStore

    wiki_db = str(tmp_path / "wiki_perm.db")
    store = WikiStore(db_path=wiki_db)
    indexer = WikiIndexer(wiki_store=store, embed_fn=lambda texts: [[0.05] * 1024 for _ in texts])
    monkeypatch.setattr(wiki_api_module, "get_wiki_store", lambda: store)
    monkeypatch.setattr(wiki_api_module, "_wiki_indexer", indexer)

    db = isolated_api.app.state.user_db
    alice, alice_auth = make_user("alice_wiki")
    bob, bob_auth = make_user("bob_wiki")

    # Alice creates private KB
    db.create_kb_metadata("kb_private", "Private KB", alice["id"], "private")

    # 1. Bob cannot create page in Alice's private KB (403 or 404)
    resp = isolated_api.post(
        "/wiki/pages",
        json={"kb_id": "kb_private", "title": "Secret Doc", "content": "Initial"},
        headers=bob_auth,
    )
    assert resp.status_code in (403, 404)

    # 2. Alice creates the page successfully
    resp = isolated_api.post(
        "/wiki/pages",
        json={"kb_id": "kb_private", "title": "Secret Doc", "content": "Initial"},
        headers=alice_auth,
    )
    assert resp.status_code == 201
    page_id = resp.json()["page"]["id"]

    # 3. Bob cannot get page
    resp = isolated_api.get(f"/wiki/pages/{page_id}", headers=bob_auth)
    assert resp.status_code in (403, 404)

    # 4. Bob cannot get page history
    resp = isolated_api.get(f"/wiki/pages/{page_id}/history", headers=bob_auth)
    assert resp.status_code in (403, 404)

    # 5. Bob cannot update page
    resp = isolated_api.put(
        f"/wiki/pages/{page_id}",
        json={"content": "Hacked content"},
        headers=bob_auth,
    )
    assert resp.status_code in (403, 404)

    # 6. Bob cannot rollback page
    resp = isolated_api.post(
        f"/wiki/pages/{page_id}/rollback",
        json={"revision_num": 1},
        headers=bob_auth,
    )
    assert resp.status_code in (403, 404)

    # 7. Bob cannot delete page
    resp = isolated_api.delete(f"/wiki/pages/{page_id}", headers=bob_auth)
    assert resp.status_code in (403, 404)

    # 8. Bob cannot list pages of private KB
    resp = isolated_api.get("/wiki/pages", params={"kb_id": "kb_private"}, headers=bob_auth)
    assert resp.status_code in (403, 404)

    # 9. Alice CAN view and update
    resp = isolated_api.get(f"/wiki/pages/{page_id}", headers=alice_auth)
    assert resp.status_code == 200

    resp = isolated_api.put(
        f"/wiki/pages/{page_id}",
        json={"content": "Alice updated content"},
        headers=alice_auth,
    )
    assert resp.status_code == 200

    store.close()


def test_get_revision_diff(api_client):
    """API-01: GET /wiki/pages/{id}/revisions/{num}/diff returns unified diff."""
    client: TestClient = api_client["client"]

    # 1. Create page
    create_resp = client.post(
        "/wiki/pages",
        json={
            "kb_id": "kb_prod",
            "title": "Redis Cache Guide",
            "content": "Line 1: Redis is in-memory.\nLine 2: Fast access.",
        },
    )
    assert create_resp.status_code == 201
    page_id = create_resp.json()["page"]["id"]

    # 2. Update page to produce revision 2
    update_resp = client.put(
        f"/wiki/pages/{page_id}",
        json={
            "content": "Line 1: Redis is an in-memory store.\nLine 2: Fast access.\nLine 3: Cluster support.",
            "comment": "Clarified store and added cluster",
        },
    )
    assert update_resp.status_code == 200

    # 3. Call revision 2 diff endpoint
    diff_resp = client.get(f"/wiki/pages/{page_id}/revisions/2/diff")
    assert diff_resp.status_code == 200
    data = diff_resp.json()
    assert data["page_id"] == page_id
    assert data["revision_num"] == 2
    assert data["delta"] is not None
    assert "+Line 3: Cluster support." in data["delta"]
    assert data["comment"] == "Clarified store and added cluster"

    # 4. Nonexistent revision returns 404
    missing_resp = client.get(f"/wiki/pages/{page_id}/revisions/99/diff")
    assert missing_resp.status_code == 404

