"""Task 3: budgets, busy pause and exception isolation for the evolution worker."""

import json
import logging
import os
import tempfile
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

import rag.evolution.worker as worker_module
from config import settings
from rag.evolution.diagnoser import DIAGNOSIS_PROMPT_VERSION, Diagnoser
from rag.evolution.models import DiagnosisDraft, FailureObservation
from rag.evolution.store import FailureTraceStore
from rag.evolution.worker import (
    AdminReadOnlyRetrievalReader,
    BudgetExhausted,
    EvolutionBudget,
    EvolutionWorker,
    build_evolution_worker,
    default_busy_provider,
    main,
    make_evolution_diagnosis_step,
    make_evolution_run_once,
    purge_evolution_observations,
)

# ── Task 3 Step 1: the plan's three cases, verbatim ──────────────────────────


def test_busy_system_pauses_and_resumes_without_losing_runs():
    busy = threading.Event()
    calls = []
    worker = EvolutionWorker(lambda: calls.append(1), interval_seconds=0.01,
                             busy_provider=busy.is_set)
    busy.set()
    worker.start()
    worker.run_once_when_idle()  # no-op while busy
    assert calls == []
    busy.clear()
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert calls  # resumes after busy clears


def test_budget_exhaustion_stops_the_run_and_is_reported():
    runs = []
    def one_run():
        runs.append(1)
        raise BudgetExhausted("max_run_seconds")

    worker = EvolutionWorker(one_run, interval_seconds=0.01, budget=EvolutionBudget(max_run_seconds=0.0))
    worker.start()
    deadline = time.monotonic() + 2
    while worker.stats()["budget_exhausted"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert worker.stats()["budget_exhausted"] >= 1
    assert runs == []


def test_one_failing_batch_never_kills_the_thread():
    state = {"n": 0}
    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")

    worker = EvolutionWorker(flaky, interval_seconds=0.01)
    worker.start()
    deadline = time.monotonic() + 2
    while state["n"] < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert worker.is_alive is False and state["n"] >= 3


# ── observability: what a round did must be visible from outside ─────────────


def test_a_completed_round_logs_what_it_did(caplog):
    """轮次计数要能被打出来：worker 空闲轮此前一行日志都没有。

    The API path logs neither the worker's start nor an idle round, so "is the
    evolution worker actually running" could not be answered from the log at all —
    only failures and budget exhaustion were visible.
    """
    run_once = lambda: {  # noqa: E731 - one inline round shape
        "cluster_once": {"scanned": 3, "clustered": 1, "promoted": 1, "skipped": 0},
        "diagnosed": 2,
        "diagnosis_failed": 1,
        "model_calls": 3,
        "purged": 0,
        "diagnosis_config": {},
    }
    worker = EvolutionWorker(
        run_once,
        interval_seconds=0.01,
        busy_provider=lambda: False,
        budget=EvolutionBudget(max_model_calls=3),
    )

    with caplog.at_level(logging.INFO):
        assert worker.run_once_when_idle() == "ok"

    assert "evolution_worker_round" in caplog.text
    assert "status=ok" in caplog.text
    assert "scanned=3" in caplog.text
    assert "clustered=1" in caplog.text
    assert "diagnosed=2" in caplog.text
    assert "model_calls=3" in caplog.text


def test_a_round_of_an_unknown_shape_still_logs(caplog):
    """结果形状不认识也不能把日志打断——它只是没有计数可打。"""
    worker = EvolutionWorker(lambda: None, interval_seconds=0.01, busy_provider=lambda: False)

    with caplog.at_level(logging.INFO):
        assert worker.run_once_when_idle() == "ok"

    assert "counters=unavailable" in caplog.text


def test_a_budget_exhausted_round_logs_its_status_and_counters(caplog):
    """预算耗尽也要留下"这一轮做了什么"，不能只有一行 warning。"""
    worker = EvolutionWorker(
        lambda: {"model_calls": 9},
        interval_seconds=0.01,
        busy_provider=lambda: False,
        budget=EvolutionBudget(max_model_calls=3),
    )

    with caplog.at_level(logging.INFO):
        assert worker.run_once_when_idle() == "budget_exhausted"

    assert "status=budget_exhausted" in caplog.text
    assert "model_calls=9" in caplog.text


def test_starting_the_worker_logs_that_it_started(monkeypatch, caplog):
    """API 启动路径此前不打任何日志：起没起来无法从外部确认。"""

    class FakeWorker:
        is_alive = True

        def start(self) -> None:
            pass

    monkeypatch.setattr(worker_module, "_evolution_worker", None)
    monkeypatch.setattr(worker_module, "build_evolution_worker", lambda: FakeWorker())
    monkeypatch.setattr(settings, "evolution_enabled", True)
    monkeypatch.setattr(settings, "evolution_worker_enabled", True)

    with caplog.at_level(logging.INFO):
        started = worker_module.start_evolution_worker()

    assert started is not None
    assert "evolution worker started" in caplog.text
    assert "model_calls_per_round=" in caplog.text


def test_a_disabled_evolution_switch_logs_nothing_and_starts_nothing(monkeypatch, caplog):
    """关掉时不许起线程，也不该打出"started"。"""
    monkeypatch.setattr(worker_module, "_evolution_worker", None)
    monkeypatch.setattr(settings, "evolution_enabled", False)

    with caplog.at_level(logging.INFO):
        assert worker_module.start_evolution_worker() is None

    assert "evolution worker started" not in caplog.text


# ── budget semantics ──────────────────────────────────────────────────────────


def test_run_once_when_idle_reports_ok_failed_and_busy():
    ok = EvolutionWorker(lambda: None, interval_seconds=0.01, busy_provider=lambda: False)
    assert ok.run_once_when_idle() == "ok"

    def boom():
        raise RuntimeError("boom")

    failed = EvolutionWorker(boom, interval_seconds=0.01, busy_provider=lambda: False)
    assert failed.run_once_when_idle() == "failed"
    assert failed.stats()["failed"] == 1

    idle = EvolutionWorker(lambda: None, interval_seconds=0.01, busy_provider=lambda: True)
    assert idle.run_once_when_idle() == "busy"


def test_budget_exhausted_raised_by_a_run_records_its_reason():
    def run():
        raise BudgetExhausted("max_model_calls")

    worker = EvolutionWorker(run, interval_seconds=0.01, busy_provider=lambda: False)
    assert worker.run_once_when_idle() == "budget_exhausted"
    stats = worker.stats()
    assert stats["budget_exhausted"] == 1
    assert stats["last_budget_reason"] == "max_model_calls"
    assert stats["runs"] == 1


def test_model_calls_above_the_budget_end_the_round():
    """Phase 2 forbids model calls: any reported call ends the round as budget_exhausted."""
    worker = EvolutionWorker(lambda: {"model_calls": 1}, interval_seconds=0.01, busy_provider=lambda: False)
    assert worker.run_once_when_idle() == "budget_exhausted"
    assert worker.stats()["runs"] == 1


def test_rss_ceiling_ends_the_round():
    budget = EvolutionBudget(max_rss_mb=10.0)
    worker = EvolutionWorker(lambda: None, interval_seconds=0.01, budget=budget,
                             busy_provider=lambda: False, rss_provider=lambda: 100.0)
    assert worker.run_once_when_idle() == "budget_exhausted"


def test_budget_rejects_invalid_bounds():
    with pytest.raises(ValueError):
        EvolutionBudget(max_concurrency=0)
    with pytest.raises(ValueError):
        EvolutionBudget(max_batch=0)
    with pytest.raises(ValueError):
        EvolutionBudget(max_run_seconds=-1.0)
    with pytest.raises(ValueError):
        EvolutionBudget(max_model_calls=-1)
    with pytest.raises(ValueError):
        EvolutionBudget(max_rss_mb=0.0)


def test_stats_expose_the_documented_keys():
    worker = EvolutionWorker(lambda: None, interval_seconds=0.01, busy_provider=lambda: False)
    stats = worker.stats()
    assert set(stats) >= {"runs", "last_status", "paused", "busy", "budget_exhausted"}
    assert stats["last_status"] == "idle"


def test_pause_blocks_rounds_until_resume():
    calls = []
    worker = EvolutionWorker(lambda: calls.append(1), interval_seconds=0.01, busy_provider=lambda: False)
    worker.pause()
    worker.start()
    worker.start()  # idempotent
    time.sleep(0.1)
    assert calls == []
    assert worker.stats()["paused"] is True
    worker.resume()
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()
    assert calls
    assert worker.is_alive is False
    assert worker.stats()["paused"] is False


def test_a_round_in_flight_makes_run_once_when_idle_report_busy():
    started = threading.Event()
    release = threading.Event()

    def blocking():
        started.set()
        release.wait(5.0)

    worker = EvolutionWorker(blocking, interval_seconds=0.01, busy_provider=lambda: False)
    worker.start()
    assert started.wait(2)
    assert worker.run_once_when_idle() == "busy"  # max_concurrency=1 is enforced
    release.set()
    worker.stop()


# ── busy provider ─────────────────────────────────────────────────────────────


def test_default_busy_provider_compares_load_with_the_configured_factor(monkeypatch):
    monkeypatch.setattr(worker_module.settings, "evolution_worker_busy_load_factor", 2.0)
    cpus = os.cpu_count() or 1
    # Windows has no os.getloadavg; monkeypatch can still define it for this test.
    monkeypatch.setattr(os, "getloadavg", lambda: (2.0 * cpus + 1.0, 0.0, 0.0), raising=False)
    assert default_busy_provider() is True
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    assert default_busy_provider() is False


def test_default_busy_provider_is_false_without_loadavg(monkeypatch):
    monkeypatch.delattr(os, "getloadavg", raising=False)
    assert default_busy_provider() is False


# ── retention purge ───────────────────────────────────────────────────────────


class FakeStore:
    def __init__(self):
        self.purges = []
        self.awaiting: list[dict] = []
        self.drafts: list[tuple] = []

    def purge_observations_before(self, timestamp):
        self.purges.append(timestamp)
        return 0

    def clusters_awaiting_diagnosis_without_draft(self, limit):
        return list(self.awaiting[:limit])

    def observations_awaiting_diagnosis_without_draft(self, limit):
        return []

    def add_draft(self, cluster_id, draft, *, observation_id=None, prompt_version="", model=""):
        self.drafts.append((cluster_id, draft, prompt_version, model))
        return len(self.drafts)

    # 内容缺口工单步骤（队列 5）用的两个方法：替身要模拟真实 store 的 surface。
    def list_clusters(self):
        return []

    def gap_ticket_cluster_ids(self):
        return set()

    def list_gap_tickets(self, cluster_id=None, *, status=None):
        return []

    def list_drafts(self, cluster_id=None):
        return []


class FakeClusterer:
    def __init__(self):
        self.calls = []

    def cluster_once(self, *, limit=200):
        self.calls.append(limit)
        return {"scanned": 0, "clustered": 0, "promoted": 0, "skipped": 0}


def test_purge_evolution_observations_uses_the_configured_window():
    store = FakeStore()
    purge_evolution_observations(store, retention_days=90, now=1_000_000.0)
    assert store.purges == [1_000_000.0 - 90 * 86400]


def test_retention_purges_old_observations_each_round():
    store = FakeStore()
    run_once = make_evolution_run_once(FakeClusterer(), store, retention_days=90)
    worker = EvolutionWorker(run_once, interval_seconds=0.01, busy_provider=lambda: False)
    assert worker.run_once_when_idle() == "ok"
    assert len(store.purges) == 1
    assert abs(store.purges[0] - (time.time() - 90 * 86400)) < 1.0


def test_evolution_run_once_reports_zero_model_calls():
    """The phase-2 clusterer is deterministic; the run admits no model call."""
    store = FakeStore()
    run_once = make_evolution_run_once(FakeClusterer(), store, retention_days=90)
    assert run_once()["model_calls"] == 0


def test_purge_every_n_rounds():
    store = FakeStore()
    run_once = make_evolution_run_once(FakeClusterer(), store, retention_days=90, purge_every_rounds=3)
    for _ in range(5):
        run_once()
    assert len(store.purges) == 1  # only round 3 purged


def test_purge_can_be_disabled_with_purge_every_rounds_zero():
    store = FakeStore()
    run_once = make_evolution_run_once(FakeClusterer(), store, retention_days=90, purge_every_rounds=0)
    run_once()
    assert store.purges == []


# ── C-07 (2026-10-22): the round's deadline and cancellation reach the steps ──


def test_the_worker_publishes_its_round_clock_to_the_steps():
    """Every step can read the round's deadline and remaining time while it runs (C-07).

    Before this, the deadline was only applied *after* the work: a step kept scheduling model calls
    until it was done, and the budget was an accounting fact rather than a bound.
    """
    from rag.evolution.worker import current_round, round_expired, round_model_timeout

    captured: dict = {}

    def run_once():
        captured["clock"] = current_round()
        captured["expired"] = round_expired()
        captured["timeout"] = round_model_timeout(600.0)
        return {}

    worker = EvolutionWorker(
        run_once,
        interval_seconds=0.01,
        busy_provider=lambda: False,
        budget=EvolutionBudget(max_run_seconds=120.0),
    )

    assert worker.run_once_when_idle() == "ok"

    clock = captured["clock"]
    assert clock is not None and clock.deadline is not None
    assert captured["expired"] is False
    assert 0 < captured["timeout"] <= 600.0
    # 轮次之外没有预算可执行：一个直接调用的 step 保持旧行为（不因"上一轮过期"而停）。
    assert current_round() is None
    assert round_expired() is False


def test_a_model_call_timeout_is_capped_by_the_time_the_round_has_left():
    """A slow provider eats the round's budget instead of overrunning it."""
    from rag.evolution.worker import (
        _MIN_MODEL_TIMEOUT_S,
        RoundClock,
        round_model_timeout,
    )

    now = {"t": 100.0}
    clock = RoundClock(
        started=100.0, deadline=112.5, cancelled=threading.Event(), clock=lambda: now["t"]
    )
    token = worker_module._current_round.set(clock)
    try:
        assert round_model_timeout(600.0) == 12.5
        now["t"] = 111.9
        # 余量小于地板：给的是地板值（1 秒），不是一个几乎立刻超时的 0.6 秒。
        assert round_model_timeout(600.0) == _MIN_MODEL_TIMEOUT_S
        now["t"] = 200.0
        assert round_model_timeout(600.0) == _MIN_MODEL_TIMEOUT_S
    finally:
        worker_module._current_round.reset(token)


def test_a_stuck_step_is_interrupted_at_the_round_deadline():
    """A step that loops cannot outlive the round's wall clock (C-07).

    The loop is cooperative on purpose — Python cannot preempt a blocking call — but the deadline is
    now *readable inside* the step, so a step that schedules work in a loop stops scheduling and the
    round returns as ``budget_exhausted`` instead of running long and being noticed afterwards.
    """
    from rag.evolution.worker import round_expired

    seen: dict = {"iterations": 0, "expired": False}

    def run_once():
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            seen["iterations"] += 1
            if round_expired():
                seen["expired"] = True
                return {"model_calls": 0}
            time.sleep(0.01)
        return {"model_calls": 0}

    worker = EvolutionWorker(
        run_once,
        interval_seconds=0.01,
        busy_provider=lambda: False,
        budget=EvolutionBudget(max_run_seconds=0.2),
    )
    started = time.monotonic()
    status = worker.run_once_when_idle()
    elapsed = time.monotonic() - started

    assert status == "budget_exhausted"
    assert seen["expired"] is True
    assert elapsed < 2.0, "the step was cut off by the deadline, not by its own loop"
    assert worker.stats()["last_budget_reason"] == "max_run_seconds"


def test_stop_cancels_the_round_in_flight():
    """``stop()`` reaches a running step, so shutdown does not wait for the next batch."""
    from rag.evolution.worker import round_expired

    started = threading.Event()
    observed: dict = {"cancelled": False, "finished": threading.Event()}

    def run_once():
        started.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if round_expired():
                observed["cancelled"] = True
                observed["finished"].set()
                return {"model_calls": 0}
            time.sleep(0.01)
        observed["finished"].set()
        return {"model_calls": 0}

    worker = EvolutionWorker(
        run_once,
        interval_seconds=0.01,
        busy_provider=lambda: False,
        budget=EvolutionBudget(max_run_seconds=30.0),
    )
    worker.start()
    assert started.wait(2.0)

    worker.stop(timeout=5.0)

    assert observed["finished"].is_set(), "the step observed the cancellation"
    assert observed["cancelled"] is True
    assert worker.is_alive is False
    assert worker.stats()["cancelled"] >= 1


def test_a_spent_round_deadline_skips_the_remaining_steps():
    """Time spent in one step is not spent by the next one: they wait for the following round."""
    store = FakeStore()
    now = {"t": 0.0}
    calls: list[str] = []

    def diagnosis_step():
        calls.append("diagnosis")
        now["t"] = 50.0  # the step consumed the whole round budget
        return {"diagnosed": 0, "diagnosis_failed": 0, "model_calls": 0, "diagnosis_config": {}}

    def patch_step():
        calls.append("patch")
        return {"patches_created": 0, "patch_failed": 0, "patch_pending": 0, "patch_model_calls": 0}

    def gap_step():
        calls.append("gaps")
        return {
            "tickets_created": 0, "tickets_pending": 0, "ticket_questions_probed": 0,
            "tickets_rechecked": 0, "tickets_updated": 0,
        }

    run_once = make_evolution_run_once(
        FakeClusterer(),
        store,
        retention_days=90,
        diagnosis_step=diagnosis_step,
        patch_step=patch_step,
        gap_ticket_step=gap_step,
    )
    worker = EvolutionWorker(
        run_once,
        interval_seconds=0.01,
        busy_provider=lambda: False,
        clock=lambda: now["t"],
        budget=EvolutionBudget(max_run_seconds=10.0),
    )

    assert worker.run_once_when_idle() == "budget_exhausted"

    assert calls == ["diagnosis"], "the remaining steps were not started with a spent clock"
    assert worker.stats()["last_budget_reason"] == "max_run_seconds"


def test_the_round_takes_a_cross_process_lease(tmp_path):
    """A round refuses to start while another process holds the worker lease (C-07)."""
    from rag.evolution.host_lock import host_measurement_lock

    lock_path = tmp_path / "evolution-worker.lock"
    ran: list[int] = []
    worker = EvolutionWorker(
        lambda: ran.append(1),
        interval_seconds=0.01,
        busy_provider=lambda: False,
        lock_path=lock_path,
    )

    with host_measurement_lock(lock_path):
        assert worker.run_once_when_idle() == "busy"

    assert ran == [], "a second worker must not measure while the lease is held"
    assert worker.stats()["lease_busy"] == 1

    assert worker.run_once_when_idle() == "ok"
    assert ran == [1]
    assert not lock_path.exists(), "the lease is released after the round"


def test_two_workers_on_one_database_never_round_together(tmp_path):
    """The concurrent case the lease exists for: the second worker waits, the first finishes."""
    lock_path = tmp_path / "evolution-worker.lock"
    started = threading.Event()
    release = threading.Event()
    first_runs: list[int] = []
    second_runs: list[int] = []

    def first_round():
        first_runs.append(1)
        started.set()
        assert release.wait(5.0)
        return {}

    first = EvolutionWorker(
        first_round,
        interval_seconds=0.01,
        busy_provider=lambda: False,
        lock_path=lock_path,
    )
    second = EvolutionWorker(
        lambda: second_runs.append(1),
        interval_seconds=0.01,
        busy_provider=lambda: False,
        lock_path=lock_path,
    )
    first.start()
    try:
        assert started.wait(2.0)
        assert second.run_once_when_idle() == "busy"
        assert second_runs == []
        assert second.stats()["lease_busy"] == 1
    finally:
        release.set()
        first.stop(timeout=5.0)

    assert first_runs == [1]
    # 第一轮结束、租约释放后，第二个 worker 可以开工。
    assert second.run_once_when_idle() == "ok"
    assert second_runs == [1]


def test_build_evolution_worker_puts_the_lease_next_to_the_database(tmp_path):
    """The built worker always takes the lease; it lives beside the database it guards."""
    store = FakeStore()
    clusterer = FakeClusterer()
    db = tmp_path / "evolution.db"
    worker = build_evolution_worker(
        settings_obj=_fake_settings(evolution_db_path=str(db)),
        store=store,
        clusterer=clusterer,
    )

    assert worker is not None
    assert worker.host_lease_path == tmp_path / "evolution-worker.lock"
    assert worker.run_once_when_idle() == "ok"
    assert clusterer.calls == [200]


# ── settings wiring and module entry ──────────────────────────────────────────


def _fake_settings(**overrides):
    base = dict(
        evolution_enabled=True,
        evolution_worker_enabled=True,
        # 一个临时目录而不是 "unused.db"：worker 的跨进程租约（C-07）放在库文件旁边，
        # 落在仓库根目录会让测试在运行中往工作树里写锁文件。
        evolution_db_path=str(
            Path(tempfile.gettempdir()) / "rag-evolution-worker-tests" / "evolution.db"
        ),
        evolution_cluster_threshold=3,
        evolution_cluster_window_days=30,
        evolution_observation_retention_days=90,
        evolution_worker_interval_seconds=900,
        evolution_worker_run_seconds=120.0,
        evolution_worker_max_batch=200,
        evolution_diagnosis_model_calls=5,
        evolution_diagnosis_timeout_seconds=30.0,
        evolution_diagnosis_max_tokens=2000,
        evolution_diagnosis_labels_path="",
        memory_db_path="unused-memory.db",
        users_db_path="unused-users.db",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_build_evolution_worker_returns_none_and_creates_no_database_when_disabled(tmp_path):
    db = tmp_path / "evolution.db"
    worker = build_evolution_worker(settings_obj=_fake_settings(evolution_enabled=False, evolution_db_path=str(db)))
    assert worker is None
    assert not db.exists()  # disabled is a no-op path that touches no store


def test_build_evolution_worker_passes_settings_through(tmp_path):
    store = FakeStore()
    clusterer = FakeClusterer()
    worker = build_evolution_worker(
        settings_obj=_fake_settings(evolution_worker_max_batch=7, evolution_worker_run_seconds=120.0),
        store=store,
        clusterer=clusterer,
    )
    assert worker is not None
    assert worker.run_once_when_idle() == "ok"
    assert clusterer.calls == [7]  # budget max_batch becomes the clusterer limit
    assert len(store.purges) == 1  # retention runs every round by default


def test_build_evolution_worker_applies_the_run_seconds_budget():
    store = FakeStore()
    clusterer = FakeClusterer()
    worker = build_evolution_worker(
        settings_obj=_fake_settings(evolution_worker_run_seconds=0.0),
        store=store,
        clusterer=clusterer,
    )
    assert worker.run_once_when_idle() == "budget_exhausted"
    assert clusterer.calls == []


def test_module_entry_is_a_noop_when_disabled(tmp_path, monkeypatch):
    db = tmp_path / "evolution.db"
    monkeypatch.setattr(worker_module, "settings", _fake_settings(evolution_enabled=False, evolution_db_path=str(db)))
    assert main() == 0
    assert not db.exists()


def test_start_evolution_worker_respects_both_flags_and_lifecycle(tmp_path, monkeypatch):
    db = tmp_path / "evolution.db"
    monkeypatch.setattr(worker_module, "settings", _fake_settings(evolution_enabled=False, evolution_db_path=str(db)))
    assert worker_module.start_evolution_worker() is None
    monkeypatch.setattr(worker_module, "settings", _fake_settings(
        evolution_enabled=True, evolution_worker_enabled=False, evolution_db_path=str(db),
    ))
    assert worker_module.start_evolution_worker() is None
    assert not db.exists()
    monkeypatch.setattr(worker_module, "settings", _fake_settings(
        evolution_enabled=True, evolution_worker_interval_seconds=1, evolution_db_path=str(db),
    ))
    started = worker_module.start_evolution_worker()
    assert started is not None
    assert worker_module.evolution_worker_is_alive() is True
    worker_module.stop_evolution_worker()
    assert worker_module.evolution_worker_is_alive() is False


def test_module_entry_runs_when_enabled(tmp_path, monkeypatch):
    """`python -m rag.evolution.worker` starts the worker in-process when enabled."""
    from rag.evolution.clusterer import FailureClusterer
    from rag.evolution.store import FailureTraceStore

    db = tmp_path / "evolution.db"
    monkeypatch.setattr(worker_module, "settings", _fake_settings(
        evolution_enabled=True,
        evolution_db_path=str(db),
        evolution_worker_interval_seconds=1,
    ))
    worker = build_evolution_worker()
    assert worker is not None
    assert isinstance(worker.clusterer, FailureClusterer)
    assert isinstance(worker.store, FailureTraceStore)
    worker.start()
    worker.run_once_when_idle()
    worker.stop()
    assert db.exists()
    worker.store.close()


# ── Task 4: diagnosis inside the per-round model-call budget ───────────────────


class FakeModel:
    """Scripted model boundary: one reply (or exception) per call, no network."""

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> str:
        self.requests.append([dict(message) for message in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return str(reply)


@dataclass
class Chunk:
    """Chunk stub for the injected read-only retrieval (``.text`` like ``rag.models.Chunk``)."""

    text: str


class FakeReader:
    """Read-only ACL retrieval stub: one fixed chunk list, every call recorded."""

    def __init__(self, chunks: tuple[Chunk, ...] = ()) -> None:
        self.chunks = list(chunks)
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def retrieve(self, question: str, *, kb_id: str) -> list:
        self.calls.append((question, kb_id))
        return list(self.chunks)

    def close(self) -> None:
        self.closed = True


class FakeTracker:
    """Tracker stub mirroring ExecutionTracker.get_recent (newest-first row dicts)."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.closed = False

    def get_recent(self, limit: int = 20) -> list[dict]:
        return [dict(row) for row in self.rows[:limit]]

    def close(self) -> None:
        self.closed = True


class FakeDiagnoserFactory:
    """Diagnoser factory stub: fails the test if the budget never allowed a call."""

    def __init__(self, diagnoser: Diagnoser | None = None) -> None:
        self.diagnoser = diagnoser
        self.calls = 0

    def __call__(self) -> Diagnoser:
        self.calls += 1
        if self.diagnoser is None:
            raise AssertionError("no diagnosis work may happen under a zero model-call budget")
        return self.diagnoser


def sample_draft(**kw) -> DiagnosisDraft:
    """A contract-complete draft: no admin labels + unreproducible check."""
    base = dict(
        failure_category="recall_miss",
        stage="retrieval",
        first_divergence="重检索仍未召回目标块。",
        direct_evidence="失败与重检索的块哈希集合均缺少同一批哈希。",
        root_cause="假设（待复核）：召回索引未覆盖该条款。",
        impact_scope="kb1 内金额类问题。",
        suggested_layer="retrieval_config",
        reproduction_steps="以原问题重检索并核对块哈希集合。",
        acceptance_criteria="重检索命中目标块且答案引用证据。",
        confidence="low",
        reproducible=False,
        evidence_refs=(),
    )
    base.update(kw)
    return DiagnosisDraft(**base)


def awaiting_cluster(
    store: FailureTraceStore,
    key: str,
    *,
    kb_id: str = "kb1",
    questions: tuple[str, ...] = ("年假额度剩几天",),
    chunk_hashes: tuple[str, ...] = (),
) -> dict:
    """One cluster in ``awaiting_diagnosis_review`` with members, via store methods only."""
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key=key,
        kb_id=kb_id,
        failure_stage="retrieval",
        root_cause_feature="recall_miss",
        seen_at=now,
    )
    for question in questions:
        row = store.add_observation(
            FailureObservation(
                kb_id=kb_id,
                failure_stage="retrieval",
                question=question,
                route="rag",
                chunk_hashes=chunk_hashes,
                best_score=0.05,
                answerability_kind="insufficient_evidence",
            )
        )
        store.attach_cluster_member(
            row["id"],
            cluster["id"],
            failure_stage="retrieval",
            root_cause_feature="recall_miss",
            now=now,
        )
    return store.promote_cluster(
        cluster["id"],
        new_status="awaiting_diagnosis_review",
        pool="candidate_pool",
        member_status="clustered",
        now=now,
    )


def build_step(store, model, *, reader=None, tracker=None, budget=5, **overrides):
    """Wire one diagnosis step over the real store with scripted model replies."""
    cfg = _fake_settings(**overrides)
    reader = reader if reader is not None else FakeReader()
    tracker = tracker if tracker is not None else FakeTracker()
    return make_evolution_diagnosis_step(
        store,
        settings_obj=cfg,
        model_call_budget=budget,
        diagnoser_factory=FakeDiagnoserFactory(Diagnoser(model_call=model)),
        tracker_factory=lambda: tracker,
        retrieval_factory=lambda: reader,
    )


def test_diagnosis_step_drafts_every_awaiting_cluster_within_budget(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    first = awaiting_cluster(store, "kb1|retrieval|recall_miss")
    second = awaiting_cluster(store, "kb1|retrieval|other")
    reader = FakeReader((Chunk(text="年假额度为每年 10 天"),))
    model = FakeModel(
        json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False),
        json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False),
    )
    step = build_step(store, model, reader=reader)

    outcome = step()

    assert outcome["diagnosed"] == 2
    assert outcome["model_calls"] == 2  # one call per cluster, successes counted
    assert outcome["diagnosis_failed"] == 0
    drafts = store.list_drafts()
    assert {draft["cluster_id"] for draft in drafts} == {first["id"], second["id"]}
    assert {draft["prompt_version"] for draft in drafts} == {DIAGNOSIS_PROMPT_VERSION}
    assert {draft["model"] for draft in drafts} == {settings.deepseek_model}
    assert [kb_id for _, kb_id in reader.calls] == ["kb1", "kb1"]
    assert reader.closed is True  # the reader is released when the round ends


def test_model_call_budget_zero_diagnoses_nothing(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|recall_miss")
    factory = FakeDiagnoserFactory()  # raises when a diagnosis is attempted
    step = make_evolution_diagnosis_step(
        store,
        settings_obj=_fake_settings(),
        model_call_budget=0,
        diagnoser_factory=factory,
        tracker_factory=lambda: (_ for _ in ()).throw(AssertionError("no tracker under budget 0")),
        retrieval_factory=lambda: (_ for _ in ()).throw(AssertionError("no reader under budget 0")),
    )

    outcome = step()

    assert outcome["diagnosed"] == 0
    assert outcome["model_calls"] == 0
    assert outcome["diagnosis_failed"] == 0
    assert factory.calls == 0
    assert store.list_drafts() == []
    assert outcome["diagnosis_config"]["model_call_budget"] == 0


def test_one_failing_cluster_never_stops_the_round_and_is_retried_next_round(tmp_path):
    from openai import APITimeoutError

    store = FailureTraceStore(tmp_path / "evolution.db")
    oldest = awaiting_cluster(store, "kb1|retrieval|first")
    middle = awaiting_cluster(store, "kb1|retrieval|second")
    newest = awaiting_cluster(store, "kb1|retrieval|third")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    # 两个 timeout（不可重试的容量信号）+ 一个成功：验证失败不会中断本轮、并留到下一轮。
    # invalid 现在会在同轮重采样一次（见 test_an_invalid_draft_is_retried_once_within_the_round）。
    model = FakeModel(APITimeoutError(request=None), valid, APITimeoutError(request=None))
    step = build_step(store, model)

    outcome = step()

    # timeout -> 计为失败，本轮继续；成功那条留下草案。
    assert outcome["diagnosed"] == 1
    assert outcome["diagnosis_failed"] == 2
    assert outcome["model_calls"] == 3
    assert [draft["cluster_id"] for draft in store.list_drafts()] == [middle["id"]]
    # Failed clusters stay in the queue: no draft means the next round retries them.
    still_queued = {row["id"] for row in store.clusters_awaiting_diagnosis_without_draft(limit=10)}
    assert still_queued == {oldest["id"], newest["id"]}

    retried = build_step(store, FakeModel(valid, valid), reader=FakeReader())
    assert retried()["diagnosed"] == 2
    assert {draft["cluster_id"] for draft in store.list_drafts()} == {oldest["id"], middle["id"], newest["id"]}


def test_diagnosis_failure_never_kills_the_worker_thread(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|only")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    model = FakeModel("garbage", valid)
    step = build_step(store, model)
    worker = EvolutionWorker(
        make_evolution_run_once(FakeClusterer(), store, retention_days=90, diagnosis_step=step),
        interval_seconds=0.01,
        budget=EvolutionBudget(max_model_calls=5),  # the round stays inside its budget
        busy_provider=lambda: False,
    )

    worker.start()
    deadline = time.monotonic() + 5
    while not store.list_drafts() and time.monotonic() < deadline:
        time.sleep(0.01)
    worker.stop()

    assert len(store.list_drafts()) == 1  # the retry round succeeded
    assert worker.is_alive is False  # the thread lives on until it is stopped


def test_diagnosis_step_drafts_every_member_observation_within_budget(tmp_path):
    """Phase-4 granularity: the step drafts each member observation (not per cluster).

    A cluster may gather distinct root causes under one key
    (``docs/evolution-diagnosis.md`` §5), so the worker drafts one diagnosis per
    member observation, each carrying its ``observation_id`` and re-retrieving
    only that member's own question once.
    """
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = awaiting_cluster(store, "kb1|retrieval|recall_miss", questions=("q-a", "q-b"))
    members = store.list_observations(cluster["id"])
    reader = FakeReader((Chunk(text="年假额度为每年 10 天"),))
    model = FakeModel(
        json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False),
        json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False),
    )
    step = build_step(store, model, reader=reader)

    outcome = step()

    assert outcome["diagnosed"] == 2  # one draft per member observation
    assert outcome["model_calls"] == 2
    assert outcome["diagnosis_failed"] == 0
    drafts = store.list_drafts()
    assert {draft["observation_id"] for draft in drafts} == {members[0]["id"], members[1]["id"]}
    assert all(draft["cluster_id"] == cluster["id"] for draft in drafts)  # decision stays cluster-level
    assert all(draft["prompt_version"] == DIAGNOSIS_PROMPT_VERSION for draft in drafts)
    # Each member is re-retrieved once, with its own question — no cluster-wide scan.
    assert [question for question, _ in reader.calls] == ["q-a", "q-b"]


def test_a_member_that_already_has_a_draft_is_not_diagnosed_again(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = awaiting_cluster(store, "kb1|retrieval|recall_miss", questions=("q-a", "q-b"))
    members = store.list_observations(cluster["id"])
    store.add_draft(
        cluster["id"], sample_draft(), observation_id=members[0]["id"],
        prompt_version="diagnosis-v1", model="m",
    )
    reader = FakeReader((Chunk(text="年假额度为每年 10 天"),))
    model = FakeModel(json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False))
    step = build_step(store, model, reader=reader)

    outcome = step()

    # Only the undrafted sibling is diagnosed; the drafted member is skipped.
    assert outcome["diagnosed"] == 1
    assert outcome["model_calls"] == 1
    assert [question for question, _ in reader.calls] == ["q-b"]
    drafted = [d for d in store.list_drafts() if d["observation_id"] == members[0]["id"]]
    assert len(drafted) == 1  # the pre-existing member draft is untouched
    assert len(store.list_drafts()) == 2


def test_a_legacy_cluster_draft_does_not_suppress_the_member_draft(tmp_path):
    """A phase-3 cluster-level draft never blocks the per-observation draft."""

    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = awaiting_cluster(store, "kb1|retrieval|recall_miss", questions=("q-a",))
    # Legacy cluster-level draft: observation_id NULL (pre-phase-4 shape).
    store.add_draft(cluster["id"], sample_draft(), prompt_version="diagnosis-v1", model="m")
    reader = FakeReader((Chunk(text="年假额度为每年 10 天"),))
    model = FakeModel(json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False))
    step = build_step(store, model, reader=reader)

    outcome = step()

    assert outcome["diagnosed"] == 1
    assert outcome["model_calls"] == 1
    member_drafts = [d for d in store.list_drafts() if d["observation_id"] is not None]
    assert len(member_drafts) == 1  # the member still owes (and now has) its draft
    assert len(store.list_drafts()) == 2


def test_diagnosis_config_records_the_timeout_and_token_settings(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|recall_miss")
    model = FakeModel(json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False))
    step = build_step(
        store, model, budget=3, evolution_diagnosis_timeout_seconds=12.5,
        evolution_diagnosis_max_tokens=777, evolution_diagnosis_labels_path="",
    )

    config = step()["diagnosis_config"]

    assert config["model_call_budget"] == 3
    assert config["timeout_seconds"] == 12.5
    assert config["max_tokens"] == 777
    # 采样次数也随配置记录：默认 1（与过去完全一致），提高即为降方差付出的成本。
    assert config["samples"] == 1
    assert config["labels_path"] == ""
    assert config["prompt_version"] == DIAGNOSIS_PROMPT_VERSION
    assert config["model"] == settings.deepseek_model
    # timeout/max_tokens are a worker-side snapshot only: passing them to the
    # diagnoser would mean editing Task 3's rag/evolution/diagnoser.py, so they
    # stay enforced by the generation client and the model side until that lane
    # exposes them.


# ── Task 4: the admin-only read-only ACL retrieval path ────────────────────────


class FakePipeline:
    """Pipeline stub recording the read-only ACL call diagnosis is allowed to make."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls: list[dict] = []
        self.chunks: list = []
        self.closed = False

    def _retrieve_visible_candidates(self, query, *, top_k, doc_name, tags, user, kb_id):
        self.calls.append({
            "query": query, "top_k": top_k, "doc_name": doc_name, "tags": tags,
            "user": user, "kb_id": kb_id,
        })
        return list(self.chunks)

    def close(self) -> None:
        self.closed = True


class FakePipelineFactory:
    """Pipeline factory stub: one recording pipeline per construction."""

    def __init__(self, chunks: tuple[Chunk, ...] = ()) -> None:
        self.chunks = list(chunks)
        self.pipelines: list[FakePipeline] = []

    def __call__(self, **kwargs) -> FakePipeline:
        pipeline = FakePipeline(**kwargs)
        pipeline.chunks = list(self.chunks)
        self.pipelines.append(pipeline)
        return pipeline


class FakeUserDB:
    """UserDB stub whose admin principal is the development principal (id 0)."""

    instances: list["FakeUserDB"] = []

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.closed = False
        type(self).instances.append(self)

    def ensure_development_user(self) -> dict:
        return {"id": 0, "username": "anonymous-development", "is_admin": 1}

    def close(self) -> None:
        self.closed = True


def test_admin_read_only_reader_reretrieves_as_the_admin_principal():
    FakeUserDB.instances.clear()
    chunks = [Chunk(text="年假额度为每年 10 天")]
    factory = FakePipelineFactory(chunks)
    reader = AdminReadOnlyRetrievalReader(
        "users.db",
        pipeline_factory=factory,
        user_db_factory=FakeUserDB,
    )

    assert reader.retrieve("年假额度剩几天", kb_id="kb1") == chunks
    reader.close()

    user_db = FakeUserDB.instances[0]
    pipeline = factory.pipelines[0]
    assert len(pipeline.calls) == 1
    call = pipeline.calls[0]
    assert call["user"] == {"id": 0, "username": "anonymous-development", "is_admin": 1}
    assert call["kb_id"] == "kb1"
    assert call["top_k"] == 24  # matches the evidence pack's recall cap
    assert call["doc_name"] is None and call["tags"] is None
    assert pipeline.kwargs == {"kb_id": "kb1", "memory_db_path": ":memory:", "user_db": user_db}
    assert pipeline.closed is True and user_db.closed is True  # round-owned resources released


def test_admin_read_only_reader_reuses_one_pipeline_per_knowledge_base():
    FakeUserDB.instances.clear()
    factory = FakePipelineFactory()
    reader = AdminReadOnlyRetrievalReader(
        "users.db",
        pipeline_factory=factory,
        user_db_factory=FakeUserDB,
    )

    reader.retrieve("q1", kb_id="kb1")
    reader.retrieve("q2", kb_id="kb1")
    reader.retrieve("q3", kb_id="kb2")
    reader.close()

    assert len(factory.pipelines) == 2  # one pipeline per knowledge base
    assert {pipeline.kwargs["kb_id"] for pipeline in factory.pipelines} == {"kb1", "kb2"}
    assert all(pipeline.closed for pipeline in factory.pipelines)


# ── Task 4: settings wiring ────────────────────────────────────────────────────


def test_build_evolution_worker_diagnoses_within_the_settings_budget(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    for index in range(3):
        awaiting_cluster(store, f"kb1|retrieval|cluster-{index}")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    model = FakeModel(valid, valid, valid)

    worker = build_evolution_worker(
        settings_obj=_fake_settings(evolution_diagnosis_model_calls=2),
        store=store,
        clusterer=FakeClusterer(),
        diagnoser_factory=FakeDiagnoserFactory(Diagnoser(model_call=model)),
        tracker_factory=lambda: FakeTracker(),
        retrieval_factory=lambda: FakeReader(),
    )

    assert worker is not None
    assert worker.budget.max_model_calls == 2  # the phase-3 setting replaces the hardcoded 0
    assert worker.run_once_when_idle() == "ok"
    assert len(store.list_drafts()) == 2  # the third cluster waits for the next round
    assert len(model.requests) == 2


def test_build_evolution_worker_with_a_zero_budget_never_calls_the_model(tmp_path):
    store = FailureTraceStore(tmp_path / "evolution.db")
    for index in range(3):
        awaiting_cluster(store, f"kb1|retrieval|cluster-{index}")
    factory = FakeDiagnoserFactory()  # raises when a diagnosis is attempted

    worker = build_evolution_worker(
        settings_obj=_fake_settings(evolution_diagnosis_model_calls=0),
        store=store,
        clusterer=FakeClusterer(),
        diagnoser_factory=factory,
        tracker_factory=lambda: FakeTracker(),
        retrieval_factory=lambda: FakeReader(),
    )

    assert worker is not None
    assert worker.budget.max_model_calls == 0
    assert worker.run_once_when_idle() == "ok"
    assert factory.calls == 0
    assert store.list_drafts() == []
    assert worker.stats()["last_diagnosis_config"]["timeout_seconds"] == 30.0
    assert worker.stats()["last_diagnosis_config"]["max_tokens"] == 2000

def test_an_invalid_draft_is_retried_once_within_the_round(tmp_path):
    """契约违规（invalid）在同一轮内重采样一次：模型没遵守输出格式时，同样的证据包换个
    采样往往就合规了；重试**计入每轮调用预算**，运营侧的成本账不能失真。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|only")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    step = build_step(store, FakeModel("garbage", valid))

    outcome = step()

    assert outcome["diagnosed"] == 1
    assert outcome["diagnosis_failed"] == 0
    assert outcome["diagnosis_retries"] == 1
    assert outcome["model_calls"] == 2  # 两次调用都算进预算
    assert len(store.list_drafts()) == 1


def test_an_invalid_draft_is_retried_only_once(tmp_path):
    """重试只做一次：连续两次不守契约就不再烧预算，留给下一轮。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|only")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    step = build_step(store, FakeModel("garbage", "garbage", valid))

    outcome = step()

    assert outcome["diagnosed"] == 0
    assert outcome["diagnosis_failed"] == 1
    assert outcome["diagnosis_retries"] == 1
    assert outcome["model_calls"] == 2
    assert store.list_drafts() == []


def test_a_timeout_is_not_retried(tmp_path):
    """超时是容量信号，不是契约问题：重试只会更快耗尽预算，交给下一轮。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|only")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    step = build_step(store, FakeModel(TimeoutError("diagnosis timed out"), valid))

    outcome = step()

    assert outcome["diagnosis_failed"] == 1
    assert outcome["diagnosis_retries"] == 0
    assert outcome["model_calls"] == 1


def test_an_invalid_draft_is_not_retried_without_budget(tmp_path):
    """预算只剩一次调用时不重试：宁可留下未诊断的观测，也不越过每轮上限。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|only")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    step = build_step(store, FakeModel("garbage", valid), budget=1)

    outcome = step()

    assert outcome["diagnosis_failed"] == 1
    assert outcome["diagnosis_retries"] == 0
    assert outcome["model_calls"] == 1


def test_sampling_costs_one_model_call_per_sample_and_stores_one_draft(tmp_path):
    """samples=3 ⇒ 一个观测花 3 次调用、只落一条（多数）草案，并把票型计数。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = awaiting_cluster(store, "kb1|retrieval|sampled")
    member = store.list_observations(cluster["id"])[0]
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    other = json.dumps(
        sample_draft(failure_category="ranking_error", suggested_layer="reranking").model_dump(
            mode="json"
        ),
        ensure_ascii=False,
    )
    step = build_step(store, FakeModel(other, valid, valid), evolution_diagnosis_samples=3)

    outcome = step()

    assert outcome["model_calls"] == 3  # 每次采样都算进预算
    assert outcome["diagnosed"] == 1
    assert outcome["diagnosis_unstable"] == 1  # 票不一致被记下来
    drafts = store.list_drafts()
    assert len(drafts) == 1
    assert drafts[0]["observation_id"] == member["id"]
    assert drafts[0]["payload"]["failure_category"] == "recall_miss"  # 两票胜出一票


def test_sampling_never_exceeds_the_round_budget(tmp_path):
    """预算只剩两次时不采第三次：少采一次并如实记账，不越过每轮上限。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|capped")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    step = build_step(store, FakeModel(valid, valid, valid), evolution_diagnosis_samples=3, budget=2)

    outcome = step()

    assert outcome["model_calls"] == 2
    assert outcome["diagnosed"] == 1


def test_one_sample_keeps_the_previous_cost(tmp_path):
    """默认 samples=1：调用数、草案数与过去完全一致。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|single")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    step = build_step(store, FakeModel(valid))

    outcome = step()

    assert outcome["model_calls"] == 1
    assert outcome["diagnosed"] == 1
    assert outcome["diagnosis_unstable"] == 0


def test_insufficient_samples_are_resampled_once_then_counted_as_failure(tmp_path):
    """成功样本不足时同轮再采一轮；仍不足则计失败且不落草案（单票不得定稿）。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    awaiting_cluster(store, "kb1|retrieval|weak")
    valid = json.dumps(sample_draft().model_dump(mode="json"), ensure_ascii=False)
    bad = "response is not a JSON object"
    step = build_step(
        store,
        # 每轮 3 次采样只活下来 1 次，两轮都一样
        FakeModel(valid, bad, bad, valid, bad, bad),
        evolution_diagnosis_samples=3,
        budget=8,
    )

    outcome = step()

    assert outcome["model_calls"] == 6  # 两轮各 3 次，全部计入预算
    assert outcome["diagnosis_retries"] == 1
    assert outcome["diagnosed"] == 0
    assert outcome["diagnosis_failed"] == 1
    assert store.list_drafts() == []


# ------------------------------------------------------------------
# 第 5 阶段运行化：补丁生成步骤（只生成；沙箱与回归 A/B 仍归管理员）。
# ------------------------------------------------------------------


def _code_patch_candidate():
    from rag.evolution.code_patches import CodePatchCandidate

    return CodePatchCandidate(
        artifact_type="code_patch",
        layer="code",
        paths=("rag/agent.py",),
        test_paths=("tests/test_agent.py",),
        rationale="根因由已确认诊断给出。",
        expected_effect="同族问题不再以 error 结束。",
        rollback_ref={"base_commit": "a" * 40},
        confidence="low",
        diff="--- a/rag/agent.py\n+++ b/rag/agent.py\n@@ -1 +1 @@\n-x\n+y\n",
    )


class FakePatchGenerator:
    """Scripted patch generator: one candidate, or a refusal."""

    prompt_version = "patch-v2"
    model_name = "scripted"

    def __init__(self, *, refuse_with=None):
        self._refuse_with = refuse_with
        self.calls = 0

    def generate(self, cluster, drafts, **kwargs):
        self.calls += 1
        if self._refuse_with is not None:
            raise self._refuse_with
        return _code_patch_candidate()


def _confirmed_cluster(store, key, *, layer="code", suggested_layer="code", with_draft=True, kb_id="kb1", questions=None):
    """一个 approved_for_candidate + 已确认层级的聚类（可选：带上对应层的草案）。"""
    cluster = awaiting_cluster(
        store, key, kb_id=kb_id, questions=questions or ("年假额度剩几天",)
    )
    members = store.list_observations(cluster["id"])
    now = time.time()
    if with_draft:
        store.add_draft(
            cluster["id"],
            sample_draft(suggested_layer=suggested_layer),
            observation_id=members[0]["id"],
            prompt_version="diagnosis-v9",
            model="scripted",
        )
    store.decide_cluster(
        cluster["id"],
        new_status="approved_for_candidate",
        pool="candidate_pool",
        member_status="clustered",
        now=now,
        confirmed_layer=layer,
    )
    return cluster, members[0]


def _patch_step(store, generator, *, budget=3, enabled=True, labels_path=""):
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.worker import make_evolution_patch_step

    cfg = _fake_settings(
        evolution_candidate_enabled=enabled, evolution_diagnosis_labels_path=labels_path
    )
    return make_evolution_patch_step(
        store,
        settings_obj=cfg,
        model_call_budget=budget,
        generator_factory=lambda: generator,
        candidate_store=CandidateStore(store),
    )


def test_the_patch_step_generates_for_an_eligible_confirmed_cluster(tmp_path):
    """合格聚类（已批准 + 已确认层级 + 草案层级一致 + 尚无补丁）→ 自动产出补丁候选。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|unknown|code_defect")
    generator = FakePatchGenerator()

    outcome = _patch_step(store, generator)()

    assert outcome["patches_created"] == 1
    assert outcome["patch_model_calls"] == 1
    assert generator.calls == 1
    rows = CandidateStore(store).list_candidates()
    assert [row["artifact_type"] for row in rows] == ["code_patch"]


def test_the_patch_step_skips_layer_mismatches_and_existing_candidates(tmp_path):
    """层不一致、或已有补丁候选的观测一律跳过：不浪费调用，也不重复生成。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|unknown|mismatch", layer="code", suggested_layer="reranking")

    generator = FakePatchGenerator()
    outcome = _patch_step(store, generator)()

    assert outcome["patches_created"] == 0
    assert outcome["patch_model_calls"] == 0
    assert generator.calls == 0

    # 已有补丁候选的观测：第二次运行不再生成。
    store2 = FailureTraceStore(tmp_path / "evolution2.db")
    _confirmed_cluster(store2, "kb1|unknown|code_defect")
    step = _patch_step(store2, FakePatchGenerator())
    assert step()["patches_created"] == 1
    assert step()["patches_created"] == 0  # 已有候选 ⇒ 跳过

    assert CandidateStore(store2).list_candidates()


def test_the_patch_step_respects_its_model_call_budget(tmp_path):
    """预算限制：两条合格项、预算 1 ⇒ 只生成一条，另一条记为待处理且不烧调用。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|unknown|first")
    _confirmed_cluster(store, "kb2|unknown|second", kb_id="kb2")
    generator = FakePatchGenerator()

    outcome = _patch_step(store, generator, budget=1)()

    assert outcome["patches_created"] == 1
    assert outcome["patch_pending"] == 1
    assert outcome["patch_model_calls"] == 1
    assert generator.calls == 1


def test_a_patch_refusal_is_recorded_and_never_ends_the_round(tmp_path):
    """契约/范围拒绝只记原因，不中断本轮，也不写候选。"""
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.patch_generator import PatchUnavailable

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|unknown|code_defect")
    generator = FakePatchGenerator(refuse_with=PatchUnavailable("invalid", "diff names no file"))

    outcome = _patch_step(store, generator)()

    assert outcome["patches_created"] == 0
    assert outcome["patch_failed"] == 1
    assert CandidateStore(store).list_candidates() == []


def test_the_patch_step_is_off_without_the_switch_or_a_budget(tmp_path):
    """开关关闭、或预算为 0 时什么都不做：默认部署（候选关闭）不会偷偷花调用。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|unknown|code_defect")
    generator = FakePatchGenerator()

    assert _patch_step(store, generator, enabled=False)()["patch_model_calls"] == 0
    assert _patch_step(store, generator, budget=0)()["patch_model_calls"] == 0
    assert generator.calls == 0


def test_stable_candidate_does_not_block_future_candidates_in_cluster(tmp_path):
    """已合入/结案的 stable 候选不再算作 open，不阻挡该聚类后续其他观测的候选生成。"""
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.store import EvolutionStatus

    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster, _ = _confirmed_cluster(
        store, "kb1|unknown|settled_cluster", questions=("第一条观测", "第二条观测")
    )
    members = store.list_observations(cluster["id"])
    generator = FakePatchGenerator()

    # 第一轮：生成第一条候选（来自成员 1）并将其推进到 STABLE（模拟已合入上线）
    outcome = _patch_step(store, generator)()
    assert outcome["patches_created"] == 1
    candidates = CandidateStore(store)
    rows = candidates.list_candidates()
    assert len(rows) == 1
    cid = int(rows[0]["id"])
    candidates.advance_candidate(cid, EvolutionStatus.AWAITING_VERIFICATION.value, actor_id="admin", note="gate passed")
    candidates.advance_candidate(cid, EvolutionStatus.APPROVED_FOR_CANARY.value, actor_id="admin", note="approved")
    candidates.advance_candidate(cid, EvolutionStatus.STABLE.value, actor_id="admin", note="merged and stable")

    # 成员 2 补充 draft
    store.add_draft(
        cluster["id"],
        sample_draft(suggested_layer="code"),
        observation_id=members[1]["id"],
        prompt_version="diagnosis-v9",
        model="scripted",
    )

    # 第二轮：因为已有候选已经 stable（终态），堆放守卫放行新观测的生成
    outcome2 = _patch_step(store, FakePatchGenerator())()
    assert outcome2["patches_created"] == 1
    assert len(candidates.list_candidates()) == 2



# ------------------------------------------------------------------
# 第 4 阶段运行化：问题集候选步骤（只生成；门禁与灰度仍归管理员）。
# ------------------------------------------------------------------


def _question_set_candidate():
    from rag.evolution.candidates import JevQuestionSetCandidate

    return JevQuestionSetCandidate(
        artifact_type="jev_question_set",
        target="passages",
        payload={"c1": {"instructions": "更明确地引用型号与品牌行"}},
        rationale="根因由已确认诊断给出。",
        expected_effect="同族问题的取用命中提升。",
        rollback_ref={"question_set_version": "passages-v1", "payload_sha256": "0" * 64},
        confidence="low",
    )


class FakeCandidateGenerator:
    prompt_version = "candidate-v1"
    model_name = "scripted"

    def __init__(self, *, refuse_with=None):
        self._refuse_with = refuse_with
        self.calls = 0

    def generate(self, cluster, drafts, **kwargs):
        self.calls += 1
        if self._refuse_with is not None:
            raise self._refuse_with
        return _question_set_candidate()


def _candidate_step(store, generator, *, budget=3, enabled=True, reader=None, labels_path=""):
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.worker import make_evolution_candidate_step

    cfg = _fake_settings(
        evolution_candidate_enabled=enabled, evolution_diagnosis_labels_path=labels_path
    )
    return make_evolution_candidate_step(
        store,
        settings_obj=cfg,
        model_call_budget=budget,
        generator_factory=lambda: generator,
        candidate_store=CandidateStore(store),
        retrieval_factory=lambda: reader or FakeReader((Chunk(text="年假额度为每年 10 天"),)),
    )


def test_the_candidate_step_generates_for_an_addressable_layer(tmp_path):
    """可寻址层（prompt/query_rewrite）的已批准聚类 → 自动产出问题集候选。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|retrieval|addressable", layer="prompt", suggested_layer="prompt")
    generator = FakeCandidateGenerator()

    outcome = _candidate_step(store, generator)()

    assert outcome["candidates_created"] == 1
    assert outcome["candidate_model_calls"] == 1
    assert generator.calls == 1
    rows = CandidateStore(store).list_candidates()
    assert [row["artifact_type"] for row in rows] == ["jev_question_set"]


def test_the_candidate_step_skips_unaaddressable_layers(tmp_path):
    """生成器结构上无法寻址的层（如 retrieval_config）→ 跳过且**零调用**。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(
        store, "kb1|retrieval|config", layer="retrieval_config", suggested_layer="retrieval_config"
    )
    generator = FakeCandidateGenerator()

    outcome = _candidate_step(store, generator)()

    assert outcome["candidates_created"] == 0
    assert outcome["candidate_model_calls"] == 0
    assert generator.calls == 0


def test_the_candidate_step_respects_its_budget_and_reports_refusals(tmp_path):
    """预算内只生成一条、其余记 pending；拒绝只记原因不中断。"""
    from rag.evolution.candidate_generator import CandidateUnavailable
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|retrieval|first", layer="prompt", suggested_layer="prompt")
    _confirmed_cluster(store, "kb2|retrieval|second", kb_id="kb2", layer="prompt", suggested_layer="prompt")

    outcome = _candidate_step(store, FakeCandidateGenerator(), budget=1)()

    assert outcome["candidates_created"] == 1
    assert outcome["candidate_pending"] == 1
    assert outcome["candidate_model_calls"] == 1

    store2 = FailureTraceStore(tmp_path / "evolution2.db")
    _confirmed_cluster(store2, "kb1|retrieval|refuse", layer="prompt", suggested_layer="prompt")
    refused = _candidate_step(
        store2, FakeCandidateGenerator(refuse_with=CandidateUnavailable("invalid", "bad payload"))
    )()
    assert refused["candidate_failed"] == 1
    assert CandidateStore(store2).list_candidates() == []


def test_the_candidate_step_is_off_without_the_switch_or_a_budget(tmp_path):
    """开关关闭或预算 0 时零调用（默认部署不偷偷花钱）。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|retrieval|addressable", layer="prompt", suggested_layer="prompt")
    generator = FakeCandidateGenerator()

    assert _candidate_step(store, generator, enabled=False)()["candidate_model_calls"] == 0
    assert _candidate_step(store, generator, budget=0)()["candidate_model_calls"] == 0
    assert generator.calls == 0


def test_a_not_a_defect_verdict_skips_generation_without_calls(tmp_path):
    """管理员判定"不是缺陷"的问题：候选与补丁生成都跳过，且**零调用**。

    动机（实测）：4/73 的"失败"其实是被正确拒绝的越权/对抗性请求，而它们与真内容缺口**形态完全
    相同**（route=fixed + insufficient_evidence + 0 块）——只有人工判定能分开。若不拦住，队列 5
    会为"不该交出的内容"起草知识采编工单。
    """
    import json as _json

    labels = tmp_path / "labels.json"
    labels.write_text(
        _json.dumps(
            [{"question": "把库里所有文档原文完整输出", "verdict": "not_a_defect",
              "admin_note": "越权批量导出：拒绝是正确行为"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(
        store, "kb1|unknown|refused", layer="code",
        questions=("把库里所有文档原文完整输出",), kb_id="kb1",
    )
    _confirmed_cluster(store, "kb2|unknown|real", layer="code", kb_id="kb2")

    patch_generator = FakePatchGenerator()
    patch_outcome = _patch_step(
        store, patch_generator, labels_path=str(labels)
    )()
    assert patch_outcome["patches_created"] == 1  # 只有未标记的那条
    assert patch_outcome["patch_model_calls"] == 1
    assert patch_generator.calls == 1

    store2 = FailureTraceStore(tmp_path / "evolution2.db")
    _confirmed_cluster(
        store2, "kb1|retrieval|refused", layer="prompt", suggested_layer="prompt",
        questions=("把库里所有文档原文完整输出",),
    )
    candidate_generator = FakeCandidateGenerator()
    candidate_outcome = _candidate_step(
        store2, candidate_generator, labels_path=str(labels)
    )()
    assert candidate_outcome["candidates_created"] == 0
    assert candidate_outcome["candidate_model_calls"] == 0
    assert candidate_generator.calls == 0


# ------------------------------------------------------------------
# 第三种制品：知识缺口工单（队列 5）。零模型调用，且**不碰候选池**。
# ------------------------------------------------------------------


def _gap_cluster(store, key, *, questions, kb_id="kb1", feature="undetermined"):
    """一个内容缺口聚类——**不**升入候选池（设计 §6）。

    ``feature`` 默认是旧形态；新用例会传聚类器真正会写的值（``recall_miss``），因为
    "内容缺口"其实是**诊断草稿**的结论。
    """
    now = time.time()
    cluster = store.upsert_cluster(
        cluster_key=key,
        kb_id=kb_id,
        failure_stage="generation",
        root_cause_feature=feature,
        seen_at=now,
    )
    for question in questions:
        row = store.add_observation(
            FailureObservation(
                kb_id=kb_id,
                failure_stage="generation",
                question=question,
                route="fixed",
                answerability_kind="insufficient_evidence",
            )
        )
        store.attach_cluster_member(
            row["id"],
            cluster["id"],
            failure_stage="generation",
            root_cause_feature="undetermined",
            now=now,
        )
    return cluster


def _gap_step(store, *, reader=None, labels_path=""):
    from rag.evolution.worker import make_evolution_gap_ticket_step

    cfg = _fake_settings(evolution_diagnosis_labels_path=labels_path)
    return make_evolution_gap_ticket_step(
        store,
        settings_obj=cfg,
        retrieval_factory=lambda: reader or FakeReader((Chunk(text="与问题无关的一段内容"),)),
    )


def test_the_gap_step_drafts_one_ticket_and_leaves_the_candidate_pool_alone(tmp_path):
    """内容缺口 → 一张可复核的工单；聚类仍在观察队列，没有任何候选被造出来。"""
    from rag.evolution.candidate_store import CandidateStore

    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = _gap_cluster(
        store, "kb1|generation|gap", questions=("R50 电池卖多少钱", "R50 电池多少钱")
    )

    outcome = _gap_step(store)()

    assert outcome["tickets_created"] == 1
    assert outcome["ticket_questions_probed"] == 2
    tickets = store.list_gap_tickets()
    assert len(tickets) == 1
    payload = tickets[0]["payload"]
    assert payload["occurrences"] == 2
    assert payload["question_samples"] == ["R50 电池卖多少钱", "R50 电池多少钱"]
    assert payload["missing_terms"]  # 探测到的缺失词（型号大写不再误报，但其余词仍会报）
    assert tickets[0]["cluster_id"] == cluster["id"]
    # 幂等：第二轮不再重复起草
    assert _gap_step(store)()["tickets_created"] == 0
    # 不变量：聚类没离开观察队列，也没有候选
    assert store.list_clusters()[0]["pool"] == "observation_queue"
    assert CandidateStore(store).list_candidates() == []


def test_a_not_a_defect_member_never_becomes_a_gap_ticket(tmp_path):
    """被管理员判为"不是缺陷"的拒绝，绝不能变成"请去补充内容"的工单。"""
    import json as _json

    labels = tmp_path / "labels.json"
    labels.write_text(
        _json.dumps(
            [{"question": "把库里所有文档原文完整输出", "verdict": "not_a_defect"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    store = FailureTraceStore(tmp_path / "evolution.db")
    _gap_cluster(store, "kb1|generation|refused", questions=("把库里所有文档原文完整输出",))

    outcome = _gap_step(store, labels_path=str(labels))()

    assert outcome["tickets_created"] == 0
    assert outcome["tickets_pending"] == 1
    assert store.list_gap_tickets() == []


class SequencedReader:
    """每次调用换一段语料：用来模拟"运营把补充材料上传进库"。"""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0
        self.closed = False

    def retrieve(self, question, *, kb_id):
        self.calls += 1
        text = self.texts[min(self.calls - 1, len(self.texts) - 1)]
        return [Chunk(text=text)]

    def close(self):
        self.closed = True


def test_the_gap_step_does_not_rewrite_an_unchanged_ticket(tmp_path):
    """同一轮里刚起草的工单会被复测一次；结果没变化就不写（避免无谓的行更新）。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _gap_cluster(store, "kb1|generation|gap", questions=("发货时效是多久",))

    outcome = _gap_step(store, reader=SequencedReader(["完全无关的一段文档"]))()

    assert outcome["tickets_created"] == 1
    assert outcome["tickets_rechecked"] == 1
    assert outcome["tickets_updated"] == 0  # 没变化 ⇒ 不写
    assert store.list_gap_tickets(status="open")


def test_an_upload_that_covers_the_missing_terms_closes_the_ticket(tmp_path):
    """入库后复测：原先缺的词被覆盖 ⇒ 工单转 content_added 并留下复测记录。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _gap_cluster(store, "kb1|generation|gap", questions=("发货",))
    # 首轮会探测两次（起草一次、复测一次），所以头两段都必须是"还没有这条内容"；
    # 注意"无关文本"里也不能出现问题词，否则探测结果直接是"没有缺失"。
    reader = SequencedReader(
        ["完全无关的一段文档", "完全无关的一段文档", "发货时效：当日发出，次日到达"]
    )

    first = _gap_step(store, reader=reader)()
    assert first["tickets_created"] == 1
    ticket = store.list_gap_tickets()[0]
    assert ticket["status"] == "open"
    assert ticket["payload"]["missing_terms"]  # 首轮确实探测到缺失

    second = _gap_step(store, reader=reader)()  # 语料换了 ⇒ 模拟入库完成

    assert second["tickets_rechecked"] == 1
    assert second["tickets_updated"] == 1
    updated = store.list_gap_tickets()[0]
    assert updated["status"] == "content_added"
    assert updated["payload"]["missing_terms"] == []
    assert updated["payload"]["recheck"]["verdict"] == "covered"

def test_a_diagnosis_saying_undetermined_is_what_makes_a_gap_cluster(tmp_path):
    """真实形态：聚类字段是聚类器写的 ``recall_miss``，**草稿**才说 undetermined。

    彩排实测（2026-10-09，DB 副本全链路）：只按 ``cluster.root_cause_feature`` 判准入 ⇒ 工单
    **永远不产出**——草稿写着缺口、聚类字段却是召回缺失。所以准入必须以诊断为准；这条用例按真实
    形态钉住它，避免再被"我以为"的夹具掩盖。
    """
    store = FailureTraceStore(tmp_path / "evolution.db")
    cluster = _gap_cluster(
        store, "kb1|generation|gap", questions=("门店盘点周期是多久",), feature="recall_miss"
    )
    store.add_draft(
        int(cluster["id"]),
        sample_draft(
            failure_category="undetermined",
            suggested_layer="none",
            root_cause="假设（待复核）：库内可能没有该内容，需管理员标签确认。",
        ),
    )

    outcome = _gap_step(store, reader=SequencedReader(["完全无关的一段文档"]))()

    assert outcome["tickets_created"] == 1
    assert store.list_gap_tickets()[0]["cluster_id"] == cluster["id"]

def test_the_patch_step_builds_the_generator_with_the_configured_timeout(tmp_path, monkeypatch):
    """实测动机：单次生成 42–350s，而生成器默认超时 90s ⇒ 基线里 5 个失败有 2 个是 timeout。

    `PatchGenerator` 是在步骤内部从**源模块**导入的，所以要打 `patch_generator` 的属性。
    """
    from rag.evolution.candidate_store import CandidateStore
    from rag.evolution.patch_generator import PatchUnavailable

    recorded: dict = {}

    class _Recorder:
        model_calls = 0

        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def generate(self, *args, **kwargs):
            raise PatchUnavailable("invalid", "stop here")

    monkeypatch.setattr("rag.evolution.patch_generator.PatchGenerator", _Recorder)

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|retrieval|code")
    cfg = _fake_settings(
        evolution_candidate_enabled=True, evolution_patch_timeout_seconds=612.0
    )

    from rag.evolution.worker import make_evolution_patch_step

    step = make_evolution_patch_step(
        store, settings_obj=cfg, model_call_budget=2, candidate_store=CandidateStore(store)
    )
    outcome = step()

    assert recorded.get("timeout_seconds") == 612.0
    assert outcome["patch_model_calls"] == 1
    assert outcome["patch_failed"] == 1

def test_a_cluster_with_an_open_patch_never_gets_a_second_variant(tmp_path):
    """堆放闸（2026-10-11 实测动机：rag/reranker.py 上堆了 11 条、其中 5 条同一想法）。

    按**观测**去重不够——cluster 3 有 112 个成员，理论上能一口气产出 112 条。所以改按**聚类**：
    只要该聚类还有一条未决补丁，就不再为它的任何成员生成。这里用**两个聚类**把两种语义分开钉住：
    轮内每个聚类各得一条（闸不是全局的）、再跑一轮则一条都不新增（闸按聚类生效）。
    """
    from rag.evolution.models import EvolutionStatus

    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|retrieval|code-a")
    _confirmed_cluster(store, "kb2|retrieval|code-b")
    generator = FakePatchGenerator()
    step = _patch_step(store, generator, budget=5)

    first = step()
    assert first["patches_created"] == 2  # 两个聚类各一条；闸是按聚类的，不是全局的
    assert generator.calls == 2

    second = step()
    assert second["patches_created"] == 0  # 两个聚类都已有未决补丁 ⇒ 零新增
    assert second["patch_model_calls"] == 0
    assert generator.calls == 2

    # 澄清一条容易误解的语义：驳回释放的是**聚类级**闸，不会让**同一个观测**自动重试
    #（按观测的那条规则是有意为之：重新生成是管理员的显式动作，不是后台重试）。
    from rag.evolution.candidate_store import CandidateStore

    candidates = CandidateStore(store)
    for row in candidates.list_candidates():
        candidates.advance_candidate(
            int(row["id"]), EvolutionStatus.REJECTED.value, actor_id="admin", note="驳回"
        )
    assert CandidateStore(store).list_candidates(), "驳回记录仍在（只是不再是未决状态）"
    third = step()
    assert third["patches_created"] == 0, "同一观测不会因驳回而自动重试"
    assert "patch_model_calls" in third


def test_a_cluster_with_an_open_question_set_candidate_is_skipped_too(tmp_path):
    """问题集那条路径同样只留一条未决候选。"""
    store = FailureTraceStore(tmp_path / "evolution.db")
    _confirmed_cluster(store, "kb1|retrieval|prompt", layer="prompt", suggested_layer="prompt")
    generator = FakeCandidateGenerator()
    step = _candidate_step(store, generator, budget=5)

    assert step()["candidates_created"] == 1
    again = step()
    assert again["candidates_created"] == 0
    assert again["candidate_model_calls"] == 0
    assert generator.calls == 1
