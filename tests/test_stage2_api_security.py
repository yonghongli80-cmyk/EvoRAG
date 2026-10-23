"""阶段 2 S1（API/安全 P2）回归用例：B-03 ~ B-08 + A-06。

每条用例对应 `docs/superpowers/plans/2026-10-22-full-codebase-review-findings.md`
中的一条 P2 发现；先失败后修复（TDD）。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from config import settings
from rag.auth import verify_api_key
from rag.batch_importer import BatchImporter
from rag.data_sources.db_source import DBSource
from rag.document_versions import DuplicateDocument
from rag.guard import OutputResult
from rag.repositories.analysis_repo import AnalysisRepository
from rag.repositories.user_repo import UserRepository
from rag.user_db import UserDB

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)
    return path


def _last_audit_event(db: UserDB) -> dict:
    with db.locked_read() as conn:
        row = conn.execute(
            "SELECT actor_id, action, target_type, target_id, result, reason, details_json "
            "FROM org_audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "expected an org_audit_events row"
    event = dict(row)
    event["details"] = json.loads(event.pop("details_json"))
    return event


def _add_org_person(db: UserDB, user_id: int, *, person_code: str = "P-S1", active: int = 1) -> None:
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO org_people(person_code,user_id,display_name,job_title,active) "
            "VALUES(?,?,?,?,?)",
            (person_code, user_id, f"员工{user_id}", "专员", active),
        )


# ---------------------------------------------------------------------------
# B-03: role changes are audited; self-demotion and last-admin demotion are refused
# ---------------------------------------------------------------------------


def test_role_change_is_audited(isolated_api, make_user):
    admin, headers = make_user("s1-root", is_admin=True)
    target, _ = make_user("s1-promoted")
    db = isolated_api.app.state.user_db

    response = isolated_api.put(
        f"/users/{target['id']}/role", json={"is_admin": True}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["is_admin"] is True
    event = _last_audit_event(db)
    assert event["actor_id"] == admin["id"]
    assert event["action"] == "user_role_changed"
    assert event["target_type"] == "user"
    assert event["target_id"] == str(target["id"])
    assert event["result"] == "success"
    assert event["details"]["is_admin"] is True
    assert event["details"]["previous_is_admin"] is False


def test_admin_role_change_upsert_is_still_audited(isolated_api, make_user):
    """Even a "no change" call leaves an audit trail of who tried what."""
    admin, headers = make_user("s1-root2", is_admin=True)
    target, _ = make_user("s1-admin2", is_admin=True)
    db = isolated_api.app.state.user_db

    response = isolated_api.put(
        f"/users/{target['id']}/role", json={"is_admin": True}, headers=headers
    )

    assert response.status_code == 200, response.text
    event = _last_audit_event(db)
    assert event["target_id"] == str(target["id"])
    assert event["details"]["previous_is_admin"] is True
    assert event["details"]["is_admin"] is True


def test_admin_cannot_demote_themselves(isolated_api, make_user):
    admin, headers = make_user("s1-root3", is_admin=True)
    db = isolated_api.app.state.user_db

    response = isolated_api.put(
        f"/users/{admin['id']}/role", json={"is_admin": False}, headers=headers
    )

    assert response.status_code == 403
    assert db.get_user_by_id(admin["id"])["is_admin"] is True
    event = _last_audit_event(db)
    assert event["result"] == "denied"
    assert event["reason"] == "self_demotion"


def test_last_admin_guard_refuses_to_leave_zero_admins(isolated_api, make_user):
    """The guard is defense in depth: reachable from any non-endpoint role writer."""
    from rag.routers.auth import _apply_role_change

    target, _ = make_user("s1-only-admin", is_admin=True)
    db = isolated_api.app.state.user_db
    # A synthetic administrator principal (id 999) that is not the target row, so the
    # self-demotion guard cannot be what rejects this change.
    synthetic_actor = {"id": 999, "username": "synthetic", "is_admin": True}

    with pytest.raises(HTTPException) as excinfo:
        _apply_role_change(db, synthetic_actor, db.get_user_by_id(target["id"]), False)

    assert excinfo.value.status_code == 409
    assert db.get_user_by_id(target["id"])["is_admin"] is True
    event = _last_audit_event(db)
    assert event["result"] == "denied"
    assert event["reason"] == "last_administrator"


def test_is_admin_defaults_to_false_without_a_principal(isolated_api):
    db = isolated_api.app.state.user_db

    assert db._is_admin(None) is False
    assert UserRepository(db.cm)._is_admin(None) is False
    assert AnalysisRepository(db.cm)._is_admin(None) is False


def test_data_source_repository_fails_closed_without_principal(isolated_api):
    db = isolated_api.app.state.user_db
    source_id = db.create_data_source("s1-source", "rss", "{}", owner_id="alice")

    assert db.list_data_sources() == []
    assert db.get_data_source(source_id) is None
    assert db.delete_data_source(source_id) is False
    assert db.get_data_source(source_id, "alice") is not None


# ---------------------------------------------------------------------------
# B-04: one authentication entry point, no dead legacy helpers
# ---------------------------------------------------------------------------


def test_delete_file_accepts_the_browser_session_cookie(isolated_api, make_user, monkeypatch):
    """delete_file used to re-parse the Authorization header by hand, so a
    cookie-authenticated browser always got 401 (B-04)."""
    import rag.routers.documents as documents_router

    user, headers = make_user("s1-cookie-deleter")
    db = isolated_api.app.state.user_db
    upload_dir = Path(isolated_api.app.state.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "cookie.txt").write_text("private", encoding="utf-8")
    db.create_document_permission("cookie.txt", "rag_docs", user["id"], 1, False)
    monkeypatch.setattr(documents_router, "delete_doc", lambda *args, **kwargs: None)
    monkeypatch.setattr(documents_router, "_refresh_default_pipeline", lambda: None)
    token = headers["Authorization"].removeprefix("Bearer ")
    isolated_api.cookies.set("rag_session", token)

    response = isolated_api.delete("/files/cookie.txt")

    assert response.status_code == 200, response.text
    assert not (upload_dir / "cookie.txt").exists()


def test_suggest_requires_the_standard_authenticated_user(isolated_api):
    response = isolated_api.post(
        "/suggest", json={"question": "什么是 RAG？", "answer": "检索增强生成"}
    )

    assert response.status_code == 401
    assert "detail" in response.json()


def test_suggest_works_for_an_authenticated_user(isolated_api, make_user, monkeypatch):
    import rag.routers.query as query_router

    _, headers = make_user("s1-suggester")
    monkeypatch.setattr(
        query_router, "suggest_questions", lambda question, answer: ["追问一", "追问二"]
    )

    response = isolated_api.post(
        "/suggest",
        json={"question": "什么是 RAG？", "answer": "检索增强生成"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["questions"] == ["追问一", "追问二"]


def test_legacy_manual_auth_helpers_are_removed():
    import rag.routers.documents as documents_router
    import rag.routers.knowledge as knowledge_router

    removed = [
        (documents_router, "_resolve_user_from_token"),
        (documents_router, "_require_auth_helper"),
        (knowledge_router, "_get_current_user"),
        (knowledge_router, "_require_auth"),
    ]
    for module, name in removed:
        assert not hasattr(module, name), f"dead auth helper still present: {name}"


def test_api_key_principal_must_still_be_active(isolated_api, make_user, monkeypatch):
    user, _ = make_user("s1-service-account")
    db = isolated_api.app.state.user_db
    _add_org_person(db, user["id"], person_code="P-SERVICE")
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_keys", f'{{"{user["id"]}": "s1-key"}}')

    assert verify_api_key(api_key="s1-key", authorization="") == str(user["id"])

    with db.locked_transaction() as conn:
        conn.execute("UPDATE org_people SET active = 0 WHERE user_id = ?", (user["id"],))

    with pytest.raises(HTTPException) as excinfo:
        verify_api_key(api_key="s1-key", authorization="")
    assert excinfo.value.status_code == 403


def test_api_key_for_a_missing_user_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_keys", '{"s1-ghost": "s1-ghost-key"}')

    with pytest.raises(HTTPException) as excinfo:
        verify_api_key(api_key="s1-ghost-key", authorization="")

    assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# B-05: internal exception text never reaches the client
# ---------------------------------------------------------------------------


def test_delete_file_does_not_echo_the_vector_error(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.documents as documents_router

    user, headers = make_user("s1-vector-owner")
    db = isolated_api.app.state.user_db
    upload_dir = Path(isolated_api.app.state.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / "victim.txt").write_text("private", encoding="utf-8")
    db.create_document_permission("victim.txt", "rag_docs", user["id"], 1, False)
    monkeypatch.setattr(
        documents_router,
        "delete_doc",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("qdrant internal detail s1-vector-secret")
        ),
    )

    response = isolated_api.delete("/files/victim.txt", headers=headers)

    assert response.status_code == 500
    assert "s1-vector-secret" not in response.text
    assert "qdrant internal detail" not in response.text


def _create_kb_metadata(db: UserDB, kb_id: str, owner_id: int) -> None:
    db.create_kb_metadata(kb_id, kb_id, owner_id, "private")


def test_kb_query_does_not_echo_the_retrieval_error(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.knowledge as knowledge_router

    owner, headers = make_user("s1-kb-owner")
    db = isolated_api.app.state.user_db
    _create_kb_metadata(db, "kb_s1_query", owner["id"])

    class FakeManager:
        def search(self, kb_id, question, top_k):
            raise RuntimeError("fts internal s1-fts-secret")

    monkeypatch.setattr(knowledge_router, "KnowledgeBaseManager", FakeManager)

    response = isolated_api.post(
        "/knowledge-bases/kb_s1_query/query",
        json={"question": "any"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "s1-fts-secret" not in response.text


def test_generate_toc_does_not_echo_the_internal_error(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.knowledge as knowledge_router

    owner, headers = make_user("s1-toc-owner")
    db = isolated_api.app.state.user_db
    _create_kb_metadata(db, "kb_s1_toc", owner["id"])
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO kb_documents (kb_id, filename, chunk_count, status) "
            "VALUES('kb_s1_toc','doc.txt',1,'indexed')"
        )
    monkeypatch.setattr(
        knowledge_router,
        "generate_toc",
        lambda content: (_ for _ in ()).throw(RuntimeError("prompt internal s1-toc-secret")),
    )

    response = isolated_api.post("/knowledge-bases/kb_s1_toc/toc/generate", headers=headers)

    assert response.status_code == 500
    assert "s1-toc-secret" not in response.text


def test_generate_overview_does_not_echo_the_internal_error(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.knowledge as knowledge_router

    owner, headers = make_user("s1-ov-owner")
    db = isolated_api.app.state.user_db
    _create_kb_metadata(db, "kb_s1_ov", owner["id"])
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO kb_documents (kb_id, filename, chunk_count, status) "
            "VALUES('kb_s1_ov','doc.txt',1,'indexed')"
        )
    monkeypatch.setattr(
        knowledge_router,
        "generate_summary",
        lambda content: (_ for _ in ()).throw(RuntimeError("prompt internal s1-ov-secret")),
    )

    response = isolated_api.post(
        "/knowledge-bases/kb_s1_ov/overview/generate", headers=headers
    )

    assert response.status_code == 500
    assert "s1-ov-secret" not in response.text


def test_kb_share_does_not_echo_the_database_error(isolated_api, make_user, monkeypatch):
    owner, headers = make_user("s1-share-owner")
    target, _ = make_user("s1-share-target")
    db = isolated_api.app.state.user_db
    _create_kb_metadata(db, "kb_s1_share", owner["id"])
    monkeypatch.setattr(
        db,
        "share_kb",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("sqlite internal s1-sql-secret")
        ),
    )

    response = isolated_api.post(
        "/knowledge-bases/kb_s1_share/share",
        json={"user_id": target["id"], "permission": "view"},
        headers=headers,
    )

    assert response.status_code == 500
    assert "s1-sql-secret" not in response.text


# ---------------------------------------------------------------------------
# B-06: one unassigned KB must not take down the whole list; legacy denials = 404
# ---------------------------------------------------------------------------


def test_kb_list_skips_unassigned_policy_instead_of_404(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.knowledge as knowledge_router
    from rag.knowledge_base import KnowledgeBaseInfo

    user, headers = make_user("s1-list-user")
    db = isolated_api.app.state.user_db
    _create_kb_metadata(db, "kb_s1_owned", user["id"])
    db.create_kb_metadata("kb_s1_unassigned", "unassigned", 0, "public")
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO kb_org_policies(kb_id,org_code,kb_type) "
            "VALUES('kb_s1_unassigned',NULL,'unassigned')"
        )

    class FakeManager:
        def list_kbs(self):
            return [
                KnowledgeBaseInfo("kb_s1_owned", "owned", 0, ""),
                KnowledgeBaseInfo("kb_s1_unassigned", "unassigned", 0, ""),
            ]

    monkeypatch.setattr(knowledge_router, "KnowledgeBaseManager", FakeManager)

    response = isolated_api.get("/knowledge-bases", headers=headers)

    assert response.status_code == 200, response.text
    assert [item["kb_id"] for item in response.json()] == ["kb_s1_owned"]


def test_legacy_kb_denials_are_unified_as_404(isolated_api, make_user):
    owner, _ = make_user("s1-legacy-owner")
    outsider, outsider_headers = make_user("s1-legacy-outsider")
    db = isolated_api.app.state.user_db
    _create_kb_metadata(db, "kb_s1_legacy", owner["id"])

    responses = {
        "shares": isolated_api.get(
            "/knowledge-bases/kb_s1_legacy/shares", headers=outsider_headers
        ),
        "share": isolated_api.post(
            "/knowledge-bases/kb_s1_legacy/share",
            json={"user_id": outsider["id"], "permission": "view"},
            headers=outsider_headers,
        ),
        "unshare": isolated_api.delete(
            f"/knowledge-bases/kb_s1_legacy/share/{outsider['id']}",
            headers=outsider_headers,
        ),
        "rename": isolated_api.put(
            "/knowledge-bases/kb_s1_legacy/name",
            json={"name": "stolen"},
            headers=outsider_headers,
        ),
        "toc": isolated_api.post(
            "/knowledge-bases/kb_s1_legacy/toc/generate", headers=outsider_headers
        ),
        "overview": isolated_api.post(
            "/knowledge-bases/kb_s1_legacy/overview/generate", headers=outsider_headers
        ),
    }

    for name, response in responses.items():
        assert response.status_code == 404, f"{name}: {response.status_code} {response.text}"


# ---------------------------------------------------------------------------
# B-07: SQLite data sources are confined to whitelisted directories
# ---------------------------------------------------------------------------


def test_sqlite_source_must_resolve_inside_an_allowed_directory(tmp_path, monkeypatch):
    allowed = tmp_path / "structured_datasets"
    allowed.mkdir()
    monkeypatch.setattr(settings, "structured_datasets_root", str(allowed))

    inside = allowed / "reports.sqlite"
    inside.write_bytes(b"")
    DBSource(f"sqlite:///{inside}", "SELECT 1")  # must not raise

    outside = tmp_path / "outside.sqlite"
    outside.write_bytes(b"")
    with pytest.raises(ValueError, match="禁止访问系统数据库"):
        DBSource(f"sqlite:///{outside}", "SELECT 1")


def test_sqlite_source_keeps_the_system_database_blacklist(tmp_path, monkeypatch):
    allowed = tmp_path / "structured_datasets"
    allowed.mkdir()
    monkeypatch.setattr(settings, "structured_datasets_root", str(allowed))

    for name in ("users.db", "memory.db", "jwt_secret.txt"):
        with pytest.raises(ValueError, match="禁止访问系统数据库"):
            DBSource(f"sqlite:///{allowed / name}", "SELECT 1")


def test_sqlite_source_rejects_directory_traversal():
    with pytest.raises(ValueError, match="禁止访问系统数据库"):
        DBSource("sqlite:///../secret.db", "SELECT 1")


# ---------------------------------------------------------------------------
# B-08: BatchImporter requires an authenticated principal + edit permission
# ---------------------------------------------------------------------------


def _import_fixture(tmp_path: Path) -> Path:
    return _write_csv(
        tmp_path / "faq.csv",
        [["问题", "答案"], ["什么是 mTLS？", "双向 TLS 认证"]],
    )


def test_batch_importer_requires_user_and_user_db(tmp_path, isolated_api):
    db = isolated_api.app.state.user_db
    user_id = db.create_user("s1-batch", "Password-123!")
    user = db.get_user_by_id(user_id)
    csv_path = _import_fixture(tmp_path)

    with pytest.raises(ValueError, match="user"):
        BatchImporter().import_file(str(csv_path), user=None, user_db=db)
    with pytest.raises(ValueError, match="user_db"):
        BatchImporter().import_file(str(csv_path), user=user, user_db=None)


def test_batch_import_defaults_to_rejecting_duplicates(tmp_path, isolated_api, monkeypatch):
    db = isolated_api.app.state.user_db
    user_id = db.create_user("s1-batch-owner", "Password-123!")
    user = db.get_user_by_id(user_id)
    csv_path = _import_fixture(tmp_path)
    monkeypatch.setattr("rag.embedder.embed", lambda texts: [[0.1] * 8 for _ in texts])
    monkeypatch.setattr("rag.vector_store.add_to_collection", lambda *args, **kwargs: None)

    first = BatchImporter().import_file(str(csv_path), user=user, user_db=db)
    assert first["status"] == "imported"

    with pytest.raises(DuplicateDocument):
        BatchImporter().import_file(str(csv_path), user=user, user_db=db)

    versions = db.list_document_versions(first["document_id"])
    assert len(versions) == 1, "a rejected duplicate must not create a version"


def test_batch_import_overwrite_requires_document_edit_permission(
    tmp_path, isolated_api, monkeypatch
):
    db = isolated_api.app.state.user_db
    owner_id = db.create_user("s1-batch-alice", "Password-123!")
    other_id = db.create_user("s1-batch-bob", "Password-123!")
    owner = db.get_user_by_id(owner_id)
    other = db.get_user_by_id(other_id)
    csv_path = _import_fixture(tmp_path)
    monkeypatch.setattr("rag.embedder.embed", lambda texts: [[0.1] * 8 for _ in texts])
    monkeypatch.setattr(
        "rag.vector_store.add_to_collection", lambda *args, **kwargs: None
    )

    first = BatchImporter().import_file(str(csv_path), user=owner, user_db=db)
    assert first["status"] == "imported"

    with pytest.raises(HTTPException) as excinfo:
        BatchImporter().import_file(
            str(csv_path), user=other, user_db=db, duplicate_action="overwrite"
        )

    assert excinfo.value.status_code == 403
    versions = db.list_document_versions(first["document_id"])
    assert len(versions) == 1, "denied overwrite must not create a version"


# ---------------------------------------------------------------------------
# A-06: /regenerate follows the answer's knowledge base, warning, filter, reliability
# ---------------------------------------------------------------------------


def _regenerate_fixture(isolated_api, make_user):
    owner, headers = make_user("s1-regen-owner")
    db = isolated_api.app.state.user_db
    conversation_id = db.create_conversation(owner["id"], "chat")
    db.add_message(conversation_id, "user", "旧政策是什么？")
    message_id = db.add_message(conversation_id, "assistant", "旧答案")
    return owner, headers, db, conversation_id, message_id


def _link_answer_to_kb(db: UserDB, message_id: int, kb_id: str, owner_id: int) -> int:
    document_id = db.create_document_permission("policy.pdf", kb_id, owner_id)
    document_version_id = db.create_document_version(
        document_id, "policy.pdf", "unused", "policy-hash", owner_id
    )
    with db.locked_transaction() as conn:
        conn.execute(
            "INSERT INTO answer_citations "
            "(assistant_message_id, document_id, document_version_id, chunk_index, "
            "section_title, text_preview, rank, is_historical, source_kind, kb_id, source_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                message_id,
                document_id,
                document_version_id,
                0,
                "",
                "",
                0,
                1,
                "document",
                kb_id,
                "policy.pdf",
            ),
        )
    return document_id


class _FakePipeline:
    def __init__(self, prepared):
        self.prepared = prepared
        self.requested_kb: str | None = None

    def _memory_session_id(self, session_id, user):
        return f"u{user['id']}:{session_id}"

    def _prepare_context(
        self, question, session_id, doc_name, top_k=8, tags=None, user=None, kb_id=None
    ):
        self.requested_kb = kb_id
        return dict(self.prepared), None


def test_regenerate_uses_the_messages_kb_and_adds_the_historical_warning(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.query as query_router
    from rag.pipeline import _HISTORICAL_EVIDENCE_WARNING

    owner, headers, db, conversation_id, message_id = _regenerate_fixture(
        isolated_api, make_user
    )
    db.create_kb_metadata("kb_s1_hist", "Hist", owner["id"], "private")
    _link_answer_to_kb(db, message_id, "kb_s1_hist", owner["id"])
    pipeline = _FakePipeline(
        {
            "route": "rag",
            "messages": [{"role": "user", "content": "旧政策是什么？"}],
            "sources": [],
            "decision": SimpleNamespace(kind="answerable_with_warning"),
            "confidence": SimpleNamespace(level="medium", reasons=("historical_evidence",)),
        }
    )
    monkeypatch.setattr(
        query_router, "_get_or_create_pipeline", lambda kb_id: pipeline
    )
    monkeypatch.setattr(query_router, "generate", lambda messages, temperature=0.7: "新答案")

    response = isolated_api.post(
        "/regenerate",
        json={"conversation_id": conversation_id, "message_id": message_id},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert pipeline.requested_kb == "kb_s1_hist"
    assert response.json()["answer"] == _HISTORICAL_EVIDENCE_WARNING + "新答案"
    reliability = db.get_message_reliability(message_id, owner["id"])
    assert reliability["answerability_kind"] == "answerable_with_warning"
    assert reliability["confidence_level"] == "medium"


def test_regenerate_applies_the_output_filter(isolated_api, make_user, monkeypatch):
    import rag.routers.query as query_router

    _, headers, db, conversation_id, message_id = _regenerate_fixture(isolated_api, make_user)
    pipeline = _FakePipeline(
        {
            "route": "rag",
            "messages": [{"role": "user", "content": "旧政策是什么？"}],
            "sources": [],
        }
    )
    monkeypatch.setattr(
        query_router, "get_pipeline", lambda: pipeline
    )
    monkeypatch.setattr(query_router, "generate", lambda messages, temperature=0.7: "敏感回答")
    monkeypatch.setattr(
        query_router,
        "check_output",
        lambda text: OutputResult(filtered=True, text="[已过滤]"),
    )

    response = isolated_api.post(
        "/regenerate",
        json={"conversation_id": conversation_id, "message_id": message_id},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "[已过滤]"


def test_regenerate_invalidates_stale_reliability_without_a_new_assessment(
    isolated_api, make_user, monkeypatch
):
    import rag.routers.query as query_router

    owner, headers, db, conversation_id, message_id = _regenerate_fixture(
        isolated_api, make_user
    )
    with db.locked_transaction() as conn:
        conn.execute(
            "UPDATE chat_messages SET confidence_level='high', "
            "confidence_reasons_json='[\"stale\"]', answerability_kind='insufficient_evidence' "
            "WHERE id = ?",
            (message_id,),
        )
        conn.execute(
            "INSERT INTO answer_citations "
            "(assistant_message_id, document_id, document_version_id, chunk_index, "
                "section_title, text_preview, rank, is_historical, source_kind, kb_id, source_name) "
                "VALUES(?, 999, 999, 0, '', 'stale source', 0, 0, 'document', 'rag_docs', 'stale.pdf')",
            (message_id,),
        )
    pipeline = _FakePipeline(
        {
            "route": "rag",
            "messages": [{"role": "user", "content": "旧政策是什么？"}],
            "sources": [],
        }
    )
    monkeypatch.setattr(
        query_router, "get_pipeline", lambda: pipeline
    )
    monkeypatch.setattr(query_router, "generate", lambda messages, temperature=0.7: "新答案")

    response = isolated_api.post(
        "/regenerate",
        json={"conversation_id": conversation_id, "message_id": message_id},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert pipeline.requested_kb == "rag_docs"
    reliability = db.get_message_reliability(message_id, owner["id"])
    assert reliability["confidence_level"] == "unavailable"
    assert reliability["confidence_reasons"] == []
    assert db.get_answer_citations(message_id) == []
    assert response.json()["sources"] == []
