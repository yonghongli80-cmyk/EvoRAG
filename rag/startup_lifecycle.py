"""Owned work and one-shot cleanup for cancellable API startup operations.

The synchronous startup callable runs through :func:`asyncio.to_thread`, which
keeps the default executor and ``contextvars`` behavior.  A separate
``concurrent.futures.Future`` token tracks whether that callable can still run:
the wrapper claims it with ``set_running_or_notify_cancel`` before entering the
callable, and resolves it only after the callable returns or raises.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

_WORK_SKIPPED = object()
_WORK_FAILED = object()


class StartupLifecycle:
    """Track one app's startup work and run its cleanup hooks at most once."""

    def __init__(self, *, cancel_grace_seconds: float = 5.0) -> None:
        if cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds must be non-negative")
        self._cancel_grace_seconds = cancel_grace_seconds
        self._lock = threading.Lock()
        self._admitted = False
        self._cleanup_requested = False
        self._finalizer_claimed = False
        self._finalizer_done = False
        self._generation = 0
        self._active_work: Future[None] | None = None
        self._cleanup_hooks: tuple[tuple[str, Callable[[], object]], ...] = ()
        self._primary_error: BaseException | None = None
        self._cleanup_errors: tuple[BaseException, ...] = ()
        self._state = "idle"

    @property
    def state(self) -> str:
        """Return a small diagnostic state without claiming resources are closed."""
        with self._lock:
            return self._state

    def enter(self) -> None:
        """Reserve this app's startup/cleanup lifecycle or reject overlap."""
        with self._lock:
            if self._admitted:
                raise RuntimeError("previous API startup work or cleanup is still active")
            self._admitted = True
            self._generation += 1
            self._cleanup_requested = False
            self._finalizer_claimed = False
            self._finalizer_done = False
            self._active_work = None
            self._cleanup_hooks = ()
            self._primary_error = None
            self._cleanup_errors = ()
            self._state = "starting"

    async def to_thread(self, function: Callable[[], Any]) -> Any:
        """Run one synchronous startup function with an independently-owned token."""
        token: Future[None] = Future()

        def tracked_call() -> Any:
            # This is the first operation in the executor wrapper.  If token
            # cancellation wins while queued, the callable is never entered.
            if not token.set_running_or_notify_cancel():
                return _WORK_SKIPPED
            try:
                value = function()
            except BaseException as error:
                try:
                    token.set_exception(error)
                except BaseException:
                    _log_callback_failure("mark startup work failed")
                return _WORK_FAILED
            try:
                token.set_result(None)
            except BaseException:
                _log_callback_failure("mark startup work complete")
            return value

        with self._lock:
            if not self._admitted or self._cleanup_requested:
                raise RuntimeError("API startup lifecycle is not accepting work")
            if self._active_work is not None:
                raise RuntimeError("API startup work is already active")
            generation = self._generation
            self._active_work = token
            self._state = "startup_work"

        # Do not add callbacks while holding _lock: Future callbacks may run
        # synchronously when the token is already complete.
        token.add_done_callback(lambda completed: self._work_finished(generation, completed))
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        work_future = loop.run_in_executor(None, context.run, tracked_call)
        value = await asyncio.shield(work_future)
        if value is _WORK_FAILED:
            error = token.exception()
            if error is not None:
                raise error
            raise RuntimeError("startup work failed without an exception")
        if value is _WORK_SKIPPED:
            raise asyncio.CancelledError("startup work was cancelled before running")
        return value

    async def finish(
        self,
        cleanup_hooks: Sequence[tuple[str, Callable[[], object]]],
        primary_error: BaseException | None,
    ) -> None:
        """Run cleanup after quiescence, or defer it until the token completes."""
        with self._lock:
            if not self._admitted or self._finalizer_done:
                return
            self._cleanup_requested = True
            self._cleanup_hooks = tuple(cleanup_hooks)
            self._primary_error = primary_error
            token = self._active_work
            generation = self._generation
            self._state = "cleanup_pending"

        if token is not None and not token.done():
            # Cancel succeeds only while the wrapped function has not claimed
            # the token.  A running function remains untouched.
            token.cancel()
            deadline = time.monotonic() + self._cancel_grace_seconds
            wrapped = asyncio.wrap_future(token)
            cancellation_during_cleanup: asyncio.CancelledError | None = None
            while not token.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait({wrapped}, timeout=remaining)
                except asyncio.CancelledError as error:
                    # Repeated cancellation cannot restart the grace period or
                    # cancel the executor work.  Preserve a new cancellation
                    # arriving during otherwise-normal cleanup for propagation.
                    if primary_error is None and cancellation_during_cleanup is None:
                        cancellation_during_cleanup = error
                        with self._lock:
                            self._primary_error = error

            deferred = False
            if not token.done():
                with self._lock:
                    if (
                        generation == self._generation
                        and self._admitted
                        and not self._finalizer_done
                        and self._active_work is token
                        and not token.done()
                    ):
                        self._state = "cleanup_deferred"
                        deferred = True
                if deferred:
                    logger.error("API startup work exceeded cancellation grace; preserving shared resources")
                else:
                    self._run_finalizer(generation, deferred=False)
            else:
                self._run_finalizer(generation, deferred=False)
            if cancellation_during_cleanup is not None:
                raise cancellation_during_cleanup
            return

        self._run_finalizer(generation, deferred=False)

    def _work_finished(self, generation: int, token: Future[None]) -> None:
        """Future callback: never leak a BaseException into Future.set_*()."""
        try:
            should_finalize = False
            with self._lock:
                if generation != self._generation or self._active_work is not token:
                    return
                self._active_work = None
                should_finalize = self._cleanup_requested and not self._finalizer_claimed
                if self._cleanup_requested and not self._finalizer_done:
                    self._state = "cleanup_pending"
            if should_finalize:
                self._run_finalizer(generation, deferred=True)
        except BaseException:
            _log_callback_failure("startup work completion callback")

    def _run_finalizer(self, generation: int, *, deferred: bool) -> None:
        with self._lock:
            if generation != self._generation or not self._admitted or self._finalizer_claimed or self._finalizer_done:
                return
            token = self._active_work
            if token is not None and not token.done():
                return
            if token is not None:
                self._active_work = None
            self._finalizer_claimed = True
            self._state = "cleanup_running"
            cleanup_hooks = self._cleanup_hooks
            primary_error = self._primary_error

        errors: list[BaseException] = []
        try:
            for name, cleanup in cleanup_hooks:
                try:
                    cleanup()
                except BaseException as error:
                    errors.append(error)
                    _log_cleanup_failure(name, error)
        finally:
            with self._lock:
                self._cleanup_errors = tuple(errors)
                self._finalizer_done = True
                self._admitted = False
                self._state = "cleanup_attempted"

        if errors and primary_error is None and not deferred:
            raise errors[0]


def _log_cleanup_failure(name: str, error: BaseException) -> None:
    with suppress(BaseException):
        logger.error(
            "API lifecycle cleanup hook failed: %s",
            name,
            exc_info=(type(error), error, error.__traceback__),
        )


def _log_callback_failure(action: str) -> None:
    with suppress(BaseException):
        logger.exception("API startup lifecycle callback failed: %s", action)
