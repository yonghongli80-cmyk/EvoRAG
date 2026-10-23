"""Centralized logging configuration for RAGv3."""

import copy
import logging
import sys
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import isasyncgenfunction
from typing import Any

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_kb_id: ContextVar[str] = ContextVar("kb_id", default="-")
_route: ContextVar[str] = ContextVar("route", default="-")
_KNOWN_ROUTES = frozenset({"-", "rag", "structured", "agent", "fixed", "cached", "arena", "blocked", "error", "other"})


def get_request_id() -> str:
    """Get the current request ID from context."""
    return _request_id.get()


def set_request_id(request_id: str) -> None:
    """Set the current request ID in context."""
    _request_id.set(request_id)


def get_kb_id() -> str:
    return _kb_id.get()


def get_route() -> str:
    return _route.get()


@contextmanager
def logging_context(*, request_id: str | None = None, kb_id: str | None = None, route: str | None = None) -> Iterator[None]:
    """Bind supplied log fields and restore their prior values on every exit."""
    tokens = []
    try:
        for variable, value in ((_request_id, request_id), (_kb_id, kb_id), (_route, route)):
            if value is not None:
                tokens.append((variable, variable.set(_normalize_route(value) if variable is _route else str(value))))
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _normalize_route(route: str) -> str:
    aliases = {"clarification": "fixed", "legacy": "rag", "tool": "agent"}
    value = aliases.get(route, route)
    return value if value in _KNOWN_ROUTES else "other"


def set_route(route: str) -> None:
    """Update the logical route inside an already scoped request."""
    _route.set(_normalize_route(route))


def normalize_request_id(value: str | None) -> str:
    """Keep safe, bounded client IDs and replace malformed values with a UUID."""
    if value and len(value) <= 128 and not any(unicodedata.category(char) == "Cc" for char in value):
        return value
    return str(uuid.uuid4())


def wrap_logging_context(callback):
    """Capture only the three logging fields for work submitted to an owned executor."""
    request_id, kb_id, route = get_request_id(), get_kb_id(), get_route()

    @wraps(callback)
    def wrapped(*args: Any, **kwargs: Any):
        with logging_context(request_id=request_id, kb_id=kb_id, route=route):
            return callback(*args, **kwargs)

    return wrapped


def request_logging_context_scope(function):
    """Scope a whole sync query or each individual advance/close of a stream."""
    def effective_kb(owner, args, kwargs):
        explicit = kwargs.get("kb_id", args[6] if len(args) > 6 else None)
        return explicit or getattr(owner, "_kb_id", "-") or "-"

    if isasyncgenfunction(function):
        @wraps(function)
        async def scoped_stream(owner, *args, **kwargs):
            request_id = get_request_id()
            kb_id = effective_kb(owner, args, kwargs)
            iterator = function(owner, *args, **kwargs)
            route = "-"
            try:
                while True:
                    with logging_context(request_id=request_id, kb_id=kb_id, route=route):
                        try:
                            item = await anext(iterator)
                        except StopAsyncIteration:
                            return
                        finally:
                            route = get_route()
                    yield item
            finally:
                with logging_context(request_id=request_id, kb_id=kb_id, route=route):
                    try:
                        await iterator.aclose()
                    finally:
                        route = get_route()

        return scoped_stream

    @wraps(function)
    def scoped_call(owner, *args, **kwargs):
        with logging_context(request_id=get_request_id(), kb_id=effective_kb(owner, args, kwargs), route="-"):
            return function(owner, *args, **kwargs)

    return scoped_call


def _safe_field(value: str) -> str:
    return "".join(f"\\x{ord(char):02x}" if unicodedata.category(char) == "Cc" else char for char in value)


class RequestIdFilter(logging.Filter):
    """Inject bounded logging context fields into log records."""

    def filter(self, record):
        record.request_id = get_request_id()
        record.kb_id = get_kb_id()
        record.route = get_route()
        return True


def setup_logging(level: str = None, json_format: bool = False) -> None:
    """Configure root logger.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Default from RAG_LOG_LEVEL, fallback INFO.
        json_format: If True, output JSON lines. Default from RAG_LOG_JSON.
    """
    from config import settings

    level = level or settings.log_level or "INFO"
    json_format = json_format or settings.log_json

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())

    if json_format:
        import json as _json

        class JsonFormatter(logging.Formatter):
            def format(self, record):
                return _json.dumps(
                    {
                        "time": self.formatTime(record),
                        "level": record.levelname,
                        "logger": record.name,
                        "request_id": getattr(record, "request_id", "-"),
                        "kb_id": getattr(record, "kb_id", "-"),
                        "route": getattr(record, "route", "-"),
                        "message": record.getMessage(),
                    },
                    ensure_ascii=False,
                )

        handler.setFormatter(JsonFormatter())
    else:
        class SafeTextFormatter(logging.Formatter):
            def format(self, record):
                safe_record = copy.copy(record)
                for field in ("request_id", "kb_id", "route"):
                    setattr(safe_record, field, _safe_field(getattr(safe_record, field, "-")))
                return super().format(safe_record)

        handler.setFormatter(SafeTextFormatter(
            "%(asctime)s | %(levelname)-7s | %(request_id)s | %(kb_id)s | %(route)s | %(name)s | %(message)s"
        ))

    root.addHandler(handler)
