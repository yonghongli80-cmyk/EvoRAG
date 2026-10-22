"""Current-first retrieval with ACL-filtered historical fallback.

SQLite ``document_permissions.current_version_id`` is the authoritative
current-version source. ``is_current`` in Qdrant is only repairable metadata
and is never the sole visibility condition.

**Two version-id spaces** (S4 review, 2026-10-22): a wiki revision id and a
``document_versions.id`` are unrelated numbers that can collide (wiki revision 7
vs document version 7). They therefore travel as **two separate scopes** —
``documents`` (matched against non-wiki chunks' ``document_version_id``) and
``wiki`` (matched against ``chunk_kind == "wiki"`` chunks' ``document_version_id``,
which for wiki points is the revision id). Merging them into one set made a
visible wiki revision unlock a colliding document version.

Administrators bypass visibility filters here exactly as they do at the chunk
level (``filter_chunks_by_permission``) and in the tags endpoint: both scopes are
``None`` for an admin, which the retriever translates into "no version filter"
instead of an empty candidate set.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from config import settings


@dataclass(frozen=True)
class VersionScope:
    """One visibility scope, split by the chunk domain the ids belong to.

    ``None`` means "no restriction" (administrator). A set — including the empty
    set — means exactly those ids are visible.
    """

    documents: set[int] | None
    wiki: set[int] | None

    def is_empty(self) -> bool:
        """True when nothing at all is visible (both scopes restricted to nothing)."""
        return self.documents == set() and self.wiki == set()

    def union(self) -> set[int] | None:
        """The legacy merged view, kept for callers/tests that need one set."""
        if self.documents is None and self.wiki is None:
            return None
        merged: set[int] = set()
        if self.documents:
            merged |= self.documents
        if self.wiki:
            merged |= self.wiki
        return merged


def _visible_document_ids(user_db, user, kb_id) -> set[int] | None:
    if user is None or user_db is None:
        return set()
    if user.get("is_admin"):
        return None
    from rag.org_auth.repository import OrgRepository, is_org_policy
    policy = OrgRepository(user_db).get_kb_policy(kb_id) if hasattr(user_db, "locked_read") else None
    if is_org_policy(policy):
        from rag.org_auth.service import OrgAuthorizationService
        return set(OrgAuthorizationService(user_db).allowed_document_ids(int(user["id"]), kb_id))
    names = user_db.get_accessible_doc_names(kb_id, user["id"], user.get("permission_level", 1))
    return {
        permission["id"] for name in names
        if (permission := user_db.get_document_permission(name, kb_id)) is not None
    }


def _can_user_view_kb(user_db, user, kb_id) -> bool:
    if user is None or user_db is None:
        return False
    if user.get("is_admin"):
        return True
    try:
        from rag.api_security import require_kb_view
        require_kb_view(user_db, kb_id, user)
        return True
    except Exception:
        return False


def _get_wiki_store_instance(wiki_store=None):
    if wiki_store is not None:
        return wiki_store
    try:
        from rag.wiki.api import get_wiki_store
        return get_wiki_store()
    except Exception:
        try:
            from rag.wiki.store import get_wiki_store
            return get_wiki_store()
        except Exception:
            return None


def _visible_permissions(user_db, user, kb_id) -> list[dict]:
    """Every document permission record this principal may see, fetched once.

    The permission lookup used to run twice per request (once for the current scope, once for
    the historical one). One list now feeds both (A-09).
    """
    if not hasattr(user_db, "get_document_permission_by_id"):
        return [
            permission
            for name in user_db.get_accessible_doc_names(
                kb_id, user["id"], user.get("permission_level", 1)
            )
            if (permission := user_db.get_document_permission(name, kb_id)) is not None
        ]
    document_ids = _visible_document_ids(user_db, user, kb_id)
    if document_ids is None:
        return []
    permissions = []
    for document_id in document_ids:
        permission = user_db.get_document_permission_by_id(document_id)
        if permission is not None:
            permissions.append(permission)
    return permissions


def _visible_document_version_ids(user_db, user, kb_id) -> tuple[set[int], set[int]]:
    """(current version ids, historical version ids) in one pass over the visible documents."""
    current: set[int] = set()
    historical: set[int] = set()
    for permission in _visible_permissions(user_db, user, kb_id):
        if permission.get("current_version_id"):
            current.add(permission["current_version_id"])
        for version in user_db.list_document_versions(permission["id"]):
            if (
                version["status"] != "current"
                or version["validity_status"] == "confirmed_outdated"
            ):
                historical.add(version["id"])
    return current, historical


#: Only the revision id is needed for visibility. ``list_pages`` selects ``SELECT *`` —
#: page content included — so a per-request scan of every wiki body just to read ids was the
#: N+1/full-scan cost A-09 reported.
_WIKI_CURRENT_REVISION_SQL = (
    "SELECT current_revision_id FROM wiki_pages WHERE kb_id = ?"
)


def _wiki_current_revision_ids(store, kb_id: str) -> set[int]:
    """Current revision ids of one knowledge base's wiki pages.

    Prefers one lightweight query over the store's own SQLite connection (guarded: read-only,
    under its lock). A store without that internal (a test double, a future implementation)
    falls back to ``list_pages`` — still **one** pass, never a per-page ``get_revisions`` scan:
    wiki indexing only ever writes the current revision, so historical revision ids cannot
    match any indexed point and never need to be listed.
    """
    rows = _query_wiki_revision_rows(store, kb_id)
    if rows is not None:
        return {int(row[0]) for row in rows if row and row[0]}
    try:
        pages = store.list_pages(kb_id=kb_id, limit=10000)
    except Exception:
        return set()
    return {int(page.current_revision_id) for page in pages if page.current_revision_id}


def _query_wiki_revision_rows(store, kb_id: str):
    """One ``wiki_pages`` query over the store connection, or ``None`` when unavailable."""
    connection = getattr(store, "_conn", None)
    if connection is None:
        return None
    lock = getattr(store, "_lock", None)
    try:
        if lock is None:
            return connection.execute(_WIKI_CURRENT_REVISION_SQL, (kb_id,)).fetchall()
        with lock:
            return connection.execute(_WIKI_CURRENT_REVISION_SQL, (kb_id,)).fetchall()
    except Exception:
        return None


def _visible_wiki_revision_ids(user_db, user, kb_id, wiki_store=None) -> set[int]:
    if not _can_user_view_kb(user_db, user, kb_id):
        return set()
    store = _get_wiki_store_instance(wiki_store)
    if store is None:
        return set()
    return _wiki_current_revision_ids(store, kb_id)


def _visible_version_scopes(
    user_db, user, kb_id, wiki_store=None
) -> tuple[VersionScope, VersionScope]:
    """(current scope, historical scope) for one request, computed in a single pass."""
    if user is None or user_db is None:
        empty = VersionScope(documents=set(), wiki=set())
        return empty, empty
    if user.get("is_admin"):
        unrestricted = VersionScope(documents=None, wiki=None)
        return unrestricted, unrestricted
    current_documents, historical_documents = _visible_document_version_ids(user_db, user, kb_id)
    current_wiki = _visible_wiki_revision_ids(user_db, user, kb_id, wiki_store)
    return (
        VersionScope(documents=current_documents, wiki=current_wiki),
        VersionScope(documents=historical_documents, wiki=set()),
    )


def _visible_current_version_ids(user_db, user, kb_id, wiki_store=None) -> set[int] | None:
    """Merged current-era version ids (compatibility view of :func:`_visible_version_scopes`)."""
    current, _historical = _visible_version_scopes(user_db, user, kb_id, wiki_store=wiki_store)
    return current.union()


def _visible_historical_version_ids(user_db, user, kb_id, wiki_store=None) -> set[int] | None:
    """Merged historical version ids (compatibility view of :func:`_visible_version_scopes`)."""
    _current, historical = _visible_version_scopes(user_db, user, kb_id, wiki_store=wiki_store)
    return historical.union()


def retrieve_current_then_history(
    retriever,
    query: str,
    top_k: int,
    user,
    user_db,
    kb_id: str,
    doc_name: str = None,
    tags: list[str] = None,
    weights=None,
    wiki_store=None,
) -> list:
    """Return current valid results first; backfill history only when needed.

    Historical fallback is used only when ACL-filtered current results are
    fewer than ``min(top_k, 3)`` and receives ``historical_result_weight``.
    Both scopes travel separately so wiki revision ids can never match a
    document version (see module docstring).
    """
    def retrieve(scope: VersionScope, **kwargs):
        if weights is not None:
            kwargs["weights"] = weights
        return retriever.retrieve(
            query,
            allowed_version_ids=scope.documents,
            allowed_wiki_revision_ids=scope.wiki,
            **kwargs,
        )

    current_scope, historical_scope = _visible_version_scopes(
        user_db, user, kb_id, wiki_store=wiki_store
    )
    if current_scope.is_empty() and historical_scope.is_empty():
        return []

    current = []
    if not current_scope.is_empty():
        current = retrieve(
            current_scope,
            top_k=top_k,
            doc_name=doc_name,
            tags=tags,
            validity_statuses={"valid"},
        )
    if len(current) >= min(top_k, 3):
        return current

    if not historical_scope.is_empty():
        fallback = retrieve(
            historical_scope,
            top_k=top_k,
            doc_name=doc_name,
            tags=tags,
            validity_statuses=set(),
        )
        rebuilt = [
            replace(c, historical_weight=settings.historical_result_weight) for c in fallback
        ]
        merged = list(current)
        for chunk in rebuilt:
            if len(merged) >= top_k:
                break
            merged.append(chunk)
        return merged
    return current


__all__ = [
    "VersionScope",
    "retrieve_current_then_history",
]
