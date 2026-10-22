"""A-01/A-02：熔断必须是可识别失败，而不是一句会被当成答案的文案。

缺陷背景（2026-10-22 全库评审 P1-01/P1-02）：

* ``generate`` / ``generate_stream`` 与池内 provider 在熔断时返回
  ``"系统繁忙，请稍后重试。"``；``rewrite_query`` 原样返回该文案，pipeline 的
  ``_rewrite_cached`` 又把它当成"改变了的改写"永久缓存 → 该问题此后检索词永远
  是这句降级文案。
* 同一条降级文案在同步路径被当成正式答案落轨迹、写记忆，并附带 reliability；
  流式路径把它当 token 推给客户端，done 事件仍带 reliability。运维/演化看不到失败。

这里钉住：熔断抛出 ``CircuitOpenError``；改写缓存不写入降级文案；同步/流式都不写
memory、不附 reliability，并留下失败轨迹与失败观测/跳过记录。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from unittest.mock import MagicMock, patch

import pytest

from rag.reliability.models import AnswerabilityDecision, ConfidenceAssessment
from rag.resilience import DEGRADED_ANSWER_TEXT, CircuitOpenError


def _open_generator_breaker() -> None:
    from rag.generator import _breaker

    _breaker.record_success()
    for _ in range(_breaker.failure_threshold):
        _breaker.record_failure()
    assert _breaker.state == "open"


def _close_generator_breaker() -> None:
    from rag.generator import _breaker

    _breaker.record_success()


@pytest.fixture()
def open_generator_breaker():
    _open_generator_breaker()
    try:
        yield
    finally:
        _close_generator_breaker()


def _prepared(question: str = "年假有几天") -> dict:
    return {
        "route": "rag",
        "question": question,
        "context": [],
        "messages": [{"role": "user", "content": question}],
        "sources": [],
        "sid": "unused",
        "chunk_hashes": [],
        "weights": {},
        "stage_timings": {},
        "decision_metadata": {},
        "decision": AnswerabilityDecision(kind="answerable", reasons=("qualified evidence",)),
        "confidence": ConfidenceAssessment(level="high", reasons=("single current source",)),
    }


def _build_pipeline(tmp_path, recorder=None):
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline(
            kb_id="rag_docs",
            user_db=MagicMock(),
            memory_db_path=str(tmp_path / "memory.db"),
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
            failure_recorder=recorder if recorder is not None else MagicMock(),
        )
    pipeline.tracker = MagicMock()
    return pipeline


def _new_cache(pipeline) -> None:
    pipeline._rewrite_cache = OrderedDict()
    pipeline._rewrite_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 1) 熔断本身：专用异常，而不是字符串
# ---------------------------------------------------------------------------


def test_generate_raises_circuit_open_instead_of_returning_the_busy_copy(open_generator_breaker):
    from rag.generator import generate

    with pytest.raises(CircuitOpenError):
        generate([{"role": "user", "content": "hello"}])


def test_generate_stream_raises_circuit_open_instead_of_yielding_the_busy_copy(
    open_generator_breaker,
):
    from rag.generator import generate_stream

    tokens: list[str] = []

    async def collect():
        async for token in generate_stream([{"role": "user", "content": "hello"}]):
            tokens.append(token)

    with pytest.raises(CircuitOpenError):
        asyncio.run(collect())
    assert tokens == []


def _provider():
    from rag.llm_pool.models import LLMProviderConfig
    from rag.llm_pool.provider import LLMProvider

    config = LLMProviderConfig(
        provider_id="deepseek",
        name="DeepSeek",
        model="deepseek-chat",
        base_url="https://api.example.com/v1",
        api_key="sk-test",
    )
    return LLMProvider(config)


def test_llm_provider_sync_generate_raises_circuit_open_when_its_breaker_is_open():
    provider = _provider()
    for _ in range(provider.circuit_breaker.failure_threshold):
        provider.circuit_breaker.record_failure()

    with pytest.raises(CircuitOpenError):
        provider.generate([{"role": "user", "content": "hello"}])


def test_llm_provider_stream_raises_circuit_open_when_its_breaker_is_open():
    provider = _provider()
    for _ in range(provider.circuit_breaker.failure_threshold):
        provider.circuit_breaker.record_failure()

    tokens: list[str] = []

    async def collect():
        async for token in provider.generate_stream([{"role": "user", "content": "hello"}]):
            tokens.append(token)

    with pytest.raises(CircuitOpenError):
        asyncio.run(collect())
    assert tokens == []


# ---------------------------------------------------------------------------
# 2) 改写缓存：降级文案既不改变问题，也不进入缓存
# ---------------------------------------------------------------------------


def test_circuit_open_rewrite_is_not_cached(tmp_path, monkeypatch, open_generator_breaker):
    from config import settings

    monkeypatch.setattr(settings, "query_rewrite_history_turns", 0)
    monkeypatch.setattr(settings, "query_rewrite_cache_enabled", True)
    pipeline = _build_pipeline(tmp_path)
    pipeline.memory = MagicMock()
    pipeline.memory.get_recent_messages.return_value = []
    pipeline.memory.get_summary.return_value = None
    _new_cache(pipeline)

    rewritten = pipeline._rewrite_for_retrieval("年假有几天", "u7:default")

    assert rewritten == "年假有几天"
    assert pipeline._rewrite_cache == {}
    pipeline.close()


def test_rewrite_cached_never_stores_or_returns_the_degraded_copy(tmp_path):
    pipeline = _build_pipeline(tmp_path)
    _new_cache(pipeline)

    rewritten = pipeline._rewrite_cached("年假有几天", "", lambda: DEGRADED_ANSWER_TEXT)

    assert rewritten == "年假有几天"
    assert pipeline._rewrite_cache == {}
    pipeline.close()


def test_rewrite_cache_rejects_a_previously_poisoned_entry(tmp_path):
    """旧版本写进缓存的降级文案也不能再当改写结果。"""
    pipeline = _build_pipeline(tmp_path)
    _new_cache(pipeline)
    pipeline._rewrite_cache[("年假有几天", "")] = DEGRADED_ANSWER_TEXT

    def produce():
        raise AssertionError("命中毒化缓存时不应重新调用模型")

    rewritten = pipeline._rewrite_cached("年假有几天", "", produce)

    assert rewritten == "年假有几天"
    pipeline.close()


# ---------------------------------------------------------------------------
# 3) 同步路径：抛失败、无 memory 写入、无非正式答案的轨迹
# ---------------------------------------------------------------------------


def test_sync_pipeline_circuit_open_leaves_no_memory_and_records_failure(
    tmp_path, open_generator_breaker
):
    from rag.evolution.models import NonEvolvableReason

    recorder = MagicMock()
    pipeline = _build_pipeline(tmp_path, recorder)
    pipeline._prepare_context = MagicMock(return_value=(_prepared(), None))

    with pytest.raises(CircuitOpenError):
        pipeline.query("年假有几天", user={"id": 7})

    assert pipeline.memory.get_recent_messages("u7:default") == []
    trace = pipeline.tracker.save.call_args.args[0]
    assert trace.route == "error"
    assert trace.answer == ""
    assert trace.question == "年假有几天"
    detail = recorder.submit.call_args.args[1]
    assert detail.non_evolvable_reason is NonEvolvableReason.TRANSIENT_EXTERNAL
    pipeline.close()


def test_stream_pipeline_circuit_open_emits_error_without_memory_or_reliability(
    tmp_path, open_generator_breaker
):
    recorder = MagicMock()
    pipeline = _build_pipeline(tmp_path, recorder)
    pipeline._prepare_context = MagicMock(return_value=(_prepared(), None))
    # providers 为空 ⇒ 走 rag.generator.generate_stream（真正触发熔断的分支）。
    router = MagicMock()
    router.providers = {}
    events: list[dict] = []

    async def collect():
        async for line in pipeline.query_stream("年假有几天", session_id="s1", user={"id": 7}):
            events.append(json.loads(line.removeprefix("data: ")))

    with patch("rag.pipeline.get_llm_pool_router", return_value=router):
        asyncio.run(collect())

    assert [event["type"] for event in events] == ["error"]
    assert events[0]["degraded"] is True
    assert "reliability" not in events[0]
    assert pipeline.memory.get_recent_messages("u7:s1") == []
    trace = pipeline.tracker.save.call_args.args[0]
    assert trace.route == "error"
    assert trace.answer == ""
    recorder.submit.assert_called_once()
    pipeline.close()


def test_sync_pipeline_never_persists_the_degraded_copy(tmp_path):
    """防御旧调用方：即使 generate 仍把降级文案当字符串返回，也不能当答案。"""
    recorder = MagicMock()
    pipeline = _build_pipeline(tmp_path, recorder)
    pipeline._prepare_context = MagicMock(return_value=(_prepared(), None))

    with patch("rag.pipeline.generate", return_value=DEGRADED_ANSWER_TEXT), pytest.raises(
        CircuitOpenError
    ):
        pipeline.query("年假有几天", user={"id": 7})

    assert pipeline.memory.get_recent_messages("u7:default") == []
    assert pipeline.tracker.save.call_args.args[0].route == "error"
    recorder.submit.assert_called_once()
    pipeline.close()


def test_stream_pipeline_never_persists_the_degraded_copy(tmp_path):
    """防御旧 provider：降级文案以 token 流回来时，也只能是 error，不能是答案。"""
    recorder = MagicMock()
    pipeline = _build_pipeline(tmp_path, recorder)
    pipeline._prepare_context = MagicMock(return_value=(_prepared(), None))
    router = MagicMock()
    router.providers = {}
    events: list[dict] = []

    async def degraded_stream():
        yield DEGRADED_ANSWER_TEXT

    async def collect():
        async for line in pipeline.query_stream("年假有几天", session_id="s1", user={"id": 7}):
            events.append(json.loads(line.removeprefix("data: ")))

    with patch("rag.pipeline.get_llm_pool_router", return_value=router), patch(
        "rag.pipeline.generate_stream", return_value=degraded_stream()
    ):
        asyncio.run(collect())

    assert [event["type"] for event in events] == ["token", "error"]
    assert events[-1]["degraded"] is True
    assert pipeline.memory.get_recent_messages("u7:s1") == []
    assert pipeline.tracker.save.call_args.args[0].route == "error"
    recorder.submit.assert_called_once()
    pipeline.close()


# ---------------------------------------------------------------------------
# 4) API 层：同步返回 503 + degraded；流式 error 后不再补一个 done
# ---------------------------------------------------------------------------


def test_sync_query_endpoint_reports_degraded_without_persisting_messages(
    isolated_api, make_user
):
    alice, alice_headers = make_user("alice")
    db = isolated_api.app.state.user_db
    conversation = db.create_conversation(alice["id"], "", "file")
    pipeline = MagicMock()
    pipeline.query.side_effect = CircuitOpenError("generator")

    with patch("rag.routers.query.get_pipeline", return_value=pipeline):
        response = isolated_api.post(
            "/query",
            headers=alice_headers,
            json={"question": "年假有几天", "conversation_id": conversation},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["degraded"] is True
    assert body["error"] == DEGRADED_ANSWER_TEXT
    assert "reliability" not in body
    assert db.get_messages(conversation, alice["id"]) == []


def test_stream_endpoint_does_not_append_done_after_a_degraded_error(isolated_api, make_user):
    _, headers = make_user("alice")

    class DegradedPipeline:
        async def query_stream(self, *_args, **_kwargs):
            yield (
                'data: '
                + json.dumps(
                    {"type": "error", "reason": DEGRADED_ANSWER_TEXT, "degraded": True},
                    ensure_ascii=False,
                )
                + "\n\n"
            )

    with patch("rag.routers.query.get_pipeline", return_value=DegradedPipeline()):
        response = isolated_api.post(
            "/query/stream", headers=headers, json={"question": "年假有几天"}
        )

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [event["type"] for event in events] == ["error"]
    assert "done" not in [event["type"] for event in events]
