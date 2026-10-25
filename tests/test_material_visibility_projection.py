from __future__ import annotations

from contextlib import contextmanager

import pytest

from rag.materials.locator_visibility import iter_visible_material_documents
from rag.user_db import UserDB


@pytest.fixture
def projection_db(tmp_path):
    db = UserDB(str(tmp_path / "visibility.db"))
    db.create_material_schema()
    alice = db.create_user("projection-alice", "Password-123!")
    bob = db.create_user("projection-bob", "Password-123!")

    def seed(name, owner, kb="legacy", scope="private", current=True):
        doc_id = db.create_document_permission(name, kb, owner, scope=scope)
        if current:
            version_id = db.create_document_version(doc_id, name, f"/tmp/{name}", f"hash-{name}", owner)
            db.activate_document_version(doc_id, version_id, kb, name)
        return doc_id

    owned = seed("owned.txt", alice)
    public = seed("public.txt", alice, scope="public")
    shared_view = seed("shared-view.txt", alice, scope="shared")
    db.share_document(shared_view, bob, alice, "view")
    shared_edit = seed("shared-edit.txt", alice, scope="shared")
    db.share_document(shared_edit, bob, alice, "edit")
    mixed_private = seed("Shared.txt", alice)
    bob_owned = seed("shared.txt", bob)
    seed("versionless.txt", alice, current=False)
    deleted = seed("deleted.txt", alice)
    with db.locked_transaction() as conn:
        conn.execute("UPDATE document_permissions SET deleted_at=1 WHERE id=?", (deleted,))
        conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES('ORG',NULL,'Org','team')")
        conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('org-kb','ORG','organization')")
    org_doc = seed("org.txt", alice, kb="org-kb")
    yield (
        db,
        {
            "alice": alice,
            "bob": bob,
            "owned": owned,
            "public": public,
            "shared_view": shared_view,
            "shared_edit": shared_edit,
            "mixed_private": mixed_private,
            "bob_owned": bob_owned,
            "org": org_doc,
        },
    )
    db.close()


def test_sql_projection_matches_legacy_names_and_keeps_row_shape(projection_db):
    db, ids = projection_db
    repo = db.material_repo
    rows = repo.list_material_documents_for_kb("legacy", apply_legacy_acl=True, user_id=ids["bob"])
    old = repo.list_material_documents_with_current_version()
    old_legacy = [r for r in old if r["kb_id"] == "legacy"]
    allowed_names = set(db.get_accessible_doc_names("legacy", ids["bob"]))
    expected = [r for r in old_legacy if r["doc_name"] in allowed_names]
    assert rows == expected
    assert list(rows[0]) == list(old[0])


def test_legacy_sql_cursor_only_loads_authorized_rows(projection_db, monkeypatch):
    db, ids = projection_db
    repo = db.material_repo
    original_read = repo.cm.read_connection
    fetched = []

    class CursorCounter:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            fetched.append(len(rows))
            return rows

    class ConnectionCounter:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=()):
            assert "COLLATE BINARY" in statement
            return CursorCounter(self.connection.execute(statement, parameters))

    @contextmanager
    def counted_read():
        with original_read() as connection:
            yield ConnectionCounter(connection)

    monkeypatch.setattr(repo.cm, "read_connection", counted_read)
    rows = repo.list_material_documents_for_kb("legacy", apply_legacy_acl=True, user_id=ids["bob"])
    assert fetched == [4]
    assert {row["doc_name"] for row in rows} == {"public.txt", "shared-view.txt", "shared-edit.txt", "shared.txt"}


def test_visibility_iterator_preserves_kb_min_id_order_and_acl(projection_db):
    db, ids = projection_db
    visible = list(iter_visible_material_documents(db, db.get_user_by_id(ids["bob"])))
    assert [doc_id for doc_id, _ in visible] == [
        ids["public"],
        ids["shared_view"],
        ids["shared_edit"],
        ids["bob_owned"],
    ]
    assert all(row["kb_id"] == "legacy" for _, row in visible)


def test_none_user_id_does_not_select_full_legacy_rows(projection_db):
    db, _ = projection_db
    rows = db.material_repo.list_material_documents_for_kb("legacy", apply_legacy_acl=True, user_id=None)
    assert {row["doc_name"] for row in rows} == {"public.txt"}


def test_none_user_id_iterator_only_yields_public_legacy_documents(projection_db):
    db, _ = projection_db
    visible = list(iter_visible_material_documents(db, {"id": None, "is_admin": False}))
    assert {row["doc_name"] for _, row in visible} == {"public.txt"}


def test_authorized_org_kb_is_included_after_read_authorization(projection_db):
    db, ids = projection_db
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) VALUES('P-ALICE',?,'Alice','Engineer')",
            (ids["alice"],),
        )
        conn.execute("INSERT INTO org_memberships VALUES('P-ALICE','ORG',1)")
    visible = list(iter_visible_material_documents(db, db.get_user_by_id(ids["alice"])))
    assert ids["org"] in {doc_id for doc_id, _ in visible}


def test_all_full_path_uses_one_bulk_query_and_only_yields_decided_kbs(projection_db, monkeypatch):
    db, ids = projection_db
    admin = db.create_user("projection-admin", "Password-123!")
    db.set_user_admin(admin, True)
    repo = db.material_repo
    original = repo.list_material_documents_with_current_version
    calls = []

    def counted_bulk():
        calls.append("bulk")
        return original()

    def forbidden_projection(*args, **kwargs):
        raise AssertionError("all-full path must use the bulk reader")

    monkeypatch.setattr(repo, "list_material_documents_with_current_version", counted_bulk)
    monkeypatch.setattr(repo, "list_material_documents_for_kb", forbidden_projection)
    monkeypatch.setattr(repo, "list_material_document_kb_ids", lambda: ["legacy"])
    visible = list(iter_visible_material_documents(db, db.get_user_by_id(admin)))
    assert calls == ["bulk"]
    assert {row["kb_id"] for _, row in visible} == {"legacy"}


def test_kb_ids_are_ordered_from_unfiltered_live_current_rows(projection_db):
    db, _ = projection_db
    assert db.material_repo.list_material_document_kb_ids() == ["legacy", "org-kb"]


def _create_org_visibility_db(path, *, include_allowed_kb):
    db = UserDB(str(path))
    db.create_material_schema()
    user_id = db.create_user("org-visibility-user", "Password-123!")
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES('DENIED',NULL,'Denied','team')")
        conn.execute("INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('org-denied','DENIED','organization')")
        if include_allowed_kb:
            conn.execute("INSERT INTO org_units(code,parent_code,name,kind) VALUES('ALLOWED',NULL,'Allowed','team')")
            conn.execute(
                "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) VALUES('org-allowed','ALLOWED','organization')"
            )
            conn.execute(
                "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
                "VALUES('ORG-USER',?,'Org User','Engineer')",
                (user_id,),
            )
            conn.execute("INSERT INTO org_memberships VALUES('ORG-USER','ALLOWED',1)")

    def seed(kb_id, name):
        document_id = db.create_document_permission(name, kb_id, user_id)
        version_id = db.create_document_version(document_id, name, f"/tmp/{name}", f"hash-{name}", user_id)
        db.activate_document_version(document_id, version_id, kb_id, name)
        return document_id

    denied_id = seed("org-denied", "denied.txt")
    allowed_id = seed("org-allowed", "allowed.txt") if include_allowed_kb else None
    return db, user_id, denied_id, allowed_id


def test_denied_only_org_kb_does_not_call_bulk_reader(tmp_path, monkeypatch):
    db, user_id, denied_id, _ = _create_org_visibility_db(tmp_path / "denied-only.db", include_allowed_kb=False)
    try:
        repo = db.material_repo
        bulk_rows_fetched = []
        kb_rows_fetched = []

        def counted_bulk_reader():
            rows = original_bulk_reader()
            bulk_rows_fetched.extend(rows)
            return rows

        original_bulk_reader = repo.list_material_documents_with_current_version
        original_kb_reader = repo.list_material_documents_for_kb

        def counted_kb_reader(kb_id, **kwargs):
            rows = original_kb_reader(kb_id, **kwargs)
            kb_rows_fetched.extend((kb_id, int(row["id"])) for row in rows)
            return rows

        monkeypatch.setattr(repo, "list_material_documents_with_current_version", counted_bulk_reader)
        monkeypatch.setattr(repo, "list_material_documents_for_kb", counted_kb_reader)
        assert repo.list_material_document_kb_ids() == ["org-denied"]
        assert list(iter_visible_material_documents(db, db.get_user_by_id(user_id))) == []
        assert bulk_rows_fetched == []
        assert kb_rows_fetched == []
        assert denied_id not in {row_id for _, row_id in kb_rows_fetched}
    finally:
        db.close()


def test_mixed_allowed_and_denied_org_kbs_query_only_allowed_rows(tmp_path, monkeypatch):
    db, user_id, denied_id, allowed_id = _create_org_visibility_db(tmp_path / "mixed-org.db", include_allowed_kb=True)
    try:
        repo = db.material_repo
        bulk_calls = []
        original_bulk_reader = repo.list_material_documents_with_current_version

        def forbidden_bulk_reader():
            bulk_calls.append(True)
            return original_bulk_reader()

        original_kb_reader = repo.list_material_documents_for_kb
        kb_rows_fetched = []

        def counted_kb_reader(kb_id, **kwargs):
            rows = original_kb_reader(kb_id, **kwargs)
            kb_rows_fetched.extend((kb_id, int(row["id"])) for row in rows)
            return rows

        monkeypatch.setattr(repo, "list_material_documents_with_current_version", forbidden_bulk_reader)
        monkeypatch.setattr(repo, "list_material_documents_for_kb", counted_kb_reader)
        visible = list(iter_visible_material_documents(db, db.get_user_by_id(user_id)))
        assert [doc_id for doc_id, _ in visible] == [allowed_id]
        assert bulk_calls == []
        assert kb_rows_fetched == [("org-allowed", allowed_id)]
        assert denied_id not in {row_id for _, row_id in kb_rows_fetched}
    finally:
        db.close()
