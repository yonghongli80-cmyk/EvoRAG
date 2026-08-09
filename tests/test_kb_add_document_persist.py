"""Regression coverage for persisted knowledge-base uploads."""

import json
from pathlib import Path


def test_add_document_to_kb_persists_uploaded_file(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("kb-persist-owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_test", "Test", owner["id"], "private")
    monkeypatch.setattr("rag.api.load_document", lambda path: "persist me")
    monkeypatch.setattr("rag.api.generate_toc", lambda text: [{"title": "Guide"}])
    monkeypatch.setattr("rag.api.generate_summary", lambda text: "summary")
    monkeypatch.setattr(
        "rag.knowledge_base.KnowledgeBaseManager.add_document",
        lambda *args, **kwargs: 1,
    )

    response = isolated_api.post(
        "/knowledge-bases/kb_test/documents",
        headers=headers,
        files={"file": ("guide.txt", b"persist me", "text/plain")},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "added"
    record = db.get_kb_document("kb_test", "guide.txt")
    assert Path(record["file_path"]).read_bytes() == b"persist me"
    assert json.loads(record["toc"]) == [{"title": "Guide"}]
    assert record["summary"] == "summary"
