"""Regression tests for the one-click startup process lifecycle."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
import webbrowser

import start


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_main_rejects_an_occupied_port_before_starting_uvicorn(monkeypatch, capsys):
    """Removing the preflight check would start a competing uvicorn process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setenv("PORT", str(port))
        monkeypatch.delenv("DOCKER_CONTAINER", raising=False)

        def fail_if_started(*_args, **_kwargs):
            raise AssertionError("uvicorn must not start while the target port is occupied")

        monkeypatch.setattr(start.subprocess, "Popen", fail_if_started)

        result = start.main()

    assert result == 1
    assert "端口" in capsys.readouterr().out


def test_main_does_not_report_ready_after_uvicorn_exits(monkeypatch, capsys):
    """Removing the child poll would let an old health endpoint cause a false ready message."""
    port = _unused_local_port()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.delenv("DOCKER_CONTAINER", raising=False)

    class ExitedProcess:
        returncode = 1

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    monkeypatch.setattr(start.subprocess, "Popen", lambda *_args, **_kwargs: ExitedProcess())
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("not ready")),
    )
    monkeypatch.setattr(start.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        webbrowser,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("browser must not open after uvicorn exits")
        ),
    )

    result = start.main()

    output = capsys.readouterr().out
    assert result == 1
    assert "服务已就绪" not in output
