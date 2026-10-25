"""Fail-open, header-only upstream response observation for existing clients."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import logging
import threading
import unicodedata
from urllib.parse import urlsplit

logger = logging.getLogger("rag.upstream_observability")

_REGISTRATION_LOCK = threading.RLock()
_MAX_REQUEST_ID_LENGTH = 4096
_EVENT_SCHEMA_VERSION = 1
_EVENT_NAME = "upstream_http_response"
_ERROR_EVENT = {"schema_version": 1, "event": "upstream_observer_error"}


def _compact_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def _emit_observer_error() -> None:
    with contextlib.suppress(Exception):
        logger.warning(_compact_json(_ERROR_EVENT))


def _emit_unavailable(backend: str) -> None:
    with contextlib.suppress(Exception):
        logger.warning(
            _compact_json(
                {
                    "schema_version": _EVENT_SCHEMA_VERSION,
                    "event": "upstream_observer_unavailable",
                    "backend": backend,
                }
            )
        )


def _event_status(response: object) -> int | None:
    status_code = getattr(response, "status_code", None)
    if type(status_code) is int and 100 <= status_code <= 599:
        return status_code
    return None


def _response_path(response: object) -> str:
    request = getattr(response, "request", None)
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    if type(path) is str:
        return path
    if type(url) is str:
        return urlsplit(url).path
    return ""


def _operation_for_path(path: str) -> str:
    if path.endswith(("/chat/completions", "/responses")):
        return "generation"
    if path.endswith("/embeddings"):
        return "embedding"
    if path.endswith("/systemone"):
        return "jev"
    return "other"


def _request_id(headers: object, *, operation: str) -> tuple[str, str | None, str | None]:
    names = (
        ("x-typesafe-request-id", "x-request-id") if operation == "jev" else ("x-request-id", "x-typesafe-request-id")
    )
    for name in names:
        value = headers.get(name)
        if value is None:
            continue
        if type(value) is not str:
            return "invalid", name, None
        if value == "":
            continue
        if len(value) > _MAX_REQUEST_ID_LENGTH:
            return "invalid", name, None
        if any(unicodedata.category(char) == "Cc" for char in value):
            return "invalid", name, None
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return "invalid", name, None
        if value.isspace():
            continue
        return "present", name, hashlib.sha256(encoded).hexdigest()
    return "missing", None, None


def _observe_response(response: object, *, backend: str) -> None:
    operation = "rerank" if backend == "requests" else _operation_for_path(_response_path(response))
    state, header_name, request_id_hash = _request_id(getattr(response, "headers", {}), operation=operation)
    event = {
        "schema_version": _EVENT_SCHEMA_VERSION,
        "event": _EVENT_NAME,
        "operation": operation,
        "status_code": _event_status(response),
        "provider_request_id_state": state,
        "provider_request_id_header": header_name,
        "provider_request_id_sha256": request_id_hash,
    }
    try:
        logger.info(_compact_json(event))
    except Exception:
        _emit_observer_error()


def _observe_fail_open(response: object, *, backend: str) -> None:
    try:
        _observe_response(response, backend=backend)
    except Exception:
        _emit_observer_error()


def _sync_httpx_response_hook(response: object) -> None:
    _observe_fail_open(response, backend="httpx")


async def _async_httpx_response_hook(response: object) -> None:
    _observe_fail_open(response, backend="httpx")


def _requests_response_hook(response: object, *args: object, **kwargs: object) -> object:
    _observe_fail_open(response, backend="requests")
    return response


def _register_hook(client: object, *, backend: str, hook: object, hook_attribute: str) -> bool:
    try:
        hooks = getattr(client, hook_attribute, None)
        if type(hooks) is not dict:
            _emit_unavailable(backend)
            return False
        response_hooks = hooks.get("response")
        if type(response_hooks) is not list:
            _emit_unavailable(backend)
            return False
        with _REGISTRATION_LOCK:
            if any(existing is hook for existing in response_hooks):
                return True
            response_hooks.append(hook)
        return True
    except Exception:
        _emit_unavailable(backend)
        return False


def attach_openai_observer(client: object, *, asynchronous: bool = False) -> bool:
    """Append the mode-matched response hook to an existing OpenAI SDK client."""
    try:
        http_client = getattr(client, "_client", None)
        send = getattr(http_client, "send", None)
        if http_client is None or not callable(send):
            _emit_unavailable("openai")
            return False
        is_async = inspect.iscoroutinefunction(send)
        if type(asynchronous) is not bool or is_async is not asynchronous:
            _emit_unavailable("openai")
            return False
        hook = _async_httpx_response_hook if asynchronous else _sync_httpx_response_hook
        return _register_hook(http_client, backend="openai", hook=hook, hook_attribute="event_hooks")
    except Exception:
        _emit_unavailable("openai")
        return False


def attach_typesafe_observer(client: object) -> bool:
    """Append the HTTPX2 response hook to an existing synchronous TypeSafe client."""
    try:
        http_client = getattr(client, "_http_client", None)
        send = getattr(http_client, "send", None)
        if http_client is None or not callable(send) or inspect.iscoroutinefunction(send):
            _emit_unavailable("typesafe")
            return False
        return _register_hook(
            http_client, backend="typesafe", hook=_sync_httpx_response_hook, hook_attribute="event_hooks"
        )
    except Exception:
        _emit_unavailable("typesafe")
        return False


def attach_requests_observer(session: object) -> bool:
    """Append the rerank response hook to an existing requests Session."""
    return _register_hook(session, backend="requests", hook=_requests_response_hook, hook_attribute="hooks")
