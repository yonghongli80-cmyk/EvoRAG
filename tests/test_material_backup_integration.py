"""D-01: material uploads promote version files before the backup chain runs.

The weekly/deploy backup validates every committed ``document_versions`` row
against the copied payload.  An upload that leaves its file under
``document_versions/staging`` makes ``create_backup`` fail with
``referenced_file_missing`` because the transient staging directory is
excluded.  These tests cover the fixed upload path, the one-shot migration of
legacy staging rows, and the failure contract (no enqueue, staging preserved).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rag.backups.models import BackupContext, BackupError
from rag.backups.service import create_backup, create_if_due
from rag.document_versions import migrate_staged_document_versions
from tests.backup_fixtures import load_manifest


def _activate_uploaded_version(
    client, document_id: int, version_id: int, filename: str
) -> None:
    """Publish the uploaded version through the worker's activation primitive.

    The index worker owns extraction and is maintained separately; D-01 only
    needs the committed version state, so the test drives the same database
    transaction the worker uses instead of scheduling a job.
    """
    client.app.state.user_db.activate_document_version(
        document_id, version_id, "rag_docs", filename
    )


def _backup_context(client, backup_root: Path) -> BackupContext:
    versions_root = Path(client.app.state.document_versions_root)
    data_root = versions_root.parent
    return BackupContext(
        project_root=data_root,
        data_root=data_root,
        backup_root=backup_root,
        databases={"users": Path(client.app.state.user_db.db_path)},
        required_databases=frozenset({"users"}),
        file_roots={
            "upload": Path(client.app.state.upload_dir),
            "kb_files": Path(client.app.state.kb_files_root),
            "document_versions": versions_root,
            "structured_datasets": Path(client.app.state.structured_datasets_root),
        },
    ).validate()


def _upload(client, headers, filename: str, content: bytes):
    return client.post(
        "/materials/upload",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
    )


def test_upload_promotes_version_before_enqueue_and_backup_contains_it(
    isolated_api, make_user, tmp_path_factory
):
    _, headers = make_user("alice")
    response = _upload(isolated_api, headers, "policy.txt", b"leave policy: five days")
    assert response.status_code == 202
    body = response.json()

    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    version = db.get_document_version_by_id(body["version_id"])
    stored = Path(version["file_path"])

    assert stored.is_file()
    assert stored.read_bytes() == b"leave policy: five days"
    assert stored.relative_to(versions_root).parts[0] == str(body["document_id"])
    assert "staging" not in stored.relative_to(versions_root).parts

    staging_dir = versions_root / "staging"
    staged_names = {path.name for path in staging_dir.iterdir()} if staging_dir.is_dir() else set()
    assert not any(name.endswith("-policy.txt") for name in staged_names)

    _activate_uploaded_version(
        isolated_api, body["document_id"], body["version_id"], "policy.txt"
    )
    assert db.get_current_document_version(body["document_id"])["id"] == body["version_id"]

    context = _backup_context(isolated_api, tmp_path_factory.mktemp("d01-backups"))
    record = create_backup(context, "deploy", now=1_777_000_000.0)
    manifest = load_manifest(record.path)
    expected_key = (
        f"document_versions/{body['document_id']}/{version['version_no']}/policy.txt"
    )

    assert record.status == "verified"
    assert expected_key in manifest.file_keys
    assert expected_key in manifest.required_file_keys
    assert not any(key.startswith("document_versions/staging/") for key in manifest.file_keys)

    # The deploy command must also be able to open the next backup window.
    deploy_record = create_if_due(context, "deploy", now=1_777_604_800.0)
    assert deploy_record is not None
    assert deploy_record.status == "verified"
    assert expected_key in load_manifest(deploy_record.path).required_file_keys


def test_legacy_staging_version_is_migrated_so_backup_no_longer_fails(
    isolated_api, make_user, tmp_path_factory
):
    _, headers = make_user("alice")
    response = _upload(isolated_api, headers, "legacy.txt", b"legacy body")
    assert response.status_code == 202
    uploaded = response.json()
    version_id = uploaded["version_id"]

    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    version = db.get_document_version_by_id(version_id)
    final_path = Path(version["file_path"])
    _activate_uploaded_version(
        isolated_api, uploaded["document_id"], version_id, "legacy.txt"
    )

    # Recreate the pre-fix layout: a committed row pointing at staging/.
    staging_dir = versions_root / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = staging_dir / f"legacy-{version_id}-legacy.txt"
    shutil.move(str(final_path), legacy_path)
    db.set_version_file_path(version_id, str(legacy_path))

    context = _backup_context(isolated_api, tmp_path_factory.mktemp("d01-legacy-backups"))
    with pytest.raises(BackupError, match="referenced_file_missing"):
        create_backup(context, "manual", now=1_777_000_000.0)

    report = migrate_staged_document_versions(db, versions_root)

    assert report.migrated == 1
    migrated = db.get_document_version_by_id(version_id)
    migrated_path = Path(migrated["file_path"])
    assert migrated_path == final_path
    assert migrated_path.is_file()
    assert migrated_path.read_bytes() == b"legacy body"
    assert not legacy_path.exists()
    assert not migrated_path.is_relative_to(staging_dir)

    # Idempotent: a second run has nothing left to move.
    second = migrate_staged_document_versions(db, versions_root)
    assert (second.migrated, second.missing, second.conflicts) == (0, 0, 0)

    record = create_backup(context, "manual", now=1_777_000_000.0)
    manifest = load_manifest(record.path)
    expected_key = (
        f"document_versions/{migrated['document_id']}/{migrated['version_no']}/legacy.txt"
    )
    assert record.status == "verified"
    assert expected_key in manifest.file_keys
    assert expected_key in manifest.required_file_keys


def test_promotion_failure_keeps_staging_and_never_enqueues(
    isolated_api, make_user, monkeypatch
):
    _, headers = make_user("alice")
    db = isolated_api.app.state.user_db
    versions_root = Path(isolated_api.app.state.document_versions_root)
    attempts: list[tuple[Path, Path]] = []

    def fail_promotion(stage: Path, destination: Path) -> None:
        attempts.append((Path(stage), Path(destination)))
        raise OSError("disk full")

    monkeypatch.setattr("rag.document_versions.promote_version", fail_promotion)

    response = _upload(isolated_api, headers, "broken.txt", b"keep me")

    assert response.status_code == 500
    assert response.json()["detail"] == "material_promotion_failed"
    assert len(attempts) == 1
    assert db.get_document_permission("broken.txt", "rag_docs") is None
    assert db.get_index_job(1) is None

    staged_files = list((versions_root / "staging").glob("*-broken.txt"))
    assert len(staged_files) == 1
    assert staged_files[0].read_bytes() == b"keep me"
