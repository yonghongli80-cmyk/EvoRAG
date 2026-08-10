"""Typed, citation-preserving contracts for document comparison."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rag.reliability.models import ComparisonScope

ComparisonCellStatus = Literal["supported", "insufficient_evidence"]


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1)
    document_ids: tuple[int, ...] = Field(min_length=1)
    dimensions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_distinct_documents(self) -> ComparisonRequest:
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("comparison documents must be distinct")
        return self


class DocumentEvidence(BaseModel):
    """A tool result that has already been constrained to one visible document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int
    text: str = ""
    citations: tuple[ComparisonCitationDraft, ...] = ()


class ComparisonCitationDraft(BaseModel):
    """Source location retained until a comparison is persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: int
    document_version_id: int
    chunk_index: int
    section_title: str
    text_preview: str
    rank: int


class ComparisonCellDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonCellStatus
    text: str = ""
    citations: tuple[ComparisonCitationDraft, ...] = ()


class ComparisonRowDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: str
    values: Mapping[int, ComparisonCellDraft]


class ComparisonExecutionResult(BaseModel):
    """Internal comparison result that retains real citation locations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[ComparisonRowDraft, ...]
    partial: bool
    failures: tuple[str, ...] = ()


class ComparisonCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ComparisonCellStatus
    text: str = ""
    citation_ids: tuple[int, ...] = ()


class ComparisonRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: str
    values: Mapping[int, ComparisonCell]


class ComparisonResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rows: tuple[ComparisonRow, ...]
    partial: bool
    failures: tuple[str, ...] = ()


class DocumentEvidenceTool(Protocol):
    async def query_document(
        self,
        document_id: int,
        question: str,
        context: ComparisonScope,
    ) -> DocumentEvidence: ...


class ToolTimeout(TimeoutError):
    """A bounded per-document query exceeded its allotted time."""


def insufficient_evidence_cell() -> ComparisonCellDraft:
    return ComparisonCellDraft(status="insufficient_evidence")


def validate_comparison(result: ComparisonExecutionResult) -> ComparisonExecutionResult:
    for row in result.rows:
        for value in row.values.values():
            if value.status == "supported" and not value.citations:
                raise ValueError("supported comparison cell requires citation")
    return result
