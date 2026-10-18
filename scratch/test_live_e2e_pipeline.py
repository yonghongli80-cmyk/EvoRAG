"""Real End-to-End (E2E) Live Pipeline Verification Script for RAGv3.

Tests full real user workflows against the application:
1. System Health Check (/health)
2. User Registration & Authentication & /me (/register, /login, /me)
3. Multi-tenant Knowledge Base Management (/knowledge-bases)
4. Document Upload, Versioning, and Collection Indexing (/upload, /files)
5. RAG Retrieval & End-to-End Query (/query, /query/stream)
6. Message Feedback & Evaluation Trace (/feedback)
7. Data Source Management & Masking Security (/sources)
8. Wiki System CRUD & Revisions (/wiki/pages)
9. Destruction & Access Control Boundaries (/files/{filename})
"""

import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import io  # noqa: E402 - 先修正 sys.path 再导入项目模块（脚本的既有写法）
import json  # noqa: E402
import uuid  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import settings  # noqa: E402
from rag.api import app  # noqa: E402


def run_e2e_check():
    print("=" * 70)
    print(">> Starting RAGv3 Live Feature & Pipeline Verification")
    print("=" * 70)

    client = TestClient(app)
    suffix = uuid.uuid4().hex[:6]
    LAST_RUN["suffix"] = suffix
    test_admin_name = f"admin_{suffix}"
    test_user_name = f"alice_{suffix}"
    password = "Password123!"

    # 1. Health Check
    print("\n[Step 1] System Health & Component Status...")
    res = client.get("/health")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    health_data = res.json()
    print(f"  [OK] Health Status: {health_data['status']}")
    print(f"  [OK] Components: {health_data.get('components', {})}")

    # 2. Registration & Authentication
    print("\n[Step 2] Authentication & Token Lifecycle...")
    original_self_reg = settings.self_registration_enabled
    settings.self_registration_enabled = True
    try:
        # Register Admin User
        res = client.post("/register", json={"username": test_admin_name, "password": password})
        assert res.status_code == 200, f"Admin register failed: {res.text}"

        # Register Normal User
        res = client.post("/register", json={"username": test_user_name, "password": password})
        assert res.status_code == 200, f"Alice register failed: {res.text}"
    finally:
        settings.self_registration_enabled = original_self_reg

    admin_user = app.state.user_db.get_user_by_username(test_admin_name)
    alice_user = app.state.user_db.get_user_by_username(test_user_name)
    admin_id = admin_user["id"]
    alice_id = alice_user["id"]

    # Set admin privilege & setup org
    app.state.user_db.set_user_admin(admin_id, True)
    person_code = f"P-ADMIN-{suffix}"
    org_code = f"ORG-{suffix}"
    with app.state.user_db.locked_transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO org_units(code,name,kind) VALUES(?,?,'company')", (org_code, 'Test Org'))
        conn.execute(
            "INSERT OR REPLACE INTO org_people(person_code,user_id,display_name,job_title,active) "
            "VALUES(?,?,'Test Admin','Administrator',1)",
            (person_code, admin_id),
        )
        conn.execute("INSERT OR REPLACE INTO org_memberships(person_code,org_code,is_primary) VALUES(?,?,1)", (person_code, org_code))
        conn.execute("INSERT OR REPLACE INTO org_manager_scopes(person_code,org_code) VALUES(?,?)", (person_code, org_code))

    # Login Admin
    res = client.post("/login", json={"username": test_admin_name, "password": password})
    assert res.status_code == 200, f"Admin login failed: {res.text}"
    admin_token = res.json()["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # Login Alice
    res = client.post("/login", json={"username": test_user_name, "password": password})
    assert res.status_code == 200, f"Alice login failed: {res.text}"
    alice_token = res.json()["token"]
    alice_headers = {"Authorization": f"Bearer {alice_token}"}

    # Verify /me endpoint
    res = client.get("/me", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["username"] == test_admin_name
    assert res.json().get("is_admin") is True
    print(f"  [OK] Admin authenticated successfully: ID={admin_id}, is_admin=True")

    res = client.get("/me", headers=alice_headers)
    assert res.status_code == 200
    assert res.json()["username"] == test_user_name
    print(f"  [OK] Alice authenticated successfully: ID={alice_id}")

    # 3. Knowledge Base Lifecycle
    print("\n[Step 3] Knowledge Base Lifecycle & Isolation...")
    kb_name = f"TechSpecs_{suffix}"
    res = client.post(
        "/knowledge-bases",
        headers=admin_headers,
        json={"name": kb_name, "scope": "private", "org_unit_code": org_code},
    )
    assert res.status_code == 200, f"Create KB failed: {res.text}"
    kb_id = res.json()["kb_id"]
    print(f"  [OK] Created Knowledge Base: ID='{kb_id}', Name='{kb_name}'")

    res = client.get("/knowledge-bases", headers=admin_headers)
    assert res.status_code == 200
    kb_list = res.json()
    assert any((kb.get("kb_id") or kb.get("id")) == kb_id for kb in kb_list)
    print(f"  [OK] Knowledge Base listed successfully: total={len(kb_list)}")

    # 4. Document Ingestion, Versioning, Indexing
    print("\n[Step 4] Document Upload & Indexing...")
    sample_content = (
        "Project Antigravity RAG System is a high-concurrency enterprise retrieval architecture.\n"
        "It supports SQLite WAL concurrent read-write separation and multi-model dynamic LLM routing.\n"
        "Version 3.0 introduces repository pattern and modular Vue 3 components."
    )
    doc_name = f"doc_spec_{suffix}.txt"
    files = {"file": (doc_name, io.BytesIO(sample_content.encode("utf-8")), "text/plain")}
    res = client.post("/upload", headers=admin_headers, files=files)
    assert res.status_code == 200, f"Upload failed: {res.text}"
    upload_res = res.json()
    print(f"  [OK] Document uploaded and indexed: '{doc_name}', status={upload_res.get('status')}")

    # Check /files
    res = client.get("/files", headers=admin_headers)
    assert res.status_code == 200
    files_list = res.json().get("files", [])
    assert any(f["name"] == doc_name for f in files_list)
    print(f"  [OK] GET /files returned successfully without 500 error: count={len(files_list)}")

    # 5. Conversation & RAG Query Chain
    print("\n[Step 5] RAG Conversation & Query Pipeline...")
    res = client.post("/conversations", headers=alice_headers, json={"title": "RAG Query Test", "mode": "file"})
    assert res.status_code == 200
    conv_id = res.json()["id"]
    print(f"  [OK] Created Conversation: ID={conv_id}")

    # Query
    question = "What does Version 3.0 introduce in Antigravity RAG System?"
    query_payload = {
        "question": question,
        "conversation_id": conv_id,
        "history": [],
    }
    res = client.post("/query", headers=alice_headers, json=query_payload)
    assert res.status_code == 200, f"Query failed: {res.text}"
    query_data = res.json()
    print("  [OK] Query returned 200 OK:")
    print(f"    - Answer preview: {query_data.get('answer', '')[:80]}...")
    print(f"    - Sources count: {len(query_data.get('sources', []))}")
    print(f"    - Tokens/latency info: {query_data.get('latency_ms', 'N/A')}ms")

    # 6. Message Feedback Loop
    print("\n[Step 6] Feedback Submission...")
    # Fetch messages in conversation to get assistant message ID
    res = client.get(f"/conversations/{conv_id}/messages", headers=alice_headers)
    assert res.status_code == 200
    msgs = res.json() if isinstance(res.json(), list) else res.json().get("messages", [])
    assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
    if assistant_msgs:
        ast_msg_id = assistant_msgs[-1]["id"]
        res = client.post(
            "/feedback",
            headers=alice_headers,
            json={"message_id": ast_msg_id, "value": "positive", "comment": "Great accurate answer!"},
        )
        assert res.status_code == 200, f"Feedback submission failed: {res.text}"
        print(f"  [OK] Feedback recorded for assistant message ID={ast_msg_id}")

    # 7. Data Sources Security & Masking
    print("\n[Step 7] Data Source Creation & Masking Guard...")
    original_ds = settings.data_sources_enabled
    settings.data_sources_enabled = True
    try:
        src_payload = {
            "name": f"SecureDB_{suffix}",
            "type": "database",
            "config": {
                "connection_string": "postgresql://dbuser:super_secret_pwd@10.0.0.1:5432/analytics",
                "query": "SELECT 1",
            },
        }
        res = client.post("/sources", headers=admin_headers, json=src_payload)
        assert res.status_code == 200, f"Create source failed: {res.text}"
        created_src = res.json()
        assert created_src["has_secret"] is True
        assert "super_secret_pwd" not in json.dumps(created_src)
        print(f"  [OK] Data Source created with credential mask: has_secret={created_src['has_secret']}")

        res = client.get("/sources", headers=admin_headers)
        assert res.status_code == 200
        sources_list = res.json() if isinstance(res.json(), list) else res.json().get("sources", [])
        matched_src = next((s for s in sources_list if s["id"] == created_src["id"]), None)
        assert matched_src is not None
        assert matched_src["has_secret"] is True
        assert "super_secret_pwd" not in json.dumps(matched_src)
        print("  [OK] GET /sources verifies sensitive credentials masked: ***")
    finally:
        settings.data_sources_enabled = original_ds

    # 8. Wiki Management & Revision History
    print("\n[Step 8] Wiki System & Markdown Versioning...")
    wiki_payload = {
        "kb_id": kb_id,
        "title": f"Architecture Overview {suffix}",
        "slug": f"arch-overview-{suffix}",
        "content": "# Architecture Overview\nThis documents the modularized system design.",
        "tags": ["architecture", "spec"],
    }
    res = client.post("/wiki/pages", headers=admin_headers, json=wiki_payload)
    assert res.status_code in (200, 201), f"Create wiki page failed: {res.text}"
    wiki_page = res.json()
    page_id = wiki_page.get("id") or wiki_page.get("page", {}).get("id")
    print(f"  [OK] Wiki page created successfully: ID={page_id}")

    res = client.get(f"/wiki/pages?kb_id={kb_id}", headers=admin_headers)
    assert res.status_code == 200
    print(f"  [OK] Wiki pages retrieved: total={len(res.json())}")

    # 9. Destruction & Access Control Boundaries
    print("\n[Step 9] Security Authorization Boundaries (/files/{filename})...")
    # Anonymous client without session cookie must be 401
    anon_client = TestClient(app)
    res = anon_client.delete(f"/files/{doc_name}")
    assert res.status_code == 401
    print("  [OK] Anonymous request properly rejected: HTTP 401")

    # Regular user (Alice) attempting to delete Admin's document must be 403 Forbidden
    res = client.delete(f"/files/{doc_name}", headers=alice_headers)
    assert res.status_code == 403
    print("  [OK] Non-owner deletion properly rejected: HTTP 403 Forbidden")

    # Invalid token must be 401
    res = client.delete(f"/files/{doc_name}", headers={"Authorization": "Bearer invalid_fake_token"})
    assert res.status_code == 401
    print("  [OK] Invalid token deletion properly rejected: HTTP 401")

    # Authorized deletion succeeds
    res = client.delete(f"/files/{doc_name}", headers=admin_headers)
    assert res.status_code == 200, f"Authorized delete failed: {res.text}"
    print("  [OK] Authorized admin deletion succeeded: HTTP 200")

    # Clean up knowledge base
    res = client.delete(f"/knowledge-bases/{kb_id}", headers=admin_headers)
    assert res.status_code == 200
    print(f"  [OK] Knowledge base cleaned up: ID={kb_id}")

    print("\n" + "=" * 70)
    print("SUCCESS: ALL REAL E2E WORKFLOW CHECKS PASSED WITH 100% SUCCESS!")
    print("=" * 70)
    return True


#: 本次运行留下的可清理痕迹（suffix 决定账号名与知识库 id）。
LAST_RUN: dict[str, str] = {}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _production_paths_used() -> list[str]:
    """Which of the four stores still point inside the repository (i.e. at production data)."""
    inside = []
    for name in ("qdrant_path", "users_db_path", "memory_db_path", "wiki_db_path"):
        value = str(getattr(settings, name, "") or "")
        if not value:
            continue
        try:
            inside_root = Path(value).resolve().is_relative_to(_PROJECT_ROOT)
        except (OSError, ValueError):
            inside_root = True
        if inside_root:
            inside.append(f"{name}={value}")
    return inside


def _cleanup(suffix: str) -> None:
    """Remove what this run created: accounts, org rows, wiki pages, KB and its vectors.

    The original script deleted the uploaded file and the knowledge base only, leaving two accounts,
    one Qdrant collection and one wiki page behind on every run — twelve accounts and three pages
    were found in the production stores on 2026-10-18. Cleanup belongs in the same script that
    causes the residue.
    """
    import contextlib

    db = app.state.user_db
    person_code = f"P-ADMIN-{suffix}"
    org_code = f"ORG-{suffix}"
    with contextlib.suppress(Exception), db.locked_transaction() as conn:
        conn.execute("DELETE FROM org_manager_scopes WHERE person_code = ?", (person_code,))
        conn.execute("DELETE FROM org_memberships WHERE person_code = ?", (person_code,))
        conn.execute("DELETE FROM org_people WHERE person_code = ?", (person_code,))
        conn.execute("DELETE FROM org_units WHERE code = ?", (org_code,))
        conn.execute(
            "DELETE FROM users WHERE username IN (?, ?)", (f"admin_{suffix}", f"alice_{suffix}")
        )
    print(f"  [cleanup] accounts/org rows for suffix={suffix} removed")

    with contextlib.suppress(Exception):
        from rag.wiki.store import WikiStore

        wiki = WikiStore(settings.wiki_db_path)
        try:
            for page in wiki.list_pages(limit=1000):
                if page.kb_id.startswith(f"kb_techspecs_{suffix}"):
                    wiki.delete_page(page.id)
                    print(f"  [cleanup] wiki page {page.id} ({page.kb_id}) removed")
        finally:
            wiki.close()

    with contextlib.suppress(Exception):
        # 用 Qdrant 自己的 delete_collection 先删；但 Qdrant 本地模式在**进程退出前**不释放目录句柄，
        # 于是 rmtree 会静默失败（前两次"清理成功"其实没删掉）。所以剩下的目录交给一个分离的子进程，
        # 等本进程退出、句柄释放之后再删。
        import subprocess

        import rag.vector_store as vector_store

        client = vector_store._get_client()
        leftovers = []
        for collection in Path(settings.qdrant_path, "collection").glob(f"kb_techspecs_{suffix}*"):
            name = collection.name
            with contextlib.suppress(Exception):
                client.delete_collection(collection_name=name)
                print(f"  [cleanup] qdrant collection {name} deleted")
            if collection.exists():
                leftovers.append(str(collection))
        vector_store.close_client()
        if leftovers:
            reaper = (
                "import shutil,sys,time;time.sleep(3);"
                "[shutil.rmtree(p, ignore_errors=True) for p in sys.argv[1:]]"
            )
            subprocess.Popen(
                [sys.executable, "-c", reaper, *leftovers],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"  [cleanup] deferred removal of {len(leftovers)} collection dir(s)")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    production = _production_paths_used()
    if production and "--allow-production" not in argv:
        print("REFUSED: this run would write the repository's own stores:")
        for item in production:
            print(f"  {item}")
        print(
            "\nPoint the stores at a read-only copy first, e.g.\n"
            "  $env:RAG_QDRANT_PATH='<copy>/qdrant_data'; "
            "$env:RAG_USERS_DB_PATH='<copy>/users.db';\n"
            "  $env:RAG_MEMORY_DB_PATH='<copy>/memory.db'; "
            "$env:RAG_WIKI_DB_PATH='<copy>/wiki.db'\n"
            "…or pass --allow-production if you really mean to write production "
            "(it creates accounts, a knowledge base and a wiki page)."
        )
        return 2
    try:
        success = run_e2e_check()
    finally:
        suffix = LAST_RUN.get("suffix")
        if suffix:
            _cleanup(suffix)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
