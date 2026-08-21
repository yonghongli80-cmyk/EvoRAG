from __future__ import annotations

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def _write_fixture(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_smoke_fixture(root: Path) -> None:
    temporary_root = Path(tempfile.gettempdir()).resolve()
    target = root.resolve()
    if target == temporary_root or not target.is_relative_to(temporary_root):
        raise ValueError("smoke_target_must_be_temporary")
    if target.exists() and any(target.iterdir()):
        raise ValueError("smoke_target_not_empty")

    data_root = target / "data"
    document_path = data_root / "document_versions" / "1" / "1" / "guide.txt"
    knowledge_path = data_root / "kb_files" / "kb-smoke" / "guide.txt"
    dataset_path = data_root / "structured_datasets" / "1" / "dataset.sqlite3"
    upload_path = data_root / "upload" / "readme.txt"
    _write_fixture(document_path, "synthetic document version")
    _write_fixture(knowledge_path, "synthetic knowledge file")
    _write_fixture(dataset_path, "synthetic structured dataset")
    _write_fixture(upload_path, "synthetic upload")

    data_root.mkdir(parents=True, exist_ok=True)
    users_path = data_root / "users.db"
    with closing(sqlite3.connect(users_path)) as database:
        database.execute(
            "CREATE TABLE document_versions(file_path TEXT, status TEXT)"
        )
        database.execute(
            "CREATE TABLE kb_documents(file_path TEXT, status TEXT)"
        )
        database.execute(
            "CREATE TABLE structured_datasets(storage_key TEXT, status TEXT)"
        )
        database.execute(
            "INSERT INTO document_versions VALUES (?, 'current')",
            (str(document_path),),
        )
        database.execute(
            "INSERT INTO kb_documents VALUES (?, 'indexed')",
            (str(knowledge_path),),
        )
        database.execute(
            "INSERT INTO structured_datasets VALUES (?, 'ready')",
            ("1/dataset.sqlite3",),
        )
        database.commit()

    for name in ("analysis", "memory"):
        with closing(sqlite3.connect(data_root / f"{name}.db")) as database:
            database.execute("CREATE TABLE marker(value TEXT NOT NULL)")
            database.execute("INSERT INTO marker VALUES (?)", (f"synthetic-{name}",))
            database.commit()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        sys.stderr.write("usage: create_backup_smoke_fixture.py <temporary-root>\n")
        return 2
    try:
        create_smoke_fixture(Path(arguments[0]))
    except ValueError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
