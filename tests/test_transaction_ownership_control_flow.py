from __future__ import annotations

from pathlib import Path

import pytest

from scripts.audit_transaction_ownership import scan_tree


def _scan_source(tmp_path: Path, source: str) -> dict:
    rag = tmp_path / "rag"
    scripts = tmp_path / "scripts"
    rag.mkdir()
    scripts.mkdir()
    (rag / "fixture.py").write_text(source, encoding="utf-8")
    return scan_tree(rag, scripts)


@pytest.mark.parametrize(
    "body",
    [
        "if flag:\n        conn = sqlite3.connect('x')\n    else:\n        conn = manager.get_write_connection()",
        "conn = manager.get_write_connection()\n    for item in items:\n        conn = sqlite3.connect('x')",
        "conn = manager.get_write_connection()\n    try:\n        risky()\n        conn = sqlite3.connect('x')\n    except Exception:\n        pass",
        "conn = manager.get_write_connection()\n    with manager.transaction():\n        conn = sqlite3.connect('x')",
    ],
    ids=["different-branches", "zero-loop", "exception-path", "with-rebinding"],
)
def test_uncertain_connection_rebinding_remains_a_sink_with_unknown_hint(tmp_path: Path, body: str) -> None:
    report = _scan_source(
        tmp_path,
        "import sqlite3\ndef write(flag, items, manager):\n    " + body + "\n    conn.execute('DELETE FROM items')\n",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 1
    assert sinks[0]["connection_hint"] == "unknown"
    assert report["writer_completeness"] == "not_proven"


def test_class_import_and_hint_do_not_leak_to_module_or_method(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
class Repo:
    import sqlite3 as sql
    conn = sql.connect('x')
    def write(self):
        conn.execute('DELETE FROM items')
def outside():
    connection = sql.connect('x')
    connection.execute('DELETE FROM items')
""",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 2
    assert all(c["connection_hint"] == "unknown" for c in sinks)
    assert not any(c["owner"] == "outside" and c["marker"] == "sqlite_connect" for c in report["candidates"])


def test_deterministic_aliases_and_matching_branches_keep_connection_hint(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
import sqlite3
def write(flag):
    original = sqlite3.connect('x')
    alias = original
    alias.execute('UPDATE items SET value = 1')
    if flag:
        conn = sqlite3.connect('x')
    else:
        conn = sqlite3.connect('y')
    conn.execute('DELETE FROM items')
""",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 2
    assert all(c["connection_hint"] == "private_sqlite_connection_candidate" for c in sinks)


def test_scanned_source_with_side_effect_and_raise_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    report = _scan_source(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text('executed')
raise RuntimeError('must not run')
def write(conn):
    conn.execute('DELETE FROM items')
""",
    )
    assert not marker.exists()
    assert any(c["marker"] == "execute" and c["connection_hint"] == "unknown" for c in report["candidates"])


def test_loop_sink_and_exception_handler_do_not_assume_one_execution_path(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
import sqlite3
def write(manager, items, flag):
    conn = manager.get_write_connection()
    for item in items:
        conn.execute('UPDATE items SET value = 1')
        conn = sqlite3.connect('x')
    conn = manager.get_write_connection()
    try:
        if flag:
            conn = sqlite3.connect('x')
            risky()
    except Exception:
        conn.execute('DELETE FROM items')
""",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 2
    assert all(c["connection_hint"] == "unknown" for c in sinks)


def test_conditional_import_and_named_expression_do_not_leak_known_identity(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
def write(flag, manager):
    if flag:
        import sqlite3 as sql
    conn = sql.connect('x')
    conn.execute('DELETE FROM items')
    conn = manager.get_write_connection()
    if flag and (conn := unknown()):
        pass
    conn.execute('UPDATE items SET value = 1')
""",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 2
    assert all(c["connection_hint"] == "unknown" for c in sinks)


@pytest.mark.parametrize(
    "rebind",
    ["import os as sql", "from os import path as sql", "def sql():\n    pass", "class sql:\n    pass"],
    ids=["import", "from-import", "function", "class"],
)
def test_same_name_rebinding_invalidates_constructor_import(tmp_path: Path, rebind: str) -> None:
    report = _scan_source(
        tmp_path,
        f"""
import sqlite3 as sql
{rebind}
conn = sql.connect('x')
conn.execute('DELETE FROM items')
""",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 1
    assert sinks[0]["connection_hint"] == "unknown"
    assert not any(c["marker"] == "sqlite_connect" for c in report["candidates"])


@pytest.mark.parametrize("jump", ["break", "continue"])
def test_loop_early_exit_and_else_do_not_restore_unreachable_identity(tmp_path: Path, jump: str) -> None:
    else_body = "conn = sqlite3.connect('else')" if jump == "break" else "pass"
    report = _scan_source(
        tmp_path,
        f"""
import sqlite3
import psycopg
def write(items):
    conn = sqlite3.connect('initial')
    for item in items:
        conn = psycopg.connect('external')
        {jump}
        conn = sqlite3.connect('unreachable')
    else:
        {else_body}
    conn.execute('DELETE FROM items')
""",
    )
    sink = next(c for c in report["candidates"] if c["marker"] == "execute")
    assert sink["connection_hint"] == "unknown"


def test_with_suppressed_exception_merges_intermediate_state(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
import sqlite3
import psycopg
from contextlib import suppress
def write():
    conn = sqlite3.connect('initial')
    with suppress(Exception):
        conn = psycopg.connect('external')
        raise ValueError()
        conn = sqlite3.connect('unreachable')
    conn.execute('DELETE FROM items')
""",
    )
    sink = next(c for c in report["candidates"] if c["marker"] == "execute")
    assert sink["connection_hint"] == "unknown"


def test_with_sink_before_rebinding_keeps_context_hint(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
def write(manager):
    with manager.transaction() as conn:
        conn.execute('UPDATE items SET value = 1')
        conn = unknown()
    conn.execute('DELETE FROM items')
""",
    )
    sinks = [c for c in report["candidates"] if c["marker"] == "execute"]
    assert len(sinks) == 2
    assert sinks[0]["connection_hint"] == "shared_transaction_candidate"
    assert sinks[1]["connection_hint"] == "unknown"


@pytest.mark.parametrize(
    "rebind",
    ["def db():\n        pass", "class db:\n        pass"],
    ids=["def", "class"],
)
def test_loop_definition_invalidates_import_on_later_iterations(tmp_path: Path, rebind: str) -> None:
    report = _scan_source(
        tmp_path,
        f"""
import sqlite3 as db
for item in items:
    conn = db.connect('x')
    conn.execute('DELETE FROM items')
    {rebind}
""",
    )
    sink = next(c for c in report["candidates"] if c["marker"] == "execute")
    assert sink["connection_hint"] == "unknown"


def test_loop_import_invalidates_connection_hint_on_later_iterations(tmp_path: Path) -> None:
    report = _scan_source(
        tmp_path,
        """
import sqlite3
def write(items):
    conn = sqlite3.connect('x')
    for item in items:
        conn.execute('DELETE FROM items')
        import os as conn
""",
    )
    sink = next(c for c in report["candidates"] if c["marker"] == "execute")
    assert sink["connection_hint"] == "unknown"


@pytest.mark.parametrize(
    "rebind",
    ["import os as db", "from os import path as db", "def db():\n        pass", "class db:\n        pass"],
    ids=["import", "from-import", "def", "class"],
)
def test_function_local_binding_shadows_outer_import_before_definition(tmp_path: Path, rebind: str) -> None:
    report = _scan_source(
        tmp_path,
        f"""
import sqlite3 as db
def write():
    conn = db.connect('x')
    conn.execute('DELETE FROM items')
    {rebind}
""",
    )
    sink = next(c for c in report["candidates"] if c["marker"] == "execute")
    assert sink["connection_hint"] == "unknown"
    assert not any(c["marker"] == "sqlite_connect" for c in report["candidates"])
