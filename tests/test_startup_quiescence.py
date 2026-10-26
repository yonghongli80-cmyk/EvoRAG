"""Real-thread regression coverage for early API startup cancellation."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_cancelled_lifespan_defers_cleanup_until_index_thread_finishes(monkeypatch):
    """Cancellation must not close resources still used by to_thread startup work."""
    import rag.api as api
    from rag.startup_lifecycle import StartupLifecycle

    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    cleanup_finished = threading.Event()
    resource_open = threading.Event()
    resource_open.set()
    worker_saw_open_resource = []
    cleanup_calls = []

    def blocked_index():
        entered.set()
        try:
            release.wait(10)
            worker_saw_open_resource.append(resource_open.is_set())
        finally:
            worker_finished.set()

    def close_resources():
        cleanup_calls.append("close")
        resource_open.clear()
        cleanup_finished.set()

    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: None)
    monkeypatch.setattr(api, "auto_index_on_startup", blocked_index)
    monkeypatch.setattr(api, "_run_staged_version_migration", lambda: None)
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: None)
    monkeypatch.setattr(api, "start_evolution_worker", lambda: None)
    monkeypatch.setattr(api, "restore_question_set_rollout", lambda: None)
    monkeypatch.setattr(api, "stop_backup_scheduler", lambda: cleanup_calls.append("backup"))
    monkeypatch.setattr(api, "stop_evolution_worker", lambda: cleanup_calls.append("evolution"))
    monkeypatch.setattr(api, "close_pipeline_processors", close_resources)
    monkeypatch.setattr(
        api.app.state,
        "startup_lifecycle_owner",
        StartupLifecycle(cancel_grace_seconds=0.05),
    )

    async def exercise_lifespan_cancellation():
        lifespan_task = asyncio.create_task(_run_lifespan(api))
        try:
            assert await asyncio.to_thread(entered.wait, 5), "index thread did not enter"
            lifespan_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lifespan_task

            assert not worker_finished.is_set(), "cancel unexpectedly stopped the Python thread"
            assert resource_open.is_set(), "resources closed while startup thread was still using them"
            assert cleanup_calls == [], "cleanup ran before startup work became quiescent"
        finally:
            release.set()

        assert await asyncio.to_thread(worker_finished.wait, 5), "index thread did not finish"
        assert await asyncio.to_thread(cleanup_finished.wait, 5), "deferred cleanup did not run"
        assert worker_saw_open_resource == [True]
        assert cleanup_calls == ["backup", "evolution", "close"]

    asyncio.run(exercise_lifespan_cancellation())


async def _run_lifespan(api):
    async with api.lifespan(api.app):
        pass


def test_repeated_cancel_keeps_one_bounded_quiescence_deadline(monkeypatch):
    import rag.api as api
    from rag.startup_lifecycle import StartupLifecycle

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cleanup_finished = threading.Event()
    cleanup_calls = []
    resource_open = threading.Event()
    resource_open.set()

    def blocked_index():
        entered.set()
        try:
            release.wait(10)
        finally:
            finished.set()

    def close_resources():
        cleanup_calls.append("close")
        resource_open.clear()
        cleanup_finished.set()

    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: None)
    monkeypatch.setattr(api, "auto_index_on_startup", blocked_index)
    monkeypatch.setattr(api, "_run_staged_version_migration", lambda: None)
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: None)
    monkeypatch.setattr(api, "start_evolution_worker", lambda: None)
    monkeypatch.setattr(api, "restore_question_set_rollout", lambda: None)
    monkeypatch.setattr(api, "stop_backup_scheduler", lambda: cleanup_calls.append("backup"))
    monkeypatch.setattr(api, "stop_evolution_worker", lambda: cleanup_calls.append("evolution"))
    monkeypatch.setattr(api, "close_pipeline_processors", close_resources)
    monkeypatch.setattr(
        api.app.state,
        "startup_lifecycle_owner",
        StartupLifecycle(cancel_grace_seconds=0.15),
    )

    async def exercise():
        lifespan_task = asyncio.create_task(_run_lifespan(api))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            started = time.monotonic()
            lifespan_task.cancel()
            await asyncio.sleep(0.04)
            lifespan_task.cancel()
            await asyncio.sleep(0.04)
            lifespan_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await lifespan_task

            elapsed = time.monotonic() - started
            assert 0.12 <= elapsed < 0.4
            assert not finished.is_set()
            assert resource_open.is_set()
            assert cleanup_calls == []
        finally:
            release.set()

        assert await asyncio.to_thread(finished.wait, 5)
        assert await asyncio.to_thread(cleanup_finished.wait, 5)
        assert cleanup_calls == ["backup", "evolution", "close"]

    asyncio.run(exercise())


def test_cancelled_queued_work_never_enters_callable():
    from rag.startup_lifecycle import StartupLifecycle

    blocker_entered = threading.Event()
    release_blocker = threading.Event()
    callable_entered = threading.Event()
    cleanup_calls = []
    owner = StartupLifecycle(cancel_grace_seconds=0.1)

    def occupy_only_executor_thread():
        blocker_entered.set()
        release_blocker.wait(10)

    async def exercise():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        blocker = loop.run_in_executor(None, occupy_only_executor_thread)
        deadline = loop.time() + 5
        while not blocker_entered.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.005)
        assert blocker_entered.is_set(), "executor blocker did not enter"

        owner.enter()
        queued_call = asyncio.create_task(owner.to_thread(lambda: callable_entered.set()))
        try:
            await asyncio.sleep(0)
            await owner.finish(
                (("cleanup", lambda: cleanup_calls.append("cleanup")),),
                asyncio.CancelledError("startup cancelled while queued"),
            )
            assert cleanup_calls == ["cleanup"]
            assert not callable_entered.is_set()
        finally:
            release_blocker.set()

        await blocker
        with pytest.raises(asyncio.CancelledError):
            await queued_call
        assert not callable_entered.is_set()
        assert cleanup_calls == ["cleanup"]

    asyncio.run(exercise())


def test_abandoned_startup_blocks_a_new_lifespan_until_cleanup_finishes(monkeypatch):
    import rag.api as api
    from rag.startup_lifecycle import StartupLifecycle

    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    cleanup_finished = threading.Event()
    legacy_calls = []
    index_calls = []
    cleanup_calls = []

    def index():
        index_calls.append("index")
        if len(index_calls) == 1:
            entered.set()
            try:
                release.wait(10)
            finally:
                worker_finished.set()

    def close_resources():
        cleanup_calls.append("close")
        if cleanup_calls[-1] == "close":
            cleanup_finished.set()

    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: legacy_calls.append("legacy"))
    monkeypatch.setattr(api, "auto_index_on_startup", index)
    monkeypatch.setattr(api, "_run_staged_version_migration", lambda: None)
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: None)
    monkeypatch.setattr(api, "start_evolution_worker", lambda: None)
    monkeypatch.setattr(api, "restore_question_set_rollout", lambda: None)
    monkeypatch.setattr(api, "stop_backup_scheduler", lambda: cleanup_calls.append("backup"))
    monkeypatch.setattr(api, "stop_evolution_worker", lambda: cleanup_calls.append("evolution"))
    monkeypatch.setattr(api, "close_pipeline_processors", close_resources)
    monkeypatch.setattr(
        api.app.state,
        "startup_lifecycle_owner",
        StartupLifecycle(cancel_grace_seconds=0.05),
    )

    async def exercise():
        abandoned = asyncio.create_task(_run_lifespan(api))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
            assert api.app.state.startup_lifecycle_owner.state == "cleanup_deferred"

            with pytest.raises(RuntimeError, match="previous API startup"):
                await _run_lifespan(api)
            assert legacy_calls == ["legacy"]
            assert cleanup_calls == []
        finally:
            release.set()

        assert await asyncio.to_thread(worker_finished.wait, 5)
        assert await asyncio.to_thread(cleanup_finished.wait, 5)
        assert cleanup_calls == ["backup", "evolution", "close"]

        await _run_lifespan(api)
        assert legacy_calls == ["legacy", "legacy"]
        assert index_calls == ["index", "index"]
        assert cleanup_calls == [
            "backup",
            "evolution",
            "close",
            "backup",
            "evolution",
            "close",
        ]

    asyncio.run(exercise())


def test_early_thread_error_and_cleanup_errors_preserve_primary(monkeypatch):
    import rag.api as api

    calls = []
    primary = RuntimeError("index migration failed")
    cleanup_failure = RuntimeError("backup stop failed")

    def fail_index():
        raise primary

    def fail_backup_stop():
        calls.append("backup")
        raise cleanup_failure

    monkeypatch.setattr(api, "_run_legacy_kb_migration", lambda: None)
    monkeypatch.setattr(api, "auto_index_on_startup", fail_index)
    monkeypatch.setattr(api, "_run_staged_version_migration", lambda: None)
    monkeypatch.setattr(api, "start_backup_scheduler", lambda: None)
    monkeypatch.setattr(api, "start_evolution_worker", lambda: None)
    monkeypatch.setattr(api, "restore_question_set_rollout", lambda: None)
    monkeypatch.setattr(api, "stop_backup_scheduler", fail_backup_stop)
    monkeypatch.setattr(api, "stop_evolution_worker", lambda: calls.append("evolution"))
    monkeypatch.setattr(api, "close_pipeline_processors", lambda: calls.append("close"))

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(_run_lifespan(api))

    assert raised.value is primary
    assert calls == ["backup", "evolution", "close"]


def test_old_completion_callback_cannot_release_a_new_startup(monkeypatch):
    from rag.startup_lifecycle import StartupLifecycle

    owner = StartupLifecycle(cancel_grace_seconds=0.1)
    entered = threading.Event()
    release = threading.Event()
    callback_paused = threading.Event()
    resume_callback = threading.Event()
    old_cleanup = []
    old_generation = 1
    original_finalizer = owner._run_finalizer

    def pause_old_completion(generation, *, deferred):
        if generation == old_generation and deferred:
            callback_paused.set()
            if not resume_callback.wait(10):
                raise RuntimeError("test did not resume stale callback")
        return original_finalizer(generation, deferred=deferred)

    monkeypatch.setattr(owner, "_run_finalizer", pause_old_completion)

    def old_work():
        entered.set()
        release.wait(10)

    async def exercise():
        owner.enter()
        old_task = asyncio.create_task(owner.to_thread(old_work))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            old_token = owner._active_work
            assert old_token is not None
            finish_task = asyncio.create_task(
                owner.finish(
                    (("old", lambda: old_cleanup.append("old")),),
                    asyncio.CancelledError("old startup cancelled"),
                )
            )
            await asyncio.sleep(0)
            assert owner.state == "cleanup_pending"
            release.set()
            assert await asyncio.to_thread(callback_paused.wait, 5)

            # Model the concurrent finish path winning after the old callback
            # decided to finalize but before it can claim the one-shot latch.
            original_finalizer(old_generation, deferred=False)
            assert old_cleanup == ["old"]
            owner.enter()
            resume_callback.set()

            await finish_task
            await old_task
            owner._work_finished(old_generation, old_token)
            assert owner.state == "starting"
            with pytest.raises(RuntimeError, match="previous API startup"):
                owner.enter()
            await owner.finish((), None)
        finally:
            release.set()
            resume_callback.set()

    asyncio.run(exercise())


def test_completion_after_event_loop_close_runs_cleanup_without_pending_task():
    from rag.startup_lifecycle import StartupLifecycle

    owner = StartupLifecycle(cancel_grace_seconds=0.05)
    entered = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    cleanup_finished = threading.Event()
    cleanup_calls = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def blocked_work():
        entered.set()
        try:
            release.wait(10)
        finally:
            worker_finished.set()

    async def run_cancelled_lifecycle():
        owner.enter()
        try:
            await owner.to_thread(blocked_work)
        except BaseException as error:
            await owner.finish(
                (("cleanup", lambda: (cleanup_calls.append("cleanup"), cleanup_finished.set())),),
                error,
            )
            raise

    try:
        lifecycle_task = loop.create_task(run_cancelled_lifecycle())

        async def wait_until_entered():
            deadline = loop.time() + 5
            while not entered.is_set() and loop.time() < deadline:
                await asyncio.sleep(0.005)
            assert entered.is_set(), "worker did not enter"

        loop.run_until_complete(wait_until_entered())
        lifecycle_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            loop.run_until_complete(lifecycle_task)
        assert owner.state == "cleanup_deferred"
        assert not [task for task in asyncio.all_tasks(loop) if not task.done()]
        loop.close()
        asyncio.set_event_loop(None)

        release.set()
        assert worker_finished.wait(5)
        assert cleanup_finished.wait(5)
        assert cleanup_calls == ["cleanup"]
        assert owner.state == "cleanup_attempted"
    finally:
        release.set()
        if not loop.is_closed():
            loop.close()
        asyncio.set_event_loop(None)
