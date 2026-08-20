from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrgRole(StrEnum):
    NONE = "none"
    READ = "read"
    MANAGE = "manage"
    ADMINISTER = "administer"


class KnowledgeBaseType(StrEnum):
    COMPANY_PUBLIC = "company_public"
    ORGANIZATION = "organization"
    SENSITIVE = "sensitive"
    UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    role: OrgRole
    reason: str


@dataclass(frozen=True)
class PersonProfile:
    person_code: str
    user_id: int
    username: str
    display_name: str
    job_title: str
    primary_org: dict | None
    secondary_orgs: tuple[dict, ...]
    active: bool
    security_version: int
