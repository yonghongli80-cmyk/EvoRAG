"""重新生成测试。"""
import os
import tempfile


def test_user_db_update_message():
    """UserDB.update_message 应该更新消息内容。"""
    from rag.user_db import UserDB

    db_path = tempfile.mktemp(suffix=".db")
    try:
        db = UserDB(db_path)
        user_id = db.create_user("test", "password123")
        conv_id = db.create_conversation(user_id)
        db.add_message(conv_id, "user", "什么是 mTLS？")
        db.add_message(conv_id, "assistant", "mTLS 是双向 TLS...")

        messages = db.get_messages(conv_id, user_id)
        original_count = len(messages)
        assistant_msg_id = messages[-1]["id"]

        db.update_message(assistant_msg_id, "mTLS（双向 TLS）是一种安全协议...")

        messages_after = db.get_messages(conv_id, user_id)
        assert len(messages_after) == original_count
        assert messages_after[-1]["content"] == "mTLS（双向 TLS）是一种安全协议..."
    finally:
        db.close()
        os.unlink(db_path)


def test_regenerate_endpoint_exists():
    """POST /regenerate 端点应该存在于 app 上。

    路由已迁到 `rag.routers.query`（由 `rag.api` 挂载）。这里直接断言 app 的路由表：
    套件在 auth 关闭下运行，`require_user` 会注入开发用户，此时请求一个不存在的会话
    会被处理器以 404「消息不存在」拒绝——状态码 404 已无法区分「路由缺失」与
    「资源不存在」，所以用路由注册事实来表达「端点存在」这一契约。
    """
    from fastapi import routing

    from rag.api import app

    regenerate_routes = [
        ctx.route
        for ctx in routing.iter_route_contexts(app.routes)
        if getattr(ctx, "route", None) is not None and ctx.route.path == "/regenerate"
    ]

    assert regenerate_routes, "POST /regenerate 未挂载到 app"
    assert any("POST" in route.methods for route in regenerate_routes)
