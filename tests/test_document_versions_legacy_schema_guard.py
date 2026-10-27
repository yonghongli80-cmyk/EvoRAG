"""Regression tests for safe legacy document_versions table rebuilds.

These tests use only synthetic in-memory SQLite schemas.  The legacy upgrader
currently rebuilds from a fixed column list, so the extra-column cases should
fail until it validates the complete table layout before changing connection or
schema state.
"""

import contextlib
import sqlite3

import pytest

from rag.repositories.schema import rebuild_document_versions_status

_LEGACY_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('staging','indexing','current','historical','failed','purged')),
    validity_status TEXT NOT NULL CHECK(validity_status IN
        ('valid','reported_outdated','confirmed_outdated')) DEFAULT 'valid',
    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    superseded_at REAL,
    restored_from_version_id INTEGER REFERENCES document_versions(id)
"""

_LEGACY_CONSTRAINTS = """
    , UNIQUE(document_id, version_no),
    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
    FOREIGN KEY(uploaded_by) REFERENCES users(id)
"""


def _connection_with_legacy_versions(
    extra_column="",
    supports_recycle_pending=False,
    mixed_ascii_case=False,
    reversed_column_order=False,
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE document_permissions(id INTEGER PRIMARY KEY);
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE version_status_probe(version_id INTEGER, status TEXT);
        CREATE TABLE document_version_children(
            id INTEGER PRIMARY KEY,
            version_id INTEGER NOT NULL REFERENCES document_versions(id)
        );
        """
    )
    columns = _LEGACY_COLUMNS
    if supports_recycle_pending:
        columns = columns.replace(
            "'staging','indexing','current','historical','failed','purged'",
            "'staging','indexing','current','historical','recycle_pending','failed','purged'",
        )
    column_definitions = [column.strip() for column in columns.split(",\n")]
    if mixed_ascii_case:
        replacement_names = {
            "id": "ID",
            "document_id": "DoCuMeNt_Id",
            "version_no": "VERSION_NO",
            "original_filename": "Original_Filename",
            "file_path": "FILE_PATH",
            "content_hash": "Content_Hash",
            "uploaded_by": "UPLOADED_BY",
            "status": "StAtUs",
            "validity_status": "VALIDITY_STATUS",
            "created_at": "Created_At",
            "superseded_at": "SUPERSEDED_AT",
            "restored_from_version_id": "Restored_From_Version_Id",
        }
        for index, definition in enumerate(column_definitions):
            name = definition.split(None, 1)[0]
            if name in replacement_names:
                column_definitions[index] = definition.replace(name, replacement_names[name], 1)
    if extra_column:
        column_definitions.append(extra_column)
    if reversed_column_order:
        column_definitions.reverse()
    columns = ",\n".join(column_definitions) + _LEGACY_CONSTRAINTS
    conn.execute(f"CREATE TABLE document_versions ({columns})")
    conn.execute("INSERT INTO document_permissions(id) VALUES (17)")
    conn.execute("INSERT INTO users(id) VALUES (23)")
    conn.execute(
        "INSERT INTO document_versions "
        "(id, document_id, version_no, original_filename, file_path, content_hash, "
        "uploaded_by, status, validity_status, created_at, superseded_at, "
        "restored_from_version_id) "
        "VALUES (41, 17, 2, 'report.pdf', '/synthetic/report.pdf', 'hash-41', "
        "23, 'current', 'valid', 1700000000.0, NULL, NULL)"
    )
    conn.execute("INSERT INTO document_version_children(id, version_id) VALUES (51, 41)")
    conn.execute("CREATE INDEX idx_test_document_versions_canonical ON document_versions(document_id, status)")
    conn.execute(
        "CREATE TRIGGER trg_test_document_versions_status "
        "AFTER UPDATE OF status ON document_versions "
        "BEGIN INSERT INTO version_status_probe(version_id, status) "
        "VALUES (NEW.id, NEW.status); END"
    )
    conn.commit()
    return conn


def _legacy_state(conn):
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_versions'"
    ).fetchone()[0]
    rows = [tuple(row) for row in conn.execute("SELECT * FROM document_versions")]
    objects = [
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name='document_versions' AND type IN ('index','trigger') "
            "AND sql IS NOT NULL ORDER BY type, name"
        )
    ]
    probe = [tuple(row) for row in conn.execute("SELECT * FROM version_status_probe")]
    return table_sql, rows, objects, probe, conn.execute("PRAGMA foreign_keys").fetchone()[0], conn.in_transaction


@pytest.mark.parametrize(
    "extra_column,secret_schema_detail",
    [
        ("business_note TEXT NOT NULL DEFAULT 'retained-value'", "business_note"),
        ("virtual_total INTEGER GENERATED ALWAYS AS (version_no + 100) VIRTUAL", "virtual_total"),
        ("stored_total INTEGER GENERATED ALWAYS AS (version_no + 200) STORED", "stored_total"),
    ],
    ids=["business-column", "virtual-generated-column", "stored-generated-column"],
)
def test_legacy_rebuild_rejects_unknown_columns_before_mutating_schema_or_connection(
    extra_column, secret_schema_detail
):
    with contextlib.closing(_connection_with_legacy_versions(extra_column)) as conn:
        before = _legacy_state(conn)

        with pytest.raises(RuntimeError) as error:
            rebuild_document_versions_status(conn)

        assert secret_schema_detail not in str(error.value)
        assert _legacy_state(conn) == before
        assert conn.execute("SELECT version_id FROM document_version_children").fetchone()[0] == 41
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "mixed_ascii_case,reversed_column_order",
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["canonical", "mixed-ascii-case", "reordered-columns", "mixed-case-reordered"],
)
def test_known_twelve_column_legacy_table_rebuilds_and_preserves_supported_objects_and_foreign_keys(
    mixed_ascii_case, reversed_column_order
):
    with contextlib.closing(
        _connection_with_legacy_versions(
            mixed_ascii_case=mixed_ascii_case,
            reversed_column_order=reversed_column_order,
        )
    ) as conn:
        rebuild_document_versions_status(conn)
        assert not conn.in_transaction

        version = conn.execute(
            "SELECT id, document_id, version_no, original_filename, file_path, content_hash, "
            "uploaded_by, status, validity_status, created_at, superseded_at, "
            "restored_from_version_id FROM document_versions WHERE id=41"
        ).fetchone()
        assert tuple(version) == (
            41,
            17,
            2,
            "report.pdf",
            "/synthetic/report.pdf",
            "hash-41",
            23,
            "current",
            "valid",
            1700000000.0,
            None,
            None,
        )
        conn.execute("UPDATE document_versions SET status='recycle_pending' WHERE id=41")
        assert conn.execute("SELECT status FROM document_versions WHERE id=41").fetchone()[0] == "recycle_pending"
        assert conn.execute("SELECT version_id FROM document_version_children").fetchone()[0] == 41
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_test_document_versions_canonical'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_test_document_versions_status'"
            ).fetchone()
            is not None
        )
        assert tuple(conn.execute("SELECT version_id, status FROM version_status_probe").fetchone()) == (
            41,
            "recycle_pending",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.commit()
        assert not conn.in_transaction


def test_table_already_supporting_recycle_pending_keeps_early_return_for_extension_columns():
    with contextlib.closing(
        _connection_with_legacy_versions(
            "business_note TEXT NOT NULL DEFAULT 'retained-value'",
            supports_recycle_pending=True,
        )
    ) as conn:
        conn.execute("UPDATE document_versions SET business_note='keep-me' WHERE id=41")
        conn.commit()
        before = _legacy_state(conn)

        rebuild_document_versions_status(conn)

        assert _legacy_state(conn) == before
        assert conn.execute("SELECT business_note FROM document_versions").fetchone()[0] == "keep-me"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.in_transaction
