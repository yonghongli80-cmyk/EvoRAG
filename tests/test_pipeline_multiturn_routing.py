"""有历史的澄清路由必须重新判一次：指代补全之后再决定往哪走。

2026-10-13 的多轮基准实测（development-log 第 18 节）抓到一条真实回归：追问本身不含先行词时
（"那一步具体是怎么做的？"），Jev 路径在**路由层**就判 `clarification` 直接返回，7 例只对 1 例，
而旧路径 7/7 —— 因为路由层只看当前这一句、看不到前几轮。这里的判据是：

* 有历史且历史改变了问法、改写后不再要澄清 ⇒ 必须按新路由继续（旧行为是直接返回澄清）；
* 没有历史 ⇒ **一次都不多花**（不额外调用路由，原样返回原决策）；
* 历史没能改变问法（改写结果相同）⇒ 不重新路由，仍返回澄清；
* 改写不可用（异常）⇒ 退回原决策，不把"补全失败"变成"路由失败"。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from config import settings
from rag.jev.models import PreflightDecision
from rag.jev.policy import PassageSelection, RouteDecision
from rag.pipeline import RAGPipeline


def route(kind: str, *, should_rewrite: bool = False) -> RouteDecision:
    return RouteDecision(
        route=kind,
        should_rewrite=should_rewrite,
        reason=f"{kind}_reason",
        preflight=PreflightDecision(
            intent="retrieval_qa",
            intent_confidence=0.95,
            complexity="simple",
            complexity_confidence=0.91,
            rewrite_probability=0.1,
            model="jev-latest",
            question_set_version="preflight-v1",
            duration_ms=1.0,
        ),
    )


def build_pipeline(jev_service) -> RAGPipeline:
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
            jev_service=jev_service,
        )
    pipeline._feedback_processor.get_weight.return_value = 1.0
    pipeline._feedback_processor.get_weights.return_value = {}
    # 判定层返回真实的 PassageSelection（空证据）而不是 MagicMock：mock 的 `.judged` 是 MagicMock，
    # 会在 `max(stage[...], selection.judged)` 上炸——那测的就不是路由了。
    jev_service.select.return_value = PassageSelection(
        accepted=(), conflicts=(), excluded_ids=(), has_answer_evidence=False
    )
    return pipeline


@pytest.fixture
def active_jev(monkeypatch):
    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    # 多轮改写打开：修正逻辑只在改写真的能带历史时才有意义（默认 0 时行为一字不变，
    # 那条路径由下面 no-history 的用例覆盖）。
    monkeypatch.setattr(settings, "query_rewrite_history_turns", 1)


def _seed_history(pipeline, session_id: str, turns: int = 1) -> None:
    for index in range(turns):
        pipeline.memory.add_message(session_id, "user", f"第 {index} 轮的问题")
        pipeline.memory.add_message(session_id, "assistant", f"第 {index} 轮的回答")


def test_a_clarifying_follow_up_with_history_is_re_routed_after_the_rewrite(active_jev):
    jev = MagicMock()
    jev.route.side_effect = [route("clarification"), route("rag")]
    pipeline = build_pipeline(jev)
    _seed_history(pipeline, "s1", turns=2)

    with patch("rag.pipeline.rewrite_query", return_value="补全后的完整问题") as mock_rewrite, \
         patch("rag.pipeline.generate", return_value="answer"):
        pipeline.query("那一步具体是怎么做的？", session_id="s1")

    rewritten, *_ = mock_rewrite.call_args.args
    assert rewritten == "那一步具体是怎么做的？"
    assert mock_rewrite.call_args.kwargs.get("history"), "改写必须带上历史"
    assert jev.route.call_count == 2
    assert jev.route.call_args_list[0].args[0] == "那一步具体是怎么做的？"
    assert jev.route.call_args_list[1].args[0] == "补全后的完整问题"


def test_no_history_means_no_extra_route_call(active_jev):
    """单轮（或没开多轮）：澄清就是澄清，一个字都不变，也不多花一次路由。"""
    jev = MagicMock()
    jev.route.return_value = route("clarification")
    pipeline = build_pipeline(jev)

    with patch("rag.pipeline.rewrite_query") as mock_rewrite, \
         patch("rag.pipeline.generate") as mock_generate:
        pipeline.query("那个怎么弄？", session_id="no-history")

    assert jev.route.call_count == 1
    mock_rewrite.assert_not_called()
    mock_generate.assert_not_called()


def test_a_rewrite_that_changes_nothing_keeps_the_clarification(active_jev):
    """历史没能改变问法 ⇒ 不重新路由：避免用"同样的问法"再问一次模型。"""
    jev = MagicMock()
    jev.route.return_value = route("clarification")
    pipeline = build_pipeline(jev)
    _seed_history(pipeline, "s1")

    with patch("rag.pipeline.rewrite_query", return_value="那一步具体是怎么做的？"), \
         patch("rag.pipeline.generate") as mock_generate:
        pipeline.query("那一步具体是怎么做的？", session_id="s1")

    assert jev.route.call_count == 1
    mock_generate.assert_not_called()


def test_a_failed_rewrite_falls_back_to_the_original_clarification(active_jev):
    jev = MagicMock()
    jev.route.return_value = route("clarification")
    pipeline = build_pipeline(jev)
    _seed_history(pipeline, "s1")

    with patch("rag.pipeline.rewrite_query", side_effect=RuntimeError("provider down")), \
         patch("rag.pipeline.generate") as mock_generate:
        pipeline.query("那一步具体是怎么做的？", session_id="s1")

    assert jev.route.call_count == 1  # 没有可用的补全结果，就不重新路由
    mock_generate.assert_not_called()


def test_a_confident_route_with_history_still_routes_once(active_jev):
    """本来就不需要澄清的追问：不额外调用路由（历史不改变"已经明确"的判定）。"""
    jev = MagicMock()
    jev.route.return_value = route("rag")
    pipeline = build_pipeline(jev)
    _seed_history(pipeline, "s1")

    with patch("rag.pipeline.generate", return_value="answer"):
        pipeline.query("大模型幻觉分几类？", session_id="s1")

    assert jev.route.call_count == 1
