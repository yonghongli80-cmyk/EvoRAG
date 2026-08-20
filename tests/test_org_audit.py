import json

from rag.org_auth.audit import OrgAuditService
from rag.user_db import UserDB


def test_audit_redacts_password_token_and_query_text(tmp_path):
    db = UserDB(str(tmp_path / "audit.db"))
    try:
        service = OrgAuditService(db)
        service.record(1, "login_failed", "user", "1", "denied", "bad_credentials", {"password": "123456", "token": "abc", "query": "正文"})
        event = service.list_events()[0]
        serialized = json.dumps(event, ensure_ascii=False)
        assert "123456" not in serialized
        assert "abc" not in serialized
        assert "正文" not in serialized
        assert event["details"] == {"password": "[REDACTED]", "token": "[REDACTED]", "query": "[REDACTED]"}
    finally:
        db.close()
