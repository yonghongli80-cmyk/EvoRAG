"""Compensating extraction lifecycle for one versioned spreadsheet dataset."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rag.materials.models import CitationLocation, ExtractedMaterial, ExtractedUnit
from rag.spreadsheets.chunker import DatasetMetadata, chunk_workbook
from rag.spreadsheets.parser import ParsedWorkbook
from rag.spreadsheets.store import DatasetManifest, write_dataset
from rag.user_db import UserDB

logger = logging.getLogger(__name__)


def workbook_index_material(
    workbook: ParsedWorkbook,
    metadata: DatasetMetadata,
    *,
    parser_schema_version: str = "spreadsheet-v1",
) -> tuple[ExtractedMaterial, dict[str, object]]:
    """Build stable semantic units and their rich spreadsheet chunk templates."""
    chunks = chunk_workbook(workbook, metadata)
    units = []
    templates = {}
    for chunk in chunks:
        if chunk.chunk_kind == "workbook_summary":
            unit_key = "workbook:summary"
        elif chunk.chunk_kind == "sheet_schema":
            unit_key = f"{chunk.sheet_key}:schema"
        else:
            unit_key = f"{chunk.sheet_key}:rows:{chunk.row_start}:{chunk.row_end}"
        units.append(
            ExtractedUnit(
                unit_key=unit_key,
                text=chunk.text,
                location=CitationLocation(
                    kind="cell_range",
                    value=unit_key,
                ),
                source="native_text",
                confidence=1.0,
                metadata={"unit_key": unit_key},
            )
        )
        templates[unit_key] = chunk
    return (
        ExtractedMaterial(
            version_id=metadata.document_version_id,
            parser_schema_version=parser_schema_version,
            units=tuple(units),
        ),
        templates,
    )


@dataclass(frozen=True)
class ExtractionContext:
    db: UserDB
    document_id: int
    document_version_id: int
    dataset_id: int
    kb_id: str
    filename: str
    dataset_path: Path
    workbook: ParsedWorkbook
    write_vectors: Callable[[ParsedWorkbook], None]
    delete_version_vectors: Callable[[int], None]
    mark_version_current: Callable[[int], None]
    mark_other_versions_historical: Callable[[int, int], None]


@dataclass(frozen=True)
class ExtractionResult:
    document_id: int
    document_version_id: int
    dataset_id: int
    manifest: DatasetManifest
    cleanup_repair_enqueued: bool


def extract_spreadsheet_version(context: ExtractionContext) -> ExtractionResult:
    """Write, vectorize, and atomically activate a spreadsheet version.

    The SQLite pointer, rather than Qdrant's eventually repaired flag, remains
    the authority for which version retrieval may expose.
    """
    vectors_written = False
    try:
        manifest = write_dataset(context.dataset_path, context.workbook)
        context.write_vectors(context.workbook)
        vectors_written = True
        context.mark_version_current(context.document_version_id)
        context.db.activate_document_and_dataset(
            document_id=context.document_id,
            version_id=context.document_version_id,
            dataset_id=context.dataset_id,
            kb_id=context.kb_id,
            filename=context.filename,
        )
    except Exception as exc:
        if vectors_written:
            try:
                context.delete_version_vectors(context.document_version_id)
            except Exception:
                logger.exception("spreadsheet vector rollback failed")
        context.dataset_path.unlink(missing_ok=True)
        context.db.mark_structured_dataset_failed(context.dataset_id, type(exc).__name__)
        raise

    repair_enqueued = False
    try:
        context.mark_other_versions_historical(context.document_id, context.document_version_id)
    except Exception:
        logger.exception("spreadsheet vector current-flag cleanup failed")
        context.db.enqueue_vector_flag_repair(context.document_id, context.document_version_id)
        repair_enqueued = True
    return ExtractionResult(
        document_id=context.document_id,
        document_version_id=context.document_version_id,
        dataset_id=context.dataset_id,
        manifest=manifest,
        cleanup_repair_enqueued=repair_enqueued,
    )
