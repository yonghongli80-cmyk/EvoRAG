"""Current-first retrieval with ACL-filtered historical fallback.

SQLite ``document_permissions.current_version_id`` is the authoritative
current-version source. ``is_current`` in Qdrant is only repairable metadata
and is never the sole visibility condition.

Administrators bypass visibility filters here exactly as they do at the chunk
level (``filter_chunks_by_permission``) and in the tags endpoint: the helpers
below return ``None`` for an admin, which the caller translates into "no
version filter" instead of an empty candidate set.
"""

from __future__ import annotations

from dataclasses import replace

from config import settings


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


def _visible_current_version_ids(user_db, user, kb_id, wiki_store=None) -> set[int] | None:
    if user is None or user_db is None:
        return set()
    if user.get("is_admin"):
        return None
    ids: set[int] = set()
    if not hasattr(user_db, "get_document_permission_by_id"):
        for name in user_db.get_accessible_doc_names(kb_id, user["id"], user.get("permission_level", 1)):
            perm = user_db.get_document_permission(name, kb_id)
            if perm and perm.get("current_version_id"):
                ids.add(perm["current_version_id"])
    else:
        document_ids = _visible_document_ids(user_db, user, kb_id)
        if document_ids is None:
            return None
        for document_id in document_ids:
            perm = user_db.get_document_permission_by_id(document_id)
            if perm and perm.get("current_version_id"):
                ids.add(perm["current_version_id"])

    if _can_user_view_kb(user_db, user, kb_id):
        w_store = _get_wiki_store_instance(wiki_store)
        if w_store is not None:
            try:
                for page in w_store.list_pages(kb_id=kb_id, limit=10000):
                    if page.current_revision_id:
                        ids.add(page.current_revision_id)
            except Exception:
                pass

    return ids


def _visible_historical_version_ids(user_db, user, kb_id, wiki_store=None) -> set[int] | None:
    """Visible historical version IDs plus confirmed-outdated current versions."""
    if user is None or user_db is None:
        return set()
    if user.get("is_admin"):
        return None
    ids: set[int] = set()
    if not hasattr(user_db, "get_document_permission_by_id"):
        permissions = [user_db.get_document_permission(name, kb_id) for name in user_db.get_accessible_doc_names(kb_id, user["id"], user.get("permission_level", 1))]
    else:
        document_ids = _visible_document_ids(user_db, user, kb_id)
        if document_ids is None:
            return None
        permissions = [user_db.get_document_permission_by_id(document_id) for document_id in document_ids]
    for perm in permissions:
        if not perm:
            continue
        for version in user_db.list_document_versions(perm["id"]):
            if (
                version["status"] != "current"
                or version["validity_status"] == "confirmed_outdated"
            ):
                ids.add(version["id"])

    if _can_user_view_kb(user_db, user, kb_id):
        w_store = _get_wiki_store_instance(wiki_store)
        if w_store is not None:
            try:
                for page in w_store.list_pages(kb_id=kb_id, limit=10000):
                    for rev in w_store.get_revisions(page.id):
                        if rev.id != page.current_revision_id:
                            ids.add(rev.id)
            except Exception:
                pass

    return ids


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
    An administrator passes ``None`` version filters, mirroring the chunk-level
    permission bypass.
    """
    def retrieve(**kwargs):
        if weights is not None:
            kwargs["weights"] = weights
        return retriever.retrieve(query, **kwargs)

    current_ids = _visible_current_version_ids(user_db, user, kb_id, wiki_store=wiki_store)
    historical_ids = _visible_historical_version_ids(user_db, user, kb_id, wiki_store=wiki_store)
    if current_ids is not None and historical_ids is not None and not current_ids and not historical_ids:
        return []

    current = []
    if current_ids is None or current_ids:
        current = retrieve(
            top_k=top_k,
            doc_name=doc_name,
            tags=tags,
            allowed_version_ids=current_ids,
            validity_statuses={"valid"},
        )
    if len(current) >= min(top_k, 3):
        return current

    if historical_ids is None or historical_ids:
        fallback = retrieve(
            top_k=top_k,
            doc_name=doc_name,
            tags=tags,
            allowed_version_ids=historical_ids,
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
