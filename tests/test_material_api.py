"""Task 10: material API routes — authorization and safe responses."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from rag.materials.formats import build_default_format_registry
from rag.materials.worker import MaterialWorker, WorkerServices

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def pptx_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ppt/presentation.xml", "<p:presentation/>")
    return buf.getvalue()


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_upload_returns_queued_version_without_blocking_for_index(isolated_api, make_user):
    _, user_headers = make_user("alice")
    response = isolated_api.post(
        "/materials/upload",
        headers=user_headers,
        files={"file": ("policy.pptx", pptx_bytes(), PPTX_MIME)},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] in {"queued", "waiting_for_capacity"}
    assert body["version_id"]


def test_organization_material_upload_requires_manage_role(isolated_api, make_user):
    _alice, headers = make_user("org-member")
    db = isolated_api.app.state.user_db
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','协作组','team')")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb_org','组织库')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb_org','TEAM','organization')"
        )

    response = isolated_api.post(
        "/materials/upload?kb_id=kb_org",
        headers=headers,
        files={"file": ("policy.txt", b"content", "text/plain")},
    )

    assert response.status_code == 404
    assert db.get_document_permission("policy.txt", "kb_org") is None


def test_unassigned_legacy_owner_cannot_recycle_document(isolated_api, make_user):
    alice, headers = make_user("legacy-owner")
    db = isolated_api.app.state.user_db
    document_id = db.create_document_permission("legacy.txt", "legacy-kb", alice["id"])
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,kb_type) VALUES('legacy-kb','unassigned')"
        )

    response = isolated_api.delete(f"/materials/{document_id}", headers=headers)

    assert response.status_code == 404


def test_organization_manager_can_recycle_seed_owned_document(isolated_api, make_user):
    from rag.auth import create_token

    manager, _headers = make_user("manager")
    seed_owner, _ = make_user("seed-owner")
    db = isolated_api.app.state.user_db
    document_id = db.create_document_permission("seeded.txt", "kb_org", seed_owner["id"])
    with db.locked_transaction() as conn:
        conn.execute("INSERT INTO org_units(code,name,kind) VALUES('TEAM','协作组','team')")
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title) "
            "VALUES('P1',?,'甲','负责人')", (manager["id"],),
        )
        conn.execute("INSERT INTO org_manager_scopes VALUES('P1','TEAM')")
        conn.execute("INSERT INTO kb_metadata(kb_id,name) VALUES('kb_org','组织库')")
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb_org','TEAM','organization')"
        )
    headers = {"Authorization": "Bearer " + create_token({
        "user_id": manager["id"], "username": "manager", "security_version": 1,
    })}

    response = isolated_api.delete(f"/materials/{document_id}", headers=headers)

    assert response.status_code == 200


def test_upload_archive_returns_manifest_preview(isolated_api, make_user):
    _, user_headers = make_user("alice")
    response = isolated_api.post(
        "/materials/upload",
        headers=user_headers,
        files={"file": ("onboarding.zip", zip_bytes({"张三/合同.pdf": b"%PDF-1.4\nA"}), "application/zip")},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "awaiting_archive_confirmation"
    assert body["manifest_id"]


def test_archive_confirm_requires_owner_or_editor(isolated_api, make_user):
    _, user_headers = make_user("alice")
    _, viewer_headers = make_user("viewer")
    upload = isolated_api.post(
        "/materials/upload",
        headers=user_headers,
        files={"file": ("bundle.zip", zip_bytes({"a.pdf": b"%PDF-1.4\nA"}), "application/zip")},
    )
    manifest_id = upload.json()["manifest_id"]
    preview = isolated_api.get(f"/materials/archives/{manifest_id}", headers=user_headers)
    assert preview.status_code == 200
    supported_ids = [
        entry["id"] for entry in preview.json()["entries"] if entry["state"] == "approved"
    ]
    response = isolated_api.post(
        f"/materials/archives/{manifest_id}/confirm",
        headers=viewer_headers,
        json={
            "selected_entry_ids": supported_ids,
            "expected_version": 0,
            "idempotency_key": "x",
        },
    )
    assert response.status_code == 403


def test_location_response_never_contains_physical_path(isolated_api, make_user):
    _, user_headers = make_user("alice")
    response = isolated_api.post(
        "/materials/locate", headers=user_headers, json={"text": "我的合同"}
    )
    assert response.status_code == 200
    assert "file_path" not in response.text
    assert "qdrant" not in response.text.lower()


def test_recycle_list_and_restore(isolated_api, make_user):
    _, user_headers = make_user("alice")
    db = isolated_api.app.state.user_db
    alice = db.get_user_by_username("alice")
    doc_id = db.create_document_permission("contract.pdf", "rag_docs", alice["id"])

    deleted = isolated_api.delete(f"/materials/{doc_id}", headers=user_headers)
    assert deleted.status_code == 200
    listing = isolated_api.get("/materials/recycle", headers=user_headers)
    assert listing.status_code == 200
    assert any(item["document_id"] == doc_id for item in listing.json()["items"])

    restored = isolated_api.post(f"/materials/{doc_id}/restore", headers=user_headers, json={"expected_metadata_version": 2})
    assert restored.status_code == 200


def test_recycle_list_hides_other_users_deleted_documents(isolated_api, make_user):
    alice, alice_headers = make_user("alice")
    _, bob_headers = make_user("bob")
    db = isolated_api.app.state.user_db
    doc_id = db.create_document_permission("private.pdf", "rag_docs", alice["id"])

    assert isolated_api.delete(f"/materials/{doc_id}", headers=alice_headers).status_code == 200
    listing = isolated_api.get("/materials/recycle", headers=bob_headers)

    assert listing.status_code == 200
    assert all(item["document_id"] != doc_id for item in listing.json()["items"])


def test_archive_manifest_is_hidden_from_non_owner(isolated_api, make_user):
    _, alice_headers = make_user("alice")
    _, bob_headers = make_user("bob")
    upload = isolated_api.post(
        "/materials/upload",
        headers=alice_headers,
        files={"file": ("private.zip", zip_bytes({"a.pdf": b"%PDF-1.4\nA"}), "application/zip")},
    )
    manifest_id = upload.json()["manifest_id"]

    response = isolated_api.get(f"/materials/archives/{manifest_id}", headers=bob_headers)

    assert response.status_code == 404


def test_material_job_requires_document_view_permission(isolated_api, make_user):
    alice, _ = make_user("alice")
    _, bob_headers = make_user("bob")
    db = isolated_api.app.state.user_db
    doc_id = db.create_document_permission("private.pdf", "rag_docs", alice["id"])
    job_id = db.enqueue_material_job(doc_id, 0)

    response = isolated_api.get(f"/materials/jobs/{job_id}", headers=bob_headers)

    assert response.status_code == 403


def test_queued_material_is_visible_in_files_listing(isolated_api, make_user):
    _, headers = make_user("alice")
    upload = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("queued.txt", b"queued material", "text/plain")},
    )
    assert upload.status_code == 202

    listing = isolated_api.get("/files", headers=headers)

    assert listing.status_code == 200
    assert [item["name"] for item in listing.json()["files"]] == ["queued.txt"]


def test_staging_material_can_be_downloaded_by_its_owner(isolated_api, make_user):
    """A newly uploaded material can be transferred into a knowledge base before indexing."""
    _, headers = make_user("alice")
    upload = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("pending.txt", b"pending material", "text/plain")},
    )

    assert upload.status_code == 202
    download = isolated_api.get("/files/pending.txt/download", headers=headers)
    assert download.status_code == 200
    assert download.content == b"pending material"


def test_download_can_target_the_new_staging_version_of_a_replaced_material(
    isolated_api, make_user, mock_embedding_indexing
):
    """A KB transfer must not silently substitute the old current file."""
    _, headers = make_user("alice")
    first = isolated_api.post(
        "/upload",
        headers=headers,
        files={"file": ("policy.txt", b"old policy", "text/plain")},
    )
    assert first.status_code == 200
    replacement = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("policy.txt", b"new policy", "text/plain")},
    )
    assert replacement.status_code == 202

    download = isolated_api.get(
        f"/files/policy.txt/download?version_id={replacement.json()['version_id']}",
        headers=headers,
    )

    assert download.status_code == 200
    assert download.content == b"new policy"


def test_uploaded_material_job_activates_its_version(isolated_api, make_user):
    """A queued API upload becomes the current searchable document when consumed."""
    _, headers = make_user("alice")
    upload = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("queued.txt", b"leave policy: five days", "text/plain")},
    )
    assert upload.status_code == 202
    body = upload.json()

    class VectorStore:
        def add_units(self, *_args, **_kwargs):
            pass

        def delete_version(self, *_args, **_kwargs):
            pass

        def mark_current(self, *_args, **_kwargs):
            pass

    worker = MaterialWorker(
        WorkerServices(
            db=isolated_api.app.state.user_db,
            versions_root=Path(isolated_api.app.state.document_versions_root),
            registry=build_default_format_registry(),
            vector_store=VectorStore(),
            embedder=lambda texts: [[0.1] * 1024 for _ in texts],
        ),
        worker_id="test-material-worker",
    )

    results = worker.run(batch=1)

    assert results[0].status == "done"
    assert isolated_api.app.state.user_db.get_index_job(1)["status"] == "done"
    current = isolated_api.app.state.user_db.get_current_document_version(body["document_id"])
    assert current is not None
    assert current["id"] == body["version_id"]


def test_archive_manifest_preview_exposes_the_confirm_cas_version(
    isolated_api, make_user
):
    _, headers = make_user("alice-archive-cas")
    upload = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("cas.zip", zip_bytes({"a.pdf": b"%PDF-1.4\nA"}), "application/zip")},
    )
    manifest_id = upload.json()["manifest_id"]
    preview = isolated_api.get(f"/materials/archives/{manifest_id}", headers=headers).json()

    # The container has no live version until the archive is confirmed.
    assert preview["expected_version"] == 0
    supported_ids = [
        entry["id"] for entry in preview["entries"] if entry["state"] == "approved"
    ]
    confirmed = isolated_api.post(
        f"/materials/archives/{manifest_id}/confirm",
        headers=headers,
        json={
            "selected_entry_ids": supported_ids,
            "expected_version": preview["expected_version"],
            "idempotency_key": "cas-1",
        },
    )
    assert confirmed.status_code == 200, confirmed.text

    # A second upload of the same container sees the confirmed version as the
    # CAS token, so the client never needs to omit the field.
    second = isolated_api.post(
        "/materials/upload",
        headers=headers,
        files={"file": ("cas.zip", zip_bytes({"b.pdf": b"%PDF-1.4\nB"}), "application/zip")},
    )
    second_preview = isolated_api.get(
        f"/materials/archives/{second.json()['manifest_id']}", headers=headers
    ).json()
    assert second_preview["expected_version"] == 1


def test_recycle_list_exposes_metadata_version_for_restore_cas(isolated_api, make_user):
    _, headers = make_user("alice-recycle-cas")
    db = isolated_api.app.state.user_db
    alice = db.get_user_by_username("alice-recycle-cas")
    doc_id = db.create_document_permission("recycle-cas.pdf", "rag_docs", alice["id"])
    assert isolated_api.delete(f"/materials/{doc_id}", headers=headers).status_code == 200

    listing = isolated_api.get("/materials/recycle", headers=headers).json()
    item = next(entry for entry in listing["items"] if entry["document_id"] == doc_id)
    stored = db.get_document_permission_by_id(doc_id)
    assert item["metadata_version"] == stored["metadata_version"] == 2

    restored = isolated_api.post(
        f"/materials/{doc_id}/restore",
        headers=headers,
        json={"expected_metadata_version": item["metadata_version"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["metadata_version"] == item["metadata_version"] + 1

    # The stale token is rejected instead of silently succeeding.
    stale = isolated_api.post(
        f"/materials/{doc_id}/restore",
        headers=headers,
        json={"expected_metadata_version": item["metadata_version"]},
    )
    assert stale.status_code == 409
