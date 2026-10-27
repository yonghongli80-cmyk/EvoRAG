"""Regression tests for the SQL query tool's read-only database opening."""

import sqlite3

import pytest


def test_select_from_missing_database_fails_without_creating_file(tmp_path):
    from rag.tools import create_sql_tool

    db_path = tmp_path / "missing.sqlite3"
    query = create_sql_tool(str(db_path))

    with pytest.raises(sqlite3.OperationalError):
        query("SELECT 1")

    assert not db_path.exists()


def test_missing_table_fails_without_creating_database_file(tmp_path):
    from rag.tools import create_sql_tool

    db_path = tmp_path / "missing-table.sqlite3"
    query = create_sql_tool(str(db_path))

    with pytest.raises(sqlite3.OperationalError):
        query("SELECT * FROM absent_table")

    assert not db_path.exists()


def test_missing_table_in_existing_database_still_fails(tmp_path):
    from rag.tools import create_sql_tool

    db_path = tmp_path / "existing.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.close()

    with pytest.raises(sqlite3.OperationalError):
        create_sql_tool(str(db_path))("SELECT * FROM absent_table")

    assert db_path.exists()


def test_select_reads_a_database_with_uri_special_characters_in_path(tmp_path):
    from rag.tools import create_sql_tool

    db_path = tmp_path / "空 格 # 百分% 数据库.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute("INSERT INTO records VALUES (?)", ("手工预置行",))
        connection.commit()
    finally:
        connection.close()

    result = create_sql_tool(str(db_path))("SELECT value FROM records")

    assert result == "value\n手工预置行"


def test_memory_database_remains_available_for_select():
    from rag.tools import create_sql_tool

    result = create_sql_tool(":memory:")("SELECT 1")

    assert result == "1\n1"


def test_select_tool_still_rejects_semicolons(tmp_path):
    from rag.tools import create_sql_tool

    query = create_sql_tool(str(tmp_path / "unused.sqlite3"))

    with pytest.raises(ValueError, match="分号"):
        query("SELECT 1;")


def test_select_tool_still_rejects_blocked_keywords(tmp_path):
    from rag.tools import create_sql_tool

    query = create_sql_tool(str(tmp_path / "unused.sqlite3"))

    with pytest.raises(ValueError, match="UNION"):
        query("SELECT 1 UNION SELECT 2")


def test_select_tool_still_caps_results_at_1000_rows(tmp_path):
    from rag.tools import create_sql_tool

    db_path = tmp_path / "many-rows.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE numbers (value INTEGER)")
        connection.executemany(
            "INSERT INTO numbers VALUES (?)",
            ((value,) for value in range(1001)),
        )
        connection.commit()
    finally:
        connection.close()

    result = create_sql_tool(str(db_path))("SELECT value FROM numbers ORDER BY value")
    lines = result.splitlines()

    assert len(lines) == 1002
    assert lines[0] == "value"
    assert lines[1] == "0"
    assert lines[1000] == "999"
    assert lines[1001] == "... (结果已截断，最多 1000 行)"
