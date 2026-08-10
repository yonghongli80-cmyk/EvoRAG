import pytest
from pydantic import ValidationError

from rag.spreadsheets.query import Metric, QueryScope, StructuredQueryPlan, execute_structured_query


def test_plan_schema_rejects_raw_sql():
    with pytest.raises(ValidationError):
        StructuredQueryPlan.model_validate({"dataset_id": 1, "operation": "aggregate", "sql": "SELECT *"})


def test_decimal_average_is_exact(tmp_path):
    db_path = tmp_path / "dataset.sqlite3"
    import sqlite3

    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE sheet_manifest (sheet_key TEXT PRIMARY KEY, table_name TEXT NOT NULL);
        CREATE TABLE column_manifest (sheet_key TEXT, column_key TEXT, display_name TEXT, inferred_type TEXT);
        CREATE TABLE sheet_0001 (__row_number INTEGER, c_0001 TEXT);
        INSERT INTO sheet_manifest VALUES ('sheet_0001', 'sheet_0001');
        INSERT INTO column_manifest VALUES ('sheet_0001', 'c_0001', 'amount', 'decimal');
        INSERT INTO sheet_0001 VALUES (2, '0.1'), (3, '0.2');
        """
    )
    connection.close()
    plan = StructuredQueryPlan(dataset_id=1, operation="aggregate", metrics=(Metric(column_key="c_0001", function="average"),))

    result = execute_structured_query(plan, QueryScope(dataset_paths={1: db_path}, allowed_dataset_ids=frozenset({1})))

    assert result.values[0].canonical == "0.15"
    assert result.citations[0].cell_range == "sheet_0001!c_0001:2-3"
