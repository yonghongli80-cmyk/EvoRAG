"""Directory lookup and cleanup-root ordering regressions."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from rag.kb_lifecycle_cleanup import _path_references_cleanup_target, collect_kb_cleanup


class _CountingSet(set):
    """Record directory iteration separately from direct membership checks."""

    def __init__(self, values=()):
        super().__init__(values)
        self.iterations = 0
        self.elements_visited = 0
        self.membership_checks = 0

    def __iter__(self):
        self.iterations += 1
        for value in super().__iter__():
            self.elements_visited += 1
            yield value

    def __contains__(self, value):
        self.membership_checks += 1
        return super().__contains__(value)


def _legacy_path_references_cleanup_target(
    stored_path: str,
    *,
    default_root: Path,
    planned_paths: set[Path],
    planned_dirs: set[Path],
) -> bool:
    """Preserve the old all-directory predicate as an independent oracle."""
    if not stored_path:
        return False
    candidate = Path(stored_path)
    if not candidate.is_absolute():
        candidate = Path(default_root) / candidate
    try:
        candidates = {candidate.absolute(), candidate.resolve()}
    except (OSError, RuntimeError):
        candidates = {candidate.absolute()}
    for reference in candidates:
        if reference in planned_paths:
            return True
        if any(reference.is_relative_to(directory) for directory in planned_dirs):
            return True
    return False


@pytest.mark.parametrize("directory_count", [1, 16, 64])
def test_negative_reference_lookup_matches_legacy_without_scanning_all_directories(tmp_path, directory_count):
    directories = [tmp_path / f"planned-{index}" for index in range(directory_count)]
    scanned = _CountingSet(directories)
    stored_path = str(tmp_path / "unrelated" / "deep" / "file.txt")
    planned_paths: set[Path] = set()

    expected = _legacy_path_references_cleanup_target(
        stored_path,
        default_root=tmp_path,
        planned_paths=planned_paths,
        planned_dirs=set(directories),
    )
    actual = _path_references_cleanup_target(
        stored_path,
        default_root=tmp_path,
        planned_paths=planned_paths,
        planned_dirs=scanned,
    )

    assert actual is expected is False
    # The old implementation performs one full directory scan for each unique
    # absolute/resolve candidate; the indexed implementation should use parents.
    assert scanned.elements_visited == 0


def test_reference_lookup_relationships_match_legacy_for_path_boundaries(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    child = root / "nested"
    cases = [
        (str(root), root),  # directory itself
        (str(child / "file.txt"), root),  # descendant
        (str(root / "file.txt"), Path(tmp_path.anchor)),  # filesystem root
        (str(tmp_path / "owned-sibling" / "file.txt"), root),  # sibling prefix
        ("nested/file.txt", tmp_path / "nested"),  # relative positive
        ("nested/../file.txt", tmp_path),  # point-segment positive after resolve
        ("nested/../file.txt", tmp_path / "nested"),  # point-segment negative
        (str(root / "file.txt").swapcase(), root),  # platform Path case behavior
    ]
    for stored_path, planned_dir in cases:
        planned_dirs = {planned_dir}
        planned_paths: set[Path] = set()
        expected = _legacy_path_references_cleanup_target(
            stored_path,
            default_root=tmp_path,
            planned_paths=planned_paths,
            planned_dirs=planned_dirs,
        )
        actual = _path_references_cleanup_target(
            stored_path,
            default_root=tmp_path,
            planned_paths=planned_paths,
            planned_dirs=planned_dirs,
        )
        assert actual is expected, (stored_path, planned_dir)

    # The candidate's resolved identity can independently match a cleanup dir.
    alias_path = tmp_path / "alias" / "file.txt"
    resolved_candidate = root / "file.txt"
    original_resolve = Path.resolve

    def resolve_alias(path, *args, **kwargs):
        if path == alias_path:
            return resolved_candidate
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_alias)
    expected = _legacy_path_references_cleanup_target(
        str(alias_path),
        default_root=tmp_path,
        planned_paths=set(),
        planned_dirs={root},
    )
    actual = _path_references_cleanup_target(
        str(alias_path),
        default_root=tmp_path,
        planned_paths=set(),
        planned_dirs={root},
    )
    assert actual is expected is True


def test_reference_lookup_keeps_file_priority_and_resolve_failure_fallback(tmp_path, monkeypatch):
    target = tmp_path / "planned" / "file.txt"
    planned_dirs = _CountingSet({tmp_path / "planned"})
    assert _path_references_cleanup_target(
        str(target),
        default_root=tmp_path,
        planned_paths={target},
        planned_dirs=planned_dirs,
    )
    assert planned_dirs.iterations == 0

    failing_path = tmp_path / "planned" / "fallback.txt"
    original_resolve = Path.resolve

    def fail_for_candidate(path, *args, **kwargs):
        if path == failing_path:
            raise OSError("synthetic resolve failure")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_for_candidate)
    assert _path_references_cleanup_target(
        str(failing_path),
        default_root=tmp_path,
        planned_paths=set(),
        planned_dirs={tmp_path / "planned"},
    )


def _legacy_minimal_cleanup_dirs(paths: list[Path]) -> list[Path]:
    materialized = sorted(set(paths), key=lambda path: len(path.parts))
    return [
        path
        for path in materialized
        if not any(parent != path and path.is_relative_to(parent) for parent in materialized)
    ]


def test_minimal_cleanup_dirs_matches_legacy_order_and_strict_parent_rules(tmp_path):
    parent = tmp_path / "stage"
    paths = [
        parent / "nested" / "leaf",
        tmp_path / "stage-sibling",
        parent,
        parent,
        Path("relative") / "nested",
        Path("relative"),
        Path("relative") / ".." / "odd",
    ]
    from rag.kb_lifecycle_cleanup import _minimal_cleanup_dirs

    assert _minimal_cleanup_dirs(paths) == _legacy_minimal_cleanup_dirs(paths)


class _ReadConnection:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def read_connection(self):
        yield self.connection


class _EmptyCleanupDb:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.cm = _ReadConnection(self.connection)
        self.connection.executescript(
            """
            CREATE TABLE structured_datasets (id INTEGER, document_id INTEGER, document_version_id INTEGER);
            CREATE TABLE document_versions (id INTEGER, document_id INTEGER, file_path TEXT);
            CREATE TABLE material_archive_manifests (
                id INTEGER, container_document_id INTEGER, container_version_id INTEGER, source_path TEXT
            );
            """
        )

    def list_document_permissions_all(self):
        return []

    def list_kb_documents(self, _kb_id):
        return []


def test_collect_uses_minimal_cleanup_directory_helper(tmp_path, monkeypatch):
    import rag.kb_lifecycle_cleanup as cleanup

    versions_root = tmp_path / "versions"
    kb_hash = hashlib.sha256(b"target-kb").hexdigest()
    stage = versions_root / "staging" / "kb" / kb_hash
    stage.mkdir(parents=True)
    called_with = []
    original_helper = cleanup._minimal_cleanup_dirs

    def recording_helper(paths):
        called_with.append(list(paths))
        return original_helper(paths)

    monkeypatch.setattr(cleanup, "_minimal_cleanup_dirs", recording_helper, raising=False)
    db = _EmptyCleanupDb()
    try:
        plan = collect_kb_cleanup(db, "target-kb", versions_root, tmp_path / "kb-files", tmp_path / "datasets")
    finally:
        db.connection.close()

    assert called_with == [[stage.absolute()]]
    assert plan["cleanup_dirs"] == [stage.absolute()]
