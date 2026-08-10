"""Read-only spreadsheet viewer routes."""
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request

from rag.api_security import require_doc_view
from rag.auth import decode_token

router = APIRouter()

@router.get("/documents/{document_id}/versions/{version_id}/sheets/{sheet_key}/rows")
def rows(document_id: int, version_id: int, sheet_key: str, request: Request, authorization: str | None = Header(default=None), after_row: int = 0, limit: int = Query(default=100, ge=1, le=500)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未提供认证信息")
    db = request.app.state.user_db
    payload = decode_token(authorization[7:])
    user = db.get_user_by_id(int(payload["user_id"])) if payload else None
    if user is None:
        raise HTTPException(401, "token 无效或已过期")
    document = db.get_document_permission_by_id(document_id)
    if document is None:
        raise HTTPException(404, "document not found")
    require_doc_view(db, document["doc_name"], document["kb_id"], user)
    version = db.get_document_version_by_id(version_id)
    dataset = db.get_structured_dataset_for_version(version_id)
    if version is None or dataset is None or version["document_id"] != document_id or dataset["document_id"] != document_id or dataset["status"] != "ready":
        raise HTTPException(404, "spreadsheet version not found")
    root = Path(request.app.state.structured_datasets_root)
    path = (root / dataset["storage_key"]).resolve()
    if root.resolve() not in path.parents:
        raise HTTPException(404, "spreadsheet version not found")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        sheet = connection.execute("SELECT table_name FROM sheet_manifest WHERE sheet_key = ?", (sheet_key,)).fetchone()
        columns = connection.execute("SELECT column_key FROM column_manifest WHERE sheet_key = ? ORDER BY ordinal", (sheet_key,)).fetchall()
        if sheet is None:
            raise HTTPException(404, "sheet not found")
        keys = [row[0] for row in columns]
        quoted = ", ".join(f'"{key}"' for key in keys)
        result = connection.execute(f'SELECT __row_number, {quoted} FROM "{sheet[0]}" WHERE __row_number > ? ORDER BY __row_number LIMIT ?', (after_row, limit)).fetchall()
        return {"rows": [{"row_number": row[0], "values": list(row[1:])} for row in result]}
    finally:
        connection.close()
