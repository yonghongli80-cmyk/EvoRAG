"""F-04: ``start.py`` must own the container's shutdown signal and forward it.

The container runs ``python start.py`` as PID 1, so a ``docker stop`` (SIGTERM) only
reaches ``start.py``.  Before this fix the child uvicorn process never saw the signal,
``proc.wait()`` was interrupted and resumed (POSIX restarts it), and the container was
eventually SIGKILLed after the 10s grace period — which skips the FastAPI lifespan
cleanup (worker shutdown, WAL checkpoint, temp files).

These tests pin the supervisor contract:

* SIGTERM/SIGINT are forwarded to the uvicorn child verbatim (POSIX) instead of being
  swallowed, and the parent returns the child's exit status.
* A child that ignores the signal is killed after ``RAG_SHUTDOWN_TIMEOUT`` seconds,
  not left hanging.
* A second signal means "stop now" (operator impatience), also a kill.
* Handler registration is reversible so a normal local ``Ctrl+C`` still behaves.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import start

ROOT = Path(__file__).resolve().parents[1]


class FakeProcess:
    """Stand-in for ``subprocess.Popen`` with the handful of methods we use."""

    def __init__(self, *, exits_on_signal: bool = False, exits_after_waits: int | None = None):
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.terminated = 0
        self.killed = 0
        self.wait_calls: list[float | None] = []
        #: None = the child never exits on its own (until killed).
        self._exits_on_signal = exits_on_signal
        self._remaining_waits = exits_after_waits

    def poll(self):
        return self.returncode

    def send_signal(self, signum):
        self.signals.append(signum)
        if self._exits_on_signal:
            self.returncode = 0

    def terminate(self):
        self.terminated += 1
        if self._exits_on_signal:
            self.returncode = 0

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if self._remaining_waits is not None:
            if self._remaining_waits <= 0:
                self.returncode = 0
                return self.returncode
            self._remaining_waits -= 1
        raise subprocess.TimeoutExpired("uvicorn", timeout)


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _forwarded(proc: FakeProcess) -> bool:
    """True when the child received the termination request on this platform."""
    return bool(proc.signals) or proc.terminated >= 1


def test_sigterm_is_forwarded_and_the_child_exit_code_is_returned():
    """Without the handler the uvicorn child would survive the container's SIGTERM."""
    proc = FakeProcess(exits_on_signal=True)
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=FakeClock())

    supervisor.handle(signal.SIGTERM)

    assert _forwarded(proc)
    assert proc.killed == 0
    assert supervisor.wait() == 0


def test_sigint_is_forwarded_so_local_ctrl_c_still_stops_uvicorn():
    proc = FakeProcess(exits_on_signal=True)
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=FakeClock())

    supervisor.handle(signal.SIGINT)

    assert _forwarded(proc)
    assert supervisor.wait() == 0


def test_child_that_ignores_the_signal_is_killed_after_the_grace_period():
    """A wedged uvicorn must not leave the container in 'stopping' until SIGKILL."""
    clock = FakeClock(0.0)
    proc = FakeProcess()  # ignores SIGTERM
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=clock)

    supervisor.handle(signal.SIGTERM)
    clock.advance(10.5)

    assert supervisor.wait() == -9
    assert proc.killed == 1


def test_no_kill_while_the_grace_period_is_still_running():
    clock = FakeClock(0.0)
    proc = FakeProcess(exits_after_waits=2)  # slow but honest graceful shutdown
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=clock)

    supervisor.handle(signal.SIGTERM)
    clock.advance(1.0)

    assert supervisor.wait() == 0
    assert proc.killed == 0


def test_second_signal_forces_an_immediate_kill():
    clock = FakeClock(0.0)
    proc = FakeProcess()
    supervisor = start.ShutdownSupervisor(proc, timeout=600.0, clock=clock)

    supervisor.handle(signal.SIGTERM)
    supervisor.handle(signal.SIGTERM)

    assert proc.killed == 1
    assert supervisor.wait() == -9


def test_handlers_are_installed_and_restored():
    previous = signal.getsignal(signal.SIGTERM)
    proc = FakeProcess()
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=FakeClock())

    assert supervisor.install() is True
    try:
        assert signal.getsignal(signal.SIGTERM) == supervisor.handle
        assert signal.getsignal(signal.SIGINT) == supervisor.handle
    finally:
        supervisor.uninstall()

    assert signal.getsignal(signal.SIGTERM) is previous


def test_failed_install_does_not_claim_success(monkeypatch):
    proc = FakeProcess()
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=FakeClock())

    def refuse(*_args, **_kwargs):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(start.signal, "signal", refuse)

    assert supervisor.install() is False


def _supervised_process(proc: FakeProcess, monkeypatch, *, exits_when_forwarded: bool):
    """Make ``main()`` supervise ``proc`` and deliver one SIGTERM while it waits."""
    original_wait = proc.wait
    delivered = {"done": False}

    def wait_and_deliver_signal(timeout=None):
        if not delivered["done"]:
            delivered["done"] = True
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler), "start.py must install a SIGTERM handler while supervising"
            handler(signal.SIGTERM, None)
        return original_wait(timeout)

    def forward(signum=None):
        # POSIX forwards the original signal; Windows has no SIGTERM delivery primitive,
        # so start.py falls back to terminate().  Both count as "forwarded".
        if signum is None:
            proc.terminated += 1
        else:
            proc.signals.append(signum)
        if exits_when_forwarded:
            proc.returncode = 0

    proc.wait = wait_and_deliver_signal  # type: ignore[method-assign]
    proc.send_signal = forward  # type: ignore[method-assign]
    proc.terminate = forward  # type: ignore[method-assign]

    monkeypatch.setattr(start.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(start.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(start.webbrowser, "open", lambda *_args, **_kwargs: None)
    return proc


def test_main_installs_a_handler_that_forwards_sigterm_to_uvicorn(monkeypatch, capsys):
    """End-to-end contract: the signal the container sends reaches the child."""
    port = _free_port()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    monkeypatch.setenv("RAG_START_HEALTH_ATTEMPTS", "1")

    proc = _supervised_process(FakeProcess(), monkeypatch, exits_when_forwarded=True)

    result = start.main()

    assert _forwarded(proc)
    assert result == 0
    assert capsys.readouterr().out  # banner still printed


def test_main_kills_a_wedged_child_when_the_shutdown_timeout_expires(monkeypatch):
    port = _free_port()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    monkeypatch.setenv("RAG_START_HEALTH_ATTEMPTS", "1")
    monkeypatch.setenv("RAG_SHUTDOWN_TIMEOUT", "0")

    proc = _supervised_process(FakeProcess(), monkeypatch, exits_when_forwarded=False)

    result = start.main()

    assert proc.killed == 1
    assert result == 137  # 128 + SIGKILL, the status a shell reports


def test_main_reports_a_startup_timeout_without_claiming_ready(monkeypatch, capsys):
    """F-06: an unresponsive /health must never print the '服务已就绪' banner."""
    port = _free_port()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)
    monkeypatch.setenv("RAG_START_HEALTH_ATTEMPTS", "2")

    proc = FakeProcess(exits_after_waits=1)  # alive, but /health never answers
    monkeypatch.setattr(start.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("not ready")),
    )
    monkeypatch.setattr(start.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(start.webbrowser, "open", lambda *_args, **_kwargs: None)

    result = start.main()

    output = capsys.readouterr().out
    assert result == 0
    assert "服务已就绪" not in output
    assert "超时" in output


def test_wait_until_ready_stops_as_soon_as_uvicorn_exits(monkeypatch):
    proc = FakeProcess()
    proc.returncode = 3

    ready = start._wait_until_ready(proc, port=1, attempts=5, interval=0.0)

    assert ready is False


def test_wait_until_ready_retries_until_health_answers(monkeypatch):
    attempts = {"count": 0}

    def flaky(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise urllib.error.URLError("not ready")
        return object()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr(start.time, "sleep", lambda _seconds: None)

    ready = start._wait_until_ready(FakeProcess(), port=1, attempts=5, interval=0.0)

    assert ready is True
    assert attempts["count"] == 3


def test_shutdown_timeout_env_is_parsed_defensively(monkeypatch):
    monkeypatch.setenv("RAG_SHUTDOWN_TIMEOUT", "2.5")
    assert start._shutdown_timeout() == pytest.approx(2.5)

    monkeypatch.setenv("RAG_SHUTDOWN_TIMEOUT", "not-a-number")
    assert start._shutdown_timeout() == start.DEFAULT_SHUTDOWN_TIMEOUT

    monkeypatch.delenv("RAG_SHUTDOWN_TIMEOUT")
    assert start._shutdown_timeout() == start.DEFAULT_SHUTDOWN_TIMEOUT


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_health_probe_uses_the_documented_path(monkeypatch):
    seen: list[str] = []

    def record(url, *_args, **_kwargs):
        seen.append(url)
        return object()

    monkeypatch.setattr(urllib.request, "urlopen", record)

    assert start._wait_until_ready(FakeProcess(), port=8123, attempts=1, interval=0.0) is True
    assert seen == [f"http://127.0.0.1:8123{start.READINESS_PATH}"]


def test_supervision_waits_with_a_bounded_probe_interval():
    """The loop must stay responsive to signals, not block forever inside wait()."""

    class OneProbeProcess(FakeProcess):
        def wait(self, timeout=None):
            assert timeout is not None and timeout <= 1.0
            return 0

    supervisor = start.ShutdownSupervisor(OneProbeProcess(), timeout=10.0, clock=FakeClock())
    assert supervisor.wait() == 0


def test_exit_status_is_normalised_for_signals():
    assert start._exit_status(0) == 0
    assert start._exit_status(1) == 1
    assert start._exit_status(-15) == 143
    assert start._exit_status(None) == 1


def test_sleep_loop_does_not_busy_spin(monkeypatch):
    """Guard against a refactor that removes the inter-attempt sleep."""
    slept: list[float] = []
    monkeypatch.setattr(start.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("no")),
    )

    assert start._wait_until_ready(FakeProcess(), port=1, attempts=3, interval=0.5) is False
    assert slept == [0.5, 0.5, 0.5]
    assert all(value > 0 for value in slept)


@pytest.mark.parametrize("attempts", [0, 1])
def test_zero_attempt_budget_is_not_ready(monkeypatch, attempts):
    monkeypatch.setattr(start.time, "sleep", lambda _seconds: None)
    assert start._wait_until_ready(FakeProcess(), port=1, attempts=attempts, interval=0.0) is False


def test_supervisor_poll_interval_is_bounded():
    assert 0 < start.SHUTDOWN_POLL_INTERVAL <= 1.0


def test_terminated_child_does_not_receive_another_signal():
    proc = FakeProcess()
    proc.returncode = 0
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0, clock=FakeClock())

    supervisor.handle(signal.SIGTERM)

    assert proc.signals == []
    assert proc.terminated == 0
    assert proc.killed == 0


def test_stop_requested_exposes_the_first_signal_time():
    clock = FakeClock(7.0)
    supervisor = start.ShutdownSupervisor(FakeProcess(), timeout=10.0, clock=clock)

    assert supervisor.stop_requested_at is None
    supervisor.handle(signal.SIGTERM)
    assert supervisor.stop_requested_at == 7.0
    clock.advance(3.0)
    supervisor.handle(signal.SIGTERM)
    assert supervisor.stop_requested_at == 7.0  # first request wins


def test_forwarding_terminates_a_real_child_process():
    """Integration check (no fakes): a sleep child dies when the handler forwards a stop.

    This is the end-to-end shape of the F-04 bug: the handler must actually reach a real
    ``Popen``, not only the fake used above.
    """
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=os.name == "posix",
    )
    supervisor = start.ShutdownSupervisor(child, timeout=5.0)
    try:
        supervisor.handle(signal.SIGTERM)
        returncode = supervisor.wait()
        assert returncode is not None
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.skipif(os.name != "posix", reason="Windows terminate() cannot be ignored")
def test_a_real_child_that_ignores_sigterm_is_killed_after_the_grace_period():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    supervisor = start.ShutdownSupervisor(child, timeout=0.5)
    try:
        supervisor.handle(signal.SIGTERM)
        assert supervisor.wait() == -9
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_monotonic_clock_is_the_default():
    proc = FakeProcess()
    supervisor = start.ShutdownSupervisor(proc, timeout=10.0)

    before = time.monotonic()
    supervisor.handle(signal.SIGTERM)
    after = time.monotonic()

    assert supervisor.stop_requested_at is not None
    assert before <= supervisor.stop_requested_at <= after


def test_container_command_stays_exec_form_and_names_its_stop_signal():
    """The image must not hide start.py behind a shell (F-04 root cause).

    ``CMD ["python", "-u", "start.py"]`` is the exec form: PID 1 is the Python supervisor
    itself, so ``docker stop`` (SIGTERM) reaches the code that forwards it. A shell-form
    ``CMD python start.py`` would put ``/bin/sh`` in PID 1 and swallow the signal.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["python", "-u", "start.py"]' in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
