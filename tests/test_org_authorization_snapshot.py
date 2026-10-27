import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rag.org_auth.models import OrgRole
from rag.org_auth.service import OrgAuthorizationService
from rag.user_db import UserDB


@pytest.fixture
def database(tmp_path):
    db = UserDB(tmp_path / "snapshot.sqlite")
    with db.locked_transaction() as c:
        c.execute(
            "INSERT INTO users(id,username,salt,password,is_admin) VALUES (1,'member','s','p',0),(2,'admin','s','p',1)"
        )
        c.execute("INSERT INTO org_units(code,name,kind) VALUES ('org','Old org','department')")
        c.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES ('person',1,'Person','Employee')"
        )
        c.execute("INSERT INTO org_memberships(person_code,org_code) VALUES ('person','org')")
        c.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type,sensitivity) VALUES ('sensitive','org','sensitive','finance'),('regular','org','organization',NULL),('unassigned',NULL,'unassigned',NULL)"
        )
        c.execute(
            "INSERT INTO kb_metadata(kb_id,name) VALUES ('sensitive','Sensitive'),('regular','Old name'),('unassigned','Unassigned')"
        )
        c.execute(
            "INSERT INTO document_permissions(id,doc_name,kb_id,owner_id) VALUES (10,'old','regular',1),(12,'sensitive document','sensitive',1)"
        )
    try:
        yield db
    finally:
        db.close()


def commit_at(db, needle, sql):
    reader = db.cm.get_read_connection()
    fired = []

    def trace(statement):
        if needle.lower() in statement.lower() and not fired:
            fired.append(True)
            with sqlite3.connect(db.db_path, timeout=2) as writer:
                writer.executescript(sql)

    reader.set_trace_callback(trace)
    return reader, fired


@pytest.mark.parametrize(
    "method", ["decision", "authorize", "allowed_kb_ids", "allowed_document_ids", "visible_knowledge_bases"]
)
def test_sensitive_revocation_never_combines_two_denied_states(database, method):
    db = database
    service = OrgAuthorizationService(db)
    reader, fired = commit_at(
        db,
        "FROM org_sensitive_qualifications",
        """
        BEGIN; DELETE FROM org_memberships WHERE person_code='person';
        INSERT INTO org_sensitive_qualifications VALUES ('person','finance'); COMMIT;
    """,
    )
    try:
        if method in ("decision", "authorize"):
            assert not getattr(service, method)(1, "sensitive").allowed
        elif method == "allowed_kb_ids":
            assert "sensitive" not in service.allowed_kb_ids(1)
        elif method == "allowed_document_ids":
            assert service.allowed_document_ids(1, "sensitive") == frozenset()
        else:
            assert "sensitive" not in {row["kb_id"] for row in service.visible_knowledge_bases(1)}
        assert fired
    finally:
        reader.set_trace_callback(None)


def test_visible_metadata_and_role_share_snapshot(database):
    db = database
    service = OrgAuthorizationService(db)
    reader, fired = commit_at(
        db,
        "SELECT p.kb_id,m.name",
        """
        BEGIN; UPDATE kb_metadata SET name='New name' WHERE kb_id='regular';
        DELETE FROM org_memberships WHERE person_code='person'; COMMIT;
    """,
    )
    try:
        result = service.visible_knowledge_bases(1)
        regular = next(row for row in result if row["kb_id"] == "regular")
        assert (regular["name"], regular["effective_role"]) == ("Old name", "read")
        assert fired
    finally:
        reader.set_trace_callback(None)


def test_document_ids_share_authorization_snapshot(database):
    db = database
    reader, fired = commit_at(
        db,
        "SELECT id FROM document_permissions",
        """
        BEGIN; DELETE FROM org_memberships WHERE person_code='person';
        DELETE FROM document_permissions WHERE id=10;
        INSERT INTO document_permissions(id,doc_name,kb_id,owner_id) VALUES (11,'new','regular',1); COMMIT;
    """,
    )
    try:
        assert OrgAuthorizationService(db).allowed_document_ids(1, "regular") == frozenset({10})
        assert fired
    finally:
        reader.set_trace_callback(None)


def test_policy_contract_and_all_public_methods(database):
    db = database
    service = OrgAuthorizationService(db)
    with db.locked_transaction() as c:
        c.execute(
            "INSERT INTO kb_org_grants(kb_id,subject_type,subject_id,role,effect,granted_by) VALUES ('regular','user','1','read','deny',2)"
        )
    assert not service.decision(1, "regular").allowed
    assert not service.authorize(1, "regular").allowed
    assert not service.can_manage_org(1, "org")
    assert service.can_manage_org(2, "org")
    assert service.allowed_kb_ids(1) == frozenset()
    assert service.allowed_document_ids(1, "regular") == frozenset()
    assert service.visible_knowledge_bases(1) == []
    assert service.allowed_kb_ids(2) == frozenset({"regular", "sensitive", "unassigned"})
    assert service.decision(2, "unassigned").role is OrgRole.ADMINISTER


def test_outer_reader_transaction_is_borrowed(database):
    db = database
    reader = db.cm.get_read_connection()
    reader.execute("BEGIN")
    try:
        assert OrgAuthorizationService(db).decision(1, "regular").allowed
        assert reader.in_transaction
    finally:
        reader.rollback()


def test_exception_releases_snapshot_and_next_call_sees_commit(database):
    db = database
    service = OrgAuthorizationService(db)
    reader = db.cm.get_read_connection()

    def authorizer(action, arg1, arg2, name, source):
        if action == sqlite3.SQLITE_READ and arg1 == "org_sensitive_qualifications":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    reader.set_authorizer(authorizer)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            service.decision(1, "sensitive")
    finally:
        reader.set_authorizer(None)
    assert not reader.in_transaction
    with db.locked_transaction() as c:
        c.execute("INSERT INTO org_sensitive_qualifications VALUES ('person','finance')")
    assert service.decision(1, "sensitive").allowed


def test_shared_service_thread_local_snapshots_and_writer_commit(database):
    db = database
    service = OrgAuthorizationService(db)
    entered = threading.Event()
    release = threading.Event()

    def first():
        reader = db.cm.get_read_connection()

        def trace(statement):
            if "FROM org_sensitive_qualifications" in statement:
                entered.set()
                assert release.wait(5)

        reader.set_trace_callback(trace)
        try:
            return service.decision(1, "sensitive")
        finally:
            reader.set_trace_callback(None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(first)
        try:
            assert entered.wait(5)
            with sqlite3.connect(db.db_path, timeout=2) as writer:
                writer.execute("INSERT INTO org_sensitive_qualifications VALUES ('person','finance')")
            assert pool.submit(service.decision, 1, "sensitive").result(timeout=5).allowed
        finally:
            release.set()
        assert not future.result(timeout=5).allowed
