"""Read-only inventory of SQL and transaction markers in a RAG source tree.

This is a candidate inventory, not a complete SQL parser or a PostgreSQL
compatibility verdict. Python is parsed with :mod:`ast` and never imported or
executed. SQL files are lexed conservatively; quoted strings and comments are
removed before feature matching, while statement splitting only recognizes
semicolons outside the common SQL quote/comment forms.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_SQL_SINKS = {"execute", "executemany", "executescript"}
_TRANSACTION_CALLS = {
    "commit": "commit",
    "rollback": "rollback",
    "transaction": "transaction",
    "read_connection": "read_connection",
    "write_connection": "write_connection",
}
_FEATURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRAGMA", re.compile(r"\bPRAGMA\b", re.IGNORECASE)),
    ("AUTOINCREMENT", re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE)),
    ("INSERT_OR_IGNORE", re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)),
    ("INSERT_OR_REPLACE", re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.IGNORECASE)),
    ("REPLACE_INTO", re.compile(r"\bREPLACE\s+INTO\b", re.IGNORECASE)),
    ("ROWID", re.compile(r"(?<!\w)_ROWID_(?!\w)|\b(?:ROWID|OID)\b", re.IGNORECASE)),
    ("FTS_MATCH", re.compile(r"\bMATCH\b", re.IGNORECASE)),
    ("FTS_VIRTUAL_TABLE", re.compile(r"\bCREATE\s+VIRTUAL\s+TABLE\b", re.IGNORECASE)),
    ("FTS5", re.compile(r"\bFTS5\b", re.IGNORECASE)),
    ("STRFTIME", re.compile(r"\bSTRFTIME\s*\(", re.IGNORECASE)),
    ("JSON_EXTRACT", re.compile(r"\bJSON_EXTRACT\s*\(", re.IGNORECASE)),
    ("LASTROWID", re.compile(r"\bLASTROWID\b", re.IGNORECASE)),
    ("QMARK_PLACEHOLDER", re.compile(r"\?")),
    ("WITHOUT_ROWID", re.compile(r"\bWITHOUT\s+ROWID\b", re.IGNORECASE)),
)
_SQL_TRANSACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("begin", re.compile(r"^\s*BEGIN\b", re.IGNORECASE)),
    ("commit", re.compile(r"^\s*COMMIT\b", re.IGNORECASE)),
    ("rollback", re.compile(r"^\s*ROLLBACK\b", re.IGNORECASE)),
    ("savepoint", re.compile(r"^\s*SAVEPOINT\b", re.IGNORECASE)),
    ("release", re.compile(r"^\s*RELEASE\b", re.IGNORECASE)),
)


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_source_files(root: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    """Return sorted .py/.sql files without following symlinks."""
    found: list[Path] = []
    errors: list[dict[str, Any]] = []

    def on_walk_error(exc: OSError) -> None:
        error_path = Path(exc.filename) if exc.filename else root
        try:
            relative = _relative(error_path, root)
        except ValueError:
            relative = "."
        errors.append(
            {"path": relative, "kind": "directory_read_error", "error_type": type(exc).__name__}
        )

    for directory, directory_names, file_names in os.walk(
        root, followlinks=False, onerror=on_walk_error
    ):
        current = Path(directory)
        kept_directories: list[str] = []
        for name in directory_names:
            child = current / name
            if child.is_symlink():
                errors.append(
                    {"path": _relative(child, root), "kind": "symlink_directory_skipped"}
                )
            else:
                kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in file_names:
            path = current / name
            if path.suffix.lower() not in {".py", ".sql"}:
                continue
            if path.is_symlink():
                errors.append({"path": _relative(path, root), "kind": "symlink_file_skipped"})
                continue
            found.append(path)
    found.sort(key=lambda path: _relative(path, root))
    errors.sort(key=lambda item: (item["path"], item["kind"]))
    return found, errors


def _static_string(node: ast.AST) -> tuple[str | None, str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, "static"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, _ = _static_string(node.left)
        right, _ = _static_string(node.right)
        if left is not None and right is not None:
            return left + right, "static_concat"
        return None, "unsupported_expression"
    if isinstance(node, ast.JoinedStr):
        return None, "dynamic_fstring"
    return None, "dynamic_expression"


def _active_sql(sql: str) -> str:
    """Blank comments and quoted literals/identifiers while preserving lines."""
    output: list[str] = []
    index = 0
    state = "normal"
    quote_end = ""
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if state == "line_comment":
            if char == "\n":
                output.append("\n")
                state = "normal"
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                state = "normal"
                index += 2
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if state == "quoted":
            if char == quote_end:
                if next_char == quote_end and quote_end in {"'", '"', "`"}:
                    output.extend((" ", " "))
                    index += 2
                    continue
                state = "normal"
            output.append("\n" if char == "\n" else " ")
            index += 1
            continue

        if char == "-" and next_char == "-":
            output.extend((" ", " "))
            state = "line_comment"
            index += 2
        elif char == "/" and next_char == "*":
            output.extend((" ", " "))
            state = "block_comment"
            index += 2
        elif char in {"'", '"', "`"}:
            quote_end = char
            output.append(" ")
            state = "quoted"
            index += 1
        elif char == "[":
            quote_end = "]"
            output.append(" ")
            state = "quoted"
            index += 1
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _features(sql: str) -> list[str]:
    active = _active_sql(sql)
    return [name for name, pattern in _FEATURE_PATTERNS if pattern.search(active)]


def _sql_statements(text: str) -> tuple[list[tuple[int, str]], bool]:
    """Split SQL on semicolons outside strings/comments; report unterminated forms."""
    statements: list[tuple[int, str]] = []
    line = 1
    statement_line = 1
    state = "normal"
    quote_end = ""
    current: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if state == "line_comment":
            if char == "\n":
                current.append(char)
                state = "normal"
                line += 1
                if not _active_sql("".join(current)).strip():
                    statement_line = line
            index += 1
            continue
        if state == "block_comment":
            if char == "*" and next_char == "/":
                state = "normal"
                index += 2
            else:
                if char == "\n":
                    line += 1
                index += 1
            continue
        if state == "quoted":
            current.append(char)
            if char == "\n":
                line += 1
            if char == quote_end:
                if next_char == quote_end and quote_end in {"'", '"', "`"}:
                    current.append(next_char)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue
        if char == "-" and next_char == "-":
            current.append(" ")
            state = "line_comment"
            index += 2
        elif char == "/" and next_char == "*":
            current.append(" ")
            state = "block_comment"
            index += 2
        elif char in {"'", '"', "`"}:
            quote_end = char
            current.append(char)
            state = "quoted"
            index += 1
        elif char == "[":
            quote_end = "]"
            current.append(char)
            state = "quoted"
            index += 1
        elif char == ";":
            if _active_sql("".join(current)).strip():
                statements.append((statement_line, "".join(current).strip()))
            current = []
            index += 1
            statement_line = line
        else:
            if char not in " \t\r\n" and not _active_sql("".join(current)).strip():
                statement_line = line
            current.append(char)
            if char == "\n":
                line += 1
                if not _active_sql("".join(current)).strip():
                    statement_line = line
            index += 1
    if state in {"quoted", "block_comment"}:
        return statements, False
    if _active_sql("".join(current)).strip():
        statements.append((statement_line, "".join(current).strip()))
    return statements, True


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


class _PythonInventory(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.owners: list[str] = []
        self.entries: list[dict[str, Any]] = []
        self.unresolved: list[dict[str, Any]] = []
        self.transaction_markers: list[dict[str, Any]] = []
        self.code_markers: list[dict[str, Any]] = []

    @property
    def owner(self) -> str:
        return ".".join(self.owners) if self.owners else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.owners.append(node.name)
        self.generic_visit(node)
        self.owners.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name in _SQL_SINKS:
            sql_node: ast.AST | None = node.args[0] if node.args else None
            if sql_node is None:
                keyword = next(
                    (
                        item.value
                        for item in node.keywords
                        if item.arg in {"sql", "query", "statement"}
                    ),
                    None,
                )
                sql_node = keyword
            if sql_node is None:
                self.unresolved.append(
                    {
                        "path": self.relative_path,
                        "line": node.lineno,
                        "owner": self.owner,
                        "sink": name,
                        "classification": "missing_sql_argument",
                    }
                )
            else:
                sql, classification = _static_string(sql_node)
                if sql is None:
                    self.unresolved.append(
                        {
                            "path": self.relative_path,
                            "line": sql_node.lineno,
                            "owner": self.owner,
                            "sink": name,
                            "classification": classification,
                        }
                    )
                else:
                    self.entries.append(
                        {
                            "path": self.relative_path,
                            "line": sql_node.lineno,
                            "owner": self.owner,
                            "sink": name,
                            "classification": classification,
                            "sql": sql,
                            "features": _features(sql),
                        }
                    )
                    statements, well_formed = _sql_statements(sql)
                    if not well_formed:
                        self.unresolved.append(
                            {
                                "path": self.relative_path,
                                "line": sql_node.lineno,
                                "owner": self.owner,
                                "sink": name,
                                "classification": "sql_lex_error",
                            }
                        )
                    for statement_index, (relative_line, statement) in enumerate(statements, start=1):
                        active_sql = _active_sql(statement)
                        for marker, pattern in _SQL_TRANSACTION_PATTERNS:
                            if pattern.search(active_sql):
                                self.transaction_markers.append(
                                    {
                                        "path": self.relative_path,
                                        "line": sql_node.lineno,
                                        "owner": self.owner,
                                        "marker": marker,
                                        "location_kind": "python_expression_start",
                                        "sql_relative_line": relative_line,
                                        "statement_index": statement_index,
                                    }
                                )
        if name in _TRANSACTION_CALLS:
            self.transaction_markers.append(
                {
                    "path": self.relative_path,
                    "line": node.lineno,
                    "owner": self.owner,
                    "marker": _TRANSACTION_CALLS[name],
                }
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr.lower() == "lastrowid":
            self.code_markers.append(
                {
                    "path": self.relative_path,
                    "line": node.lineno,
                    "owner": self.owner,
                    "marker": "lastrowid",
                }
            )
        self.generic_visit(node)


def scan_tree(root: Path) -> dict[str, Any]:
    """Inventory Python SQL sinks and SQL files beneath an explicit source root.

    Only ``.py`` and ``.sql`` files are opened. The function never imports source
    files, connects to a database, or writes files. Results use paths relative to
    ``root`` so repeated runs in different checkouts remain comparable.
    """
    requested_root = Path(root)
    errors: list[dict[str, Any]] = []
    if not requested_root.exists():
        errors.append({"path": ".", "kind": "source_root_missing"})
        return _report(
            [], [], [], [], errors,
            files_discovered=0,
            files_scanned=0,
            python_files=0,
            sql_files=0,
        )
    if not requested_root.is_dir():
        errors.append({"path": ".", "kind": "source_root_not_directory"})
        return _report(
            [], [], [], [], errors,
            files_discovered=0,
            files_scanned=0,
            python_files=0,
            sql_files=0,
        )
    source_root = requested_root.resolve()
    files, discovery_errors = _iter_source_files(source_root)
    errors.extend(discovery_errors)
    if not files:
        errors.append({"path": ".", "kind": "empty_source_root"})
    entries: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    transaction_markers: list[dict[str, Any]] = []
    code_markers: list[dict[str, Any]] = []
    scanned = 0
    python_count = 0
    sql_count = 0

    for path in files:
        relative = _relative(path, source_root)
        if path.suffix.lower() == ".py":
            python_count += 1
        else:
            sql_count += 1
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(
                {
                    "path": relative,
                    "kind": "source_read_error",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        scanned += 1
        if path.suffix.lower() == ".py":
            try:
                tree = ast.parse(text, filename=relative)
            except SyntaxError as exc:
                errors.append(
                    {
                        "path": relative,
                        "line": exc.lineno or 1,
                        "kind": "python_syntax_error",
                    }
                )
                continue
            visitor = _PythonInventory(relative)
            visitor.visit(tree)
            entries.extend(visitor.entries)
            unresolved.extend(visitor.unresolved)
            transaction_markers.extend(visitor.transaction_markers)
            code_markers.extend(visitor.code_markers)
        else:
            statements, well_formed = _sql_statements(text)
            if not well_formed:
                errors.append(
                    {"path": relative, "line": text.count("\n") + 1, "kind": "sql_lex_error"}
                )
            for line, statement in statements:
                entries.append(
                    {
                        "path": relative,
                        "line": line,
                        "owner": "<sql-file>",
                        "sink": "sql_file",
                        "classification": "static_sql",
                        "sql": statement,
                        "features": _features(statement),
                    }
                )
                active = _active_sql(statement)
                for marker, pattern in _SQL_TRANSACTION_PATTERNS:
                    if pattern.search(active):
                        transaction_markers.append(
                            {
                                "path": relative,
                                "line": line,
                                "owner": "<sql-file>",
                                "marker": marker,
                                "location_kind": "sql_file_actual_line",
                            }
                        )

    return _report(
        entries,
        unresolved,
        transaction_markers,
        code_markers,
        errors,
        files_discovered=len(files),
        files_scanned=scanned,
        python_files=python_count,
        sql_files=sql_count,
    )


def _report(
    entries: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    transaction_markers: list[dict[str, Any]],
    code_markers: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    *,
    files_discovered: int,
    files_scanned: int,
    python_files: int,
    sql_files: int,
) -> dict[str, Any]:
    def entry_key(item: dict[str, Any]) -> tuple[str, int, str, str]:
        return item["path"], item["line"], item["sink"], item.get("sql", "")

    unresolved.sort(key=lambda item: (item["path"], item["line"], item["sink"]))
    entries.sort(key=entry_key)
    transaction_markers.sort(key=lambda item: (item["path"], item["line"], item["marker"]))
    code_markers.sort(key=lambda item: (item["path"], item["line"], item["marker"]))
    errors.sort(key=lambda item: (item["path"], item.get("line", 0), item["kind"]))
    return {
        "schema_version": 1,
        "entries": entries,
        "unresolved": unresolved,
        "transaction_markers": transaction_markers,
        "code_markers": code_markers,
        "errors": errors,
        "coverage": {
            "files_discovered": files_discovered,
            "files_scanned": files_scanned,
            "python_files": python_files,
            "sql_files": sql_files,
            "unresolved_count": len(unresolved),
            "errors_count": len(errors),
            "complete": not unresolved and not errors,
        },
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write stable JSON to a new path; never overwrite an existing artifact."""
    destination = Path(output)
    with destination.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="explicit rag source root to scan")
    parser.add_argument(
        "--output", type=Path, help="new JSON report path; existing files are refused"
    )
    args = parser.parse_args(argv)
    report = scan_tree(args.root)
    if args.output is not None:
        try:
            write_report(report, args.output)
        except OSError as exc:
            print(f"report write failed: {type(exc).__name__}", file=sys.stderr)
            return 2
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if report["coverage"]["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
