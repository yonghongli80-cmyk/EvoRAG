from openpyxl import Workbook

from rag.spreadsheets.chunker import DatasetMetadata, chunk_workbook
from rag.spreadsheets.parser import parse_spreadsheet


def test_row_groups_repeat_headers_and_preserve_ranges(tmp_path):
    path = tmp_path / "sales.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.append(["name", "month", "amount"])
    for index in range(120):
        sheet.append([f"person-{index}", "2026-08", index])
    book.save(path)

    chunks = chunk_workbook(
        parse_spreadsheet(path, "xlsx", None),
        DatasetMetadata(dataset_id=9, document_id=7, document_version_id=4, document_name="sales.xlsx"),
    )

    row_groups = [item for item in chunks if item.chunk_kind == "row_group"]
    assert all(item.text.startswith("name\tmonth\tamount") for item in row_groups)
    assert all(item.row_end - item.row_start + 1 <= 50 for item in row_groups)
    assert [(item.row_start, item.row_end) for item in row_groups] == [(2, 51), (52, 101), (102, 121)]
    assert all(item.dataset_id == 9 and item.document_version_id == 4 for item in row_groups)
    assert row_groups[0].sheet_key == "sheet_0001"
    assert row_groups[0].column_keys == ("c_0001", "c_0002", "c_0003")
