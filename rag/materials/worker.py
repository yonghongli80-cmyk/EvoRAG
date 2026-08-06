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

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from rag.document_version_store import resolve_version_path
from rag.materials.embedding_cache import EmbeddingCache
from rag.materials.formats import FormatCapabilityRegistry
from rag.materials.incremental import fingerprint_material
from rag.materials.models import ExtractedMaterial, FormatResolution
from rag.models import Chunk
from rag.user_db import UserDB


class ActivationFailed(Exception):
    """Raised when the activation transaction fails before commit."""


class _StaleVersionError(Exception):
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
    if db.get_latest_version_id(document_id) != version_id:
        return JobResult(job["id"], "cancelled", version_id, 0, 0, "stale_version")

    path = resolve_version_path(services.versions_root, version["file_path"])
    signature = b""
    try:
        with open(path, "rb") as fh:
            signature = fh.read(16)
    except OSError:
        pass
    resolution = services.registry.resolve(version["original_filename"], None, signature)
    material = services.extractor(path, version_id, resolution)

    current_fps = fingerprint_material(material, services.parser_schema_version)
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
        chunks = [
            Chunk(
                text=unit.text,
                doc_name=document["doc_name"],
                chunk_index=index,
                document_id=document_id,
                document_version_id=version_id,
                version_no=version["version_no"],
                is_current=True,
            )
            for index, unit in enumerate(changed_units)
        ]

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
            db._activate_document_version_in_tx(
                document_id, version_id, document["kb_id"], document["doc_name"]
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
    except _StaleVersionError:
        services.vector_store.delete_version(document["kb_id"], version_id)
        return JobResult(job["id"], "cancelled", version_id, len(changed), len(unchanged), "stale_version")
    except Exception:
        services.vector_store.delete_version(document["kb_id"], version_id)
        raise ActivationFailed(version_id) from None

    # Post-commit advisory repair.
    try:
        services.vector_store.mark_current(document["kb_id"], document_id, version_id)
    except Exception:
        _enqueue_repair(db, document_id, version_id)

    return JobResult(job["id"], "done", version_id, len(changed), len(unchanged))
