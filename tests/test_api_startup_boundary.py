"""Contract tests for the side-effect-free API startup implementation module."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_api_startup_import_does_not_open_storage_or_network_or_create_directories():
    child = r"""
import importlib
import builtins
import io
import os
import pathlib
import socket
import sqlite3
import sys

sys.dont_write_bytecode = True

def denied(*_args, **_kwargs):
    raise AssertionError("forbidden import-time side effect")

socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
sqlite3.connect = denied
os.mkdir = denied
os.makedirs = denied
io.open = denied
builtins.open = denied
pathlib.Path.mkdir = denied

class ForbiddenDependencyFinder:
    blocked = {"config", "rag.api", "rag.context", "rag.user_db"}

    def find_spec(self, fullname, _path=None, _target=None):
        if fullname in self.blocked:
            raise AssertionError(f"forbidden eager dependency: {fullname}")
        return None

sys.meta_path.insert(0, ForbiddenDependencyFinder())
module = importlib.import_module("rag.api_startup")
assert module is not None
assert not any(name in sys.modules for name in ("config", "rag.api", "rag.context", "rag.user_db"))
"""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            env[name] = value

    completed = subprocess.run(
        [sys.executable, "-B", "-c", child],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_api_startup_facades_pass_the_live_api_module(monkeypatch):
    import rag.api as api
    import rag.api_startup as startup

    names = (
        "_run_document_version_migration",
        "_run_material_metadata_migration",
        "_run_spreadsheet_migration",
        "_register_repo_document_version",
        "_run_legacy_kb_migration",
        "_run_staged_version_migration",
        "auto_index_on_startup",
    )
    seen = []
    sentinel = object()

    for name in names:

        def record(api_module, *args, _name=name):
            seen.append((_name, api_module, args))
            return sentinel

        monkeypatch.setattr(startup, name, record, raising=False)

    results = [
        api._run_document_version_migration(),
        api._run_material_metadata_migration(),
        api._run_spreadsheet_migration(),
        api._register_repo_document_version("fixture.txt", Path("fixture.txt")),
        api._run_legacy_kb_migration(),
        api._run_staged_version_migration(),
        api.auto_index_on_startup(),
    ]

    assert results == [sentinel] * len(names)
    assert [(name, module) for name, module, _args in seen] == [(name, api) for name in names]
    assert seen[3][2] == ("fixture.txt", Path("fixture.txt"))
