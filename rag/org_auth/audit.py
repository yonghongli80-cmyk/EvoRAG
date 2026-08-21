from __future__ import annotations

import json

_SECRET_KEYS = {"password", "token", "authorization", "secret", "query", "content", "document_text"}


class OrgAuditService:
    def __init__(self, db):
        self.db = db

    def record(self, actor_id: int | None, action: str, target_type: str, target_id: str,
               result: str, reason: str, details: dict | None = None) -> int:
        safe = {str(k): "[REDACTED]" if str(k).lower() in _SECRET_KEYS else v for k, v in (details or {}).items()}
        with self.db.locked_transaction() as conn:
            cur = conn.execute(
                "INSERT INTO org_audit_events(actor_id,action,target_type,target_id,result,reason,details_json) VALUES(?,?,?,?,?,?,?)",
                (actor_id, action, target_type, str(target_id), result, reason, json.dumps(safe, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def list_events(self, limit: int = 200) -> list[dict]:
        with self.db.locked_read() as conn:
            rows = conn.execute("SELECT * FROM org_audit_events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result
