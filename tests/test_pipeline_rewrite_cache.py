"""检索前改写缓存：只缓存改变了问题的结果，key 含历史签名。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import rag.pipeline as pipeline_mod
from config import settings
from rag.pipeline import RAGPipeline


def _pipeline(memory=None) -> RAGPipeline:
    pipeline = object.__new__(RAGPipeline)
    pipeline.memory = memory or SimpleNamespace(
        get_recent_messages=lambda *a, **k: pytest.fail("默认关闭时不应读取会话历史"),
        get_summary=lambda *a, **k: pytest.fail("默认关闭时不应读取摘要"),
    )
    return pipeline


def _rewriter(monkeypatch, produce):
    monkeypatch.setattr(pipeline_mod, "rewrite_query", produce, raising=True)


def test_same_question_hits_the_cache(monkeypatch):
    calls: list[str] = []

    def fake(question, **kwargs):
        calls.append(question)
        return f"改写:{question}"

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)

    pipeline = _pipeline()
    assert pipeline._rewrite_for_retrieval("Q", "s1") == "改写:Q"
    assert pipeline._rewrite_for_retrieval("Q", "s2") == "改写:Q"
    assert calls == ["Q"], "同一问题命中缓存后不应再调模型"


def test_different_questions_do_not_share(monkeypatch):
    calls: list[str] = []

    def fake(question, **kwargs):
        calls.append(question)
        return f"改写:{question}"

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)

    pipeline = _pipeline()
    pipeline._rewrite_for_retrieval("Q1", "s")
    pipeline._rewrite_for_retrieval("Q2", "s")
    assert calls == ["Q1", "Q2"]


def test_an_unchanged_rewrite_is_not_cached(monkeypatch):
    calls: list[str] = []

    def fake(question, **kwargs):
        calls.append(question)
        return question  # 模型失败/原句已足够好时的退化结果

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)

    pipeline = _pipeline()
    pipeline._rewrite_for_retrieval("Q", "s")
    pipeline._rewrite_for_retrieval("Q", "s")
    assert calls == ["Q", "Q"], "未改变问题的结果不缓存，瞬时失败才有重试机会"


def test_cache_can_be_disabled(monkeypatch):
    calls: list[str] = []

    def fake(question, **kwargs):
        calls.append(question)
        return f"改写:{question}"

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", False, raising=False)

    pipeline = _pipeline()
    pipeline._rewrite_for_retrieval("Q", "s")
    pipeline._rewrite_for_retrieval("Q", "s")
    assert calls == ["Q", "Q"]


def test_history_signature_separates_turns(monkeypatch):
    calls: list[dict] = []
    state = {"history": []}

    def fake(question, **kwargs):
        calls.append(kwargs)
        return f"改写:{question}:{len(calls)}"

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 2, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)

    memory = SimpleNamespace(
        get_recent_messages=lambda sid, n=None: state["history"],
        get_summary=lambda sid: "摘要",
    )
    pipeline = _pipeline(memory)

    state["history"] = [SimpleNamespace(role="user", content="问甲")]
    first = pipeline._rewrite_for_retrieval("它呢？", "s")
    state["history"] = [SimpleNamespace(role="user", content="问乙")]
    second = pipeline._rewrite_for_retrieval("它呢？", "s")
    assert first != second
    assert len(calls) == 2

    state["history"] = [SimpleNamespace(role="user", content="问甲")]
    assert pipeline._rewrite_for_retrieval("它呢？", "s") == first
    assert len(calls) == 2, "同一问题 + 同一历史必须命中缓存"


def test_reading_memory_failure_is_not_cached(monkeypatch):
    calls: list[str] = []
    state = {"fail": True}

    def fake(question, **kwargs):
        calls.append(question)
        return f"改写:{question}"

    def recent(sid, n=None):
        if state["fail"]:
            raise RuntimeError("memory down")
        return []

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 2, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)

    pipeline = _pipeline(SimpleNamespace(get_recent_messages=recent, get_summary=lambda sid: ""))
    pipeline._rewrite_for_retrieval("Q", "s")
    state["fail"] = False
    pipeline._rewrite_for_retrieval("Q", "s")
    assert calls == ["Q", "Q"], "退化路径不能污染缓存"


def test_lru_evicts_the_oldest(monkeypatch):
    calls: list[str] = []

    def fake(question, **kwargs):
        calls.append(question)
        return f"改写:{question}"

    _rewriter(monkeypatch, fake)
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_size", 2, raising=False)

    pipeline = _pipeline()
    for question in ("Q1", "Q2", "Q3", "Q1"):
        pipeline._rewrite_for_retrieval(question, "s")
    assert calls == ["Q1", "Q2", "Q3", "Q1"]
    assert len(pipeline._rewrite_cache) == 2


def test_no_memory_read_when_history_is_disabled(monkeypatch):
    _rewriter(monkeypatch, lambda question, **kwargs: f"改写:{question}")
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True, raising=False)

    pipeline = _pipeline()  # memory 的两个方法都是 pytest.fail
    pipeline._rewrite_for_retrieval("Q", "s")
    pipeline._rewrite_for_retrieval("Q", "s")
