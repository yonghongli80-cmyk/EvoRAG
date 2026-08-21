import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Settings
from rag.backups.models import BackupError, BackupManifest, context_from_settings
from tests.backup_fixtures import make_backup_context, make_manifest


def test_backup_settings_have_weekly_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RAG_BACKUP_ENABLED", raising=False)
    configured = Settings(_env_file=None)

    assert configured.backup_enabled is True
    assert configured.backup_interval_days == 7
    assert configured.backup_retention_count == 8
    assert configured.backup_check_interval_seconds == 3600
    assert Path(configured.backup_root).name == "RAGv3-backups"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("backup_interval_days", 0),
        ("backup_retention_count", 0),
        ("backup_check_interval_seconds", 0),
    ],
)
def test_backup_settings_reject_non_positive_values(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_context_maps_only_authoritative_sources(tmp_path):
    context = make_backup_context(tmp_path)

    assert set(context.databases) == {"users", "analysis", "memory"}
    assert context.required_databases == frozenset({"users"})
    assert set(context.file_roots) == {
        "upload",
        "kb_files",
        "document_versions",
        "structured_datasets",
    }

    serialized = make_manifest().to_json_bytes()
    assert b'"qdrant_included":false' in serialized
    assert b"absolute" not in serialized


def test_manifest_json_is_deterministic_and_round_trips_tuple_fields():
    manifest = make_manifest()

    serialized = manifest.to_json_bytes()
    assert serialized == json.dumps(
        asdict(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    restored = BackupManifest.from_dict(json.loads(serialized))
    assert restored == manifest
    assert isinstance(restored.databases, tuple)
    assert isinstance(restored.files, tuple)
    assert isinstance(restored.required_file_keys, tuple)
    assert restored.file_keys == frozenset({"upload/example.txt"})


@pytest.mark.parametrize("source_kind", ["file_root", "database_parent"])
@pytest.mark.parametrize("direction", ["inside", "contains"])
def test_context_rejects_backup_root_overlap_in_both_directions(
    tmp_path, source_kind, direction
):
    context = make_backup_context(tmp_path)
    source = (
        context.file_roots["upload"]
        if source_kind == "file_root"
        else context.databases["users"].parent
    )
    backup_root = source / "backups" if direction == "inside" else source.parent

    with pytest.raises(BackupError, match="backup_root_overlaps_source") as error:
        replace(context, backup_root=backup_root).validate()

    assert error.value.code == "backup_root_overlaps_source"


def test_context_from_settings_maps_paths_without_reading_them(tmp_path):
    project_root = tmp_path / "project"
    data_root = project_root / "data"
    configured = Settings(
        _env_file=None,
        users_db_path=str(data_root / "users.db"),
        analysis_db_path=str(data_root / "analysis.db"),
        memory_db_path=str(data_root / "memory.db"),
        document_versions_root=str(data_root / "versions"),
        structured_datasets_root=str(data_root / "datasets"),
        backup_root=str(tmp_path / "backups"),
        backup_interval_days=9,
        backup_retention_count=4,
        backup_check_interval_seconds=120,
    )

    context = context_from_settings(configured)

    assert context.project_root == Path(__file__).resolve().parents[1]
    assert context.data_root == data_root
    assert context.databases["users"] == data_root / "users.db"
    assert context.file_roots["upload"] == data_root / "upload"
    assert context.file_roots["kb_files"] == data_root / "kb_files"
    assert context.file_roots["document_versions"] == data_root / "versions"
    assert context.file_roots["structured_datasets"] == data_root / "datasets"
    assert context.interval_days == 9
    assert context.retention_count == 4
    assert context.check_interval_seconds == 120
    assert not data_root.exists()
