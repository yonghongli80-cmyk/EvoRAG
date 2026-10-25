"""Contract tests for safe per-response upstream observation hooks."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import httpx2
import pytest
import requests
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, OpenAI
from requests.adapters import BaseAdapter
from typesafe_sdk import Noul, TypeSafeClient

LOGGER_NAME = "rag.upstream_observability"


@pytest.fixture
def observer():
    return importlib.import_module("rag.upstream_observability")


def _events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.INFO
    ]


def _warning_events(caplog: pytest.LogCaptureFixture) -> list[dict]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == LOGGER_NAME and record.levelno == logging.WARNING
    ]


def _expected_event(
    *,
    operation: str,
    status_code: int | None,
    state: str = "present",
    header: str | None = "x-request-id",
    request_id: str | None = "provider-id",
) -> dict:
    return {
        "schema_version": 1,
        "event": "upstream_http_response",
        "operation": operation,
        "status_code": status_code,
        "provider_request_id_state": state,
        "provider_request_id_header": header,
        "provider_request_id_sha256": (
            hashlib.sha256(request_id.encode("utf-8")).hexdigest() if state == "present" else None
        ),
    }


def _response(path: str, *, status_code: object = 200, headers: object = None):
    return SimpleNamespace(
        request=SimpleNamespace(url=SimpleNamespace(path=path)),
        status_code=status_code,
        headers={"x-request-id": "provider-id"} if headers is None else headers,
    )


def _last_httpx_hook(client):
    return client.event_hooks["response"][-1]


class _SyntheticPooledHttpxTransport(httpx.HTTPTransport):
    def __init__(self, *, limits: httpx.Limits, handler):
        super().__init__(limits=limits)
        self.handler = handler
        self.close_count = 0

    def handle_request(self, request):
        return self.handler(request)

    def close(self):
        self.close_count += 1
        super().close()


def test_observer_module_exposes_required_attach_functions():
    try:
        module = importlib.import_module("rag.upstream_observability")
    except ModuleNotFoundError as error:
        pytest.fail(f"required upstream observer module is missing: {error.name}")
    assert callable(module.attach_openai_observer)
    assert callable(module.attach_typesafe_observer)
    assert callable(module.attach_requests_observer)


def test_openai_attach_is_idempotent_ordered_and_preserves_client_configuration(observer, caplog):
    calls = []

    def first_hook(response):
        calls.append(("first", response.status_code))

    def second_hook(response):
        calls.append(("second", response.status_code))

    limits = httpx.Limits(max_connections=7, max_keepalive_connections=3, keepalive_expiry=12)
    transport = _SyntheticPooledHttpxTransport(
        limits=limits,
        handler=lambda request: httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "provider-id"},
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ),
    )
    http_client = httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(17),
        limits=limits,
        follow_redirects=True,
        event_hooks={"response": [first_hook, second_hook]},
    )
    sdk = OpenAI(api_key="synthetic-key", base_url="https://unit.test/v1", http_client=http_client)
    underlying = sdk._client
    pool = underlying._transport._pool
    before = {
        "sdk": sdk,
        "client": underlying,
        "transport": underlying._transport,
        "pool": pool,
        "timeout": underlying.timeout,
        "limits": (pool._max_connections, pool._max_keepalive_connections, pool._keepalive_expiry),
        "follow_redirects": underlying.follow_redirects,
        "close": (underlying.close.__self__, underlying.close.__func__),
        "hooks": list(underlying.event_hooks["response"]),
    }

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    assert observer.attach_openai_observer(sdk) is True
    assert observer.attach_openai_observer(sdk) is True
    assert sdk.chat.completions.create(model="test-model", messages=[{"role": "user", "content": "hi"}])

    assert sdk is before["sdk"]
    assert sdk._client is before["client"]
    assert sdk._client._transport is before["transport"]
    assert sdk._client._transport._pool is before["pool"]
    assert sdk._client.timeout is before["timeout"]
    assert (
        pool._max_connections,
        pool._max_keepalive_connections,
        pool._keepalive_expiry,
    ) == before["limits"]
    assert sdk._client.follow_redirects is before["follow_redirects"]
    assert (sdk._client.close.__self__, sdk._client.close.__func__) == before["close"]
    assert sdk._client.event_hooks["response"][:2] == before["hooks"]
    assert all(
        actual is expected
        for actual, expected in zip(sdk._client.event_hooks["response"][:2], before["hooks"], strict=True)
    )
    assert len(sdk._client.event_hooks["response"]) == 3
    assert calls == [("first", 200), ("second", 200)]
    assert _events(caplog) == [_expected_event(operation="generation", status_code=200)]
    sdk.close()
    assert transport.close_count == 1


def test_attach_rejects_wrong_openai_mode_and_preserves_existing_hook_identity(observer, caplog):
    sync_client = OpenAI(api_key="synthetic-key", max_retries=0)
    async_client = AsyncOpenAI(api_key="synthetic-key", max_retries=0)
    sync_existing = list(sync_client._client.event_hooks["response"])
    async_existing = list(async_client._client.event_hooks["response"])

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    assert observer.attach_openai_observer(sync_client, asynchronous=True) is False
    assert observer.attach_openai_observer(async_client, asynchronous=False) is False
    assert sync_client._client.event_hooks["response"] == sync_existing
    assert async_client._client.event_hooks["response"] == async_existing
    assert _warning_events(caplog) == [
        {"schema_version": 1, "event": "upstream_observer_unavailable", "backend": "openai"},
        {"schema_version": 1, "event": "upstream_observer_unavailable", "backend": "openai"},
    ]
    sync_client.close()
    asyncio.run(async_client.close())


def test_openai_hook_sees_each_internal_retry_and_final_status_error(observer, caplog):
    statuses = iter((500, 429, 200))
    attempts = []

    def handler(request):
        status = next(statuses)
        attempts.append(status)
        if status == 200:
            return httpx.Response(
                status,
                request=request,
                headers={"x-request-id": "final-id"},
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        return httpx.Response(
            status,
            request=request,
            headers={"Retry-After": "0", "x-request-id": f"retry-{status}"},
            json={"error": {"message": "sensitive response body sentinel"}},
        )

    sdk = OpenAI(
        api_key="synthetic-key",
        base_url="https://unit.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert observer.attach_openai_observer(sdk) is True
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    sdk.chat.completions.create(model="test-model", messages=[])
    assert attempts == [500, 429, 200]
    assert [event["status_code"] for event in _events(caplog)] == [500, 429, 200]
    assert [event["provider_request_id_sha256"] for event in _events(caplog)] == [
        hashlib.sha256(b"retry-500").hexdigest(),
        hashlib.sha256(b"retry-429").hexdigest(),
        hashlib.sha256(b"final-id").hexdigest(),
    ]
    assert "sensitive response body sentinel" not in caplog.text
    sdk.close()

    final_attempts = []

    def failing_handler(request):
        final_attempts.append(request)
        return httpx.Response(
            503,
            request=request,
            headers={"Retry-After": "0", "x-request-id": "final-error-id"},
            json={"error": {"message": "private final error sentinel"}},
        )

    final_sdk = OpenAI(
        api_key="synthetic-key",
        base_url="https://unit.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(failing_handler)),
    )
    observer.attach_openai_observer(final_sdk)
    caplog.clear()
    with pytest.raises(APIStatusError) as raised:
        final_sdk.chat.completions.create(model="test-model", messages=[])
    assert raised.value.status_code == 503
    assert len(final_attempts) == 3
    assert [event["status_code"] for event in _events(caplog)] == [503, 503, 503]
    assert "private final error sentinel" not in caplog.text
    final_sdk.close()


def test_openai_connection_failure_has_no_response_event(observer, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("private transport failure", request=request)

    sdk = OpenAI(
        api_key="synthetic-key",
        base_url="https://unit.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    observer.attach_openai_observer(sdk)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with pytest.raises(APIConnectionError):
        sdk.chat.completions.create(model="test-model", messages=[])
    assert len(calls) == 3
    assert _events(caplog) == []
    sdk.close()


class _PoisonSyncStream(httpx.SyncByteStream):
    def __init__(self):
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        raise AssertionError("observer or SDK consumed stream before caller iteration")

    def close(self):
        pass


class _PoisonHttpx2Stream(httpx2.SyncByteStream):
    def __init__(self):
        self.iterated = False

    def __iter__(self):
        self.iterated = True
        raise AssertionError("observer or SDK consumed HTTPX2 stream before caller iteration")

    def close(self):
        pass


def test_openai_stream_logs_headers_before_any_body_iteration(observer, caplog):
    body = _PoisonSyncStream()

    def handler(request):
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream", "x-request-id": "stream-id"},
            stream=body,
        )

    sdk = OpenAI(
        api_key="synthetic-key",
        base_url="https://unit.test/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    observer.attach_openai_observer(sdk)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    stream = sdk.chat.completions.create(model="test-model", messages=[], stream=True)
    assert body.iterated is False
    assert _events(caplog) == [_expected_event(operation="generation", status_code=200, request_id="stream-id")]
    stream.close()
    sdk.close()


def test_typesafe_locked_sdk_uses_httpx2_hook_and_observes_retries(observer, caplog):
    attempts = []

    def handler(request):
        attempts.append(request)
        if len(attempts) < 3:
            status = 500 if len(attempts) == 1 else 429
            return httpx2.Response(
                status,
                request=request,
                headers={"Retry-After": "0", "x-typesafe-request-id": f"retry-{status}"},
                json={"error": "sensitive typesafe body sentinel"},
            )
        return httpx2.Response(
            200,
            request=request,
            headers={"x-typesafe-request-id": "jev-id"},
            json={
                "model": "test-model",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "answers": {"intent": {"type": "noul", "noul": 0.5}},
            },
        )

    sdk = TypeSafeClient(
        api_key="synthetic-key",
        base_url="https://unit.test/v1",
        transport=httpx2.MockTransport(handler),
    )
    sdk_identity = sdk
    http_client = sdk._http_client
    transport = http_client._transport
    before_hooks = list(http_client.event_hooks["response"])
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    assert observer.attach_typesafe_observer(sdk) is True
    assert observer.attach_typesafe_observer(sdk) is True
    result = sdk.system_one(state="synthetic", questions={"intent": Noul(instructions="synthetic")})

    assert result.model == "test-model"
    assert sdk is sdk_identity
    assert sdk._http_client is http_client
    assert http_client._transport is transport
    assert http_client.event_hooks["response"][: len(before_hooks)] == before_hooks
    assert all(
        a is b
        for a, b in zip(
            http_client.event_hooks["response"],
            [*before_hooks, http_client.event_hooks["response"][-1]],
            strict=True,
        )
    )
    assert len(http_client.event_hooks["response"]) == len(before_hooks) + 1
    assert len(attempts) == 3
    events = _events(caplog)
    assert [event["status_code"] for event in events] == [500, 429, 200]
    assert all(event["operation"] == "jev" for event in events)
    assert [event["provider_request_id_header"] for event in events] == [
        "x-typesafe-request-id",
        "x-typesafe-request-id",
        "x-typesafe-request-id",
    ]
    assert "sensitive typesafe body sentinel" not in caplog.text
    sdk.close()


def test_shared_sync_httpx2_client_registers_only_one_shared_observer(observer, caplog):
    http_client = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200,
                request=request,
                headers={"x-request-id": "shared-id"},
            )
        )
    )
    openai_like = SimpleNamespace(_client=http_client)
    typesafe_like = SimpleNamespace(_http_client=http_client)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    assert observer.attach_openai_observer(openai_like) is True
    assert observer.attach_typesafe_observer(typesafe_like) is True
    http_client.get("https://unit.test/v1/embeddings")

    assert _events(caplog) == [_expected_event(operation="embedding", status_code=200, request_id="shared-id")]
    http_client.close()


def test_httpx2_direct_client_observer_records_before_poison_body(observer, caplog):
    body = _PoisonHttpx2Stream()

    def handler(request):
        return httpx2.Response(200, request=request, headers={"x-request-id": "h2-id"}, stream=body)

    http_client = httpx2.Client(transport=httpx2.MockTransport(handler))
    sdk_like = SimpleNamespace(_http_client=http_client)
    # OpenAI's helper is intentionally limited to clients exposing `_client`.
    assert observer.attach_typesafe_observer(sdk_like) is True
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    response = http_client.send(http_client.build_request("GET", "https://unit.test/v1/embeddings"), stream=True)
    assert body.iterated is False
    assert _events(caplog) == [_expected_event(operation="embedding", status_code=200, request_id="h2-id")]
    response.close()
    http_client.close()


class _SyntheticRequestsAdapter(BaseAdapter):
    def __init__(self, response_factory):
        self.response_factory = response_factory

    def send(self, request, **kwargs):
        response = self.response_factory(request)
        response.request = request
        response.url = request.url
        return response

    def close(self):
        pass


class _PoisonRequestsRaw:
    def read(self, *args, **kwargs):
        raise AssertionError("observer consumed response body")

    def close(self):
        pass


def test_requests_real_session_hook_is_ordered_idempotent_and_rerank(observer, caplog):
    called = []

    def original(response, *args, **kwargs):
        called.append(response.status_code)
        return response

    session = requests.Session()
    session.hooks["response"].append(original)
    adapter = _SyntheticRequestsAdapter(lambda request: _requests_response("rerank-id", raw=_PoisonRequestsRaw()))
    session.mount("https://", adapter)
    before = list(session.hooks["response"])
    assert observer.attach_requests_observer(session) is True
    assert observer.attach_requests_observer(session) is True
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    response = session.get("https://unit.test/rerank?secret=query", stream=True)

    assert all(a is b for a, b in zip(session.hooks["response"][:1], before, strict=True))
    assert len(session.hooks["response"]) == 2
    assert called == [200]
    assert _events(caplog) == [_expected_event(operation="rerank", status_code=200, request_id="rerank-id")]
    assert "secret=query" not in caplog.text
    response.close()
    session.close()


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/v1/chat/completions", "generation"),
        ("/v1/responses", "generation"),
        ("/v1/embeddings", "embedding"),
        ("/v1/systemone", "jev"),
        ("/v1/other", "other"),
    ],
)
def test_httpx_operation_uses_only_path_suffix(observer, caplog, path, operation):
    http_client = httpx.Client()
    sdk = SimpleNamespace(_client=http_client)
    assert observer.attach_openai_observer(sdk) is True
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _last_httpx_hook(http_client)(_response(path))
    assert _events(caplog) == [_expected_event(operation=operation, status_code=200)]
    http_client.close()


def test_jev_prefers_typesafe_header_and_other_operations_prefer_standard_header(observer, caplog):
    http_client = httpx.Client()
    assert observer.attach_openai_observer(SimpleNamespace(_client=http_client)) is True
    hook = _last_httpx_hook(http_client)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    hook(
        _response(
            "/v1/systemone",
            headers={"x-request-id": "standard", "x-typesafe-request-id": "typesafe"},
        )
    )
    hook(
        _response(
            "/v1/embeddings",
            headers={"x-request-id": "standard", "x-typesafe-request-id": "typesafe"},
        )
    )
    events = _events(caplog)
    assert events[0] == _expected_event(
        operation="jev", status_code=200, request_id="typesafe", header="x-typesafe-request-id"
    )
    assert events[1] == _expected_event(operation="embedding", status_code=200, request_id="standard")
    http_client.close()


def test_id_selection_fallback_invalid_values_hash_and_status_validation(observer, caplog):
    http_client = httpx.Client()
    sdk = SimpleNamespace(_client=http_client)
    assert observer.attach_openai_observer(sdk) is True
    hook = _last_httpx_hook(http_client)
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    hook(_response("/v1/embeddings", headers={"x-request-id": "   ", "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": "x" * 4097, "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": "bad\x7f-id", "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": "\ud800", "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": "\t", "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": " " * 4097, "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": 7, "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": 0, "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": False, "x-typesafe-request-id": "fallback"}))
    hook(_response("/v1/chat/completions", status_code=True, headers={"x-request-id": "present"}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": "", "x-typesafe-request-id": None}))
    hook(_response("/v1/chat/completions", headers={"x-request-id": "unicode-雪"}))

    events = _events(caplog)
    assert events == [
        _expected_event(operation="embedding", status_code=200, request_id="fallback", header="x-typesafe-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=200, state="invalid", header="x-request-id"),
        _expected_event(operation="generation", status_code=None, request_id="present"),
        _expected_event(operation="generation", status_code=200, state="missing", header=None),
        _expected_event(operation="generation", status_code=200, request_id="unicode-雪"),
    ]
    assert all("fallback" not in record.getMessage() for record in caplog.records if record.name == LOGGER_NAME)
    http_client.close()


def test_response_hook_does_not_inspect_body_or_unapproved_headers(observer, caplog):
    class PoisonHeaders(dict):
        def items(self):
            raise AssertionError("observer enumerated arbitrary headers")

    class PoisonResponse:
        request = SimpleNamespace(url=SimpleNamespace(path="/v1/chat/completions", query=b"hidden=query"))
        status_code = 200
        headers = PoisonHeaders(
            {
                "x-request-id": "safe-id",
                "authorization": "secret-auth-sentinel",
                "x-extra": "secret-header-sentinel",
            }
        )

        @property
        def content(self):
            raise AssertionError("observer consumed response body")

        @property
        def text(self):
            raise AssertionError("observer consumed response body")

    http_client = httpx.Client()
    assert observer.attach_openai_observer(SimpleNamespace(_client=http_client)) is True
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    _last_httpx_hook(http_client)(PoisonResponse())
    assert _events(caplog) == [_expected_event(operation="generation", status_code=200, request_id="safe-id")]
    assert "secret-auth-sentinel" not in caplog.text
    assert "secret-header-sentinel" not in caplog.text
    assert "hidden=query" not in caplog.text
    http_client.close()


def test_same_httpx_client_concurrent_attachment_is_idempotent_by_identity(observer):
    http_client = httpx.Client()
    sdk = SimpleNamespace(_client=http_client)
    existing = object()
    http_client.event_hooks["response"].append(existing)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: observer.attach_openai_observer(sdk), range(40)))

    hooks = http_client.event_hooks["response"]
    assert all(results)
    assert hooks[0] is existing
    assert len(hooks) == 2
    http_client.close()


def test_existing_hook_with_throwing_equality_does_not_break_identity_dedupe(observer):
    class BadEquality:
        def __eq__(self, other):
            raise AssertionError("hook equality must never be called")

    http_client = httpx.Client()
    existing = BadEquality()
    http_client.event_hooks["response"].append(existing)
    sdk = SimpleNamespace(_client=http_client)

    assert observer.attach_openai_observer(sdk) is True
    assert observer.attach_openai_observer(sdk) is True
    assert http_client.event_hooks["response"][0] is existing
    assert len(http_client.event_hooks["response"]) == 2
    http_client.close()


def test_unsupported_backend_and_registration_errors_fail_open_with_safe_warning(observer, caplog):
    class BrokenClient:
        @property
        def _client(self):
            raise RuntimeError("secret object representation")

    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    assert observer.attach_openai_observer(BrokenClient()) is False
    assert observer.attach_typesafe_observer(object()) is False
    assert observer.attach_requests_observer(object()) is False
    warning_events = _warning_events(caplog)
    assert warning_events == [
        {"schema_version": 1, "event": "upstream_observer_unavailable", "backend": "openai"},
        {"schema_version": 1, "event": "upstream_observer_unavailable", "backend": "typesafe"},
        {"schema_version": 1, "event": "upstream_observer_unavailable", "backend": "requests"},
    ]
    assert "secret object representation" not in caplog.text


def test_logger_exceptions_do_not_escape_or_change_http_response(observer, monkeypatch, caplog):
    http_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)))
    sdk = SimpleNamespace(_client=http_client)
    assert observer.attach_openai_observer(sdk) is True
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    def fail_info(*args, **kwargs):
        raise RuntimeError("secret logging failure")

    monkeypatch.setattr(observer.logger, "info", fail_info)
    response = http_client.get("https://unit.test/v1/chat/completions")
    assert response.status_code == 200
    assert _warning_events(caplog) == [{"schema_version": 1, "event": "upstream_observer_error"}]
    assert "secret logging failure" not in caplog.text
    http_client.close()


def test_unavailable_warning_logger_exception_is_also_fail_open(observer, monkeypatch):
    def fail_warning(*args, **kwargs):
        raise RuntimeError("logger unavailable")

    monkeypatch.setattr(observer.logger, "warning", fail_warning)
    assert observer.attach_requests_observer(object()) is False


def test_base_exception_from_logger_is_not_swallowed(observer, monkeypatch):
    http_client = httpx.Client()
    observer.attach_openai_observer(SimpleNamespace(_client=http_client))

    def stop(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(observer.logger, "info", stop)
    with pytest.raises(KeyboardInterrupt):
        _last_httpx_hook(http_client)(_response("/v1/chat/completions"))
    http_client.close()


def test_httpx_sync_logging_context_stays_with_concurrent_request(observer, caplog):
    from rag.logging_config import RequestIdFilter, logging_context

    http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, request=request, headers={"x-request-id": "id"})
        )
    )
    observer.attach_openai_observer(SimpleNamespace(_client=http_client))
    caplog.handler.addFilter(RequestIdFilter())
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    def request(request_id: str):
        with logging_context(request_id=request_id, kb_id=f"kb-{request_id}", route="rag"):
            return http_client.get("https://unit.test/v1/embeddings").status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(request, ("sync-a", "sync-b"))) == [200, 200]

    records = [record for record in caplog.records if record.name == LOGGER_NAME and record.levelno == logging.INFO]
    assert {(record.request_id, record.kb_id, record.route) for record in records} == {
        ("sync-a", "kb-sync-a", "rag"),
        ("sync-b", "kb-sync-b", "rag"),
    }
    http_client.close()


def test_httpx_async_logging_context_stays_with_concurrent_request(observer, caplog):
    from rag.logging_config import RequestIdFilter, logging_context

    async def run():
        async def handler(request):
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                request=request,
                headers={"x-request-id": "async-id"},
                json={"models": []},
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk = AsyncOpenAI(api_key="synthetic-key", base_url="https://unit.test/v1", http_client=http_client)
        assert observer.attach_openai_observer(sdk, asynchronous=True) is True
        caplog.handler.addFilter(RequestIdFilter())
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        async def request(request_id: str):
            with logging_context(request_id=request_id, kb_id=f"kb-{request_id}", route="agent"):
                await sdk.models.list()

        await asyncio.gather(request("async-a"), request("async-b"))
        await sdk.close()

    asyncio.run(run())
    records = [record for record in caplog.records if record.name == LOGGER_NAME and record.levelno == logging.INFO]
    assert {(record.request_id, record.kb_id, record.route) for record in records} == {
        ("async-a", "kb-async-a", "agent"),
        ("async-b", "kb-async-b", "agent"),
    }


def test_typesafe_attach_rejects_unsupported_async_client_mode(observer, caplog):
    from typesafe_sdk import AsyncTypeSafeClient

    client = AsyncTypeSafeClient(api_key="synthetic-key")
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)
    assert observer.attach_typesafe_observer(client) is False
    assert _warning_events(caplog) == [
        {"schema_version": 1, "event": "upstream_observer_unavailable", "backend": "typesafe"}
    ]
    asyncio.run(client.aclose())


def test_requests_logger_failure_keeps_session_response(observer, monkeypatch):
    session = requests.Session()
    adapter = _SyntheticRequestsAdapter(lambda request: _requests_response("request-id"))
    session.mount("https://", adapter)
    assert observer.attach_requests_observer(session) is True

    def fail_info(*args, **kwargs):
        raise RuntimeError("logging failure")

    monkeypatch.setattr(observer.logger, "info", fail_info)
    response = session.get("https://unit.test/rerank", stream=True)
    assert response.status_code == 200
    response.close()
    session.close()


def _requests_response(request_id: str, *, raw: object | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.headers["x-request-id"] = request_id
    response.raw = raw if raw is not None else _PoisonRequestsRaw()
    return response
