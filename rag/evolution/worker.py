"""Budgeted, busy-aware background worker for the observation phase.

The worker runs one clustering round at a time on a daemon thread, reusing the
``rag/backups/scheduler.py`` stop/pause pattern. Three budget dimensions bound
every round: wall-clock run seconds, batch size and model calls (phase 2 makes
zero model calls, so any reported call ends the round as budget-exhausted). A
busy system pauses rounds without ever dropping queued observations, and one
failing batch can never kill the thread: the round is logged and the loop keeps
running. Nothing here can change an online response, a candidate or a gate.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

_PAUSE_POLL_SECONDS = 0.1


class BudgetExhausted(Exception):
    """Raised (or reported) when a round exceeds one of the worker budgets."""

    def __init__(self, reason: str = "budget_exhausted"):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EvolutionBudget:
    """Per-round budgets; ``max_concurrency`` is structurally 1 in phase 2."""

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
    """Daemon-thread worker with per-round budgets, busy pause and error isolation."""

    def __init__(
        self,
        run_once: Callable[[], object],
        *,
        interval_seconds: float,
        budget: EvolutionBudget | None = None,
        busy_provider: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
        rss_provider: Callable[[], float | None] | None = None,
        name: str = "rag-evolution-worker",
    ):
        self._run_once = run_once
        self._interval_seconds = interval_seconds
        self._budget = budget or EvolutionBudget()
        self._busy_provider = busy_provider or default_busy_provider
        self._clock = clock or time.monotonic
        self._rss_provider = rss_provider
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
        self._budget_exhausted = 0
        self._last_budget_reason: str | None = None
        self._last_status = "idle"

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def budget(self) -> EvolutionBudget:
        return self._budget

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
                "last_budget_reason": self._last_budget_reason,
                "alive": self.is_alive,
            }

    # ── internals ──────────────────────────────────────────────────────────

    def _execute_round(self) -> str:
        """Run one round under the concurrency budget; a round in flight means busy."""
        if not self._round_lock.acquire(blocking=False):
            self._record("busy")
            return "busy"
        try:
            status = self._run_round()
        finally:
            self._round_lock.release()
        self._record(status)
        return status

    def _run_round(self) -> str:
        if self._is_busy():
            return "busy"
        budget = self._budget
        started = self._clock()
        deadline = started + budget.max_run_seconds if budget.max_run_seconds is not None else None
        if deadline is not None and self._clock() >= deadline:
            # A non-positive run-seconds budget never starts a round at all.
            return self._exhaust("max_run_seconds")
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
            with self._state_lock:
                self._runs += 1
        if deadline is not None and self._clock() >= deadline:
            return self._exhaust("max_run_seconds")
        if self._over_model_budget(result):
            return self._exhaust("max_model_calls")
        if self._over_rss_budget():
            return self._exhaust("max_rss_mb")
        return "ok"

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

    Phase 2's clusterer is deterministic and always reports ``0`` above the
    zero ceiling, so any real model call ends the round as budget-exhausted.
    """
    if isinstance(result, Mapping) and "model_calls" in result:
        try:
            return int(result["model_calls"])
        except (TypeError, ValueError):
            return None
    return None


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
) -> Callable[[], dict]:
    """Build the round function: one bounded clustering batch, then retention.

    Clustering runs first so the purge can never shrink a cluster mid-round;
    the next round recomputes member counts from the surviving rows anyway.
    """
    rounds = {"n": 0}

    def run_once() -> dict:
        rounds["n"] += 1
        outcome = clusterer.cluster_once(limit=batch_limit)
        purged = 0
        if purge_every_rounds > 0 and rounds["n"] % purge_every_rounds == 0:
            purged = purge_evolution_observations(store, retention_days=retention_days)
        return {"cluster_once": outcome, "purged": purged, "model_calls": 0}

    return run_once


def build_evolution_worker(
    *,
    settings_obj=None,
    store=None,
    clusterer=None,
    busy_provider: Callable[[], bool] | None = None,
    clock: Callable[[], float] | None = None,
    rss_provider: Callable[[], float | None] | None = None,
) -> EvolutionWorker | None:
    """Assemble the worker from settings; returns None (no store) when disabled.

    The failed-trace store and the clusterer are attached to the returned worker
    as ``worker.store`` / ``worker.clusterer`` so the lifecycle owner can close
    the store on shutdown.
    """
    cfg = settings_obj or settings
    if not cfg.evolution_enabled:
        return None
    budget = EvolutionBudget(
        max_concurrency=1,
        max_batch=cfg.evolution_worker_max_batch,
        max_run_seconds=cfg.evolution_worker_run_seconds,
        max_model_calls=0,  # phase 2 makes no model calls
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
    )
    worker = EvolutionWorker(
        run_once,
        interval_seconds=cfg.evolution_worker_interval_seconds,
        budget=budget,
        busy_provider=busy_provider,
        clock=clock,
        rss_provider=rss_provider,
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
    logger.info(
        "evolution worker started interval=%ss run_seconds=%s batch=%s",
        settings.evolution_worker_interval_seconds,
        settings.evolution_worker_run_seconds,
        settings.evolution_worker_max_batch,
    )
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
