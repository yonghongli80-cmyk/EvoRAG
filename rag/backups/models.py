from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
from typing import Mapping

from config import Settings


class BackupError(Exception):
    """Stable, non-sensitive backup failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BackupContext:
    project_root: Path
    data_root: Path
    backup_root: Path
    databases: Mapping[str, Path]
    required_databases: frozenset[str]
    file_roots: Mapping[str, Path]
    interval_days: int = 7
    retention_count: int = 8
    check_interval_seconds: int = 3600

    def validate(self) -> BackupContext:
        backup = self.backup_root.resolve()
        sources = tuple(path.resolve() for path in self.file_roots.values()) + tuple(
            path.resolve().parent for path in self.databases.values()
        )
        if any(
            backup == source
            or backup.is_relative_to(source)
            or source.is_relative_to(backup)
            for source in sources
        ):
            raise BackupError("backup_root_overlaps_source")
        return replace(self, backup_root=backup)


@dataclass(frozen=True)
class DatabaseEntry:
    name: str
    path: str
    size: int
    sha256: str
    integrity: str
    required: bool


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupManifest:
    schema_version: int
    backup_id: str
    created_at: float
    app_version: str
    python_version: str
    trigger: str
    databases: tuple[DatabaseEntry, ...]
    files: tuple[FileEntry, ...]
    required_file_keys: tuple[str, ...]
    excluded: tuple[str, ...]
    total_files: int
    total_bytes: int
    qdrant_included: bool
    vector_rebuild_required: bool

    @property
    def file_keys(self) -> frozenset[str]:
        return frozenset(item.path for item in self.files)

    def to_json_bytes(self) -> bytes:
        return deterministic_json_bytes(asdict(self))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> BackupManifest:
        return parse_schema_one_manifest(payload)


@dataclass(frozen=True)
class BackupRecord:
    backup_id: str
    created_at: float
    status: str
    path: Path
    file_count: int
    database_count: int
    total_bytes: int
    vector_rebuild_required: bool


@dataclass(frozen=True)
class BackupVerification:
    backup_id: str
    status: str
    file_count: int
    database_count: int
    total_bytes: int
    vector_rebuild_required: bool


@dataclass(frozen=True)
class RestoreReport:
    backup_id: str
    status: str
    target: Path
    file_count: int
    database_count: int
    vector_rebuild_required: bool


def deterministic_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def parse_schema_one_manifest(payload: Mapping[str, object]) -> BackupManifest:
    if payload.get("schema_version") != 1:
        raise BackupError("unsupported_manifest_schema")
    try:
        return BackupManifest(
            schema_version=1,
            backup_id=str(payload["backup_id"]),
            created_at=float(payload["created_at"]),
            app_version=str(payload["app_version"]),
            python_version=str(payload["python_version"]),
            trigger=str(payload["trigger"]),
            databases=tuple(
                DatabaseEntry(**entry) for entry in payload["databases"]  # type: ignore[arg-type]
            ),
            files=tuple(FileEntry(**entry) for entry in payload["files"]),  # type: ignore[arg-type]
            required_file_keys=tuple(str(item) for item in payload["required_file_keys"]),  # type: ignore[union-attr]
            excluded=tuple(str(item) for item in payload["excluded"]),  # type: ignore[union-attr]
            total_files=int(payload["total_files"]),
            total_bytes=int(payload["total_bytes"]),
            qdrant_included=bool(payload["qdrant_included"]),
            vector_rebuild_required=bool(payload["vector_rebuild_required"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BackupError("invalid_manifest") from error


def context_from_settings(configured: Settings | None = None) -> BackupContext:
    if configured is None:
        from config import settings

        configured = settings

    users_db_path = Path(configured.users_db_path)
    data_root = users_db_path.parent
    context = BackupContext(
        project_root=Path(__file__).resolve().parents[2],
        data_root=data_root,
        backup_root=Path(configured.backup_root),
        databases={
            "users": users_db_path,
            "analysis": Path(configured.analysis_db_path),
            "memory": Path(configured.memory_db_path),
        },
        required_databases=frozenset({"users"}),
        file_roots={
            "upload": data_root / "upload",
            "kb_files": data_root / "kb_files",
            "document_versions": Path(configured.document_versions_root),
            "structured_datasets": Path(configured.structured_datasets_root),
        },
        interval_days=configured.backup_interval_days,
        retention_count=configured.backup_retention_count,
        check_interval_seconds=configured.backup_check_interval_seconds,
    )
    return context.validate()
