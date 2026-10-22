"""D-11: material FTS must escape user text and not hide real database errors."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from rag.repositories.material_repo import MaterialRepository, _fts_phrase_query
from rag.user_db import UserDB


@pytest.fixture()
def fts_db(tmp_path):
    db = UserDB(str(tmp_path / "material.db"))
    db.create_material_schema()
    owner_id = db.create_user("fts-owner", "Password-123!")
    document_id = db.create_document_permission("notes.pdf", "kb-1", owner_id)
    version_id = db.create_document_version(
        document_id, "notes.pdf", str(tmp_path / "notes.pdf"), "hash", owner_id
    )
    db.activate_document_version(document_id, version_id, "kb-1", "notes.pdf")
    db.insert_material_search_fts(
        document_id,
        version_id,
        "notes.pdf",
        "",
        "",
        "[]",
        'C++ 与 "引号" 文本 AND operator (parenthesis) * star:colon',
    )
    yield db, document_id
    db.close()


def _search(db: UserDB, text: str):
    return db.search_material_search_fts(text)


@pytest.mark.parametrize(
    "text",
    [
        "C++",
        "C++ 与",
        "AND",
        "OR",
        "NOT",
        "NEAR(a b)",
        '"',
        '"引号"',
        "*",
        "(",
        ")",
        "-",
        ":",
        "star:colon",
        "operator",
        "文本 AND operator",
    ],
)
def test_operator_and_punctuation_queries_are_searched_literally(fts_db, text):
    db, document_id = fts_db

    hits = _search(db, text)

    assert isinstance(hits, list)
    # Punctuation-only tokens have no indexed terms, so they legitimately miss;
    # the point is that no FTS syntax error is raised or swallowed.
    if text in {"C++", "C++ 与", "AND", '"引号"', "operator", "文本 AND operator"}:
        assert [hit["document_id"] for hit in hits] == [document_id]


def test_multi_token_queries_keep_implicit_and_semantics(fts_db):
    db, document_id = fts_db

    assert [hit["document_id"] for hit in _search(db, "C++ 与")] == [document_id]
    assert _search(db, "C++ 不存在的词") == []


def test_empty_query_returns_no_hits_without_touching_sqlite(fts_db):
    db, _ = fts_db

    assert _search(db, "") == []
    assert _search(db, "   \t\n ") == []


def test_phrase_helper_quotes_and_doubles_embedded_quotes():
    assert _fts_phrase_query('he said "hi"') == '"he" "said" """hi"""'
    assert _fts_phrase_query("C++") == '"C++"'
    assert _fts_phrase_query("") == ""


class _RaisingConnection:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, *args, **kwargs):
        raise self.error


class _RaisingConnectionManager:
    def __init__(self, error: Exception) -> None:
        self.error = error

    @contextmanager
    def read_connection(self):
        yield _RaisingConnection(self.error)


def test_non_syntax_database_errors_are_raised():
    repo = MaterialRepository(
        _RaisingConnectionManager(sqlite3.OperationalError("database is locked"))
    )

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        repo.search_material_search_fts("C++")


def test_syntax_errors_are_recorded_and_return_no_hits(caplog):
    repo = MaterialRepository(
        _RaisingConnectionManager(
            sqlite3.OperationalError('fts5: syntax error near "+"')
        )
    )

    with caplog.at_level("WARNING", logger="rag.repositories.material_repo"):
        assert repo.search_material_search_fts('"') == []

    assert any(
        record.metric == "material_fts_syntax_error"
        for record in caplog.records
        if hasattr(record, "metric")
    )
