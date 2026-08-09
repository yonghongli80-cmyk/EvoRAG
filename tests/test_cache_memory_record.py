import asyncio

from rag.memory import DialogueMemory
from rag.pipeline import RAGPipeline


def _cached_pipeline(tmp_path):
    pipeline = object.__new__(RAGPipeline)
    pipeline.session_id = None
    pipeline.memory = DialogueMemory(str(tmp_path / "memory.db"))
    pipeline._prepare_context = lambda *_args, **_kwargs: (
        {
            "route": "cached",
            "answer": "cached answer",
            "context": [],
            "sources": [],
        },
        None,
    )
    return pipeline


def _assert_cached_round_is_in_next_prompt(pipeline):
    messages = pipeline.memory.build_messages("session-1", "next question", [])
    assert {"role": "user", "content": "cached question"} in messages
    assert {"role": "assistant", "content": "cached answer"} in messages


def test_sync_cache_hit_is_recorded_in_dialogue_memory(tmp_path):
    pipeline = _cached_pipeline(tmp_path)
    try:
        result = pipeline.query("cached question", session_id="session-1")

        assert result.answer == "cached answer"
        _assert_cached_round_is_in_next_prompt(pipeline)
    finally:
        pipeline.memory.close()


def test_stream_cache_hit_is_recorded_in_dialogue_memory(tmp_path):
    pipeline = _cached_pipeline(tmp_path)

    async def consume():
        return [event async for event in pipeline.query_stream("cached question", session_id="session-1")]

    try:
        events = asyncio.run(consume())

        assert any('"type": "done"' in event for event in events)
        _assert_cached_round_is_in_next_prompt(pipeline)
    finally:
        pipeline.memory.close()
