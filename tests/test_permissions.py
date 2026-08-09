"""Tests for rag.permissions — document permission utility functions.

简化模型：owner / is_public / protected / admin
"""

import pytest
from fastapi import HTTPException

from rag.permissions import check_doc_permission
from rag.user_db import UserDB


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    udb = UserDB(path)
    yield udb
    udb.close()


def _make_user(db: UserDB, username: str, is_admin: bool = False) -> dict:
    uid = db.create_user(username, "pwd")
    if is_admin:
        db.set_user_admin(uid, True)
    return db.get_user_by_id(uid)


# -- check_doc_permission: view -------------------------------------------


def test_owner_can_view(db):
    """文档上传者（owner）可以查看。"""
    owner = _make_user(db, "alice")
    db.create_document_permission("report.pdf", "rag_docs", owner["id"], is_public=False)
    result = check_doc_permission(db, "report.pdf", "rag_docs", owner, action="view")
    assert result["doc_name"] == "report.pdf"


def test_public_doc_visible_to_anyone(db):
    """公开文档任何人可查看。"""
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    db.create_document_permission("report.pdf", "rag_docs", owner["id"], is_public=True)
    result = check_doc_permission(db, "report.pdf", "rag_docs", viewer, action="view")
    assert result["doc_name"] == "report.pdf"


def test_private_doc_only_visible_to_owner(db):
    """私有文档仅 owner 可查看。"""
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    db.create_document_permission("secret.pdf", "rag_docs", owner["id"], is_public=False)
    with pytest.raises(HTTPException) as exc_info:
        check_doc_permission(db, "secret.pdf", "rag_docs", viewer, action="view")
    assert exc_info.value.status_code == 403


def test_shared_user_can_view(db):
    """被共享的用户可以查看 shared 文档。"""
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    doc_id = db.create_document_permission("secret.pdf", "rag_docs", owner["id"], scope="shared")
    db.share_document(doc_id, viewer["id"], owner["id"])
    result = check_doc_permission(db, "secret.pdf", "rag_docs", viewer, action="view")
    assert result["doc_name"] == "secret.pdf"


def test_admin_bypass_all_permissions(db):
    """系统管理员绕过所有权限限制。"""
    owner = _make_user(db, "alice")
    admin = _make_user(db, "root", is_admin=True)
    db.create_document_permission("secret.pdf", "rag_docs", owner["id"], is_public=False)
    result = check_doc_permission(db, "secret.pdf", "rag_docs", admin, action="view")
    assert result["doc_name"] == "secret.pdf"


def test_old_doc_without_permission_record_denied(db):
    """旧文档无权限记录时，默认拒绝（安全收敛）；仅 admin 放行。"""
    user = _make_user(db, "alice")
    admin = _make_user(db, "root", is_admin=True)

    # 普通用户：view / delete / edit 一律 403
    for action in ("view", "delete", "edit"):
        with pytest.raises(HTTPException) as exc_info:
            check_doc_permission(db, "old_file.pdf", "rag_docs", user, action=action)
        assert exc_info.value.status_code == 403

    # 管理员放行（返回 None，不抛异常）
    assert check_doc_permission(db, "old_file.pdf", "rag_docs", admin, action="delete") is None


# -- check_doc_permission: edit / delete -----------------------------------


def test_owner_can_edit(db):
    owner = _make_user(db, "alice")
    db.create_document_permission("report.pdf", "rag_docs", owner["id"])
    result = check_doc_permission(db, "report.pdf", "rag_docs", owner, action="edit")
    assert result["doc_name"] == "report.pdf"


def test_owner_can_delete(db):
    owner = _make_user(db, "alice")
    db.create_document_permission("report.pdf", "rag_docs", owner["id"])
    result = check_doc_permission(db, "report.pdf", "rag_docs", owner, action="delete")
    assert result["doc_name"] == "report.pdf"


def test_non_owner_cannot_edit(db):
    owner = _make_user(db, "alice")
    other = _make_user(db, "bob")
    db.create_document_permission("report.pdf", "rag_docs", owner["id"])
    with pytest.raises(HTTPException) as exc_info:
        check_doc_permission(db, "report.pdf", "rag_docs", other, action="edit")
    assert exc_info.value.status_code == 403


def test_non_owner_cannot_delete(db):
    owner = _make_user(db, "alice")
    other = _make_user(db, "bob")
    db.create_document_permission("report.pdf", "rag_docs", owner["id"])
    with pytest.raises(HTTPException) as exc_info:
        check_doc_permission(db, "report.pdf", "rag_docs", other, action="delete")
    assert exc_info.value.status_code == 403


def test_admin_can_edit_any_doc(db):
    owner = _make_user(db, "alice")
    admin = _make_user(db, "root", is_admin=True)
    db.create_document_permission("report.pdf", "rag_docs", owner["id"])
    result = check_doc_permission(db, "report.pdf", "rag_docs", admin, action="edit")
    assert result["doc_name"] == "report.pdf"


# -- get_accessible_doc_names ---------------------------------------------


def test_get_accessible_doc_names_includes_owned_and_public(db):
    from rag.permissions import get_accessible_doc_names
    user = _make_user(db, "alice")
    db.create_document_permission("a.pdf", "kb1", user["id"], is_public=False)
    db.create_document_permission("b.pdf", "kb1", user["id"], is_public=True)
    names = get_accessible_doc_names(db, "kb1", user)
    assert set(names) == {"a.pdf", "b.pdf"}


def test_get_accessible_doc_names_includes_shared(db):
    from rag.permissions import get_accessible_doc_names
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    doc_id = db.create_document_permission("secret.pdf", "kb1", owner["id"], is_public=False)
    db.share_document(doc_id, viewer["id"], owner["id"])
    names = get_accessible_doc_names(db, "kb1", viewer)
    assert "secret.pdf" in names


def test_get_accessible_doc_names_excludes_inaccessible(db):
    from rag.permissions import get_accessible_doc_names
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    db.create_document_permission("secret.pdf", "kb1", owner["id"], is_public=False)
    names = get_accessible_doc_names(db, "kb1", viewer)
    assert "secret.pdf" not in names


# -- filter_chunks_by_permission -------------------------------------------


def test_filter_chunks_admin_sees_all(db):
    """管理员不过滤。"""
    from rag.models import Chunk
    from rag.permissions import filter_chunks_by_permission
    admin = _make_user(db, "root", is_admin=True)
    chunks = [Chunk(text="a", doc_name="x.pdf", chunk_index=0)]
    result = filter_chunks_by_permission(db, "kb1", chunks, admin)
    assert len(result) == 1


def test_filter_chunks_removes_private_non_owned(db):
    """过滤掉非 owner 的私有文档的 chunks。"""
    from rag.models import Chunk
    from rag.permissions import filter_chunks_by_permission
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    db.create_document_permission("secret.pdf", "kb1", owner["id"], is_public=False)
    db.create_document_permission("public.pdf", "kb1", owner["id"], is_public=True)
    chunks = [
        Chunk(text="a", doc_name="secret.pdf", chunk_index=0),
        Chunk(text="b", doc_name="public.pdf", chunk_index=0),
    ]
    result = filter_chunks_by_permission(db, "kb1", chunks, viewer)
    assert len(result) == 1
    assert result[0].doc_name == "public.pdf"


def test_filter_chunks_old_doc_no_permission_record(db):
    """旧文档无权限记录，视为公开，保留。"""
    from rag.models import Chunk
    from rag.permissions import filter_chunks_by_permission
    viewer = _make_user(db, "bob")
    chunks = [Chunk(text="a", doc_name="old_file.pdf", chunk_index=0)]
    result = filter_chunks_by_permission(db, "kb1", chunks, viewer)
    assert len(result) == 1


def test_filter_chunks_shared_doc_included(db):
    """被共享的文档的 chunks 保留。"""
    from rag.models import Chunk
    from rag.permissions import filter_chunks_by_permission
    owner = _make_user(db, "alice")
    viewer = _make_user(db, "bob")
    doc_id = db.create_document_permission("secret.pdf", "kb1", owner["id"], is_public=False)
    db.share_document(doc_id, viewer["id"], owner["id"])
    chunks = [Chunk(text="a", doc_name="secret.pdf", chunk_index=0)]
    result = filter_chunks_by_permission(db, "kb1", chunks, viewer)
    assert len(result) == 1
