import sqlite3
from datetime import date

from openpyxl import Workbook

from rag.spreadsheets.parser import parse_spreadsheet
from rag.spreadsheets.store import resolve_dataset_path, write_dataset


def _parsed_workbook(tmp_path):
    source = tmp_path / "sales.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Sales Data"
    sheet.append(["count", "enabled", "amount", "on", "formula"])
    sheet.append([0, False, "12.50", date(2026, 8, 10), "=A2+1"])
    book.save(source)
    return parse_spreadsheet(source, "xlsx", None)


def test_dataset_path_is_version_isolated(tmp_path):
    assert resolve_dataset_path(tmp_path, 12, 34) == tmp_path / "12" / "34" / "dataset.sqlite3"


def test_store_writes_read_only_internal_tables_and_formula_metadata(tmp_path):
    target = resolve_dataset_path(tmp_path, 12, 34)

    manifest = write_dataset(target, _parsed_workbook(tmp_path))

    assert target.exists()
    assert manifest.sheets[0].table_name == "sheet_0001"
    connection = sqlite3.connect(target)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM sheet_0001").fetchone()[0] == 1
        row = connection.execute(
            "SELECT c_0001, c_0002, c_0003, c_0004 FROM sheet_0001"
        ).fetchone()
        assert row == (0, 0, "12.50", "2026-08-10")
        assert connection.execute(
            "SELECT formula, cached_value FROM formula_cells"
        ).fetchone() == ("=A2+1", None)
    finally:
        connection.close()
