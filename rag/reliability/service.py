"""Application service for executing and persisting document comparisons."""

from __future__ import annotations

import asyncio
from typing import Any

from rag.reliability.llama_workflow import compare_documents


async def execute_persisted_comparison(
    comparison_request: Any,
    scope: Any,
    document_tool: Any,
    user_db: Any,
    actor_id: int,
):
    """Execute a scoped comparison, then persist its citations in one transaction."""
    execution = await compare_documents(comparison_request, scope, document_tool)
    return await asyncio.to_thread(
        user_db.persist_document_comparison,
        actor_id,
        comparison_request,
        execution,
    )
