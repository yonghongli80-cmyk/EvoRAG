"""Regression coverage for keeping SSE stream setup off the event loop."""

import asyncio
import json
import sqlite3
import threading
import time
from unittest.mock import MagicMock, patch


def _http_scope(method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def _request(
    app, method: str, path: str, body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None
) -> list[dict]:
    """Issue one in-process ASGI request without buffering the stream client-side."""
    request_sent = False
    disconnect = asyncio.Event()
    messages = []

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await app(_http_scope(method, path, headers), receive, send)
    return messages


def test_stream_preparation_does_not_delay_health_request(tmp_path, monkeypatch):
    """A slow synchronous preparation must leave /health responsive before it finishes."""
    import rag.api as api_module
    from config import settings
    from rag.pipeline import RAGPipeline

    preparation_started = threading.Event()
    timing = {}
    prepared = {
        "route": "rag",
        "question": "test",
        "context": [],
        "messages": [{"role": "user", "content": "test"}],
        "sources": [],
        "sid": "stream-session",
    }

    def slow_prepare(*args, **kwargs):
        preparation_started.set()
        time.sleep(0.25)
        timing["preparation_finished"] = time.perf_counter()
        return prepared, None

    async def fake_generate_stream(messages):
        yield "answer"

    monkeypatch.setattr(settings, "auth_enabled", False)
    users_db_path = tmp_path / "users.db"
    sqlite3.connect(users_db_path).close()
    monkeypatch.setattr(settings, "users_db_path", str(users_db_path))
    health_client = MagicMock()
    health_client.get_collections.return_value = []

    with patch("rag.pipeline.Retriever"), \
         patch("rag.pipeline.Reranker"), \
         patch("rag.agent.RAGAgent"), \
         patch("rag.pipeline.generate_stream", fake_generate_stream), \
         patch("rag.vector_store._get_client", return_value=health_client):
        pipeline = RAGPipeline("test.txt", kb_id="test_kb", memory_db_path=str(tmp_path / "memory.db"))

        # The test observes scheduling, so persistence is outside its scope.
        pipeline.memory = MagicMock()
        pipeline.memory.should_summarize.return_value = False
        pipeline.tracker = MagicMock()
        pipeline._cache = MagicMock()
        monkeypatch.setattr(pipeline, "_prepare_context", slow_prepare)
        monkeypatch.setattr(api_module, "pipeline", pipeline)

        async def health_after_preparation_starts():
            assert await asyncio.to_thread(preparation_started.wait, 1)
            messages = await _request(api_module.app, "GET", "/health")
            timing["health_finished"] = time.perf_counter()
            return messages

        async def run_requests():
            stream_task = asyncio.create_task(
                _request(
                    api_module.app,
                    "POST",
                    "/query/stream",
                    json.dumps({"question": "test"}).encode(),
                    [(b"content-type", b"application/json")],
                )
            )
            health_task = asyncio.create_task(health_after_preparation_starts())
            stream_messages = await stream_task
            health_messages = await health_task
            return stream_messages, health_messages

        stream_messages, health_messages = asyncio.run(run_requests())

    assert any(message.get("status") == 200 for message in health_messages)
    assert any(message.get("body", b"").startswith(b"data: ") for message in stream_messages)
    assert timing["health_finished"] < timing["preparation_finished"]
