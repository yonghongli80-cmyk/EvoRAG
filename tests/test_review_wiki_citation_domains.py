"""Wiki citations must retain their source domain across persistence and refresh."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from rag.citations import CitationRecord
from rag.models import Chunk
from rag.pipeline import RAGPipeline
from rag.user_db import UserDB
from rag.wiki.store import get_wiki_store


def _seed_colliding_wiki_answer(db, owner_id, wiki_store):
    db.create_kb_metadata("kb_collision_docs", "Documents", owner_id)
    db.create_kb_metadata("kb_collision_wiki", "Wiki", owner_id)
    document_id = db.create_document_permission(
        "collision.pdf", "kb_collision_docs", owner_id
    )
    document_version_id = db.create_document_version(
        document_id, "collision.pdf", "unused", "collision-hash", owner_id
    )
    page = wiki_store.create_page("kb_collision_wiki", "Collision page", "Wiki text")
    assert (page.id, page.current_revision_id) == (document_id, document_version_id)

    conversation_id = db.create_conversation(owner_id, "", "kb")
    db.add_message(conversation_id, "user", "Where is this from?")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "It is on the wiki.",
        [
            CitationRecord(
                document_id=page.id,
                document_version_id=page.current_revision_id,
                chunk_index=0,
                section_title="",
                text_preview="Wiki text",
                rank=0,
                is_historical=False,
                source_kind="wiki",
                kb_id=page.kb_id,
                source_name=page.title,
            )
        ],
        "wiki-collision-answer",
    )
    return page, conversation_id, assistant_id, document_id


def test_real_database_wiki_collision_does_not_join_document_versions(
    isolated_api, make_user
):
    owner, _headers = make_user("wiki_collision_owner")
    db = isolated_api.app.state.user_db
    wiki_store = get_wiki_store()
    page, _conversation_id, assistant_id, document_id = _seed_colliding_wiki_answer(
        db, owner["id"], wiki_store
    )

    citation = db.get_answer_citations(assistant_id)[0]

    assert page.id == document_id
    assert citation["source_kind"] == "wiki"
    assert citation["kb_id"] == "kb_collision_wiki"
    assert citation["source_name"] == "Collision page"
    assert citation["original_filename"] is None

    from rag.knowledge_feedback import FeedbackValidationError, submit_knowledge_feedback

    with pytest.raises(FeedbackValidationError, match="wiki"):
        submit_knowledge_feedback(
            db,
            owner,
            assistant_id,
            "outdated",
            citation["id"],
            "",
            "",
        )


def test_conversation_refresh_returns_only_currently_visible_wiki_citations(
    isolated_api, make_user
):
    owner, headers = make_user("wiki_history_owner")
    db = isolated_api.app.state.user_db
    wiki_store = get_wiki_store()
    page, conversation_id, assistant_id, _document_id = _seed_colliding_wiki_answer(
        db, owner["id"], wiki_store
    )

    response = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    )
    assistant = next(message for message in response.json() if message["id"] == assistant_id)
    assert assistant["sources"][0]["source_kind"] == "wiki"
    assert assistant["sources"][0]["source_name"] == page.title
    assert assistant["sources"][0]["kb_id"] == page.kb_id

    assert wiki_store.delete_page(page.id)
    response_after_delete = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    )
    assistant_after_delete = next(
        message for message in response_after_delete.json() if message["id"] == assistant_id
    )
    assert assistant_after_delete["sources"] == []


def test_history_hides_wiki_citations_after_kb_access_is_revoked(
    isolated_api, make_user
):
    owner, _ = make_user("wiki_share_owner")
    viewer, viewer_headers = make_user("wiki_share_viewer")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_shared_wiki", "Shared Wiki", owner["id"])
    from rag.org_auth.repository import OrgRepository

    OrgRepository(db).install_schema()
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_units(code, name, kind) VALUES('wiki-org', 'Wiki Org', 'company')"
        )
        conn.execute(
            "INSERT INTO org_people(person_code, user_id, display_name, job_title) "
            "VALUES(?, ?, ?, ?)",
            (f"person-{viewer['id']}", viewer["id"], "Wiki viewer", "viewer"),
        )
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id, org_code, kb_type) "
            "VALUES(?, 'wiki-org', 'organization')",
            ("kb_shared_wiki",),
        )
        conn.execute(
            "INSERT INTO kb_org_grants(kb_id, subject_type, subject_id, role, effect, granted_by) "
            "VALUES(?, 'user', ?, 'read', 'allow', ?)",
            ("kb_shared_wiki", str(viewer["id"]), owner["id"]),
        )
    wiki_store = get_wiki_store()
    page = wiki_store.create_page("kb_shared_wiki", "Shared page", "Shared text")
    conversation_id = db.create_conversation(viewer["id"], "", "kb")
    db.add_message(conversation_id, "user", "Question")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "Answer",
        [
            CitationRecord(
                page.id,
                page.current_revision_id,
                0,
                "",
                "Shared text",
                0,
                False,
                source_kind="wiki",
                kb_id=page.kb_id,
                source_name=page.title,
            )
        ],
        "shared-wiki-answer",
    )

    visible = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=viewer_headers
    ).json()
    assistant = next(message for message in visible if message["id"] == assistant_id)
    assert assistant["sources"]

    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE kb_org_grants SET active=0 WHERE kb_id=? AND subject_type='user' "
            "AND subject_id=? AND role='read'",
            ("kb_shared_wiki", str(viewer["id"])),
        )
    from rag.org_auth.models import OrgRole
    from rag.org_auth.service import OrgAuthorizationService

    assert not OrgAuthorizationService(db).authorize(viewer["id"], "kb_shared_wiki", OrgRole.READ).allowed
    hidden = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=viewer_headers
    ).json()
    assistant_after_revoke = next(
        message for message in hidden if message["id"] == assistant_id
    )
    assert assistant_after_revoke["sources"] == []


def test_regenerate_target_uses_wiki_kb_without_document_id_lookup(
    isolated_api, make_user
):
    from rag.routers.query import _regenerate_target_kb

    owner, _headers = make_user("wiki_regenerate_owner")
    db = isolated_api.app.state.user_db
    wiki_store = get_wiki_store()
    _page, _conversation_id, assistant_id, document_id = _seed_colliding_wiki_answer(
        db, owner["id"], wiki_store
    )

    assert db.get_document_permission_by_id(document_id)["kb_id"] == "kb_collision_docs"
    assert _regenerate_target_kb(db, assistant_id, {}) == "kb_collision_wiki"


def test_document_citation_hash_keeps_legacy_shape():
    from rag.repositories.chat_repo import ChatRepository

    citation = CitationRecord(7, 3, 2, "Heading", "Excerpt", 1, True)
    legacy = hashlib.sha256(
        json.dumps(
            [(7, 3, 2, "Heading", "Excerpt", 1, 1)], ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    assert ChatRepository._citations_hash([citation]) == legacy


def _finalize_test_pipeline():
    with (
        patch("rag.pipeline.load", return_value="text"),
        patch("rag.pipeline.clean_document", return_value=("text", {})),
        patch("rag.pipeline.chunk", return_value=[]),
        patch("rag.pipeline.embed", return_value=[]),
        patch("rag.pipeline.clear"),
        patch("rag.pipeline.add"),
        patch("rag.pipeline.Retriever"),
        patch("rag.pipeline.Reranker"),
    ):
        return RAGPipeline(
            "test.txt",
            session_id="wiki-citation-test",
            memory_db_path=":memory:",
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )


def test_pipeline_sources_keep_wiki_domain_and_name():
    pipeline = _finalize_test_pipeline()
    try:
        chunk = Chunk(
            text="Wiki text",
            doc_name="Wiki title",
            chunk_index=0,
            document_id=1,
            document_version_id=1,
            chunk_kind="wiki",
            metadata={"kb_id": "kb_wiki", "title": "Wiki title"},
        )
        prepared = pipeline._finalize_rag_context(
            "question", "session", [chunk], 5, "kb_wiki", build_bundle=False
        )

        source = prepared["sources"][0]
        assert source["source_kind"] == "wiki"
        assert source["kb_id"] == "kb_wiki"
        assert source["source_name"] == "Wiki title"
    finally:
        pipeline.close()


def test_existing_database_migration_adds_wiki_citation_columns(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = UserDB(str(path))
    legacy.close()

    # Recreate the old table shape from before source-domain columns existed.
    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("ALTER TABLE answer_citations RENAME TO citations_new")
    connection.execute(
        """CREATE TABLE answer_citations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assistant_message_id INTEGER NOT NULL,
            document_id INTEGER NOT NULL,
            document_version_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            section_title TEXT NOT NULL,
            text_preview TEXT NOT NULL,
            rank INTEGER NOT NULL,
            is_historical INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'available',
            created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )"""
    )
    connection.execute("DROP TABLE citations_new")
    connection.commit()
    connection.close()

    migrated = UserDB(str(path))
    try:
        with migrated.cm.read_connection() as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(answer_citations)")
            }
        assert {"source_kind", "kb_id", "source_name"} <= columns
    finally:
        migrated.close()
