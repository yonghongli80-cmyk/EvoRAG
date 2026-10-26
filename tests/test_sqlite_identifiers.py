from __future__ import annotations

import pytest

from rag.sqlite_identifiers import quote_sqlite_identifier


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sheet_0001", '"sheet_0001"'),
        ("select", '"select"'),
        ("two words", '"two words"'),
        ("雪", '"雪"'),
        ("a'b", '"a\'b"'),
        ('a"b', '"a""b"'),
        ("", '""'),
    ],
)
def test_quote_sqlite_identifier_quotes_one_name_without_normalizing(value, expected):
    assert quote_sqlite_identifier(value) == expected


def test_quote_sqlite_identifier_rejects_non_string_without_coercion():
    with pytest.raises(TypeError) as exc_info:
        quote_sqlite_identifier(123)
    assert "123" not in str(exc_info.value)


def test_quote_sqlite_identifier_rejects_nul_without_echoing_input():
    value = "private\x00name"
    with pytest.raises(ValueError) as exc_info:
        quote_sqlite_identifier(value)
    assert value not in str(exc_info.value)
