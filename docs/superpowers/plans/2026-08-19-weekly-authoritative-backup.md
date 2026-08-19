# Weekly Authoritative Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add weekly, locally stored, verifiable backups of RAGv3 authoritative SQLite and document data, with isolated restore testing and a deployment backup gate.

**Architecture:** A focused `rag.backups` package owns immutable contracts, safe path handling, snapshot/copy primitives, verification, publication, retention, and scheduling. The FastAPI lifespan only starts and stops the scheduler; a standalone CLI reuses the same service for manual and deployment operations. Qdrant and other derived caches are excluded and explicitly marked for rebuild after restore.

**Tech Stack:** Python 3.12 standard library (`sqlite3`, `hashlib`, `json`, `shutil`, `threading`, `pathlib`, `uuid`), Pydantic Settings, pytest, Docker Compose, Bash, GitHub Actions YAML.

**Spec:** `docs/superpowers/specs/2026-08-19-weekly-authoritative-backup-design.md`

## Global Constraints

- Strict TDD: every behavior test must be observed failing for the intended reason before production code is written.
- Tests use only `tmp_path`; never read, write, copy, lock, or hash production `data/`, `qdrant_data/`, `.env`, or a real backup root.
- Back up `users.db` as required; treat `analysis.db` and `memory.db` as optional.
- Back up logical roots `upload`, `kb_files`, `document_versions`, and `structured_datasets`; exclude transient `staging`, `quarantine`, and `archive_work` directories.
- Never include `.env`, credentials, JWT secrets, Qdrant, Redis, `bm25_index.db`, `embedding_cache.sqlite3`, charts, or `index_state.json`.
- Default cadence is exactly 7 days; default retention is exactly 8 verified backups; check interval is exactly 3600 seconds.
- Local default root is the sibling directory `RAGv3-backups`; Docker overrides it to `/app/backups` on a dedicated `backup_data` volume.
- A backup becomes visible only by atomic rename after complete verification. A failed creation never triggers retention cleanup.
- `restore_test` may write only to a new or empty `<isolation-root>/<backup-id>` and never to project, data, Qdrant, or backup source paths.
- SHA-256 detects accidental corruption only; do not describe it as authentication or encryption.
- At execution start, preserve the existing uncommitted startup/health fixes. Commit them separately before backup commits or execute in an isolated worktree based on a commit that contains them.

## File Map

- Create `rag/backups/__init__.py`: stable public exports only.
- Create `rag/backups/models.py`: dataclasses, manifest serialization, stable error codes.
- Create `rag/backups/service.py`: safe paths, SQLite snapshots, file copy, verification, publication, restore test, due calculation, and retention.
- Create `rag/backups/scheduler.py`: stoppable weekly checker with no backup algorithms.
- Create `scripts/backup.py`: CLI adapter over `rag.backups`.
- Create `tests/test_backup_models.py`: contracts and configuration mapping.
- Create `tests/backup_fixtures.py`: synthetic contexts, manifests, and test-only builders shared by backup tests.
- Create `tests/test_backup_service.py`: snapshots, copy integrity, publication, and verification.
- Create `tests/test_backup_restore.py`: isolation and restored-content validation.
- Create `tests/test_backup_schedule.py`: cadence, locking, retention, and scheduler behavior.
- Create `tests/test_backup_cli.py`: exit codes and JSON output.
- Create `tests/test_backup_deployment.py`: Compose volume and command-order invariants.
- Create `scripts/create_backup_smoke_fixture.py`: synthetic temporary fixture for the real CLI drill; refuses non-temporary targets.
- Modify `config.py`: typed backup settings.
- Modify `rag/api.py`: scheduler lifecycle only.
- Modify `tests/conftest.py`: disable the real scheduler before application imports; scheduler tests inject temporary contexts explicitly.
- Modify `docker-compose.yml`: dedicated backup volume and environment.
- Modify `Dockerfile`: create `/app/backups` before switching to the non-root `rag` user.
- Modify `deploy.sh`: deployment backup gate.
- Modify `.github/workflows/deploy.yml`: deployment backup gate.
- Modify `.gitignore` and `.dockerignore`: prevent local backup payloads entering source or image context.
- Modify `docs/deployment.md` and `.env.example`: operator instructions and settings.

---

### Task 1: Backup Contracts, Configuration, and Safe Context

**Files:**
- Create: `rag/backups/__init__.py`
- Create: `rag/backups/models.py`
- Create: `tests/test_backup_models.py`
- Modify: `config.py:65-80`

**Interfaces:**
- Consumes: `config.Settings` and explicit test paths.
- Produces: `BackupError`, `BackupContext`, `FileEntry`, `DatabaseEntry`, `BackupManifest`, `BackupRecord`, `BackupVerification`, `RestoreReport`, and `context_from_settings(configured: Settings | None = None) -> BackupContext`.

- [ ] **Step 1: Write failing configuration and serialization tests**

```python
from pathlib import Path

import pytest

from config import Settings
from dataclasses import replace

from rag.backups.models import BackupError

from tests.backup_fixtures import make_backup_context, make_manifest


def test_backup_settings_have_weekly_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    configured = Settings(_env_file=None)
    assert configured.backup_enabled is True
    assert configured.backup_interval_days == 7
    assert configured.backup_retention_count == 8
    assert configured.backup_check_interval_seconds == 3600
    assert Path(configured.backup_root).name == "RAGv3-backups"


def test_context_maps_only_authoritative_sources(tmp_path):
    context = make_backup_context(tmp_path)
    assert set(context.databases) == {"users", "analysis", "memory"}
    assert context.required_databases == frozenset({"users"})
    assert set(context.file_roots) == {
        "upload", "kb_files", "document_versions", "structured_datasets"
    }
    serialized = make_manifest("backup-1", 1_777_000_000.0).to_json_bytes()
    assert b"qdrant_included" in serialized
    assert b"absolute" not in serialized


def test_context_rejects_backup_root_inside_source_data(tmp_path):
    context = make_backup_context(tmp_path)
    with pytest.raises(BackupError, match="backup_root_overlaps_source"):
        replace(context, backup_root=context.file_roots["upload"]).validate()
```

- [ ] **Step 2: Run tests and confirm RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_models.py -v
```

Expected: collection fails because `rag.backups` and the backup settings do not exist.

- [ ] **Step 3: Implement exact contracts and defaults**

Import `Field` from Pydantic and add these exact settings:

```python
backup_enabled: bool = True
backup_root: str = str(Path(__file__).resolve().parent.parent / "RAGv3-backups")
backup_interval_days: int = Field(default=7, ge=1)
backup_retention_count: int = Field(default=8, ge=1)
backup_check_interval_seconds: int = Field(default=3600, ge=1)
```

Implement these exact immutable contracts; tuple fields serialize as JSON arrays and parse back to tuples:

```python
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

    def validate(self) -> "BackupContext":
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
    def from_dict(cls, payload: Mapping[str, object]) -> "BackupManifest":
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
```

Define `BackupError` exactly as `BackupError(code: str)` with a public `code` attribute and no raw exception/path interpolation. `context_from_settings(configured=None)` imports the module-level settings only when `configured` is `None`, derives `data_root` from the parent of `users_db_path`, maps `upload` and `kb_files` below it, uses the configured document-version and structured-dataset roots, and calls `validate()` before returning.

The manifest serializer must use:

```python
json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

`BackupContext.validate()` must reject overlap in either direction between the backup root and every database parent or file root. `context_from_settings()` maps paths without reading their contents.

Create `tests/backup_fixtures.py` with `make_backup_context(tmp_path)` and reusable synthetic-data builders. Test-only construction helpers must remain in this file and must not become production class methods.

- [ ] **Step 4: Run model tests and critical lint**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_models.py -v
& .\.venv\Scripts\ruff.exe check rag\backups\models.py tests\test_backup_models.py config.py --select E9,F63,F7,F82
```

Expected: all tests pass and Ruff reports no selected errors.

- [ ] **Step 5: Commit Task 1**

```powershell
git add config.py rag/backups/__init__.py rag/backups/models.py tests/backup_fixtures.py tests/test_backup_models.py
git commit -m "feat: define authoritative backup contracts"
```

---

### Task 2: SQLite Snapshot and Verified File-Copy Primitives

**Files:**
- Create: `rag/backups/service.py`
- Create: `tests/test_backup_service.py`

**Interfaces:**
- Consumes: `BackupContext`, `DatabaseEntry`, `FileEntry`, `BackupError` from Task 1.
- Produces: `snapshot_sqlite(source: Path, target: Path) -> DatabaseEntry`, `copy_file_verified(source: Path, target: Path, logical_path: str) -> FileEntry`, `sha256_file(path: Path) -> str`.

- [ ] **Step 1: Write failing SQLite snapshot tests**

```python
import sqlite3

from rag.backups.service import snapshot_sqlite


def test_snapshot_sqlite_uses_a_consistent_database_copy(tmp_path):
    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE notes(value TEXT NOT NULL)")
    writer.execute("INSERT INTO notes VALUES ('before')")
    writer.commit()

    entry = snapshot_sqlite(source, tmp_path / "snapshot.db")
    writer.execute("INSERT INTO notes VALUES ('after')")
    writer.commit()
    writer.close()

    restored = sqlite3.connect(tmp_path / "snapshot.db")
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT value FROM notes").fetchall() == [("before",)]
    restored.close()
    assert entry.integrity == "ok"
```

- [ ] **Step 2: Run the snapshot test and confirm RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py::test_snapshot_sqlite_uses_a_consistent_database_copy -v
```

Expected: import fails because `snapshot_sqlite` does not exist.

- [ ] **Step 3: Implement SQLite snapshot with the backup API**

The implementation must open the source in URI read-only mode, create the destination exclusively, call `source_connection.backup(target_connection)`, close both in `finally`, then independently open the destination read-only for `PRAGMA integrity_check`. It must never copy `-wal` or `-shm` files.

```python
source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
    with sqlite3.connect(target) as target_db:
        source_db.backup(target_db)
```

Map missing required DBs to `BackupError("required_database_missing")`, snapshot failures to `BackupError("sqlite_snapshot_failed")`, and non-`ok` integrity to `BackupError("sqlite_integrity_failed")`.

- [ ] **Step 4: Write failing mutation and symlink copy tests**

```python
from pathlib import Path
import shutil

import pytest

from rag.backups.models import BackupError
from rag.backups.service import copy_file_verified


def test_copy_rejects_source_that_changes_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    real_copy = shutil.copyfile

    def copy_then_mutate(src, dst):
        result = real_copy(src, dst)
        Path(src).write_text("after", encoding="utf-8")
        return result

    monkeypatch.setattr("rag.backups.service.shutil.copyfile", copy_then_mutate)
    with pytest.raises(BackupError, match="source_changed_during_copy"):
        copy_file_verified(source, tmp_path / "target.txt", "upload/source.txt")


def test_copy_rejects_symbolic_links(tmp_path):
    source = tmp_path / "real.txt"
    source.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symbolic links are unavailable for this Windows account")
    with pytest.raises(BackupError, match="symbolic_link_rejected"):
        copy_file_verified(link, tmp_path / "target.txt", "upload/link.txt")
```

- [ ] **Step 5: Run copy tests and confirm RED**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py -k "copy" -v
```

Expected: tests fail because verified copy behavior is missing.

- [ ] **Step 6: Implement verified copy**

Reject symlinks before resolving. Capture `(size, mtime_ns)` and source SHA-256 before copying, use `shutil.copyfile`, recapture source metadata and SHA-256, and hash the target. Raise `source_changed_during_copy` unless both source observations match and the target hash equals the first source hash. Return a `FileEntry` containing only the supplied logical path, size, and hash.

- [ ] **Step 7: Run Task 2 tests and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py -v
& .\.venv\Scripts\ruff.exe check rag\backups\service.py tests\test_backup_service.py --select E9,F63,F7,F82
git add rag/backups/service.py tests/test_backup_service.py
git commit -m "feat: snapshot sqlite and copy backup files safely"
```

---

### Task 3: Manifest Verification and Corruption Rejection

**Files:**
- Modify: `rag/backups/service.py`
- Modify: `tests/test_backup_service.py`

**Interfaces:**
- Consumes: deterministic `BackupManifest` serialization and Task 2 primitives.
- Produces: `write_manifest(staging: Path, manifest: BackupManifest) -> None`, `verify_backup(backup_dir: Path) -> BackupVerification`.

- [ ] **Step 1: Write failing verification tests**

Create a helper in the test file that writes one database, one data file, a literal schema-v1 manifest, and `manifest.sha256`. Then add these tests:

```python
def test_verify_accepts_complete_backup(complete_backup):
    result = verify_backup(complete_backup)
    assert result.status == "verified"
    assert result.file_count == 1
    assert result.database_count == 1


def test_verify_rejects_changed_file(complete_backup):
    (complete_backup / "files/upload/guide.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(BackupError, match="file_hash_mismatch"):
        verify_backup(complete_backup)


def test_verify_rejects_corrupt_sqlite(complete_backup):
    (complete_backup / "sqlite/users.db").write_bytes(b"not sqlite")
    with pytest.raises(BackupError, match="sqlite_integrity_failed"):
        verify_backup(complete_backup)


def test_verify_rejects_unlisted_database_payload(complete_backup):
    (complete_backup / "sqlite/rogue.db").write_bytes(b"unexpected")
    with pytest.raises(BackupError, match="unexpected_backup_file"):
        verify_backup(complete_backup)


@pytest.mark.parametrize("logical_path", ["../escape.txt", "/absolute.txt", "upload/../escape.txt"])
def test_verify_rejects_unsafe_manifest_paths(complete_backup, logical_path):
    rewrite_manifest_file_path(complete_backup, logical_path)
    with pytest.raises(BackupError, match="unsafe_manifest_path"):
        verify_backup(complete_backup)
```

- [ ] **Step 2: Run verification tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py -k "verify" -v
```

Expected: tests fail because `verify_backup` does not exist.

- [ ] **Step 3: Implement verification in fail-closed order**

Implement these exact stages: resolve backup directory; reject symlinks; load at most 4 MiB of `manifest.json`; require `manifest.sha256` to contain exactly 64 lowercase hexadecimal characters plus an optional final newline; validate the checksum; parse schema 1 through `BackupManifest.from_dict`; normalize every logical path with `PurePosixPath`; reject absolute paths, `..`, duplicates, and backslashes; require exact listed DB/file size and hash; reject unlisted files below `sqlite/` or `files/`; run SQLite integrity; confirm all `required_file_keys` exist; return `BackupVerification(status="verified", ...)` only after every stage succeeds. The exact optional `restore-report.json` at the restored backup root is allowed and ignored by payload verification.

Do not accept unknown schema versions. Errors expose stable codes and never absolute paths.

- [ ] **Step 4: Run verification tests and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py -v
git add rag/backups/service.py tests/test_backup_service.py
git commit -m "feat: verify backup manifests and payload integrity"
```

---

### Task 4: Atomic Backup Creation and Publication

**Files:**
- Modify: `rag/backups/service.py`
- Modify: `tests/test_backup_service.py`

**Interfaces:**
- Consumes: `snapshot_sqlite`, `copy_file_verified`, `write_manifest`, `verify_backup`.
- Produces: `create_backup(context: BackupContext, trigger: str, now: float | None = None) -> BackupRecord`.

- [ ] **Step 1: Write failing success and no-publication tests**

```python
def test_create_backup_publishes_only_a_verified_directory(backup_context):
    record = create_backup(backup_context, "manual", now=1_777_000_000.0)
    assert record.status == "verified"
    assert record.path.name.startswith("20260425T")
    assert not list(backup_context.backup_root.glob(".incomplete-*"))
    assert verify_backup(record.path).status == "verified"


def test_failed_copy_never_publishes_backup(backup_context, monkeypatch):
    monkeypatch.setattr(
        "rag.backups.service.copy_file_verified",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("copy_failed")),
    )
    with pytest.raises(BackupError, match="copy_failed"):
        create_backup(backup_context, "manual", now=1_777_000_000.0)
    assert list_verified_backups(backup_context.backup_root) == ()
    assert not list(backup_context.backup_root.glob(".incomplete-*"))


def test_create_backup_excludes_transient_and_derived_data(backup_context):
    create_fixture_file(backup_context.file_roots["document_versions"] / "staging/tmp.bin")
    create_fixture_file(backup_context.file_roots["document_versions"] / "7/1/current.pdf")
    create_fixture_file(backup_context.data_root / "bm25_index.db")
    record = create_backup(backup_context, "manual", now=1_777_000_000.0)
    manifest = load_manifest(record.path)
    assert "document_versions/7/1/current.pdf" in manifest.file_keys
    assert "document_versions/staging/tmp.bin" not in manifest.file_keys
    assert all("bm25" not in key for key in manifest.file_keys)
    assert manifest.qdrant_included is False
    assert manifest.vector_rebuild_required is True


def test_create_requires_users_database_but_allows_optional_databases(backup_context):
    backup_context.databases["analysis"].unlink(missing_ok=True)
    backup_context.databases["memory"].unlink(missing_ok=True)
    assert create_backup(backup_context, "manual", now=1_777_000_000.0).status == "verified"
    backup_context.databases["users"].unlink()
    with pytest.raises(BackupError, match="required_database_missing"):
        create_backup(backup_context, "manual", now=1_777_604_800.0)
```

- [ ] **Step 2: Run creation tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py -k "create_backup" -v
```

Expected: tests fail because orchestration and atomic publication are missing.

- [ ] **Step 3: Implement create flow**

Validate context before creating directories. Acquire a nonblocking operating-system advisory lock on `.backup.lock`: use `fcntl.flock(fd, LOCK_EX | LOCK_NB)` on POSIX and a one-byte `msvcrt.locking(fd, LK_NBLCK, 1)` lock on Windows. Keep the descriptor open through publication and release it in `finally`; the operating system must release the lock after a process crash, so do not use a persistent `O_EXCL` sentinel as the lock itself. Create `.incomplete-<uuid>`, snapshot databases, copy sorted file roots while skipping exact transient directory names, compute `required_file_keys` from the snapshotted `users.db`, write and verify the manifest, then call `staging.replace(final_path)`.

Compute `required_file_keys` using these snapshot queries when the tables exist: committed `document_versions.file_path` with status `current` or `historical`; non-null `kb_documents.file_path` with status `indexed`; and `structured_datasets.storage_key` with status `ready` or `historical`. Map each path to one configured logical root, reject paths outside every root, and fail creation with `referenced_file_missing` when the resulting logical key is absent from copied entries. Test-only helpers `create_fixture_file()` and `load_manifest()` live in `tests/backup_fixtures.py`.

On every exception, remove only the resolved staging directory after confirming it is a child of `backup_root`. Never delete or rename an existing formal backup.

- [ ] **Step 4: Run Task 4 tests and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_service.py -v
git add rag/backups/service.py tests/test_backup_service.py
git commit -m "feat: publish verified backups atomically"
```

---

### Task 5: Isolated Restore Testing

**Files:**
- Create: `tests/test_backup_restore.py`
- Modify: `rag/backups/service.py`

**Interfaces:**
- Consumes: `verify_backup`, `BackupContext` protected roots.
- Produces: `restore_test(backup_dir: Path, isolation_root: Path, protected_roots: tuple[Path, ...]) -> RestoreReport`.

- [ ] **Step 1: Write failing isolation tests**

```python
def test_restore_test_copies_and_revalidates_backup(complete_backup, tmp_path):
    report = restore_test(complete_backup, tmp_path / "isolation", protected_roots=())
    assert report.status == "verified"
    assert report.vector_rebuild_required is True
    assert (report.target / "restore-report.json").is_file()
    assert verify_backup(report.target).status == "verified"


@pytest.mark.parametrize("target_kind", ["project", "data", "qdrant", "backup"])
def test_restore_rejects_protected_targets(complete_backup, restore_roots, target_kind):
    with pytest.raises(BackupError, match="restore_target_protected"):
        restore_test(
            complete_backup,
            restore_roots[target_kind],
            protected_roots=tuple(restore_roots.values()),
        )


def test_restore_rejects_nonempty_destination(complete_backup, tmp_path):
    destination = tmp_path / "isolation" / complete_backup.name
    destination.mkdir(parents=True)
    (destination / "existing.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BackupError, match="restore_target_not_empty"):
        restore_test(complete_backup, tmp_path / "isolation", protected_roots=())
    assert (destination / "existing.txt").read_text(encoding="utf-8") == "keep"


def test_restore_report_contains_no_absolute_paths(complete_backup, tmp_path):
    report = restore_test(complete_backup, tmp_path / "isolation", protected_roots=())
    payload = (report.target / "restore-report.json").read_text(encoding="utf-8")
    assert str(tmp_path) not in payload
    assert json.loads(payload)["vector_rebuild_required"] is True
```

- [ ] **Step 2: Run restore tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_restore.py -v
```

Expected: tests fail because isolated restore is missing.

- [ ] **Step 3: Implement isolation-only restore**

Resolve all protected roots and reject equality or descendant relationships in both directions where source data could be overwritten. Verify the source before creating the target. Copy into a temporary sibling of `<isolation-root>/<backup-id>`, reject symlinks, atomically rename, run `verify_backup` on the restored copy, then write deterministic `restore-report.json` without absolute paths. On failure, remove only the temporary restore directory.

- [ ] **Step 4: Run restore tests and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_restore.py tests\test_backup_service.py -v
git add rag/backups/service.py tests/test_backup_restore.py
git commit -m "feat: verify backups through isolated restore"
```

---

### Task 6: Weekly Due Logic, Retention, and Concurrency

**Files:**
- Create: `tests/test_backup_schedule.py`
- Modify: `rag/backups/service.py`

**Interfaces:**
- Consumes: verified manifests and `create_backup`.
- Produces: `list_verified_backups(root: Path) -> tuple[BackupRecord, ...]`, `create_if_due(context: BackupContext, trigger: str, now: float | None = None) -> BackupRecord | None`, `prune_verified_backups(root: Path, keep: int) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing cadence tests**

```python
WEEK = 7 * 24 * 60 * 60


def test_first_run_creates_backup(backup_context):
    assert create_if_due(backup_context, "scheduled", now=1_700_000_000.0) is not None


def test_recent_backup_is_reused_without_duplicate(backup_context):
    first = create_if_due(backup_context, "scheduled", now=1_700_000_000.0)
    second = create_if_due(backup_context, "scheduled", now=1_700_000_000.0 + WEEK - 1)
    assert first is not None
    assert second is None
    assert len(list_verified_backups(backup_context.backup_root)) == 1


def test_exactly_seven_days_is_due(backup_context):
    create_if_due(backup_context, "scheduled", now=1_700_000_000.0)
    assert create_if_due(backup_context, "scheduled", now=1_700_000_000.0 + WEEK) is not None


def test_future_manifest_time_does_not_create_duplicate(backup_context, caplog):
    create_if_due(backup_context, "scheduled", now=1_700_000_100.0)
    assert create_if_due(backup_context, "scheduled", now=1_700_000_000.0) is None
    assert "backup_clock_moved_backwards" in caplog.text
```

Implement `seed_verified_backups()` in `tests/backup_fixtures.py` by repeatedly calling the real `create_if_due` with week-separated literal timestamps. The concurrency test uses the real public `create_backup` boundary and blocks only the SQLite snapshot dependency; do not expose a production lock-testing method.

- [ ] **Step 2: Run cadence tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_schedule.py -k "run or seven or future" -v
```

Expected: tests fail because due calculation is missing.

- [ ] **Step 3: Implement verified-manifest due calculation**

List only directories whose name matches the formal backup format and whose `verify_backup` succeeds. Sort by manifest `created_at`, then backup ID. `create_if_due` returns `None` when `now - latest.created_at < interval_days * 86400`; negative deltas also return `None` and log the stable warning code.

- [ ] **Step 4: Write failing retention and lock tests**

```python
def test_ninth_success_keeps_latest_eight(backup_context):
    for week in range(9):
        create_if_due(backup_context, "scheduled", now=1_700_000_000.0 + week * WEEK)
    records = list_verified_backups(backup_context.backup_root)
    assert len(records) == 8
    assert records[0].created_at == 1_700_000_000.0 + WEEK


def test_failed_ninth_backup_does_not_prune_existing_eight(backup_context, monkeypatch):
    seed_verified_backups(backup_context, count=8)
    monkeypatch.setattr(
        "rag.backups.service.snapshot_sqlite",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(BackupError("sqlite_snapshot_failed")),
    )
    with pytest.raises(BackupError, match="sqlite_snapshot_failed"):
        create_backup(backup_context, "scheduled", now=1_800_000_000.0)
    assert len(list_verified_backups(backup_context.backup_root)) == 8


def test_concurrent_creation_is_rejected_by_lock(backup_context):
    entered = threading.Event()
    release = threading.Event()
    real_snapshot = snapshot_sqlite

    def blocking_snapshot(source, target):
        entered.set()
        assert release.wait(timeout=1.0)
        return real_snapshot(source, target)

    with patch("rag.backups.service.snapshot_sqlite", blocking_snapshot):
        worker = threading.Thread(
            target=create_backup,
            args=(backup_context, "manual", 1_800_000_000.0),
        )
        worker.start()
        assert entered.wait(timeout=1.0)
        with pytest.raises(BackupError, match="backup_in_progress"):
            create_backup(backup_context, "manual", now=1_800_000_001.0)
        release.set()
        worker.join(timeout=2.0)
        assert worker.is_alive() is False
```

- [ ] **Step 5: Run retention tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_schedule.py -k "ninth or concurrent" -v
```

Expected: tests fail because pruning and lock behavior are missing or incomplete.

- [ ] **Step 6: Implement retention after publication only**

Validate `keep >= 1`. Verify every deletion candidate again immediately before deletion, resolve it as a direct child of backup root, and delete oldest verified candidates only after the new backup has been published. Return deleted backup IDs. Failed and incomplete directories are never retention candidates.

- [ ] **Step 7: Run Task 6 tests and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_schedule.py tests\test_backup_service.py -v
git add rag/backups/service.py tests/test_backup_schedule.py
git commit -m "feat: schedule weekly backups and retain eight"
```

---

### Task 7: Stoppable Application Scheduler

**Files:**
- Create: `rag/backups/scheduler.py`
- Modify: `tests/test_backup_schedule.py`
- Modify: `rag/api.py:57-64`
- Modify: `tests/conftest.py:1-10`

**Interfaces:**
- Consumes: `create_if_due`, `context_from_settings`, injected clock and wait function.
- Produces: `BackupScheduler.start()`, `BackupScheduler.stop(timeout: float = 5.0)`, `start_backup_scheduler()`, `stop_backup_scheduler()`, `backup_scheduler_is_alive()`.

- [ ] **Step 1: Write failing scheduler behavior tests**

```python
def test_scheduler_failure_does_not_end_future_checks(backup_context):
    calls = []

    def run_once():
        calls.append(len(calls))
        if len(calls) == 1:
            raise BackupError("disk_full")

    scheduler = BackupScheduler(run_once=run_once, check_interval_seconds=0.01)
    scheduler.start()
    wait_until(lambda: len(calls) >= 2)
    scheduler.stop()
    assert len(calls) >= 2


def test_scheduler_stop_joins_background_thread():
    scheduler = BackupScheduler(run_once=lambda: None, check_interval_seconds=3600)
    scheduler.start()
    scheduler.stop(timeout=1.0)
    assert scheduler.is_alive is False


def test_disabled_scheduler_does_not_create_thread(monkeypatch):
    monkeypatch.setattr(settings, "backup_enabled", False)
    assert start_backup_scheduler() is None
    assert backup_scheduler_is_alive() is False
```

Define the test-only `wait_until(predicate)` with `time.monotonic()`, a one-second deadline, and 10 ms polling; raise `AssertionError("scheduler condition timed out")` at the deadline. It belongs in `tests/test_backup_schedule.py`, not production code.

- [ ] **Step 2: Run scheduler tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_schedule.py -k "scheduler" -v
```

Expected: tests fail because `BackupScheduler` does not exist.

- [ ] **Step 3: Implement scheduler with `Event.wait`**

The daemon thread runs one immediate `create_if_due(..., trigger="scheduled")`, catches `BackupError` and unexpected exceptions with redacted logging, then waits using `stop_event.wait(check_interval_seconds)`. `stop()` sets the event and joins. Repeated `start()` and `stop()` are idempotent. Disabled settings return without creating a thread.

Before any application import in `tests/conftest.py`, add `os.environ.setdefault("RAG_BACKUP_ENABLED", "false")`. Scheduler unit tests must instantiate `BackupScheduler` with a temporary `BackupContext` or injected `run_once`; no test may rely on the module-level production context.

Add module-level lifecycle wrappers in `rag/api.py`; start after `auto_index_on_startup()` and stop in the lifespan `finally` before closing pipeline processors.

- [ ] **Step 4: Add failing lifespan cleanup test**

```python
def test_lifespan_stops_backup_scheduler(monkeypatch):
    calls = []
    monkeypatch.setattr("rag.api.auto_index_on_startup", lambda: None)
    monkeypatch.setattr("rag.api.start_backup_scheduler", lambda: calls.append("start"))
    monkeypatch.setattr("rag.api.stop_backup_scheduler", lambda: calls.append("stop"))
    with TestClient(app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]
```

- [ ] **Step 5: Confirm RED, implement lifecycle, and run GREEN**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_schedule.py tests\test_api.py -k "backup_scheduler or lifespan" -v
```

Expected after implementation: all selected tests pass with no live backup thread left behind.

- [ ] **Step 6: Commit Task 7**

```powershell
git add rag/backups/scheduler.py rag/api.py tests/conftest.py tests/test_backup_schedule.py tests/test_api.py
git commit -m "feat: run weekly backup checks in application lifecycle"
```

---

### Task 8: CLI and Deployment Gate

**Files:**
- Create: `scripts/backup.py`
- Create: `tests/test_backup_cli.py`
- Create: `tests/test_backup_deployment.py`
- Modify: `docker-compose.yml:17-43`
- Modify: `Dockerfile:52-56`
- Modify: `deploy.sh:24-29`
- Modify: `.github/workflows/deploy.yml:20-33`
- Modify: `.gitignore`
- Modify: `.dockerignore`

**Interfaces:**
- Consumes: Task 1 context builder and Tasks 3-6 service functions.
- Produces: CLI subcommands `create`, `create-if-due`, `verify`, `restore-test`, `list`; deployment gate command `python scripts/backup.py create-if-due --trigger deploy`.

- [ ] **Step 1: Write failing CLI tests**

Import `scripts.backup.main` and inject a temporary context factory rather than spawning against real configuration:

```python
def test_cli_create_if_due_prints_stable_json(backup_context, capsys):
    code = main(
        ["create-if-due", "--trigger", "deploy"],
        context_factory=lambda: backup_context,
        now=lambda: 1_777_000_000.0,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "verified"
    assert payload["vector_rebuild_required"] is True
    assert "path" not in payload


def test_cli_failure_returns_nonzero_without_absolute_path(backup_context, capsys, monkeypatch):
    def fail_create(*_args, **_kwargs):
        raise BackupError("disk_full")

    monkeypatch.setattr("scripts.backup.create_if_due", fail_create)
    code = main(["create-if-due", "--trigger", "deploy"], context_factory=lambda: backup_context)
    output = capsys.readouterr().err
    assert code == 1
    assert json.loads(output) == {"status": "error", "code": "disk_full"}
    assert str(backup_context.backup_root) not in output


def test_cli_verify_restore_and_list_use_backup_ids(backup_context, tmp_path, capsys):
    created = create_backup(backup_context, "manual", now=1_777_000_000.0)
    assert main(["verify", created.backup_id], context_factory=lambda: backup_context) == 0
    assert main(["list"], context_factory=lambda: backup_context) == 0
    assert main(
        ["restore-test", created.backup_id, str(tmp_path / "isolation")],
        context_factory=lambda: backup_context,
    ) == 0
    assert created.backup_id in capsys.readouterr().out


@pytest.mark.parametrize("backup_id", ["../escape", "a/b", "a\\b", ""])
def test_cli_rejects_unsafe_backup_ids(backup_context, backup_id, capsys):
    code = main(["verify", backup_id], context_factory=lambda: backup_context)
    assert code != 0
    assert "unsafe_backup_id" in capsys.readouterr().err
```

- [ ] **Step 2: Run CLI tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_cli.py -v
```

Expected: import fails because the CLI is missing.

- [ ] **Step 3: Implement CLI adapter**

Use `argparse` subparsers with exact command names from the spec. `main(argv=None, context_factory=context_from_settings, now=time.time) -> int` returns 0 on verified, reused, or listed results; returns 1 on `BackupError`; and writes compact JSON only. Resolve backup IDs as direct children of configured backup root and reject separators or traversal.

- [ ] **Step 4: Write failing deployment invariant tests**

```python
def test_compose_uses_a_separate_backup_volume():
    compose = COMPOSE.read_text(encoding="utf-8")
    assert "backup_data:/app/backups" in compose
    assert "RAG_BACKUP_ROOT=/app/backups" in compose
    assert "backup_data:" in compose


def test_image_prepares_backup_directory_for_nonroot_user():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "mkdir -p /app/data/upload /app/qdrant_data /app/backups" in dockerfile
    assert dockerfile.index("/app/backups") < dockerfile.index("USER rag")


def test_deploy_scripts_gate_up_on_backup():
    for path in (DEPLOY_SH, DEPLOY_WORKFLOW):
        text = path.read_text(encoding="utf-8")
        backup = text.index("python scripts/backup.py create-if-due --trigger deploy")
        up = text.index("docker compose up -d")
        assert backup < up
        if path == DEPLOY_WORKFLOW:
            assert text.index("set -euo pipefail") < backup
```

- [ ] **Step 5: Run deployment tests and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_deployment.py -v
```

Expected: tests fail because the volume and backup gate are absent.

- [ ] **Step 6: Implement Docker and deployment integration**

Create `/app/backups` in the Dockerfile before the existing recursive `chown` and `USER rag`. Add `backup_data:/app/backups`, `RAG_BACKUP_ROOT=/app/backups`, and top-level `backup_data:` to Compose. After image build and before `up -d`, run:

```bash
docker compose run --rm --no-deps rag-app \
  python scripts/backup.py create-if-due --trigger deploy
```

Use the same command in the GitHub SSH script. Both deployment scripts must execute `set -euo pipefail` before the backup command, so a nonzero backup exit stops before `up -d` and leaves the old container running. Add `/backups/`, `/RAGv3-backups/`, and sibling-safe documentation comments to ignore files; exclude backup payloads from Docker build context.

Insert `set -euo pipefail` as the first command in the GitHub SSH script; do not rely on the SSH action to stop a multiline script after a failed command.

- [ ] **Step 7: Run Task 8 tests and commit**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_cli.py tests\test_backup_deployment.py tests\test_deployment_health.py -v
& .\.venv\Scripts\ruff.exe check scripts\backup.py tests\test_backup_cli.py tests\test_backup_deployment.py --select E9,F63,F7,F82
git add scripts/backup.py tests/test_backup_cli.py tests/test_backup_deployment.py Dockerfile docker-compose.yml deploy.sh .github/workflows/deploy.yml .gitignore .dockerignore
git commit -m "feat: gate deployments on weekly backups"
```

---

### Task 9: Operator Documentation and Final Verification

**Files:**
- Modify: `.env.example:28-55`
- Modify: `docs/deployment.md:52-65`
- Modify: `README.md:40-60`
- Create: `scripts/create_backup_smoke_fixture.py`
- Modify: `tests/test_backup_cli.py`

**Interfaces:**
- Consumes: completed CLI and settings.
- Produces: exact operator commands, retention explanation, exclusion warning, and recovery-test instructions.

- [ ] **Step 1: Write documentation contract test**

Add to `tests/test_backup_deployment.py`:

```python
def test_deployment_docs_explain_backup_and_isolated_restore():
    docs = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    assert "每 7 天" in docs
    assert "保留最近 8 份" in docs
    assert "python scripts/backup.py verify" in docs
    assert "python scripts/backup.py restore-test" in docs
    assert "不包含 Qdrant" in docs
    assert "重新生成向量索引" in docs
```

Add a failing safety test before creating the smoke fixture helper:

```python
def test_smoke_fixture_refuses_repository_target(tmp_path):
    with pytest.raises(ValueError, match="smoke_target_must_be_temporary"):
        create_smoke_fixture(PROJECT_ROOT)
```

- [ ] **Step 2: Run documentation test and confirm RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_deployment.py::test_deployment_docs_explain_backup_and_isolated_restore -v
```

Expected: the documentation test fails because the current guide only says to back up volumes manually, and the safety test fails because the fixture helper does not exist.

- [ ] **Step 3: Update operator documentation**

Document all five CLI commands, weekly/8-copy defaults, local sibling directory, Docker volume, non-encryption, secret/Qdrant exclusions, deployment failure behavior, and the fact that restore-test never changes production. Add commented `.env.example` entries with exact variable names and defaults. README should link to the detailed deployment section rather than duplicate the entire procedure.

- [ ] **Step 4: Run targeted backup suite**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_backup_models.py tests\test_backup_service.py tests\test_backup_restore.py tests\test_backup_schedule.py tests\test_backup_cli.py tests\test_backup_deployment.py tests\test_deployment_health.py -q
```

Expected: all backup and deployment tests pass.

- [ ] **Step 5: Run full verification from a clean process**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests -q
& .\.venv\Scripts\ruff.exe check rag\backups scripts\backup.py tests\test_backup_models.py tests\test_backup_service.py tests\test_backup_restore.py tests\test_backup_schedule.py tests\test_backup_cli.py tests\test_backup_deployment.py --select E9,F63,F7,F82
Push-Location frontend
npm test
npm run build
Pop-Location
& .\.venv\Scripts\python.exe -m pip check
git diff --check
git status --short
```

Expected: backend and frontend tests pass, production build succeeds, critical Ruff and dependency checks pass, diff check is clean, and status contains only intentional implementation files.

- [ ] **Step 6: Run an isolated create → verify → restore-test smoke test**

Create a temporary fixture root, set all `RAG_*_DB_PATH`, document roots, and `RAG_BACKUP_ROOT` to that fixture, then run the real CLI commands. Do not point the commands at repository `data/`. `create_smoke_fixture(root)` must resolve `tempfile.gettempdir()` and reject targets outside it before creating three synthetic SQLite databases and four synthetic file roots.

```powershell
$backupSmokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ragv3-backup-smoke-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $backupSmokeRoot | Out-Null
try {
    & .\.venv\Scripts\python.exe scripts\create_backup_smoke_fixture.py $backupSmokeRoot
    $env:RAG_USERS_DB_PATH = Join-Path $backupSmokeRoot "data/users.db"
    $env:RAG_ANALYSIS_DB_PATH = Join-Path $backupSmokeRoot "data/analysis.db"
    $env:RAG_MEMORY_DB_PATH = Join-Path $backupSmokeRoot "data/memory.db"
    $env:RAG_DOCUMENT_VERSIONS_ROOT = Join-Path $backupSmokeRoot "data/document_versions"
    $env:RAG_STRUCTURED_DATASETS_ROOT = Join-Path $backupSmokeRoot "data/structured_datasets"
    $env:RAG_BACKUP_ROOT = Join-Path $backupSmokeRoot "backups"
    $created = (& .\.venv\Scripts\python.exe scripts\backup.py create --trigger manual) | ConvertFrom-Json
    & .\.venv\Scripts\python.exe scripts\backup.py verify $created.backup_id
    & .\.venv\Scripts\python.exe scripts\backup.py restore-test $created.backup_id (Join-Path $backupSmokeRoot "isolation")
} finally {
    Remove-Item Env:RAG_USERS_DB_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:RAG_ANALYSIS_DB_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:RAG_MEMORY_DB_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:RAG_DOCUMENT_VERSIONS_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:RAG_STRUCTURED_DATASETS_ROOT -ErrorAction SilentlyContinue
    Remove-Item Env:RAG_BACKUP_ROOT -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backupSmokeRoot -Recurse -Force
}
```

The implementation must create `scripts/create_backup_smoke_fixture.py` as a development-only helper that refuses non-temporary targets and initializes only synthetic SQLite rows and text files. The CLI context builder must also map `upload` and `kb_files` below the common parent of the configured document-version root; the fixture uses that exact layout. If any command exits nonzero, final verification fails.

- [ ] **Step 7: Commit documentation and smoke fixture**

```powershell
git add .env.example README.md docs/deployment.md tests/test_backup_deployment.py scripts/create_backup_smoke_fixture.py
git commit -m "docs: document backup verification and recovery drills"
```

- [ ] **Step 8: Review commit series and hand off**

```powershell
git log --oneline --decorate -10
git status --short
```

Confirm that no real backup directory, database, Qdrant payload, `.env`, credential, or temporary restore directory is tracked. Report exact test counts and note that production replacement restore remains intentionally unavailable.
