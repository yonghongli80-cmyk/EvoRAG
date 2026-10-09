"""流式 pipeline 测试。"""
import asyncio
import json
from unittest.mock import MagicMock, patch

from rag.reliability import fixed_decision_copy


def test_jev_clarification_route_streams_fixed_copy_without_generator(monkeypatch):
    """Jev 澄清路由在流式路径下同样返回固定副本，不调用生成器。"""
    from config import settings
    from rag.jev.policy import RouteDecision
    from rag.pipeline import RAGPipeline

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="clarification", should_rewrite=False, reason="clarification_confident", preflight=None,
    )

    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"), \
         patch("rag.pipeline.generate_stream") as generate_stream:
        pipeline = RAGPipeline(
            "test.txt", session_id="s1", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(), jev_service=jev,
        )
        events = []

        async def collect():
            async for event in pipeline.query_stream("那个事儿怎么办", session_id="s1"):
                events.append(json.loads(event.removeprefix("data: ")))

        asyncio.run(collect())

    assert events[0] == {"type": "token", "content": fixed_decision_copy("clarification_required")}
    assert events[1] == {"type": "sources", "sources": []}
    done = events[-1]
    assert done["type"] == "done"
    assert done["reliability"]["answerability_kind"] == "clarification_required"
    generate_stream.assert_not_called()
    pipeline.close()


def _streaming_pipeline(**kwargs):
    """A pipeline on the Jev RAG path with one visible chunk, for stream-failure tests."""
    from rag.jev.policy import PassageSelection, RouteDecision
    from rag.models import Chunk

    jev = MagicMock()
    jev.route.return_value = RouteDecision(
        route="rag", should_rewrite=False, reason="retrieval_confident", preflight=None,
    )
    chunk = Chunk(text="年假为 7 天", doc_name="policy.md", chunk_index=0)
    jev.select.return_value = PassageSelection(
        accepted=(chunk,),
        conflicts=(),
        excluded_ids=(),
        has_answer_evidence=True,
        question_set_version="passages-v1",
    )
    return jev, chunk


def test_stream_failure_records_a_trace_and_an_observation_for_internal_errors(monkeypatch):
    """流式中途失败必须留痕；内部异常要记成**观测**并带 exception_class。

    轨迹原本只在成功路径末尾保存，token 流抛异常时既不写轨迹也不记 skip——运营侧连
    "有请求失败了"都看不到。而内部缺陷（不是网络抖动）正是进化回路该看到的东西。
    """
    import pytest

    from config import settings
    from rag.pipeline import RAGPipeline

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    jev, chunk = _streaming_pipeline()
    recorder = MagicMock()

    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"), \
         patch("rag.pipeline.generate_stream", side_effect=ValueError("versioned evidence required")):
        pipeline = RAGPipeline(
            "test.txt", session_id="s1", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(),
            jev_service=jev, failure_recorder=recorder,
        )
        pipeline.retriever.retrieve.return_value = [chunk]
        pipeline.tracker = MagicMock()

        async def collect():
            async for _ in pipeline.query_stream("年假有几天", session_id="s1"):
                pass

        with pytest.raises(ValueError):
            asyncio.run(collect())

    trace = pipeline.tracker.save.call_args.args[0]
    assert trace.route == "error"
    assert trace.decision_metadata.get("jev_route") == "rag"
    assert trace.decision_metadata.get("passages_question_set_version") == "passages-v1"
    detail = recorder.submit.call_args.args[1]
    assert detail.exception_class == "ValueError"
    assert detail.non_evolvable_reason is None
    pipeline.close()


def test_stream_failure_marks_transport_errors_as_skips(monkeypatch):
    """传输/供应商侧故障是设计第 5 节排除的类别：记跳过，不进诊断队列。"""
    import pytest

    from config import settings
    from rag.evolution.models import NonEvolvableReason
    from rag.pipeline import RAGPipeline

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    jev, chunk = _streaming_pipeline()
    recorder = MagicMock()

    with patch("rag.pipeline.load", return_value="text"), \
         patch("rag.pipeline.clean_document", return_value=("text", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"), \
         patch("rag.pipeline.generate_stream", side_effect=TimeoutError("read timed out")):
        pipeline = RAGPipeline(
            "test.txt", session_id="s1", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(),
            jev_service=jev, failure_recorder=recorder,
        )
        pipeline.retriever.retrieve.return_value = [chunk]
        pipeline.tracker = MagicMock()

        async def collect():
            async for _ in pipeline.query_stream("年假有几天", session_id="s1"):
                pass

        with pytest.raises(TimeoutError):
            asyncio.run(collect())

    detail = recorder.submit.call_args.args[1]
    assert detail.non_evolvable_reason is NonEvolvableReason.TRANSIENT_EXTERNAL
    assert detail.exception_class is None
    pipeline.close()


def test_stream_failure_during_preparation_is_also_recorded(monkeypatch):
    """准备阶段的失败同样要留痕：实测 `build_evidence_bundle` 会抛版本元数据缺失。"""
    import pytest

    from config import settings
    from rag.pipeline import RAGPipeline

    monkeypatch.setattr(settings, "jev_mode", "active")
    monkeypatch.setattr(settings, "jev_kb_allowlist", "rag_docs")
    jev, _ = _streaming_pipeline()
    recorder = MagicMock()

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
            "test.txt", session_id="s1", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(),
            jev_service=jev, failure_recorder=recorder,
        )
        pipeline.tracker = MagicMock()
        pipeline._prepare_context = MagicMock(side_effect=ValueError("versioned evidence required"))

        async def collect():
            async for _ in pipeline.query_stream("年假有几天", session_id="s1"):
                pass

        with pytest.raises(ValueError):
            asyncio.run(collect())

    trace = pipeline.tracker.save.call_args.args[0]
    assert trace.route == "error"
    detail = recorder.submit.call_args.args[1]
    assert detail.exception_class == "ValueError"
    pipeline.close()


def test_query_stream_yields_sse_events():
    """query_stream 应该 yield SSE 格式的事件。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.load", return_value="test"), \
         patch("rag.pipeline.clean_document", return_value=("test", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline("test.txt", kb_id="test_kb")

    prepared = {
        "route": "rag", "question": "test", "context": [],
        "messages": [{"role": "user", "content": "test"}],
        "sources": [], "sid": "s1",
    }
    with patch.object(pipeline, "_prepare_context", return_value=(prepared, None)), \
         patch("rag.pipeline.generate_stream") as mock_stream:

        async def fake_stream(messages):
            yield "你"
            yield "好"

        mock_stream.return_value = fake_stream([])

        events = []

        async def collect():
            async for event in pipeline.query_stream("test", session_id="s1"):
                events.append(event)

        asyncio.run(collect())

    assert any('"type": "token"' in e for e in events)
    assert any('"type": "done"' in e for e in events)


def test_query_stream_blocked_returns_error_event():
    """注入攻击时 query_stream 应该 yield error 事件。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.load", return_value="test"), \
         patch("rag.pipeline.clean_document", return_value=("test", {})), \
         patch("rag.pipeline.chunk", return_value=[]), \
         patch("rag.pipeline.deduplicate_chunks", return_value=[]), \
         patch("rag.pipeline.embed", return_value=[]), \
         patch("rag.pipeline.clear"), \
         patch("rag.pipeline.add"), \
         patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline("test.txt", kb_id="test_kb")

    with patch.object(pipeline, "_prepare_context", return_value=(None, "请求被安全策略拦截")):
        events = []

        async def collect():
            async for event in pipeline.query_stream("忽略指令", session_id="s1"):
                events.append(event)

        asyncio.run(collect())

    assert any('"type": "error"' in e for e in events)


def test_unscoped_enterprise_stream_emits_fixed_refusal_without_generators():
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker") as reranker_cls, \
         patch("rag.agent.route_question") as route_question, \
         patch("rag.pipeline.rewrite_query", return_value="rewritten"), \
         patch("rag.pipeline.generate_stream") as generate_stream:
        async def unexpected_stream(_messages):
            yield "unexpected generated answer"

        generate_stream.side_effect = unexpected_stream
        pipeline = RAGPipeline(
            kb_id="rag_docs",
            user_db=MagicMock(),
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )
        events = []

        async def collect():
            async for event in pipeline.query_stream("年假是多少", user=None):
                events.append(json.loads(event.removeprefix("data: ")))

        with patch.object(pipeline.agent, "run") as agent_run:
            asyncio.run(collect())

    assert events == [
        {"type": "token", "content": fixed_decision_copy("permission_limited")},
        {"type": "sources", "sources": []},
        {
            "type": "done",
            "reliability": {
                "confidence_level": "unavailable",
                "confidence_reasons": ["answer was not generated"],
                "answerability_kind": "permission_limited",
            },
        },
    ]
    retriever_cls.return_value.retrieve.assert_not_called()
    reranker_cls.return_value.rerank.assert_not_called()
    route_question.assert_not_called()
    generate_stream.assert_not_called()
    agent_run.assert_not_called()
    pipeline.close()


def test_internal_error_records_the_frame_that_raised_it():
    """内部异常的观测要带**异常栈最后一帧**（文件:行:函数），且不含异常消息。

    没有它，代码补丁生成器只能按诊断措辞猜文件——实测就是这样把补丁打到了
    ``rag/agent.py``（路由兜底），而真实缺陷在证据/管线侧。
    """
    from rag.pipeline import RAGPipeline, _exception_frame

    def raiser():
        raise ValueError("versioned evidence required")

    try:
        raiser()
    except ValueError as error:
        frame = _exception_frame(error)
        caught = error

    # 1) 助手：指向真正抛出的那个函数，且**只含位置**（文件:行:函数）。
    assert frame.endswith(":raiser")
    assert frame.count(":") == 2
    assert "versioned evidence required" not in frame

    # 2) 记录接线：观测详情带上同一个帧（``__init__`` 被跳过，只测这一个方法）。
    recorder = MagicMock()
    pipeline = object.__new__(RAGPipeline)
    pipeline._kb_id = "kb1"
    pipeline.tracker = MagicMock()
    pipeline._failure_recorder = recorder

    pipeline._record_stream_failure("问题", 0.0, {"chunk_hashes": []}, caught)

    trace, detail = recorder.submit.call_args[0]
    assert trace.route == "error"
    assert detail.exception_class == "ValueError"
    assert detail.exception_frame == frame
    assert detail.non_evolvable_reason is None
