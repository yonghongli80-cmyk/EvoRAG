"""Task 10: material API routes — authorization and safe responses."""

from __future__ import annotations

import io
import zipfile

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
