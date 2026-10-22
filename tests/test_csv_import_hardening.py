"""D-12: CSV must be parsed by ``csv.reader`` and neutralized on import."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rag.spreadsheets.parser import parse_spreadsheet


def _write_csv(tmp_path, text: str, name: str = "import.csv"):
    path = tmp_path / name
    # ``write_bytes`` keeps the literal newlines: ``write_text`` applies
    # platform line-ending translation and would mask quoted-newline handling.
    path.write_bytes(text.encode())
    return path


def test_quoted_newline_stays_inside_one_record(tmp_path):
    source = _write_csv(
        tmp_path,
        'name,note\n张三,"第一行\n第二行"\n李四,ok\n',
    )

    parsed = parse_spreadsheet(source, "csv", None)
    sheet = parsed.sheets[0]

    # ``str.splitlines()`` used to turn the quoted field into three records,
    # shifting 李四 onto a phantom row and duplicating the header width.
    assert [row.row_number for row in sheet.rows] == [2, 3]
    assert sheet.rows[0].cells[0].value == "张三"
    assert sheet.rows[0].cells[1].value == "第一行\n第二行"
    assert sheet.rows[1].cells[0].value == "李四"
    assert sheet.rows[1].cells[1].value == "ok"


def test_quoted_carriage_return_newline_is_preserved(tmp_path):
    path = tmp_path / "crlf.csv"
    path.write_bytes('name,note\r\n张三,"第一行\r\n第二行"\r\n'.encode())

    sheet = parse_spreadsheet(path, "csv", None).sheets[0]

    assert len(sheet.rows) == 1
    assert sheet.rows[0].cells[1].value == "第一行\r\n第二行"


@pytest.mark.parametrize(
    "payload",
    ["=1+1", "+SUM(A1)", "-2+3", "@SUM(A1)", "\t=cmd", "\r=cmd"],
)
def test_text_cells_starting_with_formula_characters_are_neutralized(
    tmp_path, payload
):
    source = _write_csv(tmp_path, "value\n" + f'"{payload}"\n')

    cell = parse_spreadsheet(source, "csv", None).sheets[0].rows[0].cells[0]

    assert cell.value == f"'{payload}"
    assert not str(cell.value).startswith(("=", "+", "-", "@", "\t", "\r"))


def test_numeric_cells_keep_their_numeric_type(tmp_path):
    source = _write_csv(tmp_path, "value\n-1\n+2\n-1.50\n+3.5\n")

    values = [
        row.cells[0].value
        for row in parse_spreadsheet(source, "csv", None).sheets[0].rows
    ]

    assert values == [-1, 2, Decimal("-1.50"), Decimal("3.5")]


def test_plain_text_is_not_rewritten(tmp_path):
    source = _write_csv(tmp_path, "value\nhello\nC++\n")

    values = [
        row.cells[0].value
        for row in parse_spreadsheet(source, "csv", None).sheets[0].rows
    ]

    assert values == ["hello", "C++"]
