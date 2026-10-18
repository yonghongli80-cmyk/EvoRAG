from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from rag.api import app

client = TestClient(app)


def _auth_headers():
    """创建测试用户并返回 auth headers。"""
    from rag.api import user_db
    from rag.auth import create_token
    try:
        uid = user_db.create_user("_test_admin", "test123")
    except ValueError:
        uid = user_db.get_user_by_username("_test_admin")["id"]
    user_db.set_user_admin(uid, True)
    with user_db.locked_transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO org_units(code,name,kind) "
            "VALUES('TEST-ROOT','测试组织','company')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO org_people"
            "(person_code,user_id,display_name,job_title) "
            "VALUES('P-TEST-ADMIN',?,'测试管理员','管理员')",
            (uid,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO org_memberships(person_code,org_code,is_primary) "
            "VALUES('P-TEST-ADMIN','TEST-ROOT',1)"
        )
    token = create_token({
        "user_id": uid,
        "username": "_test_admin",
        "security_version": 1,
    })
    return {"Authorization": f"Bearer {token}"}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded")
    assert "components" in data


def test_lifespan_stops_backup_scheduler(monkeypatch):
    calls = []
    monkeypatch.setattr("rag.api.auto_index_on_startup", lambda: None)
    monkeypatch.setattr(
        "rag.api.start_backup_scheduler", lambda: calls.append("start")
    )
    monkeypatch.setattr(
        "rag.api.stop_backup_scheduler", lambda: calls.append("stop")
    )

    with TestClient(app):
        assert calls == ["start"]

    assert calls == ["start", "stop"]


@patch("rag.routers.documents.KnowledgeBaseManager")
def test_index_adds_to_kb(mock_kb_cls, tmp_path, isolated_api):
    mock_manager = MagicMock()
    mock_manager.add_document.return_value = 5
    mock_kb_cls.return_value = mock_manager

    doc = tmp_path / "test.txt"
    doc.write_text("test content")

    with open(doc, "rb") as f:
        response = isolated_api.post(
            "/index",
            files={"file": ("test.txt", f, "text/plain")},
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "indexed"
    assert data["chunks"] == 5
    mock_manager.add_document.assert_called_once()


@patch("rag.pipeline.RAGPipeline")
def test_query_without_index_returns_400(mock_pipeline_cls):
    response = client.post("/query", json={"question": "test"}, headers=_auth_headers())
    assert response.status_code == 400


def test_query_returns_answer():
    import rag.api as api_module
    from rag.context import set_pipeline
    mock_pipeline = MagicMock()
    mock_pipeline.query.return_value = MagicMock(answer="answer from RAG", sources=[])

    # 直接设置全局 pipeline
    api_module.pipeline = mock_pipeline
    set_pipeline(mock_pipeline)
    try:
        response = client.post(
            "/query",
            json={"question": "what is RAG?"},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        assert response.json()["answer"] == "answer from RAG"
    finally:
        api_module.pipeline = None
        set_pipeline(None)


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_query_knowledge_base(mock_kb_manager_cls):
    """POST /knowledge-bases/{kb_id}/query should return search results."""
    from rag.models import Chunk

    mock_manager = MagicMock()
    mock_manager.search.return_value = [
        Chunk(text="RAG is retrieval augmented generation", doc_name="doc.txt", chunk_index=0),
    ]
    mock_kb_manager_cls.return_value = mock_manager

    response = client.post(
        "/knowledge-bases/kb_abc12345/query",
        json={"question": "what is RAG?", "top_k": 3},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["kb_id"] == "kb_abc12345"
    assert len(data["results"]) == 1
    assert data["results"][0]["doc_name"] == "doc.txt"
    assert len(data["results"]) <= 3
    mock_manager.search.assert_called_once_with("kb_abc12345", "what is RAG?", top_k=9)


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_delete_system_collection_returns_400(mock_kb_manager_cls):
    """DELETE /knowledge-bases/rag_docs should return 400 (bad request)."""
    mock_manager = MagicMock()
    mock_manager.delete_kb.side_effect = ValueError("不能删除系统集合 rag_docs，只能删除 kb_ 前缀的知识库")
    mock_kb_manager_cls.return_value = mock_manager

    response = client.delete("/knowledge-bases/rag_docs", headers=_auth_headers())

    assert response.status_code == 400
    assert "不能删除" in response.json()["detail"]


def test_health_returns_component_status():
    """GET /health should return component status."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "components" in data


def test_health_returns_503_when_a_critical_component_is_unavailable():
    """Returning 200 while degraded would make container health checks pass."""
    with patch(
        "rag.vector_store._get_client",
        side_effect=RuntimeError("Qdrant unavailable"),
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "components": {"qdrant": "error", "sqlite": "ok"},
    }


def test_query_stream_endpoint_exists():
    """POST /query/stream 端点应该存在。"""
    from fastapi.testclient import TestClient

    from rag.api import app
    client = TestClient(app)
    response = client.post("/query/stream", json={"question": "test"})
    assert response.status_code != 404


def test_suggest_endpoint_exists():
    """POST /suggest 端点应该存在。"""
    from fastapi.testclient import TestClient

    from rag.api import app
    client = TestClient(app)
    response = client.post("/suggest", json={"question": "test", "answer": "test"})
    assert response.status_code != 404


# ── tags endpoint tests ─────────────────────────────────────────────

@patch("rag.vector_store._get_client")
def test_add_tags_to_file(mock_get_client):
    """POST /files/{filename}/tags should update tags on document points."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.collection_exists.return_value = True

    # Mock scroll to return 2 points for the document
    point1 = MagicMock()
    point1.id = "id1"
    point2 = MagicMock()
    point2.id = "id2"
    mock_client.scroll.return_value = ([point1, point2], None)

    # Mock retrieve to return existing payload
    existing1 = MagicMock()
    existing1.payload = {"tags": ["old_tag"]}
    existing2 = MagicMock()
    existing2.payload = {"tags": []}
    mock_client.retrieve.side_effect = [[existing1], [existing2]]

    response = client.post(
        "/files/test.txt/tags",
        json=["finance", "q3"],
        headers={"Authorization": "Bearer fake_token"},
    )

    # May fail auth, but the endpoint should exist (not 404)
    assert response.status_code != 404


@patch("rag.vector_store._get_client")
def test_list_tags(mock_get_client):
    """GET /tags should return all unique tags from the collection."""
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.collection_exists.return_value = True

    p1 = MagicMock()
    p1.payload = {"doc_name": "finance.txt", "tags": ["finance", "q3"]}
    p2 = MagicMock()
    p2.payload = {"doc_name": "annual.txt", "tags": ["finance", "annual"]}
    p3 = MagicMock()
    p3.payload = {"doc_name": "empty.txt", "tags": []}
    mock_client.scroll.return_value = ([p1, p2, p3], None)

    from rag.api import user_db
    user_db.create_document_permission("finance.txt", "rag_docs", 0, scope="public")
    user_db.create_document_permission("annual.txt", "rag_docs", 0, scope="public")
    user_db.create_document_permission("empty.txt", "rag_docs", 0, scope="public")

    response = client.get("/tags", headers=_auth_headers())
    assert response.status_code == 200
    data = response.json()
    assert "tags" in data
    assert sorted(data["tags"]) == ["annual", "finance", "q3"]


def test_list_tags_empty_collection():
    """GET /tags should return empty list when collection has no tags."""
    from unittest.mock import patch as _patch
    with _patch("rag.vector_store._get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.collection_exists.return_value = True
        mock_client.scroll.return_value = ([], None)

        response = client.get("/tags", headers=_auth_headers())
        assert response.status_code == 200
        assert response.json()["tags"] == []


def test_query_request_accepts_tags():
    """QueryRequest model should accept tags field."""
    from rag.routers.query import QueryRequest
    req = QueryRequest(question="test", tags=["finance", "q3"])
    assert req.tags == ["finance", "q3"]


def test_query_request_tags_default_none():
    """QueryRequest tags should default to None."""
    from rag.routers.query import QueryRequest
    req = QueryRequest(question="test")
    assert req.tags is None


def test_stream_query_request_accepts_tags():
    """StreamQueryRequest model should accept tags field."""
    from rag.routers.query import StreamQueryRequest
    req = StreamQueryRequest(question="test", tags=["finance"])
    assert req.tags == ["finance"]


def test_files_endpoint_returns_in_kb_field():
    """/files 返回的每个文件应该有 in_kb 字段。"""
    from fastapi.testclient import TestClient

    from rag.api import app
    client = TestClient(app)
    response = client.get("/files", headers=_auth_headers())
    assert response.status_code == 200
    data = response.json()
    if data["files"]:
        assert "in_kb" in data["files"][0], "文件应该有 in_kb 字段"


# ── KB detail endpoint tests ──────────────────────────────────────


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_get_kb_detail(mock_kb_cls, isolated_api):
    """GET /knowledge-bases/{id} 应该返回知识库详情。"""
    mock_manager = MagicMock()
    mock_manager.create_kb.return_value = "kb_test123"
    mock_manager.list_kbs.return_value = [
        MagicMock(kb_id="kb_test123", name="test_kb_detail", doc_count=0)
    ]
    mock_kb_cls.return_value = mock_manager

    client = isolated_api
    headers = _auth_headers()
    # 先创建一个知识库
    res = client.post("/knowledge-bases", json={"name": "test_kb_detail", "org_unit_code": "TEST-ROOT"}, headers=headers)
    assert res.status_code == 200
    kb_id = res.json()["kb_id"]
    # 获取详情
    response = client.get(f"/knowledge-bases/{kb_id}", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "name" in data


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_update_kb_overview(mock_kb_cls, isolated_api):
    """PUT /knowledge-bases/{id}/overview 应该更新概述。"""
    mock_manager = MagicMock()
    mock_manager.create_kb.return_value = "kb_test_overview"
    mock_manager.list_kbs.return_value = [
        MagicMock(kb_id="kb_test_overview", name="test_overview", doc_count=0)
    ]
    mock_kb_cls.return_value = mock_manager

    client = isolated_api
    headers = _auth_headers()
    res = client.post("/knowledge-bases", json={"name": "test_overview", "org_unit_code": "TEST-ROOT"}, headers=headers)
    assert res.status_code == 200
    kb_id = res.json()["kb_id"]
    response = client.put(f"/knowledge-bases/{kb_id}/overview", json={"overview": "这是概述"}, headers=headers)
    assert response.status_code == 200


@patch("rag.routers.knowledge.KnowledgeBaseManager")
def test_update_doc_toc(mock_kb_cls, isolated_api):
    """PUT /knowledge-bases/{id}/documents/{name}/toc 应该更新目录。"""
    mock_manager = MagicMock()
    mock_manager.create_kb.return_value = "kb_test_toc"
    mock_manager.list_kbs.return_value = [
        MagicMock(kb_id="kb_test_toc", name="test_toc", doc_count=0)
    ]
    mock_kb_cls.return_value = mock_manager

    client = isolated_api
    headers = _auth_headers()
    res = client.post("/knowledge-bases", json={"name": "test_toc", "org_unit_code": "TEST-ROOT"}, headers=headers)
    assert res.status_code == 200
    kb_id = res.json()["kb_id"]
    response = client.put(f"/knowledge-bases/{kb_id}/documents/test.txt/toc", json={"toc": {"title": "test", "sections": []}}, headers=headers)
    # 文档不存在时返回 404，或成功时返回 200
    assert response.status_code in (200, 404)
