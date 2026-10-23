"""A-07：企业问答不再无条件多花一次路由模型调用；agent 文案与 SQL 工具来源要准确。

2026-10-22 审查：

* enterprise 下 ``route_question`` 的结果只有 ``agent`` 一个分支有用，而那个分支本身固定拒答
  ——于是每条企业问答都白付一次路由模型调用，且它还会无谓地放大尾延迟。
* 企业模式下 agent 分支的回答文案是"资料存在冲突或范围不明确"，与真实原因（需要工具执行）
  不符。
* ``RAGAgent`` 默认把 SQL 工具绑到 ``:memory:``：每条连接都是新的空库，工具永远查不到东西，
  却让模型以为手上有可用的 SQL 数据源。
"""

import sqlite3
from unittest.mock import MagicMock, patch

from rag.agent import RAGAgent, create_agent_tools
from rag.pipeline import RAGPipeline
from rag.user_db import UserDB

# ── SQL 工具：明确来源或禁用 ────────────────────────────────────────────────


def test_sql_tool_is_disabled_without_an_explicit_data_source():
    tools = create_agent_tools(retriever=MagicMock(), db_path=None)

    assert "sql_query" not in [tool.name for tool in tools]
    assert [tool.name for tool in tools] == ["retrieve", "calculate", "plot_chart"]


def test_memory_path_is_not_a_data_source():
    """``:memory:`` 每条连接都是空库：它必须被当成"没有来源"，而不是一个可查的库。"""
    tools = create_agent_tools(retriever=MagicMock(), db_path=":memory:")

    assert "sql_query" not in [tool.name for tool in tools]


def test_sql_tool_is_registered_for_an_existing_database(tmp_path):
    db_path = tmp_path / "imported.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sales (amount INTEGER)")
        conn.execute("INSERT INTO sales VALUES (42)")

    tools = create_agent_tools(retriever=MagicMock(), db_path=str(db_path))

    names = [tool.name for tool in tools]
    assert "sql_query" in names
    sql_tool = next(tool for tool in tools if tool.name == "sql_query")
    assert "42" in sql_tool.func("SELECT amount FROM sales")


def test_rag_agent_defaults_to_no_sql_source():
    agent = RAGAgent(retriever=MagicMock())

    assert agent.db_path is None
    assert "sql_query" not in [tool.name for tool in agent.tools]


def test_agent_registers_four_tools_when_a_real_source_is_given(tmp_path):
    db_path = tmp_path / "imported.db"
    db_path.write_bytes(b"")

    agent = RAGAgent(retriever=MagicMock(), db_path=str(db_path))

    assert [tool.name for tool in agent.tools] == [
        "retrieve",
        "calculate",
        "sql_query",
        "plot_chart",
    ]


# ── enterprise：跳过路由调用 + 准确的工具分支文案 ───────────────────────────


def _build_pipeline(**kwargs):
    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls:
        retriever_cls.return_value.retrieve.return_value = []
        reranker_cls.return_value.rerank.side_effect = lambda _query, chunks: chunks
        pipeline = RAGPipeline(
            "test.txt",
            session_id="s1",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            **kwargs,
        )
    pipeline._feedback_processor.get_weight.return_value = 1.0
    pipeline._feedback_processor.get_weights.return_value = {}
    return pipeline


def _create_enterprise_version_db(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    owner_id = db.create_user("enterprise-owner", "Password-123!")
    document_id = db.create_document_permission("policy.md", "kb1", owner_id)
    file_path = tmp_path / "policy.md"
    file_path.write_text("policy", encoding="utf-8")
    version_id = db.create_document_version(
        document_id, "policy.md", str(file_path), "policy-hash", owner_id
    )
    db.activate_document_version(document_id, version_id, "kb1", "policy.md")
    return db, document_id, version_id


def test_enterprise_requests_skip_the_legacy_router_model_call(monkeypatch, tmp_path):
    from config import settings
    from rag.models import Chunk

    monkeypatch.setattr(settings, "jev_mode", "off")
    user = {"id": 7, "permission_level": 1, "is_admin": True}
    user_db, document_id, version_id = _create_enterprise_version_db(tmp_path)
    chunk = Chunk(
        text="年假为 7 天", doc_name="policy.md", chunk_index=0,
        document_id=document_id, document_version_id=version_id,
        rerank_score=0.9, score_source="reranker",
    )
    pipeline = _build_pipeline(kb_id="kb1", user_db=user_db)
    pipeline.retriever.retrieve.return_value = [chunk]

    with patch("rag.agent.route_question") as mock_route, \
         patch("rag.pipeline.rewrite_query", return_value="q"), \
         patch("rag.pipeline.generate", return_value="7 天"):
        result = pipeline.query("年假有几天", user=user, kb_id="kb1")

    mock_route.assert_not_called()
    assert result.answer == "7 天"
    pipeline.close()
    user_db.close()


def test_non_enterprise_requests_still_use_the_router(monkeypatch):
    from config import settings
    from rag.models import Chunk

    monkeypatch.setattr(settings, "jev_mode", "off")
    chunk = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    pipeline = _build_pipeline()
    pipeline.retriever.retrieve.return_value = [chunk]

    with patch("rag.agent.route_question", return_value="rag") as mock_route, \
         patch("rag.pipeline.rewrite_query", return_value="q"), \
         patch("rag.pipeline.generate", return_value="7 天"):
        pipeline.query("年假有几天")

    mock_route.assert_called_once()


def test_enterprise_agent_route_is_not_reachable_so_the_copy_is_not_emitted(monkeypatch, tmp_path):
    """企业模式跳过路由后，legacy 的 agent 分支不再产生"资料冲突"式拒答。"""
    from config import settings
    from rag.models import Chunk

    monkeypatch.setattr(settings, "jev_mode", "off")
    user_db, document_id, version_id = _create_enterprise_version_db(tmp_path)
    chunk = Chunk(
        text="年假为 7 天", doc_name="policy.md", chunk_index=0,
        document_id=document_id, document_version_id=version_id,
        rerank_score=0.9, score_source="reranker",
    )
    pipeline = _build_pipeline(kb_id="kb1", user_db=user_db)
    pipeline.retriever.retrieve.return_value = [chunk]

    with patch("rag.agent.route_question", return_value="agent") as mock_route, \
         patch("rag.pipeline.rewrite_query", return_value="q"), \
         patch("rag.pipeline.generate", return_value="7 天") as mock_generate, \
         patch.object(pipeline.agent, "run") as agent_run:
        prepared, error = pipeline._prepare_context(
            "帮我算出同比增长率", "s1", None,
            user={"id": 7, "is_admin": True}, kb_id="kb1",
        )

    assert error is None
    mock_route.assert_not_called()
    assert prepared["route"] == "rag"
    assert "冲突" not in (prepared.get("answer") or "")
    agent_run.assert_not_called()
    mock_generate.assert_not_called()
    pipeline.close()
    user_db.close()


def test_jev_tool_route_in_enterprise_uses_the_same_accurate_copy(monkeypatch):
    from config import settings
    from rag.jev.policy import RouteDecision

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "kb1")
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="tool", should_rewrite=False, reason="tool_confident", preflight=None
    )
    pipeline = _build_pipeline(kb_id="kb1", user_db=MagicMock(), jev_service=jev)

    with patch("rag.pipeline.generate") as mock_generate:
        prepared, error = pipeline._prepare_context(
            "帮我算出同比增长率", "s1", None,
            user={"id": 7, "is_admin": True}, kb_id="kb1",
        )

    assert error is None
    assert prepared["route"] == "fixed"
    assert "工具" in prepared["answer"]
    mock_generate.assert_not_called()
