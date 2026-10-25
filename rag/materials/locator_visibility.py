"""Request-scoped SQL projection of documents visible to the material locator."""

from __future__ import annotations

from collections.abc import Iterator

from rag.org_auth.models import OrgRole
from rag.org_auth.repository import OrgRepository, is_org_policy
from rag.org_auth.service import OrgAuthorizationService
from rag.user_db import UserDB


def iter_visible_material_documents(db: UserDB, user: dict) -> Iterator[tuple[int, dict]]:
    """Yield visible live/current rows in the legacy KB-then-document order."""
    kb_ids = db.material_repo.list_material_document_kb_ids()
    org_repository = OrgRepository(db)
    org_authorization = OrgAuthorizationService(db)
    user_id = user.get("id")
    admin = bool(user.get("is_admin"))
    full_kbs: list[str] = []
    legacy_kbs: list[str] = []

    for kb_id in kb_ids:
        policy = org_repository.get_kb_policy(kb_id)
        if is_org_policy(policy):
            if user_id is not None and org_authorization.authorize(int(user_id), kb_id, OrgRole.READ).allowed:
                full_kbs.append(kb_id)
        elif admin:
            full_kbs.append(kb_id)
        else:
            legacy_kbs.append(kb_id)

    if len(full_kbs) == len(kb_ids):
        if full_kbs:
            documents_by_kb: dict[str, list[dict]] = {kb_id: [] for kb_id in full_kbs}
            for row in db.material_repo.list_material_documents_with_current_version():
                kb_id = row["kb_id"]
                if kb_id in documents_by_kb:
                    documents_by_kb[kb_id].append(row)
            for kb_id in full_kbs:
                for row in documents_by_kb[kb_id]:
                    yield int(row["id"]), row
        return

    rows_by_kb = {kb_id: [] for kb_id in kb_ids}
    for kb_id in full_kbs:
        rows_by_kb[kb_id] = db.material_repo.list_material_documents_for_kb(kb_id, apply_legacy_acl=False)
    for kb_id in legacy_kbs:
        rows_by_kb[kb_id] = db.material_repo.list_material_documents_for_kb(
            kb_id,
            apply_legacy_acl=True,
            user_id=int(user_id) if user_id is not None else None,
        )
    for kb_id in kb_ids:
        for row in rows_by_kb[kb_id]:
            yield int(row["id"]), row
