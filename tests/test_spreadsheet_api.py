from openpyxl import Workbook

from rag.spreadsheets.api import MAX_NON_EDITABLE_CELLS
from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset


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


def test_rows_fall_back_to_configured_storage_root_when_app_state_is_absent(
    isolated_api, make_user, tmp_path
):
    owner, headers = make_user("sheet-storage-fallback-owner")
    document_id, version_id = _seed_sheet(isolated_api, owner, tmp_path)
    del isolated_api.app.state.structured_datasets_root

    response = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}"
        "/sheets/sheet_0001/rows",
        headers=headers,
    )

    assert response.status_code == 200


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
    assert response.json()["merged_ranges"] == []
    assert response.json()["non_editable_cells_truncated"] is False
    assert "storage" not in response.text.lower()
    assert "sqlite" not in response.text.lower()

    outsider, outsider_headers = make_user("sheet-outsider")
    denied = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}/sheets/sheet_0001/rows",
        headers=outsider_headers,
    )
    assert denied.status_code == 403
    assert outsider["id"] != owner["id"]


def _add_merged_range(client, document_id, version_id, range_ref):
    """Write a merge directly into the dataset: openpyxl cannot build whole-grid merges."""
    import sqlite3

    path = resolve_dataset_path(
        client.app.state.structured_datasets_root, document_id, version_id
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT INTO merged_ranges (sheet_key, range_ref) VALUES (?, ?)",
            ("sheet_0001", range_ref),
        )
        connection.commit()
    finally:
        connection.close()


def test_rows_do_not_expand_a_whole_grid_merged_range(
    isolated_api, make_user, tmp_path
):
    owner, headers = make_user("sheet-huge-merge-owner")
    document_id, version_id = _seed_sheet(isolated_api, owner, tmp_path)
    _add_merged_range(isolated_api, document_id, version_id, "A1:XFD1048576")

    response = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}"
        "/sheets/sheet_0001/rows?limit=100",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    # The range is reported instead of being expanded cell by cell; the hard cap
    # keeps the annotation list bounded for a merge covering the entire grid.
    assert body["merged_ranges"] == ["A1:XFD1048576"]
    assert body["non_editable_cells"] == []
    assert body["non_editable_cells_truncated"] is True
    assert len(body["non_editable_cells"]) <= MAX_NON_EDITABLE_CELLS


def test_rows_annotate_only_cells_of_the_returned_page(
    isolated_api, make_user, tmp_path
):
    owner, headers = make_user("sheet-page-merge-owner")
    document_id, version_id = _seed_sheet(isolated_api, owner, tmp_path)
    _add_merged_range(isolated_api, document_id, version_id, "A1:B1")
    _add_merged_range(isolated_api, document_id, version_id, "A2:B2")

    body = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}"
        "/sheets/sheet_0001/rows?limit=100",
        headers=headers,
    ).json()

    # Row 1 is outside the returned data page, so its merge is reported neither
    # as an annotation nor as a page range.  Row 2 is on the page: only the
    # non-anchor B2 cell is non-editable.
    assert body["merged_ranges"] == ["A2:B2"]
    assert body["non_editable_cells"] == ["B2"]
    assert body["non_editable_cells_truncated"] is False


def test_rows_treat_page_cells_of_a_header_merge_as_non_editable(
    isolated_api, make_user, tmp_path
):
    owner, headers = make_user("sheet-span-merge-owner")
    document_id, version_id = _seed_sheet(isolated_api, owner, tmp_path)
    _add_merged_range(isolated_api, document_id, version_id, "A1:B2")

    body = isolated_api.get(
        f"/documents/{document_id}/versions/{version_id}"
        "/sheets/sheet_0001/rows?limit=100",
        headers=headers,
    ).json()

    # The merge anchor (A1) is above the page, so every page cell in the range
    # is read-only.
    assert body["merged_ranges"] == ["A1:B2"]
    assert body["non_editable_cells"] == ["A2", "B2"]
    assert body["non_editable_cells_truncated"] is False
