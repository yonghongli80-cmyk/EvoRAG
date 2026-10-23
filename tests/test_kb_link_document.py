from types import SimpleNamespace

import rag.api as api


def test_link_document_reindexes_an_existing_source_into_the_target_kb(
    isolated_api, make_user, monkeypatch, tmp_path
):
    admin, headers = make_user("admin", is_admin=True)
    db = api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb_link','目标库')")
    source_doc = db.create_document_permission("source.txt", "rag_docs", admin["id"])
    source_path = tmp_path / "source.txt"
    source_path.write_text("hello", encoding="utf-8")
    source_version = db.create_document_version(
        source_doc, "source.txt", str(source_path), "hash", admin["id"]
    )
    db.activate_document_version(source_doc, source_version, "rag_docs", "source.txt")
    target_path = tmp_path / "target.txt"
    target_path.write_text("hello", encoding="utf-8")
    target_version = 999
    result = SimpleNamespace(
        version_id=target_version,
        document_id=999,
        version_no=1,
        chunks=2,
        filename="source.txt",
    )
    seen = {}

    def fake_ingest(*args, **kwargs):
        seen["args"] = args
        return result

    monkeypatch.setattr("rag.routers.knowledge.ingest_document_version", fake_ingest)
    original_get_version = db.get_document_version_by_id

    def fake_get_version(version_id):
        if version_id == target_version:
            return {"file_path": str(target_path)}
        return original_get_version(version_id)

    monkeypatch.setattr(db, "get_document_version_by_id", fake_get_version)
    monkeypatch.setattr("rag.routers.knowledge.load_document", lambda path: {"text": "hello"})
    monkeypatch.setattr("rag.routers.knowledge.generate_toc", lambda parsed: [])
    monkeypatch.setattr("rag.routers.knowledge.generate_summary", lambda parsed: "summary")

    response = isolated_api.post(
        "/knowledge-bases/kb_link/documents/link",
        headers=headers,
        json={"document_id": source_doc},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "linked"
    assert body["source_document_id"] == source_doc
    assert body["document_id"] == 999
    assert seen["args"][3] == "kb_link"
    with db.locked_read() as conn:
        row = conn.execute(
            "SELECT filename,status,chunk_count FROM kb_documents WHERE kb_id='kb_link'"
        ).fetchone()
    assert tuple(row) == ("source.txt", "indexed", 2)


def test_link_document_rejects_an_unknown_source(isolated_api, make_user):
    _admin, headers = make_user("admin", is_admin=True)
    db = api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb_link2','目标库')")
    response = isolated_api.post(
        "/knowledge-bases/kb_link2/documents/link",
        headers=headers,
        json={"document_id": 987654},
    )
    assert response.status_code == 404
