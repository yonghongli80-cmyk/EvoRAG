"""Organization audit writes used by material mutations."""

from __future__ import annotations

from rag.org_auth.audit import OrgAuditService
from rag.org_auth.repository import OrgRepository, is_org_policy


def record_org_material_audit(db, actor: dict, action: str, kb_id: str) -> None:
    """Insert a successful material audit row into the caller's transaction.

    The caller owns transaction begin/commit so the audit cannot outlive or
    independently commit the material state it describes.
    """
    if not db._conn.in_transaction:
        raise RuntimeError("material audit requires an active write transaction")
    if not is_org_policy(OrgRepository(db).get_kb_policy(kb_id)):
        return
    OrgAuditService(db).record(int(actor["id"]), action, "knowledge_base", kb_id, "success", "authorized")
