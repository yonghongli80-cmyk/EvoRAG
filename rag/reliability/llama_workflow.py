"""Narrow LlamaIndex Workflow adapter for pre-authorized document comparison."""

from __future__ import annotations

from llama_index.core.workflow import StartEvent, StopEvent, Workflow, step

from config import settings
from rag.reliability.comparison import (
    ComparisonCellDraft,
    ComparisonExecutionResult,
    ComparisonRequest,
    ComparisonRowDraft,
    DocumentEvidenceTool,
    insufficient_evidence_cell,
    validate_comparison,
)
from rag.reliability.models import ComparisonScope


class ComparisonStartEvent(StartEvent):
    """Only immutable request and permission scope cross the workflow boundary."""

    request: ComparisonRequest
    scope: ComparisonScope


class LlamaComparisonWorkflow(Workflow):
    def __init__(self, document_tool: DocumentEvidenceTool) -> None:
        super().__init__()
        self._document_tool = document_tool

    @step
    async def collect_document_evidence(self, event: ComparisonStartEvent) -> StopEvent:
        values: dict[int, ComparisonCellDraft] = {}
        failures: list[str] = []
        for document_id in event.request.document_ids:
            try:
                evidence = await self._document_tool.query_document(
                    document_id,
                    event.request.question,
                    event.scope,
                )
            except Exception:
                values[document_id] = insufficient_evidence_cell()
                failures.append("document query failed")
                continue

            if evidence.document_id != document_id or not evidence.text or not evidence.citations:
                values[document_id] = insufficient_evidence_cell()
                failures.append("document evidence unavailable")
                continue
            if any(citation.document_id != document_id for citation in evidence.citations):
                values[document_id] = insufficient_evidence_cell()
                failures.append("document evidence unavailable")
                continue
            values[document_id] = ComparisonCellDraft(
                status="supported",
                text=evidence.text,
                citations=evidence.citations,
            )

        dimensions = event.request.dimensions or ("comparison",)
        result = ComparisonExecutionResult(
            rows=tuple(ComparisonRowDraft(dimension=dimension, values=values) for dimension in dimensions),
            partial=bool(failures),
            failures=tuple(failures),
        )
        return StopEvent(result=validate_comparison(result))


async def compare_documents(
    request: ComparisonRequest,
    scope: ComparisonScope,
    document_tool: DocumentEvidenceTool,
) -> ComparisonExecutionResult:
    """Compare only document IDs pre-authorized in the immutable scope."""
    if len(request.document_ids) > settings.comparison_max_documents:
        raise ValueError("comparison document limit exceeded")
    if not set(request.document_ids).issubset(scope.allowed_document_ids):
        raise PermissionError("document comparison scope denied")

    workflow = LlamaComparisonWorkflow(document_tool)
    return await workflow.run(start_event=ComparisonStartEvent(request=request, scope=scope))
