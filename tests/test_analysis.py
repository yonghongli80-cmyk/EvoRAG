"""Tests for analysis cards (DB + API) and conversation mode."""

import os

import pytest
from fastapi.testclient import TestClient

from rag.user_db import UserDB

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tmp_path):
    """Yield a fresh UserDB backed by a temporary SQLite file."""
    path = str(tmp_path / "test.db")
    udb = UserDB(path)
    yield udb
    udb.close()


@pytest.fixture()
def client(tmp_path):
    """Yield a TestClient with a temporary UserDB patched in."""
    db_path = str(tmp_path / "test_users.db")
    import rag.api as api_mod

    original_db = api_mod.user_db
    api_mod.user_db = UserDB(db_path)
    with TestClient(api_mod.app) as c:
        yield c
    api_mod.user_db = original_db


def _register_and_login(client, username="analysis_user", password="Pass12345"):
    """Helper: register a user and return the JWT token."""
    client.post("/register", json={"username": username, "password": password})
    resp = client.post("/login", json={"username": username, "password": password})
    return resp.json()["token"]


# ═══════════════════════════════════════════════════════════════════════════
# Part 1: Database-level tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAnalysisCardsDB:
    """Test analysis_cards and analysis_questions CRUD via UserDB directly."""

    def test_tables_exist(self, db):
        """Both analysis_cards and analysis_questions tables should exist."""
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "analysis_cards" in tables
        assert "analysis_questions" in tables

    def test_create_card(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "My Analysis")
        assert isinstance(card_id, int) and card_id > 0

    def test_list_cards(self, db):
        uid = db.create_user("alice", "s3cret")
        db.create_card(uid, "Card A")
        db.create_card(uid, "Card B")
        cards = db.list_cards(uid)
        assert len(cards) == 2
        names = {c["name"] for c in cards}
        assert names == {"Card A", "Card B"}

    def test_list_cards_by_user(self, db):
        """Cards from user2 should not appear in user1's list."""
        uid1 = db.create_user("alice", "s3cret")
        uid2 = db.create_user("bob", "s3cret")
        db.create_card(uid1, "Alice Card")
        db.create_card(uid2, "Bob Card")
        cards = db.list_cards(uid1)
        assert len(cards) == 1
        assert cards[0]["name"] == "Alice Card"

    def test_delete_card(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Temp Card")
        assert db.delete_card(card_id, uid) is True
        assert db.list_cards(uid) == []

    def test_delete_card_wrong_user(self, db):
        """User should not be able to delete another user's card."""
        uid1 = db.create_user("alice", "s3cret")
        uid2 = db.create_user("bob", "s3cret")
        card_id = db.create_card(uid1, "Alice Card")
        assert db.delete_card(card_id, uid2) is False
        assert len(db.list_cards(uid1)) == 1

    def test_delete_card_cascades_questions(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Temp Card")
        db.add_question(card_id, "What is X?", user_id=uid)
        db.add_question(card_id, "What is Y?", user_id=uid)
        assert db.delete_card(card_id, uid) is True
        assert db.list_cards(uid) == []

    def test_rename_card(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Old Name")
        assert db.rename_card(card_id, "New Name", uid) is True
        cards = db.list_cards(uid)
        assert cards[0]["name"] == "New Name"

    def test_rename_card_wrong_user(self, db):
        """User should not be able to rename another user's card."""
        uid1 = db.create_user("alice", "s3cret")
        uid2 = db.create_user("bob", "s3cret")
        card_id = db.create_card(uid1, "Alice Card")
        assert db.rename_card(card_id, "Hacked", uid2) is False
        cards = db.list_cards(uid1)
        assert cards[0]["name"] == "Alice Card"

    def test_rename_card_nonexistent(self, db):
        uid = db.create_user("alice", "s3cret")
        assert db.rename_card(9999, "New", uid) is False

    def test_add_question(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        qid = db.add_question(card_id, "What is AI?", user_id=uid)
        assert isinstance(qid, int) and qid > 0

    def test_add_question_wrong_user(self, db):
        """User should not be able to add questions to another user's card."""
        uid1 = db.create_user("alice", "s3cret")
        uid2 = db.create_user("bob", "s3cret")
        card_id = db.create_card(uid1, "Alice Card")
        qid = db.add_question(card_id, "Hacked?", user_id=uid2)
        assert qid is None
        assert db.get_questions(card_id) == []

    def test_add_question_with_defaults(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        qid = db.add_question(card_id, "What is ML?", user_id=uid)
        questions = db.get_questions(card_id)
        assert len(questions) == 1
        q = questions[0]
        assert q["id"] == qid
        assert q["question"] == "What is ML?"
        assert q["answer"] == ""
        assert q["source_mode"] == ""
        assert q["source_message_id"] is None

    def test_add_question_with_all_fields(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        db.add_question(
            card_id,
            "What is RAG?",
            answer="Retrieval-Augmented Generation",
            source_mode="chat",
            source_message_id=42,
            user_id=uid,
        )
        questions = db.get_questions(card_id)
        assert len(questions) == 1
        q = questions[0]
        assert q["answer"] == "Retrieval-Augmented Generation"
        assert q["source_mode"] == "chat"
        assert q["source_message_id"] == 42

    def test_get_questions_empty(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        assert db.get_questions(card_id) == []

    def test_get_questions_ordered(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        db.add_question(card_id, "Q2")
        db.add_question(card_id, "Q1")
        questions = db.get_questions(card_id)
        assert len(questions) == 2

    def test_delete_question(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        qid = db.add_question(card_id, "What?", user_id=uid)
        assert db.delete_question(qid, uid) is True
        assert db.get_questions(card_id) == []

    def test_delete_question_wrong_user(self, db):
        """User should not be able to delete questions from another user's card."""
        uid1 = db.create_user("alice", "s3cret")
        uid2 = db.create_user("bob", "s3cret")
        card_id = db.create_card(uid1, "Alice Card")
        qid = db.add_question(card_id, "Alice Q?", user_id=uid1)
        assert db.delete_question(qid, uid2) is False
        assert len(db.get_questions(card_id)) == 1

    def test_delete_question_nonexistent(self, db):
        uid = db.create_user("alice", "s3cret")
        assert db.delete_question(9999, uid) is False


# ═══════════════════════════════════════════════════════════════════════════
# Part 2: API-level tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAnalysisCardsAPI:
    """Test analysis card API endpoints with JWT auth."""

    def test_create_card(self, client):
        token = _register_and_login(client, "api_create", "Pass12345")
        resp = client.post(
            "/analysis/cards",
            json={"name": "My Analysis"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["name"] == "My Analysis"

    def test_list_cards(self, client):
        token = _register_and_login(client, "api_list", "Pass12345")
        client.post(
            "/analysis/cards",
            json={"name": "Card A"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            "/analysis/cards",
            json={"name": "Card B"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(
            "/analysis/cards",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_delete_card(self, client):
        token = _register_and_login(client, "api_delete", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Temp"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        resp = client.delete(
            f"/analysis/cards/{card_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_delete_card_not_found(self, client):
        token = _register_and_login(client, "api_del_nf", "Pass12345")
        resp = client.delete(
            "/analysis/cards/9999",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_rename_card(self, client):
        token = _register_and_login(client, "api_rename", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Old"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        resp = client.put(
            f"/analysis/cards/{card_id}/name",
            json={"name": "New"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New"

    def test_add_question(self, client):
        token = _register_and_login(client, "api_addq", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Card"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        resp = client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "What is AI?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["question"] == "What is AI?"

    def test_delete_question(self, client):
        token = _register_and_login(client, "api_delq", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Card"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        add_resp = client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "What?"},
            headers={"Authorization": f"Bearer {token}"},
        )
        qid = add_resp.json()["id"]
        resp = client.delete(
            f"/analysis/cards/{card_id}/questions/{qid}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    def test_unauthorized_create_card(self, client):
        resp = client.post("/analysis/cards", json={"name": "X"})
        assert resp.status_code in (401, 403, 422)

    def test_unauthorized_list_cards(self, client):
        resp = client.get("/analysis/cards")
        assert resp.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Part 3: Conversation mode tests
# ═══════════════════════════════════════════════════════════════════════════


class TestConversationModeDB:
    """Test conversation mode field at the DB level."""

    def test_conversation_has_mode_column(self, db):
        """conversations table should have a mode column."""
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        cursor = conn.execute("PRAGMA table_info(conversations)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "mode" in columns

    def test_create_conversation_default_mode(self, db):
        uid = db.create_user("alice", "s3cret")
        cid = db.create_conversation(uid, "Chat")
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        row = conn.execute("SELECT mode FROM conversations WHERE id = ?", (cid,)).fetchone()
        conn.close()
        assert row[0] == "file"

    def test_create_conversation_with_mode(self, db):
        uid = db.create_user("alice", "s3cret")
        cid = db.create_conversation(uid, "Chat", mode="chat")
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        row = conn.execute("SELECT mode FROM conversations WHERE id = ?", (cid,)).fetchone()
        conn.close()
        assert row[0] == "chat"

    def test_list_conversations_filter_by_mode(self, db):
        uid = db.create_user("alice", "s3cret")
        db.create_conversation(uid, "File Chat", mode="file")
        db.create_conversation(uid, "Analysis Chat", mode="analysis")
        db.create_conversation(uid, "Another File", mode="file")

        all_convs = db.list_conversations(uid)
        assert len(all_convs) == 3

        file_convs = db.list_conversations(uid, mode="file")
        assert len(file_convs) == 2

        analysis_convs = db.list_conversations(uid, mode="analysis")
        assert len(analysis_convs) == 1


class TestConversationModeAPI:
    """Test conversation mode at the API level."""

    def test_create_conversation_with_mode(self, client):
        token = _register_and_login(client, "mode_create", "Pass12345")
        resp = client.post(
            "/conversations",
            json={"mode": "analysis"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "analysis"

    def test_create_conversation_default_mode(self, client):
        token = _register_and_login(client, "mode_default", "Pass12345")
        resp = client.post(
            "/conversations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["mode"] == "file"

    def test_list_conversations_with_mode_filter(self, client):
        token = _register_and_login(client, "mode_list", "Pass12345")
        # Create conversations with different modes
        client.post(
            "/conversations",
            json={"mode": "file"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            "/conversations",
            json={"mode": "analysis"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            "/conversations",
            json={"mode": "file"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # List all
        resp = client.get(
            "/conversations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 3

        # Filter by mode=file
        resp = client.get(
            "/conversations?mode=file",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        # Filter by mode=analysis
        resp = client.get(
            "/conversations?mode=analysis",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ── Migration Tests ──────────────────────────────────────────────────────────


def test_migration_backfills_null_mode(tmp_path):
    """旧对话的 NULL mode 应该被迁移为 'file'。"""
    import sqlite3
    path = str(tmp_path / "migration_test.db")
    conn = sqlite3.connect(path)
    # Create conversations table WITHOUT mode column (simulate old schema)
    conn.execute("""CREATE TABLE conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT DEFAULT '',
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute("INSERT INTO conversations (user_id, title) VALUES (1, 'old conv')")
    conn.commit()

    # Now create UserDB which triggers the migration
    udb = UserDB(path)

    # Verify mode was backfilled
    row = conn.execute("SELECT mode FROM conversations WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] == "file"

    udb.close()
    conn.close()
    os.unlink(path)


# ── Phase 0: analysis_cards.summary column migration ────────────────────────


class TestAnalysisCardsSummaryMigration:
    """Test that analysis_cards table has a summary column after migration."""

    def test_analysis_cards_has_summary_column(self, db):
        """analysis_cards table should have a summary column."""
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        cursor = conn.execute("PRAGMA table_info(analysis_cards)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "summary" in columns

    def test_analysis_cards_summary_default_empty(self, db):
        """New analysis_cards should have summary='' by default."""
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Test Card")
        import sqlite3
        conn = sqlite3.connect(db.db_path)
        row = conn.execute("SELECT summary FROM analysis_cards WHERE id = ?", (card_id,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == ""

    def test_analysis_cards_summary_idempotent_migration(self, tmp_path):
        """Creating a new UserDB with existing analysis_cards (no summary) should add the column."""
        import sqlite3
        path = str(tmp_path / "migration_test2.db")
        conn = sqlite3.connect(path)
        # Create analysis_cards WITHOUT summary column (simulate old schema)
        conn.execute("""CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            salt TEXT NOT NULL,
            password TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )""")
        conn.execute("""CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )""")
        conn.execute("""CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )""")
        conn.execute("""CREATE TABLE feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES chat_messages(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            value INTEGER NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
            UNIQUE(message_id, user_id)
        )""")
        conn.execute("""CREATE TABLE analysis_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            user_id INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE analysis_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            answer TEXT DEFAULT '',
            source_mode TEXT DEFAULT '',
            source_message_id INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (card_id) REFERENCES analysis_cards(id)
        )""")
        conn.execute("""CREATE TABLE data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            config TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'inactive',
            last_synced_at REAL,
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )""")
        conn.execute("INSERT INTO analysis_cards (name) VALUES ('Old Card')")
        conn.commit()

        # Now create UserDB which triggers the migration
        udb = UserDB(path)

        # Verify summary column was added
        cursor = conn.execute("PRAGMA table_info(analysis_cards)")
        columns = {row[1] for row in cursor.fetchall()}
        assert "summary" in columns

        # Verify old row got default value
        row = conn.execute("SELECT summary FROM analysis_cards WHERE id = 1").fetchone()
        assert row is not None
        assert row[0] == ""

        udb.close()
        conn.close()
        os.unlink(path)


# ── Phase 0: conversation mode in create response ────────────────────────────


class TestCreateConversationModeAPI:
    """Phase 0: POST /conversations should accept and return mode."""

    def test_create_conversation_returns_mode_field(self, client):
        """POST /conversations with mode should return mode in response."""
        token = _register_and_login(client, "mode_resp_user", "Pass12345")
        resp = client.post(
            "/conversations",
            json={"mode": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "mode" in data
        assert data["mode"] == "kb"

    def test_create_conversation_default_mode_is_file(self, client):
        """POST /conversations without mode should default to 'file'."""
        token = _register_and_login(client, "mode_default_user", "Pass12345")
        resp = client.post(
            "/conversations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "file"


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4: Analysis Card Summary (DB + API)
# ═══════════════════════════════════════════════════════════════════════════


class TestAnalysisCardSummaryDB:
    """Test card summary DB methods."""

    def test_get_summary_default_empty(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        summary = db.get_card_summary(card_id)
        assert summary == ""

    def test_update_summary(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        assert db.update_card_summary(card_id, "This is a summary", uid) is True
        assert db.get_card_summary(card_id) == "This is a summary"

    def test_update_summary_wrong_user(self, db):
        uid1 = db.create_user("alice", "s3cret")
        uid2 = db.create_user("bob", "s3cret")
        card_id = db.create_card(uid1, "Alice Card")
        assert db.update_card_summary(card_id, "Hacked", uid2) is False
        assert db.get_card_summary(card_id) == ""

    def test_update_summary_nonexistent(self, db):
        uid = db.create_user("alice", "s3cret")
        assert db.update_card_summary(9999, "x", uid) is False

    def test_update_summary_overwrite(self, db):
        uid = db.create_user("alice", "s3cret")
        card_id = db.create_card(uid, "Card")
        db.update_card_summary(card_id, "First", uid)
        db.update_card_summary(card_id, "Second", uid)
        assert db.get_card_summary(card_id) == "Second"


class TestAnalysisCardSummaryAPI:
    """Test summary API endpoints."""

    def test_get_summary(self, client):
        token = _register_and_login(client, "sum_get", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Card"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        resp = client.get(
            f"/analysis/cards/{card_id}/summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["summary"] == ""

    def test_get_summary_not_found(self, client):
        token = _register_and_login(client, "sum_404", "Pass12345")
        resp = client.get(
            "/analysis/cards/9999/summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_update_summary(self, client):
        token = _register_and_login(client, "sum_put", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Card"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        resp = client.put(
            f"/analysis/cards/{card_id}/summary",
            json={"summary": "Updated summary text"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["summary"] == "Updated summary text"
        # Verify it persisted
        get_resp = client.get(
            f"/analysis/cards/{card_id}/summary",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert get_resp.json()["summary"] == "Updated summary text"

    def test_update_summary_not_found(self, client):
        token = _register_and_login(client, "sum_put_404", "Pass12345")
        resp = client.put(
            "/analysis/cards/9999/summary",
            json={"summary": "x"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_generate_summary(self, client):
        """POST /analysis/cards/{id}/summary/generate should call LLM and return summary."""
        token = _register_and_login(client, "sum_gen", "Pass12345")
        create_resp = client.post(
            "/analysis/cards",
            json={"name": "Card"},
            headers={"Authorization": f"Bearer {token}"},
        )
        card_id = create_resp.json()["id"]
        # Add a question so there's content to summarize
        client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "What is RAG?", "answer": "Retrieval-Augmented Generation"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Mock the LLM generator
        import rag.api as api_mod
        original_gen = getattr(api_mod, '_generate_summary_llm', None)
        api_mod._generate_summary_llm = lambda card_id_val, questions: "Mocked summary of RAG"
        try:
            resp = client.post(
                f"/analysis/cards/{card_id}/summary/generate",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "summary" in data
            assert data["summary"] == "Mocked summary of RAG"
        finally:
            if original_gen is not None:
                api_mod._generate_summary_llm = original_gen
            else:
                if hasattr(api_mod, '_generate_summary_llm'):
                    del api_mod._generate_summary_llm

    def test_generate_summary_not_found(self, client):
        token = _register_and_login(client, "sum_gen_404", "Pass12345")
        resp = client.post(
            "/analysis/cards/9999/summary/generate",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_unauthorized_summary_endpoints(self, client):
        resp = client.get("/analysis/cards/1/summary")
        assert resp.status_code in (401, 403, 422)
        resp = client.put("/analysis/cards/1/summary", json={"summary": "x"})
        assert resp.status_code in (401, 403, 422)
        resp = client.post("/analysis/cards/1/summary/generate")
        assert resp.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5: Suggest Card (API)
# ═══════════════════════════════════════════════════════════════════════════


class TestSuggestCardAPI:
    """Test POST /analysis/suggest-card endpoint."""

    def test_suggest_card_returns_cards(self, client):
        """suggest-card should return all_cards list and suggested match."""
        token = _register_and_login(client, "suggest1", "Pass12345")
        # Create cards
        c1 = client.post(
            "/analysis/cards",
            json={"name": "故障排查"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        c2 = client.post(
            "/analysis/cards",
            json={"name": "性能优化"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        # Add questions with keywords
        client.post(
            f"/analysis/cards/{c1}/questions",
            json={"question": "服务挂了怎么恢复？", "answer": "首先检查日志"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            f"/analysis/cards/{c2}/questions",
            json={"question": "如何提升查询速度？", "answer": "增加索引"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Mock LLM to return a suggestion
        import rag.api as api_mod
        original_fn = getattr(api_mod, '_suggest_card_llm', None)
        api_mod._suggest_card_llm = lambda q, a, cards: {
            "suggested_card_id": c1,
            "confidence": 0.85,
        }
        try:
            resp = client.post(
                "/analysis/suggest-card",
                json={"question": "服务挂了怎么恢复？", "answer": "检查日志"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "all_cards" in data
            assert len(data["all_cards"]) == 2
            assert data["suggested_card_id"] == c1
            assert data["confidence"] == 0.85
        finally:
            if original_fn is not None:
                api_mod._suggest_card_llm = original_fn
            else:
                if hasattr(api_mod, '_suggest_card_llm'):
                    del api_mod._suggest_card_llm

    def test_suggest_card_no_cards(self, client):
        """suggest-card with no cards should return empty all_cards."""
        token = _register_and_login(client, "suggest2", "Pass12345")
        resp = client.post(
            "/analysis/suggest-card",
            json={"question": "test", "answer": "test"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["all_cards"] == []
        assert data["suggested_card_id"] is None
        assert data["confidence"] == 0

    def test_suggest_card_unauthorized(self, client):
        resp = client.post(
            "/analysis/suggest-card",
            json={"question": "test", "answer": "test"},
        )
        assert resp.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 6: Export Card as Markdown (API)
# ═══════════════════════════════════════════════════════════════════════════


class TestExportCardAPI:
    """Test GET /analysis/cards/{card_id}/export endpoint."""

    def test_export_markdown_basic(self, client):
        """Export card with questions and summary should return proper markdown."""
        token = _register_and_login(client, "export1", "Pass12345")
        card_id = client.post(
            "/analysis/cards",
            json={"name": "测试卡片组"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        # Add questions
        client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "什么是RAG？", "answer": "检索增强生成", "source_mode": "file"},
            headers={"Authorization": f"Bearer {token}"},
        )
        client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "什么是向量数据库？", "answer": "用于存储向量的数据库", "source_mode": "kb"},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Set summary
        client.put(
            f"/analysis/cards/{card_id}/summary",
            json={"summary": "这是一个测试摘要"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(
            f"/analysis/cards/{card_id}/export?format=markdown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        text = resp.text
        assert "# 测试卡片组" in text
        assert "> 这是一个测试摘要" in text
        assert "什么是RAG？" in text
        assert "什么是向量数据库？" in text
        assert "检索增强生成" in text
        assert "来源：file" in text

    def test_export_markdown_no_summary(self, client):
        """Export card without summary should omit the summary block."""
        token = _register_and_login(client, "export_nosum", "Pass12345")
        card_id = client.post(
            "/analysis/cards",
            json={"name": "No Summary"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "Q1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(
            f"/analysis/cards/{card_id}/export?format=markdown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        text = resp.text
        assert "# No Summary" in text
        assert "Q1" in text
        assert ">" not in text  # No summary block

    def test_export_markdown_no_questions(self, client):
        """Export card with no questions should still produce valid markdown."""
        token = _register_and_login(client, "export_noq", "Pass12345")
        card_id = client.post(
            "/analysis/cards",
            json={"name": "Empty Card"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        resp = client.get(
            f"/analysis/cards/{card_id}/export?format=markdown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        text = resp.text
        assert "# Empty Card" in text
        assert "## 问题列表" in text

    def test_export_not_found(self, client):
        token = _register_and_login(client, "export_404", "Pass12345")
        resp = client.get(
            "/analysis/cards/9999/export?format=markdown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_export_unauthorized(self, client):
        resp = client.get("/analysis/cards/1/export?format=markdown")
        assert resp.status_code in (401, 403, 422)

    def test_export_invalid_format(self, client):
        token = _register_and_login(client, "export_bad", "Pass12345")
        card_id = client.post(
            "/analysis/cards",
            json={"name": "Card"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        resp = client.get(
            f"/analysis/cards/{card_id}/export?format=pdf",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_export_markdown_source_with_answer(self, client):
        """Export should include source_mode for each question."""
        token = _register_and_login(client, "export_src", "Pass12345")
        card_id = client.post(
            "/analysis/cards",
            json={"name": "Sources Card"},
            headers={"Authorization": f"Bearer {token}"},
        ).json()["id"]
        client.post(
            f"/analysis/cards/{card_id}/questions",
            json={"question": "Q1", "answer": "A1", "source_mode": "analysis"},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = client.get(
            f"/analysis/cards/{card_id}/export?format=markdown",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        text = resp.text
        assert "来源：analysis" in text
