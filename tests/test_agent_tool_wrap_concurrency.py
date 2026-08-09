"""Regression tests for per-invocation Agent tool-call capture."""

import asyncio
import threading
import time
from types import SimpleNamespace

from rag.pipeline import RAGPipeline


class _Tracker:
    def __init__(self):
        self.traces = []
        self._lock = threading.Lock()

    def save(self, trace):
        with self._lock:
            self.traces.append(trace)


class _Memory:
    def add_message(self, *_args):
        pass

    def should_summarize(self, _sid):
        return False

    def summarize_old_rounds(self, _sid):
        raise AssertionError("summary should not run")


class _Cache:
    def set(self, *_args, **_kwargs):
        pass


class _ConcurrentAgent:
    def __init__(self, barrier):
        self.barrier = barrier
        self.tools = [SimpleNamespace(name="probe", func=lambda value: f"out:{value}")]

    def run(self, question, *, tool_calls=None, **_kwargs):
        self.barrier.wait(timeout=2)
        started = time.perf_counter()
        output = self.tools[0].func(question)
        if tool_calls is not None:
            tool_calls.append(
                {
                    "tool_name": "probe",
                    "input": question,
                    "output": output,
                    "duration_ms": (time.perf_counter() - started) * 1000,
                }
            )
        return output


def _pipeline_for_concurrency():
    pipeline = object.__new__(RAGPipeline)
    pipeline.session_id = "session"
    pipeline._kb_id = "kb"
    pipeline._user_db = None
    pipeline.tracker = _Tracker()
    pipeline.memory = _Memory()
    pipeline._cache = _Cache()
    pipeline.agent = _ConcurrentAgent(threading.Barrier(2))

    def prepare(question, *_args, **_kwargs):
        return {"route": "agent", "question": question}, None

    pipeline._prepare_context = prepare
    return pipeline


def test_sync_and_stream_agent_calls_keep_capture_isolated_without_mutating_tools():
    """Concurrent calls must not cross-record or leave a wrapper on shared tools."""
    pipeline = _pipeline_for_concurrency()
    original_func = pipeline.agent.tools[0].func
    sync_result = []

    sync_thread = threading.Thread(
        target=lambda: sync_result.append(pipeline.query("sync-question")),
        daemon=True,
    )
    sync_thread.start()

    async def collect_stream():
        return [event async for event in pipeline.query_stream("stream-question")]

    stream_events = asyncio.run(collect_stream())
    sync_thread.join(timeout=2)

    assert not sync_thread.is_alive()
    assert sync_result[0].answer == "out:sync-question"
    assert any("out:stream-question" in event for event in stream_events)
    assert pipeline.agent.tools[0].func is original_func

    calls_by_question = {
        trace.question: [(call.tool_name, call.input, call.output) for call in trace.tool_calls]
        for trace in pipeline.tracker.traces
    }
    assert calls_by_question == {
        "sync-question": [("probe", "sync-question", "out:sync-question")],
        "stream-question": [("probe", "stream-question", "out:stream-question")],
    }


def test_rag_agent_records_wrapped_tool_into_invocation_list():
    """The real RAGAgent recorder must populate the list passed to run()."""
    from rag.agent import RAGAgent, _wrap_tool_with_reflection

    agent = object.__new__(RAGAgent)
    agent.user = None
    agent.kb_id = "kb"
    agent.output_dir = "unused"
    agent._run_ctx = threading.local()
    tool = SimpleNamespace(name="probe", func=lambda value: f"out:{value}")
    _wrap_tool_with_reflection(tool, recorder=agent._record_tool_call)
    agent._run_reflection = lambda question: (tool.func(question), [])

    captured_calls = []
    answer = agent.run("question", tool_calls=captured_calls)

    assert answer == "out:question"
    assert len(captured_calls) == 1
    assert captured_calls[0]["tool_name"] == "probe"
    assert captured_calls[0]["input"] == "question"
    assert captured_calls[0]["output"] == "out:question"
    assert captured_calls[0]["duration_ms"] >= 0
