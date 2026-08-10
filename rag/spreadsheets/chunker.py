"""Render version-aware spreadsheet chunks for semantic retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field

from rag.models import Chunk
from rag.spreadsheets.parser import ParsedRow, ParsedSheet, ParsedWorkbook


@dataclass(frozen=True)
class DatasetMetadata:
    dataset_id: int
    document_id: int
    document_version_id: int
    document_name: str
    sheet_ids: dict[str, int] = field(default_factory=dict)


def chunk_workbook(workbook: ParsedWorkbook, metadata: DatasetMetadata) -> list[Chunk]:
    """Emit a bounded summary, schema and row-range chunks for a workbook."""
    chunks = [_workbook_summary(workbook, metadata)]
    for sheet in workbook.sheets:
        chunks.append(_schema_chunk(sheet, metadata))
        chunks.extend(_row_group_chunks(sheet, metadata))
    return chunks


def _workbook_summary(workbook: ParsedWorkbook, metadata: DatasetMetadata) -> Chunk:
    description = "; ".join(
        f"{sheet.name} ({len(sheet.rows)} rows, {len(sheet.columns)} columns)"
        for sheet in workbook.sheets
    )
    return _chunk(
        text=f"Workbook {metadata.document_name}: {description}",
        metadata=metadata,
        chunk_index=0,
        kind="workbook_summary",
    )


def _schema_chunk(sheet: ParsedSheet, metadata: DatasetMetadata) -> Chunk:
    columns = "\t".join(
        f"{column.display_name} ({column.inferred_type.value})" for column in sheet.columns
    )
    return _chunk(
        text=f"Sheet {sheet.name}\n{columns}",
        metadata=metadata,
        chunk_index=sheet.ordinal,
        kind="sheet_schema",
        sheet=sheet,
        column_keys=tuple(column.column_key for column in sheet.columns),
    )


def _row_group_chunks(sheet: ParsedSheet, metadata: DatasetMetadata) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[ParsedRow] = []
    for row in sheet.rows:
        candidate = [*current, row]
        if current and (len(candidate) > 50 or _token_estimate(_render_rows(sheet, candidate)) > 1200):
            chunks.append(_row_group_chunk(sheet, current, metadata))
            current = [row]
        else:
            current = candidate
    if current:
        chunks.append(_row_group_chunk(sheet, current, metadata))
    return chunks


def _row_group_chunk(sheet: ParsedSheet, rows: list[ParsedRow], metadata: DatasetMetadata) -> Chunk:
    return _chunk(
        text=_render_rows(sheet, rows),
        metadata=metadata,
        chunk_index=rows[0].row_number,
        kind="row_group",
        sheet=sheet,
        row_start=rows[0].row_number,
        row_end=rows[-1].row_number,
        column_keys=tuple(column.column_key for column in sheet.columns),
    )


def _render_rows(sheet: ParsedSheet, rows: list[ParsedRow]) -> str:
    lines = ["\t".join(sheet.headers)]
    lines.extend("\t".join("" if cell.value is None else str(cell.value) for cell in row.cells) for row in rows)
    return "\n".join(lines)


def _token_estimate(value: str) -> int:
    return max(1, (len(value) + 3) // 4)


def _chunk(
    *,
    text: str,
    metadata: DatasetMetadata,
    chunk_index: int,
    kind: str,
    sheet: ParsedSheet | None = None,
    row_start: int | None = None,
    row_end: int | None = None,
    column_keys: tuple[str, ...] = (),
) -> Chunk:
    return Chunk(
        text=text,
        doc_name=metadata.document_name,
        chunk_index=chunk_index,
        document_id=metadata.document_id,
        document_version_id=metadata.document_version_id,
        dataset_id=metadata.dataset_id,
        sheet_id=metadata.sheet_ids.get(sheet.sheet_key) if sheet else None,
        sheet_key=sheet.sheet_key if sheet else None,
        sheet_name=sheet.name if sheet else None,
        chunk_kind=kind,
        row_start=row_start,
        row_end=row_end,
        column_keys=column_keys,
    )
