"""Full-report compatibility guards for staging-audit job schema handling."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rag.staging_audit import audit_staging

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REPORTS = REPO_ROOT / "tests" / "fixtures" / "staging_audit_jobs_expected.json"


def _base_schema(conn: sqlite3.Connection) -> None:
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
        """
    )


def _seed_versions(conn: sqlite3.Connection, versions: list[tuple]) -> None:
    conn.executemany(
        "INSERT INTO document_permissions(id, doc_name, kb_id, current_version_id) VALUES (?, ?, ?, ?)",
        [(1, "synthetic-document", "kb-synthetic", versions[0][0] if versions else None)],
    )
    conn.executemany(
        "INSERT INTO document_versions(id, document_id, version_no, original_filename, file_path, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        versions,
    )


def _create_case(case: str, root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "snapshot.sqlite"
    versions_root = root / "versions"
    versions_root.mkdir()
    conn = sqlite3.connect(database)
    try:
        _base_schema(conn)
        if case == "jobs_table_missing":
            _seed_versions(conn, [(11, 1, 1, "one.txt", "1/1/one.txt", "staging")])
        elif case == "minimal_jobs_table":
            _seed_versions(
                conn,
                [
                    (11, 1, 1, "one.txt", "1/1/one.txt", "staging"),
                    (12, 1, 2, "two.txt", "1/2/two.txt", "current"),
                ],
            )
            conn.execute("CREATE TABLE document_index_jobs (id INTEGER PRIMARY KEY, version_id INTEGER, status TEXT)")
            conn.executemany(
                "INSERT INTO document_index_jobs(id, version_id, status) VALUES (?, ?, ?)",
                [(9, 11, "completed"), (3, 11, "RUNNING"), (7, 12, "queued")],
            )
        elif case == "scope_type_values":
            _seed_versions(
                conn,
                [
                    (21, 1, 1, "one.txt", "1/1/one.txt", "staging"),
                    (22, 1, 2, "two.txt", "1/2/two.txt", "current"),
                    (23, 1, 3, "three.txt", "1/3/three.txt", "staging"),
                ],
            )
            conn.execute(
                "CREATE TABLE document_index_jobs (id INTEGER PRIMARY KEY, version_id INTEGER, status TEXT, "
                "job_scope TEXT, job_type TEXT)"
            )
            conn.executemany(
                "INSERT INTO document_index_jobs(id, version_id, status, job_scope, job_type) VALUES (?, ?, ?, ?, ?)",
                [
                    (90, 21, "completed", "material", "material_index"),
                    (20, 21, "running", "material", "material_index"),
                    (40, 21, "queued", "Material", "material_index"),
                    (30, 21, "retry", "material", "MATERIAL_INDEX"),
                    (50, 22, "completed", None, "material_index"),
                    (60, 22, "running", "material", None),
                    (70, 23, "queued", "", "material_index"),
                    (80, 23, "completed", "material", "other"),
                ],
            )
        elif case == "ordered_mixed_report":
            _seed_versions(
                conn,
                [
                    (31, 1, 1, "first.txt", "1/1/first.txt", "staging"),
                    (32, 1, 2, "second.txt", "1/2/second.txt", "current"),
                    (33, 1, 3, "third.txt", "1/3/third.txt", "staging"),
                ],
            )
            conn.executescript(
                """
                CREATE TABLE document_index_jobs (
                    id INTEGER PRIMARY KEY, version_id INTEGER, status TEXT, job_scope TEXT, job_type TEXT
                );
                CREATE TABLE kb_documents (
                    id INTEGER PRIMARY KEY, kb_id TEXT NOT NULL, status TEXT NOT NULL,
                    document_id INTEGER, document_version_id INTEGER, file_path TEXT
                );
                CREATE TABLE material_archive_manifests (
                    id INTEGER PRIMARY KEY, container_document_id INTEGER, container_version_id INTEGER,
                    status TEXT NOT NULL, source_path TEXT
                );
                """
            )
            # Deliberately insert in reverse/mixed order; the audit contract orders by id.
            conn.executemany(
                "INSERT INTO document_index_jobs(id, version_id, status, job_scope, job_type) VALUES (?, ?, ?, ?, ?)",
                [
                    (95, 31, "completed", "material", "material_index"),
                    (15, 31, "running", "material", "material_index"),
                    (75, 32, "failed", "material", "material_index"),
                    (45, 32, "retry", "material", "material_index"),
                    (85, 9001, "queued", "material", "material_index"),
                ],
            )
            source_dir = versions_root / "staging" / "kb" / hashlib.sha256(b"kb-synthetic").hexdigest()
            source_dir.mkdir(parents=True)
            source_file = source_dir / "pending-source.bin"
            source_file.write_bytes(b"synthetic source")
            stage_file = versions_root / "staging" / "orphan-stage.bin"
            stage_file.parent.mkdir(parents=True, exist_ok=True)
            stage_file.write_bytes(b"synthetic stage")
            (versions_root / "1" / "3").mkdir(parents=True)
            (versions_root / "1" / "3" / "candidate.txt").write_bytes(b"candidate")
            conn.execute(
                "INSERT INTO kb_documents(id, kb_id, status, document_id, document_version_id, file_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (4, "kb-synthetic", "pending", 1, 31, None),
            )
            conn.execute(
                "INSERT INTO material_archive_manifests(id, container_document_id, container_version_id, "
                "status, source_path) VALUES (?, ?, ?, ?, ?)",
                (8, 1, 32, "pending", str(source_file)),
            )
        else:
            raise AssertionError(f"unknown golden case: {case}")
        conn.commit()
    finally:
        conn.close()
    return database, versions_root


def _hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            result[relative] = "<directory>"
    return result


def _normalize(value: Any, temporary_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item, temporary_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, temporary_root) for item in value]
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                suffix = candidate.relative_to(temporary_root).as_posix()
            except ValueError:
                return value
            return "<TMP>" if suffix == "." else f"<TMP>/{suffix}"
    return value


def _expected() -> dict[str, Any]:
    return json.loads(EXPECTED_REPORTS.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "case",
    ["jobs_table_missing", "minimal_jobs_table", "scope_type_values", "ordered_mixed_report"],
)
def test_full_report_matches_frozen_jobs_compatibility_golden(case: str, tmp_path: Path) -> None:
    case_root = tmp_path / case
    database, versions_root = _create_case(case, case_root)
    before_database = hashlib.sha256(database.read_bytes()).hexdigest()
    before_tree = _hash_tree(versions_root)

    report = audit_staging(database, versions_root)

    assert _normalize(report, tmp_path) == _expected()["reports"][case]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_database
    assert _hash_tree(versions_root) == before_tree


def _create_invalid_case(case: str, root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    database = root / "snapshot.sqlite"
    versions_root = root / "versions"
    versions_root.mkdir()
    conn = sqlite3.connect(database)
    try:
        _base_schema(conn)
        _seed_versions(conn, [(51, 1, 1, "one.txt", "1/1/one.txt", "staging")])
        if case == "missing_version_id_column":
            conn.execute("CREATE TABLE document_index_jobs (id INTEGER PRIMARY KEY, status TEXT)")
        elif case == "missing_status_column":
            conn.execute("CREATE TABLE document_index_jobs (id INTEGER PRIMARY KEY, version_id INTEGER)")
        else:
            conn.execute("CREATE TABLE document_index_jobs (id, version_id, status, job_scope, job_type)")
            malformed = {
                "invalid_id_nonmaterial": ("bad", 51, "queued", "other", "other"),
                "invalid_version_id_orphan": (2, 0, "queued", "other", "other"),
                "invalid_status_orphan": (2, 999, 17, "other", "other"),
                "invalid_scope_nonmaterial": (2, 999, "queued", 23, "other"),
                "invalid_type_orphan": (2, 999, "queued", "other", 23),
            }.get(case)
            if malformed is None:
                raise AssertionError(f"unknown invalid case: {case}")
            conn.execute(
                "INSERT INTO document_index_jobs(id, version_id, status, job_scope, job_type) VALUES (?, ?, ?, ?, ?)",
                malformed,
            )
        conn.commit()
    finally:
        conn.close()
    return database, versions_root


@pytest.mark.parametrize(
    "case",
    [
        "missing_version_id_column",
        "missing_status_column",
        "invalid_id_nonmaterial",
        "invalid_version_id_orphan",
        "invalid_status_orphan",
        "invalid_scope_nonmaterial",
        "invalid_type_orphan",
    ],
)
def test_malformed_jobs_return_complete_unsupported_schema_without_mutation(case: str, tmp_path: Path) -> None:
    case_root = tmp_path / case
    database, versions_root = _create_invalid_case(case, case_root)
    before_database = hashlib.sha256(database.read_bytes()).hexdigest()
    before_tree = _hash_tree(versions_root)

    report = audit_staging(database, versions_root)

    assert _normalize(report, tmp_path) == _expected()["errors"][case]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_database
    assert _hash_tree(versions_root) == before_tree
