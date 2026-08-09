"""Frozen material-lifecycle contracts shared by every task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ExtractedUnit:
    unit_key: str
    text: str
    location: CitationLocation
    source: Literal["native_text", "local_ocr", "metadata"]
    confidence: float
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedMaterial:
    version_id: int
    parser_schema_version: str
    units: tuple[ExtractedUnit, ...] = ()
    warnings: tuple[str, ...] = ()

CitationKind = Literal[
    "paragraph", "page", "page_region", "slide", "cell_range", "metadata", "archive_entry"
]


class CitationLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: CitationKind
    value: str


class FormatCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str
    extensions: frozenset[str]
    mime_types: frozenset[str]
    magic_families: frozenset[str]
    archive: bool = True
    preview: bool = False
    extract_text: bool = False
    local_ocr: bool = False
    structured_query: bool = False
    semantic_index: bool = False
    archive_container: bool = False
    citation_kind: CitationKind


class FormatResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    state: Literal[
        "supported", "archive_only", "temporarily_unavailable", "format_mismatch", "rejected"
    ]
    capability: FormatCapability | None
    detected_mime: str
    reason_code: str
    can_index: bool


class MaterialLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    file_bytes: int = 50 * 1024 * 1024
    archive_bytes: int = 200 * 1024 * 1024
    archive_expanded_bytes: int = 1024 * 1024 * 1024
    archive_entry_count: int = 1000
    archive_nesting_depth: int = 2
    archive_ratio: int = 100
    stage_chunk_bytes: int = 64 * 1024
    max_embedding_concurrency: int = 4


class MaterialState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version_status: Literal[
        "staging", "indexing", "current", "historical", "recycle_pending", "failed", "purged"
    ]
    document_deleted: bool = False


class IndexJobType(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    requires_parse: bool = True


class MaterialLocationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: Annotated[str, Field(min_length=1, max_length=500)]
    subject: Literal["self"] | int | None = None
    document_types: tuple[str, ...] = ()
    uploaded_after: datetime | None = None
    logical_path_prefix: str | None = None
    top_k: Annotated[int, Field(ge=1, le=50)] = 10
