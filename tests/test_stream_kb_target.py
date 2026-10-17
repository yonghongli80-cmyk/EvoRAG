from collections.abc import AsyncIterator


def test_stream_query_uses_explicit_authorized_kb(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_hr", "HR", owner["id"], "private")
    constructed = []

    class FakePipeline:
        def __init__(self, kb_id, user_db, **kwargs):
            # 工厂会按需再注入记录器/灰度控制器等协作器；本用例只断言"用了哪个库"。
            constructed.append((kb_id, user_db))

        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("rag.pipeline.RAGPipeline", FakePipeline)

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "leave policy", "kb_id": "kb_hr"},
    )

    assert response.status_code == 200
    assert constructed == [("kb_hr", db)]


def test_stream_query_rejects_unshared_kb_before_pipeline_creation(
    isolated_api, make_user, monkeypatch
):
    owner, _ = make_user("owner")
    _, headers = make_user("outsider")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_hr", "HR", owner["id"], "private")
    constructed = []
    monkeypatch.setattr(
        "rag.pipeline.RAGPipeline",
        lambda **kwargs: constructed.append(kwargs),
    )

    response = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "leave policy", "kb_id": "kb_hr"},
    )

    assert response.status_code == 403
    assert constructed == []


def test_stream_query_caches_and_reuses_pipeline(
    isolated_api, make_user, monkeypatch
):
    owner, headers = make_user("owner_reuse")
    db = isolated_api.app.state.user_db
    db.create_kb_metadata("kb_finance", "Finance", owner["id"], "private")
    constructed = []

    class FakePipeline:
        def __init__(self, kb_id, user_db, **kwargs):
            constructed.append((kb_id, user_db))

        async def query_stream(self, question, **kwargs) -> AsyncIterator[str]:
            yield 'data: {"type":"done"}\n\n'

    monkeypatch.setattr("rag.pipeline.RAGPipeline", FakePipeline)

    res1 = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "q1", "kb_id": "kb_finance"},
    )
    assert res1.status_code == 200
    assert len(constructed) == 1

    res2 = isolated_api.post(
        "/query/stream",
        headers=headers,
        json={"question": "q2", "kb_id": "kb_finance"},
    )
    assert res2.status_code == 200
    assert len(constructed) == 1

