from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import rag.staging_audit as staging_audit
from rag.spreadsheets.models import ColumnType, SheetVisibility
from rag.spreadsheets.parser import (
    ParsedCell,
    ParsedColumn,
    ParsedRow,
    ParsedSheet,
    ParsedWorkbook,
)
from rag.spreadsheets.store import write_dataset


def _quote_for_fixture(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _manual_workbook(sheet_key: str, column_key: str, value: str) -> ParsedWorkbook:
    column = ParsedColumn(
        column_key=column_key,
        ordinal=1,
        display_name="fixture value",
        normalized_name="fixture_value",
        inferred_type=ColumnType.TEXT,
        number_format="",
    )
    sheet = ParsedSheet(
        sheet_key=sheet_key,
        name="fixture",
        ordinal=1,
        visibility=SheetVisibility.VISIBLE,
        header_row=1,
        first_data_row=2,
        headers=("fixture value",),
        columns=(column,),
        rows=(ParsedRow(row_number=2, cells=(ParsedCell(value),)),),
        formula_cells=(),
        merged_ranges=(),
    )
    return ParsedWorkbook(format="synthetic", sheets=(sheet,))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def test_store_quotes_actual_quoted_table_and_column_and_preserves_values(tmp_path):
    target = tmp_path / "dataset.sqlite3"
    table_name = 'sheet "alpha"'
    column_name = 'metric "snow 雪"'
    stored_value = "stored value differs from both names"

    manifest = write_dataset(target, _manual_workbook(table_name, column_name, stored_value))

    assert manifest.sheets[0].table_name == table_name
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            f"SELECT {_quote_for_fixture(column_name)} FROM {_quote_for_fixture(table_name)}"
        ).fetchone() == (stored_value,)
        assert connection.execute(
            "SELECT table_name FROM sheet_manifest WHERE sheet_key = ?",
            (table_name,),
        ).fetchone() == (table_name,)
    assert not list(tmp_path.glob("dataset.sqlite3.*.staging"))


def test_store_keeps_staging_atomic_when_identifier_contains_nul(tmp_path):
    target = tmp_path / "dataset.sqlite3"

    try:
        write_dataset(target, _manual_workbook("sheet\x00bad", "c_0001", "value"))
    except (TypeError, ValueError, sqlite3.DatabaseError):
        pass
    else:
        raise AssertionError("invalid SQLite identifier unexpectedly persisted")

    assert not target.exists()
    assert not list(tmp_path.glob("dataset.sqlite3.*.staging"))


def test_store_nul_failure_preserves_existing_dataset_bytes_and_rows(tmp_path):
    target = tmp_path / "dataset.sqlite3"
    write_dataset(target, _manual_workbook("sheet_0001", "c_0001", "old-value"))
    before = _file_sha256(target)
    with sqlite3.connect(target) as connection:
        before_rows = connection.execute("SELECT __row_number, c_0001 FROM sheet_0001").fetchall()

    try:
        write_dataset(target, _manual_workbook("sheet\x00bad", "c_0001", "new-value"))
    except (TypeError, ValueError, sqlite3.DatabaseError):
        pass
    else:
        raise AssertionError("invalid SQLite identifier unexpectedly persisted")

    assert _file_sha256(target) == before
    with sqlite3.connect(target) as connection:
        assert (
            connection.execute("SELECT __row_number, c_0001 FROM sheet_0001").fetchall()
            == before_rows
            == [(2, "old-value")]
        )
    assert not list(tmp_path.glob("dataset.sqlite3.*.staging"))


def _seed_api_dataset(client: TestClient, owner: dict, tmp_path: Path):
    db = client.app.state.user_db
    document_id = db.create_document_permission("quoted.xlsx", "rag_docs", owner["id"])
    version_id = db.create_document_version(document_id, "quoted.xlsx", "source.xlsx", "hash", owner["id"])
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    target = Path(client.app.state.structured_datasets_root) / str(document_id) / str(version_id) / "dataset.sqlite3"
    target.parent.mkdir(parents=True, exist_ok=True)
    table_name = 'actual "table" 雪'
    column_name = 'actual "column" name'
    stored_values = {
        1: "stored row one differs from identifier",
        3: "stored row three differs from identifier",
        5: "stored row five differs from identifier",
    }
    with sqlite3.connect(target) as connection:
        connection.executescript(
            """
            CREATE TABLE sheet_manifest (
                sheet_key TEXT PRIMARY KEY,
                table_name TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                visibility TEXT NOT NULL,
                header_row INTEGER,
                first_data_row INTEGER,
                row_count INTEGER NOT NULL,
                column_count INTEGER NOT NULL
            );
            CREATE TABLE column_manifest (
                sheet_key TEXT NOT NULL,
                column_key TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                inferred_type TEXT NOT NULL,
                number_format TEXT NOT NULL,
                PRIMARY KEY(sheet_key, column_key)
            );
            CREATE TABLE formula_cells (
                sheet_key TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                column_key TEXT NOT NULL,
                formula TEXT NOT NULL,
                cached_value
            );
            CREATE TABLE merged_ranges (sheet_key TEXT, range_ref TEXT);
            """
        )
        connection.execute(
            f"CREATE TABLE {_quote_for_fixture(table_name)} "
            f"(__row_number INTEGER NOT NULL, "
            f"{_quote_for_fixture(column_name)} TEXT)"
        )
        connection.executemany(
            f"INSERT INTO {_quote_for_fixture(table_name)} VALUES (?, ?)",
            [(row_number, stored_values[row_number]) for row_number in (5, 1, 3)],
        )
        connection.execute(
            "INSERT INTO sheet_manifest VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sheet_0001", table_name, "quoted", 1, "visible", 1, 1, 3, 1),
        )
        connection.execute(
            "INSERT INTO column_manifest VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "sheet_0001",
                column_name,
                1,
                "Actual column",
                "actual_column",
                "text",
                "",
            ),
        )
    db.activate_document_and_dataset(
        document_id=document_id,
        version_id=version_id,
        dataset_id=dataset_id,
        kb_id="rag_docs",
        filename="quoted.xlsx",
    )
    return document_id, version_id, table_name, column_name, stored_values


def test_rows_reads_quoted_manifest_identifiers_and_keeps_pagination_contract(isolated_api, make_user, tmp_path):
    isolated_api._transport.raise_server_exceptions = False
    owner, headers = make_user("quoted-identifiers-owner")
    document_id, version_id, table_name, column_name, stored_values = _seed_api_dataset(isolated_api, owner, tmp_path)

    endpoint = f"/documents/{document_id}/versions/{version_id}/sheets/sheet_0001/rows"
    response = isolated_api.get(endpoint, params={"after_row": 0, "limit": 2}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == [
        {"row_number": 1, "values": [stored_values[1]]},
        {"row_number": 3, "values": [stored_values[3]]},
    ]
    assert body["columns"] == [
        {
            "column_key": column_name,
            "ordinal": 1,
            "display_name": "Actual column",
            "value_type": "text",
        }
    ]
    assert body["can_edit"] is True
    assert table_name not in response.text
    assert "sqlite" not in response.text.lower()

    cursor_page = isolated_api.get(endpoint, params={"after_row": 1, "limit": 1}, headers=headers)
    assert cursor_page.status_code == 200
    assert cursor_page.json()["rows"] == [{"row_number": 3, "values": [stored_values[3]]}]

    last_page = isolated_api.get(endpoint, params={"after_row": 3, "limit": 1}, headers=headers)
    assert last_page.status_code == 200
    assert last_page.json()["rows"] == [{"row_number": 5, "values": [stored_values[5]]}]

    empty_page = isolated_api.get(endpoint, params={"after_row": 5, "limit": 2}, headers=headers)
    assert empty_page.status_code == 200
    assert empty_page.json()["rows"] == []


def _create_minimal_snapshot(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE document_permissions (
                id INTEGER PRIMARY KEY,
                doc_name TEXT NOT NULL,
                kb_id TEXT NOT NULL,
                owner_id INTEGER,
                current_version_id INTEGER
            );
            CREATE TABLE document_versions (
                id INTEGER PRIMARY KEY,
                document_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content_hash TEXT,
                status TEXT NOT NULL,
                "extra ""quoted"" 雪" TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, status, "
            '"extra ""quoted"" 雪") VALUES (?, ?, ?, ?, ?, ?, ?)',
            (1, 11, 1, "source.xlsx", "legacy/source.xlsx", "staging", "extra-value"),
        )


def test_staging_audit_reads_quoted_extra_schema_column_without_dropping_it(monkeypatch, tmp_path):
    database = tmp_path / "snapshot.sqlite3"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_minimal_snapshot(database)
    before = _file_sha256(database)
    captured: dict[str, object] = {}
    original_diagnose = staging_audit._diagnose_versions

    def capture_versions(report, root, snapshot):
        captured["versions"] = snapshot["versions"]
        return original_diagnose(report, root, snapshot)

    monkeypatch.setattr(staging_audit, "_diagnose_versions", capture_versions)

    report = staging_audit.audit_staging(database, versions_root)

    assert "error" not in report
    assert captured["versions"][0]['extra "quoted" 雪'] == "extra-value"
    assert _file_sha256(database) == before
