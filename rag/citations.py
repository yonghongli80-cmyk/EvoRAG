"""Citation serialization, persistence records, and safe response mapping."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CitationRecord:
    document_id: int
    document_version_id: int
    chunk_index: int
    section_title: str
    text_preview: str
    rank: int
    is_historical: bool
    source_kind: str = "document"
    kb_id: str = ""
    source_name: str = ""


def citation_records_from_sources(sources: list[dict], default_kb_id: str = "") -> list[CitationRecord]:
    """Convert live pipeline sources into durable citation records."""
    records = []
    for source in sources or []:
        document_id = source.get("document_id")
        version_id = source.get("document_version_id")
        if document_id is None or version_id is None:
            continue
        records.append(
            CitationRecord(
                document_id=document_id,
                document_version_id=version_id,
                chunk_index=source.get("chunk_index", 0),
                section_title=source.get("section_title") or "",
                text_preview=(source.get("text_preview") or "")[:200],
                rank=source.get("rank", 0),
                is_historical=bool(source.get("is_historical", False)),
                source_kind=source.get("source_kind") or source.get("chunk_kind") or "document",
                kb_id=source.get("kb_id") or default_kb_id,
                source_name=source.get("source_name") or source.get("doc_name") or "",
            )
        )
    return records


def citation_response(db, citation: dict, user: dict) -> dict:
    """Map a persisted citation row to a safe response.

    Physical paths, source credentials, and vector IDs never appear here.
    """
    document_id = citation["document_id"]
    source_kind = citation.get("source_kind") or "document"
    document = (
        db.get_document_permission_by_id(document_id)
        if source_kind == "document"
        else None
    )
    payload = {
        "citation_id": citation["id"],
        "document_id": document_id,
        "document_version_id": citation["document_version_id"],
        "source_kind": source_kind,
        "kb_id": citation.get("kb_id") or (document["kb_id"] if document else ""),
        "source_name": citation.get("source_name") or (document["doc_name"] if document else ""),
        "doc_name": (document["doc_name"] if document else citation.get("source_name", "")),
        "section_title": citation.get("section_title") or "",
        "text_preview": citation.get("text_preview") or "",
        "rank": citation.get("rank", 0),
        "is_historical": bool(citation.get("is_historical", 0)),
        "version_no": citation.get("version_no"),
    }
    if source_kind == "document":
        payload["download_url"] = (
            f"/documents/{document_id}/versions/{citation['document_version_id']}/download"
        )
    elif source_kind == "wiki":
        payload["wiki_url"] = f"/wiki/pages/{document_id}"
    return payload
