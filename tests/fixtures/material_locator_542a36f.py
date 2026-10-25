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

from rag.materials.models import CitationLocation, MaterialLocationQuery
from rag.org_auth.models import OrgRole
from rag.org_auth.repository import OrgRepository, is_org_policy
from rag.org_auth.service import OrgAuthorizationService
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

    One query loads every live document with its current version, and
    authorization is decided once per knowledge base instead of once per
    document (D observation: the previous loop performed a permission lookup and
    a current-version lookup for every row).  The decision is equivalent to
    :func:`rag.api_security.require_doc_view`:

    * organization KB: one READ authorization covers every document of the KB;
    * legacy KB: the batched ``get_accessible_doc_names`` projection applies the
      same owner/public/shared rules as ``check_doc_permission``;
    * administrators see every live document.
    """
    rows = db.material_repo.list_material_documents_with_current_version()
    documents_by_kb: dict[str, list[dict]] = {}
    for row in rows:
        documents_by_kb.setdefault(row["kb_id"], []).append(row)

    visible: dict[int, dict] = {}
    for kb_id, documents in documents_by_kb.items():
        policy = OrgRepository(db).get_kb_policy(kb_id)
        if is_org_policy(policy):
            if OrgAuthorizationService(db).authorize(
                int(user["id"]), kb_id, OrgRole.READ
            ).allowed:
                for document in documents:
                    visible[int(document["id"])] = document
            continue
        if user.get("is_admin"):
            for document in documents:
                visible[int(document["id"])] = document
            continue
        allowed_names = set(db.get_accessible_doc_names(kb_id, user["id"]))
        for document in documents:
            if document["doc_name"] in allowed_names:
                visible[int(document["id"])] = document
    return visible


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
        "download_url": (
            f"/documents/{result.document_id}/versions/{result.document_version_id}/download"
        ),
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
        self._visible_docs = visible_docs or _visible_documents
        # Authorized metadata the model was allowed to see (diagnostics only).
        self.model_input = ""

    def locate(self, query: MaterialLocationQuery, user: dict) -> list[MaterialLocationResult]:
        visible = self._visible_docs(self.db, user)
        if not visible:
            self.model_input = ""
            return []

        docs = dict(visible)
        versions = {}
        for document_id, document in list(docs.items()):
            if document.get("current_version_id") is None:
                docs.pop(document_id, None)
                continue
            if document.get("current_filename") is not None:
                # The batched visibility query already carries the current
                # version, so no per-document lookup is needed here.
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

        # Apply explicit subject/type/date/path filters inside the ACL set.
        if query.subject == "self":
            docs = {k: v for k, v in docs.items() if v.get("subject_user_id") == user["id"]}
        elif isinstance(query.subject, int):
            docs = {k: v for k, v in docs.items() if v.get("subject_user_id") == query.subject}
        if query.document_types:
            docs = {k: v for k, v in docs.items() if v.get("document_type") in query.document_types}
        if query.uploaded_after is not None:
            timestamp = query.uploaded_after.timestamp()
            docs = {
                k: v for k, v in docs.items() if (v.get("updated_at") or 0) >= timestamp
            }
        if query.logical_path_prefix:
            docs = {
                k: v
                for k, v in docs.items()
                if (v.get("logical_path") or "").startswith(query.logical_path_prefix)
            }
        if not docs:
            self.model_input = ""
            return []

        # The model only ever sees already-authorized metadata.
        self.model_input = "\n".join(
            f"{doc['doc_name']} {doc.get('logical_path') or ''} {doc.get('document_type') or ''}"
            for doc in docs.values()
        )

        allowed_version_ids = {versions[d]["id"] for d in docs}
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
            version = versions[document_id]
            results.append(
                MaterialLocationResult(
                    document_id=document_id,
                    document_version_id=version["id"],
                    filename=version["original_filename"],
                    logical_path=doc.get("logical_path") or "",
                    document_type=doc.get("document_type") or "",
                    updated_at=doc.get("updated_at"),
                    ocr_warning=bool(version.get("validity_status") == "reported_outdated"),
                    citation=LocationCitation(location=citation),
                    rank=rank,
                    source=source,
                )
            )

        # Exact employee subject outranks similarity.
        for document_id, doc in docs.items():
            is_exact_subject = (
                (query.subject == "self" and doc.get("subject_user_id") == user["id"])
                or (isinstance(query.subject, int) and doc.get("subject_user_id") == query.subject)
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
