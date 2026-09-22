"""Bounded, non-blocking shadow evaluation of Jev decisions against the legacy path.

Shadow mode runs the legacy path unchanged and queues comparisons in the background. The
evaluator never alters the current response or candidate order, and it drops work instead of
blocking when the queue is full.
"""

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from rag.jev.policy import KbCapabilities
from rag.models import Chunk

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowCase:
    """One shadow comparison case built from the legacy response path."""

    question: str
    query: str
    legacy_route: str
    legacy_ranks: tuple[tuple[str, float | None], ...]
    candidates: tuple[Chunk, ...]
    capabilities: KbCapabilities = field(default_factory=KbCapabilities)


@dataclass(frozen=True)
class ShadowComparison:
    """The recorded outcome of one shadow evaluation; content-free by construction."""

    legacy_route: str
    jev_route: str
    route_agrees: bool
    jev_available: bool
    failure_kind: str | None
    top_k_overlap: float


class ShadowEvaluator:
    """Runs Jev judgments for completed legacy requests on a bounded executor."""

    def __init__(
        self,
        jev_service,
        *,
        max_workers: int = 2,
        queue_limit: int = 8,
        on_comparison: Callable[[ShadowComparison], None] | None = None,
    ):
        self._jev = jev_service
        self._on_comparison = on_comparison
        self._queue_limit = queue_limit
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="jev-shadow")
        self._slots = threading.BoundedSemaphore(queue_limit)
        self._lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._dropped = 0
        self._closed = False

    def submit(self, case: ShadowCase) -> bool:
        """Queue one shadow case without blocking the caller; drop it when the queue is full."""
        with self._lock:
            if self._closed:
                return False
            self._submitted += 1
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._dropped += 1
            return False
        try:
            self._executor.submit(self._run, case)
        except RuntimeError:
            self._slots.release()
            with self._lock:
                self._dropped += 1
            return False
        return True

    def stats(self) -> dict:
        with self._lock:
            return {
                "submitted": self._submitted,
                "completed": self._completed,
                "dropped": self._dropped,
                "queue_limit": self._queue_limit,
                "closed": self._closed,
            }

    def close(self, wait: bool = True) -> None:
        """Shut the executor down; already-queued work finishes only when ``wait`` is set."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait)

    def _run(self, case: ShadowCase) -> None:
        comparison = None
        try:
            comparison = self._evaluate(case)
        except Exception:
            logger.warning("Shadow evaluation failed", exc_info=False)
        finally:
            self._slots.release()
            with self._lock:
                self._completed += 1
        if comparison is not None and self._on_comparison is not None:
            try:
                self._on_comparison(comparison)
            except Exception:
                logger.warning("Shadow comparison sink failed", exc_info=False)

    def _evaluate(self, case: ShadowCase) -> ShadowComparison:
        route_decision = self._jev.route(case.question, case.capabilities)
        selection = self._jev.select(case.question, case.candidates, query=case.query)
        failure_kind = None
        reason = route_decision.reason
        if reason.startswith("preflight_unavailable:"):
            failure_kind = reason.split(":", 1)[1]
        overlap = 0.0
        if selection is not None and case.legacy_ranks:
            legacy_ids = {identity for identity, _ in case.legacy_ranks}
            jev_ids = {f"{chunk.doc_name}#{chunk.chunk_index}" for chunk in selection.accepted}
            if legacy_ids:
                overlap = len(legacy_ids & jev_ids) / len(legacy_ids)
        return ShadowComparison(
            legacy_route=case.legacy_route,
            jev_route=route_decision.route,
            route_agrees=case.legacy_route == route_decision.route,
            jev_available=selection is not None,
            failure_kind=failure_kind,
            top_k_overlap=overlap,
        )


__all__ = ["ShadowCase", "ShadowComparison", "ShadowEvaluator"]
