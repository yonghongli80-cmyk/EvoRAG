"""Cancellation must leave one frozen partial trace without inventing an answer."""

import asyncio
import json
import threading
from unittest.mock import MagicMock, patch

import pytest

QUESTION = "synthetic question"


def _pipeline(prepared=None, **kwargs):
    from rag.pipeline import RAGPipeline

    with (
        patch("rag.pipeline.load", return_value="synthetic text"),
        patch("rag.pipeline.clean_document", return_value=("synthetic text", {})),
        patch("rag.pipeline.chunk", return_value=[]),
        patch("rag.pipeline.deduplicate_chunks", return_value=[]),
        patch("rag.pipeline.embed", return_value=[]),
        patch("rag.pipeline.delete_doc"),
        patch("rag.pipeline.add"),
        patch("rag.pipeline.Retriever"),
        patch("rag.pipeline.Reranker"),
    ):
        pipeline = RAGPipeline(
            "synthetic.txt", session_id="cancel-test", memory_db_path=":memory:",
            feedback_processor=MagicMock(), gap_analyzer=MagicMock(), **kwargs,
        )
    pipeline._prepare_context = MagicMock(
        return_value=(prepared or {
            "route": "rag", "question": QUESTION, "context": [], "messages": [],
            "sources": [], "chunk_hashes": [], "stage_timings": {},
            "decision_metadata": {},
        }, None)
    )
    pipeline.tracker = MagicMock()
    pipeline.memory = MagicMock()
    return pipeline


def _payload(line):
    return json.loads(line.removeprefix("data: "))


def test_aclose_after_first_yield_saves_partial_cancelled_trace_and_closes_provider():
    closed = []

    async def provider(_messages):
        try:
            yield "first token"
            yield "second token"
        finally:
            closed.append(True)

    pipeline = _pipeline(failure_recorder=MagicMock())
    try:
        with patch("rag.pipeline.generate_stream", side_effect=provider):
            async def consume_one_then_close():
                stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
                first = _payload(await anext(stream))
                assert first == {"type": "token", "content": "first token"}
                await stream.aclose()
                assert closed == [True]

            asyncio.run(consume_one_then_close())

        [trace] = [call.args[0] for call in pipeline.tracker.save.call_args_list]
        assert trace.route == "error"
        assert trace.answer == ""
        assert trace.decision_metadata["execution_outcome"] == "cancelled"
        assert pipeline._failure_recorder.submit.call_count == 0
        pipeline.memory.add_message.assert_not_called()
    finally:
        pipeline.close()


def test_provider_await_cancellation_persists_once_without_failure_observation():
    started = asyncio.Event()
    never = asyncio.Event()
    closed = []
    recorder = MagicMock()
    pipeline = _pipeline(failure_recorder=recorder)

    async def provider(_messages):
        try:
            started.set()
            await never.wait()
            yield "unreachable"
        finally:
            closed.append(True)

    try:
        with patch("rag.pipeline.generate_stream", side_effect=provider):
            async def cancel_provider_wait():
                stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
                task = asyncio.create_task(anext(stream))
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=1)

            asyncio.run(cancel_provider_wait())

        saves = [call.args[0] for call in pipeline.tracker.save.call_args_list]
        assert len(saves) == 1
        assert saves[0].route == "error"
        assert saves[0].decision_metadata["execution_outcome"] == "cancelled"
        assert closed == [True]
        recorder.submit.assert_not_called()
        pipeline.memory.add_message.assert_not_called()
    finally:
        pipeline.close()


def test_cancel_during_blocked_prepare_seals_immediately_and_ignores_late_finish(monkeypatch):
    import rag.stage_timing as stage_timing
    from rag.stage_timing import current_stage_timings

    entered = threading.Event()
    release = threading.Event()
    collector_seen = []
    ticks = iter([10.0, 10.25, 10.5])
    monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(ticks))
    recorder = MagicMock()
    pipeline = _pipeline(failure_recorder=recorder)

    def blocked_prepare(*_args, **_kwargs):
        collector = current_stage_timings()
        collector_seen.append(collector)
        span = collector.start("rerank_ms")
        entered.set()
        release.wait()
        collector.finish(span)
        return ({"route": "rag", "question": QUESTION, "context": [], "messages": [],
                 "sources": [], "chunk_hashes": [], "decision_metadata": {}}, None)

    pipeline._prepare_context = blocked_prepare
    try:
        async def cancel_before_prepare_returns():
            try:
                stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
                advance = asyncio.create_task(anext(stream))
                assert await asyncio.to_thread(entered.wait, 5)
                advance.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await advance
                assert pipeline.tracker.save.call_count == 1
                trace = pipeline.tracker.save.call_args.args[0]
                assert trace.route == "error"
                assert trace.decision_metadata["execution_outcome"] == "cancelled"
                sealed_timings = dict(trace.stage_timings)
                await stream.aclose()
                return sealed_timings
            finally:
                # asyncio.run joins its worker threads during shutdown; release only after
                # proving cancellation returned while preparation was still blocked.
                release.set()

        sealed = asyncio.run(cancel_before_prepare_returns())
        assert collector_seen[0] is not None
        assert sealed == {"rerank_ms": 250.0}
        assert pipeline.tracker.save.call_count == 1
        assert pipeline.tracker.save.call_args.args[0].stage_timings == sealed
        pipeline.memory.add_message.assert_not_called()

        # A later request on the same pipeline gets its own collector and cannot inherit
        # the sealed active span from the cancelled request.
        second = {"route": "fixed", "question": QUESTION, "answer": "fixed answer",
                  "context": [], "messages": [], "sources": [], "stage_timings": {},
                  "decision_metadata": {}}
        def prepare_second(*args, **kwargs):
            collector_seen.append(current_stage_timings())
            return second, None
        pipeline._prepare_context = prepare_second
        async def collect_second_request():
            return [_payload(line) async for line in pipeline.query_stream(
                "second synthetic question", session_id="cancel-test"
            )]
        events = asyncio.run(collect_second_request())
        assert events[-1]["type"] == "done"
        assert len(collector_seen) == 2
        assert collector_seen[1] is not collector_seen[0]
        traces = [call.args[0] for call in pipeline.tracker.save.call_args_list]
        assert len(traces) == 2
        assert traces[1].stage_timings == {}
        recorder.submit.assert_not_called()
    finally:
        release.set()
        pipeline.close()


def test_closing_while_suspended_at_done_does_not_create_an_extra_cancelled_trace():
    async def provider(_messages):
        yield "answer"

    pipeline = _pipeline()
    try:
        with patch("rag.pipeline.generate_stream", side_effect=provider):
            async def consume_to_done_then_close():
                stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
                events = []
                while True:
                    event = _payload(await anext(stream))
                    events.append(event)
                    if event["type"] == "done":
                        await stream.aclose()
                        return events

            events = asyncio.run(consume_to_done_then_close())

        assert events[-1]["type"] == "done"
        assert pipeline.tracker.save.call_count == 1
        [trace] = [call.args[0] for call in pipeline.tracker.save.call_args_list]
        assert trace.decision_metadata["execution_outcome"] == "success"
    finally:
        pipeline.close()


def test_cached_done_does_not_create_a_new_trace_when_closed():
    prepared = {"route": "cached", "answer": "cached answer", "sources": [],
                "stage_timings": {}, "decision_metadata": {}}
    pipeline = _pipeline(prepared)
    try:
        async def consume_cached_done_then_close():
            stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
            events = []
            while True:
                event = _payload(await anext(stream))
                events.append(event)
                if event["type"] == "done":
                    await stream.aclose()
                    return events

        events = asyncio.run(consume_cached_done_then_close())
        assert [event["type"] for event in events] == ["token", "sources", "done"]
        pipeline.tracker.save.assert_not_called()
    finally:
        pipeline.close()


def test_closing_after_handled_degraded_error_does_not_append_cancelled_trace():
    from rag.resilience import DEGRADED_ANSWER_TEXT

    pipeline = _pipeline(failure_recorder=MagicMock())

    async def degraded_provider(_messages):
        yield DEGRADED_ANSWER_TEXT

    try:
        with patch("rag.pipeline.generate_stream", side_effect=degraded_provider):
            async def consume_error_then_close():
                stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
                events = []
                while True:
                    event = _payload(await anext(stream))
                    events.append(event)
                    if event["type"] == "error":
                        await stream.aclose()
                        return events

            events = asyncio.run(consume_error_then_close())
        assert [event["type"] for event in events] == ["token", "error"]
        assert pipeline.tracker.save.call_count == 1
        assert pipeline.tracker.save.call_args.args[0].decision_metadata["execution_outcome"] == "degraded"
    finally:
        pipeline.close()


def test_cancellation_after_terminal_save_starts_does_not_append_partial_trace():
    save_started = threading.Event()
    release_save = threading.Event()
    persisted = []
    pipeline = _pipeline()

    def blocked_save(trace):
        save_started.set()
        release_save.wait()
        persisted.append(trace)

    pipeline.tracker.save.side_effect = blocked_save

    async def provider(_messages):
        yield "answer"

    try:
        with patch("rag.pipeline.generate_stream", side_effect=provider):
            async def cancel_during_terminal_write():
                try:
                    stream = pipeline.query_stream(QUESTION, session_id="cancel-test")
                    events = []
                    while True:
                        advance = asyncio.create_task(anext(stream))
                        try:
                            event = await advance
                        except StopAsyncIteration:
                            break
                        events.append(_payload(event))
                        if events[-1]["type"] == "sources":
                            terminal_advance = asyncio.create_task(anext(stream))
                            assert await asyncio.to_thread(save_started.wait, 5)
                            terminal_advance.cancel()
                            with pytest.raises(asyncio.CancelledError):
                                await terminal_advance
                            break
                    return events
                finally:
                    release_save.set()

            events = asyncio.run(cancel_during_terminal_write())

        assert [event["type"] for event in events] == ["token", "sources"]
        assert pipeline.tracker.save.call_count == 1
        assert len(persisted) == 1
        assert persisted[0].decision_metadata["execution_outcome"] == "success"
    finally:
        release_save.set()
        pipeline.close()


def test_terminal_save_error_propagates_without_a_success_trace():
    pipeline = _pipeline()
    failure = OSError("synthetic tracker failure")
    pipeline.tracker.save.side_effect = failure

    async def provider(_messages):
        yield "answer"

    try:
        with patch("rag.pipeline.generate_stream", side_effect=provider):
            async def collect():
                return [_payload(line) async for line in pipeline.query_stream(
                    QUESTION, session_id="cancel-test"
                )]

            with pytest.raises(OSError, match="synthetic tracker failure"):
                asyncio.run(collect())

        assert pipeline.tracker.save.call_count == 1
        pipeline.memory.add_message.assert_not_called()
    finally:
        pipeline.close()
