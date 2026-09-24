"""Bounded, non-blocking shadow comparison of a candidate Jev question set.

Layer 3 of the candidate gates compares the **current** Jev question set with a
**candidate** one on sampled live questions. The current path keeps running
untouched: each sampled question is judged once by each service on a bounded
executor, work is dropped instead of blocking when the queue is full, and any
candidate failure is recorded as an availability difference — never as a changed
response, never as an exception for the caller, never as a second effect on the
current side.

The execution pattern is the one :mod:`rag.jev.shadow` already uses for the
Jev-vs-legacy comparison (bounded executor + bounded semaphore + queue-full
drop + ``on_comparison`` sink). What changes is *what* is compared: two question
sets behind the same ``JevDecisionService`` route/select facade, so a live
request can hand over its own question, kb, query and ACL-filtered candidates
and have both sides judged from the same input.

Two deliberate scope decisions:

* ``submit()`` carries no capability summary, so both sides are judged with a
  neutral :class:`~rag.jev.policy.KbCapabilities`. The comparison then measures
  the question set's own effect, not the live request's permission context, and
  both sides always see the same capabilities.
* ``CandidateShadowComparison`` keeps the caller's question (the sink and the
  summary correlate on it), while :meth:`CandidateShadowEvaluator.comparisons`
  projects it to a stable digest, so no raw question, chunk or key text can
  leave through the in-memory buffer.

Sampling stability: the decision is ``hash((question, kb_id)) % 1000 <
sample_rate * 1000``, exactly as specified. CPython randomizes ``str`` hashing
per process unless ``PYTHONHASHSEED`` is fixed, so the same pair is sampled the
same way for the lifetime of one process (one server, one test run) but the
*same* question may be sampled differently in the next process. That is enough
for a 5% shadow load and for reproducible tests; if cross-process reproducibility
ever becomes a requirement (correlating samples across restarts), the bucket
should be computed from a stable digest instead.
"""

import hashlib
import logging
import threading
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from rag.jev.policy import KbCapabilities, PassageSelection
from rag.models import Chunk

logger = logging.getLogger(__name__)

#: Background workers for the shadow judgments; the same default
#: :class:`~rag.jev.shadow.ShadowEvaluator` uses.
_SHADOW_WORKERS = 2

#: Retained comparisons. ``summary()`` aggregates this window, so a busy instance
#: reports a rolling recent picture instead of growing without bound.
_MAX_BUFFERED_COMPARISONS = 512

#: Digest size of the content-free question handle (16 hex characters).
_DIGEST_BYTES = 8

#: Sampling arithmetic: ``sample_rate`` is compared against a permille bucket.
_SAMPLE_BUCKETS = 1000

_CURRENT = "current"
_CANDIDATE = "candidate"
_PREFLIGHT_UNAVAILABLE = "preflight_unavailable:"
_ERROR = "error:"
_PASSAGES_UNAVAILABLE = "passages_unavailable"
_UNKNOWN_FAILURE = "unknown"


@dataclass(frozen=True)
class CandidateShadowComparison:
    """One recorded candidate-vs-current comparison for one sampled question."""

    question: str
    route_agrees: bool
    selection_agrees: float | None
    current_available: bool
    candidate_available: bool
    failure_kind: str | None


@dataclass(frozen=True)
class _ShadowRequest:
    """One sampled question plus the state needed to judge it twice."""

    question: str
    kb_id: str
    query: str
    candidates: tuple[Chunk, ...]


@dataclass(frozen=True)
class _SideOutcome:
    """What one service answered for one sampled question, failures normalized."""

    available: bool
    route: str | None
    selection: PassageSelection | None
    failure: str | None


class CandidateShadowEvaluator:
    """Judges sampled live questions with both question sets on a bounded executor.

    ``current_service`` and ``candidate_service`` are ``JevDecisionService``
    facades (or test doubles with the same ``route``/``select`` signatures). The
    candidate side is the one under evaluation, so its failures are contained:
    an exception, an unavailable preflight batch or a missing passage selection
    only ever sets ``candidate_available=False``. The current side is judged
    exactly as the live path would judge it, and its outcome is never derived
    from the candidate's.
    """

    def __init__(
        self,
        current_service,
        candidate_service,
        *,
        sample_rate: float = 0.05,
        queue_limit: int = 64,
        on_comparison: Callable[[CandidateShadowComparison], None] | None = None,
    ):
        if not 0.0 <= sample_rate <= 1.0:
            raise ValueError("sample_rate must be within [0.0, 1.0]")
        if int(queue_limit) < 1:
            raise ValueError("queue_limit must be >= 1")
        self._current = current_service
        self._candidate = candidate_service
        self._sample_rate = float(sample_rate)
        self._on_comparison = on_comparison
        self._queue_limit = int(queue_limit)
        self._executor = ThreadPoolExecutor(max_workers=_SHADOW_WORKERS, thread_name_prefix="jev-candidate-shadow")
        self._slots = threading.BoundedSemaphore(self._queue_limit)
        self._lock = threading.Lock()
        self._comparisons: deque[CandidateShadowComparison] = deque(maxlen=_MAX_BUFFERED_COMPARISONS)
        self._not_sampled = 0
        self._sampled = 0
        self._completed = 0
        self._dropped = 0
        self._closed = False

    # ------------------------------------------------------------------
    # Submitting work.
    # ------------------------------------------------------------------

    def submit(
        self,
        question: str,
        *,
        kb_id: str,
        query: str,
        candidates: Sequence[Chunk] = (),
    ) -> bool:
        """Queue one sampled comparison without ever blocking the caller.

        Sampling is a deterministic function of ``(question, kb_id)`` —
        ``hash((question, kb_id)) % 1000 < sample_rate * 1000`` — so the same
        pair is always sampled the same way inside one process (which keeps the
        5% shadow load reproducible and testable; see the module note on
        cross-process stability). Python's modulo keeps the bucket inside
        ``0..999`` even for a negative hash.

        Returns ``True`` only when the comparison was queued. An unsampled
        question, a full queue and a closed evaluator all return ``False``
        immediately.
        """
        request = _ShadowRequest(
            question=str(question),
            kb_id=str(kb_id),
            query=str(query),
            candidates=tuple(candidates),
        )
        if not self._sampled_in(request):
            with self._lock:
                self._not_sampled += 1
            return False
        with self._lock:
            if self._closed:
                self._dropped += 1
                return False
            self._sampled += 1
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._sampled -= 1
                self._dropped += 1
            return False
        try:
            self._executor.submit(self._run, request)
        except RuntimeError:
            # The executor was shut down between the check and the submit.
            self._slots.release()
            with self._lock:
                self._sampled -= 1
                self._dropped += 1
            return False
        return True

    def _sampled_in(self, request: _ShadowRequest) -> bool:
        """Deterministic per-(question, kb_id) sampling decision."""
        return hash((request.question, request.kb_id)) % _SAMPLE_BUCKETS < self._sample_rate * _SAMPLE_BUCKETS

    # ------------------------------------------------------------------
    # Reading the comparison window.
    # ------------------------------------------------------------------

    def comparisons(self, limit: int = 50) -> list[dict]:
        """The most recent comparisons as content-free dicts, oldest first.

        ``limit=0`` (or a negative limit) returns nothing. The question itself is
        replaced by a stable digest so the buffer never exposes raw text.
        """
        count = max(int(limit), 0)
        if count == 0:
            return []
        with self._lock:
            recent = list(self._comparisons)[-count:]
        return [_project(comparison) for comparison in recent]

    def summary(self) -> dict:
        """Aggregate the retained comparisons plus the executor counters.

        Rates are ``None`` until the first relevant sample exists, so an empty
        evaluator reports ``None`` rather than a misleading ``0.0``. The
        availability difference is a count (``current - candidate``): a positive
        value means the candidate question set was available less often.
        """
        with self._lock:
            records = list(self._comparisons)
            sampled = self._sampled
            not_sampled = self._not_sampled
            dropped = self._dropped
            closed = self._closed
        samples = len(records)
        selections = [record.selection_agrees for record in records if record.selection_agrees is not None]
        current_available = sum(1 for record in records if record.current_available)
        candidate_available = sum(1 for record in records if record.candidate_available)
        return {
            "samples": samples,
            "route_agreement": _rate(sum(1 for record in records if record.route_agrees), samples),
            "selection_agreement_mean": _rate(sum(selections), len(selections)),
            "selection_agreement_samples": len(selections),
            "current_available": current_available,
            "candidate_available": candidate_available,
            "availability_difference": current_available - candidate_available,
            "sampled": sampled,
            "not_sampled": not_sampled,
            "dropped": dropped,
            "sample_rate": self._sample_rate,
            "queue_limit": self._queue_limit,
            "closed": closed,
        }

    def stats(self) -> dict:
        """Executor health: queued, skipped, completed, dropped and closed counts."""
        with self._lock:
            return {
                "sampled": self._sampled,
                "not_sampled": self._not_sampled,
                "completed": self._completed,
                "dropped": self._dropped,
                "queue_limit": self._queue_limit,
                "sample_rate": self._sample_rate,
                "closed": self._closed,
            }

    def close(self, wait: bool = True) -> None:
        """Shut the executor down; closing twice is a no-op."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait)

    # ------------------------------------------------------------------
    # Internals.
    # ------------------------------------------------------------------

    def _run(self, request: _ShadowRequest) -> None:
        comparison = None
        try:
            comparison = self._evaluate(request)
        except Exception:
            logger.warning("Candidate shadow evaluation failed", exc_info=False)
        finally:
            self._slots.release()
            with self._lock:
                self._completed += 1
        if comparison is None:
            return
        with self._lock:
            self._comparisons.append(comparison)
        if self._on_comparison is not None:
            try:
                self._on_comparison(comparison)
            except Exception:
                logger.warning("Candidate shadow comparison sink failed", exc_info=False)

    def _evaluate(self, request: _ShadowRequest) -> CandidateShadowComparison:
        """Judge one sampled question with both question sets and record the diff."""
        current = self._judge(self._current, request)
        candidate = self._judge(self._candidate, request)
        return CandidateShadowComparison(
            question=request.question,
            route_agrees=(
                current.route is not None and candidate.route is not None and current.route == candidate.route
            ),
            selection_agrees=_selection_agreement(current.selection, candidate.selection),
            current_available=current.available,
            candidate_available=candidate.available,
            failure_kind=_failure_kind(current, candidate),
        )

    def _judge(self, service, request: _ShadowRequest) -> _SideOutcome:
        """Judge one question with one service, normalizing every failure.

        ``route`` and ``select`` follow the ``JevDecisionService`` signatures.
        An exception, an unavailable preflight batch (``reason`` prefixed with
        ``preflight_unavailable:``) and a ``None`` passage selection all become
        ``available=False`` plus a failure kind, so a broken side can never raise
        into the comparison loop or into the online path.
        """
        try:
            decision = service.route(request.question, KbCapabilities())
        except Exception as error:
            return _SideOutcome(False, None, None, f"{_ERROR}{type(error).__name__}")
        route = getattr(decision, "route", None)
        failure = _route_failure(decision)
        if failure is not None:
            return _SideOutcome(False, route, None, failure)
        if not request.candidates:
            return _SideOutcome(True, route, None, None)
        try:
            selection = service.select(request.question, request.candidates, query=request.query)
        except Exception as error:
            return _SideOutcome(False, route, None, f"{_ERROR}{type(error).__name__}")
        if selection is None:
            return _SideOutcome(False, route, None, _PASSAGES_UNAVAILABLE)
        return _SideOutcome(True, route, selection, None)


def _route_failure(decision: object) -> str | None:
    """The normalized failure kind a route decision carries, if any.

    A ``legacy`` route reached by the deterministic policy (low confidence,
    missing capability) is a decision, not a failure: only the
    ``preflight_unavailable:<kind>`` reason marks the service as unavailable.
    """
    reason = getattr(decision, "reason", "") or ""
    if reason.startswith(_PREFLIGHT_UNAVAILABLE):
        return reason.split(":", 1)[1] or _UNKNOWN_FAILURE
    return None


def _selection_agreement(current: PassageSelection | None, candidate: PassageSelection | None) -> float | None:
    """Intersection over union of the accepted chunks; ``None`` without a judgment.

    Both sides accepting nothing is full agreement (an empty union), one side
    accepting what the other did not is measured as the shared share.
    """
    if current is None or candidate is None:
        return None
    current_ids = _accepted_ids(current)
    candidate_ids = _accepted_ids(candidate)
    union = current_ids | candidate_ids
    if not union:
        return 1.0
    return len(current_ids & candidate_ids) / len(union)


def _accepted_ids(selection: PassageSelection) -> set[str]:
    """Accepted-chunk identity: the same ``doc_name#chunk_index`` convention
    :mod:`rag.jev.shadow` uses, so both shadow layers rank agreement alike."""
    return {f"{chunk.doc_name}#{chunk.chunk_index}" for chunk in selection.accepted}


def _failure_kind(current: _SideOutcome, candidate: _SideOutcome) -> str | None:
    """``<side>:<kind>`` for a failing side, ``None`` when both sides answered.

    The candidate side is reported first when both fail, because it is the
    component under evaluation; the per-side truth stays in the two availability
    flags and in ``summary()``.
    """
    for side, outcome in ((_CANDIDATE, candidate), (_CURRENT, current)):
        if outcome.failure is not None:
            return f"{side}:{outcome.failure}"
    return None


def _project(comparison: CandidateShadowComparison) -> dict:
    """One content-free comparison row, the question replaced by a digest."""
    return {
        "question_digest": _digest(comparison.question),
        "route_agrees": comparison.route_agrees,
        "selection_agrees": comparison.selection_agrees,
        "current_available": comparison.current_available,
        "candidate_available": comparison.candidate_available,
        "failure_kind": comparison.failure_kind,
    }


def _digest(question: str) -> str:
    """A stable, one-way handle for correlating comparisons without the text."""
    return hashlib.blake2b(question.encode("utf-8"), digest_size=_DIGEST_BYTES).hexdigest()


def _rate(count: int, total: int) -> float | None:
    """A share in ``[0, 1]``; ``None`` when nothing was measured yet."""
    if total <= 0:
        return None
    return count / total


__all__ = ["CandidateShadowComparison", "CandidateShadowEvaluator"]
