"""Task 3: budgets, busy pause and exception isolation for the evolution worker."""

import os
import threading
import time
import types

import pytest

import rag.evolution.worker as worker_module
from rag.evolution.worker import (
    BudgetExhausted,
    EvolutionBudget,
    EvolutionWorker,
    build_evolution_worker,
    default_busy_provider,
    main,
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

    def purge_observations_before(self, timestamp):
        self.purges.append(timestamp)
        return 0


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


# ── settings wiring and module entry ──────────────────────────────────────────


def _fake_settings(**overrides):
    base = dict(
        evolution_enabled=True,
        evolution_worker_enabled=True,
        evolution_db_path="unused.db",
        evolution_cluster_threshold=3,
        evolution_cluster_window_days=30,
        evolution_observation_retention_days=90,
        evolution_worker_interval_seconds=900,
        evolution_worker_run_seconds=120.0,
        evolution_worker_max_batch=200,
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
