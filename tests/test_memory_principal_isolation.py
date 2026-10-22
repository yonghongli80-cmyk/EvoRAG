"""B-01：对话记忆必须按主体隔离，且生成前先校验对话归属。

缺陷背景（2026-10-22 全库评审 P0-02）：

* ``/query``、``/query/stream`` 在收到 ``conversation_id`` 时直接映射成
  ``conv_<id>`` 并进入生成，归属校验只在**生成之后**落库时才有；顺序整数可猜，
  于是任何登录用户都能把别人的对话历史读进 prompt。
* 缺省 ``session_id`` 落到全局 ``"default"``；两个用户什么都不传就会共享同一段
  记忆，互相看到对方的问答，新问题还会写进同一会话（泄露 + 投毒）。

这里钉住三件事：跨用户 conversation_id 在生成前 404；同名 session（含缺省
session）按 principal 命名空间隔离；同一用户的多轮记忆仍然正常。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rag.memory import session_key
from rag.models import Chunk


def test_session_key_namespaces_by_principal():
    """会话键 = ``u{user_id}:{session_id}``；缺省 session 不再落全局 default。"""
    assert session_key(1, "conv_2") == "u1:conv_2"
    assert session_key(2, None) == "u2:default"
    assert session_key(2, "") == "u2:default"
    # 幂等：pipeline 入口与 _prepare_context 都会调用它，不能叠成 u2:u2:default。
    assert session_key(2, "u2:default") == "u2:default"
    assert session_key(1, "u2:default") == "u1:u2:default"
    # 没有主体（CLI/离线调用）保持旧的裸键，避免改变非 API 路径的行为。
    assert session_key(None, "s1") == "s1"


def _prepared(question: str, sid: str) -> dict:
    return {
        "route": "rag",
        "question": question,
        "context": [],
        "messages": [],
        "sources": [],
        "sid": sid,
        "chunk_hashes": [],
        "weights": {},
        "stage_timings": {},
        "decision_metadata": {},
    }


def _build_pipeline(tmp_path):
    """构造一个只走内存/临时库的 pipeline，索引与外部依赖全部打桩。"""
    from rag.pipeline import RAGPipeline

    with patch("rag.pipeline.Retriever"), patch("rag.pipeline.Reranker"):
        pipeline = RAGPipeline(
            kb_id="rag_docs",
            user_db=MagicMock(),
            memory_db_path=str(tmp_path / "memory.db"),
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )
    pipeline.tracker = MagicMock()
    return pipeline


def _prepare_from_memory(pipeline):
    """让 _prepare_context 用真实记忆构建 messages，从而能观察跨用户泄露。"""

    def prepare(question, sid, *_args, **_kwargs):
        prepared = _prepared(question, sid)
        prepared["messages"] = pipeline.memory.build_messages(sid, question, [])
        return prepared, None

    return prepare


def test_default_session_memory_is_isolated_between_users(tmp_path):
    """两个用户都不传 session_id 时，缺省会话不能互见，也不能共享同一个键。"""
    pipeline = _build_pipeline(tmp_path)
    alice = {"id": 101, "permission_level": 1, "is_admin": False}
    bob = {"id": 202, "permission_level": 1, "is_admin": False}
    pipeline._prepare_context = MagicMock(side_effect=_prepare_from_memory(pipeline))

    with patch(
        "rag.pipeline.generate",
        side_effect=["alice 的私有回答", "bob 的回答", "alice 的第二轮回答"],
    ) as generate:
        pipeline.query("alice 的私有问题", user=alice)
        pipeline.query("bob 的问题", user=bob)
        pipeline.query("alice 的追问", user=alice)

    # 每次生成前，pipeline 交给 _prepare_context 的 session 已经带主体前缀。
    assert pipeline._prepare_context.call_args_list[0].args[1] == "u101:default"
    assert pipeline._prepare_context.call_args_list[1].args[1] == "u202:default"
    assert pipeline._prepare_context.call_args_list[2].args[1] == "u101:default"

    bob_prompt = "\n".join(m["content"] for m in generate.call_args_list[1].args[0])
    assert "alice 的私有问题" not in bob_prompt
    assert "alice 的私有回答" not in bob_prompt

    alice_second_prompt = "\n".join(m["content"] for m in generate.call_args_list[2].args[0])
    assert "alice 的私有问题" in alice_second_prompt
    assert "alice 的私有回答" in alice_second_prompt
    assert "bob 的问题" not in alice_second_prompt

    assert [m.content for m in pipeline.memory.get_recent_messages("u101:default")] == [
        "alice 的私有问题",
        "alice 的私有回答",
        "alice 的追问",
        "alice 的第二轮回答",
    ]
    assert [m.content for m in pipeline.memory.get_recent_messages("u202:default")] == [
        "bob 的问题",
        "bob 的回答",
    ]
    # 不再有任何请求写进全局 default 命名空间。
    assert pipeline.memory.get_recent_messages("default") == []
    pipeline.close()


def test_named_session_from_another_user_does_not_leak(tmp_path):
    """A 传 B 用过的 session_id：键被主体前缀隔开，B 的历史不进入 A 的 prompt。"""
    pipeline = _build_pipeline(tmp_path)
    alice = {"id": 101, "permission_level": 1, "is_admin": False}
    bob = {"id": 202, "permission_level": 1, "is_admin": False}
    pipeline._prepare_context = MagicMock(side_effect=_prepare_from_memory(pipeline))

    with patch("rag.pipeline.generate", side_effect=["B 的秘密回答", "A 的回答"]) as generate:
        pipeline.query("B 的秘密问题", session_id="team-room", user=bob)
        pipeline.query("A 的问题", session_id="team-room", user=alice)

    alice_prompt = "\n".join(m["content"] for m in generate.call_args_list[1].args[0])
    assert "B 的秘密问题" not in alice_prompt
    assert "B 的秘密回答" not in alice_prompt
    assert [m.content for m in pipeline.memory.get_recent_messages("u101:team-room")] == [
        "A 的问题",
        "A 的回答",
    ]
    assert [m.content for m in pipeline.memory.get_recent_messages("u202:team-room")] == [
        "B 的秘密问题",
        "B 的秘密回答",
    ]
    pipeline.close()


def test_sync_query_rejects_another_users_conversation_before_generation(
    isolated_api, make_user
):
    """同步 /query：B 的 conversation_id 在生成前 404，pipeline 一次都不被调用。"""
    _, alice_headers = make_user("alice")
    bob, _ = make_user("bob")
    db = isolated_api.app.state.user_db
    bob_conversation = db.create_conversation(bob["id"], "", "file")
    pipeline = MagicMock()

    with patch("rag.routers.query.get_pipeline", return_value=pipeline):
        response = isolated_api.post(
            "/query",
            headers=alice_headers,
            json={"question": "偷偷读一下", "conversation_id": bob_conversation},
        )

    assert response.status_code == 404
    pipeline.query.assert_not_called()


def test_stream_query_rejects_another_users_conversation_before_generation(
    isolated_api, make_user
):
    """SSE /query/stream：同样在生成前 404，不创建任何流式生成。"""
    _, alice_headers = make_user("alice")
    bob, _ = make_user("bob")
    db = isolated_api.app.state.user_db
    bob_conversation = db.create_conversation(bob["id"], "", "file")
    pipeline = MagicMock()

    with patch("rag.routers.query.get_pipeline", return_value=pipeline):
        response = isolated_api.post(
            "/query/stream",
            headers=alice_headers,
            json={"question": "偷偷读一下", "conversation_id": bob_conversation},
        )

    assert response.status_code == 404
    pipeline.query_stream.assert_not_called()


def test_owner_conversation_still_works_and_persists(isolated_api, make_user):
    """正向用例：自己的 conversation_id 仍能正常生成并落库（回归保护）。"""
    alice, alice_headers = make_user("alice")
    db = isolated_api.app.state.user_db
    conversation = db.create_conversation(alice["id"], "", "file")
    pipeline = MagicMock()
    pipeline.query.return_value = SimpleNamespace(answer="正式回答", sources=[])

    with patch("rag.routers.query.get_pipeline", return_value=pipeline):
        response = isolated_api.post(
            "/query",
            headers=alice_headers,
            json={"question": "年假有几天", "conversation_id": conversation},
        )

    assert response.status_code == 200
    assert response.json()["answer"] == "正式回答"
    assert pipeline.query.call_args.kwargs["session_id"] == f"conv_{conversation}"
    messages = db.get_messages(conversation, alice["id"])
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "正式回答"


def test_build_messages_still_accepts_a_namespaced_key(tmp_path):
    """DialogueMemory 本身仍按不透明键读写；命名空间由 pipeline 负责。"""
    from rag.memory import DialogueMemory

    memory = DialogueMemory(str(tmp_path / "memory.db"))
    try:
        memory.add_message("u7:s1", "user", "问题")
        memory.add_message("u7:s1", "assistant", "回答")
        memory.add_message("u8:s1", "user", "另一个问题")

        messages = memory.build_messages("u7:s1", "下一问", [Chunk(text="ctx", doc_name="d", chunk_index=0)])
        contents = [message["content"] for message in messages]
        assert any(content == "问题" for content in contents)
        assert all("另一个问题" not in content for content in contents)
    finally:
        memory.close()
