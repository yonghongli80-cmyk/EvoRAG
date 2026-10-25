"""Complexity regression for the real staging audit SQLite load path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import rag.staging_audit as staging_audit


class _CountingRows(list[dict]):
    """Retain the actual loaded rows while counting complete list traversals."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__(rows)
        self.iterations = 0
        self.row_visits = 0

    def __iter__(self):
        self.iterations += 1
        for row in super().__iter__():
            self.row_visits += 1
            yield row


def _create_snapshot(path: Path, version_count: int) -> int:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE document_permissions (
                id INTEGER PRIMARY KEY, doc_name TEXT NOT NULL, kb_id TEXT NOT NULL,
                current_version_id INTEGER
            );
            CREATE TABLE document_versions (
                id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, version_no INTEGER NOT NULL,
                original_filename TEXT NOT NULL, file_path TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE document_index_jobs (
                id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL, job_scope TEXT,
                job_type TEXT, status TEXT NOT NULL
            );
            """
        )
        job_count = 0
        for offset in range(version_count):
            document_id = offset + 1
            version_id = offset + 1001
            conn.execute(
                "INSERT INTO document_permissions VALUES (?, ?, ?, ?)",
                (document_id, f"doc-{document_id}", "kb-complexity", version_id),
            )
            conn.execute(
                "INSERT INTO document_versions VALUES (?, ?, 1, ?, ?, 'staging')",
                (version_id, document_id, f"doc-{document_id}.txt", f"{document_id}/1/file.txt"),
            )
            # One active and one terminal material row per version; IDs and insertion order
            # are intentionally observable in the report.
            conn.executemany(
                "INSERT INTO document_index_jobs VALUES (?, ?, 'material', 'material_index', ?)",
                ((job_count + 1, version_id, "queued"), (job_count + 2, version_id, "done")),
            )
            job_count += 2
            # Valid nonmaterial row for a known version and a valid orphan row both remain
            # part of the fully validated global jobs list.
            conn.execute(
                "INSERT INTO document_index_jobs VALUES (?, ?, 'other', 'material_index', 'done')",
                (job_count + 1, version_id),
            )
            job_count += 1
        conn.execute(
            "INSERT INTO document_index_jobs VALUES (?, ?, 'material', 'material_index', 'done')",
            (job_count + 1, 999999),
        )
        job_count += 1
        conn.commit()
        return job_count
    finally:
        conn.close()


@pytest.mark.parametrize("version_count", [1, 12, 48])
def test_audit_loads_and_traverses_global_jobs_only_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version_count: int
) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    expected_job_count = _create_snapshot(db_path, version_count)
    real_select_rows = staging_audit._select_rows
    counted_jobs: list[_CountingRows] = []

    def counting_select_rows(conn: sqlite3.Connection, table: str, columns: set[str]) -> list[dict]:
        rows = real_select_rows(conn, table, columns)
        if table == "document_index_jobs":
            counted = _CountingRows(rows)
            counted_jobs.append(counted)
            return counted
        return rows

    monkeypatch.setattr(staging_audit, "_select_rows", counting_select_rows)

    report = staging_audit.audit_staging(db_path, versions_root)

    assert "error" not in report
    assert len(counted_jobs) == 1
    jobs = counted_jobs[0]
    assert len(jobs) == expected_job_count
    assert jobs.iterations == 2
    assert jobs.row_visits == 2 * expected_job_count
    version_entries = [entry for entry in report["entries"] if entry["kind"] == "document_version"]
    assert len(version_entries) == version_count
    assert all(
        entry["material_job"] == {"state": "active", "statuses": ["queued", "done"]} for entry in version_entries
    )
