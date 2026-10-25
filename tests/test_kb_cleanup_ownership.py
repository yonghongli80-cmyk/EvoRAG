"""Owner checks for KB teardown paths and archive references."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import rag.api as api
from rag.kb_document_cleanup import resolve_kb_document_paths, resolve_owned_version_path


def _create_version(db, root: Path, document_id: int, filename: str, owner_id: int):
    path = root / str(document_id) / "1" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"version for {document_id}".encode())
    version_id = db.create_document_version(
        document_id, filename, str(path), f"hash-{document_id}", owner_id
    )
    return version_id, path


def _stub_delete_side_effects(monkeypatch, manager_calls):
    import rag.routers.knowledge as knowledge
    import rag.wiki.api as wiki_api

    class Manager:
        def remove_document(self, *_args):
            manager_calls.append("remove_document")

        def delete_kb(self, _kb_id):
            manager_calls.append("delete_kb")

    class WikiStore:
        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        _lock = Lock()

        class Connection:
            @staticmethod
            def execute(*_args):
                class Rows:
                    @staticmethod
                    def fetchall():
                        return []

                return Rows()

        _conn = Connection()

    class WikiIndexer:
        @staticmethod
        def remove_kb_content(*_args, **_kwargs):
            manager_calls.append("remove_wiki")

    class BM25:
        @staticmethod
        def invalidate(_kb_id):
            manager_calls.append("invalidate_bm25")

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(knowledge, "KnowledgeBaseManager", Manager)
    monkeypatch.setattr(wiki_api, "get_wiki_store", lambda: WikiStore())
    monkeypatch.setattr(wiki_api, "get_wiki_indexer", lambda *_args: WikiIndexer())
    monkeypatch.setattr("rag.bm25_store.BM25Store", BM25)


def test_kb_delete_rejects_version_path_owned_by_another_kb_without_directory_row(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("cleanup-owner-missing-directory-row")
    db = api.app.state.user_db
    target_kb = "kb_cleanup_target_without_row"
    other_kb = "kb_cleanup_external_owner"
    db.create_kb_metadata(target_kb, "Target", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Other", owner["id"], "private")
    target_document_id = db.create_document_permission(
        "target.txt", target_kb, owner["id"]
    )
    other_document_id = db.create_document_permission(
        "other.txt", other_kb, owner["id"]
    )
    versions_root = Path(api.app.state.document_versions_root)
    _other_version_id, other_path = _create_version(
        db, versions_root, other_document_id, "other.txt", owner["id"]
    )
    target_version_id = db.create_document_version(
        target_document_id,
        "target.txt",
        str(other_path),
        "target-hash",
        owner["id"],
    )

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{target_kb}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == "version_path_unowned"
    assert manager_calls == []
    assert other_path.read_bytes() == b"version for " + str(other_document_id).encode()
    assert db.get_document_permission_by_id(target_document_id) is not None
    assert db.get_document_version_by_id(target_version_id)["file_path"] == str(other_path)
    assert db.get_document_permission_by_id(other_document_id) is not None


def test_kb_delete_rejects_manifest_with_version_from_another_document(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("cleanup-owner-archive-identity")
    db = api.app.state.user_db
    target_kb, other_kb = "kb_archive_identity_target", "kb_archive_identity_other"
    db.create_kb_metadata(target_kb, "Target", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Other", owner["id"], "private")
    target_document_id = db.create_document_permission(
        "archive.zip", target_kb, owner["id"]
    )
    other_document_id = db.create_document_permission(
        "other.zip", other_kb, owner["id"]
    )
    versions_root = Path(api.app.state.document_versions_root)
    target_version_id, target_path = _create_version(
        db, versions_root, target_document_id, "archive.zip", owner["id"]
    )
    other_version_id, _other_path = _create_version(
        db, versions_root, other_document_id, "other.zip", owner["id"]
    )
    archive_source = (
        versions_root
        / "staging"
        / "kb"
        / hashlib.sha256(target_kb.encode()).hexdigest()
        / "archive.zip"
    )
    archive_source.parent.mkdir(parents=True, exist_ok=True)
    archive_source.write_bytes(b"original archive")
    manifest_id = db.create_material_archive_manifest(
        target_document_id, other_version_id, str(archive_source), owner["id"], 4_102_444_800
    )

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{target_kb}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == "archive_identity_mismatch"
    assert manager_calls == []
    assert target_path.exists() and archive_source.read_bytes() == b"original archive"
    assert db.get_material_archive_manifest(manifest_id)["container_version_id"] == other_version_id
    assert db.get_document_permission_by_id(target_document_id) is not None
    assert db.get_document_version_by_id(target_version_id) is not None


@pytest.mark.parametrize("reference_target", ["planned_file", "cleanup_directory"])
def test_kb_delete_rejects_cross_kb_manifest_reference_to_planned_target(
    isolated_api, make_user, monkeypatch, reference_target
):
    owner, headers = make_user("cleanup-owner-shared-manifest")
    db = api.app.state.user_db
    target_kb, other_kb = "kb_shared_path_target", "kb_shared_path_other"
    db.create_kb_metadata(target_kb, "Target", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Other", owner["id"], "private")
    target_document_id = db.create_document_permission(
        "target.txt", target_kb, owner["id"]
    )
    other_document_id = db.create_document_permission(
        "other.zip", other_kb, owner["id"]
    )
    versions_root = Path(api.app.state.document_versions_root)
    if reference_target == "planned_file":
        _target_version_id, target_path = _create_version(
            db, versions_root, target_document_id, "target.txt", owner["id"]
        )
        preserved_file = target_path
        shared_path = target_path
    else:
        target_hash = hashlib.sha256(target_kb.encode()).hexdigest()
        shared_path = versions_root / "staging" / "kb" / target_hash
        preserved_file = shared_path / "keep.txt"
        preserved_file.parent.mkdir(parents=True, exist_ok=True)
        preserved_file.write_bytes(b"staging owner")
    other_version_id, other_path = _create_version(
        db, versions_root, other_document_id, "other.zip", owner["id"]
    )
    manifest_id = db.create_material_archive_manifest(
        other_document_id, other_version_id, str(shared_path), owner["id"], 4_102_444_800
    )

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{target_kb}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == "cleanup_path_shared"
    assert manager_calls == []
    assert preserved_file.exists()
    if reference_target == "planned_file":
        assert preserved_file.read_bytes() == b"version for " + str(target_document_id).encode()
    else:
        assert preserved_file.read_bytes() == b"staging owner"
    assert other_path.exists()
    assert db.get_document_permission_by_id(target_document_id) is not None
    assert db.get_document_permission_by_id(other_document_id) is not None
    assert db.get_material_archive_manifest(manifest_id)["source_path"] == str(shared_path)


@pytest.mark.parametrize("reference_target", ["planned_file", "cleanup_directory"])
def test_kb_delete_rejects_cross_kb_version_reference_to_planned_target(
    isolated_api, make_user, monkeypatch, reference_target
):
    owner, headers = make_user(f"cleanup-owner-shared-version-{reference_target}")
    db = api.app.state.user_db
    target_kb, other_kb = "kb_shared_version_target", "kb_shared_version_other"
    db.create_kb_metadata(target_kb, "Target", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Other", owner["id"], "private")
    target_document_id = db.create_document_permission(
        "target.txt", target_kb, owner["id"]
    )
    other_document_id = db.create_document_permission(
        "other.txt", other_kb, owner["id"]
    )
    versions_root = Path(api.app.state.document_versions_root)
    if reference_target == "planned_file":
        _target_version_id, shared_path = _create_version(
            db, versions_root, target_document_id, "target.txt", owner["id"]
        )
        preserved_file = shared_path
    else:
        target_hash = hashlib.sha256(target_kb.encode()).hexdigest()
        shared_path = versions_root / "staging" / "kb" / target_hash
        preserved_file = shared_path / "keep.txt"
        preserved_file.parent.mkdir(parents=True, exist_ok=True)
        preserved_file.write_bytes(b"staging owner")
    other_version_id = db.create_document_version(
        other_document_id, "other.txt", str(shared_path), "external-ref", owner["id"]
    )

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{target_kb}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == "cleanup_path_shared"
    assert manager_calls == []
    assert preserved_file.exists()
    if reference_target == "cleanup_directory":
        assert preserved_file.read_bytes() == b"staging owner"
    assert db.get_document_permission_by_id(target_document_id) is not None
    assert db.get_document_permission_by_id(other_document_id) is not None
    assert db.get_document_version_by_id(other_version_id)["file_path"] == str(shared_path)


@pytest.mark.parametrize(
    ("manifest_status", "confirmation_state"),
    [
        ("pending_confirmation", "pending"),
        ("confirmed", "confirmed"),
        # Failed activation leaves the manifest pending while the container
        # version path is already final and the source remains quarantined.
        ("pending_confirmation", "failed_confirmation"),
    ],
)
def test_kb_delete_accepts_archive_source_not_equal_to_container_version_path(
    isolated_api, make_user, monkeypatch, manifest_status, confirmation_state
):
    owner, headers = make_user(f"cleanup-owner-valid-archive-{confirmation_state}")
    db = api.app.state.user_db
    kb_id = f"kb_valid_archive_{confirmation_state}"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    document_id = db.create_document_permission("archive.zip", kb_id, owner["id"])
    versions_root = Path(api.app.state.document_versions_root)
    version_id, version_path = _create_version(
        db, versions_root, document_id, "archive.zip", owner["id"]
    )
    source_path = (
        versions_root
        / "staging"
        / "kb"
        / hashlib.sha256(kb_id.encode()).hexdigest()
        / "original-upload.zip"
    )
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"archive source")
    manifest_id = db.create_material_archive_manifest(
        document_id, version_id, str(source_path), owner["id"], 4_102_444_800
    )
    db.set_material_archive_manifest_status(manifest_id, manifest_status)

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 200, response.text
    assert not version_path.exists() and not source_path.exists()
    assert db.get_document_permission_by_id(document_id) is None


def test_kb_delete_rejects_archive_source_in_container_final_path(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("cleanup-owner-final-archive-source")
    db = api.app.state.user_db
    kb_id = "kb_final_archive_source"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    document_id = db.create_document_permission("archive.zip", kb_id, owner["id"])
    versions_root = Path(api.app.state.document_versions_root)
    version_id, version_path = _create_version(
        db, versions_root, document_id, "archive.zip", owner["id"]
    )
    original_version_bytes = version_path.read_bytes()
    final_source = version_path
    manifest_id = db.create_material_archive_manifest(
        document_id, version_id, str(final_source), owner["id"], 4_102_444_800
    )

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == "archive_source_unowned"
    assert manager_calls == []
    assert version_path.read_bytes() == original_version_bytes
    assert db.get_material_archive_manifest(manifest_id)["source_path"] == str(final_source)
    assert db.get_document_permission_by_id(document_id) is not None


def test_kb_delete_reports_legacy_global_archive_source_before_unowned_version_path(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("cleanup-owner-legacy-pending-archive")
    db = api.app.state.user_db
    kb_id = "kb_legacy_pending_archive_source"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    document_id = db.create_document_permission("archive.zip", kb_id, owner["id"])
    versions_root = Path(api.app.state.document_versions_root)
    global_source = versions_root / "staging" / "legacy-archive.zip"
    global_source.parent.mkdir(parents=True, exist_ok=True)
    global_source.write_bytes(b"preserve legacy global archive")
    version_id = db.create_document_version(
        document_id, "archive.zip", str(global_source), "legacy-hash", owner["id"]
    )
    manifest_id = db.create_material_archive_manifest(
        document_id, version_id, str(global_source), owner["id"], 4_102_444_800
    )

    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == "archive_source_unowned"
    assert manager_calls == []
    assert global_source.read_bytes() == b"preserve legacy global archive"
    assert db.get_material_archive_manifest(manifest_id)["source_path"] == str(global_source)
    assert db.get_document_version_by_id(version_id)["file_path"] == str(global_source)
    assert db.get_document_permission_by_id(document_id) is not None


def test_resolve_owned_version_path_accepts_document_stage_and_missing_target(tmp_path):
    root = tmp_path / "versions"
    document_id = 27
    final_missing = root / str(document_id) / "3" / "missing.txt"
    document_stage = root / "staging" / str(document_id) / "upload.txt"
    kb_stage = (
        root
        / "staging"
        / "kb"
        / hashlib.sha256(b"kb-owner").hexdigest()
        / "archive.zip"
    )

    assert resolve_owned_version_path(
        root, str(final_missing), document_id=document_id, kb_id="kb-owner"
    ) == final_missing
    assert resolve_owned_version_path(
        root, str(document_stage), document_id=document_id, kb_id="kb-owner"
    ) == document_stage
    assert resolve_owned_version_path(
        root, str(kb_stage), document_id=document_id, kb_id="kb-owner"
    ) == kb_stage


def test_resolve_owned_version_path_rejects_foreign_staging_escape_and_links(tmp_path):
    import hashlib

    root = tmp_path / "versions"
    document_id = 27
    kb_id = "kb-owner"
    foreign_stage = root / "staging" / "kb" / hashlib.sha256(b"other-kb").hexdigest() / "x.zip"
    escaped = tmp_path / "outside.txt"
    traversal = root / str(document_id) / "2" / ".." / "28" / "1" / "x.txt"
    for stored_path in (
        str(foreign_stage),
        str(escaped),
        str(root / "28" / "1" / "x.txt"),
        str(traversal),
    ):
        with pytest.raises(ValueError, match="version_path_unowned"):
            resolve_owned_version_path(
                root, stored_path, document_id=document_id, kb_id=kb_id
            )

    target = root / "28" / "1" / "target.txt"
    link = root / str(document_id) / "2" / "link.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("preserve", encoding="utf-8")
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"host cannot create a temporary symlink: {exc}")
    with pytest.raises(ValueError, match="version_path_unowned"):
        resolve_owned_version_path(
            root, str(link), document_id=document_id, kb_id=kb_id
        )
    assert target.read_text(encoding="utf-8") == "preserve"


def test_single_document_cleanup_does_not_accept_kb_wide_stage(
    isolated_api, make_user
):
    owner, _headers = make_user("single-document-stage-owner")
    db = api.app.state.user_db
    kb_id = "kb_single_document_stage"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    document_id = db.create_document_permission("archive.zip", kb_id, owner["id"])
    versions_root = Path(api.app.state.document_versions_root)
    kb_stage = (
        versions_root
        / "staging"
        / "kb"
        / hashlib.sha256(kb_id.encode()).hexdigest()
        / "archive.zip"
    )
    kb_stage.parent.mkdir(parents=True, exist_ok=True)
    kb_stage.write_bytes(b"archive")
    db.create_document_version(
        document_id, "archive.zip", str(kb_stage), "hash", owner["id"]
    )
    record = {"id": 1, "filename": "archive.zip", "file_path": str(kb_stage)}

    with pytest.raises(ValueError, match="version_path_unowned"):
        resolve_kb_document_paths(
            db, kb_id, record, versions_root, Path(api.app.state.kb_files_root)
        )
    assert resolve_kb_document_paths(
        db,
        kb_id,
        record,
        versions_root,
        Path(api.app.state.kb_files_root),
        kb_wide=True,
    ) == [kb_stage]
    assert kb_stage.read_bytes() == b"archive"


@pytest.mark.parametrize(
    ("path_kind", "error_code"),
    [
        ("version_directory", "version_path_unowned"),
        ("archive_source_directory", "archive_source_unowned"),
        ("legacy_file_directory", "cleanup_path_invalid"),
        ("dataset_directory", "cleanup_path_invalid"),
        ("kb_staging_root_file", "cleanup_path_invalid"),
        ("document_staging_root_file", "cleanup_path_invalid"),
    ],
)
def test_kb_delete_rejects_non_regular_owned_targets_before_manager_calls(
    isolated_api, make_user, monkeypatch, path_kind, error_code
):
    owner, headers = make_user(f"cleanup-owner-non-regular-{path_kind}")
    db = api.app.state.user_db
    kb_id = f"kb_non_regular_{path_kind}"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    versions_root = Path(api.app.state.document_versions_root)
    kb_files_root = Path(api.app.state.kb_files_root)
    datasets_root = Path(api.app.state.structured_datasets_root)
    document_id = None
    version_id = None
    manifest_id = None
    kb_document_id = None

    if path_kind == "version_directory":
        document_id = db.create_document_permission("target.txt", kb_id, owner["id"])
        malformed_path = versions_root / str(document_id) / "1" / "claimed-file"
        protected_file = malformed_path / "keep.bin"
        protected_file.parent.mkdir(parents=True)
        protected_file.write_bytes(b"version directory payload")
        version_id = db.create_document_version(
            document_id, "target.txt", str(malformed_path), "hash", owner["id"]
        )
    elif path_kind == "archive_source_directory":
        document_id = db.create_document_permission("archive.zip", kb_id, owner["id"])
        version_id, version_path = _create_version(
            db, versions_root, document_id, "archive.zip", owner["id"]
        )
        malformed_path = (
            versions_root
            / "staging"
            / "kb"
            / hashlib.sha256(kb_id.encode()).hexdigest()
            / "claimed-source"
        )
        protected_file = malformed_path / "keep.bin"
        protected_file.parent.mkdir(parents=True)
        protected_file.write_bytes(b"archive directory payload")
        manifest_id = db.create_material_archive_manifest(
            document_id, version_id, str(malformed_path), owner["id"], 4_102_444_800
        )
    elif path_kind == "legacy_file_directory":
        kb_document_id = db.create_kb_document_pending(kb_id, "legacy.txt")
        malformed_path = kb_files_root / kb_id / str(kb_document_id) / "legacy.txt"
        protected_file = malformed_path / "keep.bin"
        protected_file.parent.mkdir(parents=True)
        protected_file.write_bytes(b"legacy directory payload")
        db.finish_kb_document(kb_document_id, str(malformed_path), "[]", "summary", 1)
    elif path_kind == "dataset_directory":
        document_id = db.create_document_permission("table.xlsx", kb_id, owner["id"])
        version_id, _version_path = _create_version(
            db, versions_root, document_id, "table.xlsx", owner["id"]
        )
        db.create_structured_dataset(document_id, version_id, "xlsx", "schema_only")
        malformed_path = datasets_root / str(document_id) / str(version_id) / "dataset.sqlite3"
        protected_file = malformed_path / "keep.bin"
        protected_file.parent.mkdir(parents=True)
        protected_file.write_bytes(b"dataset directory payload")
    elif path_kind == "kb_staging_root_file":
        malformed_path = (
            versions_root
            / "staging"
            / "kb"
            / hashlib.sha256(kb_id.encode()).hexdigest()
        )
        malformed_path.parent.mkdir(parents=True)
        malformed_path.write_bytes(b"staging root file")
        protected_file = malformed_path
    else:
        document_id = db.create_document_permission("staging.txt", kb_id, owner["id"])
        malformed_path = versions_root / "staging" / str(document_id)
        malformed_path.parent.mkdir(parents=True, exist_ok=True)
        malformed_path.write_bytes(b"document staging file")
        protected_file = malformed_path

    protected_bytes = protected_file.read_bytes()
    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 503
    assert response.headers["X-KB-Lifecycle-Error"] == error_code
    assert manager_calls == []
    assert protected_file.read_bytes() == protected_bytes
    assert db.get_kb_metadata(kb_id) is not None
    if document_id is not None:
        assert db.get_document_permission_by_id(document_id) is not None
    if version_id is not None:
        assert db.get_document_version_by_id(version_id) is not None
    if manifest_id is not None:
        assert db.get_material_archive_manifest(manifest_id) is not None
    if kb_document_id is not None:
        assert db.get_kb_document(kb_id, "legacy.txt")["id"] == kb_document_id


@pytest.mark.parametrize(
    "missing_kind",
    ["version_file", "archive_source", "legacy_file", "dataset_file"],
)
def test_kb_delete_keeps_missing_file_retry_behavior(
    isolated_api, make_user, monkeypatch, missing_kind
):
    owner, headers = make_user(f"cleanup-owner-missing-{missing_kind}")
    db = api.app.state.user_db
    kb_id = f"kb_missing_{missing_kind}"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    versions_root = Path(api.app.state.document_versions_root)
    kb_files_root = Path(api.app.state.kb_files_root)
    datasets_root = Path(api.app.state.structured_datasets_root)
    document_id = None
    missing_path = None

    if missing_kind == "version_file":
        document_id = db.create_document_permission("missing.txt", kb_id, owner["id"])
        missing_path = versions_root / str(document_id) / "1" / "missing.txt"
        db.create_document_version(
            document_id, "missing.txt", str(missing_path), "missing-hash", owner["id"]
        )
    elif missing_kind == "archive_source":
        document_id = db.create_document_permission("archive.zip", kb_id, owner["id"])
        version_id, _version_path = _create_version(
            db, versions_root, document_id, "archive.zip", owner["id"]
        )
        missing_path = (
            versions_root
            / "staging"
            / "kb"
            / hashlib.sha256(kb_id.encode()).hexdigest()
            / "missing.zip"
        )
        db.create_material_archive_manifest(
            document_id, version_id, str(missing_path), owner["id"], 4_102_444_800
        )
    elif missing_kind == "legacy_file":
        kb_document_id = db.create_kb_document_pending(kb_id, "missing.txt")
        missing_path = kb_files_root / kb_id / str(kb_document_id) / "missing.txt"
        db.finish_kb_document(kb_document_id, str(missing_path), "[]", "summary", 1)
    else:
        document_id = db.create_document_permission("table.xlsx", kb_id, owner["id"])
        version_id, _version_path = _create_version(
            db, versions_root, document_id, "table.xlsx", owner["id"]
        )
        db.create_structured_dataset(document_id, version_id, "xlsx", "schema_only")
        missing_path = datasets_root / str(document_id) / str(version_id) / "dataset.sqlite3"

    assert not missing_path.exists()
    manager_calls = []
    _stub_delete_side_effects(monkeypatch, manager_calls)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

    assert response.status_code == 200, response.text
    assert not missing_path.exists()
    assert db.get_kb_metadata(kb_id) is None
    if document_id is not None:
        assert db.get_document_permission_by_id(document_id) is None
    if missing_kind == "legacy_file":
        assert db.get_kb_document(kb_id, "missing.txt") is None


def _create_windows_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows directory junctions are unavailable on this platform")
    env = os.environ.copy()
    env["KB_CLEANUP_TEST_JUNCTION_LINK"] = str(link)
    env["KB_CLEANUP_TEST_JUNCTION_TARGET"] = str(target)
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
            "-Path $env:KB_CLEANUP_TEST_JUNCTION_LINK "
            "-Target $env:KB_CLEANUP_TEST_JUNCTION_TARGET | Out-Null",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert link.is_junction()


def test_owned_version_junction_is_rejected_by_helper_and_delete_route(
    isolated_api, make_user, monkeypatch, tmp_path
):
    owner, headers = make_user("cleanup-owner-junction")
    db = api.app.state.user_db
    kb_id = "kb_version_junction_owner"
    db.create_kb_metadata(kb_id, "Target", owner["id"], "private")
    document_id = db.create_document_permission("target.txt", kb_id, owner["id"])
    versions_root = Path(api.app.state.document_versions_root)
    target_dir = versions_root / str(document_id) / "1" / "real-directory"
    target_file = target_dir / "target.txt"
    target_file.parent.mkdir(parents=True)
    target_file.write_bytes(b"preserve junction target")
    junction = versions_root / str(document_id) / "1" / "junction"
    stored_path = junction / "target.txt"
    version_id = db.create_document_version(
        document_id, "target.txt", str(stored_path), "junction-hash", owner["id"]
    )
    _create_windows_junction(junction, target_dir)

    try:
        with pytest.raises(ValueError, match="version_path_unowned"):
            resolve_owned_version_path(
                versions_root,
                str(stored_path),
                document_id=document_id,
                kb_id=kb_id,
            )

        manager_calls = []
        _stub_delete_side_effects(monkeypatch, manager_calls)
        response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)

        assert response.status_code == 503
        assert response.headers["X-KB-Lifecycle-Error"] == "version_path_unowned"
        assert manager_calls == []
        assert target_file.read_bytes() == b"preserve junction target"
        assert db.get_document_version_by_id(version_id)["file_path"] == str(stored_path)
        assert db.get_document_permission_by_id(document_id) is not None
    finally:
        if junction.is_junction():
            junction.rmdir()
