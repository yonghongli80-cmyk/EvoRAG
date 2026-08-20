from __future__ import annotations

from rag.org_auth.models import AuthorizationDecision, KnowledgeBaseType, OrgRole
from rag.org_auth.policy import AuthorizationContext, resolve_role, role_allows
from rag.org_auth.repository import OrgRepository


class OrgAuthorizationService:
    def __init__(self, db):
        self.db = db
        self.repo = OrgRepository(db)

    def decision(self, user_id: int, kb_id: str) -> AuthorizationDecision:
        user = self.db.get_user_by_id(user_id)
        person = self.repo.get_person_for_user(user_id)
        policy = self.repo.get_kb_policy(kb_id)
        if user is None or policy is None:
            return AuthorizationDecision(False, OrgRole.NONE, "not_found")
        if person is None:
            return resolve_role(AuthorizationContext(True, bool(user.get("is_admin")), True, bool(policy["active"]), KnowledgeBaseType(policy["kb_type"]), policy["sensitivity"], False, False, False, OrgRole.NONE, False))
        memberships = {m["org_code"] for m in self.repo.memberships(person["person_code"]) if m["active"]}
        target = policy["org_code"]
        manager = any(target in self.repo.descendants(scope) for scope in self.repo.manager_scopes(person["person_code"])) if target else False
        grants = self.repo.grants(kb_id)
        matches = [g for g in grants if (g["subject_type"] == "user" and g["subject_id"] == str(user_id)) or (g["subject_type"] == "org" and g["subject_id"] in memberships)]
        denied = any(g["effect"] == "deny" for g in matches)
        allowed = [OrgRole(g["role"]) for g in matches if g["effect"] == "allow"]
        allow_role = OrgRole.MANAGE if OrgRole.MANAGE in allowed else (OrgRole.READ if allowed else OrgRole.NONE)
        unit = self.repo.get_unit(target) if target else None
        return resolve_role(AuthorizationContext(
            bool(person["active"]), bool(user.get("is_admin")), bool(unit and unit["active"]), bool(policy["active"]),
            KnowledgeBaseType(policy["kb_type"]), policy["sensitivity"],
            policy["sensitivity"] in self.repo.qualifications(person["person_code"]) if policy["sensitivity"] else True,
            target in memberships, manager, allow_role, denied,
        ))

    def can_manage_org(self, user_id: int, org_code: str) -> bool:
        user = self.db.get_user_by_id(user_id)
        if user and user.get("is_admin"):
            return self.repo.get_unit(org_code) is not None
        person = self.repo.get_person_for_user(user_id)
        if person is None or not person["active"]:
            return False
        return any(org_code in self.repo.descendants(scope) for scope in self.repo.manager_scopes(person["person_code"]))

    def authorize(self, user_id: int, kb_id: str, required: OrgRole = OrgRole.READ) -> AuthorizationDecision:
        decision = self.decision(user_id, kb_id)
        if role_allows(decision.role, required):
            return decision
        return AuthorizationDecision(False, decision.role, decision.reason)

    def allowed_kb_ids(self, user_id: int, required: OrgRole = OrgRole.READ) -> frozenset[str]:
        with self.db.locked_read() as conn:
            ids = [r[0] for r in conn.execute("SELECT kb_id FROM kb_org_policies")]
        return frozenset(kb_id for kb_id in ids if self.authorize(user_id, kb_id, required).allowed)

    def allowed_document_ids(self, user_id: int, kb_id: str) -> frozenset[int]:
        if not self.authorize(user_id, kb_id, OrgRole.READ).allowed:
            return frozenset()
        with self.db.locked_read() as conn:
            rows = conn.execute(
                "SELECT id FROM document_permissions WHERE kb_id=? AND deleted_at IS NULL",
                (kb_id,),
            ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def visible_knowledge_bases(self, user_id: int) -> list[dict]:
        allowed = self.allowed_kb_ids(user_id)
        if not allowed:
            return []
        with self.db.locked_read() as conn:
            rows = conn.execute("""
                SELECT p.kb_id,m.name,p.kb_type,p.sensitivity,p.org_code,u.name AS org_name,
                       (SELECT count(*) FROM kb_documents d WHERE d.kb_id=p.kb_id AND d.status!='deleting') AS doc_count
                FROM kb_org_policies p JOIN kb_metadata m ON m.kb_id=p.kb_id
                LEFT JOIN org_units u ON u.code=p.org_code ORDER BY m.name
            """).fetchall()
        result = []
        for row in rows:
            if row["kb_id"] not in allowed:
                continue
            role = self.decision(user_id, row["kb_id"]).role.value
            result.append({"kb_id": row["kb_id"], "name": row["name"], "doc_count": row["doc_count"],
                           "kb_type": row["kb_type"], "org_unit": {"code": row["org_code"], "name": row["org_name"]} if row["org_code"] else None,
                           "effective_role": role, "sensitivity": row["sensitivity"]})
        return result
