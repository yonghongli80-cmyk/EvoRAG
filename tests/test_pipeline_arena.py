"""竞技场（多模型并行）流式分支必须与单模型分支共用同一套后处理。

缺陷背景：``_stream_prepared`` 的竞技场分支构造完 ``ExecutionTrace`` 就 ``return``，
因此比单模型分支少做了两件事——合并回答没有经过 ``check_output`` 就写入对话记忆，
失败也没有交给自演化观测（``_submit_failure_observation``）。这几条用例把收口后的
不变量钉住：

1. 竞技场结束时要提交一次失败观测（且带着可诊断的 detail，不是空提交）；
2. 写入记忆/落轨迹的那份合并回答必须是过滤后的文本（流式已发出的 token 收不回来，
   这一点不在用例范围内）；
3. 轨迹记的 ``route="arena"`` 必须能被 recorder / 聚类 / metrics 原样处理。
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

from rag.evolution.clusterer import FailureClusterer
from rag.evolution.metrics import compute_metrics
from rag.evolution.recorder import FailureRecorder
from rag.evolution.store import FailureTraceStore
from rag.guard import OutputResult
from rag.reliability.models import AnswerabilityDecision, ConfidenceAssessment

QUESTION = "年假有几天？"
#: 竞技场合并回答的形状（``### [模型]`` 分段），与 pipeline 里的拼装格式一致。
MERGED = "### [m1]\n第一段\n\n### [m2]\n第二段"


def _prepared() -> dict:
    """一份可诊断的 RAG 准备工作：判定为证据不足，因此会产生失败观测。"""
    return {
        "route": "rag",
        "question": QUESTION,
        "context": [],
        "messages": [{"role": "user", "content": QUESTION}],
        "sources": [{"doc_name": "policy.md", "chunk_index": 0}],
        "chunk_hashes": ["hash-1"],
        "weights": {},
        "stage_timings": {},
        "decision_metadata": {},
        "decision": AnswerabilityDecision(kind="insufficient_evidence", reasons=("no qualified evidence",)),
        "confidence": ConfidenceAssessment(level="low", reasons=("no qualified evidence",)),
    }


def _arena_router() -> MagicMock:
    """并行流事件序列：两个模型的 token + 逐模型完成 + 最终 done。"""
    async def fake_parallel_stream(messages, provider_ids):
        yield {"type": "token", "model": "m1", "content": "第一段"}
        yield {"type": "token", "model": "m2", "content": "第二段"}
        yield {"type": "model_done", "model": "m1"}
        yield {"type": "model_done", "model": "m2"}
        yield {"type": "done"}

    router = MagicMock()
    router.generate_stream_parallel = fake_parallel_stream
    return router


def _build_pipeline(recorder):
    """离线构建一个注入了记录器的 pipeline，准备阶段换成固定 prepared。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline(
            "test.txt", session_id="s-arena", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(),
            failure_recorder=recorder,
        )
    pipeline._prepare_context = MagicMock(return_value=(_prepared(), None))
    pipeline.tracker = MagicMock()
    return pipeline


async def _collect(pipeline) -> list[dict]:
    events = []
    async for line in pipeline.query_stream(QUESTION, session_id="s-arena", models=["m1", "m2"]):
        events.append(json.loads(line.removeprefix("data: ")))
    return events


def test_arena_submits_exactly_one_failure_observation(monkeypatch):
    """竞技场结束后 ``_submit_failure_observation`` 被调用一次，轨迹 route 为 arena。"""
    recorder = MagicMock()
    pipeline = _build_pipeline(recorder)
    submitted = MagicMock(wraps=pipeline._submit_failure_observation)
    monkeypatch.setattr(pipeline, "_submit_failure_observation", submitted)

    # 必须打到 rag.pipeline 里的那个名字：``from rag.llm_pool import get_llm_pool_router``
    # 已经把函数对象绑进本模块的全局，改 rag.llm_pool 的同名属性到不了这里。
    with patch("rag.pipeline.get_llm_pool_router", return_value=_arena_router()):
        events = asyncio.run(_collect(pipeline))

    assert submitted.call_count == 1
    trace, prepared = submitted.call_args.args
    assert trace.route == "arena"          # 竞技场仍是可辨认的执行模式
    assert trace.answer == MERGED
    assert prepared["route"] == "rag"      # 竞技场不等于换了路由
    # 交出去的 detail 是可诊断的：这才叫"进了自演化观测"，而不是空提交。
    assert recorder.submit.call_count == 1
    assert recorder.submit.call_args.args[1].answerability_kind == "insufficient_evidence"
    # 收口后仍只发一次 sources，且它紧挨着 done（顺序与合并前一致）。
    assert [event["type"] for event in events][-2:] == ["sources", "done"]
    pipeline.close()


def test_arena_persists_the_filtered_merged_answer(monkeypatch):
    """落库/入记忆的那份合并回答必须是 check_output 过滤后的文本。"""
    recorder = MagicMock()
    pipeline = _build_pipeline(recorder)
    check = MagicMock(return_value=OutputResult(filtered=True, text="已过滤的合并回答"))
    monkeypatch.setattr("rag.pipeline.check_output", check)

    with patch("rag.pipeline.get_llm_pool_router", return_value=_arena_router()):
        events = asyncio.run(_collect(pipeline))

    check.assert_called_once_with(MERGED)
    # 轨迹里存的是过滤后的文本（轨迹在过滤之后构造）。
    trace = pipeline.tracker.save.call_args.args[0]
    assert trace.answer == "已过滤的合并回答"
    # 对话记忆里也是过滤后的文本，不是原始合并回答。
    assert [message.content for message in pipeline.memory.get_recent_messages("s-arena")] == [
        QUESTION,
        "已过滤的合并回答",
    ]
    # 已流出的 token 无法回收：客户端收到的仍是未过滤的模型分片，这一点保持现状。
    assert {"type": "token", "model": "m1", "content": "第一段"} in events
    pipeline.close()


def test_arena_route_does_not_break_the_evolution_recorder():
    """``route="arena"`` 要能被真实的 recorder/聚类/metrics 原样处理。"""
    store = FailureTraceStore(":memory:")
    recorder = FailureRecorder(store, max_queue=8)
    pipeline = _build_pipeline(recorder)

    try:
        with patch("rag.pipeline.get_llm_pool_router", return_value=_arena_router()):
            asyncio.run(_collect(pipeline))

        recorder.close(wait=True)          # 后台写线程收工后再断言，避免竞态
        observations = store.list_observations()
        assert len(observations) == 1
        [observation] = observations
        assert observation["route"] == "arena"
        assert observation["details_answerability_kind"] == "insufficient_evidence"
        # route 不是 "blocked"，所以它是一条观测而不是跳过记录。
        assert store.count_skips_by_reason() == {}

        # 聚类只看 stage/root_cause 与 blocked/structured 两个特判：未知取值走默认分支，
        # 不抛异常也不改变分类结果（与普通 "rag" 观测同族，故能在同一聚类里累计）。
        stage, feature = FailureClusterer(store).classify(observation)
        assert str(stage) == "generation"
        assert str(feature) == "generator_ignored_evidence"

        # 指标层完全不读 route，多一个取值不影响计数。
        metrics = compute_metrics(store)
        assert metrics["counts"]["observations"] == 1
        assert metrics["counts"]["skipped"] == 0
    finally:
        store.close()
        pipeline.close()
