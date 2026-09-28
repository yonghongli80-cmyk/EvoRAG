"""Task 3: budgets, busy pause and exception isolation for the evolution worker."""

import json
import logging
import os
import threading
import time
import types
from dataclasses import dataclass

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
    model = FakeModel("not json at all", valid, APITimeoutError(request=None))
    step = build_step(store, model)

    outcome = step()

    # invalid -> no draft, round continues; timeout -> counted as failed, round continues.
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
