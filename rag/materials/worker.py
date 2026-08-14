"""Bounded persistent job worker for incremental material indexing.

Worker ordering (mirrors the plan):
1. verify the job version is still desired;
2. extract and fingerprint;
3. resolve cached/new embeddings (bounded concurrency);
4. write only changed Qdrant points under the target version;
5. write current-version FTS rows inside the activation transaction;
6. recheck the desired version and activate in one ``BEGIN IMMEDIATE``;
7. commit, then repair the advisory ``is_current`` flags;
8. on pre-commit failure delete only this version's points/artifacts;
9. on post-commit repair failure enqueue ``repair_vector_flags``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from config import settings
from rag.document_version_store import resolve_version_path
from rag.materials.embedding_cache import EmbeddingCache
from rag.materials.formats import FormatCapabilityRegistry
from rag.materials.incremental import fingerprint_material
from rag.materials.jobs import claim_material_jobs
from rag.materials.models import ExtractedMaterial, FormatResolution
from rag.models import Chunk
from rag.spreadsheets.chunker import DatasetMetadata
from rag.spreadsheets.extractor import workbook_index_material
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset
from rag.user_db import UserDB

logger = logging.getLogger(__name__)


class ActivationFailed(Exception):
    """Raised when the activation transaction fails before commit."""


class _StaleVersionError(Exception):
    pass


class _PermissionRevokedError(Exception):
    pass


@dataclass(frozen=True)
class JobResult:
    job_id: int
    status: str
    version_id: int
    embedded_units: int
    reused_units: int
    error_code: str = ""


class DefaultVectorStore:
    """Adapter over ``rag.vector_store`` for real Qdrant writes."""

    def add_units(self, kb_id, chunks, vectors, *, document_id, document_version_id, version_no):
        from rag.vector_store import add_to_collection

        add_to_collection(
            kb_id,
            chunks,
            vectors,
            document_id=document_id,
            document_version_id=document_version_id,
            version_no=version_no,
            is_current=True,
            updated_at=time.time(),
        )

    def delete_version(self, kb_id, version_id):
        from rag.vector_store import delete_document_version

        delete_document_version(kb_id, version_id)

    def copy_units_to_version(
        self,
        kb_id,
        source_version_id,
        target_version_id,
        document_id,
        version_no,
        unit_keys,
        dataset_id=None,
    ):
        from rag.vector_store import copy_units_to_version

        return copy_units_to_version(
            kb_id,
            source_version_id=source_version_id,
            target_version_id=target_version_id,
            document_id=document_id,
            version_no=version_no,
            unit_keys=unit_keys,
            dataset_id=dataset_id,
        )

    def mark_current(self, kb_id, document_id, version_id):
        from rag.vector_store import mark_document_version_current

        mark_document_version_current(kb_id, document_id, version_id)


def _default_extractor(path: Path, version_id: int, resolution: FormatResolution) -> ExtractedMaterial:
    from rag.materials.extraction import extract_material

    return extract_material(path, version_id, resolution)


@dataclass(frozen=True)
class WorkerServices:
    db: UserDB
    versions_root: Path
    registry: FormatCapabilityRegistry
    vector_store: object = field(default_factory=DefaultVectorStore)
    cache: EmbeddingCache | None = None
    embedder: Callable[[list[str]], list[list[float]]] | None = None
    extractor: Callable[[Path, int, FormatResolution], ExtractedMaterial] = _default_extractor
    parser_schema_version: str = "material-v1"
    model_key: str = settings.bailian_embed_model
    dimension: int = settings.embed_dimension
    max_embedding_concurrency: int = 4
    structured_datasets_root: Path = field(
        default_factory=lambda: Path(settings.structured_datasets_root)
    )


class MaterialWorker:
    """Bounded persistent worker with graceful lease release on shutdown.

    ``start_one_job`` claims a single due job (test/drain hook); the production
    loop claims with a bounded batch. ``shutdown`` stops new claims, releases
    in-flight leases back to ``queued`` so jobs are never lost, and returns
    them atomically per job.
    """

    def __init__(
        self,
        services: WorkerServices,
        worker_id: str = "worker-1",
        lease_seconds: int = 120,
        max_attempts: int = 5,
    ):
        self.services = services
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._running = False
        self._in_flight: list[int] = []

    def claim_one(self, now: float | None = None) -> dict | None:
        jobs = claim_material_jobs(
            self.services.db,
            self.worker_id,
            now if now is not None else time.time(),
            1,
            self.lease_seconds,
        )
        return jobs[0] if jobs else None

    def start_one_job(self, now: float | None = None) -> dict | None:
        job = self.claim_one(now)
        if job is not None:
            self._in_flight.append(job["id"])
            self._running = True
        return job

    def run(self, batch: int = 4) -> list[JobResult]:
        """Claim and process up to ``batch`` due jobs synchronously."""
        jobs = claim_material_jobs(
            self.services.db, self.worker_id, time.time(), batch, self.lease_seconds
        )
        results = []
        for job in jobs:
            self._in_flight.append(job["id"])
            renewal_stop = threading.Event()
            renewal_interval = max(1.0, self.lease_seconds / 3)

            def _renew_lease(
                job_id: int = job["id"],
                stop: threading.Event = renewal_stop,
                interval: float = renewal_interval,
            ) -> None:
                while not stop.wait(interval):
                    if not self.services.db.renew_material_job(
                        job_id, self.worker_id, time.time(), self.lease_seconds
                    ):
                        return

            renewal_thread = threading.Thread(target=_renew_lease, daemon=True)
            renewal_thread.start()
            try:
                result = process_index_job(job, self.services)
            except Exception:
                logger.exception("material index job %s failed", job["id"])
                attempts_exhausted = job["attempt"] >= self.max_attempts
                result = JobResult(
                    job["id"],
                    "failed" if attempts_exhausted else "retry",
                    job["version_id"],
                    0,
                    0,
                    "processing_failed",
                )
                if attempts_exhausted:
                    _exhaust_spreadsheet_job(job, self.services)
                retry_at = None
                if result.status == "retry":
                    retry_at = time.time() + min(60, 2 ** job["attempt"])
                self.services.db.settle_material_job(
                    job["id"], self.worker_id, result.status, result.error_code, retry_at
                )
            else:
                self.services.db.settle_material_job(
                    job["id"], self.worker_id, result.status, result.error_code
                )
            finally:
                renewal_stop.set()
                renewal_thread.join(timeout=1.0)
                self._in_flight.remove(job["id"])
            results.append(result)
        return results

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop claims and atomically return in-flight jobs to the queue."""
        self._running = False
        db = self.services.db
        for job_id in list(self._in_flight):
            with db._lock:
                db._conn.execute(
                    "UPDATE document_index_jobs "
                    "SET status='queued', lease_owner=NULL, lease_expires_at=NULL, updated_at=? "
                    "WHERE id=? AND status='running' AND lease_owner=?",
                    (time.time(), job_id, self.worker_id),
                )
                db._conn.commit()
        self._in_flight.clear()


def _enqueue_repair(db: UserDB, document_id: int, version_id: int) -> None:
    try:
        with db._lock:
            db._conn.execute(
                "INSERT INTO document_index_jobs "
                "(document_id, version_id, content_hash, job_type, available_at) "
                "VALUES (?, ?, '', 'repair_vector_flags', ?)",
                (document_id, version_id, time.time()),
            )
            db._conn.commit()
    except Exception:
        pass


def _finish_spreadsheet_failure(
    db: UserDB,
    session: dict | None,
    version_id: int,
    *,
    status: str,
    error_code: str,
) -> None:
    if session is not None and session.get("status") == "pending":
        try:
            db.set_spreadsheet_edit_status(
                int(session["id"]), status, error_code=error_code
            )
        except Exception:
            logger.exception("could not settle spreadsheet edit session %s", session["id"])
    db.set_version_status(version_id, "failed")


def _exhaust_spreadsheet_job(job: dict, services: WorkerServices) -> None:
    """Fail spreadsheet audit/artifacts only after the worker retry budget ends."""
    db = services.db
    version_id = int(job["version_id"])
    session = db.get_spreadsheet_edit_session_for_version(version_id)
    if session is None:
        return
    dataset = db.get_structured_dataset_for_version(version_id)
    if dataset is not None:
        path = resolve_dataset_path(
            services.structured_datasets_root,
            int(job["document_id"]),
            version_id,
        )
        path.unlink(missing_ok=True)
        db.mark_structured_dataset_failed(int(dataset["id"]), "processing_failed")
    document = db.get_document_permission_by_id(int(job["document_id"]))
    if document is not None:
        try:
            services.vector_store.delete_version(document["kb_id"], version_id)
        except Exception:
            logger.exception("spreadsheet exhausted-job vector cleanup failed")
    _finish_spreadsheet_failure(
        db,
        session,
        version_id,
        status="failed",
        error_code="processing_failed",
    )


def process_index_job(job: dict, services: WorkerServices) -> JobResult:
    db = services.db
    document_id = job["document_id"]
    version_id = job["version_id"]

    document = db.get_document_permission_by_id(document_id)
    if document is None:
        return JobResult(job["id"], "failed", version_id, 0, 0, "document_missing")
    version = db.get_document_version_by_id(version_id)
    if version is None:
        return JobResult(job["id"], "failed", version_id, 0, 0, "version_missing")
    edit_session = db.get_spreadsheet_edit_session_for_version(version_id)
    if db.get_latest_version_id(document_id) != version_id:
        _finish_spreadsheet_failure(
            db,
            edit_session,
            version_id,
            status="cancelled",
            error_code="stale_version",
        )
        return JobResult(job["id"], "cancelled", version_id, 0, 0, "stale_version")

    path = resolve_version_path(services.versions_root, version["file_path"])
    signature = b""
    try:
        with open(path, "rb") as fh:
            signature = fh.read(16)
    except OSError:
        pass
    resolution = services.registry.resolve(version["original_filename"], None, signature)
    spreadsheet_dataset_id: int | None = None
    spreadsheet_dataset_path: Path | None = None
    chunk_templates: dict[str, Chunk] = {}
    if resolution.capability is not None and resolution.capability.key == "xlsx":
        workbook = parse_spreadsheet(path, "xlsx", None)
        existing_dataset = db.get_structured_dataset_for_version(version_id)
        spreadsheet_dataset_id = (
            int(existing_dataset["id"])
            if existing_dataset is not None
            else db.create_structured_dataset(
                document_id, version_id, "xlsx", "row_groups"
            )
        )
        spreadsheet_dataset_path = resolve_dataset_path(
            services.structured_datasets_root, document_id, version_id
        )
        manifest = write_dataset(spreadsheet_dataset_path, workbook)
        material, chunk_templates = workbook_index_material(
            workbook,
            DatasetMetadata(
                dataset_id=spreadsheet_dataset_id,
                document_id=document_id,
                document_version_id=version_id,
                document_name=document["doc_name"],
            ),
        )
        with db._lock:
            db._conn.execute(
                "UPDATE structured_datasets SET content_hash = ?, sheet_count = ?, "
                "total_rows = ?, total_columns = ?, extraction_version = ? WHERE id = ?",
                (
                    version["content_hash"],
                    len(manifest.sheets),
                    sum(sheet.row_count for sheet in manifest.sheets),
                    sum(sheet.column_count for sheet in manifest.sheets),
                    material.parser_schema_version,
                    spreadsheet_dataset_id,
                ),
            )
            db._conn.commit()
    else:
        material = services.extractor(path, version_id, resolution)

    parser_schema_version = material.parser_schema_version or services.parser_schema_version
    current_fps = fingerprint_material(material, parser_schema_version)
    previous_version_id = db.current_version_id(document_id)
    previous_digests = (
        db.load_unit_fingerprints(document_id, previous_version_id)
        if previous_version_id is not None
        else {}
    )
    changed = [fp for fp in current_fps if previous_digests.get(fp.unit_key) != fp.digest]
    unchanged = [fp for fp in current_fps if previous_digests.get(fp.unit_key) == fp.digest]

    units_by_key = {unit.unit_key: unit for unit in material.units}
    changed_units = [units_by_key[fp.unit_key] for fp in changed if fp.unit_key in units_by_key]
    fingerprints = [fp.digest for fp in changed if fp.unit_key in units_by_key]

    chunks = []
    vectors: list[list[float]] = []
    if changed_units:
        texts = [unit.text for unit in changed_units]
        if services.cache is not None:
            vectors = services.cache.get_or_embed(
                fingerprints, texts, services.model_key, services.dimension,
                lambda ts: services.embedder(ts),
            )
        else:
            vectors = services.embedder(texts)
        chunks = []
        for index, unit in enumerate(changed_units):
            template = chunk_templates.get(unit.unit_key)
            if template is None:
                chunk = Chunk(
                    text=unit.text,
                    doc_name=document["doc_name"],
                    chunk_index=index,
                    metadata={**dict(unit.metadata), "unit_key": unit.unit_key},
                    document_id=document_id,
                    document_version_id=version_id,
                    version_no=version["version_no"],
                    is_current=True,
                )
            else:
                chunk = replace(
                    template,
                    metadata={**dict(template.metadata), "unit_key": unit.unit_key},
                    version_no=version["version_no"],
                    is_current=True,
                )
            chunks.append(chunk)

    if unchanged:
        if previous_version_id is None:
            raise ActivationFailed("unchanged units have no source version")
        copied = services.vector_store.copy_units_to_version(
            document["kb_id"],
            previous_version_id,
            version_id,
            document_id,
            version["version_no"],
            [fp.unit_key for fp in unchanged],
            dataset_id=spreadsheet_dataset_id,
        )
        if copied != len(unchanged):
            services.vector_store.delete_version(document["kb_id"], version_id)
            raise ActivationFailed("incomplete unchanged-unit copy")

    if chunks:
        services.vector_store.add_units(
            document["kb_id"], chunks, vectors,
            document_id=document_id,
            document_version_id=version_id,
            version_no=version["version_no"],
        )

    # Recheck the desired version before activating.
    if db.get_latest_version_id(document_id) != version_id:
        services.vector_store.delete_version(document["kb_id"], version_id)
        return JobResult(job["id"], "cancelled", version_id, len(changed), len(unchanged), "stale_version")

    extracted_text = "\n".join(unit.text for unit in material.units)
    fingerprint_map = {fp.unit_key: fp.digest for fp in current_fps}
    try:
        # Activation owns the lock; row mutations and reads run directly on the
        # connection (the ``_in_tx`` pattern) and never re-acquire ``UserDB._lock``.
        with db._lock, db.transaction(immediate=True):
            latest = db._conn.execute(
                "SELECT id FROM document_versions "
                "WHERE document_id = ? ORDER BY version_no DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if latest is None or latest["id"] != version_id:
                raise _StaleVersionError()
            if edit_session is not None:
                current = db._conn.execute(
                    "SELECT current_version_id, owner_id, scope, protected "
                    "FROM document_permissions WHERE id = ?",
                    (document_id,),
                ).fetchone()
                actor = db._conn.execute(
                    "SELECT is_admin FROM users WHERE id = ?",
                    (edit_session["actor_user_id"],),
                ).fetchone()
                shared_edit = db._conn.execute(
                    "SELECT 1 FROM document_shares "
                    "WHERE doc_id=? AND user_id=? AND permission='edit'",
                    (document_id, edit_session["actor_user_id"]),
                ).fetchone()
                allowed = (
                    current is not None
                    and not bool(current["protected"])
                    and actor is not None
                    and (
                        bool(actor["is_admin"])
                        or current["owner_id"] == edit_session["actor_user_id"]
                        or (
                            current["scope"] == "shared"
                            and shared_edit is not None
                        )
                    )
                )
                if not allowed:
                    raise _PermissionRevokedError()
                if current["current_version_id"] != edit_session["base_version_id"]:
                    raise _StaleVersionError()
                if edit_session["status"] != "pending":
                    raise _StaleVersionError()
            db._activate_document_version_in_tx(
                document_id, version_id, document["kb_id"], document["doc_name"]
            )
            if spreadsheet_dataset_id is not None:
                db._activate_structured_dataset_in_tx(
                    document_id, spreadsheet_dataset_id
                )
            db._conn.execute(
                "DELETE FROM material_search_fts WHERE document_id = ?", (document_id,)
            )
            db._conn.execute(
                "INSERT INTO material_search_fts "
                "(document_id, version_id, filename, logical_path, document_type, tags, extracted_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id, version_id, document["doc_name"],
                    document.get("logical_path") or "",
                    document.get("document_type") or "",
                    document.get("tags_json") or "[]",
                    extracted_text,
                ),
            )
            db._conn.execute(
                "DELETE FROM material_unit_fingerprints WHERE document_id = ?", (document_id,)
            )
            db._conn.executemany(
                "INSERT INTO material_unit_fingerprints "
                "(document_id, version_id, unit_key, digest) VALUES (?, ?, ?, ?)",
                [(document_id, version_id, key, digest) for key, digest in fingerprint_map.items()],
            )
            if edit_session is not None:
                db._conn.execute(
                    "UPDATE spreadsheet_edit_sessions SET status='active', activated_at=?, "
                    "error_code='' WHERE id=?",
                    (time.time(), edit_session["id"]),
                )
    except _StaleVersionError:
        services.vector_store.delete_version(document["kb_id"], version_id)
        if spreadsheet_dataset_path is not None:
            spreadsheet_dataset_path.unlink(missing_ok=True)
        _finish_spreadsheet_failure(
            db, edit_session, version_id, status="cancelled", error_code="stale_version"
        )
        return JobResult(job["id"], "cancelled", version_id, len(changed), len(unchanged), "stale_version")
    except _PermissionRevokedError:
        services.vector_store.delete_version(document["kb_id"], version_id)
        if spreadsheet_dataset_path is not None:
            spreadsheet_dataset_path.unlink(missing_ok=True)
        if spreadsheet_dataset_id is not None:
            db.mark_structured_dataset_failed(
                spreadsheet_dataset_id, "permission_revoked"
            )
        _finish_spreadsheet_failure(
            db,
            edit_session,
            version_id,
            status="cancelled",
            error_code="permission_revoked",
        )
        return JobResult(
            job["id"], "cancelled", version_id, len(changed), len(unchanged), "permission_revoked"
        )
    except Exception:
        services.vector_store.delete_version(document["kb_id"], version_id)
        if spreadsheet_dataset_path is not None:
            spreadsheet_dataset_path.unlink(missing_ok=True)
        if spreadsheet_dataset_id is not None:
            db.mark_structured_dataset_failed(spreadsheet_dataset_id, "activation_failed")
        if edit_session is not None:
            _finish_spreadsheet_failure(
                db,
                edit_session,
                version_id,
                status="failed",
                error_code="activation_failed",
            )
        raise ActivationFailed(version_id) from None

    # Post-commit advisory repair.
    try:
        services.vector_store.mark_current(document["kb_id"], document_id, version_id)
    except Exception:
        _enqueue_repair(db, document_id, version_id)

    return JobResult(job["id"], "done", version_id, len(changed), len(unchanged))
