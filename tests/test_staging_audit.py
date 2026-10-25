"""Read-only staging audit contract over explicit, synthetic SQLite snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from rag.staging_audit import audit_staging

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "audit_staging.py"
CLI_BOOTSTRAP = """
import runpy
import sys
import offline_network_guard

class BlockApplicationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'config' or fullname == 'rag.user_db' or fullname.startswith('rag.materials'):
            raise RuntimeError(f'forbidden application import: {fullname}')
        return None

sys.meta_path.insert(0, BlockApplicationImports())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


def _cli_environment() -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(
        (
            str(REPO_ROOT),
            str(REPO_ROOT / "artifacts" / "kb-lifecycle-concurrency-2026-10-24" / "tools"),
        )
    )
    return env


def _create_snapshot(path: Path, *, optional_tables: bool = True) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
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
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        if optional_tables:
            conn.executescript(
                """
                CREATE TABLE kb_documents (
                    id INTEGER PRIMARY KEY,
                    kb_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    file_path TEXT,
                    status TEXT,
                    document_id INTEGER,
                    document_version_id INTEGER
                );
                CREATE TABLE document_index_jobs (
                    id INTEGER PRIMARY KEY,
                    document_id INTEGER NOT NULL,
                    version_id INTEGER NOT NULL,
                    job_type TEXT NOT NULL,
                    job_scope TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE TABLE material_archive_manifests (
                    id INTEGER PRIMARY KEY,
                    container_document_id INTEGER NOT NULL,
                    container_version_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_path TEXT
                );
                CREATE TABLE material_archive_entries (
                    id INTEGER PRIMARY KEY,
                    manifest_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                """
            )
        conn.commit()
    finally:
        conn.close()


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(root: Path) -> tuple[tuple[str, str], ...]:
    snapshot: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot.append((relative, f"link:{os.readlink(path)}"))
        elif path.is_file():
            snapshot.append((relative, _file_digest(path)))
        elif path.is_dir():
            snapshot.append((relative, "directory"))
    return tuple(snapshot)


def _codes(report: dict) -> set[str]:
    return {finding["code"] for finding in report["findings"]}


def _seed_staging_recovery_snapshot(db_path: Path, versions_root: Path) -> tuple[Path, Path]:
    kb_id = "kb-snapshot"
    staged = versions_root / "staging" / "kb" / hashlib.sha256(kb_id.encode()).hexdigest()
    staged.mkdir(parents=True)
    declared = staged / "missing-upload.bin"
    candidate_dir = versions_root / "17" / "3"
    candidate_dir.mkdir(parents=True)
    candidate = candidate_dir / "opaque-recovery-candidate.dat"
    candidate.write_bytes(b"synthetic recovery bytes")

    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO document_permissions(id, doc_name, kb_id, owner_id, current_version_id) "
            "VALUES (17, 'report.txt', ?, 5, 31)",
            (kb_id,),
        )
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, content_hash, status) "
            "VALUES (31, 17, 3, 'report.txt', ?, 'snapshot-hash', 'staging')",
            (str(declared),),
        )
        conn.execute(
            "INSERT INTO kb_documents "
            "(id, kb_id, filename, file_path, status, document_id, document_version_id) "
            "VALUES (41, ?, 'report.txt', ?, 'pending', 17, 31)",
            (kb_id, str(declared)),
        )
        conn.commit()
    finally:
        conn.close()
    return declared, candidate


def test_missing_database_is_rejected_without_creating_it(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()

    report = audit_staging(db_path, versions_root)

    assert report["error"]["code"] == "database_not_found"
    assert report["action"] == "inspect_only"
    assert not db_path.exists()


def test_dangling_versions_root_symlink_is_rejected_as_a_link(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    _create_snapshot(db_path)
    dangling_root = tmp_path / "linked-versions"
    try:
        dangling_root.symlink_to(tmp_path / "not-created", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    report = audit_staging(db_path, dangling_root)

    assert report["error"]["code"] == "versions_root_link_unsupported"


@pytest.mark.parametrize("sidecar", ["-wal", "-shm"])
def test_sqlite_sidecar_rejects_non_self_contained_snapshot_without_changes(tmp_path: Path, sidecar: str) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_snapshot(db_path)
    sidecar_path = Path(f"{db_path}{sidecar}")
    sidecar_path.write_bytes(b"sidecar evidence")
    before_db = _file_digest(db_path)
    before_sidecar = _file_digest(sidecar_path)
    before_tree = _tree_snapshot(versions_root)

    report = audit_staging(db_path, versions_root)

    assert report["error"]["code"] == "snapshot_sidecar_present"
    assert _file_digest(db_path) == before_db
    assert _file_digest(sidecar_path) == before_sidecar
    assert _tree_snapshot(versions_root) == before_tree


def test_move_back_and_repoint_failure_keeps_context_and_reports_candidate_only(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_snapshot(db_path)
    declared, candidate = _seed_staging_recovery_snapshot(db_path, versions_root)
    before_db = _file_digest(db_path)
    before_tree = _tree_snapshot(versions_root)

    report = audit_staging(db_path, versions_root)

    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()

    assert report["schema_version"] == 1
    assert report["action"] == "inspect_only"
    version = next(entry for entry in report["entries"] if entry["kind"] == "document_version")
    assert version["version_id"] == 31
    assert version["file_state"] == "missing"
    assert version["declared_path"] == str(declared)
    assert version["material_job"]["state"] == "missing"
    assert {reference["kind"] for reference in version["references"]} == {
        "document_permission_owner",
        "permission_current_version",
        "kb_document_version",
    }
    assert "staging_version_missing_material_job" in _codes(report)
    assert "version_file_missing" in _codes(report)
    assert "owned_version_candidate" in _codes(report)
    assert any(entry.get("kind") == "pending_kb_document" for entry in report["entries"])
    assert all(entry["action"] == "inspect_only" for entry in report["entries"])
    assert all(finding["action"] == "inspect_only" for finding in report["findings"])

    candidate_entry = next(entry for entry in report["entries"] if entry["kind"] == "owned_version_candidate")
    assert candidate_entry["path"] == str(candidate)
    assert candidate_entry["version_id"] == 31
    assert candidate_entry["action"] == "inspect_only"
    assert "content_hash" not in candidate_entry
    assert "original_filename" not in candidate_entry
    assert _file_digest(db_path) == before_db
    assert _tree_snapshot(versions_root) == before_tree


def test_unregistered_stage_and_archive_work_files_are_reported_without_opening_them(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_snapshot(db_path)
    stage = versions_root / "staging" / "orphan.bin"
    archive = versions_root / "archive_work" / "orphan-member.bin"
    stage.parent.mkdir()
    archive.parent.mkdir()
    stage.write_bytes(b"never read by the audit")
    archive.write_bytes(b"never read by the audit")
    before_db = _file_digest(db_path)
    before_tree = _tree_snapshot(versions_root)

    report = audit_staging(db_path, versions_root)

    unregistered = [entry for entry in report["entries"] if entry["kind"] == "stage_file"]
    assert {entry["path"] for entry in unregistered} == {str(stage), str(archive)}
    assert all(entry["state"] == "unregistered" for entry in unregistered)
    assert "unregistered_stage_file" in _codes(report)
    assert _file_digest(db_path) == before_db
    assert _tree_snapshot(versions_root) == before_tree


def test_external_database_path_reference_is_reported_without_filesystem_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    external = tmp_path / "outside" / "record.txt"
    versions_root.mkdir()
    external.parent.mkdir()
    external.write_bytes(b"outside root")
    _create_snapshot(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO document_permissions(id, doc_name, kb_id, owner_id, current_version_id) "
            "VALUES (2, 'outside.txt', 'kb-outside', 6, 8)"
        )
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, content_hash, status) "
            "VALUES (8, 2, 1, 'outside.txt', ?, 'db-hash', 'current')",
            (str(external),),
        )
        conn.commit()
    finally:
        conn.close()

    report = audit_staging(db_path, versions_root)

    assert "version_path_unowned" in _codes(report)
    outside = next(entry for entry in report["entries"] if entry.get("version_id") == 8)
    assert outside["file_state"] == "outside_root_uninspected"
    assert "filesystem_exists" not in outside


def test_cross_kb_version_references_are_reported_as_shared_context(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_snapshot(db_path)
    owned_path = versions_root / "7" / "1" / "owned.txt"
    owned_path.parent.mkdir(parents=True)
    owned_path.write_bytes(b"synthetic")
    conn = _connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO document_permissions(id, doc_name, kb_id, owner_id, current_version_id) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (7, "owned.txt", "kb-one", 1, 71),
                (8, "other.txt", "kb-two", 2, 71),
            ],
        )
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, content_hash, status) "
            "VALUES (71, 7, 1, 'owned.txt', ?, 'db-hash', 'current')",
            (str(owned_path),),
        )
        conn.execute(
            "INSERT INTO kb_documents "
            "(id, kb_id, filename, file_path, status, document_id, document_version_id) "
            "VALUES (81, 'kb-two', 'other.txt', NULL, 'pending', 8, 71)"
        )
        conn.commit()
    finally:
        conn.close()

    report = audit_staging(db_path, versions_root)

    version = next(entry for entry in report["entries"] if entry.get("version_id") == 71)
    assert version["ownership"] == "owned"
    assert version["file_state"] == "present"
    assert version["shared_reference_count"] == 2
    assert "version_shared_reference" in _codes(report)
    assert any(reference.get("kb_id") == "kb-two" for reference in version["references"])


def test_pending_archive_source_outside_root_is_unowned_and_uninspected(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_snapshot(db_path)
    outside = tmp_path / "elsewhere" / "archive.zip"
    outside.parent.mkdir()
    outside.write_bytes(b"outside evidence")
    conn = _connect(db_path)
    try:
        owned_path = versions_root / "4" / "2" / "bundle.zip"
        owned_path.parent.mkdir(parents=True)
        owned_path.write_bytes(b"synthetic archive")
        conn.execute(
            "INSERT INTO document_permissions(id, doc_name, kb_id, owner_id, current_version_id) "
            "VALUES (4, 'bundle.zip', 'kb-archive', 1, 42)"
        )
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, content_hash, status) "
            "VALUES (42, 4, 2, 'bundle.zip', ?, 'db-hash', 'current')",
            (str(owned_path),),
        )
        conn.execute(
            "INSERT INTO material_archive_manifests "
            "(id, container_document_id, container_version_id, status, source_path) "
            "VALUES (43, 4, 42, 'pending_confirmation', ?)",
            (str(outside),),
        )
        conn.commit()
    finally:
        conn.close()

    report = audit_staging(db_path, versions_root)

    manifest = next(entry for entry in report["entries"] if entry.get("manifest_id") == 43)
    assert manifest["source_state"] == "unowned"
    assert "pending_archive_manifest" in _codes(report)
    assert "archive_source_unowned" in _codes(report)
    assert "filesystem_exists" not in manifest


def test_legacy_committed_stage_and_confirmed_missing_archive_source_are_not_cleanup_claims(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    _create_snapshot(db_path)
    legacy_stage = versions_root / "staging" / "legacy-committed.txt"
    legacy_stage.parent.mkdir()
    legacy_stage.write_bytes(b"legacy committed bytes")
    missing_confirmed_source = versions_root / "staging" / "already-confirmed.zip"
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO document_permissions(id, doc_name, kb_id, owner_id, current_version_id) "
            "VALUES (3, 'legacy.txt', 'kb-legacy', 7, 9)"
        )
        conn.execute(
            "INSERT INTO document_versions "
            "(id, document_id, version_no, original_filename, file_path, content_hash, status) "
            "VALUES (9, 3, 1, 'legacy.txt', ?, 'db-hash', 'current')",
            (str(legacy_stage),),
        )
        conn.execute(
            "INSERT INTO material_archive_manifests "
            "(id, container_document_id, container_version_id, status, source_path) "
            "VALUES (12, 3, 9, 'confirmed', ?)",
            (str(missing_confirmed_source),),
        )
        conn.commit()
    finally:
        conn.close()

    report = audit_staging(db_path, versions_root)

    legacy = next(entry for entry in report["entries"] if entry.get("version_id") == 9)
    assert legacy["file_state"] == "present_legacy_committed_stage"
    assert legacy["ownership"] == "unowned"
    assert "legacy_committed_stage_preserve" in _codes(report)
    confirmed = next(entry for entry in report["entries"] if entry.get("manifest_id") == 12)
    assert confirmed["source_state"] == "disappeared_after_confirmation"
    assert "confirmed_archive_source_missing" not in _codes(report)


def test_symlink_under_staging_is_reported_without_following_target(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    target = tmp_path / "outside" / "secret.txt"
    versions_root.mkdir()
    target.parent.mkdir()
    target.write_bytes(b"target must not be traversed")
    _create_snapshot(db_path)
    stage_dir = versions_root / "staging"
    stage_dir.mkdir()
    link = stage_dir / "linked-directory"
    try:
        link.symlink_to(target.parent, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    report = audit_staging(db_path, versions_root)

    paths = {entry.get("path") for entry in report["entries"]}
    assert str(link) in paths
    assert str(target) not in paths
    assert "unsafe_link" in _codes(report)


def test_cli_is_json_explicit_and_independent_of_cwd_or_env(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.db"
    versions_root = tmp_path / "versions"
    working_dir = tmp_path / "different-cwd"
    versions_root.mkdir()
    working_dir.mkdir()
    (working_dir / ".env").write_text("not-a-real-configuration\n", encoding="utf-8")
    _create_snapshot(db_path, optional_tables=False)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI_BOOTSTRAP,
            str(AUDIT_SCRIPT),
            "--database",
            str(db_path),
            "--versions-root",
            str(versions_root),
        ],
        cwd=working_dir,
        env=_cli_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["action"] == "inspect_only"
    assert report["entries"] == []


def test_cli_rejects_apply_as_json_input_error(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            CLI_BOOTSTRAP,
            str(AUDIT_SCRIPT),
            "--database",
            "snapshot.db",
            "--versions-root",
            "versions",
            "--apply",
        ],
        cwd=tmp_path,
        env=_cli_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["error"]["code"] == "invalid_arguments"
    assert report["action"] == "inspect_only"


def test_missing_core_table_is_unsupported_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    versions_root = tmp_path / "versions"
    versions_root.mkdir()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE document_permissions(id INTEGER, kb_id TEXT)")
        conn.commit()
    finally:
        conn.close()

    report = audit_staging(db_path, versions_root)

    assert report["error"]["code"] == "unsupported_schema"
