"""Tests for agent tools."""
import pytest


class TestCalculateTool:
    def test_basic_arithmetic(self):
        from rag.tools import calculate
        assert calculate("2 + 3") == "5"
        assert calculate("10 * 5") == "50"
        assert calculate("100 / 4") == "25.0"
        assert calculate("2 ** 10") == "1024"
        assert calculate("17 % 5") == "2"

    def test_complex_expression(self):
        from rag.tools import calculate
        result = calculate("(1200 - 1068) / 1068 * 100")
        assert "12.36" in result

    def test_rejects_dangerous_code(self):
        from rag.tools import calculate
        with pytest.raises(ValueError):
            calculate("__import__('os').system('rm -rf /')")
        with pytest.raises(ValueError):
            calculate("exec('print(1)')")
        with pytest.raises(ValueError):
            calculate("open('/etc/passwd').read()")

    def test_rejects_function_calls(self):
        from rag.tools import calculate
        with pytest.raises(ValueError):
            calculate("print(42)")


class TestSqlQueryTool:
    def test_select_query(self, tmp_path):
        import sqlite3

        from rag.tools import create_sql_tool
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE sales (product TEXT, amount INTEGER)")
        conn.execute("INSERT INTO sales VALUES ('A', 100)")
        conn.execute("INSERT INTO sales VALUES ('B', 200)")
        conn.commit()
        conn.close()

        tool = create_sql_tool(db_path)
        result = tool("SELECT * FROM sales ORDER BY amount DESC")
        assert "B" in result
        assert "200" in result

    def test_rejects_non_select(self, tmp_path):
        import sqlite3

        from rag.tools import create_sql_tool
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        tool = create_sql_tool(db_path)
        with pytest.raises(ValueError, match="只允许 SELECT"):
            tool("DROP TABLE t")
        with pytest.raises(ValueError, match="只允许 SELECT"):
            tool("DELETE FROM t")
        with pytest.raises(ValueError, match="只允许 SELECT"):
            tool("UPDATE t SET id=1")


class TestPlotChartTool:
    def test_bar_chart(self, tmp_path):
        from rag.tools import plot_chart
        output = plot_chart(
            {"labels": ["A", "B", "C"], "values": [10, 20, 15]},
            chart_type="bar",
            title="Test Chart",
            output_dir=str(tmp_path),
        )
        assert output.endswith(".png")
        import os
        assert os.path.exists(output)

    def test_invalid_chart_type(self, tmp_path):
        from rag.tools import plot_chart
        with pytest.raises(ValueError, match="不支持"):
            plot_chart({"labels": [], "values": []}, chart_type="heatmap", output_dir=str(tmp_path))

    def test_empty_data(self, tmp_path):
        from rag.tools import plot_chart
        with pytest.raises(ValueError, match="数据为空"):
            plot_chart({"labels": [], "values": []}, chart_type="bar", output_dir=str(tmp_path))


class TestImportDataTool:
    def test_import_csv(self, tmp_path):
        import sqlite3

        from rag.tools import import_data
        csv_file = tmp_path / "sales.csv"
        csv_file.write_text("product,amount\nA,100\nB,200\n", encoding="utf-8")
        db_path = str(tmp_path / "analysis.db")

        table_name = import_data(str(csv_file), db_path)
        assert table_name == "sales"

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM sales ORDER BY amount").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][1] == "100"

    def test_import_excel(self, tmp_path):
        import sqlite3

        import openpyxl

        from rag.tools import import_data
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["name", "score"])
        ws.append(["Alice", 95])
        ws.append(["Bob", 87])
        xlsx_file = tmp_path / "grades.xlsx"
        wb.save(str(xlsx_file))
        db_path = str(tmp_path / "analysis.db")

        table_name = import_data(str(xlsx_file), db_path)
        assert table_name == "grades"

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM grades ORDER BY score DESC").fetchall()
        conn.close()
        assert rows[0][0] == "Alice"

    def test_import_nonexistent_file(self, tmp_path):
        from rag.tools import import_data
        with pytest.raises(FileNotFoundError):
            import_data("/nonexistent.csv", str(tmp_path / "db.db"))

    def test_import_unsupported_format(self, tmp_path):
        from rag.tools import import_data
        f = tmp_path / "data.json"
        f.write_text("{}")
        with pytest.raises(ValueError, match="不支持"):
            import_data(str(f), str(tmp_path / "db.db"))
