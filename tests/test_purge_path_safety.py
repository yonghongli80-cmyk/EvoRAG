import pytest

from rag.materials.recycle import PURGED_STATE, RecycleError, purge_document
from rag.spreadsheets.store import resolve_dataset_path
from rag.user_db import UserDB


@pytest.fixture
def case(tmp_path):
    db = UserDB(str(tmp_path / "users.db"))
    db.create_material_schema()
    user = db.create_user("alice", "Password-123!")
    doc = db.create_document_permission("one.pdf", "kb-1", user)
    root = tmp_path / "versions"
    root.mkdir()
    target = root / "legacy.pdf"
    target.write_bytes(b"legacy")
    version = db.create_document_version(doc, "one.pdf", str(target), "hash", user)
    db.set_version_file_path(version, str(target))
    db.activate_document_version(doc, version, "kb-1", "one.pdf")
    with db._lock, db.transaction(immediate=True):
        db._conn.execute("UPDATE document_permissions SET deleted_at=1, restore_until=2 WHERE id=?", (doc,))
    yield db, user, doc, version, root, target
    db.close()


def set_path(db, version, path):
    db.set_version_file_path(version, str(path))


def assert_retryable(db, doc):
    assert db.get_document_permission_by_id(doc)["purge_state"] != PURGED_STATE


class Vector:
    def __init__(self):
        self.calls = []

    def delete_document(self, document_id):
        self.calls.append(document_id)


@pytest.mark.parametrize("kind", ["outside", "missing_root", "other_version", "archive_source", "later_invalid"])
def test_unsafe_target_fails_before_any_side_effect(case, tmp_path, kind):
    db, user, doc, version, root, target = case
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_path = root / "other.pdf"
    other_path.write_bytes(b"other")
    other_version = db.create_document_version(other, "other.pdf", str(other_path), "hash-other", user)
    db.set_version_file_path(other_version, str(other_path))
    selected_root = root
    if kind == "outside":
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"outside")
        set_path(db, version, outside)
        target = outside
    elif kind == "missing_root":
        selected_root = None
    elif kind == "other_version":
        set_path(db, version, other_path)
        target = other_path
    elif kind == "archive_source":
        db.create_material_archive_manifest(other, other_version, str(target), user, 1000)
    else:
        outside = tmp_path / "outside.pdf"
        outside.write_bytes(b"outside")
        second = db.create_document_version(doc, "two.pdf", str(outside), "hash-two", user)
        set_path(db, second, outside)
    vector = Vector()
    with pytest.raises(RecycleError) as exc:
        purge_document(db, doc, now=10, versions_root=selected_root, vector_store=vector)
    assert exc.value.reason_code
    assert target.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


def test_parent_symlink_is_rejected(case, tmp_path):
    db, _, doc, version, root, target = case
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.pdf").write_bytes(b"protected")
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create directory symlink: {exc}")
    set_path(db, version, link / "file.pdf")
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root)
    assert (outside / "file.pdf").is_file()
    assert_retryable(db, doc)


def test_dataset_version_must_belong_to_document(case, tmp_path):
    db, user, doc, _, root, target = case
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(root / "other.pdf"), "other", user)
    datasets = tmp_path / "datasets"
    dataset = resolve_dataset_path(datasets, doc, other_version)
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"dataset")
    dataset_id = db.create_structured_dataset(other, other_version, "xlsx", "row_groups")
    with db._lock, db.transaction(immediate=True):
        db._conn.execute("UPDATE structured_datasets SET document_id=? WHERE id=?", (doc, dataset_id))
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, structured_datasets_root=datasets)
    assert target.is_file() and dataset.is_file()
    assert_retryable(db, doc)


def test_legacy_file_in_root_is_purged(case):
    db, _, doc, _, root, target = case
    assert purge_document(db, doc, now=10, versions_root=root).status == "purged"
    assert not target.exists()


def test_final_transaction_failure_can_retry(case, monkeypatch):
    import rag.materials.recycle as recycle

    db, _, doc, _, root, target = case
    original = recycle._append_audit

    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(recycle, "_append_audit", fail)
    with pytest.raises(RuntimeError, match="audit failure"):
        purge_document(db, doc, now=10, versions_root=root)
    assert not target.exists()
    assert_retryable(db, doc)
    monkeypatch.setattr(recycle, "_append_audit", original)
    assert purge_document(db, doc, now=11, versions_root=root).status == "purged"


def test_deferred_constraint_commit_failure_can_retry(case):
    import sqlite3

    db, _, doc, _, root, target = case
    with db._lock:
        db._conn.execute("PRAGMA foreign_keys=ON")
        db._conn.executescript("""
            CREATE TABLE purge_test_parent (id INTEGER PRIMARY KEY);
            CREATE TABLE purge_test_child (
                parent_id INTEGER REFERENCES purge_test_parent(id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER purge_test_commit_failure AFTER UPDATE OF purge_state ON document_permissions
            BEGIN INSERT INTO purge_test_child(parent_id) VALUES (42); END;
        """)
    with pytest.raises(sqlite3.IntegrityError):
        purge_document(db, doc, now=10, versions_root=root)
    assert not target.exists()
    assert_retryable(db, doc)
    with db._lock:
        db._conn.execute("DROP TRIGGER purge_test_commit_failure")
    assert purge_document(db, doc, now=11, versions_root=root).status == "purged"


def test_other_document_symlink_reference_protects_target(case):
    db, user, doc, _, root, target = case
    alias = root / "alias.pdf"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create file symlink: {exc}")
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(alias), "other", user)
    db.set_version_file_path(other_version, str(alias))
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert target.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


def test_relative_path_without_root_fails_closed(case):
    db, _, doc, version, _, target = case
    set_path(db, version, target.name)
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, vector_store=vector)
    assert target.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


def test_dataset_rows_without_root_fail_before_side_effects(case, tmp_path):
    db, _, doc, version, root, target = case
    datasets = tmp_path / "datasets"
    dataset = resolve_dataset_path(datasets, doc, version)
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"dataset")
    dataset_id = db.create_structured_dataset(doc, version, "xlsx", "row_groups")
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert target.is_file() and dataset.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)
    assert db.get_structured_dataset_for_version(version)["id"] == dataset_id


def test_relative_root_accepts_persisted_final_version_path(case, tmp_path, monkeypatch):
    from pathlib import Path

    from rag.document_version_store import final_version_path

    db, _, doc, version, _, _ = case
    monkeypatch.chdir(tmp_path)
    root = Path("data/versions")
    target = final_version_path(root, doc, 1, "one.pdf")
    target.parent.mkdir(parents=True)
    target.write_bytes(b"version")
    set_path(db, version, target)
    assert purge_document(db, doc, now=10, versions_root=root).status == "purged"
    assert not target.exists()


def staged_target(tmp_path, monkeypatch, doc):
    from pathlib import Path

    from rag.document_sources.base import SourceObject
    from rag.document_version_store import stage_version

    monkeypatch.chdir(tmp_path)
    root = Path("data/versions")
    source = SourceObject("upload", "source", "one.pdf", b"version", "hash", 1.0)
    target = stage_version(root, source, document_id=doc)
    assert not target.is_absolute()
    return root, target


def test_relative_root_accepts_persisted_stage_path(case, tmp_path, monkeypatch):
    db, _, doc, version, _, _ = case
    root, target = staged_target(tmp_path, monkeypatch, doc)
    set_path(db, version, target)
    assert purge_document(db, doc, now=10, versions_root=root).status == "purged"
    assert not target.exists()


def test_ambiguous_relative_target_is_not_deleted(case, tmp_path, monkeypatch):
    db, _, doc, version, _, _ = case
    root, target = staged_target(tmp_path, monkeypatch, doc)
    alternate = root / target
    alternate.parent.mkdir(parents=True)
    alternate.write_bytes(b"second")
    set_path(db, version, target)
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert target.is_file() and alternate.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


def test_relative_foreign_reference_protects_persisted_target(case, tmp_path, monkeypatch):
    db, user, doc, version, _, _ = case
    root, target = staged_target(tmp_path, monkeypatch, doc)
    set_path(db, version, target.relative_to(root))
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(target), "other", user)
    db.set_version_file_path(other_version, str(target))
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert target.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


@pytest.mark.parametrize("reference_kind", ["version", "archive"])
def test_outside_symlink_reference_protects_target(case, tmp_path, reference_kind):
    db, user, doc, _, root, target = case
    alias = tmp_path / "outside-alias.pdf"
    try:
        alias.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create file symlink: {exc}")
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(alias), "other", user)
    if reference_kind == "version":
        db.set_version_file_path(other_version, str(alias))
    else:
        db.set_version_file_path(other_version, str(root / "unrelated.pdf"))
        db.create_material_archive_manifest(other, other_version, str(alias), user, 1000)
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert target.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)
    assert db.get_document_permission_by_id(doc)["current_version_id"] is not None


def test_other_document_reference_to_dataset_under_separate_root(case, tmp_path):
    db, user, doc, version, root, target = case
    datasets = tmp_path / "datasets"
    dataset = resolve_dataset_path(datasets, doc, version)
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"dataset")
    db.create_structured_dataset(doc, version, "xlsx", "row_groups")
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(dataset), "other", user)
    db.set_version_file_path(other_version, str(dataset))
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, structured_datasets_root=datasets, vector_store=vector)
    assert target.is_file() and dataset.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)
    assert db.get_structured_dataset_for_version(version) is not None


def test_other_document_reference_with_parent_component_protects_target(case):
    db, user, doc, _, root, target = case
    reference = root / ".." / root.name / target.name
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(reference), "other", user)
    db.set_version_file_path(other_version, str(reference))
    vector = Vector()
    with pytest.raises(RecycleError):
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert target.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


@pytest.mark.parametrize("root_layout", ["equal", "nested"])
def test_foreign_dataset_protects_version_target_with_overlapping_roots(case, root_layout):
    db, user, doc, version, root, original = case
    datasets = root if root_layout == "equal" else root / "datasets"
    other = db.create_document_permission("other.xlsx", "kb-1", user)
    other_version = db.create_document_version(other, "other.xlsx", str(root / "other.xlsx"), "other", user)
    dataset = resolve_dataset_path(datasets, other, other_version)
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"foreign dataset")
    dataset_id = db.create_structured_dataset(other, other_version, "xlsx", "row_groups")
    set_path(db, version, dataset)
    vector = Vector()
    with pytest.raises(RecycleError) as exc:
        purge_document(db, doc, now=10, versions_root=root, structured_datasets_root=datasets, vector_store=vector)
    assert exc.value.reason_code == "purge_path_shared"
    assert original.is_file() and dataset.read_bytes() == b"foreign dataset"
    assert vector.calls == []
    assert_retryable(db, doc)
    assert db.get_structured_dataset_for_version(other_version)["id"] == dataset_id


def test_foreign_dataset_without_root_fails_before_side_effects(case):
    db, user, doc, _, root, target = case
    other = db.create_document_permission("other.xlsx", "kb-1", user)
    other_version = db.create_document_version(other, "other.xlsx", str(root / "other.xlsx"), "other", user)
    dataset = resolve_dataset_path(root, other, other_version)
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"foreign dataset")
    db.create_structured_dataset(other, other_version, "xlsx", "row_groups")
    vector = Vector()
    with pytest.raises(RecycleError) as exc:
        purge_document(db, doc, now=10, versions_root=root, vector_store=vector)
    assert exc.value.reason_code == "purge_datasets_root_missing"
    assert target.is_file() and dataset.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)


def test_foreign_version_protects_dataset_target_with_equal_roots(case):
    db, user, doc, version, root, target = case
    dataset = resolve_dataset_path(root, doc, version)
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"own dataset")
    db.create_structured_dataset(doc, version, "xlsx", "row_groups")
    other = db.create_document_permission("other.pdf", "kb-1", user)
    other_version = db.create_document_version(other, "other.pdf", str(dataset), "other", user)
    db.set_version_file_path(other_version, str(dataset))
    vector = Vector()
    with pytest.raises(RecycleError) as exc:
        purge_document(db, doc, now=10, versions_root=root, structured_datasets_root=root, vector_store=vector)
    assert exc.value.reason_code == "purge_path_shared"
    assert target.is_file() and dataset.is_file()
    assert vector.calls == []
    assert_retryable(db, doc)
    assert db.get_structured_dataset_for_version(version) is not None
