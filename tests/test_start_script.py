"""Regression tests for the one-click startup process lifecycle."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
import webbrowser
from types import SimpleNamespace

import start
from rag.user_db import UserDB


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


def test_remote_binding_rejects_active_org_accounts_with_initial_password(tmp_path):
    db = UserDB(str(tmp_path / "remote.db"))
    try:
        user_id = db.create_user("demo001", "123456")
        with db.locked_transaction() as conn:
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES('P-001',?,'员工','专员')",
                (user_id,),
            )
        config = SimpleNamespace(
            auth_enabled=True,
            allow_initial_passwords_remote=False,
        )

        error = start._remote_deployment_error("0.0.0.0", config, db)

        assert "初始密码" in error
    finally:
        db.close()


def test_loopback_binding_allows_initial_password_for_local_use(tmp_path):
    db = UserDB(str(tmp_path / "local.db"))
    try:
        config = SimpleNamespace(
            auth_enabled=True,
            allow_initial_passwords_remote=False,
        )
        assert start._remote_deployment_error("127.0.0.1", config, db) is None
    finally:
        db.close()
