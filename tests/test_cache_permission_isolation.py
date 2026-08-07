"""缓存 key 隔离 + 缓存命中权限过滤测试。

验证：
- 同一 question 在不同 kb_id 下 get/set 互不影响
- 同一 question 在不同 user_id 下互不影响
- pipeline 缓存命中后，对无权 doc_name 的 sources 被过滤
- 旧的单参数调用（无 kb_id/user）不崩
"""

from unittest.mock import MagicMock, patch

import pytest

from rag.redis_cache import RedisCache
from rag.user_db import UserDB


@pytest.fixture
def mem_cache():
    """强制走内存降级缓存，避免触碰运行中的 Redis。"""
    cache = RedisCache()
    with patch.object(cache, "_get_redis", return_value=None):
        yield cache


def _make_pipeline(user_db=None):
    """构造不索引、不连 Qdrant 的 RAGPipeline。"""
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


# ---------------------------------------------------------------------------
# 缓存 key 隔离
# ---------------------------------------------------------------------------


def test_same_question_different_kb_isolated(mem_cache):
    """先 set(kb1) 再 get(kb2) 返回 None。"""
    mem_cache.set("question", "kb1 answer", kb_id="kb1", user_id="u1")
    assert mem_cache.get("question", kb_id="kb1", user_id="u1")["answer"] == "kb1 answer"
    assert mem_cache.get("question", kb_id="kb2", user_id="u1") is None


def test_same_question_different_user_isolated(mem_cache):
    """同一 question 在不同 user_id 下互不影响。"""
    mem_cache.set("question", "user1 answer", kb_id="kb1", user_id="u1")
    assert mem_cache.get("question", kb_id="kb1", user_id="u1")["answer"] == "user1 answer"
    assert mem_cache.get("question", kb_id="kb1", user_id="u2") is None


def test_cache_key_contains_kb_and_user():
    """_key 生成的 key 应包含 kb_id 与 user_id，仍以 rag:query: 开头。"""
    cache = RedisCache()
    key = cache._key("question", "kb1", "u1")
    assert key.startswith("rag:query:kb1:u1:")
    assert key != cache._key("question", "kb2", "u1")
    assert key != cache._key("question", "kb1", "u2")
    assert key == cache._key("question", "kb1", "u1")


def test_clear_works_with_new_key_format(mem_cache):
    """clear() 能清掉新格式（rag:query:kb:user:md5）的 key。"""
    mem_cache.set("question", "answer", kb_id="kb1", user_id="u1")
    assert mem_cache.get("question", kb_id="kb1", user_id="u1") is not None
    mem_cache.clear()
    assert mem_cache.get("question", kb_id="kb1", user_id="u1") is None


# ---------------------------------------------------------------------------
# 旧调用兼容
# ---------------------------------------------------------------------------


def test_legacy_single_arg_calls_do_not_crash(mem_cache):
    """旧的单参数调用（无 kb_id/user）不崩。"""
    mem_cache.set("question", "legacy answer")
    data = mem_cache.get("question")
    assert data is not None
    assert data["answer"] == "legacy answer"
    mem_cache.delete("question")
    assert mem_cache.get("question") is None


# ---------------------------------------------------------------------------
# pipeline 缓存命中权限过滤
# ---------------------------------------------------------------------------


def test_pipeline_cache_get_uses_kb_and_user():
    """pipeline 读缓存时使用 kb_id + user_id 维度。"""
    p = _make_pipeline()
    with patch.object(p._cache, "get", return_value=None) as mock_get, \
         patch("rag.agent.route_question", return_value="agent"):
        user = {"id": "u1"}
        p._prepare_context("question", "s1", None, user=user, kb_id="kb1")
    mock_get.assert_called_once_with("question", "kb1", "u1")


def test_pipeline_cache_hit_filters_unauthorized_sources():
    """缓存命中后 filter_chunks_by_permission 被调用，无权 doc_name 的 sources 被过滤。"""
    p = _make_pipeline(user_db=MagicMock())
    with patch.object(p._cache, "_get_redis", return_value=None):
        p._cache.set(
            "cached question", "cached answer", kb_id="kb1", user_id="u1",
            sources=[
                {"doc_name": "private.txt", "chunk_index": 0},
                {"doc_name": "public.txt", "chunk_index": 1},
            ],
        )
        user = {"id": "u1", "is_admin": False}

        def fake_filter(db, kb_id, chunks, user):
            return [c for c in chunks if c.doc_name != "private.txt"]

        with patch("rag.permissions.filter_chunks_by_permission", side_effect=fake_filter) as mock_filter:
            result, error = p._prepare_context("cached question", "s1", None, user=user, kb_id="kb1")

    assert error is None
    assert result["route"] == "cached"
    assert result["answer"] == "cached answer"
    assert [s["doc_name"] for s in result["sources"]] == ["public.txt"]
    mock_filter.assert_called_once()


def test_pipeline_cache_hit_real_userdb_filters(tmp_path):
    """真实 UserDB：缓存命中的 private 文档 source 被过滤，public / 无权限记录保留。"""
    db = UserDB(str(tmp_path / "users.db"))
    try:
        owner = db.create_user("owner_user", "Password1")
        other = db.create_user("other_user", "Password2")
        db.create_document_permission("secret.txt", "kb1", owner_id=owner, scope="private")
        db.create_document_permission("public.txt", "kb1", owner_id=owner, scope="public")

        p = _make_pipeline(user_db=db)
        with patch.object(p._cache, "_get_redis", return_value=None):
            p._cache.set(
                "cached question", "cached answer", kb_id="kb1", user_id=other,
                sources=[
                    {"doc_name": "secret.txt", "chunk_index": 0},
                    {"doc_name": "public.txt", "chunk_index": 1},
                    {"doc_name": "legacy.txt", "chunk_index": 2},  # 无权限记录 → 旧文档，保留
                ],
            )
            user = {"id": other, "is_admin": False}
            result, error = p._prepare_context("cached question", "s1", None, user=user, kb_id="kb1")

        assert error is None
        assert result["route"] == "cached"
        assert [s["doc_name"] for s in result["sources"]] == ["public.txt", "legacy.txt"]
    finally:
        db.close()
