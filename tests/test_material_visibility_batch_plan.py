from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from rag.materials.locator_visibility import iter_visible_material_documents
from rag.user_db import UserDB


def _seed_current(db, name, kb_id, owner_id, *, scope="private"):
    document_id = db.create_document_permission(name, kb_id, owner_id, scope=scope)
    version_id = db.create_document_version(
        document_id, name, f"/synthetic/{kb_id}/{name}", f"hash-{document_id}", owner_id
    )
    db.activate_document_version(document_id, version_id, kb_id, name)
    return document_id


@pytest.fixture
def interleaved_kb_db(tmp_path):
    db = UserDB(str(tmp_path / "interleaved-kbs.db"))
    db.create_material_schema()
    owner_id = db.create_user("batch-plan-owner", "Password-123!")
    other_id = db.create_user("batch-plan-other", "Password-123!")
    expected_ids = []
    hidden_ids = []
    name_source_ids = []
    for index in range(12):
        kb_id = f"kb-{index:02d}"
        if index == 0:
            expected_ids.append(_seed_current(db, "shared-name.txt", kb_id, other_id, scope="public"))
        elif index == 1:
            hidden_ids.append(_seed_current(db, "shared-name.txt", kb_id, other_id))
            expected_ids.append(_seed_current(db, "visible-01.txt", kb_id, owner_id))
        else:
            expected_ids.append(_seed_current(db, f"visible-{index:02d}.txt", kb_id, owner_id))
        hidden_ids.append(_seed_current(db, f"private-{index:02d}.txt", kb_id, other_id))
    expected_ids.insert(1, _seed_current(db, "late-kb00.txt", "kb-00", owner_id))
    expected_ids.insert(6, _seed_current(db, "late-kb04.txt", "kb-04", owner_id))
    name_source_ids.append(db.create_document_permission("versionless-source.txt", "kb-00", other_id, scope="public"))
    deleted_source = _seed_current(db, "deleted-source.txt", "kb-00", other_id, scope="public")
    with db.locked_transaction() as conn:
        conn.execute("UPDATE document_permissions SET deleted_at=1 WHERE id=?", (deleted_source,))
    name_source_ids.append(deleted_source)
    yield db, owner_id, expected_ids, hidden_ids, name_source_ids
    db.close()


def test_many_legacy_kbs_use_one_authorized_wide_query_with_old_order_and_rows(interleaved_kb_db, monkeypatch):
    db, owner_id, expected_ids, hidden_ids, name_source_ids = interleaved_kb_db
    sql = []
    fetched_document_rows = []
    original_read = db.material_repo.cm.read_connection

    class CursorCounter:
        def __init__(self, cursor, statement):
            self.cursor = cursor
            self.statement = statement

        def fetchall(self):
            rows = self.cursor.fetchall()
            upper = self.statement.upper()
            if "SELECT DP.ID, DP.DOC_NAME" in upper and "DOCUMENT_PERMISSIONS DP" in upper:
                fetched_document_rows.extend(dict(row)["id"] for row in rows)
            return rows

        def fetchone(self):
            return self.cursor.fetchone()

    class ConnectionCounter:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            sql.append(statement)
            return CursorCounter(self.connection.execute(statement, parameters), statement)

        def getlimit(self, category):
            return self.connection.getlimit(category)

    @contextmanager
    def counted_read():
        with original_read() as connection:
            yield ConnectionCounter(connection)

    monkeypatch.setattr(db.material_repo.cm, "read_connection", counted_read)
    actual = list(iter_visible_material_documents(db, db.get_user_by_id(owner_id)))

    wide_queries = [
        statement
        for statement in sql
        if "SELECT DP.ID, DP.DOC_NAME" in statement.upper() and "DOCUMENT_PERMISSIONS DP" in statement.upper()
    ]
    assert len(wide_queries) == 1
    assert [doc_id for doc_id, _ in actual] == expected_ids
    assert {doc_id for doc_id, _ in actual}.isdisjoint(hidden_ids)
    assert {doc_id for doc_id, _ in actual}.isdisjoint(name_source_ids)
    assert fetched_document_rows == expected_ids
    legacy_names = set(db.get_accessible_doc_names("kb-00", owner_id))
    assert {"versionless-source.txt", "deleted-source.txt"} <= legacy_names

    all_current = {int(row["id"]): row for row in db.material_repo.list_material_documents_with_current_version()}
    assert actual == [(doc_id, all_current[doc_id]) for doc_id in expected_ids]


def test_batch_acl_pairs_names_with_kb_and_none_user_only_sees_public(interleaved_kb_db):
    db, _, _, _, _ = interleaved_kb_db
    public_in_kb0 = db.material_repo.list_material_documents_for_access_plan(
        [("kb-00", True), ("kb-01", True)], user_id=None
    )
    assert [(row["kb_id"], row["doc_name"]) for row in public_in_kb0] == [("kb-00", "shared-name.txt")]

    owner_id = db.create_user("batch-plan-case", "Password-123!")
    upper_id = _seed_current(db, "Name.txt", "kb-00", db.get_user_by_id(owner_id)["id"], scope="public")
    _seed_current(db, "name.txt", "kb-00", db.get_user_by_id(owner_id)["id"])
    rows = db.material_repo.list_material_documents_for_access_plan([("kb-00", True)], user_id=None)
    assert upper_id in {int(row["id"]) for row in rows}
    assert "name.txt" not in {row["doc_name"] for row in rows}


def test_empty_access_plan_returns_without_opening_a_read_connection(interleaved_kb_db, monkeypatch):
    db, _, _, _, _ = interleaved_kb_db

    def forbidden_connection():
        raise AssertionError("empty plan must not execute SQL")

    monkeypatch.setattr(db.material_repo.cm, "read_connection", forbidden_connection)
    assert db.material_repo.list_material_documents_for_access_plan([], user_id=None) == []


def test_low_sqlite_bind_limit_uses_contiguous_chunks_and_preserves_order(interleaved_kb_db):
    db, owner_id, expected_ids, _, _ = interleaved_kb_db
    repo = db.material_repo
    connection = repo.cm.get_read_connection()
    category = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(category)
    connection.setlimit(category, 11)
    sql = []
    connection.set_trace_callback(sql.append)
    try:
        plan = [(f"kb-{index:02d}", True) for index in range(12)]
        rows = repo.list_material_documents_for_access_plan(plan, user_id=owner_id)
    finally:
        connection.set_trace_callback(None)
        connection.setlimit(category, original_limit)

    projection_queries = [
        statement
        for statement in sql
        if "SELECT DP.ID, DP.DOC_NAME" in statement.upper() and "DOCUMENT_PERMISSIONS DP" in statement.upper()
    ]
    assert len(projection_queries) == 4
    assert [int(row["id"]) for row in rows] == expected_ids


def test_variable_limit_below_one_plan_item_fails_before_document_query(interleaved_kb_db):
    db, owner_id, _, _, _ = interleaved_kb_db
    repo = db.material_repo
    connection = repo.cm.get_read_connection()
    category = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(category)
    connection.setlimit(category, 4)
    sql = []
    connection.set_trace_callback(sql.append)
    try:
        with pytest.raises(ValueError, match="variable limit is too low"):
            repo.list_material_documents_for_access_plan([("kb-00", True)], user_id=owner_id)
    finally:
        connection.set_trace_callback(None)
        connection.setlimit(category, original_limit)
    assert sql == []


def test_access_plan_hard_cap_splits_301_kbs_into_two_queries(interleaved_kb_db):
    db, owner_id, _, _, _ = interleaved_kb_db
    connection = db.material_repo.cm.get_read_connection()
    sql = []
    connection.set_trace_callback(sql.append)
    try:
        plan = [(f"no-doc-{index:03d}", True) for index in range(301)]
        assert db.material_repo.list_material_documents_for_access_plan(plan, user_id=owner_id) == []
    finally:
        connection.set_trace_callback(None)
    projection_queries = [
        statement
        for statement in sql
        if "SELECT DP.ID, DP.DOC_NAME" in statement.upper() and "DOCUMENT_PERMISSIONS DP" in statement.upper()
    ]
    assert len(projection_queries) == 2


def test_mixed_org_and_legacy_plan_omits_denied_org_rows(interleaved_kb_db, monkeypatch):
    db, owner_id, expected_ids, hidden_ids, _ = interleaved_kb_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES('ORG-ALLOW',NULL,'Allow','team')")
        conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES('ORG-DENY',NULL,'Deny','team')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('org-allowed','ORG-ALLOW','organization')"
        )
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('org-denied','ORG-DENY','organization')"
        )
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('BATCH-OWNER',?,'Owner','Engineer')",
            (owner_id,),
        )
        conn.execute("INSERT INTO org_memberships VALUES('BATCH-OWNER','ORG-ALLOW',1)")
    allowed_org_id = _seed_current(db, "org-allowed.txt", "org-allowed", owner_id)
    denied_org_id = _seed_current(db, "org-denied.txt", "org-denied", owner_id)

    sql = []
    fetched_document_rows = []
    original_read = db.material_repo.cm.read_connection

    class CursorCounter:
        def __init__(self, cursor, statement):
            self.cursor = cursor
            self.statement = statement

        def fetchall(self):
            rows = self.cursor.fetchall()
            upper = self.statement.upper()
            if "SELECT DP.ID, DP.DOC_NAME" in upper and "DOCUMENT_PERMISSIONS DP" in upper:
                fetched_document_rows.extend(int(row["id"]) for row in rows)
            return rows

        def fetchone(self):
            return self.cursor.fetchone()

    class ConnectionCounter:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            sql.append(statement)
            return CursorCounter(self.connection.execute(statement, parameters), statement)

        def getlimit(self, category):
            return self.connection.getlimit(category)

    @contextmanager
    def counted_read():
        with original_read() as connection:
            yield ConnectionCounter(connection)

    monkeypatch.setattr(db.material_repo.cm, "read_connection", counted_read)
    user = db.get_user_by_id(owner_id)
    actual = list(iter_visible_material_documents(db, user))

    projection_queries = [
        statement
        for statement in sql
        if "SELECT DP.ID, DP.DOC_NAME" in statement.upper() and "DOCUMENT_PERMISSIONS DP" in statement.upper()
    ]
    assert len(projection_queries) == 1
    actual_ids = [doc_id for doc_id, _ in actual]
    assert actual_ids == [*expected_ids, allowed_org_id]
    assert denied_org_id not in actual_ids
    assert set(fetched_document_rows) == set(actual_ids)
    assert not set(fetched_document_rows) & set(hidden_ids)
    assert "org-denied" not in " ".join(projection_queries)


def test_admin_all_full_path_keeps_existing_bulk_reader(interleaved_kb_db, monkeypatch):
    db, _, expected_ids, hidden_ids, _ = interleaved_kb_db
    admin_id = db.create_user("batch-plan-admin", "Password-123!")
    db.set_user_admin(admin_id, True)
    repo = db.material_repo

    def forbidden_access_plan(*args, **kwargs):
        raise AssertionError("all-full admin path should keep the existing bulk reader")

    monkeypatch.setattr(repo, "list_material_documents_for_access_plan", forbidden_access_plan)
    visible = list(iter_visible_material_documents(db, db.get_user_by_id(admin_id)))
    bulk_rows = repo.list_material_documents_with_current_version()
    kb_ids = repo.list_material_document_kb_ids()
    assert [doc_id for doc_id, _ in visible] == [
        int(row["id"]) for kb_id in kb_ids for row in bulk_rows if row["kb_id"] == kb_id
    ]
    assert set(expected_ids) <= {doc_id for doc_id, _ in visible}
    assert set(hidden_ids) <= {doc_id for doc_id, _ in visible}
