"""Baseline facade parity tests for rag.user_db.UserDB.

These tests establish an exhaustive regression baseline for all core methods
exposed by UserDB (users, org, conversations, messages, feedback, documents,
versions, spreadsheets, and analysis cards) before modular sub-repository decomposition.
"""

from __future__ import annotations

import threading
import time

import pytest

from rag.db.connection import ConnectionManager
from rag.org_auth.repository import OrgRepository
from rag.user_db import UserDB


@pytest.fixture()
def db(tmp_path):
    """Yield a fresh UserDB instance backed by a temporary SQLite file."""
    path = str(tmp_path / "facade_test.db")
    udb = UserDB(path)
    yield udb
    udb.close()


# ----------------------------------------------------------------------
# 1. User Management
# ----------------------------------------------------------------------


def test_user_management_facade(db):
    # Ensure development user
    dev_user = db.ensure_development_user()
    assert dev_user is not None
    assert dev_user["id"] == 0

    # Create user
    uid = db.create_user("charlie", "Password123")
    assert isinstance(uid, int) and uid > 0

    # Authenticate
    auth_success = db.authenticate("charlie", "Password123")
    assert auth_success is not None
    assert auth_success["username"] == "charlie"
    assert db.authenticate("charlie", "WrongPassword123") is None

    # Get by ID and username
    by_id = db.get_user_by_id(uid)
    assert by_id is not None
    assert by_id["username"] == "charlie"
    assert by_id["is_admin"] is False

    by_name = db.get_user_by_username("charlie")
    assert by_name is not None
    assert by_name["id"] == uid

    # Admin flag toggle
    db.set_user_admin(uid, True)
    assert db.get_user_by_id(uid)["is_admin"] is True

    # Search users
    search_results = db.search_users("char")
    assert any(u["id"] == uid and u["username"] == "charlie" for u in search_results)

    # Password management
    db.change_password(uid, "Password123", "NewSecret456")
    assert db.authenticate("charlie", "NewSecret456") is not None
    assert db.authenticate("charlie", "Password123") is None

    # Reset password (admin bypass)
    db.reset_password(uid, "ResetPass789")
    assert db.authenticate("charlie", "ResetPass789") is not None


# ----------------------------------------------------------------------
# 2. Organization & Department Hierarchy
# ----------------------------------------------------------------------


def test_organization_repository_facade(db):
    org_repo = OrgRepository(db)

    # Create top-level unit
    org_repo.create_unit("HQ", None, "Headquarters", "division")
    unit = org_repo.get_unit("HQ")
    assert unit is not None
    assert unit["name"] == "Headquarters"
    assert unit["kind"] == "division"

    # Create child units
    org_repo.create_units([
        ("TECH", "HQ", "Technology Dept", "department"),
        ("AI_LAB", "TECH", "AI Research Lab", "team"),
    ])

    descendants = org_repo.descendants("HQ")
    assert "TECH" in descendants
    assert "AI_LAB" in descendants

    # Bind person to user
    uid = db.create_user("researcher", "Password123")
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people (person_code, user_id, display_name, job_title) "
            "VALUES (?, ?, ?, ?)",
            ("P001", uid, "Dr. Alice", "Principal Scientist"),
        )
        conn.execute(
            "INSERT INTO org_memberships (person_code, org_code, is_primary) VALUES (?, ?, 1)",
            ("P001", "AI_LAB"),
        )
        conn.execute(
            "INSERT INTO org_manager_scopes (person_code, org_code) VALUES (?, ?)",
            ("P001", "AI_LAB"),
        )
        conn.execute(
            "INSERT INTO org_sensitive_qualifications (person_code, sensitivity) VALUES (?, ?)",
            ("P001", "finance"),
        )

    profile = org_repo.get_profile_for_user(uid)
    assert profile is not None
    assert profile.display_name == "Dr. Alice"
    assert profile.primary_org["code"] == "AI_LAB"
    assert "AI_LAB" in org_repo.manager_scopes("P001")
    assert "finance" in org_repo.qualifications("P001")


# ----------------------------------------------------------------------
# 3. Conversations, Messages & Feedback
# ----------------------------------------------------------------------


def test_conversation_messages_and_feedback_facade(db):
    uid = db.create_user("chatter", "Password123")

    # Conversation
    cid = db.create_conversation(uid, "Deep Research Session")
    assert isinstance(cid, int) and uid > 0

    conv = db.get_conversation(cid, uid)
    assert conv is not None
    assert conv["title"] == "Deep Research Session"

    db.update_conversation_title(cid, uid, "Updated Session")
    convs = db.list_conversations(uid)
    assert any(c["id"] == cid and c["title"] == "Updated Session" for c in convs)

    # Messages
    m1 = db.add_message(cid, "user", "What is RAG?")
    m2 = db.add_message(cid, "assistant", "Retrieval-Augmented Generation.", confidence_level="high")
    assert m1 > 0 and m2 > 0

    messages = db.get_messages(cid, uid)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Retrieval-Augmented Generation."

    # Feedback
    fid = db.add_feedback(m2, uid, 1, "Accurate and concise.")
    assert isinstance(fid, int) and fid > 0

    feedback = db.get_feedback(fid)
    assert feedback is not None
    assert feedback["message_id"] == m2
    assert feedback["comment"] == "Accurate and concise."

    # Deletion of conversation (tested on conversation without referenced feedback)
    c_temp = db.create_conversation(uid, "Temporary")
    db.add_message(c_temp, "user", "one-time")
    assert db.delete_conversation(c_temp, uid) is True
    assert db.get_conversation(c_temp, uid) is None
    assert db.get_messages(c_temp, uid) == []


# ----------------------------------------------------------------------
# 4. Knowledge Base, Permissions & Sharing
# ----------------------------------------------------------------------


def test_kb_and_document_permissions_facade(db):
    owner_id = db.create_user("doc_owner", "Password123")
    viewer_id = db.create_user("doc_viewer", "Password123")

    # KB Metadata
    db.create_kb_metadata("ml_kb", "Machine Learning KB", owner_id=owner_id, scope="private")
    kb = db.get_kb_metadata("ml_kb")
    assert kb is not None
    assert kb["name"] == "Machine Learning KB"
    assert kb["owner_id"] == owner_id

    # KB Sharing
    share_id = db.share_kb("ml_kb", viewer_id, granted_by=owner_id, permission="view")
    assert share_id > 0
    assert db.is_kb_shared("ml_kb", viewer_id) is True
    shared_users = db.list_kb_shared_users("ml_kb")
    assert any(u["id"] == viewer_id for u in shared_users)

    db.unshare_kb("ml_kb", viewer_id)
    assert db.is_kb_shared("ml_kb", viewer_id) is False

    # Document Permission CRUD
    doc_id = db.create_document_permission("transformer.pdf", "ml_kb", owner_id, is_public=False)
    assert isinstance(doc_id, int) and doc_id > 0

    perm = db.get_document_permission("transformer.pdf", "ml_kb")
    assert perm is not None
    assert perm["id"] == doc_id
    assert perm["is_public"] is False

    # Toggle visibility
    db.toggle_document_visibility(doc_id, is_public=True)
    perm_updated = db.get_document_permission_by_id(doc_id)
    assert perm_updated["is_public"] is True

    # Document Sharing
    d_share_id = db.share_document(doc_id, viewer_id, granted_by=owner_id, permission="edit")
    assert d_share_id > 0
    assert db.is_document_shared(doc_id, viewer_id) is True
    assert any(u["id"] == viewer_id for u in db.list_shared_users(doc_id))

    db.unshare_document(doc_id, viewer_id)
    assert db.is_document_shared(doc_id, viewer_id) is False

    # Cleanup
    db.delete_document_permission(doc_id)
    assert db.get_document_permission_by_id(doc_id) is None


# ----------------------------------------------------------------------
# 5. Document Versioning & Background Jobs
# ----------------------------------------------------------------------


def test_document_versioning_and_jobs_facade(db):
    uid = db.create_user("version_admin", "Password123")
    doc_id = db.create_document_permission("spec.docx", "spec_kb", uid)

    # Version 1
    v1_id = db.create_document_version(
        document_id=doc_id,
        original_filename="spec.docx",
        file_path="/data/storage/spec_v1.docx",
        content_hash="sha256:abc1",
        uploaded_by=uid,
    )
    assert isinstance(v1_id, int) and v1_id > 0

    v1 = db.get_document_version_by_id(v1_id)
    assert v1["version_no"] == 1
    assert v1["status"] == "staging"
    assert v1["validity_status"] == "valid"

    # Promote to current
    db.activate_document_version(document_id=doc_id, version_id=v1_id, kb_id="spec_kb", filename="spec.docx")
    curr_v = db.get_current_document_version(doc_id)
    assert curr_v is not None
    assert curr_v["id"] == v1_id
    assert curr_v["status"] == "current"

    # Version validity update
    db.set_version_validity(v1_id, "reported_outdated")
    assert db.get_document_version_by_id(v1_id)["validity_status"] == "reported_outdated"

    # Material lifecycle schema & job queue
    db.create_material_schema()
    job_id = db.enqueue_material_job(doc_id, v1_id)
    assert isinstance(job_id, int) and job_id > 0

    job = db.get_index_job(job_id)
    assert job is not None
    assert job["document_id"] == doc_id
    assert job["version_id"] == v1_id
    assert job["status"] == "queued"


# ----------------------------------------------------------------------
# 6. Structured Datasets & Spreadsheet Edits
# ----------------------------------------------------------------------


def test_structured_dataset_and_spreadsheet_facade(db):
    uid = db.create_user("sheet_editor", "Password123")
    doc_id = db.create_document_permission("financials.xlsx", "fin_kb", uid)
    v1_id = db.create_document_version(
        document_id=doc_id,
        original_filename="financials.xlsx",
        file_path="/storage/financials_v1.xlsx",
        content_hash="sha256:fin123",
        uploaded_by=uid,
    )
    db.activate_document_version(document_id=doc_id, version_id=v1_id, kb_id="fin_kb", filename="financials.xlsx")

    # Structured Dataset
    dataset_id = db.create_structured_dataset(
        document_id=doc_id,
        document_version_id=v1_id,
        fmt="xlsx",
        semantic_index_mode="schema_only",
    )
    assert isinstance(dataset_id, int) and dataset_id > 0

    db.activate_structured_dataset(dataset_id)
    dataset = db.get_structured_dataset_for_version(v1_id)
    assert dataset is not None
    assert dataset["id"] == dataset_id
    assert dataset["status"] == "ready"

    # Spreadsheet Edit Session
    session_id, v2_id = db.reserve_spreadsheet_edit(
        document_id=doc_id,
        base_version_id=v1_id,
        actor_user_id=uid,
        original_filename="financials.xlsx",
        staged_path="/storage/financials_v2.xlsx",
        content_hash="sha256:fin456",
        operation="edit",
        reason="Updated Q3 revenue",
        restored_from_version_id=None,
        changes=(
            {
                "sheet_key": "Sheet1",
                "sheet_name_snapshot": "Summary",
                "cell_address": "B2",
                "old_value_type": "number",
                "old_value_json": 100,
                "new_value_type": "number",
                "new_value_json": 150,
                "sensitive": False,
            },
        ),
    )
    assert session_id > 0
    assert v2_id > 0

    session = db.get_spreadsheet_edit_session(session_id)
    assert session is not None
    assert session["operation"] == "edit"
    assert session["status"] == "pending"

    db.set_spreadsheet_edit_status(session_id, "active")
    session_updated = db.get_spreadsheet_edit_session(session_id)
    assert session_updated["status"] == "active"


# ----------------------------------------------------------------------
# 7. Analysis Cards & Questions
# ----------------------------------------------------------------------


def test_analysis_cards_and_questions_facade(db):
    uid = db.create_user("analyst", "Password123")

    # Card CRUD
    card_id = db.create_card(uid, "Competitive Analysis")
    assert isinstance(card_id, int) and card_id > 0

    cards = db.list_cards(uid)
    assert any(c["id"] == card_id and c["name"] == "Competitive Analysis" for c in cards)

    db.rename_card(card_id, "2026 Competitive Analysis", uid)
    db.update_card_summary(card_id, "Overview of top competitors", uid)
    assert db.get_card_summary(card_id) == "Overview of top competitors"

    cards_updated = db.list_cards(uid)
    assert any(c["id"] == card_id and c["name"] == "2026 Competitive Analysis" for c in cards_updated)

    # Questions
    q_id = db.add_question(card_id, "What is our market share?", "Approximately 25%", user_id=uid)
    assert isinstance(q_id, int) and q_id > 0

    questions = db.get_questions(card_id)
    assert len(questions) == 1
    assert questions[0]["question"] == "What is our market share?"
    assert questions[0]["answer"] == "Approximately 25%"

    # Deletion
    assert db.delete_question(q_id, uid) is True
    assert db.get_questions(card_id) == []
    assert db.delete_card(card_id, uid) is True
    assert db.list_cards(uid) == []


# ----------------------------------------------------------------------
# 8. Integrity & Transaction Semantics
# ----------------------------------------------------------------------


def test_database_transaction_and_locks_facade(db):
    # Foreign key constraint verification
    violations = db.foreign_key_check()
    assert len(violations) == 0

    # locked_read
    with db.locked_read() as conn:
        res = conn.execute("SELECT 1").fetchone()
        assert res[0] == 1

    # locked_transaction
    with db.locked_transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS facade_tmp (key TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO facade_tmp (key) VALUES ('atomic')")

    with db.locked_read() as conn:
        row = conn.execute("SELECT key FROM facade_tmp WHERE key='atomic'").fetchone()
        assert row is not None
        assert row["key"] == "atomic"


def test_user_db_connection_manager_injection(tmp_path):
    """Test UserDB accepting an externally managed ConnectionManager instance."""
    path = str(tmp_path / "injected.db")
    cm = ConnectionManager(path, busy_timeout_ms=5000)
    db = UserDB(connection_manager=cm)

    assert db.cm is cm
    assert db._cm is cm
    assert db._lock is cm._write_lock
    assert db._conn is cm.get_write_connection()
    assert db.db_path == path

    uid = db.create_user("injected_user", "Pass123456")
    user = db.get_user_by_id(uid)
    assert user is not None
    assert user["username"] == "injected_user"

    db.close()
    assert cm._closed is True


def test_user_db_wal_read_write_concurrency(tmp_path):
    """Verify that reader queries on UserDB execute concurrently without waiting for writer locks."""
    path = str(tmp_path / "concurrency.db")
    db = UserDB(path)
    uid = db.create_user("speedy_reader", "Password123")
    cid = db.create_conversation(uid, "Concurrency Test")

    write_holding_event = threading.Event()
    read_completed_event = threading.Event()
    results: dict[str, float] = {}

    def writer_worker():
        # Start a write transaction on UserDB and simulate a slow transaction holding the write lock
        with db.locked_transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO chat_messages (conversation_id, role, content) VALUES (?, 'user', 'slow_write')",
                (cid,),
            )
            write_holding_event.set()
            time.sleep(0.3)
        results["writer_finished_at"] = time.time()

    def reader_worker():
        write_holding_event.wait(timeout=2.0)
        start_read = time.time()
        # Read operation on UserDB must use query_only read connection and not be blocked by the writer lock
        user = db.get_user_by_id(uid)
        assert user is not None
        assert user["username"] == "speedy_reader"
        convs = db.list_conversations(uid)
        assert len(convs) == 1
        results["reader_finished_at"] = time.time()
        results["read_duration"] = results["reader_finished_at"] - start_read
        read_completed_event.set()

    writer_t = threading.Thread(target=writer_worker)
    reader_t = threading.Thread(target=reader_worker)

    writer_t.start()
    reader_t.start()

    writer_t.join(timeout=3.0)
    reader_t.join(timeout=3.0)

    # Reader must finish before writer releases its 300ms transaction
    assert results["reader_finished_at"] < results["writer_finished_at"]
    # Reading must be rapid and unaffected by the 300ms writer sleep
    assert results["read_duration"] < 0.25

    db.close()

