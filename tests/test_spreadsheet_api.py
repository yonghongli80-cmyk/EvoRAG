from openpyxl import Workbook

from config import settings
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset


def test_production_app_configures_spreadsheet_storage_root():
    import rag.api as api

    assert (
        api.app.state.structured_datasets_root
        == settings.structured_datasets_root
    )


def _seed_sheet(client, owner, tmp_path):
    db = client.app.state.user_db
    document_id = db.create_document_permission("sales.xlsx", "rag_docs", owner["id"])
    version_id = db.create_document_version(document_id, "sales.xlsx", "source.xlsx", "hash", owner["id"])
    dataset_id = db.create_structured_dataset(document_id, version_id, "xlsx", "row_groups")
    source = tmp_path / "sales.xlsx"
    workbook = Workbook()
    workbook.active.append(["region", "amount"])
    workbook.active.append(["east", 0])
    workbook.save(source)
    target = resolve_dataset_path(client.app.state.structured_datasets_root, document_id, version_id)
    write_dataset(target, parse_spreadsheet(source, "xlsx", None))
    db.activate_document_and_dataset(
        document_id=document_id, version_id=version_id, dataset_id=dataset_id,
        kb_id="rag_docs", filename="sales.xlsx",
    )
    return document_id, version_id


def test_rows_are_acl_scoped_and_do_not_expose_storage_path(isolated_api, make_user, tmp_path):
    owner, headers = make_user("sheet-owner")
    document_id, version_id = _seed_sheet(isolated_api, owner, tmp_path)

    response = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}/sheets/sheet_0001/rows?after_row=0&limit=100",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["rows"] == [{"row_number": 2, "values": ["east", 0]}]
    assert response.json()["columns"] == [
        {
            "column_key": "c_0001",
            "ordinal": 1,
            "display_name": "region",
            "value_type": "text",
        },
        {
            "column_key": "c_0002",
            "ordinal": 2,
            "display_name": "amount",
            "value_type": "integer",
        },
    ]
    assert response.json()["can_edit"] is True
    assert response.json()["current_version_id"] == version_id
    assert response.json()["non_editable_cells"] == []
    assert "storage" not in response.text.lower()
    assert "sqlite" not in response.text.lower()

    outsider, outsider_headers = make_user("sheet-outsider")
    denied = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}/sheets/sheet_0001/rows",
        headers=outsider_headers,
    )
    assert denied.status_code == 403
    assert outsider["id"] != owner["id"]
