"""Build reliability bundles from ACL-filtered, versioned chunks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rag.models import Chunk
from rag.reliability.models import EvidenceBundle, EvidenceRef, EvidenceRole, RetrievalMetrics


def _to_ref(
    item: Chunk,
    *,
    kb_id: str,
    rank: int,
    role: EvidenceRole,
) -> EvidenceRef:
    if item.document_id is None or item.document_version_id is None:
        raise ValueError("versioned evidence required")
    return EvidenceRef(
        document_id=item.document_id,
        document_version_id=item.document_version_id,
        version_no=item.version_no,
        kb_id=kb_id,
        section_title=item.section_title or "",
        chunk_index=item.chunk_index,
        score=item.rerank_score,
        score_source=item.score_source,
        rank=rank,
        is_current=item.historical_weight >= 1.0,
        validity_status=item.validity_status,
        acl_checked=True,
        role=role,
        updated_at=item.updated_at,
    )


def build_evidence_bundle(
    question: str,
    chunks: Sequence[Chunk],
    *,
    kb_id: str,
    acl_checked: bool,
    structured_results: Sequence[Mapping[str, Any]] = (),
    conflicts: Sequence[Mapping[str, Any]] = (),
    permission_limited: bool = False,
    background: Sequence[Chunk] = (),
) -> EvidenceBundle:
    """Build the evidence bundle from the chunks an answer may use.

    ``chunks`` are the passages the request is allowed to answer from. ``background``
    name passages that are only cited: Jev judges them relevant without answer
    evidence, so they stay in the bundle with ``role="background"`` — visible in the
    retrieval metrics and the citation list, never counted as answer evidence (A-03).
    """
    if not acl_checked:
        raise ValueError("ACL-filtered evidence required")

    current: list[EvidenceRef] = []
    historical: list[EvidenceRef] = []
    for rank, item in enumerate(chunks):
        ref = _to_ref(item, kb_id=kb_id, rank=rank, role="evidence")
        (current if ref.is_current else historical).append(ref)
    for offset, item in enumerate(background):
        ref = _to_ref(item, kb_id=kb_id, rank=len(chunks) + offset, role="background")
        (current if ref.is_current else historical).append(ref)

    all_refs = current + historical
    current_scores = [item.score for item in current if item.score is not None]
    metrics = RetrievalMetrics(
        visible_candidates=len(all_refs),
        top_score=max(current_scores) if current_scores else None,
        average_score=(sum(current_scores) / len(current_scores)) if current_scores else None,
        permission_limited=permission_limited,
    )
    return EvidenceBundle(
        question=question,
        current_evidence=tuple(current),
        historical_evidence=tuple(historical),
        structured_results=tuple(dict(item) for item in structured_results),
        conflicts=tuple(dict(item) for item in conflicts),
        retrieval_metrics=metrics,
    )
