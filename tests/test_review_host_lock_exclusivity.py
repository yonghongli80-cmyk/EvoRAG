"""Kernel-backed lease ownership must survive metadata publication and cleanup races."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import rag.evolution.host_lock as host_lock_module
from rag.evolution.host_lock import HostLocked, host_measurement_lock


def _child_holds_lock(path, acquired, release):
    with host_measurement_lock(path):
        acquired.set()
        release.wait(10)


def test_empty_metadata_publication_is_protected_by_kernel_mutex(tmp_path, monkeypatch):
    lock_path = tmp_path / "host.lock"
    metadata_created = threading.Event()
    publish = threading.Event()
    first_inside = threading.Event()
    release_first = threading.Event()
    original = host_lock_module._take
    calls = 0

    def pause_first_publish(path, *args):
        nonlocal calls
        calls += 1
        if calls == 1:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                metadata_created.set()
                assert publish.wait(5)
                stream.write(json.dumps({"pid": os.getpid(), "taken_at": time.time()}))
            return
        original(path, *args)

    monkeypatch.setattr(host_lock_module, "_take", pause_first_publish)

    def first_holder():
        with host_measurement_lock(lock_path):
            first_inside.set()
            release_first.wait(5)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(first_holder)
        assert metadata_created.wait(5)
        try:
            with pytest.raises(HostLocked), host_measurement_lock(lock_path):
                pytest.fail("an empty lease file must not be stolen while its kernel lock is held")
            assert lock_path.exists()
        finally:
            publish.set()
            assert first_inside.wait(5)
            release_first.set()
        first.result(timeout=5)


def test_two_stale_takeover_contenders_never_share_or_delete_the_new_lease(tmp_path):
    lock_path = tmp_path / "host.lock"
    lock_path.write_text(
        json.dumps({"pid": 999_999_999, "taken_at": time.time() - 10_000}),
        encoding="utf-8",
    )
    start = threading.Barrier(3)
    release_holder = threading.Event()
    entered = threading.Event()
    refused = threading.Event()

    def contender():
        start.wait(5)
        try:
            with host_measurement_lock(lock_path):
                entered.set()
                release_holder.wait(5)
                return "acquired"
        except HostLocked:
            refused.set()
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(contender)
        second = pool.submit(contender)
        start.wait(5)
        assert entered.wait(5)
        assert refused.wait(5)
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
        assert holder.get("token")
        release_holder.set()
        outcomes = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(outcomes) == ["acquired", "refused"]
    assert not lock_path.exists()


def test_same_pid_cannot_steal_an_active_lease_as_ancient(tmp_path):
    lock_path = tmp_path / "host.lock"

    with host_measurement_lock(lock_path):
        original_owner = json.loads(lock_path.read_text(encoding="utf-8"))
        assert original_owner.get("token")
        stale = {**original_owner, "taken_at": time.time() - host_lock_module.STALE_AFTER_S - 1}
        lock_path.write_text(json.dumps(stale), encoding="utf-8")

        with pytest.raises(HostLocked), host_measurement_lock(lock_path):
            pytest.fail("same-process nested lease must not replace the active owner")

        assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == original_owner["token"]


def test_other_process_cannot_enter_while_kernel_lease_is_held(tmp_path):
    lock_path = tmp_path / "host.lock"
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    process = context.Process(target=_child_holds_lock, args=(str(lock_path), acquired, release))
    process.start()
    try:
        assert acquired.wait(10)
        with pytest.raises(HostLocked), host_measurement_lock(lock_path):
            pytest.fail("another process already holds the host")
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
