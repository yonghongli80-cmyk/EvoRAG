import sqlite3

from rag.citations import CitationRecord
from rag.user_db import UserDB
from rag.wiki.chunker import MarkdownASTChunker
from rag.wiki.store import get_wiki_store


def _seed_database(path):
    db = UserDB(str(path))
    user_id = db.create_user("legacy-citation-owner", "password")
    conversation_id = db.create_conversation(user_id, "chat")
    db.add_message(conversation_id, "user", "question")
    assistant_id = db.add_message(conversation_id, "assistant", "answer")
    return db, user_id, conversation_id, assistant_id


def _rebuild_answer_citations(path, assistant_id, schema_shape):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("DROP TABLE IF EXISTS citation_domain_migrations")
    connection.execute("ALTER TABLE answer_citations RENAME TO citations_saved")
    source_columns = ", source_kind TEXT NOT NULL DEFAULT 'document', kb_id TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL DEFAULT ''" if schema_shape == "first_round" else ""
    connection.execute(
        "CREATE TABLE answer_citations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, assistant_message_id INTEGER NOT NULL, "
        "document_id INTEGER NOT NULL, document_version_id INTEGER NOT NULL, "
        "chunk_index INTEGER NOT NULL, section_title TEXT NOT NULL, text_preview TEXT NOT NULL, "
        "rank INTEGER NOT NULL, is_historical INTEGER NOT NULL DEFAULT 0, "
        "state TEXT NOT NULL DEFAULT 'available', created_at REAL NOT NULL DEFAULT 0"
        f"{source_columns})"
    )
    connection.execute(
        "INSERT INTO answer_citations(assistant_message_id, document_id, document_version_id, "
        "chunk_index, section_title, text_preview, rank, is_historical) "
        "VALUES(?, 41, 61, 2, 'Heading', 'old preview', 0, 0)",
        (assistant_id,),
    )
    if schema_shape == "first_round":
        connection.execute(
            "INSERT INTO answer_citations(assistant_message_id, document_id, "
            "document_version_id, chunk_index, section_title, text_preview, rank, "
            "is_historical, source_kind, kb_id, source_name) "
            "VALUES(?, 71, 81, 0, '', 'current explicit citation', 1, 0, "
            "'document', 'kb_known', 'known.pdf')",
            (assistant_id,),
        )
    connection.execute("DROP TABLE citations_saved")
    connection.commit()
    connection.close()


def test_raw_old_schema_and_first_round_schema_migrate_once(tmp_path):
    for shape in ("raw_old", "first_round"):
        path = tmp_path / f"{shape}.db"
        db, user_id, conversation_id, assistant_id = _seed_database(path)
        db.close()
        _rebuild_answer_citations(path, assistant_id, shape)

        migrated = UserDB(str(path))
        try:
            citation = migrated.get_answer_citations(assistant_id)[0]
            assert citation["source_kind"] == "legacy"
            assert not citation["kb_id"]
            assert not citation["source_name"]
            if shape == "first_round":
                preserved = migrated.get_answer_citations(assistant_id)[1]
                assert preserved["source_kind"] == "document"
                assert preserved["kb_id"] == "kb_known"
                assert preserved["source_name"] == "known.pdf"
            explicit_message_id = migrated.persist_assistant_answer_with_citations(
                conversation_id,
                "explicit document",
                [CitationRecord(7, 8, 0, "", "ordinary", 0, False, source_kind="document")],
                f"explicit-{shape}",
            )
        finally:
            migrated.close()

        reopened = UserDB(str(path))
        try:
            assert reopened.get_answer_citations(assistant_id)[0]["source_kind"] == "legacy"
            assert reopened.get_answer_citations(explicit_message_id)[0]["source_kind"] == "document"
            with reopened.locked_read() as conn:
                assert conn.execute(
                    "SELECT COUNT(*) FROM citation_domain_migrations WHERE name=?",
                    ("legacy-source-domain-v1",),
                ).fetchone()[0] == 1
        finally:
            reopened.close()


def test_exact_legacy_wiki_is_recovered_only_without_document_candidate(
    isolated_api, make_user
):
    from rag.citation_resolution import resolve_legacy_citation

    owner, _headers = make_user("legacy-wiki-recovery")
    db = isolated_api.app.state.user_db
    store = get_wiki_store()
    page = store.create_page("kb_legacy", "Old title", "# Heading\n\nExact historical text")
    revision = store.get_revision(page.current_revision_id)
    historic_page = page.model_copy(
        update={"title": revision.title, "content": revision.content}
    )
    wiki_chunk = MarkdownASTChunker().chunk_page(historic_page)[0]
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    db.add_message(conversation_id, "user", "question")
    db.add_message(conversation_id, "assistant", "answer")
    citation_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [
            CitationRecord(
                page.id,
                revision.id,
                wiki_chunk.chunk_index,
                wiki_chunk.section_path,
                wiki_chunk.text[:100],
                0,
                False,
                source_kind="legacy",
            )
        ],
        "old-wiki-source",
    )
    citation = db.get_answer_citations(citation_id)[0]

    resolved = resolve_legacy_citation(db, citation, store)

    assert resolved["source_kind"] == "wiki"
    assert resolved["kb_id"] == page.kb_id
    assert resolved["source_name"] == revision.title
    assert db.get_answer_citations(citation_id)[0]["source_kind"] == "wiki"
    assert resolve_legacy_citation(db, db.get_answer_citations(citation_id)[0], store)["source_kind"] == "wiki"


def test_legacy_document_requires_exact_document_version_and_nonwiki_preview(
    isolated_api, make_user
):
    from rag.citation_resolution import resolve_legacy_citation

    owner, _headers = make_user("legacy-document-recovery")
    db = isolated_api.app.state.user_db
    document_id = db.create_document_permission("plain.txt", "rag_docs", owner["id"])
    version_id = db.create_document_version(
        document_id, "plain.txt", "unused", "plain-hash", owner["id"]
    )
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    db.add_message(conversation_id, "user", "question")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [
            CitationRecord(
                document_id,
                version_id,
                0,
                "Heading",
                "Ordinary document preview",
                0,
                False,
                source_kind="legacy",
            )
        ],
        "legacy-ordinary-document",
    )

    resolved = resolve_legacy_citation(db, db.get_answer_citations(assistant_id)[0])

    assert resolved["source_kind"] == "document"
    assert resolved["kb_id"] == "rag_docs"
    assert resolved["source_name"] == "plain.txt"


def test_legacy_document_resolution_does_not_open_wiki_store(
    isolated_api, make_user, monkeypatch
):
    import rag.wiki.store
    from rag.citation_resolution import resolve_legacy_citation

    owner, _headers = make_user("legacy-document-no-wiki-store")
    db = isolated_api.app.state.user_db
    document_id = db.create_document_permission("plain.txt", "rag_docs", owner["id"])
    version_id = db.create_document_version(
        document_id, "plain.txt", "unused", "plain-hash-no-wiki", owner["id"]
    )
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    message_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [
            CitationRecord(
                document_id,
                version_id,
                0,
                "",
                "ordinary preview",
                0,
                False,
                source_kind="legacy",
            )
        ],
        "legacy-document-no-wiki-store",
    )
    monkeypatch.setattr(
        rag.wiki.store,
        "get_wiki_store",
        lambda: (_ for _ in ()).throw(AssertionError("Wiki store should stay unopened")),
    )

    resolved = resolve_legacy_citation(db, db.get_answer_citations(message_id)[0])

    assert resolved["source_kind"] == "document"


def test_unresolved_legacy_citation_blocks_regeneration_fallback(
    isolated_api, make_user
):
    from fastapi import HTTPException

    from rag.routers.query import _regenerate_target_kb

    owner, _headers = make_user("legacy-regenerate-unresolved")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    message_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [CitationRecord(9999, 9999, 0, "", "[Wiki: removed] text", 0, False, source_kind="legacy")],
        "legacy-regenerate-unresolved",
    )

    try:
        _regenerate_target_kb(db, message_id, {})
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("unresolved history must not fall back to the default KB")


def test_unknown_citation_response_has_no_source_link():
    from rag.citations import citation_response

    response = citation_response(
        object(),
        {
            "id": 1,
            "document_id": 41,
            "document_version_id": 61,
            "source_kind": "legacy",
        },
        {},
    )

    assert "download_url" not in response
    assert "wiki_url" not in response


def test_legacy_wiki_history_and_regenerate_recheck_current_kb_access(
    isolated_api, make_user
):
    from rag.routers.query import _regenerate_target_kb

    owner, headers = make_user("legacy-wiki-history-owner")
    other, _ = make_user("legacy-wiki-history-other")
    db = isolated_api.app.state.user_db
    store = get_wiki_store()
    store.create_page("kb_legacy_history", "Unused", "unused")
    page = store.create_page(
        "kb_legacy_history", "Manual.pdf", "# Intro\n\nHistorically cited body"
    )
    db.create_kb_metadata(page.kb_id, "Legacy Wiki", owner["id"])
    document_id = db.create_document_permission(page.title, "rag_docs", owner["id"])
    db.create_document_version(
        document_id, page.title, "unused", "same-name-doc", owner["id"]
    )
    revision = store.get_revision(page.current_revision_id)
    chunk = MarkdownASTChunker().chunk_page(
        page.model_copy(update={"title": revision.title, "content": revision.content})
    )[0]
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    db.add_message(conversation_id, "user", "question")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [
            CitationRecord(
                page.id,
                revision.id,
                chunk.chunk_index,
                chunk.section_path,
                chunk.text[:100],
                0,
                False,
                source_kind="legacy",
            )
        ],
        "legacy-wiki-history-answer",
    )

    history = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()
    assistant = next(message for message in history if message["id"] == assistant_id)
    assert assistant["sources"][0]["source_kind"] == "wiki"
    assert assistant["sources"][0]["doc_name"] == page.title
    assert _regenerate_target_kb(db, assistant_id, {}) == page.kb_id

    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE kb_metadata SET owner_id=? WHERE kb_id=?",
            (other["id"], page.kb_id),
        )
    revoked = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()
    assert next(message for message in revoked if message["id"] == assistant_id)["sources"] == []

    store.delete_page(page.id)
    deleted = isolated_api.get(
        f"/conversations/{conversation_id}/messages", headers=headers
    ).json()
    assert next(message for message in deleted if message["id"] == assistant_id)["sources"] == []


def test_legacy_wiki_collision_or_deleted_page_stays_unresolved(
    isolated_api, make_user
):
    from rag.citation_resolution import resolve_legacy_citation

    owner, _headers = make_user("legacy-wiki-collision")
    db = isolated_api.app.state.user_db
    page = get_wiki_store().create_page("kb_legacy_collision", "Collision", "body")
    doc_id = db.create_document_permission("collision.pdf", "kb_docs", owner["id"])
    version_id = db.create_document_version(doc_id, "collision.pdf", "unused", "hash", owner["id"])
    # Force legacy document IDs to match the wiki page/revision IDs.
    with db.locked_transaction() as conn:
        conn.execute("UPDATE document_permissions SET id=? WHERE id=?", (page.id, doc_id))
        conn.execute("UPDATE document_versions SET id=? WHERE id=?", (page.current_revision_id, version_id))
    wiki_chunk = MarkdownASTChunker().chunk_page(page)[0]
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    db.add_message(conversation_id, "user", "question")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [
            CitationRecord(
                page.id,
                page.current_revision_id,
                wiki_chunk.chunk_index,
                wiki_chunk.section_path,
                wiki_chunk.text[:100],
                0,
                False,
                source_kind="legacy",
            )
        ],
        "legacy-wiki-collision",
    )
    citation = db.get_answer_citations(assistant_id)[0]
    result = resolve_legacy_citation(db, citation, get_wiki_store())
    assert result["source_kind"] == "legacy"

    # Once safely classified, a subsequently deleted Wiki page remains a Wiki reference and is hidden.
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE answer_citations SET document_id=?, document_version_id=? "
            "WHERE assistant_message_id=?",
            (page.id + 100, page.current_revision_id + 100, assistant_id),
        )
    get_wiki_store().delete_page(page.id)
    assert resolve_legacy_citation(
        db, db.get_answer_citations(assistant_id)[0], get_wiki_store()
    )["source_kind"] == "legacy"


def test_outdated_feedback_rejects_unresolved_legacy_citation(isolated_api, make_user):
    from rag.knowledge_feedback import FeedbackValidationError, submit_knowledge_feedback

    owner, _headers = make_user("legacy-feedback-unresolved")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(owner["id"], "", "kb")
    db.add_message(conversation_id, "user", "question")
    assistant_id = db.add_message(conversation_id, "assistant", "answer")
    assistant_id = db.persist_assistant_answer_with_citations(
        conversation_id,
        "answer",
        [CitationRecord(999, 999, 0, "", "", 0, False, source_kind="legacy")],
        "legacy-feedback",
    )
    citation_id = db.get_answer_citations(assistant_id)[0]["id"]

    try:
        submit_knowledge_feedback(
            db, owner, assistant_id, "outdated", citation_id, "", ""
        )
    except FeedbackValidationError as error:
        assert "unresolved" in str(error) or "document" in str(error)
    else:
        raise AssertionError("unresolved legacy source must not create document outdated feedback")
