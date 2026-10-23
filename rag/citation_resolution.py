"""Conservative recovery for answer citations written before source domains existed."""

from __future__ import annotations

from rag.wiki.chunker import MarkdownASTChunker

_WIKI_CHUNK_PREFIX = "[Wiki:"


def resolve_legacy_citation(db, citation: dict, wiki_store=None) -> dict:
    """Resolve a legacy citation only when its source can be proven from stored data.

    Wiki chunks have carried a ``[Wiki: ...]`` breadcrumb since the Wiki chunker
    was introduced. That marker prevents a deleted Wiki page's colliding numeric
    IDs from being guessed as a document citation. Unmarked rows require an
    exact document/version relationship. Marked rows require an exact historical
    Wiki revision/chunk match and no matching document/version candidate.
    """
    if (citation.get("source_kind") or "legacy") != "legacy":
        return citation

    document_id = int(citation["document_id"])
    version_id = int(citation["document_version_id"])
    preview = citation.get("text_preview") or ""
    is_wiki_marked = preview.startswith(_WIKI_CHUNK_PREFIX)

    try:
        document = db.get_document_permission_by_id(document_id)
        version = db.get_document_version_by_id(version_id)
    except Exception:  # noqa: BLE001 - failed lookups leave the source unresolved
        return citation
    document_candidate = bool(
        document and version and int(version.get("document_id", -1)) == document_id
    )

    if is_wiki_marked:
        try:
            if wiki_store is None:
                from rag.wiki.store import get_wiki_store

                wiki_store = get_wiki_store()
            page = wiki_store.get_page(document_id)
            revision = wiki_store.get_revision(version_id)
        except Exception:  # noqa: BLE001 - failed Wiki lookup leaves source unresolved
            return citation
        if not page or not revision or revision.page_id != page.id or document_candidate:
            return citation

        historic_page = page.model_copy(
            update={
                "title": revision.title,
                "content": revision.content,
                "current_revision_id": revision.id,
            }
        )
        matching = next(
            (
                chunk
                for chunk in MarkdownASTChunker().chunk_page(historic_page)
                if chunk.chunk_index == int(citation.get("chunk_index", -1))
            ),
            None,
        )
        if (
            matching is None
            or matching.section_path != (citation.get("section_title") or "")
            or matching.text[:100] != preview
        ):
            return citation
        resolved = {
            **citation,
            "source_kind": "wiki",
            "kb_id": page.kb_id,
            "source_name": revision.title,
        }
    else:
        if not preview or preview.startswith(_WIKI_CHUNK_PREFIX) or not document_candidate:
            return citation
        # An unmarked document row is resolvable only because Wiki source text
        # always has the synthetic breadcrumb prefix. The relationship check
        # above also proves the exact document/version pair.
        resolved = {
            **citation,
            "source_kind": "document",
            "kb_id": str(document["kb_id"]),
            "source_name": str(document["doc_name"]),
            "version_no": version.get("version_no"),
        }

    db.chat_repo.resolve_legacy_citation_domain(
        int(citation["id"]),
        resolved["source_kind"],
        resolved["kb_id"],
        resolved["source_name"],
    )
    stored = db.get_answer_citation_by_id(int(citation["id"]))
    return {**resolved, **stored} if stored else resolved
