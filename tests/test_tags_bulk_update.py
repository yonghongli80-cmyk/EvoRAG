from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_add_tags_to_file_reads_and_updates_points_in_batches():
    from rag.routers import documents

    client = MagicMock()
    client.collection_exists.return_value = True
    first = MagicMock(id="id1")
    second = MagicMock(id="id2")
    client.scroll.return_value = ([first, second], None)
    old_first = MagicMock(payload={"tags": ["old_tag"]})
    old_second = MagicMock(payload={"tags": ["shared"]})
    client.retrieve.return_value = [old_first, old_second]

    with patch("rag.permissions.check_doc_permission"), patch(
        "rag.vector_store._get_client", return_value=client
    ):
        # 端点现在用 `user_dict: dict = Depends(require_user)` 接收主体，
        # 不再自己解析 token，因此直接注入用户而不 patch _get_current_user。
        result = await documents.add_tags_to_file(
            "test.txt", ["shared", "new_tag"], user_dict={"id": 1}
        )

    assert result["points_updated"] == 2
    client.retrieve.assert_called_once_with(
        collection_name="rag_docs",
        ids=["id1", "id2"],
        with_payload=True,
    )
    client.set_payload.assert_called_once_with(
        collection_name="rag_docs",
        payload={"tags": ["new_tag", "old_tag", "shared"]},
        points=["id1", "id2"],
    )
