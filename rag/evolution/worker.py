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
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)

_PAUSE_POLL_SECONDS = 0.1

#: Re-retrieval width for one diagnosis reproduction check. The evidence module
#: caps its own recall the same way; the worker only owns the request.
_DIAGNOSIS_RECALL_TOP_K = 24

#: 每个观测在一轮内最多尝试几次模型调用。第二次只用于 ``invalid``（模型没遵守输出契约）：
#: 同一证据包换一次采样往往就合规（实测 ranking 族的截断属此类），而超时/限流是容量信号，
#: 重试只会更快耗尽预算。重试计入每轮调用预算，所以这是成本上限而不是绕过它。
_MAX_DIAGNOSIS_ATTEMPTS = 2

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
        self._last_diagnosis_config: dict | None = None

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
        self._record_diagnosis_config(result)
        # One line per round whatever the outcome: the worker runs in a background
        # thread of the API process, and without it "is it running, and is it doing
        # anything" is unanswerable from outside — the only other records on this
        # path are failures and budget warnings.
        counters = _round_counts(result)
        if deadline is not None and self._clock() >= deadline:
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
) -> Callable[[], dict]:
    """Build the round function: one bounded clustering batch, then diagnosis, then retention.

    Clustering runs first so the purge can never shrink a cluster mid-round;
    the next round recomputes member counts from the surviving rows anyway.
    Diagnosis (phase 3) runs second — while every member observation is still
    present — and is bounded by its own per-round model-call budget. Without a
    ``diagnosis_step`` the round reports zero diagnosis work, exactly like the
    phase-2 rounds did.
    """
    rounds = {"n": 0}

    def run_once() -> dict:
        rounds["n"] += 1
        outcome = clusterer.cluster_once(limit=batch_limit)
        diagnosis = diagnosis_step() if diagnosis_step is not None else dict(_NO_DIAGNOSIS)
        purged = 0
        if purge_every_rounds > 0 and rounds["n"] % purge_every_rounds == 0:
            purged = purge_evolution_observations(store, retention_days=retention_days)
        return {
            "cluster_once": outcome,
            "diagnosed": diagnosis["diagnosed"],
            "diagnosis_failed": diagnosis["diagnosis_failed"],
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
            timeout_seconds=cfg.evolution_diagnosis_timeout_seconds,
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
            for item in pending:
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
