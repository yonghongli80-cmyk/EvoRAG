"""End-to-end KB lifecycle admission and retryable teardown coverage."""

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException

from rag.api_security import require_kb_edit, require_kb_view
from rag.document_version_store import final_version_path


def test_kb_authorization_checks_lifecycle_after_existing_acl(isolated_api, make_user):
    owner, _headers = make_user("lifecycle-owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_lifecycle_api", "Lifecycle", owner["id"], "private")
    user = db.get_user_by_id(owner["id"])
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO kb_lifecycle(kb_id,state,delete_token,generation,requested_at) "
            "VALUES('kb_lifecycle_api','deleting','token',1,0)"
        )

    with pytest.raises(HTTPException) as denied:
        require_kb_view(db, "kb_lifecycle_api", user)
    assert denied.value.status_code == 409

    with pytest.raises(HTTPException) as denied:
        require_kb_edit(db, "kb_lifecycle_api", user)
    assert denied.value.status_code == 409


def test_failed_kb_delete_keeps_tombstone_and_retry_identity(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("delete-owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_delete_retry_api", "Retry", owner["id"], "private")

    class BrokenManager:
        def delete_kb(self, _kb_id):
            raise RuntimeError("injected vector outage")

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", BrokenManager)
    first = isolated_api.delete("/knowledge-bases/kb_delete_retry_api", headers=headers)
    assert first.status_code == 503
    failed = db.get_kb_lifecycle("kb_delete_retry_api")
    assert failed["state"] == "deleting"
    assert failed["last_error_code"]

    class WorkingManager:
        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", WorkingManager)
    second = isolated_api.delete("/knowledge-bases/kb_delete_retry_api", headers=headers)
    assert second.status_code == 200
    assert db.get_kb_lifecycle("kb_delete_retry_api")["state"] == "deleted"


def test_kb_delete_removes_only_owned_material_staging_and_preserves_history(
    isolated_api, make_user, monkeypatch, tmp_path
):
    owner, headers = make_user("staging-owner")
    db = isolated_api.app.state.user_db
    kb_id = "kb_staging_owner"
    other_kb = "kb_staging_other"
    db.create_kb_metadata(kb_id, "Owner", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Other", owner["id"], "private")
    first = db.create_document_permission("first.txt", kb_id, owner["id"])
    second = db.create_document_permission("second.txt", other_kb, owner["id"])
    versions_root = Path(isolated_api.app.state.document_versions_root)
    kb_hash = hashlib.sha256(kb_id.encode("utf-8")).hexdigest()
    document_stage = versions_root / "staging" / str(first)
    document_stage.mkdir(parents=True, exist_ok=True)
    (document_stage / "pending.txt").write_bytes(b"pending")
    owned = versions_root / "staging" / "kb" / kb_hash / "archive.zip"
    other = versions_root / "staging" / "kb" / hashlib.sha256(other_kb.encode()).hexdigest() / "keep.zip"
    unowned = versions_root / "staging" / "legacy.zip"
    for path in (owned, other, unowned):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"stage")

    class Manager:
        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", Manager)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
    assert response.status_code == 200
    assert not owned.exists()
    assert not document_stage.exists()
    assert other.exists() and unowned.exists()
    assert db.get_document_permission_by_id(second) is not None
    assert db.get_document_permission_by_id(first) is None




def test_kb_delete_removes_material_derived_rows_without_touching_other_kb(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("derived-owner")
    db = isolated_api.app.state.user_db
    kb_id, other_kb = "kb_derived_owner", "kb_derived_other"
    db.create_kb_metadata(kb_id, "Owner", owner["id"], "private")
    db.create_kb_metadata(other_kb, "Other", owner["id"], "private")
    owned = db.create_document_permission("owned.txt", kb_id, owner["id"])
    other = db.create_document_permission("other.txt", other_kb, owner["id"])
    for document_id in (owned, other):
        db.insert_material_search_fts(
            document_id, 101, f"{document_id}.txt", "", "", "[]", "derived body"
        )
    with db.locked_transaction() as conn:
        conn.executemany(
            "INSERT INTO material_unit_fingerprints(document_id,version_id,unit_key,digest) "
            "VALUES(?,?, 'unit','digest')",
            [(owned, 101), (other, 102)],
        )

    class Manager:
        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", Manager)
    response = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
    assert response.status_code == 200
    with db.cm.read_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM material_search_fts WHERE document_id = ?", (owned,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM material_unit_fingerprints WHERE document_id = ?", (owned,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM material_search_fts WHERE document_id = ?", (other,)
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM material_unit_fingerprints WHERE document_id = ?", (other,)
        ).fetchone()[0] == 1


def test_link_uses_source_snapshot_captured_before_target_ingest(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("snapshot-owner")
    db = isolated_api.app.state.user_db
    source_kb, target_kb = "kb_snapshot_source", "kb_snapshot_target"
    db.create_kb_metadata(source_kb, "Source", owner["id"], "private")
    db.create_kb_metadata(target_kb, "Target", owner["id"], "private")
    source_id = db.create_document_permission("source.txt", source_kb, owner["id"])
    versions_root = Path(isolated_api.app.state.document_versions_root)
    first_file = final_version_path(versions_root, source_id, 1, "source.txt")
    first_file.parent.mkdir(parents=True, exist_ok=True)
    first_file.write_text("old snapshot", encoding="utf-8")
    first_version = db.create_document_version(
        source_id, "source.txt", str(first_file), "old-hash", owner["id"]
    )
    db.activate_document_version(source_id, first_version, source_kb, "source.txt")

    import rag.routers.knowledge as knowledge

    original_ingest = knowledge.ingest_document_version
    target_entered = threading.Event()
    release_target = threading.Event()

    def pause_target_ingest(db_arg, manager, source, dest_kb, actor, action, root):
        if dest_kb == target_kb:
            target_entered.set()
            assert release_target.wait(10), "test did not release target ingestion"
        return original_ingest(db_arg, manager, source, dest_kb, actor, action, root)

    class Manager:
        def add_document(self, *_args, **_kwargs):
            return 1

        def remove_document(self, *_args, **_kwargs):
            return None

        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr(knowledge, "ingest_document_version", pause_target_ingest)
    monkeypatch.setattr(knowledge, "KnowledgeBaseManager", Manager)
    monkeypatch.setattr(knowledge, "load_document", lambda path: Path(path).read_text(encoding="utf-8"))

    with ThreadPoolExecutor(max_workers=1) as pool:
        linked = pool.submit(
            isolated_api.post,
            f"/knowledge-bases/{target_kb}/documents/link",
            headers=headers,
            json={"document_id": source_id, "duplicate_action": "keep"},
        )
        assert target_entered.wait(10), "target ingest did not start"
        updated = isolated_api.post(
            f"/knowledge-bases/{source_kb}/documents",
            headers=headers,
            data={"duplicate_action": "overwrite"},
            files={"file": ("source.txt", b"new source", "text/plain")},
        )
        assert updated.status_code == 200
        release_target.set()
        linked_response = linked.result(timeout=20)

    assert linked_response.status_code == 200
    linked_doc = db.get_document_permission("source.txt", target_kb)
    linked_version = db.get_current_document_version(linked_doc["id"])
    assert Path(linked_version["file_path"]).read_text(encoding="utf-8") == "old snapshot"


def test_kb_delete_cancels_only_matching_material_and_wiki_job_scopes(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("scope-owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_scope_other", "Other", owner["id"], "private")
    db.create_kb_metadata("kb_scope_target", "Target", owner["id"], "private")
    other_document = db.create_document_permission("other.txt", "kb_scope_other", owner["id"])
    target_document = db.create_document_permission("target.txt", "kb_scope_target", owner["id"])
    target_material_job = db.enqueue_material_job(target_document, 9001)
    other_material_job = db.enqueue_material_job(other_document, 9002)

    from rag.wiki.api import get_wiki_store
    from rag.wiki.indexer import enqueue_wiki_reindex_job

    store = get_wiki_store()
    target_page = store.create_page("kb_scope_target", "Target page", "body")
    other_page = store.create_page("kb_scope_other", "Other page", "body")
    assert enqueue_wiki_reindex_job(
        db, target_page.id, target_page.current_revision_id, kb_id=target_page.kb_id
    )
    assert enqueue_wiki_reindex_job(
        db, other_page.id, other_page.current_revision_id, kb_id=other_page.kb_id
    )
    wiki_jobs = {
        row["document_id"]: row["id"]
        for row in db._conn.execute(
            "SELECT id, document_id FROM document_index_jobs WHERE job_scope='wiki'"
        ).fetchall()
    }

    class Manager:
        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", Manager)
    response = isolated_api.delete("/knowledge-bases/kb_scope_target", headers=headers)
    assert response.status_code == 200
    assert db.get_index_job(target_material_job)["status"] == "cancelled"
    assert db.get_index_job(other_material_job)["status"] == "queued"
    assert db.get_index_job(wiki_jobs[target_page.id])["status"] == "cancelled"
    assert db.get_index_job(wiki_jobs[other_page.id])["status"] == "queued"


def test_upload_does_not_report_success_when_finish_row_disappears(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("lost-finish-owner")
    db = isolated_api.app.state.user_db
    kb_id = "kb_lost_finish"
    db.create_kb_metadata(kb_id, "Lost finish", owner["id"], "private")

    class Manager:
        def add_document(self, *_args, **_kwargs):
            return 1

    original_finish = db.finish_kb_document

    def drop_then_finish(document_id, *args):
        db.delete_kb_document(document_id)
        return original_finish(document_id, *args)

    monkeypatch.setattr("rag.routers.knowledge.KnowledgeBaseManager", Manager)
    monkeypatch.setattr(db, "finish_kb_document", drop_then_finish)
    monkeypatch.setattr("rag.routers.knowledge.load_document", lambda _path: "body")
    response = isolated_api.post(
        f"/knowledge-bases/{kb_id}/documents",
        headers=headers,
        files={"file": ("report.txt", b"body", "text/plain")},
    )

    assert response.status_code == 400
    assert db.get_kb_document(kb_id, "report.txt") is None


def test_upload_holds_admission_until_directory_and_version_are_committed(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("admission-owner")
    db = isolated_api.app.state.user_db
    kb_id = "kb_upload_admission"
    db.create_kb_metadata(kb_id, "Admission", owner["id"], "private")
    entered = threading.Event()
    release = threading.Event()
    import rag.routers.knowledge as knowledge

    original_ingest = knowledge.ingest_document_version

    def blocked_ingest(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test did not release the upload worker"
        return original_ingest(*args, **kwargs)

    class Manager:
        def add_document(self, *_args, **_kwargs):
            return 1

        def remove_document(self, *_args, **_kwargs):
            return None

        def delete_kb(self, _kb_id):
            return None

    monkeypatch.setattr(knowledge, "ingest_document_version", blocked_ingest)
    monkeypatch.setattr(knowledge, "KnowledgeBaseManager", Manager)
    monkeypatch.setattr(knowledge, "load_document", lambda _path: "body")

    with ThreadPoolExecutor(max_workers=1) as pool:
        upload = pool.submit(
            isolated_api.post,
            f"/knowledge-bases/{kb_id}/documents",
            headers=headers,
            files={"file": ("report.txt", b"body", "text/plain")},
        )
        assert entered.wait(10), "upload worker did not reach ingest"
        rejected_delete = isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers)
        assert rejected_delete.status_code == 409
        release.set()
        uploaded = upload.result(timeout=20)

    assert uploaded.status_code == 200
    assert db.get_kb_document(kb_id, "report.txt")["status"] == "indexed"
    assert isolated_api.delete(f"/knowledge-bases/{kb_id}", headers=headers).status_code == 200
    assert db.get_document_permission("report.txt", kb_id) is None
