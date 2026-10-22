"""Budgeted, busy-aware background worker for the observation phase.

The worker runs one clustering round at a time on a daemon thread, reusing the
``rag/backups/scheduler.py`` stop/pause pattern. Three budget dimensions bound
every round: wall-clock run seconds, batch size and model calls (phase 3 drafts
diagnoses within ``evolution_diagnosis_model_calls`` per round, so a round that
reports more calls than its budget ends as budget-exhausted). A busy system
pauses rounds without ever dropping queued observations, and one failing batch —
or one failing diagnosis — can never kill the thread: the round is logged and
the loop keeps running. Nothing here can change an online response, a candidate
or a gate.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from config import settings
from rag.evolution.candidate_store import OPEN_CANDIDATE_STATUSES
from rag.evolution.host_lock import HostLocked, host_measurement_lock
from rag.evolution.models import EvolutionStatus

logger = logging.getLogger(__name__)

_PAUSE_POLL_SECONDS = 0.1

#: Floor for a model call's own timeout once the round's wall clock is nearly spent. A call that
#: cannot be given at least this much time is not started (the step's loop has already stopped
#: scheduling work by then); the floor exists so a rounding-to-zero remaining budget cannot turn
#: into an unbounded call.
_MIN_MODEL_TIMEOUT_S = 1.0

#: Filename of the worker's cross-process lease, created next to ``evolution_db_path``. Two workers
#: (the API-owned thread and ``python -m rag.evolution.worker``) share one evolution database, so a
#: round that measures or writes must not run twice at the same time.
HOST_LEASE_FILENAME = "evolution-worker.lock"

#: Re-retrieval width for one diagnosis reproduction check. The evidence module
#: caps its own recall the same way; the worker only owns the request.
_DIAGNOSIS_RECALL_TOP_K = 24

#: 每个观测在一轮内最多尝试几次模型调用。第二次只用于 ``invalid``（模型没遵守输出契约）：
#: 同一证据包换一次采样往往就合规（实测 ranking 族的截断属此类），而超时/限流是容量信号，
#: 重试只会更快耗尽预算。重试计入每轮调用预算，所以这是成本上限而不是绕过它。
_MAX_DIAGNOSIS_ATTEMPTS = 2

#: A round without a gap-ticket step reports no ticket work (and costs nothing: it has no model).
_NO_GAPS: dict = {
    "tickets_created": 0,
    "tickets_pending": 0,
    "ticket_questions_probed": 0,
    "tickets_rechecked": 0,
    "tickets_updated": 0,
}

#: A round without a candidate step reports no candidate work.
_NO_CANDIDATE: dict = {
    "candidates_created": 0,
    "candidate_failed": 0,
    "candidate_pending": 0,
    "candidate_model_calls": 0,
}

#: A round without a patch step reports no patch work, exactly like a pre-phase-5 round.
_NO_PATCH: dict = {
    "patches_created": 0,
    "patch_failed": 0,
    "patch_pending": 0,
    "patch_model_calls": 0,
}

#: A round without a diagnosis step (phase 2 builds, or a caller that wires only
#: clustering) reports no diagnosis work at all.
_NO_DIAGNOSIS: dict = {
    "diagnosed": 0,
    "diagnosis_failed": 0,
    "model_calls": 0,
    "diagnosis_config": None,
}


class BudgetExhausted(Exception):
    """Raised (or reported) when a round exceeds one of the worker budgets."""

    def __init__(self, reason: str = "budget_exhausted"):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EvolutionBudget:
    """Per-round budgets; ``max_concurrency`` is structurally 1 (one round at a time).

    Phase 3 added the fourth live dimension: ``max_model_calls`` bounds the
    diagnosis calls of one round and comes from
    ``settings.evolution_diagnosis_model_calls``. A round that reports more calls
    than this ceiling ends as budget-exhausted; a round that stops *at* the
    ceiling still reports ``ok`` and leaves the remaining clusters to the next
    round.
    """

    max_concurrency: int = 1
    max_batch: int = 200
    max_run_seconds: float = 120.0
    max_model_calls: int = 0
    max_rss_mb: float | None = None

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.max_batch < 1:
            raise ValueError("max_batch must be >= 1")
        if self.max_run_seconds is not None and self.max_run_seconds < 0:
            raise ValueError("max_run_seconds must be >= 0")
        if self.max_model_calls < 0:
            raise ValueError("max_model_calls must be >= 0")
        if self.max_rss_mb is not None and self.max_rss_mb <= 0:
            raise ValueError("max_rss_mb must be > 0")


@dataclass(frozen=True)
class RoundClock:
    """The one round's wall clock and cancellation flag, handed to every step (C-07).

    Two things were missing from the budget model, and both are visible from outside: the round
    only *recorded* a deadline after its work had run, and a step had no way to learn that the round
    was over, cancelled or nearly out of time. A step that ignores the clock can only be discovered
    afterwards — as a round that took far longer than its budget, with a model call still running.

    So the worker publishes this object through a :class:`~contextvars.ContextVar` for the duration
    of one round, and every step reads it:

    * ``expired()`` is the cooperative stop signal — steps stop *scheduling* work (the remaining
      items wait for the next round) instead of the round being torn down and losing its counters;
    * ``remaining()`` / ``model_timeout()`` bound a single model call by the time the round has
      left, so a slow provider eats the round's budget instead of overrunning it;
    * ``cancelled`` is the worker's stop event: ``stop()`` asks the round in flight to wind down,
      which is what makes a shutdown not wait for the next batch of observations.

    ``clock`` is injected (the worker takes one for tests), so "expired" is decided by the same
    monotonic source that computed the deadline.
    """

    started: float
    deadline: float | None
    cancelled: threading.Event
    clock: Callable[[], float]

    def remaining(self) -> float | None:
        """Seconds left in the round, or ``None`` when it has no wall-clock budget."""
        if self.deadline is None:
            return None
        return self.deadline - self.clock()

    def expired(self) -> bool:
        """True when the round's time is spent or the worker asked it to stop."""
        if self.cancelled.is_set():
            return True
        return self.deadline is not None and self.clock() >= self.deadline

    def model_timeout(self, base: float) -> float:
        """``base`` seconds, capped by the time this round has left.

        A model call is the one unbounded operation in a round (the provider decides how long to
        take), so it is the one that must inherit the round's deadline. The floor keeps a nearly
        spent round from asking for a zero-second call.
        """
        remaining = self.remaining()
        if remaining is None:
            return float(base)
        return max(_MIN_MODEL_TIMEOUT_S, min(float(base), float(remaining)))


#: The clock of the round currently executing on this thread/context, if any.
_current_round: ContextVar[RoundClock | None] = ContextVar(
    "evolution_worker_round_clock", default=None
)


def current_round() -> RoundClock | None:
    """The round in flight, or ``None`` when a step is called outside a worker round."""
    return _current_round.get()


def round_expired() -> bool:
    """True when the current round is out of time or cancelled (``False`` outside a round).

    Steps use this between units of work: returning early keeps the counters they already produced,
    and the worker still reports the round as ``budget_exhausted`` because its deadline has passed.
    Outside a round (a step called directly by a test or a script) there is no budget to enforce,
    so the answer is ``False`` — the previous behaviour.
    """
    clock = _current_round.get()
    return clock is not None and clock.expired()


def round_model_timeout(base: float) -> float:
    """A model call's timeout, capped by the current round's remaining time (C-07)."""
    clock = _current_round.get()
    return float(base) if clock is None else clock.model_timeout(base)


def default_busy_provider() -> bool:
    """True when the 1-minute load average exceeds the configured factor x CPUs.

    ``os.getloadavg`` is unavailable on Windows, so the answer there is False:
    the administrator pauses explicitly with ``pause()`` (or supplies a
    provider), which the operations doc records.
    """
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        return False
    cpus = os.cpu_count() or 1
    return load1 > settings.evolution_worker_busy_load_factor * cpus


class EvolutionWorker:
    """Daemon-thread worker with per-round budgets, busy pause and error isolation.

    ``lock_path`` enables the **cross-process** lease (C-07, 2026-10-22): the round takes
    ``host_measurement_lock(lock_path)`` before doing anything, and a round that finds the lease held
    by another process reports ``busy`` instead of running a second time against the same evolution
    database. :func:`build_evolution_worker` always configures it (next to ``evolution_db_path``);
    the default ``None`` keeps the in-process lock only, which is what a directly constructed worker
    in a test needs.
    """

    def __init__(
        self,
        run_once: Callable[[], object],
        *,
        interval_seconds: float,
        budget: EvolutionBudget | None = None,
        busy_provider: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
        rss_provider: Callable[[], float | None] | None = None,
        lock_path: str | Path | None = None,
        name: str = "rag-evolution-worker",
    ):
        self._run_once = run_once
        self._interval_seconds = interval_seconds
        self._budget = budget or EvolutionBudget()
        self._busy_provider = busy_provider or default_busy_provider
        self._clock = clock or time.monotonic
        self._rss_provider = rss_provider
        self._lock_path = None if lock_path is None else Path(lock_path)
        self._name = name
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._round_lock = threading.Lock()  # max_concurrency=1, enforced for inline runs too
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._runs = 0
        self._failed_rounds = 0
        self._busy_rounds = 0
        self._lease_busy_rounds = 0
        self._cancelled_rounds = 0
        self._budget_exhausted = 0
        self._last_budget_reason: str | None = None
        self._last_status = "idle"
        self._last_diagnosis_config: dict | None = None

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def budget(self) -> EvolutionBudget:
        return self._budget

    @property
    def host_lease_path(self) -> Path | None:
        """The cross-process lease file this worker takes, or ``None`` when it takes none."""
        return self._lock_path

    def start(self) -> None:
        with self._state_lock:
            if self.is_alive:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._state_lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._state_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None

    def pause(self) -> None:
        """Pause rounds; queued observations stay queued (nothing is dropped)."""
        self._resume_event.clear()
        self._pause_event.set()

    def resume(self) -> None:
        self._pause_event.clear()
        self._resume_event.set()

    def run_once_when_idle(self) -> str:
        """Run one round now unless the system is busy, paused or out of budget."""
        if self._pause_event.is_set():
            return "busy"
        return self._execute_round()

    def stats(self) -> dict:
        with self._state_lock:
            return {
                "runs": self._runs,
                "last_status": self._last_status,
                "paused": self._pause_event.is_set(),
                "busy": self._is_busy(),
                "budget_exhausted": self._budget_exhausted,
                "failed": self._failed_rounds,
                "busy_rounds": self._busy_rounds,
                "lease_busy": self._lease_busy_rounds,
                "cancelled": self._cancelled_rounds,
                "last_budget_reason": self._last_budget_reason,
                "last_diagnosis_config": self._last_diagnosis_config,
                "alive": self.is_alive,
            }

    # ── internals ──────────────────────────────────────────────────────────

    def _execute_round(self) -> str:
        """Run one round under the concurrency budget; a round in flight means busy."""
        if not self._round_lock.acquire(blocking=False):
            self._record("busy")
            return "busy"
        try:
            status = self._run_round_under_lease()
        finally:
            self._round_lock.release()
        self._record(status)
        return status

    def _run_round_under_lease(self) -> str:
        """Take the cross-process lease, then run the round; a held lease means busy.

        The lease is the same ``O_CREAT | O_EXCL`` file lock the host A/B uses
        (:mod:`rag.evolution.host_lock`), so it is atomic across processes and a run killed without
        cleanup does not block the next one forever. ``busy`` is the honest answer for a held lease:
        queueing a second round would run two provider-heavy measurements at once, which is exactly
        what the lock exists to prevent.
        """
        if self._lock_path is None:
            return self._run_round()
        try:
            with host_measurement_lock(self._lock_path) as takeover:
                if takeover:
                    logger.warning(
                        "evolution_worker_host_lease_takeover holder_pid=%s",
                        takeover.get("pid"),
                    )
                return self._run_round()
        except HostLocked as error:
            with self._state_lock:
                self._lease_busy_rounds += 1
            logger.info("evolution_worker_round status=busy reason=host_lease detail=%s", error)
            return "busy"

    def _run_round(self) -> str:
        if self._is_busy():
            return "busy"
        budget = self._budget
        started = self._clock()
        deadline = started + budget.max_run_seconds if budget.max_run_seconds is not None else None
        if deadline is not None and self._clock() >= deadline:
            # A non-positive run-seconds budget never starts a round at all.
            return self._exhaust("max_run_seconds")
        # Publish this round's clock to every step for the duration of the round (C-07): a step
        # stops scheduling work once it expires, and the model calls it does start are bounded by
        # the time left. ``_stop_event`` doubles as the cancellation flag, so ``stop()`` reaches a
        # round that is already running.
        round_clock = RoundClock(
            started=started, deadline=deadline, cancelled=self._stop_event, clock=self._clock
        )
        token = _current_round.set(round_clock)
        try:
            result = self._run_once()
        except BudgetExhausted as error:
            return self._exhaust(str(error.reason))
        except Exception:
            logger.exception("evolution_worker_round_failed")
            with self._state_lock:
                self._failed_rounds += 1
            return "failed"
        finally:
            _current_round.reset(token)
            with self._state_lock:
                self._runs += 1
        self._record_diagnosis_config(result)
        # One line per round whatever the outcome: the worker runs in a background
        # thread of the API process, and without it "is it running, and is it doing
        # anything" is unanswerable from outside — the only other records on this
        # path are failures and budget warnings.
        counters = _round_counts(result)
        if self._stop_event.is_set():
            # ``stop()`` asked this round to wind down and the steps honoured it: that is a
            # cancellation, not a budget problem, and the two are reported apart so a shutdown
            # does not look like an exhausted budget.
            with self._state_lock:
                self._cancelled_rounds += 1
            status = "cancelled"
        elif deadline is not None and self._clock() >= deadline:
            status = self._exhaust("max_run_seconds")
        elif self._over_model_budget(result):
            status = self._exhaust("max_model_calls")
        elif self._over_rss_budget():
            status = self._exhaust("max_rss_mb")
        else:
            status = "ok"
        logger.info("evolution_worker_round status=%s %s", status, counters)
        return status

    def _over_model_budget(self, result: object) -> bool:
        model_calls = _reported_model_calls(result)
        if model_calls is None:
            return False
        return model_calls > self._budget.max_model_calls

    def _over_rss_budget(self) -> bool:
        if self._budget.max_rss_mb is None or self._rss_provider is None:
            return False
        rss = self._rss_provider()
        return rss is not None and rss > self._budget.max_rss_mb

    def _exhaust(self, reason: str) -> str:
        with self._state_lock:
            self._budget_exhausted += 1
            self._last_budget_reason = reason
        logger.warning("evolution_worker_budget_exhausted reason=%s", reason)
        return "budget_exhausted"

    def _record(self, status: str) -> None:
        with self._state_lock:
            self._last_status = status
            if status == "busy":
                self._busy_rounds += 1

    def _record_diagnosis_config(self, result: object) -> None:
        """Keep the last diagnosis configuration snapshot observable in ``stats()``."""
        if not isinstance(result, Mapping) or "diagnosis_config" not in result:
            return
        with self._state_lock:
            self._last_diagnosis_config = dict(result["diagnosis_config"] or {}) or None

    def _is_busy(self) -> bool:
        try:
            return bool(self._busy_provider())
        except Exception:
            logger.exception("evolution_worker_busy_provider_failed")
            return True  # an unknown system state skips the round; nothing is dropped

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                self._resume_event.wait(_PAUSE_POLL_SECONDS)
                continue
            self._execute_round()
            if self._stop_event.wait(self._interval_seconds):
                break


def _reported_model_calls(result: object) -> int | None:
    """Model-call accounting seam: a run may report calls in its result mapping.

    Phase 3's diagnosis step reports the calls it actually spent (successful and
    failed ones alike), so a round that overspends its configured
    ``max_model_calls`` ends as budget-exhausted while a round that stops exactly
    at the ceiling still reports ``ok`` and defers the rest to the next round.
    """
    if isinstance(result, Mapping) and "model_calls" in result:
        try:
            return int(result["model_calls"])
        except (TypeError, ValueError):
            return None
    return None


def _round_counts(result: object) -> str:
    """One round's counters as a log fragment; an unknown shape stays unread.

    ``counters=unavailable`` is deliberate: the line must never depend on the
    round's return shape, because that shape belongs to whoever built the round
    function (``make_evolution_run_once`` in production, a lambda in tests).
    """
    if not isinstance(result, Mapping):
        return "counters=unavailable"
    cluster_once = result.get("cluster_once")
    if not isinstance(cluster_once, Mapping):
        cluster_once = {}
    fields = (
        ("scanned", cluster_once.get("scanned")),
        ("clustered", cluster_once.get("clustered")),
        ("promoted", cluster_once.get("promoted")),
        ("diagnosed", result.get("diagnosed")),
        ("failed", result.get("diagnosis_failed")),
        ("model_calls", result.get("model_calls")),
        ("purged", result.get("purged")),
    )
    return " ".join(f"{name}={value}" for name, value in fields)


def _log_worker_started() -> None:
    """The one line that proves the worker thread is up.

    Both entry points log through here — the API lifespan and the standalone
    ``python -m rag.evolution.worker`` — so "is it running" has the same answer
    wherever it was started from. Before this, the API path logged nothing at
    all and only failures were visible.
    """
    logger.info(
        "evolution worker started interval=%ss run_seconds=%s batch=%s model_calls_per_round=%s",
        settings.evolution_worker_interval_seconds,
        settings.evolution_worker_run_seconds,
        settings.evolution_worker_max_batch,
        settings.evolution_diagnosis_model_calls,
    )


def purge_evolution_observations(store, *, retention_days: float, now: float | None = None) -> int:
    """Delete observations older than the retention window; clusters are kept."""
    moment = time.time() if now is None else now
    cutoff = moment - float(retention_days) * 86400.0
    return store.purge_observations_before(cutoff)


def make_evolution_run_once(
    clusterer,
    store,
    *,
    retention_days: float,
    batch_limit: int = 200,
    purge_every_rounds: int = 1,
    diagnosis_step: Callable[[], dict] | None = None,
    patch_step: Callable[[], dict] | None = None,
    candidate_step: Callable[[], dict] | None = None,
    gap_ticket_step: Callable[[], dict] | None = None,
) -> Callable[[], dict]:
    """Build the round function: clustering, diagnosis, patch generation, then retention.

    Clustering runs first so the purge can never shrink a cluster mid-round;
    the next round recomputes member counts from the surviving rows anyway.
    Diagnosis (phase 3) runs second — while every member observation is still
    present — and is bounded by its own per-round model-call budget. Without a
    ``diagnosis_step`` the round reports zero diagnosis work, exactly like the
    phase-2 rounds did.
    """
    rounds = {"n": 0}

    def run_step(step: Callable[[], dict] | None, empty: dict) -> dict:
        """Run one step unless the round's wall clock is already spent (C-07).

        Skipping instead of raising keeps what the earlier steps already counted: the round returns
        with its real counters and the worker marks it ``budget_exhausted`` because the deadline has
        passed. The remaining work is not lost — its items stay queued for the next round.
        """
        if step is None:
            return dict(empty)
        if round_expired():
            return dict(empty)
        return step()

    def run_once() -> dict:
        rounds["n"] += 1
        outcome = clusterer.cluster_once(limit=batch_limit)
        diagnosis = run_step(diagnosis_step, _NO_DIAGNOSIS)
        # 补丁生成在诊断之后：它只处理"已批准 + 已确认层级"的聚类，与诊断互不依赖。
        patch = run_step(patch_step, _NO_PATCH)
        candidate = run_step(candidate_step, _NO_CANDIDATE)
        gaps = run_step(gap_ticket_step, _NO_GAPS)
        purged = 0
        if purge_every_rounds > 0 and rounds["n"] % purge_every_rounds == 0:
            purged = purge_evolution_observations(store, retention_days=retention_days)
        return {
            "cluster_once": outcome,
            "diagnosed": diagnosis["diagnosed"],
            "diagnosis_failed": diagnosis["diagnosis_failed"],
            "patches_created": patch["patches_created"],
            "patch_failed": patch["patch_failed"],
            "patch_pending": patch["patch_pending"],
            "patch_model_calls": patch["patch_model_calls"],
            "candidates_created": candidate["candidates_created"],
            "candidate_failed": candidate["candidate_failed"],
            "candidate_pending": candidate["candidate_pending"],
            "candidate_model_calls": candidate["candidate_model_calls"],
            "tickets_created": gaps["tickets_created"],
            "tickets_pending": gaps["tickets_pending"],
            "ticket_questions_probed": gaps["ticket_questions_probed"],
            "tickets_rechecked": gaps["tickets_rechecked"],
            "tickets_updated": gaps["tickets_updated"],
            "model_calls": diagnosis["model_calls"],
            "purged": purged,
            "diagnosis_config": diagnosis["diagnosis_config"],
        }

    return run_once


def _close_quietly(resource) -> None:
    """Close one diagnosis resource; cleanup must never fail a round."""
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        logger.exception("evolution_diagnosis_close_failed")


class AdminReadOnlyRetrievalReader:
    """Read-only, ACL-filtered retrieval as the system admin principal.

    Diagnosis is an admin-only tool, so the re-retrieval behind the reproduction
    check runs as that same principal: ``UserDB.ensure_development_user()`` (id
    0, the development admin). The admin already has full knowledge-base
    visibility, so this path can never widen visibility beyond the pipeline's own
    ACL filter — it reuses ``RAGPipeline._retrieve_visible_candidates`` exactly
    like the online path does. The candidates only feed the evidence pack (chunk
    hashes and the deterministic reproduction check); only the pack and the draft
    reach ``evolution.db``, which is admin-visible. Nothing lands in the online
    trajectory or in a log line.

    One reader (and at most one pipeline per knowledge base) is created per
    diagnosis round and closed by :meth:`close`.
    """

    def __init__(
        self,
        users_db_path: str,
        *,
        top_k: int = _DIAGNOSIS_RECALL_TOP_K,
        pipeline_factory=None,
        user_db_factory=None,
    ) -> None:
        from rag.pipeline import RAGPipeline
        from rag.user_db import UserDB

        self._pipeline_factory = pipeline_factory or RAGPipeline
        self._user_db_factory = user_db_factory or UserDB
        self._users_db_path = users_db_path
        self._top_k = top_k
        self._pipelines: dict[str, object] = {}
        self._user_db = None
        self._admin: dict | None = None

    def _principal(self) -> tuple[object, dict]:
        """The admin principal, opening the round-owned user database on first use."""
        if self._user_db is None:
            self._user_db = self._user_db_factory(self._users_db_path)
            self._admin = self._user_db.ensure_development_user()
        return self._user_db, self._admin

    def _pipeline(self, kb_id: str):
        user_db, _ = self._principal()
        pipeline = self._pipelines.get(kb_id)
        if pipeline is None:
            pipeline = self._pipeline_factory(kb_id=kb_id, memory_db_path=":memory:", user_db=user_db)
            self._pipelines[kb_id] = pipeline
        return pipeline

    def retrieve(self, question: str, *, kb_id: str) -> list:
        """One read-only ACL retrieval for the evidence pack's reproduction check."""
        pipeline = self._pipeline(kb_id)
        return pipeline._retrieve_visible_candidates(
            question,
            top_k=self._top_k,
            doc_name=None,
            tags=None,
            user=self._admin,
            kb_id=kb_id,
        )

    def close(self) -> None:
        """Release the pipelines and the user database created for one round."""
        pipelines, self._pipelines = self._pipelines, {}
        user_db, self._user_db, self._admin = self._user_db, None, None
        for pipeline in pipelines.values():
            _close_quietly(pipeline)
        _close_quietly(user_db)


def make_evolution_diagnosis_step(
    store,
    *,
    settings_obj=None,
    model_call_budget: int = 0,
    diagnoser_factory=None,
    tracker_factory=None,
    retrieval_factory=None,
) -> Callable[[], dict]:
    """Build the per-round diagnosis step for member observations without a draft.

    Phase 4 drafts one diagnosis per member **observation** instead of per
    cluster (``docs/evolution-diagnosis.md`` §5): a cluster key mixes distinct
    root causes, so a cluster-level draft can never match a mixed label set. The
    step reads ``observations_awaiting_diagnosis_without_draft`` and drafts at
    most ``model_call_budget`` observations, oldest first. Every attempted
    observation costs one unit of that budget — a successful and a failed
    diagnosis alike — so a round always stays bounded even when the model is
    slow, rate-limited or answers off-contract. An observation that fails keeps
    waiting for its own draft (no draft means the next round retries it), and no
    single-observation failure can end the round early: the remaining
    observations of the round are still diagnosed.

    The step returns ``{"diagnosed", "diagnosis_failed", "model_calls",
    "diagnosis_config"}``; the round function folds those into its result
    mapping so ``model_calls`` feeds the worker's budget check.

    ``evolution_diagnosis_timeout_seconds`` is wired into the diagnoser's own
    isolated client (a background call must never trip the online generator's
    circuit breaker); ``evolution_diagnosis_max_tokens`` stays a recorded
    configuration value because the model side owns the cap and the call
    contract lives in ``rag/evolution/diagnoser.py``.

    ``diagnoser_factory`` / ``tracker_factory`` / ``retrieval_factory`` are the
    injection seams; the defaults build the real ``Diagnoser``, an
    ``ExecutionTracker`` over ``memory_db_path`` and the admin-only read-only
    retrieval reader.
    """
    from rag.evolution.diagnoser import (
        REASON_INSUFFICIENT_SAMPLES,
        REASON_INVALID,
        draft_by_majority,
    )
    from rag.evolution.evidence import assemble_evidence

    cfg = settings_obj or settings
    budget = int(model_call_budget)
    timeout_seconds = float(cfg.evolution_diagnosis_timeout_seconds)
    max_tokens = int(cfg.evolution_diagnosis_max_tokens)
    labels_path = cfg.evolution_diagnosis_labels_path or None
    #: 每个观测采几次样取多数（默认 1 = 与过去完全一致，不增加成本）。
    samples = max(1, int(getattr(cfg, "evolution_diagnosis_samples", 1)))

    def resolve_diagnoser_factory():
        if diagnoser_factory is not None:
            return diagnoser_factory
        from rag.evolution.diagnoser import Diagnoser

        return lambda: Diagnoser(
            # 单次模型调用不得超过本轮剩余时间（C-07）：否则一个慢 provider 会把轮次预算变成
            # "事后记账"，下一轮开始时上一轮还在等模型。
            timeout_seconds=round_model_timeout(cfg.evolution_diagnosis_timeout_seconds),
            max_tokens=cfg.evolution_diagnosis_max_tokens,
        )

    def resolve_tracker_factory():
        if tracker_factory is not None:
            return tracker_factory
        from rag.tracker import ExecutionTracker

        return lambda: ExecutionTracker(cfg.memory_db_path)

    def resolve_retrieval_factory():
        if retrieval_factory is not None:
            return retrieval_factory
        return lambda: AdminReadOnlyRetrievalReader(cfg.users_db_path)

    def step() -> dict:
        diagnose = resolve_diagnoser_factory()
        track = resolve_tracker_factory()
        retrieve_reader = resolve_retrieval_factory()
        config = {
            "model_call_budget": budget,
            "timeout_seconds": timeout_seconds,
            "samples": samples,
            "max_tokens": max_tokens,
            "labels_path": labels_path or "",
            "prompt_version": "",
            "model": "",
        }
        outcome = {"diagnosed": 0, "diagnosis_failed": 0, "model_calls": 0, "diagnosis_retries": 0,
                   "diagnosis_unstable": 0, "diagnosis_config": config}
        if budget <= 0:
            return outcome  # zero budget: no diagnosis work, not even a store scan
        pending = store.observations_awaiting_diagnosis_without_draft(limit=budget)
        if not pending:
            return outcome
        try:
            tracker = track()
            reader = retrieve_reader()
            diagnoser = diagnose()
        except Exception:
            # A broken environment (unusable users.db, unavailable retriever) must
            # not fail the round: clustering and retention already succeeded, and
            # the exception is logged for the administrator to act on.
            logger.exception("evolution_diagnosis_resources_unavailable")
            return outcome
        try:
            config["prompt_version"] = str(diagnoser.prompt_version)
            config["model"] = str(diagnoser.model_name)
            marked = _marked_not_a_defect(cfg)
            for item in _keep_defects(pending, marked):
                if round_expired():
                    # 本轮时间/取消信号到了：剩下的观测留给下一轮，已花的调用照实记账。
                    break
                if outcome["model_calls"] >= budget:
                    break  # budget reached: the remaining observations wait for the next round
                observation = item["observation"]
                cluster = item["cluster"]
                try:
                    pack = assemble_evidence(
                        observation,
                        cluster,
                        store=store,
                        tracker=tracker,
                        labels_path=labels_path,
                        retrieve=reader.retrieve,
                    )
                except Exception:
                    # 证据装配失败（store/reader 异常）不影响本轮其它观测。
                    outcome["diagnosis_failed"] += 1
                    logger.exception(
                        "evolution_diagnosis_evidence_failed cluster=%s observation=%s",
                        cluster.get("id"),
                        observation.get("id"),
                    )
                    continue
                attempts = 0
                while attempts < _MAX_DIAGNOSIS_ATTEMPTS and outcome["model_calls"] < budget:
                    if round_expired():
                        # 采样本身是模型调用：本轮没时间就不再发起新的调用（已发出的照实计入）。
                        break
                    attempts += 1
                    # 采样次数受本轮剩余预算限制：宁可少采一次（票型会记下来），也不越过上限。
                    allowance = min(samples, budget - outcome["model_calls"])
                    try:
                        # The diagnoser keeps receiving the cluster row for its identity
                        # signals (design section 6); the evidence pack is the single
                        # observation's. A draft is cluster-scoped for the admin decision
                        # but observation-scoped through observation_id.
                        sampled = draft_by_majority(
                            diagnoser,
                            cluster,
                            pack,
                            samples=allowance,
                            # 单票不得定稿：采到两次以上时要求至少两个成功样本。
                            min_successes=min(2, allowance),
                        )
                        outcome["model_calls"] += max(1, sampled.calls)
                        if sampled.draft is not None:
                            store.add_draft(
                                cluster["id"],
                                sampled.draft,
                                observation_id=observation["id"],
                                prompt_version=diagnoser.prompt_version,
                                model=diagnoser.model_name,
                            )
                            if sampled.unstable:
                                # 同一证据包下样本给出了不同类别：多半票仍存下，但这个数字
                                # 告诉运营者这条草案有多可信（族级方差的直接度量）。
                                outcome["diagnosis_unstable"] += 1
                                logger.info(
                                    "evolution_diagnosis_unstable cluster=%s observation=%s votes=%s",
                                    cluster.get("id"),
                                    observation.get("id"),
                                    ",".join(sampled.votes),
                                )
                            outcome["diagnosed"] += 1
                            break
                        error = sampled.error
                        if error is None:  # pragma: no cover - 采样要么有草案要么有错误
                            outcome["diagnosis_failed"] += 1
                            break
                        # 可重试的两种：``invalid`` = 模型没遵守输出契约（约 1/4 的 ranking 族
                        # 调用如此，实测被截断的 JSON）；``insufficient_samples`` = 成功样本太少，
                        # 单票不足以定稿。两者都同轮再采一轮，且都计入每轮调用预算，成本账才不失真。
                        # ``timeout``/``rate_limited`` 是容量信号，重试只会更快耗尽预算，交给下一轮。
                        if (
                            error.reason
                            in (REASON_INVALID, REASON_INSUFFICIENT_SAMPLES)
                            and attempts < _MAX_DIAGNOSIS_ATTEMPTS
                            and outcome["model_calls"] < budget
                        ):
                            outcome["diagnosis_retries"] += 1
                            logger.info(
                                "evolution_diagnosis_retry cluster=%s observation=%s attempt=%s detail=%s",
                                cluster.get("id"),
                                observation.get("id"),
                                attempts + 1,
                                error.detail,
                            )
                            continue
                        # No draft for this observation: it stays queued and the next
                        # round may retry it.
                        outcome["diagnosis_failed"] += 1
                        logger.warning(
                            "evolution_diagnosis_unavailable cluster=%s observation=%s reason=%s detail=%s",
                            cluster.get("id"),
                            observation.get("id"),
                            error.reason,
                            error.detail,
                        )
                        break
                    except Exception:
                        # One broken observation (evidence, store, reader) never ends the
                        # round or kills the thread; the loop moves to the next one.
                        outcome["diagnosis_failed"] += 1
                        logger.exception(
                            "evolution_diagnosis_cluster_failed cluster=%s observation=%s",
                            cluster.get("id"),
                            observation.get("id"),
                        )
                        break
        finally:
            _close_quietly(reader)
            _close_quietly(tracker)
        return outcome

    return step


def _marked_not_a_defect(cfg) -> frozenset[str]:
    """Questions an administrator marked as correct behaviour (never worked on)."""
    from rag.evolution.verdicts import not_a_defect_questions

    return not_a_defect_questions(getattr(cfg, "evolution_diagnosis_labels_path", "") or None)


def _keep_defects(items: list[dict], marked: frozenset[str]) -> list[dict]:
    """Drop the observations whose question carries a not-a-defect verdict, keeping order."""
    from rag.evolution.verdicts import is_not_a_defect

    if not marked:
        return items
    return [
        item
        for item in items
        if not is_not_a_defect(str((item.get("observation") or {}).get("question") or ""), marked)
    ]


#: 候选状态里"仍然未决"的那些：只要一个聚类还有这种状态的候选，就**不再**为它生成新的变体。
#: 实测动机（2026-10-11）：`rag/reranker.py` 上堆了 11 条候选，其中 5 条是同一个"同义对齐"想法的
#: 不同写法；根因是准入此前**按观测**判（该观测尚无候选），而 cluster 3 有 112 个成员 ⇒ 理论上
#: 一个聚类能一口气产出 112 条。人被驳回（rejected）之后重新放开 ⇒ 管理员驳回即"可以再试"。
#: 2026-10-13 修复：stable 已经是上线/合入终态（如 close_out_merged 结案的 #20/#29），不应继续
#: 作为"未决"占着聚类名额，否则该聚类将永远无法再次演化。
#: 2026-10-22（C-08）：同一个集合现在也决定"观测是否还在生成队列里"，定义搬到
#: ``candidate_store.OPEN_CANDIDATE_STATUSES``（那份 SQL 要用它），这里只引用，避免两处漂移。
_OPEN_CANDIDATE_STATUSES: frozenset[str] = frozenset(OPEN_CANDIDATE_STATUSES)


def _clusters_with_open_candidates(candidate_store, artifact_type: str) -> set[int]:
    """Cluster ids that already carry an **open** candidate of this kind.

    This is the pile guard. Per-observation deduplication is not enough: a cluster with a hundred
    members would otherwise yield a hundred variants of the same idea, which is how a review queue
    turns into a heap nobody can choose from.
    """
    return {
        int(row["cluster_id"])
        for row in candidate_store.list_candidates()
        if row.get("cluster_id") is not None
        and row.get("artifact_type") == artifact_type
        and str(row.get("status") or "") in _OPEN_CANDIDATE_STATUSES
    }


def _eligible_patch_items(store, candidate_store) -> list[dict]:
    """``{cluster, observation, draft}`` for clusters that may receive a code patch.

    Narrow on purpose, because generation spends model calls and a patch is only meaningful
    when an administrator has already confirmed the layer:

    * the cluster is ``approved_for_candidate`` **and** carries ``confirmed_layer``;
    * the newest stored draft for the member observation suggests that same layer (the
      generator refuses a mismatch anyway, so asking would be a wasted call);
    * the observation has no code-patch candidate yet, **and neither does its cluster**. A concluded
      attempt (``rejected`` / ``needs_more_evidence``) releases the *cluster-level* gate, but the
      background step deliberately does not retry the same observation: regeneration is the
      administrator's explicit action (``POST /code-candidates/{id}/regenerate``), never an
      automatic retry of something a human just refused (C-08 keeps that split; see
      ``candidate_store.OPEN_CANDIDATE_STATUSES`` for the queue side);

    Content gaps are absent by construction: an undetermined root cause never leaves the
    observation queue (``clusterer`` design section 6), so it can never reach this list.
    """
    from rag.evolution.code_patches import ARTIFACT_TYPE as CODE_PATCH_ARTIFACT_TYPE

    newest_draft: dict[int, dict] = {}
    for draft in store.list_drafts():  # newest first
        observation_id = draft.get("observation_id")
        if observation_id is not None:
            newest_draft.setdefault(int(observation_id), draft)
    have_patch = {
        (int(row["observation_id"]), row["artifact_type"])
        for row in candidate_store.list_candidates()
        if row.get("observation_id") is not None
    }
    open_clusters = _clusters_with_open_candidates(candidate_store, CODE_PATCH_ARTIFACT_TYPE)
    items: list[dict] = []
    for cluster in store.list_clusters():
        layer = str(cluster.get("confirmed_layer") or "")
        if not layer or cluster.get("status") != EvolutionStatus.APPROVED_FOR_CANDIDATE.value:
            continue
        if int(cluster["id"]) in open_clusters:
            # 这个聚类已经有一条未决补丁：再生成只会并排放上同一想法的另一个写法。
            continue
        for observation in store.list_observations(cluster_id=cluster["id"]):
            observation_id = int(observation["id"])
            draft = newest_draft.get(observation_id)
            if draft is None:
                continue
            if str((draft.get("payload") or {}).get("suggested_layer") or "") != layer:
                continue
            if (observation_id, CODE_PATCH_ARTIFACT_TYPE) in have_patch:
                continue
            items.append({"cluster": cluster, "observation": observation, "draft": draft})
    return items


def make_evolution_patch_step(
    store,
    *,
    settings_obj=None,
    model_call_budget: int = 0,
    repo_root: str | Path | None = None,
    generator_factory: Callable[[], object] | None = None,
    candidate_store=None,
) -> Callable[[], dict]:
    """Phase 5's generation step: eligible confirmed clusters become code-patch candidates.

    Generation only. The sandbox (layers 2/3) is deliberately **not** run here: it needs a
    clean main tree and it cannot measure a regression without credentials (option C,
    ``docs/evolution-code-patches.md`` section 4), so it stays an administrator step — this
    step's job is to stop requiring a human to run a script before anything exists to review.

    One bounded model call per candidate, and every call counts against ``model_call_budget``;
    refusals are recorded with their reason and never end the round. Without a budget, or with
    ``evolution_candidate_enabled`` off, the step reports zero work.
    """

    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.diagnoser import DiagnosisUnavailable
    from rag.evolution.patch_generator import PatchGenerator, PatchUnavailable

    cfg = settings_obj or settings
    budget = max(0, int(model_call_budget))
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    enabled = bool(getattr(cfg, "evolution_candidate_enabled", False))

    def step() -> dict:
        outcome = {
            "patches_created": 0,
            "patch_failed": 0,
            "patch_pending": 0,
            "patch_model_calls": 0,
        }
        if not enabled or budget <= 0:
            return outcome
        candidates = candidate_store or CandidateStore(store)
        generator = (
            generator_factory
            or (
                lambda: PatchGenerator(
                    # 单次生成不得超过本轮剩余时间（C-07）。
                    timeout_seconds=round_model_timeout(
                        float(getattr(cfg, "evolution_patch_timeout_seconds", 600.0))
                    ),
                    patch_format=str(getattr(cfg, "evolution_patch_format", "diff") or "diff"),
                )
            )
        )()
        items = _keep_defects(_eligible_patch_items(store, candidates),
                              _marked_not_a_defect(cfg))
        # 轮初快照：这个聚类已经有未决补丁就不再生成；轮内再用它做增量挡（见下）。
        from rag.evolution.code_patches import ARTIFACT_TYPE as CODE_PATCH_ARTIFACT_TYPE

        open_clusters = _clusters_with_open_candidates(candidates, CODE_PATCH_ARTIFACT_TYPE)
        for item in items:
            if round_expired():
                # 时间/取消信号到了：剩下的条目等到下一轮，已经生成的候选保留。
                break
            # 轮内也要挡：轮初快照挡不住同一轮里同聚类的其他成员。
            if int(item["cluster"]["id"]) in open_clusters:
                continue
            if outcome["patch_model_calls"] >= budget:
                outcome["patch_pending"] += 1
                continue
            outcome["patch_model_calls"] += 1
            observation_id = int(item["observation"]["id"])
            cluster_id = int(item["cluster"]["id"])
            try:
                candidate = generator.generate(
                    item["cluster"],
                    [item["draft"].get("payload") or {}],
                    layer=str(item["cluster"]["confirmed_layer"]),
                    repo_root=root,
                    observation_id=observation_id,
                    cluster_id=cluster_id,
                    trace_store=store,
                )
                candidates.add_candidate(
                    observation_id,
                    cluster_id,
                    candidate,
                    prompt_version=str(getattr(generator, "prompt_version", "")),
                    model=str(getattr(generator, "model_name", "")),
                )
                outcome["patches_created"] += 1
                # 轮内也要挡：否则同一轮里同一聚类的多个成员会各产一条（本轮算出的集合是轮初快照）。
                open_clusters.add(cluster_id)
                logger.info(
                    "evolution_patch_created cluster=%s observation=%s layer=%s",
                    cluster_id,
                    observation_id,
                    item["cluster"]["confirmed_layer"],
                )
            except (PatchUnavailable, DiagnosisUnavailable) as error:
                # 契约/范围拒绝：记原因，不烧整轮；下一次轮次会重新评估（观测仍无候选）。
                outcome["patch_failed"] += 1
                logger.warning(
                    "evolution_patch_unavailable cluster=%s observation=%s reason=%s detail=%s",
                    cluster_id,
                    observation_id,
                    getattr(error, "reason", ""),
                    str(error)[:200],
                )
            except Exception:
                outcome["patch_failed"] += 1
                logger.exception(
                    "evolution_patch_failed cluster=%s observation=%s", cluster_id, observation_id
                )
            finally:
                # 生成器内部至多重试一次：把它实际发生的**额外**调用也计入预算（否则会漏账）。
                outcome["patch_model_calls"] += max(
                    0, int(getattr(generator, "model_calls", 0) or 0) - 1
                )
        return outcome

    return step


def _eligible_question_set_items(store, candidate_store) -> list[dict]:
    """``{cluster, observation, drafts}`` for clusters that may receive a question-set candidate.

    Narrow for the same reasons as the patch step: the cluster must be approved, the newest
    stored draft must suggest a layer the generator can actually address (``prompt`` or
    ``query_rewrite`` — anything else is structurally impossible, so asking would only burn a
    call), and the observation must have no question-set candidate yet.
    """
    from rag.evolution.candidate_generator import ADDRESSABLE_LAYERS
    from rag.evolution.candidates import ARTIFACT_TYPE as QUESTION_SET_ARTIFACT_TYPE

    newest_draft: dict[int, dict] = {}
    for draft in store.list_drafts():  # newest first
        observation_id = draft.get("observation_id")
        if observation_id is not None:
            newest_draft.setdefault(int(observation_id), draft)
    have_candidate = {
        (int(row["observation_id"]), row["artifact_type"])
        for row in candidate_store.list_candidates()
        if row.get("observation_id") is not None
    }
    open_clusters = _clusters_with_open_candidates(candidate_store, QUESTION_SET_ARTIFACT_TYPE)
    items: list[dict] = []
    for cluster in store.list_clusters():
        if cluster.get("status") != EvolutionStatus.APPROVED_FOR_CANDIDATE.value:
            continue
        if int(cluster["id"]) in open_clusters:
            # 同一个聚类的未决候选只留一条，理由与补丁步骤相同。
            continue
        for observation in store.list_observations(cluster_id=cluster["id"]):
            observation_id = int(observation["id"])
            draft = newest_draft.get(observation_id)
            if draft is None:
                continue
            if str((draft.get("payload") or {}).get("suggested_layer") or "") not in ADDRESSABLE_LAYERS:
                continue
            if (observation_id, QUESTION_SET_ARTIFACT_TYPE) in have_candidate:
                continue
            items.append({"cluster": cluster, "observation": observation, "draft": draft})
    return items


def make_evolution_candidate_step(
    store,
    *,
    settings_obj=None,
    model_call_budget: int = 0,
    generator_factory: Callable[[], object] | None = None,
    candidate_store=None,
    retrieval_factory: Callable[[], object] | None = None,
    target: str = "passages",
    chunk_ids_used: int = 12,
) -> Callable[[], dict]:
    """Phase 4's generation step: eligible confirmed clusters become question-set candidates.

    The provenance follows the recipe the integration scripts proved: the passages question set
    is rebuilt from positional chunk ids (``c0``…) for the observation's own question, so the
    candidate is a bounded text variant of the live set rather than a free invention. Nothing
    is applied here either — a candidate only exists to be gated and reviewed.
    """

    from functools import partial

    from rag.evolution.candidate_generator import CandidateGenerator, CandidateUnavailable
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.diagnoser import DiagnosisUnavailable
    from rag.jev.questions import PASSAGE_QUESTION_SET_VERSION, build_passage_questions

    cfg = settings_obj or settings
    budget = max(0, int(model_call_budget))
    enabled = bool(getattr(cfg, "evolution_candidate_enabled", False))

    def default_retrieval_factory():
        return AdminReadOnlyRetrievalReader(cfg.users_db_path)

    def step() -> dict:
        outcome = {
            "candidates_created": 0,
            "candidate_failed": 0,
            "candidate_pending": 0,
            "candidate_model_calls": 0,
        }
        if not enabled or budget <= 0:
            return outcome
        candidates = candidate_store or CandidateStore(store)
        generator = (
            generator_factory
            or (
                lambda: CandidateGenerator(
                    # 单次生成不得超过本轮剩余时间（C-07）。
                    timeout_seconds=round_model_timeout(
                        float(getattr(cfg, "evolution_candidate_timeout_seconds", 120.0))
                    ),
                    max_tokens=int(getattr(cfg, "evolution_candidate_max_tokens", 16000)),
                )
            )
        )()
        reader = (retrieval_factory or default_retrieval_factory)()
        try:
            items = _keep_defects(_eligible_question_set_items(store, candidates),
                              _marked_not_a_defect(cfg))
            # 同上：同一聚类只留一条未决候选，轮内也要挡。
            from rag.evolution.candidates import ARTIFACT_TYPE as QUESTION_SET_ARTIFACT_TYPE

            open_clusters = _clusters_with_open_candidates(
                candidates, QUESTION_SET_ARTIFACT_TYPE
            )
            for item in items:
                if round_expired():
                    # 时间/取消信号到了：剩下的条目等到下一轮。
                    break
                # 同上：同一轮里同一聚类只留一条未决候选。
                if int(item["cluster"]["id"]) in open_clusters:
                    continue
                if outcome["candidate_model_calls"] >= budget:
                    outcome["candidate_pending"] += 1
                    continue
                outcome["candidate_model_calls"] += 1
                observation = item["observation"]
                cluster_id = int(item["cluster"]["id"])
                observation_id = int(observation["id"])
                try:
                    chunks = reader.retrieve(
                        observation["question"], kb_id=observation["kb_id"]
                    ) or []
                    chunk_ids = [f"c{index}" for index, _ in enumerate(chunks[:chunk_ids_used])]
                    candidate = generator.generate(
                        item["cluster"],
                        [item["draft"].get("payload") or {}],
                        target=target,
                        base_builder=partial(build_passage_questions, chunk_ids),
                        base_version=PASSAGE_QUESTION_SET_VERSION,
                        observation_id=observation_id,
                        cluster_id=cluster_id,
                        trace_store=store,
                    )
                    candidates.add_candidate(
                        observation_id,
                        cluster_id,
                        candidate,
                        prompt_version=str(getattr(generator, "prompt_version", "")),
                        model=str(getattr(generator, "model_name", "")),
                    )
                    outcome["candidates_created"] += 1
                    open_clusters.add(cluster_id)
                    logger.info(
                        "evolution_candidate_created cluster=%s observation=%s target=%s",
                        cluster_id,
                        observation_id,
                        target,
                    )
                except (CandidateUnavailable, DiagnosisUnavailable) as error:
                    outcome["candidate_failed"] += 1
                    logger.warning(
                        "evolution_candidate_unavailable cluster=%s observation=%s reason=%s detail=%s",
                        cluster_id,
                        observation_id,
                        getattr(error, "reason", ""),
                        str(error)[:200],
                    )
                except Exception:
                    outcome["candidate_failed"] += 1
                    logger.exception(
                        "evolution_candidate_failed cluster=%s observation=%s",
                        cluster_id,
                        observation_id,
                    )
        finally:
            _close_quietly(reader)
        return outcome

    return step


def make_evolution_gap_ticket_step(
    store,
    *,
    settings_obj=None,
    retrieval_factory: Callable[[], object] | None = None,
    probe_limit: int = 8,
    question_limit: int = 8,
    recheck_limit: int = 5,
) -> Callable[[], dict]:
    """The content-gap artifact: an ``undetermined`` cluster becomes one reviewable ticket.

    **No model calls at all.** Everything an editor needs is already recorded or cheaply
    derivable: which questions failed and how often, over what window, in which knowledge base,
    and — after one deterministic re-retrieval per sampled question — which of their terms the
    recall window never contains. That last part is what says *what to write*.

    The ticket never touches the candidate pool: the cluster stays in the observation queue,
    because "the corpus lacks this" is not something a prompt or a patch can fix. Members already
    judged not-a-defect by an administrator are dropped, so a refusal that was correct never
    becomes a request to add content that must not be handed over.
    """
    from rag.evolution.gaps import (
        aggregate_missing_terms,
        build_ticket,
        missing_terms_for,
        question_samples,
        ticket_from_payload,
    )
    from rag.evolution.verdicts import is_not_a_defect

    cfg = settings_obj or settings
    marked = _marked_not_a_defect(cfg)

    def step() -> dict:
        outcome = {
            "tickets_created": 0,
            "tickets_pending": 0,
            "ticket_questions_probed": 0,
            "tickets_rechecked": 0,
            "tickets_updated": 0,
        }
        have = store.gap_ticket_cluster_ids()
        # 准入以**诊断草稿**为准：内容缺口是诊断的结论（failure_category=undetermined），
        # 而 cluster.root_cause_feature 是**聚类器**写的（recall_miss 之类）。彩排实测：只按后者
        # 判准入会导致工单永远不产出（草稿说 undetermined、聚类字段却是 recall_miss）。旧形态
        # （聚类字段就是 undetermined）仍然接受，两条路都留。
        newest_draft: dict[int, dict] = {}
        for draft in store.list_drafts():  # newest first
            cluster_id = draft.get("cluster_id")
            if cluster_id is not None:
                newest_draft.setdefault(int(cluster_id), draft)

        def is_gap_cluster(cluster) -> bool:
            if str(cluster.get("root_cause_feature") or "") == "undetermined":
                return True
            draft = newest_draft.get(int(cluster["id"]))
            payload = (draft or {}).get("payload") or {}
            return str(payload.get("failure_category") or "") == "undetermined"

        # 惰性：没有可探测的聚类时连 reader 都不建（每轮都跑，绝大多数轮次无事可做）。
        reader = None

        def ensure_reader():
            nonlocal reader
            if reader is None:
                reader = (
                    retrieval_factory
                    or (
                        lambda: AdminReadOnlyRetrievalReader(
                            getattr(cfg, "users_db_path", settings.users_db_path)
                        )
                    )
                )()
            return reader

        def probe(questions, kb_id):
            """Deterministic re-retrieval: the missing terms of each question, and how many ran."""
            probed = 0
            per_question: list[tuple[str, ...]] = []
            for question in questions[:probe_limit]:
                try:
                    chunks = ensure_reader().retrieve(question, kb_id=kb_id) or []
                except Exception:
                    logger.exception("evolution_gap_probe_failed kb=%s", kb_id)
                    continue
                probed += 1
                per_question.append(
                    missing_terms_for(
                        question, [getattr(chunk, "text", "") or "" for chunk in chunks]
                    )
                )
            return per_question, probed

        try:
            for cluster in store.list_clusters():
                if round_expired():
                    # 本轮时间/取消信号到了：工单等到下一轮（它是queued work，不会丢）。
                    break
                if not is_gap_cluster(cluster):
                    continue
                if int(cluster["id"]) in have:
                    continue
                members = [
                    observation
                    for observation in store.list_observations(cluster_id=cluster["id"])
                    if not is_not_a_defect(str(observation.get("question") or ""), marked)
                ]
                if not members:
                    outcome["tickets_pending"] += 1
                    continue
                per_question, probed = probe(
                    question_samples(members, limit=question_limit), cluster.get("kb_id")
                )
                ticket = build_ticket(
                    cluster,
                    members,
                    missing_terms=aggregate_missing_terms(per_question),
                    probed_questions=probed,
                    question_limit=question_limit,
                )
                store.add_gap_ticket(ticket)
                outcome["tickets_created"] += 1
                outcome["ticket_questions_probed"] += probed
                logger.info(
                    "evolution_gap_ticket_created cluster=%s kb=%s occurrences=%s terms=%s",
                    cluster.get("id"),
                    cluster.get("kb_id"),
                    ticket.occurrences,
                    ",".join(ticket.missing_terms[:4]),
                )

            # 第二阶段：内容入库后的**复测**。缺的词被覆盖 ⇒ content_added；仍有缺 ⇒ 更新清单。
            now = time.time()
            for row in store.list_gap_tickets(status="open")[:recheck_limit]:
                if round_expired():
                    break
                payload = row.get("payload") or {}
                questions = [str(item) for item in (payload.get("question_samples") or ())]
                if not questions:
                    continue
                ticket = ticket_from_payload(payload)
                per_question, probed = probe(questions, row.get("kb_id"))
                missing_now = aggregate_missing_terms(per_question)
                outcome["tickets_rechecked"] += 1
                if tuple(missing_now) == tuple(ticket.missing_terms):
                    continue  # 没有变化就不写，避免无谓的行更新
                covered = [term for term in ticket.missing_terms if term not in set(missing_now)]
                updated = ticket.with_recheck(
                    missing_terms=missing_now, covered_terms=covered, probed=probed, now=now
                )
                store.record_gap_recheck(int(row["id"]), updated, now=now)
                outcome["tickets_updated"] += 1
                logger.info(
                    "evolution_gap_ticket_updated ticket=%s verdict=%s status=%s still_missing=%s",
                    row.get("id"),
                    (updated.recheck or {}).get("verdict"),
                    updated.status,
                    ",".join(updated.missing_terms[:4]),
                )
        finally:
            _close_quietly(reader)
        return outcome

    return step


def build_evolution_worker(
    *,
    settings_obj=None,
    store=None,
    clusterer=None,
    busy_provider: Callable[[], bool] | None = None,
    clock: Callable[[], float] | None = None,
    rss_provider: Callable[[], float | None] | None = None,
    diagnoser_factory=None,
    tracker_factory=None,
    retrieval_factory=None,
) -> EvolutionWorker | None:
    """Assemble the worker from settings; returns None (no store) when disabled.

    The failed-trace store and the clusterer are attached to the returned worker
    as ``worker.store`` / ``worker.clusterer`` so the lifecycle owner can close
    the store on shutdown. ``diagnoser_factory`` / ``tracker_factory`` /
    ``retrieval_factory`` are the diagnosis step's injection seams (the defaults
    build the real diagnoser, tracker and admin-only read-only retrieval).
    """
    cfg = settings_obj or settings
    if not cfg.evolution_enabled:
        return None
    # Phase 3: the round's model calls are the diagnosis calls, bounded by the
    # diagnosis setting (the phase-2 hardcoded zero is gone).
    max_model_calls = cfg.evolution_diagnosis_model_calls
    budget = EvolutionBudget(
        max_concurrency=1,
        max_batch=cfg.evolution_worker_max_batch,
        max_run_seconds=cfg.evolution_worker_run_seconds,
        max_model_calls=max_model_calls,
    )
    if store is None:
        from rag.evolution.store import FailureTraceStore

        store = FailureTraceStore(cfg.evolution_db_path)
    if clusterer is None:
        from rag.evolution.clusterer import FailureClusterer

        clusterer = FailureClusterer(
            store,
            threshold=cfg.evolution_cluster_threshold,
            window_days=cfg.evolution_cluster_window_days,
        )
    run_once = make_evolution_run_once(
        clusterer,
        store,
        retention_days=cfg.evolution_observation_retention_days,
        batch_limit=budget.max_batch,
        diagnosis_step=make_evolution_diagnosis_step(
            store,
            settings_obj=cfg,
            model_call_budget=max_model_calls,
            diagnoser_factory=diagnoser_factory,
            tracker_factory=tracker_factory,
            retrieval_factory=retrieval_factory,
        ),
        # 第 5 阶段：把"已批准 + 已确认层级"的聚类自动变成代码补丁候选。沙箱与回归 A/B
        # 仍归管理员（沙箱需要干净主树，且无凭据时量不了回归——方案 C）。
        patch_step=make_evolution_patch_step(
            store,
            settings_obj=cfg,
            model_call_budget=int(getattr(cfg, "evolution_candidate_model_calls", 0)),
            repo_root=Path(__file__).resolve().parents[2],
        )
        if getattr(cfg, "evolution_candidate_enabled", False)
        else None,
        # 第 4 阶段：把可寻址层（prompt / query_rewrite）的已批准聚类自动变成问题集候选。
        candidate_step=make_evolution_candidate_step(
            store,
            settings_obj=cfg,
            model_call_budget=int(getattr(cfg, "evolution_candidate_model_calls", 0)),
            retrieval_factory=retrieval_factory,
        )
        if getattr(cfg, "evolution_candidate_enabled", False)
        else None,
        # 第三种制品：内容缺口工单。**不需要候选开关**（它不是候选），也不花模型调用。
        gap_ticket_step=make_evolution_gap_ticket_step(store, settings_obj=cfg),
    )
    worker = EvolutionWorker(
        run_once,
        interval_seconds=cfg.evolution_worker_interval_seconds,
        budget=budget,
        busy_provider=busy_provider,
        clock=clock,
        rss_provider=rss_provider,
        # 跨进程租约（C-07）：API 线程里的 worker 与 `python -m rag.evolution.worker` 共用同一个
        # evolution.db，同一时刻只允许一轮在里面测量/写入。锁文件放在库旁边，进程之间才看得见。
        lock_path=Path(cfg.evolution_db_path).parent / HOST_LEASE_FILENAME,
    )
    worker.store = store
    worker.clusterer = clusterer
    return worker


def start_evolution_worker() -> EvolutionWorker | None:
    """Module-level singleton: start the worker only when evolution is enabled."""
    global _evolution_worker
    if not settings.evolution_enabled or not settings.evolution_worker_enabled:
        return None
    with _evolution_worker_lock:
        if _evolution_worker is not None and _evolution_worker.is_alive:
            return _evolution_worker
        built = build_evolution_worker()
        if built is None:
            return None
        built.start()
        _evolution_worker = built
        _log_worker_started()
        return _evolution_worker


def stop_evolution_worker() -> None:
    global _evolution_worker
    with _evolution_worker_lock:
        worker = _evolution_worker
        _evolution_worker = None
    if worker is not None:
        worker.stop()
        store = getattr(worker, "store", None)
        close = getattr(store, "close", None)
        if callable(close):
            close()


def evolution_worker_is_alive() -> bool:
    worker = _evolution_worker
    return worker is not None and worker.is_alive


_evolution_worker: EvolutionWorker | None = None
_evolution_worker_lock = threading.Lock()


def main() -> int:
    """Console entry for ``python -m rag.evolution.worker``.

    With ``RAG_EVOLUTION_ENABLED`` unset (the default) this is a no-op that
    touches no database file; otherwise it runs the bounded worker in-process
    until interrupted.
    """
    if not logging.getLogger().handlers:
        # Standalone runs need a sink; inside the API the app already configured logging.
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not settings.evolution_enabled:
        logger.info("evolution worker disabled (RAG_EVOLUTION_ENABLED is not true); nothing to do")
        return 0
    if not settings.evolution_worker_enabled:
        logger.info("evolution worker disabled (RAG_EVOLUTION_WORKER_ENABLED is not true); nothing to do")
        return 0
    worker = build_evolution_worker()
    if worker is None:
        return 0
    worker.start()
    _log_worker_started()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logger.info("evolution worker stopping")
    finally:
        worker.stop()
        close = getattr(worker.store, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
