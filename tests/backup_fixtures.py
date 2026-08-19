from dataclasses import replace
from pathlib import Path

from rag.backups.models import (
    BackupContext,
    BackupManifest,
    DatabaseEntry,
    FileEntry,
)


def make_backup_context(tmp_path: Path) -> BackupContext:
    project_root = tmp_path / "RAGv3"
    data_root = project_root / "data"
    return BackupContext(
        project_root=project_root,
        data_root=data_root,
        backup_root=tmp_path / "RAGv3-backups",
        databases={
            "users": data_root / "users.db",
            "analysis": data_root / "analysis.db",
            "memory": data_root / "memory.db",
        },
        required_databases=frozenset({"users"}),
        file_roots={
            "upload": data_root / "upload",
            "kb_files": data_root / "kb_files",
            "document_versions": data_root / "document_versions",
            "structured_datasets": data_root / "structured_datasets",
        },
    ).validate()


def make_manifest(
    backup_id: str = "backup-1",
    created_at: float = 1_777_000_000.0,
) -> BackupManifest:
    return BackupManifest(
        schema_version=1,
        backup_id=backup_id,
        created_at=created_at,
        app_version="test",
        python_version="3.12",
        trigger="manual",
        databases=(
            DatabaseEntry(
                name="users",
                path="databases/users.db",
                size=12,
                sha256="a" * 64,
                integrity="ok",
                required=True,
            ),
        ),
        files=(
            FileEntry(path="upload/example.txt", size=7, sha256="b" * 64),
        ),
        required_file_keys=("upload/example.txt",),
        excluded=("qdrant", "bm25", "embedding_cache"),
        total_files=2,
        total_bytes=19,
        qdrant_included=False,
        vector_rebuild_required=True,
    )


def context_with_backup_root(context: BackupContext, backup_root: Path) -> BackupContext:
    return replace(context, backup_root=backup_root)
