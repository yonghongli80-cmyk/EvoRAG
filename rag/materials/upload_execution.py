"""Cancellation-safe handoff to synchronous material upload workers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _observe_cancelled_worker[T](task: asyncio.Task[T]) -> None:
    try:
        task.result()
    except BaseException as exc:
        logger.error(
            "material_upload_worker_failed_after_cancellation exception_type=%s",
            type(exc).__name__,
        )


async def run_upload_worker[T](worker: Callable[[], T]) -> T:
    """Run ``worker`` in a thread without letting caller cancellation cancel it."""
    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_observe_cancelled_worker)
        raise
