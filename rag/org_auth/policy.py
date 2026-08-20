from __future__ import annotations

from dataclasses import dataclass

from rag.org_auth.models import AuthorizationDecision, KnowledgeBaseType, OrgRole


@dataclass(frozen=True)
class AuthorizationContext:
    active_user: bool
    company_admin: bool
    active_unit: bool
    active_kb: bool
    kb_type: KnowledgeBaseType
    sensitivity: str | None
    qualified: bool
    member: bool
    manager: bool
    allow_role: OrgRole
    denied: bool


_RANK = {OrgRole.NONE: 0, OrgRole.READ: 1, OrgRole.MANAGE: 2, OrgRole.ADMINISTER: 3}


def resolve_role(c: AuthorizationContext) -> AuthorizationDecision:
    if not c.active_user:
        return AuthorizationDecision(False, OrgRole.NONE, "account_inactive")
    if c.company_admin:
        return AuthorizationDecision(True, OrgRole.ADMINISTER, "company_admin")
    if not c.active_kb or not c.active_unit:
        return AuthorizationDecision(False, OrgRole.NONE, "resource_inactive")
    if c.kb_type is KnowledgeBaseType.UNASSIGNED:
        return AuthorizationDecision(False, OrgRole.NONE, "unassigned")
    if c.denied:
        return AuthorizationDecision(False, OrgRole.NONE, "explicit_deny")
    scoped = c.member or c.manager or c.allow_role is not OrgRole.NONE
    if c.kb_type is KnowledgeBaseType.SENSITIVE and (not scoped or not c.qualified):
        return AuthorizationDecision(False, OrgRole.NONE, "sensitivity_required")
    candidates = [c.allow_role]
    if c.manager:
        candidates.append(OrgRole.MANAGE)
    elif c.member or c.kb_type is KnowledgeBaseType.COMPANY_PUBLIC:
        candidates.append(OrgRole.READ)
    role = max(candidates, key=_RANK.get)
    return AuthorizationDecision(role is not OrgRole.NONE, role, "allowed" if role is not OrgRole.NONE else "outside_org_scope")


def role_allows(actual: OrgRole, required: OrgRole) -> bool:
    return _RANK[actual] >= _RANK[required]
