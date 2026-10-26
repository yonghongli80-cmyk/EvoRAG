from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import audit_transaction_ownership


def _write_fixture_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    rag_root = tmp_path / "rag"
    scripts_root = tmp_path / "scripts"
    (rag_root / "repositories").mkdir(parents=True)
    (rag_root / "data_sources").mkdir(parents=True)
    scripts_root.mkdir()

    app_source = rag_root / "repositories" / "sample.py"
    app_source.write_text(
        "import sqlite3 as sql\n"
        "from sqlite3 import connect as open_db\n"
        "\n"
        "class Repo:\n"
        "    def write(self, manager, item_id):\n"
        "        with manager.locked_transaction(immediate=True) as conn:\n"
        "            with manager.transaction() as nested:\n"
        "                nested.execute('UPDATE items SET value = ? WHERE id = ?', (1, item_id))\n"
        "                nested.commit()\n"
        "        conn.rollback()\n"
        "        manager.get_write_connection().execute('DELETE FROM items')\n"
        "\n"
        "def private_store(path):\n"
        "    connection = open_db(path)\n"
        "    connection.execute('DELETE FROM cache')\n"
        "    connection.commit()\n"
        "\n"
        "def private_store_alias(path):\n"
        "    connection = sql.connect(path)\n"
        "    connection.execute('UPDATE cache SET value = 1')\n"
        "\n"
        "def private_store_annotated(path):\n"
        "    connection: sql.Connection = open_db(path)\n"
        "    connection.execute('SELECT 1')\n"
        "\n"
        "def shadowed_module_alias(sql):\n"
        "    connection = sql.connect('ignored')\n"
        "    connection.execute('SELECT 1')\n"
        "\n"
        "def shadowed_function_alias(open_db):\n"
        "    connection = open_db('ignored')\n"
        "    connection.execute('SELECT 1')\n"
        "\n"
        "def unknown_dispatch(factory):\n"
        "    handle = factory().writer_alias()\n"
        "    handle.execute('UPDATE items SET value = 1')\n",
        encoding="utf-8",
    )
    external_source = rag_root / "data_sources" / "external_db.py"
    external_source.write_text(
        "import psycopg2 as pg\n"
        "\n"
        "def fetch(connection_string, query):\n"
        "    connection = pg.connect(connection_string)\n"
        "    cursor = connection.cursor()\n"
        "    cursor.execute(query)\n"
        "    connection.commit()\n",
        encoding="utf-8",
    )
    script_source = scripts_root / "maintenance.py"
    script_source.write_text(
        "def maintenance(db):\n"
        "    with db.write_connection() as connection:\n"
        "        connection.execute('DELETE FROM expired')\n"
        "        connection.commit()\n",
        encoding="utf-8",
    )
    rebound_source = rag_root / "repositories" / "rebound.py"
    rebound_source.write_text(
        "import sqlite3 as sql\n"
        "sql = object()\n"
        "def after_module_rebind(path):\n"
        "    connection = sql.connect(path)\n"
        "    connection.execute('SELECT 1')\n",
        encoding="utf-8",
    )
    return rag_root, scripts_root, app_source


def _scan(tmp_path: Path) -> dict:
    rag_root, scripts_root, _ = _write_fixture_roots(tmp_path)
    return audit_transaction_ownership.scan_tree(rag_root, scripts_root)


def test_inventory_records_transaction_contexts_direct_commits_and_connection_access(
    tmp_path: Path,
) -> None:
    report = _scan(tmp_path)
    app_candidates = [
        candidate for candidate in report["candidates"] if candidate["path"] == "rag/repositories/sample.py"
    ]
    markers = {candidate["marker"] for candidate in app_candidates}

    expected_markers = {
        "locked_transaction",
        "transaction",
        "execute",
        "commit",
        "rollback",
        "get_write_connection",
        "sqlite_connect",
    }
    if not expected_markers <= markers:
        details = [
            (candidate["line"], candidate["marker"], candidate["owner"], candidate["connection_hint"])
            for candidate in app_candidates
            if candidate["owner"].startswith("private_")
        ]
        assert expected_markers <= markers, details
    assert all(candidate["owner"] for candidate in app_candidates)
    assert all("line" in candidate and candidate["line"] > 0 for candidate in app_candidates)


def test_nested_context_is_reported_as_lexical_scope_not_proven_atomicity(
    tmp_path: Path,
) -> None:
    report = _scan(tmp_path)
    nested_write = next(
        candidate
        for candidate in report["candidates"]
        if candidate["path"] == "rag/repositories/sample.py"
        and candidate["marker"] == "execute"
        and candidate["owner"] == "Repo.write"
    )

    assert nested_write["lexical_scope"] == ["locked_transaction", "transaction"]
    assert nested_write["transaction_ownership"] == "candidate_only"
    assert report["writer_completeness"] == "not_proven"


def test_unknown_connection_alias_is_preserved_as_unknown_writer_candidate(
    tmp_path: Path,
) -> None:
    report = _scan(tmp_path)
    unknown = next(
        candidate
        for candidate in report["candidates"]
        if candidate["owner"] == "unknown_dispatch" and candidate["marker"] == "execute"
    )

    assert unknown["connection_hint"] == "unknown"
    assert unknown["boundary"] == "unknown"
    assert unknown["transaction_ownership"] == "unknown"


def test_shadowed_or_rebound_import_aliases_are_not_promoted_to_known_connections(
    tmp_path: Path,
) -> None:
    report = _scan(tmp_path)
    candidates = report["candidates"]
    owners = {"shadowed_module_alias", "shadowed_function_alias", "after_module_rebind"}
    suspicious = [candidate for candidate in candidates if candidate["owner"] in owners]

    assert not any(candidate["marker"] == "sqlite_connect" for candidate in suspicious)
    sql_calls = [candidate for candidate in suspicious if candidate["marker"] == "execute"]
    assert len(sql_calls) == 3
    assert all(candidate["connection_hint"] == "unknown" for candidate in sql_calls)
    assert all(candidate["boundary"] == "unknown" for candidate in sql_calls)


def test_private_and_external_connections_have_separate_boundaries(
    tmp_path: Path,
) -> None:
    report = _scan(tmp_path)
    private = [
        candidate
        for candidate in report["candidates"]
        if candidate["owner"]
        in {
            "private_store",
            "private_store_alias",
            "private_store_annotated",
        }
    ]
    external = [
        candidate for candidate in report["candidates"] if candidate["path"] == "rag/data_sources/external_db.py"
    ]

    assert private
    assert all(candidate["boundary"] == "private_store" for candidate in private)
    assert any(candidate["owner"] == "private_store_alias" for candidate in private)
    assert any(
        candidate["owner"] == "private_store_annotated" and candidate["marker"] == "sqlite_connect"
        for candidate in private
    )
    assert external
    assert all(candidate["boundary"] == "external_datasource" for candidate in external)
    assert all(candidate["transaction_ownership"] != "manager_outer_commit" for candidate in external)


def test_rag_and_scripts_candidates_keep_distinct_source_scopes(tmp_path: Path) -> None:
    report = _scan(tmp_path)

    assert report["source_scopes"] == ["rag", "scripts"]
    script_candidates = [candidate for candidate in report["candidates"] if candidate["path"].startswith("scripts/")]
    assert script_candidates
    assert all(candidate["boundary"] == "maintenance_or_unknown_script" for candidate in script_candidates)
    assert all(candidate["boundary"] != "diagnostic_fixture" for candidate in script_candidates)
    assert any(candidate["path"].startswith("rag/") for candidate in report["candidates"])
    assert report["writer_completeness"] == "not_proven"


def test_ast_scan_never_imports_or_executes_fixture_modules(tmp_path: Path) -> None:
    rag_root = tmp_path / "rag"
    scripts_root = tmp_path / "scripts"
    rag_root.mkdir()
    scripts_root.mkdir()
    marker = tmp_path / "module-was-executed"
    (rag_root / "dangerous.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "def write(conn):\n"
        "    conn.execute('DELETE FROM items')\n",
        encoding="utf-8",
    )

    audit_transaction_ownership.scan_tree(rag_root, scripts_root)

    assert not marker.exists()


def test_malformed_python_is_reported_and_does_not_claim_complete_parse_coverage(
    tmp_path: Path,
) -> None:
    rag_root = tmp_path / "rag"
    scripts_root = tmp_path / "scripts"
    rag_root.mkdir()
    scripts_root.mkdir()
    (rag_root / "broken.py").write_text("def write(:\n    pass\n", encoding="utf-8")

    report = audit_transaction_ownership.scan_tree(rag_root, scripts_root)

    assert report["errors"] == [{"path": "rag/broken.py", "line": 1, "kind": "python_syntax_error"}]
    assert report["coverage"]["parse_complete"] is False
    assert report["writer_completeness"] == "not_proven"


def test_candidate_output_is_stable_and_exclusive(tmp_path: Path) -> None:
    report = _scan(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    audit_transaction_ownership.write_report(report, first)
    audit_transaction_ownership.write_report(report, second)

    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text(encoding="utf-8")) == report
    before = first.read_bytes()
    with pytest.raises(FileExistsError):
        audit_transaction_ownership.write_report(report, first)
    assert first.read_bytes() == before
