import io
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import UploadFile


@pytest.mark.asyncio
async def test_add_document_to_kb_persists_uploaded_file(tmp_path, monkeypatch):
    import rag.api as api

    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    metadata_db = tmp_path / "metadata.db"
    with sqlite3.connect(metadata_db) as conn:
        conn.execute(
            "CREATE TABLE kb_documents ("
            "kb_id TEXT, filename TEXT, file_path TEXT, toc TEXT, summary TEXT, "
            "chunk_count INTEGER, status TEXT, PRIMARY KEY (kb_id, filename))"
        )
        conn.execute(
            "CREATE TABLE kb_metadata (kb_id TEXT PRIMARY KEY, name TEXT)"
        )

    user_db = MagicMock()
    user_db.create_document_permission.return_value = 7
    monkeypatch.setattr(api, "DATA_DIR", upload_dir)
    monkeypatch.setattr(api, "_DB_PATH", metadata_db)
    monkeypatch.setattr(api, "user_db", user_db)
    file = UploadFile(filename="guide.txt", file=io.BytesIO(b"persist me"))

    with patch.object(api, "_require_auth", new=AsyncMock(return_value={"id": 1})), patch.object(
        api, "_check_kb_edit_permission"
    ), patch.object(api, "KnowledgeBaseManager") as manager_cls, patch.object(
        api, "load_document", return_value="persist me"
    ), patch.object(api, "generate_toc", return_value=[]), patch.object(
        api, "generate_summary", return_value="summary"
    ):
        manager_cls.return_value.add_document.return_value = 1
        result = await api.add_document_to_kb("kb_test", file, authorization="Bearer token")

    stored = upload_dir / "guide.txt"
    assert result["status"] == "added"
    assert stored.read_bytes() == b"persist me"
    with sqlite3.connect(metadata_db) as conn:
        row = conn.execute(
            "SELECT file_path, toc, summary FROM kb_documents WHERE kb_id = ? AND filename = ?",
            ("kb_test", "guide.txt"),
        ).fetchone()
    assert row == (str(stored), "[]", "summary")
