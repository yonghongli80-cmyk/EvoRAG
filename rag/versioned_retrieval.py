"""Current-first retrieval with ACL-filtered historical fallback.

SQLite ``document_permissions.current_version_id`` is the authoritative
current-version source. ``is_current`` in Qdrant is only repairable metadata
and is never the sole visibility condition.
"""

from __future__ import annotations

from dataclasses import replace

from config import settings


def _visible_document_ids(user_db, user, kb_id) -> set[int]:
    if user is None or user_db is None:
        return set()
    from rag.org_auth.repository import OrgRepository
    if hasattr(user_db, "locked_read") and OrgRepository(user_db).get_kb_policy(kb_id) is not None:
        from rag.org_auth.service import OrgAuthorizationService
        return set(OrgAuthorizationService(user_db).allowed_document_ids(int(user["id"]), kb_id))
    names = user_db.get_accessible_doc_names(kb_id, user["id"], user.get("permission_level", 1))
    return {
        permission["id"] for name in names
        if (permission := user_db.get_document_permission(name, kb_id)) is not None
    }


def _visible_current_version_ids(user_db, user, kb_id) -> set[int]:
    if user is None or user_db is None:
        return set()
    ids: set[int] = set()
    if not hasattr(user_db, "get_document_permission_by_id"):
        for name in user_db.get_accessible_doc_names(kb_id, user["id"], user.get("permission_level", 1)):
            perm = user_db.get_document_permission(name, kb_id)
            if perm and perm.get("current_version_id"):
                ids.add(perm["current_version_id"])
        return ids
    for document_id in _visible_document_ids(user_db, user, kb_id):
        perm = user_db.get_document_permission_by_id(document_id)
        if perm and perm.get("current_version_id"):
            ids.add(perm["current_version_id"])
    return ids


def _visible_historical_version_ids(user_db, user, kb_id) -> set[int]:
    """Visible historical version IDs plus confirmed-outdated current versions."""
    if user is None or user_db is None:
        return set()
    ids: set[int] = set()
    if not hasattr(user_db, "get_document_permission_by_id"):
        permissions = [user_db.get_document_permission(name, kb_id) for name in user_db.get_accessible_doc_names(kb_id, user["id"], user.get("permission_level", 1))]
    else:
        permissions = [user_db.get_document_permission_by_id(document_id) for document_id in _visible_document_ids(user_db, user, kb_id)]
    for perm in permissions:
        if not perm:
            continue
        for version in user_db.list_document_versions(perm["id"]):
            if (
                version["status"] != "current"
                or version["validity_status"] == "confirmed_outdated"
            ):
                ids.add(version["id"])
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
) -> list:
    """Return current valid results first; backfill history only when needed.

    Historical fallback is used only when ACL-filtered current results are
    fewer than ``min(top_k, 3)`` and receives ``historical_result_weight``.
    """
    def retrieve(**kwargs):
        if weights is not None:
            kwargs["weights"] = weights
        return retriever.retrieve(query, **kwargs)

    current_ids = _visible_current_version_ids(user_db, user, kb_id)
    historical_ids = _visible_historical_version_ids(user_db, user, kb_id)
    if not current_ids and not historical_ids:
        return []

    current = []
    if current_ids:
        current = retrieve(
            top_k=top_k,
            doc_name=doc_name,
            tags=tags,
            allowed_version_ids=current_ids,
            validity_statuses={"valid"},
        )
    if len(current) >= min(top_k, 3):
        return current

    if not historical_ids:
        return current
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
