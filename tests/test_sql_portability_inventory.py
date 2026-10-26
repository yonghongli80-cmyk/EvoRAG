from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from scripts import audit_sql_portability


def _write_fixture_tree(root: Path) -> tuple[Path, Path]:
    rag_root = root / "rag"
    repo_dir = rag_root / "repositories"
    repo_dir.mkdir(parents=True)
    python_source = repo_dir / "sample_repo.py"
    python_source.write_text(
        '''\
class SampleRepository:
    def load(self, conn, item_id):
        conn.execute("SELECT * FROM documents WHERE id = ?", (item_id,))
        conn.execute("PRAGMA " + "journal_mode")
        query = "SELECT * FROM documents"
        conn.execute(query)
        conn.execute(f"SELECT * FROM documents WHERE id = {item_id}")

    def save(self, cm, conn):
        with cm.transaction(immediate=True):
            conn.execute("INSERT OR IGNORE INTO documents(id) VALUES (?)")
        conn.commit()
        conn.rollback()
        cm.write_connection()
        new_id = conn.lastrowid
''',
        encoding="utf-8",
    )
    sql_file = rag_root / "schema.sql"
    sql_file.write_text(
        """-- INSERT OR REPLACE in this comment is not an executable feature.
CREATE TABLE example (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT);
SELECT 'PRAGMA journal_mode' AS note;
BEGIN;
""",
        encoding="utf-8",
    )
    (root / ".env").write_text("SECRET_SHOULD_NOT_BE_READ=sentinel\n", encoding="utf-8")
    (root / "users.db").write_bytes(b"not a database; sentinel")
    return python_source, sql_file


def test_scan_extracts_static_and_literal_concatenated_sql_but_marks_dynamic_sql_unresolved(
    tmp_path: Path,
) -> None:
    python_source, _ = _write_fixture_tree(tmp_path)

    report = audit_sql_portability.scan_tree(tmp_path / "rag")

    entries = report["entries"]
    python_entries = [entry for entry in entries if entry["path"].endswith("sample_repo.py")]
    assert [(entry["line"], entry["sink"]) for entry in python_entries] == [
        (3, "execute"),
        (4, "execute"),
        (11, "execute"),
    ]
    assert [entry["owner"] for entry in python_entries] == [
        "SampleRepository.load",
        "SampleRepository.load",
        "SampleRepository.save",
    ]
    assert python_entries[0]["classification"] == "static"
    assert python_entries[1]["classification"] == "static_concat"
    assert "PRAGMA" in python_entries[1]["features"]
    assert "INSERT_OR_IGNORE" in python_entries[2]["features"]

    unresolved = report["unresolved"]
    assert [(item["path"], item["line"]) for item in unresolved] == [
        (python_source.relative_to(tmp_path / "rag").as_posix(), 6),
        (python_source.relative_to(tmp_path / "rag").as_posix(), 7),
    ]
    assert all(item["owner"] == "SampleRepository.load" for item in unresolved)
    assert report["coverage"]["unresolved_count"] == 2
    assert report["coverage"]["complete"] is False


def test_scan_records_sql_file_and_transaction_markers_with_owners_and_source_lines(
    tmp_path: Path,
) -> None:
    _write_fixture_tree(tmp_path)

    report = audit_sql_portability.scan_tree(tmp_path / "rag")

    sql_entries = [entry for entry in report["entries"] if entry["path"] == "schema.sql"]
    sql_entry = next(entry for entry in sql_entries if entry["line"] == 2)
    assert sql_entry["line"] == 2
    assert "AUTOINCREMENT" in sql_entry["features"]
    sql_literal_entry = next(entry for entry in sql_entries if entry["line"] == 3)
    sql_begin_entry = next(entry for entry in sql_entries if entry["line"] == 4)
    assert "INSERT_OR_REPLACE" not in sql_entry["features"]
    assert "PRAGMA" not in sql_literal_entry["features"]
    assert "INSERT_OR_REPLACE" not in sql_literal_entry["features"]
    assert sql_begin_entry["owner"] == "<sql-file>"

    markers = report["transaction_markers"]
    assert [(marker["line"], marker["marker"], marker["owner"]) for marker in markers] == [
        (10, "transaction", "SampleRepository.save"),
        (12, "commit", "SampleRepository.save"),
        (13, "rollback", "SampleRepository.save"),
        (14, "write_connection", "SampleRepository.save"),
        (4, "begin", "<sql-file>"),
    ]
    sql_file_marker = next(marker for marker in markers if marker["owner"] == "<sql-file>")
    assert sql_file_marker["line"] == 4
    assert sql_file_marker["location_kind"] == "sql_file_actual_line"
    assert [
        (marker["line"], marker["marker"], marker["owner"])
        for marker in report["code_markers"]
    ] == [(15, "lastrowid", "SampleRepository.save")]


def test_executescript_scans_each_literal_statement_for_transaction_markers(
    tmp_path: Path,
) -> None:
    rag_root = tmp_path / "rag"
    rag_root.mkdir()
    source = rag_root / "migration.py"
    source.write_text(
        '''\
class Migrator:
    def apply(self, conn):
        conn.executescript("""CREATE TABLE t(id);
-- ROLLBACK; COMMIT;
INSERT INTO t VALUES ('text; BEGIN; COMMIT;');
BEGIN;
COMMIT;""")
''',
        encoding="utf-8",
    )

    report = audit_sql_portability.scan_tree(rag_root)

    assert [
        (
            marker["line"],
            marker["marker"],
            marker["owner"],
            marker["location_kind"],
            marker["sql_relative_line"],
            marker["statement_index"],
        )
        for marker in report["transaction_markers"]
    ] == [
        (3, "begin", "Migrator.apply", "python_expression_start", 4, 3),
        (3, "commit", "Migrator.apply", "python_expression_start", 5, 4),
    ]
    assert report["unresolved"] == []
    assert report["coverage"]["complete"] is True


def test_python_sql_marker_locations_anchor_to_ast_expression_and_keep_sql_offsets(
    tmp_path: Path,
) -> None:
    rag_root = tmp_path / "rag"
    rag_root.mkdir()
    source = rag_root / "line_mapping.py"
    source.write_text(
        "class Migrator:\n"
        "    def apply(self, conn):\n"
        '        conn.executescript("CREATE TABLE t(id);\\nBEGIN;\\nCOMMIT;")\n'
        "        conn.executescript(\n"
        '            "CREATE TABLE u(id);\\n"\n'
        '            + "BEGIN;\\n"\n'
        '            + "COMMIT;"\n'
        "        )\n",
        encoding="utf-8",
    )

    report = audit_sql_portability.scan_tree(rag_root)

    assert [
        (
            marker["line"],
            marker["marker"],
            marker["owner"],
            marker["location_kind"],
            marker["sql_relative_line"],
            marker["statement_index"],
        )
        for marker in report["transaction_markers"]
    ] == [
        (3, "begin", "Migrator.apply", "python_expression_start", 2, 2),
        (3, "commit", "Migrator.apply", "python_expression_start", 3, 3),
        (5, "begin", "Migrator.apply", "python_expression_start", 2, 2),
        (5, "commit", "Migrator.apply", "python_expression_start", 3, 3),
    ]


def test_scan_reports_syntax_errors_without_claiming_complete_coverage(tmp_path: Path) -> None:
    rag_root = tmp_path / "rag"
    rag_root.mkdir()
    (rag_root / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    report = audit_sql_portability.scan_tree(rag_root)

    assert report["errors"] == [
        {
            "path": "broken.py",
            "line": 1,
            "kind": "python_syntax_error",
        }
    ]
    assert report["coverage"]["python_files"] == 1
    assert report["coverage"]["errors_count"] == 1
    assert report["coverage"]["complete"] is False


def test_missing_or_empty_source_root_is_never_reported_as_complete(tmp_path: Path) -> None:
    missing = audit_sql_portability.scan_tree(tmp_path / "missing-rag")
    empty_root = tmp_path / "empty-rag"
    empty_root.mkdir()
    empty = audit_sql_portability.scan_tree(empty_root)

    assert missing["errors"] == [{"path": ".", "kind": "source_root_missing"}]
    assert missing["coverage"]["complete"] is False
    assert empty["errors"] == [{"path": ".", "kind": "empty_source_root"}]
    assert empty["coverage"]["complete"] is False


def test_scanner_parses_source_without_importing_or_executing_it(tmp_path: Path) -> None:
    rag_root = tmp_path / "rag"
    rag_root.mkdir()
    marker = tmp_path / "must-not-exist.txt"
    source = rag_root / "dangerous_module.py"
    source.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "def query(conn):\n"
        "    conn.execute('SELECT 1')\n",
        encoding="utf-8",
    )

    audit_sql_portability.scan_tree(rag_root)

    assert source.exists()
    assert not marker.exists()


def test_scan_is_stable_and_reads_only_python_and_sql_under_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_fixture_tree(tmp_path)
    rag_root = tmp_path / "rag"
    original_import = builtins.__import__
    original_read_text = Path.read_text
    opened_sources: list[Path] = []

    def deny_app_import(name, *args, **kwargs):
        if (
            name == "config"
            or name == "rag"
            or name.startswith("rag.")
            or name == "sqlite3"
            or name.startswith(("psycopg", "psycopg2"))
        ):
            raise AssertionError(f"scanner imported an application/database module: {name}")
        return original_import(name, *args, **kwargs)

    def guarded_read_text(path: Path, *args, **kwargs):
        resolved = path.resolve()
        assert rag_root.resolve() in resolved.parents
        assert resolved.suffix in {".py", ".sql"}
        opened_sources.append(resolved)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_app_import)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    first = audit_sql_portability.scan_tree(rag_root)
    second = audit_sql_portability.scan_tree(rag_root)

    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    assert [entry["path"] for entry in first["entries"]] == sorted(
        entry["path"] for entry in first["entries"]
    )
    assert first["coverage"]["files_scanned"] == 2
    assert all(Path(item["path"]).suffix in {".py", ".sql"} for item in first["entries"])
    assert "sentinel" not in json.dumps(first, ensure_ascii=False)
    assert len(opened_sources) == 4
    assert {path.name for path in opened_sources} == {"sample_repo.py", "schema.sql"}


def test_report_writer_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "inventory.json"
    output.write_text("keep this evidence\n", encoding="utf-8")
    before = output.read_bytes()

    with pytest.raises(FileExistsError):
        audit_sql_portability.write_report({"entries": []}, output)

    assert output.read_bytes() == before


def test_report_writer_emits_deterministic_json_to_a_new_file(tmp_path: Path) -> None:
    output = tmp_path / "inventory.json"
    report = {"z": 1, "entries": [{"path": "b.py"}, {"path": "a.py"}]}

    audit_sql_portability.write_report(report, output)

    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert output.read_text(encoding="utf-8") == json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
