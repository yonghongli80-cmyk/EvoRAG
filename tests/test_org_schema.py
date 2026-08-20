import sqlite3

import pytest

from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB


def test_org_schema_is_idempotent_and_enforces_stable_keys(tmp_path):
    db = UserDB(str(tmp_path / "org.db"))
    try:
        repo = OrgRepository(db)
        repo.install_schema()
        repo.install_schema()
        assert repo.table_names() >= {
            "org_units", "org_people", "org_memberships", "org_manager_scopes",
            "org_sensitive_qualifications", "kb_org_policies", "kb_org_grants",
            "org_audit_events", "org_seed_state",
        }
        repo.create_unit("ROOT", None, "星河协作集团", "company")
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_unit("ROOT", None, "重复", "company")
    finally:
        db.close()


def test_org_compound_write_rolls_back(tmp_path):
    db = UserDB(str(tmp_path / "org.db"))
    try:
        repo = OrgRepository(db)
        repo.install_schema()
        with pytest.raises(sqlite3.IntegrityError):
            repo.create_units((("A", None, "甲", "company"), ("A", None, "乙", "company")))
        assert repo.get_unit("A") is None
    finally:
        db.close()
