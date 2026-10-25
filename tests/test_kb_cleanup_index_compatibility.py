"""Compatibility checks for the KB cleanup plan snapshot."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from rag.kb_lifecycle import KBDeleteIncomplete
from rag.kb_lifecycle_cleanup import collect_kb_cleanup

FIXTURE = Path(__file__).parent / "fixtures" / "kb_cleanup_index_expected.json"
KB_ID = "compat-target"
OTHER_KB = "compat-external"
OWNER_ID = 7


class _ConnectionManager:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @contextmanager
    def read_connection(self):
        yield self.connection


class _SqliteDuckAdapter:
    """Small adapter that executes the same SELECT shapes as production cleanup."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.cm = _ConnectionManager(connection)

    def list_document_permissions_all(self):
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id, filename, kb_id, owner_id FROM document_permissions ORDER BY id"
            ).fetchall()
        ]

    def list_kb_documents(self, kb_id: str):
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id, kb_id, filename, file_path FROM kb_documents WHERE kb_id = ? ORDER BY id",
                (kb_id,),
            ).fetchall()
        ]

    def get_document_permission(self, filename: str, kb_id: str):
        row = self.connection.execute(
            "SELECT id, filename, kb_id, owner_id FROM document_permissions "
            "WHERE filename = ? AND kb_id = ? ORDER BY id LIMIT 1",
            (filename, kb_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_document_versions(self, document_id: int):
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT id, document_id, version, file_path FROM document_versions "
                "WHERE document_id = ? ORDER BY version DESC, id DESC",
                (document_id,),
            ).fetchall()
        ]


def _create_database(path: Path) -> tuple[sqlite3.Connection, _SqliteDuckAdapter]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE document_permissions (
            id INTEGER PRIMARY KEY, filename TEXT, kb_id TEXT, owner_id INTEGER
        );
        CREATE TABLE document_versions (
            id INTEGER PRIMARY KEY, document_id INTEGER, version INTEGER, file_path TEXT
        );
        CREATE TABLE kb_documents (
            id INTEGER PRIMARY KEY, kb_id TEXT, filename TEXT, file_path TEXT
        );
        CREATE TABLE structured_datasets (
            id INTEGER PRIMARY KEY, document_id INTEGER, document_version_id INTEGER
        );
        CREATE TABLE material_archive_manifests (
            id INTEGER PRIMARY KEY, container_document_id INTEGER,
            container_version_id INTEGER, source_path TEXT
        );
        """
    )
    return connection, _SqliteDuckAdapter(connection)


def _base_case(tmp_path: Path, case: str = "complete"):
    versions_root = tmp_path / "versions"
    kb_files_root = tmp_path / "kb-files"
    datasets_root = tmp_path / "datasets"
    versions_root.mkdir()
    kb_files_root.mkdir()
    datasets_root.mkdir()
    connection, db = _create_database(tmp_path / "synthetic.sqlite3")
    target_docs = [
        (11, "material.zip", "material"),
        (12, "table.xlsx", "table"),
        (13, "missing.txt", "missing"),
    ]
    external_docs = [(90, "external.txt", "external")]
    connection.executemany(
        "INSERT INTO document_permissions VALUES (?, ?, ?, ?)",
        [(doc_id, filename, KB_ID, OWNER_ID) for doc_id, filename, _ in target_docs]
        + [(doc_id, filename, OTHER_KB, OWNER_ID) for doc_id, filename, _ in external_docs],
    )

    def write_version(doc_id: int, version: int, filename: str, *, present=True):
        path = versions_root / str(doc_id) / str(version) / filename
        if present:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"version:{doc_id}:{version}".encode())
        connection.execute(
            "INSERT INTO document_versions VALUES (?, ?, ?, ?)",
            (doc_id * 100 + version, doc_id, version, str(path)),
        )
        return path

    material_v1 = write_version(11, 1, "material-v1.zip")
    write_version(11, 2, "material-v2.zip")
    write_version(12, 1, "table.xlsx")
    missing_path = write_version(13, 1, "absent.txt", present=False)
    external_version = write_version(90, 1, "external.txt")

    for row_id in (301, 302):
        duplicate = kb_files_root / KB_ID / str(row_id) / "material.zip"
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(f"directory-row:{row_id}".encode())
        connection.execute(
            "INSERT INTO kb_documents VALUES (?, ?, ?, ?)",
            (row_id, KB_ID, "material.zip", str(duplicate)),
        )
    legacy = kb_files_root / KB_ID / "303" / "legacy.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    connection.execute(
        "INSERT INTO kb_documents VALUES (?, ?, ?, ?)",
        (303, KB_ID, "legacy.txt", str(legacy)),
    )
    dataset = datasets_root / "12" / "1201" / "dataset.sqlite3"
    dataset.parent.mkdir(parents=True)
    dataset.write_bytes(b"synthetic dataset")
    connection.execute("INSERT INTO structured_datasets VALUES (501, 12, 1201)")
    kb_hash = hashlib.sha256(KB_ID.encode("utf-8")).hexdigest()
    material_stage = versions_root / "staging" / "kb" / kb_hash
    material_stage.mkdir(parents=True)
    (material_stage / "partial.bin").write_bytes(b"staged material")
    archive_source = material_stage / "source.zip"
    archive_source.write_bytes(b"staged archive source")
    document_stage = versions_root / "staging" / "13"
    document_stage.mkdir(parents=True)
    (document_stage / "retry.tmp").write_bytes(b"staged document")
    connection.execute(
        "INSERT INTO material_archive_manifests VALUES (701, 11, 1102, ?)",
        (str(archive_source),),
    )
    connection.execute(
        "INSERT INTO material_archive_manifests VALUES (799, 90, 9001, ?)",
        (str(external_version),),
    )

    if case == "wrong_owner":
        connection.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = 1101",
            (str(versions_root / "90" / "1" / "external.txt"),),
        )
    elif case == "shared_file":
        connection.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = 9001",
            (str(material_v1),),
        )
    elif case == "shared_directory":
        connection.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = 9001",
            (str(material_stage / "outsider.bin"),),
        )
    elif case == "shared_identity":
        connection.execute(
            "INSERT INTO material_archive_manifests VALUES (702, 90, 1101, ?)",
            (str(versions_root / "90" / "1" / "external.txt"),),
        )
    elif case == "shared_manifest_file":
        connection.execute(
            "INSERT INTO material_archive_manifests VALUES (702, 90, 9001, ?)",
            (str(material_v1),),
        )
    elif case == "shared_manifest_directory":
        connection.execute(
            "INSERT INTO material_archive_manifests VALUES (702, 90, 9001, ?)",
            (str(material_stage),),
        )
    elif case == "wrong_manifest_identity":
        connection.execute("UPDATE material_archive_manifests SET container_version_id = 9001 WHERE id = 701")
    elif case == "archive_priority":
        outside_archive = tmp_path / "outside.zip"
        outside_archive.write_bytes(b"unowned")
        connection.execute(
            "UPDATE material_archive_manifests SET source_path = ? WHERE id = 701",
            (str(outside_archive),),
        )
        connection.execute(
            "UPDATE document_versions SET file_path = ? WHERE id = 1101",
            (str(versions_root / "90" / "1" / "external.txt"),),
        )
    elif case == "directory_as_file":
        bad = versions_root / "11" / "1" / "bad-dir"
        bad.mkdir(parents=True)
        connection.execute("UPDATE document_versions SET file_path = ? WHERE id = 1101", (str(bad),))
    elif case == "link":
        if not hasattr(Path, "symlink_to"):
            pytest.fail("Path.symlink_to is required for the fail-closed link case")
        link = versions_root / "11" / "1" / "linked.zip"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(material_v1)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"cannot create the required symlink security fixture: {exc}")
        connection.execute("UPDATE document_versions SET file_path = ? WHERE id = 1101", (str(link),))

    connection.commit()
    roots = (versions_root, kb_files_root, datasets_root)
    return db, connection, roots, missing_path


def _normalize(value, roots):
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, dict):
        return {key: _normalize(item, roots) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, roots) for item in value]
    if isinstance(value, str):
        for root, token in zip(roots, ("$VERSIONS", "$KB_FILES", "$DATASETS"), strict=True):
            value = value.replace(str(root), token)
        return value
    return value


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
        elif path.is_symlink():
            digest.update(str(path.readlink()).encode())
    return digest.hexdigest()


def _state_digest(connection: sqlite3.Connection, roots) -> tuple[str, ...]:
    database_hash = hashlib.sha256(connection.serialize()).hexdigest()
    return (database_hash, *(_tree_digest(root) for root in roots))


def test_complete_cleanup_plan_matches_frozen_golden_without_side_effects(tmp_path):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db, connection, roots, _missing_path = _base_case(tmp_path)
    before = _state_digest(connection, roots)
    plan = collect_kb_cleanup(db, KB_ID, *roots)
    after = _state_digest(connection, roots)
    connection.close()

    expected = fixture["complete_plan"]
    actual = _normalize(plan, roots)
    # cleanup_dirs keeps the old Path/set hash iteration order within this process;
    # all other values and list orders are compared directly with the frozen JSON.
    actual_dirs = actual.pop("cleanup_dirs")
    expected_dirs = [Path(str(item).replace("$VERSIONS", str(roots[0]))) for item in expected.pop("cleanup_dirs")]
    materialized = sorted(set(expected_dirs), key=lambda path: len(path.parts))
    expected_dirs = [
        path
        for path in materialized
        if not any(parent != path and path.is_relative_to(parent) for parent in materialized)
    ]
    assert actual_dirs == _normalize(expected_dirs, roots)
    assert actual == expected
    assert after == before


@pytest.mark.parametrize(
    ("case", "error_code"),
    [
        ("wrong_owner", "version_path_unowned"),
        ("shared_file", "cleanup_path_shared"),
        ("shared_directory", "cleanup_path_shared"),
        ("shared_identity", "cleanup_path_shared"),
        ("shared_manifest_file", "cleanup_path_shared"),
        ("shared_manifest_directory", "cleanup_path_shared"),
        ("wrong_manifest_identity", "archive_identity_mismatch"),
        ("archive_priority", "archive_source_unowned"),
        ("link", "version_path_unowned"),
        ("directory_as_file", "version_path_unowned"),
    ],
)
def test_rejections_match_frozen_error_contract_without_side_effects(tmp_path, case, error_code):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["rejections"][case] == error_code
    db, connection, roots, _missing_path = _base_case(tmp_path, case)
    before = _state_digest(connection, roots)
    with pytest.raises(KBDeleteIncomplete) as raised:
        collect_kb_cleanup(db, KB_ID, *roots)
    after = _state_digest(connection, roots)
    connection.close()
    assert raised.value.code == error_code
    assert after == before
