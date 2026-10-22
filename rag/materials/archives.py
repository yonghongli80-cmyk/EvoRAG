"""Safe archive manifest, confirmation, and child version diffing.

An archive upload is a two-phase commit. ``inspect_archive`` validates
structure and limits and returns a preview; the user reviews it and confirms
the selected entries. ``confirm_archive_import`` revalidates, extracts only
approved regular files, stages immutable child versions, and switches the
container and every changed child pointer inside one ``BEGIN IMMEDIATE``
transaction so a failure never hides the previous container state.

Security invariants:
- No ``extractall``; every approved file is streamed to a unique stage child
  and re-checked for size, signature, and allowlisted inner format.
- Traversal, nesting, ratio, size, encryption, and symlink/device entries are
  rejected before any content is extracted.
- RAR requires an administrator-installed backend; without one the manifest is
  ``temporarily_unavailable`` and never partially extracted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from rag.document_version_store import final_version_path
from rag.materials.formats import FormatCapabilityRegistry
from rag.materials.jobs import enqueue_latest_index_job
from rag.materials.models import FormatCapability
from rag.user_db import UserDB

_CHUNK = 64 * 1024


class ArchiveValidationError(Exception):
    """Raised for an archive that is not a supported container."""


class ArchiveRejected(Exception):
    """Raised when an archive fails a structural or confirmation check."""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        super().__init__(f"archive rejected: {reason_code}: {detail}")


class ArchiveConfirmationError(Exception):
    """Raised when a confirmation cannot proceed (permission, conflict, expiry)."""


class IndexingFailed(Exception):
    """Raised by the worker hook before batch activation (test/drain injection)."""


@dataclass(frozen=True)
class ArchiveLimits:
    archive_bytes: int = 200 * 1024 * 1024
    expanded_bytes: int = 1024 * 1024 * 1024
    entry_count: int = 1000
    nesting_depth: int = 2
    ratio: int = 100
    entry_bytes: int = 50 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveEntry:
    relative_path: str
    normalized_path: str
    child_source_key: str
    size_bytes: int
    content_hash: str
    state: str
    reason_code: str
    is_dir: bool = False
    id: int = 0


@dataclass(frozen=True)
class ArchiveManifest:
    state: str
    reason_code: str
    entries: tuple[ArchiveEntry, ...]
    entry_count: int
    total_bytes: int
    total_compressed_bytes: int
    backend: str = "zip"
    warning: str = ""


@dataclass(frozen=True)
class ArchiveManifestPreview:
    manifest_id: int
    container_document_id: int
    entries: tuple[ArchiveEntry, ...]

    @property
    def supported_ids(self) -> tuple[int, ...]:
        return tuple(e.id for e in self.entries if e.state == "approved" and e.id)


@dataclass(frozen=True)
class ArchiveImportResult:
    manifest_id: int
    idempotency_key: str
    status: str
    container_document_id: int
    container_version_id: int
    created_children: tuple[str, ...] = ()
    unchanged_children: tuple[str, ...] = ()
    rejected_entries: tuple[dict, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "manifest_id": self.manifest_id,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "container_document_id": self.container_document_id,
            "container_version_id": self.container_version_id,
            "created_children": list(self.created_children),
            "unchanged_children": list(self.unchanged_children),
            "rejected_entries": list(self.rejected_entries),
        }


def child_source_key(container_document_id: int, normalized_relative_path: str) -> str:
    """Stable child identity: container + normalized relative path."""
    return hashlib.sha256(
        f"{container_document_id}:{normalized_relative_path}".encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Path and entry validation
# ---------------------------------------------------------------------------


def _normalize_relative_path(name: str) -> str | None:
    name = name.replace("\\", "/")
    if name.startswith("/"):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts:
        return None
    for part in parts:
        if part in ("..", "."):
            return None
        if ":" in part or "\x00" in part:
            return None
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in part):
            return None
    return "/".join(parts)


def _validate_entry(name: str, encrypted: bool, mode: int, limits: ArchiveLimits):
    normalized = _normalize_relative_path(name)
    if normalized is None:
        return None, "unsafe_path"
    if normalized.count("/") + 1 > limits.nesting_depth:
        return None, "nesting_depth_exceeded"
    if encrypted:
        return None, "encrypted_entry"
    if mode and (
        stat.S_ISLNK(mode)
        or stat.S_ISFIFO(mode)
        or stat.S_ISBLK(mode)
        or stat.S_ISCHR(mode)
        or stat.S_ISSOCK(mode)
    ):
        return None, "unsafe_entry"
    return normalized, ""


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


def _rar_backend_available() -> bool:
    import rarfile

    try:
        rarfile.tool_setup()
        return True
    except Exception:
        return False


def _iter_zip_entries(path: Path) -> Iterator[dict]:
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            yield {
                "name": info.filename,
                "is_dir": info.filename.endswith("/"),
                "size": info.file_size,
                "compressed": info.compress_size,
                "encrypted": bool(info.flag_bits & 0x1),
                "mode": (info.external_attr >> 16) & 0xFFFF,
            }


def _iter_7z_entries(path: Path) -> Iterator[dict]:
    import py7zr

    with py7zr.SevenZipFile(path) as sf:
        for info in sf.list():
            yield {
                "name": info.filename,
                "is_dir": getattr(info, "is_directory", False),
                "size": getattr(info, "uncompressed", 0) or 0,
                "compressed": getattr(info, "compressed", None),
                "encrypted": False,
                "mode": 0,
            }


def _iter_rar_entries(path: Path) -> Iterator[dict]:
    import rarfile

    with rarfile.RarFile(path) as rf:
        for info in rf.infolist():
            yield {
                "name": info.filename,
                "is_dir": info.isdir(),
                "size": info.file_size,
                "compressed": None,
                "encrypted": info.needs_password(),
                "mode": 0,
            }


def inspect_archive(
    path: Path, capability: FormatCapability, limits: ArchiveLimits
) -> ArchiveManifest:
    """Validate archive structure and limits and build a preview manifest.

    Content is never extracted here; approved entries are re-checked and
    streamed during confirmation.
    """
    if not capability.archive_container:
        raise ArchiveValidationError(f"not an archive container: {capability.key}")

    if capability.key == "rar":
        if not _rar_backend_available():
            return ArchiveManifest(
                state="temporarily_unavailable",
                reason_code="rar_backend_unavailable",
                entries=(),
                entry_count=0,
                total_bytes=0,
                total_compressed_bytes=0,
                backend="rar",
            )
        entries_iter = _iter_rar_entries(path)
    elif capability.key == "7z":
        entries_iter = _iter_7z_entries(path)
    elif capability.key == "zip":
        entries_iter = _iter_zip_entries(path)
    else:
        raise ArchiveValidationError(f"not an archive container: {capability.key}")

    return _build_manifest(entries_iter, limits, backend=capability.key)


def _build_manifest(
    raw_entries, limits: ArchiveLimits, backend: str
) -> ArchiveManifest:
    """Validate archive headers as they are enumerated, never all at once.

    The iterator is consumed with early rejection so a container that declares
    millions of entries cannot be materialized into a list before the entry-count
    check runs (same class of bug as the 7z member read in D-06).
    """
    entries: list[ArchiveEntry] = []
    total_compressed = 0
    total_bytes = 0
    file_count = 0
    inspected = 0
    max_inspected = limits.entry_count * 2 + 64
    for e in raw_entries:
        inspected += 1
        if inspected > max_inspected:
            return _rejected("entry_count_exceeded", backend)
        total_compressed += e.get("compressed") or 0
        if total_compressed > limits.archive_bytes:
            return _rejected("compressed_size_exceeded", backend)
        if e["is_dir"]:
            entries.append(
                ArchiveEntry(
                    relative_path=e["name"],
                    normalized_path="",
                    child_source_key="",
                    size_bytes=e["size"],
                    content_hash="",
                    state="skipped",
                    reason_code="directory",
                    is_dir=True,
                )
            )
            continue
        file_count += 1
        total_bytes += e["size"]
        if file_count > limits.entry_count:
            return _rejected("entry_count_exceeded", backend)
        if total_bytes > limits.expanded_bytes:
            return _rejected("expanded_size_exceeded", backend)
        normalized, reason = _validate_entry(
            e["name"], e.get("encrypted", False), e.get("mode", 0), limits
        )
        state = "approved" if reason == "" else "rejected"
        entries.append(
            ArchiveEntry(
                relative_path=e["name"],
                normalized_path=normalized or "",
                child_source_key="",
                size_bytes=e["size"],
                content_hash="",
                state=state,
                reason_code=reason,
            )
        )

    if total_compressed and total_bytes / total_compressed > limits.ratio:
        return _rejected("compression_ratio_exceeded", backend)

    approved = [e for e in entries if e.state == "approved"]
    state = "ok" if approved else "no_approved_entries"
    return ArchiveManifest(
        state=state,
        reason_code="",
        entries=tuple(entries),
        entry_count=file_count,
        total_bytes=total_bytes,
        total_compressed_bytes=total_compressed,
        backend=backend,
    )


def _rejected(reason_code: str, backend: str) -> ArchiveManifest:
    return ArchiveManifest(
        state="rejected",
        reason_code=reason_code,
        entries=(),
        entry_count=0,
        total_bytes=0,
        total_compressed_bytes=0,
        backend=backend,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _copyfileobj_bounded(src, dst, max_bytes: int, chunk_size: int = _CHUNK) -> int:
    """Copy bytes from src to dst up to max_bytes, aborting if exceeded."""
    total = 0
    while True:
        buf = src.read(chunk_size)
        if not buf:
            break
        total += len(buf)
        if total > max_bytes:
            raise ArchiveRejected("entry_size_exceeded", f"extracted stream exceeded {max_bytes} bytes")
        dst.write(buf)
    return total


def _extract_1zip_bounded(source_path: Path, entry_name: str, dest: Path, max_bytes: int) -> None:
    with zipfile.ZipFile(source_path) as zf, zf.open(entry_name) as src, open(dest, "wb") as out:
        _copyfileobj_bounded(src, out, max_bytes, _CHUNK)


def _extract_7z_bounded(
    source_path: Path, entry_name: str, dest: Path, max_bytes: int
) -> None:
    """Stream one 7z member to ``dest`` while enforcing the size cap.

    The historical implementation called ``SevenZipFile.read([name])``, which
    decompresses the member completely into memory *before* any limit check, so
    a crafted archive could exhaust RAM (D-06).  py7zr's ``WriterFactory`` API
    hands decompressed chunks to a writer as they arrive; the writer below stops
    the stream at ``max_bytes`` so peak memory stays at the decompressor block
    instead of the full member.  When no bound is supplied the caller has asked
    for an unbounded 7z read, which is refused rather than performed.
    """
    import py7zr
    from py7zr.io import Py7zIO, WriterFactory

    class _BoundedWriter(Py7zIO):
        def __init__(self) -> None:
            # The writer owns this handle for the duration of the decompression
            # stream; py7zr calls ``write``/``flush``/``close`` on the object, so
            # a ``with`` block cannot describe its lifetime (SIM115 is expected).
            self._handle = open(dest, "wb")  # noqa: SIM115
            self._written = 0
            self._closed = False

        def write(self, data: bytes) -> int:
            self._written += len(data)
            if self._written > max_bytes:
                self._abort()
                raise ArchiveRejected(
                    "entry_size_exceeded",
                    f"extracted stream exceeded {max_bytes} bytes",
                )
            self._handle.write(data)
            return len(data)

        def read(self, size: int | None = None) -> bytes:  # pragma: no cover - write-only
            return b""

        def seek(self, offset: int, whence: int = 0) -> int:  # pragma: no cover
            return 0

        def flush(self) -> None:
            self._handle.flush()

        def size(self) -> int:
            return self._written

        def close(self) -> None:
            if not self._closed:
                self._closed = True
                self._handle.close()

        def _abort(self) -> None:
            self.close()
            with suppress(OSError):
                dest.unlink(missing_ok=True)

    class _SingleMemberFactory(WriterFactory):
        def __init__(self) -> None:
            self._created = False

        def create(self, filename: str) -> Py7zIO:
            if self._created:
                # Duplicate member names are refused instead of interleaving two
                # decompression streams into one staging file.
                raise ArchiveRejected("duplicate_entry", "archive member is duplicated")
            self._created = True
            return _BoundedWriter()

    with py7zr.SevenZipFile(source_path) as archive:
        archive.extract(
            path=None,
            targets=[entry_name],
            factory=_SingleMemberFactory(),
        )


def _extract_entry(
    source_path: Path, backend: str, entry_name: str, dest: Path, max_bytes: int | None = None
) -> None:
    """Stream one approved archive member to ``dest`` (never ``extractall``)."""
    if backend == "zip":
        if max_bytes is not None:
            _extract_1zip_bounded(source_path, entry_name, dest, max_bytes)
        else:
            with zipfile.ZipFile(source_path) as zf, zf.open(entry_name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
    elif backend == "7z":
        if max_bytes is None:
            raise ArchiveConfirmationError("archive_entry_limit_required")
        _extract_7z_bounded(source_path, entry_name, dest, max_bytes)
    elif backend == "rar":
        import rarfile

        copy_fn = (
            (lambda s, d: _copyfileobj_bounded(s, d, max_bytes, _CHUNK))
            if max_bytes is not None
            else (lambda s, d: shutil.copyfileobj(s, d, _CHUNK))
        )
        with rarfile.RarFile(source_path) as rf, rf.open(entry_name) as src, open(dest, "wb") as out:
            copy_fn(src, out)
    else:
        raise ArchiveConfirmationError("unknown_archive_backend")


def _inner_resolution(registry: FormatCapabilityRegistry, normalized_path: str, signature: bytes):
    return registry.resolve(Path(normalized_path).name, None, signature)


def _backend_from_filename(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".") or "zip"


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def confirm_archive_import(
    db: UserDB,
    manifest_id: int,
    actor: dict,
    selected_entry_ids: tuple[int, ...],
    expected_version: int,
    idempotency_key: str,
    versions_root: Path,
    registry: FormatCapabilityRegistry,
    limits: ArchiveLimits,
    *,
    now: float | None = None,
    should_fail: Callable[[], bool] | None = None,
) -> ArchiveImportResult:
    """Atomically activate the container and every changed child version.

    Idempotency is checked first so a replay never duplicates children.
    Rechecking re-runs container permission, manifest status/expiry, the
    container current-version CAS, and per-entry signature/limits. On any
    pre-commit failure the previously current container and children stay
    untouched and only this attempt's artifacts are removed.
    """
    now = now if now is not None else time.time()

    prior = db.get_archive_import_result(manifest_id, idempotency_key)
    if prior is not None:
        payload = json.loads(prior["result_json"])
        return ArchiveImportResult(**{**payload, "status": "already_confirmed"})

    manifest = db.get_material_archive_manifest(manifest_id)
    if manifest is None:
        raise ArchiveConfirmationError("manifest_not_found")
    if manifest["status"] != "pending_confirmation":
        raise ArchiveConfirmationError("manifest_not_pending")
    if now > manifest["expires_at"]:
        raise ArchiveConfirmationError("manifest_expired")

    container = db.get_document_permission_by_id(manifest["container_document_id"])
    if container is None:
        raise ArchiveConfirmationError("container_missing")
    from fastapi import HTTPException

    from rag.api_security import require_doc_edit
    try:
        require_doc_edit(db, container["doc_name"], container["kb_id"], actor)
    except HTTPException as exc:
        raise ArchiveConfirmationError("no_edit_permission") from exc

    current = db.get_current_document_version(container["id"])
    current_no = current["version_no"] if current else 0
    if current_no != expected_version:
        raise ArchiveConfirmationError("version_conflict")

    entries = db.list_material_archive_entries(manifest_id)
    by_id = {e["id"]: e for e in entries}
    selected = [by_id[i] for i in selected_entry_ids if i in by_id and by_id[i]["state"] == "approved"]
    if not selected:
        raise ArchiveConfirmationError("no_selected_entries")

    source_path = Path(manifest["source_path"])
    if not source_path.is_file():
        raise ArchiveConfirmationError("source_missing")
    backend = _backend_from_filename(container["doc_name"])

    work_dir = versions_root / "archive_work" / f"{manifest_id}-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)

    promoted_files: list[Path] = []
    try:
        planned: list[dict] = []
        rejected_entries: list[dict] = []
        for entry in selected:
            staged = work_dir / f"{entry['child_source_key']}.bin"
            try:
                _extract_entry(
                    source_path, backend, entry["relative_path"], staged, max_bytes=limits.entry_bytes
                )
            except ArchiveRejected as exc:
                rejected_entries.append(
                    {"id": entry["id"], "relative_path": entry["normalized_path"], "status": "rejected", "reason_code": exc.reason_code}
                )
                continue
            except Exception:  # encrypted / damaged member
                rejected_entries.append(
                    {"id": entry["id"], "relative_path": entry["normalized_path"], "status": "rejected", "reason_code": "extraction_failed"}
                )
                continue
            if staged.stat().st_size > limits.entry_bytes:
                rejected_entries.append(
                    {"id": entry["id"], "relative_path": entry["normalized_path"], "status": "rejected", "reason_code": "entry_size_exceeded"}
                )
                continue
            signature = staged.open("rb").read(16)
            resolution = _inner_resolution(registry, entry["normalized_path"], signature)
            if not (resolution.state == "supported" or resolution.reason_code == "metadata_only"):
                rejected_entries.append(
                    {"id": entry["id"], "relative_path": entry["normalized_path"], "status": "rejected", "reason_code": "unsupported_inner_format"}
                )
                continue
            content_hash = _sha256_file(staged)
            db.set_material_archive_entry_content(entry["id"], content_hash)
            planned.append(
                {
                    "id": entry["id"],
                    "child_source_key": entry["child_source_key"],
                    "normalized_path": entry["normalized_path"],
                    "content_hash": content_hash,
                }
            )

        if not planned:
            raise ArchiveConfirmationError("no_approved_entries")

        # Stage the container's immutable version file from the quarantined copy.
        container_ver = db.get_document_version_by_id(manifest["container_version_id"])
        if container_ver is None:
            raise ArchiveConfirmationError("container_version_missing")
        container_final = final_version_path(
            versions_root, container["id"], container_ver["version_no"], Path(container["doc_name"]).name
        )
        container_final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, container_final)
        db.set_version_file_path(container_ver["id"], str(container_final))
        promoted_files.append(container_final)

        # Diff children by stable key + content hash; stage new child versions.
        created: list[dict] = []
        unchanged: list[str] = []
        for p in planned:
            existing = db.get_container_child(container["id"], p["normalized_path"])
            if existing is not None:
                current_child = db.get_current_document_version(existing["id"])
                if current_child is not None and current_child["content_hash"] == p["content_hash"]:
                    unchanged.append(p["normalized_path"])
                    continue
                child_id = existing["id"]
                new_doc = False
            else:
                child_doc_name = f"{container['doc_name']}/{p['normalized_path']}"
                child_id = db.create_document_permission(child_doc_name, container["kb_id"], container["owner_id"])
                db.set_child_container(child_id, container["id"], p["normalized_path"])
                new_doc = True
            staged = work_dir / f"{p['child_source_key']}.bin"
            version_id = db.create_document_version(
                child_id, p["normalized_path"], str(staged), p["content_hash"], actor["id"]
            )
            child_ver = db.get_document_version_by_id(version_id)
            final = final_version_path(
                versions_root, child_id, child_ver["version_no"], f"{p['child_source_key']}.bin"
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(staged, final)
            db.set_version_file_path(version_id, str(final))
            promoted_files.append(final)
            created.append(
                {
                    "child_id": child_id,
                    "version_id": version_id,
                    "final_path": final,
                    "rel_path": p["normalized_path"],
                    "content_hash": p["content_hash"],
                    "new_doc": new_doc,
                }
            )

        if should_fail is not None and should_fail():
            raise IndexingFailed("injected failure before batch activation")

        result = ArchiveImportResult(
            manifest_id=manifest_id,
            idempotency_key=idempotency_key,
            status="confirmed",
            container_document_id=container["id"],
            container_version_id=manifest["container_version_id"],
            created_children=tuple(c["rel_path"] for c in created),
            unchanged_children=tuple(unchanged),
            rejected_entries=tuple(rejected_entries),
        )
        try:
            # The activation transaction owns the lock: row mutations below run
            # directly on the connection (the ``_in_tx`` pattern) and never
            # re-acquire ``UserDB._lock``.
            with db._lock, db.transaction(immediate=True):
                db._activate_document_version_in_tx(
                    container["id"], manifest["container_version_id"], container["kb_id"], container["doc_name"]
                )
                for c in created:
                    db._activate_document_version_in_tx(
                        c["child_id"], c["version_id"], container["kb_id"], c["rel_path"]
                    )
                    db._conn.execute(
                        "INSERT INTO material_search_fts "
                        "(document_id, version_id, filename, logical_path, document_type, tags, extracted_text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            c["child_id"], c["version_id"], Path(c["rel_path"]).name,
                            c["rel_path"], "", "[]", "",
                        ),
                    )
                db._conn.execute(
                    "UPDATE material_archive_manifests SET status='confirmed' WHERE id=?",
                    (manifest_id,),
                )
                db._conn.execute(
                    "INSERT INTO material_archive_import_results "
                    "(manifest_id, idempotency_key, container_document_id, container_version_id, result_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        manifest_id, idempotency_key, container["id"],
                        manifest["container_version_id"], json.dumps(result.to_dict(), ensure_ascii=False),
                    ),
                )
        except BaseException:
            for c in created:
                if c["new_doc"]:
                    db.delete_document_permission(c["child_id"])
                else:
                    db.delete_document_version(c["version_id"])
                c["final_path"].unlink(missing_ok=True)
            for path in promoted_files:
                path.unlink(missing_ok=True)
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
        source_path.unlink(missing_ok=True)
        for c in created:
            enqueue_latest_index_job(
                db, c["child_id"], c["version_id"], c["content_hash"], time.time()
            )
        return result


class ArchiveService:
    """Stateful wrapper over manifest inspection/confirmation for tests and API.

    ``upload_zip`` quarantines a source archive, creates the container
    document/version, and persists a preview manifest. ``confirm`` runs the
    atomic child diff. ``search``/``current_children`` surface only committed
    state. Failure hooks let callers exercise the rollback path.
    """

    ConfirmationError = ArchiveConfirmationError

    def __init__(
        self,
        db: UserDB,
        versions_root: Path,
        registry: FormatCapabilityRegistry,
        limits: ArchiveLimits,
    ):
        self.db = db
        self.versions_root = versions_root
        self.registry = registry
        self.limits = limits
        self._fail_before_activation = False
        self.bypass_failure_hook = False
        self.last_container_id: int | None = None
        self.last_manifest_id: int | None = None

    def fail_before_batch_activation(self) -> None:
        self._fail_before_activation = True

    def _should_fail(self) -> bool:
        if self._fail_before_activation and not self.bypass_failure_hook:
            self._fail_before_activation = False
            return True
        return False

    def upload_zip(self, filename: str, kb_id: str, actor: dict, source: Path) -> ArchiveManifestPreview:
        quarantine_dir = self.versions_root / "staging"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine = quarantine_dir / f"{uuid.uuid4().hex}-{Path(filename).name}"
        shutil.copyfile(source, quarantine)

        try:
            signature = quarantine.open("rb").read(16)
            resolution = self.registry.resolve(filename, None, signature)
            if resolution.capability is None or not resolution.capability.archive_container:
                raise ArchiveRejected("not_an_archive", filename)
            manifest = inspect_archive(quarantine, resolution.capability, self.limits)

            existing = self.db.get_document_permission(filename, kb_id)
            if existing is None:
                container_id = self.db.create_document_permission(filename, kb_id, actor["id"], 1, False)
            else:
                container_id = existing["id"]
            container_version_id = self.db.create_document_version(
                container_id, filename, str(quarantine), _sha256_file(quarantine), actor["id"]
            )
            self.db.set_version_file_path(container_version_id, str(quarantine))
            manifest_id = self.db.create_material_archive_manifest(
                container_id, container_version_id, str(quarantine), actor["id"], time.time() + 3600
            )

            entries = []
            for e in manifest.entries:
                key = child_source_key(container_id, e.normalized_path) if e.normalized_path else ""
                entry_id = self.db.add_material_archive_entry(
                    manifest_id, e.relative_path, e.normalized_path, key, e.size_bytes, e.state, e.reason_code
                )
                entries.append(
                    ArchiveEntry(
                        relative_path=e.relative_path,
                        normalized_path=e.normalized_path,
                        child_source_key=key,
                        size_bytes=e.size_bytes,
                        content_hash=e.content_hash,
                        state=e.state,
                        reason_code=e.reason_code,
                        is_dir=e.is_dir,
                        id=entry_id,
                    )
                )
            self.last_container_id = container_id
            self.last_manifest_id = manifest_id
            return ArchiveManifestPreview(
                manifest_id=manifest_id, container_document_id=container_id, entries=tuple(entries)
            )
        except BaseException:
            quarantine.unlink(missing_ok=True)
            raise

    def confirm(
        self,
        manifest_id: int,
        actor: dict,
        selected_entry_ids,
        expected_version: int,
        idempotency_key: str,
        now: float | None = None,
    ) -> ArchiveImportResult:
        return confirm_archive_import(
            self.db,
            manifest_id,
            actor,
            tuple(selected_entry_ids),
            expected_version,
            idempotency_key,
            self.versions_root,
            self.registry,
            self.limits,
            now=now,
            should_fail=self._should_fail,
        )

    def search(self, text: str) -> list[dict]:
        hits = self.db.search_material_search_fts(text)
        return [
            {
                "document_id": h["document_id"],
                "version_id": h["version_id"],
                "relative_path": h["logical_path"],
            }
            for h in hits
        ]

    def current_children(self, container_document_id: int | None = None) -> list[str]:
        cid = container_document_id if container_document_id is not None else self.last_container_id
        if cid is None:
            return []
        return [r["container_relative_path"] for r in self.db.list_container_children(cid)]
