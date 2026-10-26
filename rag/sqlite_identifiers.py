"""Quoting helpers for individual SQLite identifiers."""

from __future__ import annotations


def quote_sqlite_identifier(value: str) -> str:
    """Return one SQLite identifier quoted with double quotes.

    SQL values must continue to use bound parameters; this helper is only for
    a single table or column name.
    """
    if not isinstance(value, str):
        raise TypeError("SQLite identifier must be a string")
    if "\x00" in value:
        raise ValueError("SQLite identifier must not contain NUL")
    return '"' + value.replace('"', '""') + '"'
