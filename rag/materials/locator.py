"""ACL-first hybrid material location and authorized downloads.

Visibility is resolved first: the locator only ever considers the user's
visible, non-deleted, current documents. Exact subject/document-type matches,
FTS hits, and semantic retrieval all operate inside that authorized set, so an
invisible filename can never reach the model or the response. Download actions
are backend-generated URLs that the download route rechecks against ACL on
every request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rag.materials.locator_visibility import iter_visible_material_documents
from rag.materials.models import CitationLocation, MaterialLocationQuery
from rag.user_db import UserDB


@dataclass(frozen=True)
class LocationCitation:
    """Carries a ``CitationLocation`` under ``.location`` for callers."""

    location: CitationLocation


@dataclass(frozen=True)
class MaterialLocationResult:
    document_id: int
    document_version_id: int
    filename: str
    logical_path: str
    document_type: str
    updated_at: float | None
    ocr_warning: bool
    citation: LocationCitation
    rank: float
    source: str


def _visible_documents(db: UserDB, user: dict) -> dict[int, dict]:
    """Map of visible, non-deleted, current document permissions by id.

    Compatibility wrapper preserving the complete legacy mapping shape. The
    locator's default path consumes the iterator directly to avoid this mapping.
    """
    return dict(iter_visible_material_documents(db, user))


_ORIGINAL_VISIBLE_DOCUMENTS = _visible_documents


def material_result_response(result: MaterialLocationResult, user: dict, db: UserDB) -> dict:
    """Backend-generated, ACL-rechecked download action plus safe metadata."""
    return {
        "document_id": result.document_id,
        "document_version_id": result.document_version_id,
        "filename": result.filename,
        "logical_path": result.logical_path,
        "document_type": result.document_type,
        "updated_at": result.updated_at,
        "ocr_warning": result.ocr_warning,
        "citation": {
            "location": {
                "kind": result.citation.location.kind,
                "value": result.citation.location.value,
            }
        },
        "download_url": (f"/documents/{result.document_id}/versions/{result.document_version_id}/download"),
    }


class MaterialLocator:
    def __init__(
        self,
        db: UserDB,
        *,
        semantic_retriever: Callable[[str, set[int], int], list] | None = None,
        visible_docs: Callable[[UserDB, dict], dict[int, dict]] | None = None,
    ):
        self.db = db
        self.semantic_retriever = semantic_retriever
        self._visible_docs = _visible_documents if visible_docs is None else visible_docs
        self._uses_default_visibility = self._visible_docs is _ORIGINAL_VISIBLE_DOCUMENTS
        # Authorized metadata the model was allowed to see (diagnostics only).
        self.model_input = ""

    def locate(self, query: MaterialLocationQuery, user: dict) -> list[MaterialLocationResult]:
        if query.uploaded_after is not None:
            timestamp = query.uploaded_after.timestamp()

        def matches_filters(document: dict) -> bool:
            if query.subject == "self" and document.get("subject_user_id") != user["id"]:
                return False
            if isinstance(query.subject, int) and document.get("subject_user_id") != query.subject:
                return False
            if query.document_types and document.get("document_type") not in query.document_types:
                return False
            if query.uploaded_after is not None and (document.get("updated_at") or 0) < timestamp:
                return False
            return not query.logical_path_prefix or (document.get("logical_path") or "").startswith(
                query.logical_path_prefix
            )

        use_default = self._uses_default_visibility and self._visible_docs is _ORIGINAL_VISIBLE_DOCUMENTS
        versions: dict[int, dict] | None = None
        if use_default:
            docs = {
                document_id: document
                for document_id, document in iter_visible_material_documents(self.db, user)
                if document.get("current_version_id") is not None and matches_filters(document)
            }
        else:
            visible = self._visible_docs(self.db, user)
            docs = dict(visible)
            versions = {}
            for document_id, document in list(docs.items()):
                if document.get("current_version_id") is None:
                    docs.pop(document_id, None)
                    continue
                if document.get("current_filename") is not None:
                    versions[document_id] = {
                        "id": document["current_version_id"],
                        "original_filename": document["current_filename"],
                        "validity_status": document.get("current_validity_status"),
                    }
                    continue
                current = self.db.get_current_document_version(document_id)
                if current is None:
                    docs.pop(document_id, None)
                else:
                    versions[document_id] = current

        if not use_default:
            docs = {document_id: doc for document_id, doc in docs.items() if matches_filters(doc)}
        if not docs:
            self.model_input = ""
            return []

        # The model only ever sees already-authorized metadata.
        self.model_input = "\n".join(
            f"{doc['doc_name']} {doc.get('logical_path') or ''} {doc.get('document_type') or ''}"
            for doc in docs.values()
        )

        allowed_version_ids = None
        if self.semantic_retriever is not None:
            if use_default:
                allowed_version_ids = {doc["current_version_id"] for doc in docs.values()}
            else:
                assert versions is not None
                allowed_version_ids = {versions[document_id]["id"] for document_id in docs}
        fts_hits = self.db.search_material_search_fts(query.text, query.top_k)
        semantic_hits = (
            self.semantic_retriever(query.text, allowed_version_ids, query.top_k)
            if self.semantic_retriever is not None
            else []
        )

        results: list[MaterialLocationResult] = []
        seen: set[int] = set()

        def add(rank: float, source: str, document_id: int, citation: CitationLocation) -> None:
            if document_id in seen or document_id not in docs:
                return
            seen.add(document_id)
            doc = docs[document_id]
            if use_default:
                version_id = doc["current_version_id"]
                filename = doc["current_filename"]
                validity_status = doc.get("current_validity_status")
            else:
                assert versions is not None
                version = versions[document_id]
                version_id = version["id"]
                filename = version["original_filename"]
                validity_status = version.get("validity_status")
            results.append(
                MaterialLocationResult(
                    document_id=document_id,
                    document_version_id=version_id,
                    filename=filename,
                    logical_path=doc.get("logical_path") or "",
                    document_type=doc.get("document_type") or "",
                    updated_at=doc.get("updated_at"),
                    ocr_warning=bool(validity_status == "reported_outdated"),
                    citation=LocationCitation(location=citation),
                    rank=rank,
                    source=source,
                )
            )

        # Exact employee subject outranks similarity.
        if query.subject is not None:
            for document_id, doc in docs.items():
                is_exact_subject = (query.subject == "self" and doc.get("subject_user_id") == user["id"]) or (
                    isinstance(query.subject, int) and doc.get("subject_user_id") == query.subject
                )
                if is_exact_subject:
                    add(0.0, "exact", document_id, CitationLocation(kind="metadata", value="subject"))

        for hit in fts_hits:
            add(1.0, "fts", hit["document_id"], CitationLocation(kind="metadata", value="fts"))

        for hit in semantic_hits:
            document_id = getattr(hit, "document_id", None)
            citation = getattr(hit, "citation", None)
            if document_id is not None and citation is not None:
                add(0.9, "semantic", document_id, citation)

        return results[: query.top_k]
