"""Bounded, non-blocking recording of ACL-filtered diagnosable failures.

The pattern is lifted from ``rag/jev/shadow.py``: one bounded semaphore plus a
single-worker executor, so ``submit`` returns immediately, drops work instead of
blocking when the queue is full, and never alters the online response. Skips
(guard/ACL denials, transient external failures) are counted in the skip log and
never become observations.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from rag.evolution.models import DiagnosticDetail, FailureObservation, NonEvolvableReason
from rag.evolution.store import FailureTraceStore
from rag.tracker import ExecutionTrace, _sanitize_decision_metadata

logger = logging.getLogger(__name__)


def _metadata_str(metadata: dict, key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


class FailureRecorder:
    """Queues failure observations for a single background writer thread."""

    def __init__(
        self,
        store: FailureTraceStore,
        *,
        max_queue: int = 256,
        max_workers: int = 1,
    ):
        self._store = store
        self._queue_limit = max_queue
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="evolution-record")
        self._slots = threading.BoundedSemaphore(max_queue)
        self._lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._dropped = 0
        self._skipped = 0
        self._closed = False

    def submit(self, trace: ExecutionTrace, detail: DiagnosticDetail | None = None) -> bool:
        """Queue one observation without blocking the caller; drop it when full.

        A missing detail means nothing diagnosable to record. Guard/ACL denials
        and transient external failures are logged as skips instead of
        observations, so a blocked request can never produce an observation.
        """
        if detail is None:
            return False
        with self._lock:
            if self._closed:
                self._dropped += 1
                return False
            self._submitted += 1
        reason = self._skip_reason(trace, detail)
        if reason is not None:
            with self._lock:
                self._skipped += 1
            return self._queue(self._log_skip, reason, trace, detail)
        return self._queue(self._record, trace, detail)

    def stats(self) -> dict:
        """Counters for operator reconciliation; closed means submit is rejected."""
        with self._lock:
            return {
                "submitted": self._submitted,
                "completed": self._completed,
                "dropped": self._dropped,
                "skipped": self._skipped,
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

    def _queue(self, work, *args) -> bool:
        """Run one bounded task without ever blocking the online path."""
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._dropped += 1
            return False
        try:
            self._executor.submit(self._run, work, *args)
        except RuntimeError:
            self._slots.release()
            with self._lock:
                self._dropped += 1
            return False
        return True

    def _run(self, work, *args) -> None:
        try:
            work(*args)
        except Exception:
            logger.warning("Evolution recording failed", exc_info=False)
        finally:
            self._slots.release()
            with self._lock:
                self._completed += 1

    def _record(self, trace: ExecutionTrace, detail: DiagnosticDetail) -> None:
        self._store.add_observation(self._observation(trace, detail))

    def _log_skip(self, reason: NonEvolvableReason, trace: ExecutionTrace, detail: DiagnosticDetail) -> None:
        self._store.log_skip(
            reason, question=trace.question, route=trace.route, kb_id=detail.kb_id
        )

    @staticmethod
    def _skip_reason(trace: ExecutionTrace, detail: DiagnosticDetail) -> NonEvolvableReason | None:
        if trace.route == "blocked":
            return NonEvolvableReason.PERMISSION_DENIED
        return detail.non_evolvable_reason

    @staticmethod
    def _observation(trace: ExecutionTrace, detail: DiagnosticDetail) -> FailureObservation:
        # The tracker whitelist is the single source of truth for decision metadata.
        metadata = _sanitize_decision_metadata(dict(trace.decision_metadata or {}))
        version = detail.jev_question_set_version or _metadata_str(metadata, "question_set_version")
        return FailureObservation(
            kb_id=detail.kb_id,
            failure_stage=detail.failure_stage,
            question=trace.question,
            route=trace.route,
            answerability_kind=detail.answerability_kind,
            confidence_level=detail.confidence_level,
            chunk_hashes=detail.chunk_hashes or tuple(trace.chunk_hashes or ()),
            best_score=detail.best_score,
            adopted_evidence_hashes=detail.adopted_evidence_hashes,
            tool_names=detail.tool_names or tuple(call.tool_name for call in trace.tool_calls or ()),
            structured_query_kind=detail.structured_query_kind,
            exception_class=detail.exception_class,
            exception_frame=detail.exception_frame,
            feedback_kind=detail.feedback_kind,
            jev_question_set_version=version,
            decision_metadata=metadata,
            total_ms=float(trace.total_ms or 0.0),
        )


__all__ = ["FailureRecorder"]
