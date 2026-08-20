from rag.org_auth.models import KnowledgeBaseType, OrgRole
from rag.org_auth.policy import AuthorizationContext, resolve_role


def ctx(**changes):
    values = dict(active_user=True, company_admin=False, active_unit=True, active_kb=True,
                  kb_type=KnowledgeBaseType.ORGANIZATION, sensitivity=None,
                  qualified=False, member=False, manager=False, allow_role=OrgRole.NONE,
                  denied=False)
    values.update(changes)
    return AuthorizationContext(**values)


def test_policy_matrix_and_deny_precedence():
    assert resolve_role(ctx(member=True)).role is OrgRole.READ
    assert resolve_role(ctx(manager=True)).role is OrgRole.MANAGE
    assert resolve_role(ctx(company_admin=True)).role is OrgRole.ADMINISTER
    assert resolve_role(ctx(kb_type=KnowledgeBaseType.COMPANY_PUBLIC)).role is OrgRole.READ
    assert resolve_role(ctx(member=True, denied=True)).role is OrgRole.NONE
    assert resolve_role(ctx(kb_type=KnowledgeBaseType.UNASSIGNED)).role is OrgRole.NONE


def test_sensitive_requires_scope_and_qualification_together():
    sensitive = dict(kb_type=KnowledgeBaseType.SENSITIVE, sensitivity="finance")
    assert resolve_role(ctx(**sensitive, member=True, qualified=False)).role is OrgRole.NONE
    assert resolve_role(ctx(**sensitive, member=False, qualified=True)).role is OrgRole.NONE
    assert resolve_role(ctx(**sensitive, member=True, qualified=True)).role is OrgRole.READ
    assert resolve_role(ctx(**sensitive, manager=True, qualified=True)).role is OrgRole.MANAGE
