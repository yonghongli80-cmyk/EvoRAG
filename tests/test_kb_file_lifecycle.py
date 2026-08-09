from pathlib import Path

import pytest

from rag.kb_file_store import (
    final_document_path,
    promote_stage,
    resolve_stored_path,
    stage_upload,
)


def test_kb_document_path_contains_kb_and_database_document_id(tmp_path):
    stage = stage_upload(tmp_path, "report.pdf", b"content")
    destination = final_document_path(tmp_path, "kb_finance", 42, "report.pdf")

    result = promote_stage(stage, destination)

    assert result == tmp_path / "kb_finance" / "42" / "report.pdf"
    assert result.read_bytes() == b"content"
    assert not stage.exists()


def test_final_path_rejects_traversal_components(tmp_path):
    with pytest.raises(ValueError):
        final_document_path(tmp_path, "../other", 2, "report.pdf")


def test_stored_path_outside_kb_root_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(ValueError):
        resolve_stored_path(tmp_path, str(outside))


def test_kb_upload_record_points_to_existing_file_after_request(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    monkeypatch.setattr("rag.api.load_document", lambda path: "section body")
    monkeypatch.setattr("rag.api.generate_toc", lambda text: [{"title": "section"}])
    monkeypatch.setattr("rag.api.generate_summary", lambda text: "summary")
    monkeypatch.setattr("rag.knowledge_base.KnowledgeBaseManager.add_document", lambda *args, **kwargs: 3)

    response = isolated_api.post(
        "/knowledge-bases/kb_ops/documents",
        headers=headers,
        files={"file": ("report.txt", b"section body", "text/plain")},
    )

    assert response.status_code == 200
    record = db.get_kb_document("kb_ops", "report.txt")
    assert record["status"] == "indexed"
    assert Path(record["file_path"]).read_text(encoding="utf-8") == "section body"


def test_index_failure_removes_stage_permission_and_pending_record(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    monkeypatch.setattr("rag.api.load_document", lambda path: "body")
    monkeypatch.setattr(
        "rag.knowledge_base.KnowledgeBaseManager.add_document",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("qdrant unavailable")),
    )

    response = isolated_api.post(
        "/knowledge-bases/kb_ops/documents",
        headers=headers,
        files={"file": ("report.txt", b"body", "text/plain")},
    )

    assert response.status_code == 400
    assert db.get_kb_document("kb_ops", "report.txt") is None
    assert db.get_document_permission("report.txt", "kb_ops") is None
    assert list(isolated_api.app.state.kb_files_root.rglob("*.*")) == []


def test_kb_delete_removes_document_files_before_collection_metadata(
    isolated_api, make_user, monkeypatch
):
    import rag.api as api

    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    document_id = db.create_kb_document_pending("kb_ops", "report.txt")
    stored = isolated_api.app.state.kb_files_root / "kb_ops" / str(document_id) / "report.txt"
    stored.parent.mkdir(parents=True)
    stored.write_text("body", encoding="utf-8")
    db.finish_kb_document(document_id, str(stored), "[]", "summary", 1)
    db.create_document_permission("report.txt", "kb_ops", owner["id"])
    calls = []

    class FakeManager:
        def remove_document(self, kb_id, filename):
            calls.append(("document", kb_id, filename))

        def delete_kb(self, kb_id):
            calls.append(("collection", kb_id))

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)

    response = isolated_api.delete("/knowledge-bases/kb_ops", headers=headers)

    assert response.status_code == 200
    assert calls == [
        ("document", "kb_ops", "report.txt"),
        ("collection", "kb_ops"),
    ]
    assert not stored.exists()
    assert db.get_kb_document("kb_ops", "report.txt") is None
    assert db.get_kb_metadata("kb_ops") is None


def test_kb_detail_never_exposes_stored_source_path(isolated_api, make_user, monkeypatch):
    import rag.api as api
    from rag.knowledge_base import KnowledgeBaseInfo

    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_ops", "Ops", owner["id"], "private")
    document_id = db.create_kb_document_pending("kb_ops", "report.txt")
    db.finish_kb_document(
        document_id,
        r"C:\internal\knowledge-base\report.txt",
        "[]",
        "summary",
        1,
    )

    class FakeManager:
        def list_kbs(self):
            return [KnowledgeBaseInfo("kb_ops", "Ops", 1, "")]

    monkeypatch.setattr(api, "KnowledgeBaseManager", FakeManager)
    response = isolated_api.get("/knowledge-bases/kb_ops", headers=headers)

    assert response.status_code == 200
    document = response.json()["documents"][0]
    assert "file_path" not in document
    assert "last_error" not in document
