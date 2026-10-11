"""检索前改写带上多轮上下文（默认关闭）。

动机（2026-10-11 核验审计条目）：`rewrite_query(question)` **只吃当轮问题**，
所以"它呢？/那这个多少钱？"这类指代会被**无上下文改写**。回答生成那条路本来就有上下文
（`self.memory.build_messages`），缺口只在**检索前的改写**这一处。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import rag.pipeline as pipeline_mod
from config import settings
from rag.query_rewriter import rewrite_query


def test_default_behaviour_is_unchanged(monkeypatch):
    """默认 0 轮 ⇒ 调用方式与既有代码**逐字节一致**（不带 history/summary）。"""
    calls = []

    def fake_rewrite(question, **kwargs):
        calls.append((question, kwargs))
        return question

    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0, raising=False)
    monkeypatch.setattr(pipeline_mod, "rewrite_query", fake_rewrite, raising=True)

    pipeline = object.__new__(pipeline_mod.RAGPipeline)
    pipeline.memory = SimpleNamespace(  # 不该被碰到
        get_recent_messages=lambda *a, **k: pytest.fail("默认关闭时不应读取会话历史"),
        get_summary=lambda *a, **k: pytest.fail("默认关闭时不应读取摘要"),
    )

    assert pipeline._rewrite_for_retrieval("年假是多少", "sid-1") == "年假是多少"
    assert calls == [("年假是多少", {})], "默认路径必须与既有行为完全一致"


def test_history_is_passed_when_enabled(monkeypatch):
    """开启后：把最近几轮（含会话摘要）交给改写，用于补全指代。"""
    calls = []

    def fake_rewrite(question, **kwargs):
        calls.append((question, kwargs))
        return f"改写:{question}"

    monkeypatch.setattr(settings, "query_rewrite_history_turns", 2, raising=False)
    monkeypatch.setattr(pipeline_mod, "rewrite_query", fake_rewrite, raising=True)

    pipeline = object.__new__(pipeline_mod.RAGPipeline)
    pipeline.memory = SimpleNamespace(
        get_recent_messages=lambda sid, n=None: [
            SimpleNamespace(role="user", content="适马的镜头多少钱"),
            SimpleNamespace(role="assistant", content="LH878-01 是 ￥1280"),
        ],
        get_summary=lambda sid: "用户在问适马镜头价格",
    )

    result = pipeline._rewrite_for_retrieval("它呢？", "sid-1")

    assert result == "改写:它呢？"
    question, kwargs = calls[0]
    assert question == "它呢？"
    assert kwargs["history"] == [("user", "适马的镜头多少钱"), ("assistant", "LH878-01 是 ￥1280")]
    assert kwargs["summary"] == "用户在问适马镜头价格"


def test_a_broken_memory_falls_back_to_the_old_behaviour(monkeypatch):
    """记忆读不到就退回既有行为 —— 改写绝不能因为历史而失败。"""
    calls = []
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 3, raising=False)
    monkeypatch.setattr(pipeline_mod, "rewrite_query",
                        lambda question, **kwargs: calls.append(kwargs) or question, raising=True)

    def boom(*_a, **_k):
        raise RuntimeError("memory down")

    pipeline = object.__new__(pipeline_mod.RAGPipeline)
    pipeline.memory = SimpleNamespace(get_recent_messages=boom, get_summary=boom)

    assert pipeline._rewrite_for_retrieval("它呢？", "sid-1") == "它呢？"
    assert calls == [{}], "读不到历史时也必须按旧方式调用（不带历史）"


def test_the_prompt_carries_the_history(monkeypatch):
    """历史确实进入了改写提示词（而不只是被传进函数）。"""
    prompts = []

    def fake_generate(messages):
        prompts.append(messages[-1]["content"])
        return "适马 LH878-01 的价格是多少"

    import rag.generator as generator_mod

    monkeypatch.setattr(generator_mod, "generate", fake_generate, raising=True)

    rewrite_query("它呢？", history=[("user", "适马的镜头多少钱")], summary="在问适马镜头")

    assert "适马的镜头多少钱" in prompts[0]
    assert "在问适马镜头" in prompts[0]
    assert "它呢？" in prompts[0]
