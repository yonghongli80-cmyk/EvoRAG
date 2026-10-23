from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from functools import wraps
from inspect import isasyncgenfunction
from typing import Any

KNOWN_STAGE_KEYS = frozenset(
    {"preparation_ms", "jev_preflight_ms", "jev_passages_ms", "rerank_ms", "generation_ms"}
)
_current_timings: ContextVar[StageTimings | None] = ContextVar("rag_stage_timings", default=None)


class StageTimings:
    """Thread-safe per-request accumulator for measured pipeline spans."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completed: dict[str, float] = {}
        self._active: dict[object, tuple[str, float]] = {}
        self._sealed = False
        self._sealed_snapshot: dict[str, float] | None = None

    def start(self, stage: str) -> object:
        if stage not in KNOWN_STAGE_KEYS:
            raise ValueError(f"unknown stage timing key: {stage}")
        token = object()
        with self._lock:
            if self._sealed:
                return token
        started_at = time.perf_counter()
        with self._lock:
            if not self._sealed:
                self._active[token] = (stage, started_at)
        return token

    def finish(self, token: object) -> None:
        with self._lock:
            if self._sealed or token not in self._active:
                return
        finished_at = time.perf_counter()
        with self._lock:
            if self._sealed:
                return
            active = self._active.pop(token, None)
            if active is None:
                return
            stage, started_at = active
            self._completed[stage] = self._completed.get(stage, 0.0) + max(0.0, finished_at - started_at) * 1000.0

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            if self._sealed_snapshot is not None:
                return dict(self._sealed_snapshot)
            return dict(self._completed)

    def seal(self) -> dict[str, float]:
        with self._lock:
            if self._sealed_snapshot is not None:
                return dict(self._sealed_snapshot)
        sealed_at = time.perf_counter()
        with self._lock:
            if self._sealed_snapshot is not None:
                return dict(self._sealed_snapshot)
            for stage, started_at in self._active.values():
                self._completed[stage] = self._completed.get(stage, 0.0) + max(0.0, sealed_at - started_at) * 1000.0
            self._active.clear()
            self._sealed = True
            self._sealed_snapshot = dict(self._completed)
            return dict(self._sealed_snapshot)


@contextmanager
def use_stage_timings(timings: StageTimings | None) -> Iterator[StageTimings | None]:
    """Bind one request accumulator and restore the prior context on exit."""
    token: Token[StageTimings | None] = _current_timings.set(timings)
    try:
        yield timings
    finally:
        _current_timings.reset(token)


def current_stage_timings() -> StageTimings | None:
    return _current_timings.get()


def ensure_stage_timings() -> StageTimings:
    """Return the current accumulator, creating one only for the caller to scope."""
    timings = current_stage_timings()
    return timings if timings is not None else StageTimings()


def measure_call(stage: str, callable_: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    timings = current_stage_timings()
    if timings is None:
        return callable_(*args, **kwargs)
    token = timings.start(stage)
    try:
        return callable_(*args, **kwargs)
    finally:
        timings.finish(token)


async def measure_stream(stage: str, source: AsyncIterator[Any]) -> AsyncIterator[Any]:
    """Measure each upstream advance and close it in the request's scoped context."""
    timings = current_stage_timings()
    iterator = aiter(source)
    try:
        while True:
            token = timings.start(stage) if timings is not None else None
            try:
                if timings is None:
                    item = await anext(iterator)
                else:
                    with use_stage_timings(timings):
                        item = await anext(iterator)
            except StopAsyncIteration:
                if token is not None:
                    timings.finish(token)
                return
            except BaseException:
                if token is not None:
                    timings.finish(token)
                raise
            else:
                if token is not None:
                    timings.finish(token)
            yield item
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            if timings is None:
                await close()
            else:
                with use_stage_timings(timings):
                    await close()


def request_stage_timing_scope(function):
    """Give each public query call a fresh collector and scope stream advances independently."""
    if isasyncgenfunction(function):
        @wraps(function)
        async def scoped_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            timings = StageTimings()
            iterator = function(*args, **kwargs)
            try:
                while True:
                    try:
                        with use_stage_timings(timings):
                            item = await anext(iterator)
                    except StopAsyncIteration:
                        return
                    yield item
            finally:
                try:
                    with use_stage_timings(timings):
                        await iterator.aclose()
                finally:
                    timings.seal()

        return scoped_stream

    @wraps(function)
    def scoped_call(*args: Any, **kwargs: Any) -> Any:
        timings = StageTimings()
        try:
            with use_stage_timings(timings):
                return function(*args, **kwargs)
        finally:
            timings.seal()

    return scoped_call


def preparation_stage_timing_scope(function):
    """Scope standalone private preparation calls while reusing a query collector when present."""
    @wraps(function)
    def scoped_prepare(*args: Any, **kwargs: Any) -> Any:
        current = current_stage_timings()
        if current is not None:
            result = function(*args, **kwargs)
            _attach_direct_stage_snapshot(result, current.snapshot())
            return result
        timings = StageTimings()
        with use_stage_timings(timings):
            result = function(*args, **kwargs)
            _attach_direct_stage_snapshot(result, timings.snapshot())
            return result

    return scoped_prepare


def _attach_direct_stage_snapshot(result: Any, timings: dict[str, float]) -> None:
    if not isinstance(result, tuple) or len(result) != 2:
        return
    prepared, _error = result
    if not isinstance(prepared, dict):
        return
    measured = {key: value for key, value in timings.items() if key == "rerank_ms"}
    if measured:
        prepared.setdefault("stage_timings", {}).update(measured)
