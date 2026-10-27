"""ConnectionManager shutdown and connection-publication boundaries."""

import sqlite3
import threading

import pytest

from rag.db.connection import ConnectionManager

_TIMEOUT = 5


def _join(thread):
    thread.join(_TIMEOUT)
    assert not thread.is_alive(), "worker did not finish"


def test_reader_configured_during_close_is_not_published_and_is_closed(tmp_path, monkeypatch):
    manager = ConnectionManager(tmp_path / "db.sqlite")
    configured = threading.Event()
    resume = threading.Event()
    connections = []
    outcomes = []
    configure = manager._configure_connection

    def pause_after_configuration(conn, *, is_writer):
        configure(conn, is_writer=is_writer)
        if not is_writer:
            connections.append(conn)
            configured.set()
            assert resume.wait(_TIMEOUT)

    monkeypatch.setattr(manager, "_configure_connection", pause_after_configuration)

    def reader():
        try:
            outcomes.append(manager.get_read_connection())
        except Exception as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert configured.wait(_TIMEOUT)
        manager.close()
    finally:
        resume.set()
        _join(thread)
        manager.close()

    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RuntimeError)
    assert str(outcomes[0]) == "ConnectionManager is closed"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_reader_configuration_failure_closes_unpublished_connection(tmp_path, monkeypatch):
    manager = ConnectionManager(tmp_path / "db.sqlite")
    connections = []

    def fail_configuration(conn, *, is_writer):
        connections.append(conn)
        raise ValueError("configuration failed")

    monkeypatch.setattr(manager, "_configure_connection", fail_configuration)
    try:
        with pytest.raises(ValueError, match="configuration failed"):
            manager.get_read_connection()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connections[0].execute("SELECT 1")
    finally:
        manager.close()


@pytest.mark.parametrize("method", ["write_connection", "transaction"])
def test_waiting_writer_rechecks_closed_after_acquiring_lock(tmp_path, method):
    manager = ConnectionManager(tmp_path / "db.sqlite")
    entered_lock = threading.Event()
    outcomes = []
    lock = manager._write_lock

    class ObservableLock:
        def __enter__(self):
            if threading.current_thread() is worker:
                entered_lock.set()
            return lock.__enter__()

        def __exit__(self, *args):
            return lock.__exit__(*args)

    manager._write_lock = ObservableLock()

    def write():
        try:
            with getattr(manager, method)() as conn:
                outcomes.append(conn)
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=write)
    with lock:
        worker.start()
        assert entered_lock.wait(_TIMEOUT)
        manager.close()
    _join(worker)
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RuntimeError)
    assert str(outcomes[0]) == "ConnectionManager is closed"


def test_reader_does_not_wait_for_active_writer(tmp_path):
    manager = ConnectionManager(tmp_path / "db.sqlite")
    finished = threading.Event()
    outcomes = []

    def read():
        try:
            outcomes.append(manager.get_read_connection().execute("SELECT 1").fetchone()[0])
        except Exception as exc:
            outcomes.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=read)
    try:
        with manager.write_connection():
            worker.start()
            assert finished.wait(_TIMEOUT)
        _join(worker)
        assert outcomes == [1]
    finally:
        _join(worker)
        manager.close()


def test_close_is_idempotent_and_closes_published_reader(tmp_path):
    manager = ConnectionManager(tmp_path / "db.sqlite")
    reader = manager.get_read_connection()
    manager.close()
    manager.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        reader.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="^ConnectionManager is closed$"):
        manager.get_read_connection()
