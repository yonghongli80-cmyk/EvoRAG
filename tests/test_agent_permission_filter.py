"""Agent 路由权限过滤 + 路由判定精确化测试。

验证：
- 走 agent 路由的 retrieve 工具返回结果不含用户无权 chunk
- admin 绕过权限过滤
- agent 不带 user 时行为不变（向后兼容）
- route_question 精确匹配：仅 "agent" 走 agent，其余默认 rag
- pipeline agent 分支把 user/kb_id 透传给 agent.run
"""

from unittest.mock import MagicMock, patch

from rag.models import Chunk
from rag.user_db import UserDB


def _make_agent(retriever, user=None, kb_id="kb1", user_db=None):
    from rag.agent import RAGAgent

    return RAGAgent(retriever=retriever, db_path=":memory:", user=user, kb_id=kb_id, user_db=user_db)


def _find_retrieve_tool(agent):
    return next(t for t in agent.tools if t.name == "retrieve")


def _make_user_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner = db.create_user("owner_user", "Password1")
    other = db.create_user("other_user", "Password2")
    return db, owner, other


# ---------------------------------------------------------------------------
# retrieve 工具权限过滤
# ---------------------------------------------------------------------------


def test_agent_retrieve_filters_unauthorized_chunks(tmp_path):
    """用户无权文档 A 的 chunk 不出现在 retrieve 工具结果中。"""
    db, owner, other = _make_user_db(tmp_path)
    try:
        db.create_document_permission("secret.txt", "kb1", owner_id=owner, scope="private")
        db.create_document_permission("public.txt", "kb1", owner_id=owner, scope="public")

        secret_chunk = Chunk(text="秘密薪资数据 50000", doc_name="secret.txt", chunk_index=0)
        public_chunk = Chunk(text="公开销售数据 1000", doc_name="public.txt", chunk_index=0)

        retriever = MagicMock()
        retriever.retrieve.return_value = [secret_chunk, public_chunk]

        agent = _make_agent(retriever, user={"id": other, "is_admin": False}, kb_id="kb1", user_db=db)
        result = _find_retrieve_tool(agent).func("薪资数据")
        assert "秘密薪资数据" not in result
        assert "公开销售数据" in result
        assert "secret.txt" not in result
        assert "public.txt" in result
    finally:
        db.close()


def test_agent_retrieve_admin_sees_all(tmp_path):
    """admin 绕过权限过滤。"""
    db, owner, other = _make_user_db(tmp_path)
    try:
        db.create_document_permission("secret.txt", "kb1", owner_id=owner, scope="private")
        secret_chunk = Chunk(text="秘密薪资数据 50000", doc_name="secret.txt", chunk_index=0)
        retriever = MagicMock()
        retriever.retrieve.return_value = [secret_chunk]
        agent = _make_agent(retriever, user={"id": other, "is_admin": True}, kb_id="kb1", user_db=db)
        result = _find_retrieve_tool(agent).func("薪资数据")
        assert "秘密薪资数据" in result
    finally:
        db.close()


def test_agent_without_user_returns_all_chunks():
    """agent 不带 user 时行为不变：不过滤（向后兼容）。"""
    private_chunk = Chunk(text="秘密薪资数据 50000", doc_name="secret.txt", chunk_index=0)
    retriever = MagicMock()
    retriever.retrieve.return_value = [private_chunk]
    agent = _make_agent(retriever)  # user=None, user_db=None
    result = _find_retrieve_tool(agent).func("薪资数据")
    assert "秘密薪资数据" in result


# ---------------------------------------------------------------------------
# route_question 精确判定
# ---------------------------------------------------------------------------


@patch("rag.agent.generate", return_value="agent")
def test_route_question_exact_agent(mock_generate):
    from rag.agent import route_question

    assert route_question("算一下增长率") == "agent"


@patch("rag.agent.generate", return_value="rag")
def test_route_question_exact_rag(mock_generate):
    from rag.agent import route_question

    assert route_question("什么是 RAG") == "rag"


@patch("rag.agent.generate", return_value="agency")
def test_route_question_agency_defaults_to_rag(mock_generate):
    """非精确 'agent'（如 agency）默认走 rag。"""
    from rag.agent import route_question

    assert route_question("agency 分析") == "rag"


@patch("rag.agent.generate", return_value="agents")
def test_route_question_agents_defaults_to_rag(mock_generate):
    """旧子串逻辑会误命中 'agents'，现在默认 rag。"""
    from rag.agent import route_question

    assert route_question("agents 有哪些") == "rag"


@patch("rag.agent.generate", return_value="")
def test_route_question_empty_defaults_to_rag(mock_generate):
    """空结果默认 rag（fail-closed）。"""
    from rag.agent import route_question

    assert route_question("随便什么") == "rag"


@patch("rag.agent.generate", return_value="  agent  ")
def test_route_question_strip_whitespace(mock_generate):
    """前后空白被 strip 后精确匹配 agent。"""
    from rag.agent import route_question

    assert route_question("算一下增长率") == "agent"


# ---------------------------------------------------------------------------
# pipeline agent 分支透传 user/kb_id
# ---------------------------------------------------------------------------


def _make_pipeline(user_db=None):
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Reranker"), \
         patch("rag.pipeline.Retriever") as mock_ret, \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.embed"), \
         patch("rag.pipeline.chunk"), \
         patch("rag.pipeline.load"):
        mock_ret.return_value = MagicMock()
        return RAGPipeline("test.txt", kb_id="test_kb", memory_db_path=":memory:", user_db=user_db)


@patch("rag.agent.route_question", return_value="agent")
def test_pipeline_agent_route_passes_user_kb_to_run(mock_route):
    """agent 分支把 user/kb_id 透传给 agent.run。"""
    p = _make_pipeline()
    user = {"id": 1, "is_admin": False}
    with patch.object(p._cache, "get", return_value=None) as mock_get, \
         patch.object(p.agent, "run", return_value="agent answer") as mock_run:
        result = p.query("算一下增长率", user=user, kb_id="kb1")
    assert result.answer == "agent answer"
    mock_run.assert_called_once_with("算一下增长率", user=user, kb_id="kb1")


@patch("rag.agent.route_question", return_value="agent")
def test_pipeline_agent_route_without_user_backward_compat(mock_route):
    """无 user/kb_id 时保持旧调用签名 run(question)。"""
    p = _make_pipeline()
    with patch.object(p._cache, "get", return_value=None) as mock_get, \
         patch.object(p.agent, "run", return_value="agent answer") as mock_run:
        result = p.query("算一下增长率")
    assert result.answer == "agent answer"
    mock_run.assert_called_once_with("算一下增长率")


# ---------------------------------------------------------------------------
# run 的 per-invocation 权限捕获 + regenerate 路径
# ---------------------------------------------------------------------------


@patch("rag.agent._check_answer_quality", return_value={"verdict": "pass", "missing": []})
def test_run_captures_user_per_invocation_and_filters_private_chunk(mock_check, tmp_path):
    """run(user=...) 捕获 user 到线程局部，retrieve 工具过滤无权 chunk（regenerate 安全核心）。"""
    from rag.agent import RAGAgent

    db, owner, other = _make_user_db(tmp_path)
    try:
        db.create_document_permission("secret.txt", "kb1", owner_id=owner, scope="private")
        db.create_document_permission("public.txt", "kb1", owner_id=owner, scope="public")

        secret_chunk = Chunk(text="秘密薪资数据 50000", doc_name="secret.txt", chunk_index=0)
        public_chunk = Chunk(text="公开销售数据 1000", doc_name="public.txt", chunk_index=0)
        retriever = MagicMock()
        retriever.retrieve.return_value = [secret_chunk, public_chunk]

        agent = _make_agent(retriever, kb_id="kb1", user_db=db)
        retrieve_tool = _find_retrieve_tool(agent)

        def fake_invoke(messages, config=None):
            question = messages["messages"][-1].content
            out = retrieve_tool.func(question)
            from langchain_core.messages import HumanMessage

            return {"messages": [HumanMessage(content=out)]}

        with patch.object(agent.agent, "invoke", side_effect=fake_invoke):
            result = agent.run("薪资数据是多少", user={"id": other, "is_admin": False}, kb_id="kb1")

        assert "秘密薪资数据" not in result
        assert "公开销售数据" in result
        assert "secret.txt" not in result
    finally:
        db.close()


def _make_regenerate_pipeline(user_db, retriever):
    """构造不索引、不连 Qdrant 的 RAGPipeline，注入真实 retriever（含真实 chunk）。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Reranker"), \
         patch("rag.pipeline.Retriever") as mock_ret, \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.embed"), \
         patch("rag.pipeline.chunk"), \
         patch("rag.pipeline.load"):
        mock_ret.return_value = retriever
        return RAGPipeline("test.txt", kb_id="rag_docs", memory_db_path=":memory:", user_db=user_db)


def test_regenerate_agent_route_passes_user_kb_to_run(tmp_path):
    """regenerate 走 agent 路由时把 user/kb_id 透传给 agent.run（回归 Critical #1）。"""
    import rag.api as api_module
    from pathlib import Path

    from fastapi.testclient import TestClient
    from rag.auth import create_token
    from rag.user_db import UserDB

    db = UserDB(str(tmp_path / "users.db"))
    try:
        low = db.create_user("low_user", "Password2")
        conv_id = db.create_conversation(low)
        db.add_message(conv_id, "user", "计算一下增长率")
        assistant_id = db.add_message(conv_id, "assistant", "旧答案")

        p = _make_pipeline(user_db=db)
        token = create_token({"user_id": low, "username": "low_user"})
        headers = {"Authorization": f"Bearer {token}"}

        original_user_db = api_module.user_db
        original_data_dir = api_module.DATA_DIR
        api_module.pipeline = p
        api_module.user_db = db
        api_module.DATA_DIR = Path(tmp_path) / "no_such_dir"  # 让 startup 提前返回
        client = TestClient(api_module.app)
        try:
            with patch.object(p, "_prepare_context", return_value=(
                    {"route": "agent", "question": "计算一下增长率", "user": {"id": low}, "kb_id": "rag_docs"}, None)), \
                 patch.object(p.agent, "run", return_value="新答案") as mock_run:
                resp = client.post(
                    "/regenerate",
                    json={"conversation_id": conv_id, "message_id": assistant_id},
                    headers=headers,
                )
        finally:
            api_module.pipeline = None
            api_module.user_db = original_user_db
            api_module.DATA_DIR = original_data_dir

        assert resp.status_code == 200
        assert resp.json()["answer"] == "新答案"
        mock_run.assert_called_once_with("计算一下增长率", user={"id": low}, kb_id="rag_docs")
    finally:
        db.close()


@patch("rag.agent._check_answer_quality", return_value={"verdict": "pass", "missing": []})
def test_regenerate_agent_route_filters_private_chunk(mock_check, tmp_path):
    """低权限用户 regenerate 走 agent 路由，retrieve 结果不含无权私有 chunk（回归 Critical #1）。"""
    import rag.api as api_module
    from pathlib import Path

    from fastapi.testclient import TestClient
    from rag.auth import create_token
    from rag.user_db import UserDB

    db = UserDB(str(tmp_path / "users.db"))
    try:
        owner = db.create_user("owner_user", "Password1")
        low = db.create_user("low_user", "Password2")
        db.create_document_permission("secret.txt", "rag_docs", owner_id=owner, scope="private")
        db.create_document_permission("public.txt", "rag_docs", owner_id=owner, scope="public")
        conv_id = db.create_conversation(low)
        db.add_message(conv_id, "user", "计算一下薪资增长率")
        assistant_id = db.add_message(conv_id, "assistant", "旧答案")

        secret_chunk = Chunk(text="秘密薪资数据 50000", doc_name="secret.txt", chunk_index=0)
        public_chunk = Chunk(text="公开销售数据 1000", doc_name="public.txt", chunk_index=0)
        retriever = MagicMock()
        retriever.retrieve.return_value = [secret_chunk, public_chunk]

        p = _make_regenerate_pipeline(db, retriever)
        retrieve_tool = _find_retrieve_tool(p.agent)

        def fake_invoke(messages, config=None):
            question = messages["messages"][-1].content
            out = retrieve_tool.func(question)
            from langchain_core.messages import HumanMessage

            return {"messages": [HumanMessage(content=out)]}

        token = create_token({"user_id": low, "username": "low_user"})
        headers = {"Authorization": f"Bearer {token}"}

        original_user_db = api_module.user_db
        original_data_dir = api_module.DATA_DIR
        api_module.pipeline = p
        api_module.user_db = db
        api_module.DATA_DIR = Path(tmp_path) / "no_such_dir"  # 让 startup 提前返回
        client = TestClient(api_module.app)
        try:
            with patch("rag.agent.route_question", return_value="agent"), \
                 patch.object(p.agent.agent, "invoke", side_effect=fake_invoke):
                resp = client.post(
                    "/regenerate",
                    json={"conversation_id": conv_id, "message_id": assistant_id},
                    headers=headers,
                )
        finally:
            api_module.pipeline = None
            api_module.user_db = original_user_db
            api_module.DATA_DIR = original_data_dir

        assert resp.status_code == 200
        answer = resp.json()["answer"]
        assert "秘密薪资数据" not in answer
        assert "公开销售数据" in answer
        assert "secret.txt" not in answer
    finally:
        db.close()
