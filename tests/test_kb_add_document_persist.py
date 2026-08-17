"""Regression coverage for persisted knowledge-base uploads."""

import json
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    workbook.active.append(["name", "amount"])
    workbook.active.append(["camera", 12])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


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


def test_add_spreadsheet_to_kb_immediately_creates_readable_dataset(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("kb-spreadsheet-owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_sheets", "Sheets", owner["id"], "private")
    monkeypatch.setattr("rag.api.generate_toc", lambda text: [])
    monkeypatch.setattr("rag.api.generate_summary", lambda text: "summary")
    monkeypatch.setattr(
        "rag.knowledge_base.KnowledgeBaseManager.add_document",
        lambda *args, **kwargs: 1,
    )

    response = isolated_api.post(
        "/knowledge-bases/kb_sheets/documents",
        headers=headers,
        files={
            "file": (
                "prices.xlsx",
                _xlsx_bytes(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    dataset = db.get_structured_dataset_for_version(body["version_id"])
    assert dataset is not None
    assert dataset["status"] == "ready"

    rows = isolated_api.get(
        f"/documents/{body['document_id']}/versions/{body['version_id']}"
        "/sheets/sheet_0001/rows",
        headers=headers,
    )
    assert rows.status_code == 200, rows.text
    assert rows.json()["rows"] == [{"row_number": 2, "values": ["camera", 12]}]
