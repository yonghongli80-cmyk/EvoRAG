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


def citation_response(db, citation: dict, user: dict) -> dict:
    """Map a persisted citation row to a safe response.

    Physical paths, source credentials, and vector IDs never appear here.
    """
    document_id = citation["document_id"]
    document = db.get_document_permission_by_id(document_id)
    payload = {
        "citation_id": citation["id"],
        "document_id": document_id,
        "document_version_id": citation["document_version_id"],
        "doc_name": document["doc_name"] if document else "",
        "kb_id": document["kb_id"] if document else "",
        "section_title": citation.get("section_title") or "",
        "text_preview": citation.get("text_preview") or "",
        "rank": citation.get("rank", 0),
        "is_historical": bool(citation.get("is_historical", 0)),
        "version_no": citation.get("version_no"),
        "download_url": f"/documents/{document_id}/versions/{citation['document_version_id']}/download",
    }
    return payload
