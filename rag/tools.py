"""Agent tools — calculate, sql_query, plot_chart, import_data."""

import ast
import csv
import operator
import os
import sqlite3
import uuid

SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


class _CalculationOverflow(ValueError):
    """指数或计算结果超出安全范围时抛出（继承 ValueError，可按 ValueError 捕获）。"""


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPS:
        return SAFE_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPS:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        # DoS 防护 1：指数绝对值超过 1000 直接拒绝，避免 ast.Pow 算出天文数字
        if type(node.op) is ast.Pow and isinstance(right, (int, float)) and abs(right) > 1000:
            raise _CalculationOverflow("指数过大")
        # DoS 防护 2：求值前 O(1) 位数预估（底数位数 × 指数），堵住"大底数字面量 ** 1000"放大
        # （bit_length 是巨整数的内部字段读取，O(1)；int(left) 对字面量即为 int/float）
        if type(node.op) is ast.Pow:
            size_est = None
            try:
                size_est = int(left).bit_length() * abs(right)
            except (TypeError, OverflowError, ValueError):
                # 非 int/float 底数（如 complex/inf/nan）不适用预估，交给求值与下方结果检查
                size_est = None
            if size_est is not None and size_est > 2_000_000:
                raise _CalculationOverflow("结果超出安全范围")
        result = SAFE_OPS[type(node.op)](left, right)
        # DoS 防护 3：对任何二元运算结果做数值上限检查（整数/浮点/无穷大均覆盖）
        if isinstance(result, (int, float)) and abs(result) > 10**6:
            raise _CalculationOverflow("结果超出安全范围")
        return result
    raise ValueError(f"不允许的表达式: {ast.dump(node)}")


def calculate(expression: str) -> str:
    """安全计算数值表达式，只允许四则运算。"""
    tree = ast.parse(expression, mode="eval")
    try:
        result = _eval_node(tree.body)
    except ZeroDivisionError:
        return "错误：除数不能为零"
    except _CalculationOverflow:
        return "计算被拒绝：指数或结果超出安全范围"
    # Round floats to avoid excessive precision
    if isinstance(result, float):
        result = round(result, 2)
    return str(result)


def create_sql_tool(db_path: str):
    """创建绑定到指定数据库的 SQL 查询工具。"""
    # 禁止的 SQL 关键字（防止危险操作）
    _BLOCKED_KEYWORDS = {
        "UNION",
        "ATTACH",
        "DETACH",
        "LOAD_EXTENSION",
        "DROP",
        "DELETE",
        "INSERT",
        "UPDATE",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "REPLACE",
        "PRAGMA",
    }
    _MAX_ROWS = 1000

    def sql_query(sql: str) -> str:
        sql_upper = sql.strip().upper()
        if not sql_upper.startswith("SELECT"):
            raise ValueError("只允许 SELECT 查询，禁止修改数据")
        if ";" in sql.strip():
            raise ValueError("禁止包含分号（防止多语句注入）")
        # 检查危险关键字（按单词匹配，避免误判列名含关键字）
        import re

        tokens = re.findall(r"[A-Z_]+", sql_upper)
        for token in tokens:
            if token in _BLOCKED_KEYWORDS:
                raise ValueError(f"禁止使用 {token} 关键字")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA query_only = ON")  # Hard defense: SQLite refuses all writes
        try:
            cursor = conn.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(_MAX_ROWS + 1)
            if not rows:
                return "查询结果为空"
            truncated = len(rows) > _MAX_ROWS
            rows = rows[:_MAX_ROWS]
            lines = ["\t".join(columns)]
            for row in rows:
                lines.append("\t".join(str(v) for v in row))
            if truncated:
                lines.append(f"... (结果已截断，最多 {_MAX_ROWS} 行)")
            return "\n".join(lines)
        finally:
            conn.close()

    return sql_query


ALLOWED_CHART_TYPES = {"bar", "line", "pie", "scatter"}


def plot_chart(data: dict, chart_type: str, title: str = "", output_dir: str = "data/charts") -> str:
    """生成图表 PNG 文件，返回文件路径。"""
    if chart_type not in ALLOWED_CHART_TYPES:
        raise ValueError(f"不支持的图表类型: {chart_type}，可选: {ALLOWED_CHART_TYPES}")
    labels = data.get("labels", [])
    values = data.get("values", [])
    if not labels or not values:
        raise ValueError("数据为空")
    os.makedirs(output_dir, exist_ok=True)
    filename = f"chart_{uuid.uuid4().hex[:8]}.png"
    filepath = os.path.join(output_dir, filename)

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 中文字体支持
    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "WenQuanYi Micro Hei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(8, 5))
    try:
        if chart_type == "bar":
            ax.bar(labels, values)
        elif chart_type == "line":
            ax.plot(labels, values, marker="o")
        elif chart_type == "pie":
            ax.pie(values, labels=labels, autopct="%1.1f%%")
        elif chart_type == "scatter":
            ax.scatter(range(len(values)), values)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels)
        if title:
            ax.set_title(title)
        fig.tight_layout()
        fig.savefig(filepath, dpi=100)
    finally:
        plt.close(fig)
    return filepath


def import_data(file_path: str, db_path: str) -> str:
    """导入 Excel/CSV 到 SQLite，返回表名。"""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"文件不存在: {file_path}")
    ext = os.path.splitext(file_path)[1].lower()
    import re

    raw_name = os.path.splitext(os.path.basename(file_path))[0]
    table_name = re.sub(r"[^a-zA-Z0-9_一-鿿]", "_", raw_name).strip("_") or "data"
    # 清洗列名防止 SQL 注入
    import re

    _safe_header = re.compile(r"[^a-zA-Z0-9_一-鿿]")

    if ext == ".csv":
        with open(file_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            raw_headers = next(reader)
            headers = [_safe_header.sub("_", h).strip("_") or f"col_{i}" for i, h in enumerate(raw_headers)]
            rows = list(reader)
    elif ext == ".xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(file_path, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        raw_headers = [str(h) for h in next(rows_iter)]
        headers = [_safe_header.sub("_", h).strip("_") or f"col_{i}" for i, h in enumerate(raw_headers)]
        rows = [[str(c) if c is not None else "" for c in row] for row in rows_iter]
        wb.close()
    else:
        raise ValueError(f"不支持的文件格式: {ext}，请使用 .csv 或 .xlsx")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        col_defs = ", ".join(f'"{h}" TEXT' for h in headers)
        conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        conn.execute(f'CREATE TABLE "{table_name}" ({col_defs})')
        placeholders = ", ".join("?" for _ in headers)
        conn.executemany(f'INSERT INTO "{table_name}" VALUES ({placeholders})', rows)
        conn.commit()
    finally:
        conn.close()
    return table_name
