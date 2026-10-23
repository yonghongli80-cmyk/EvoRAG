"""Tests for BatchImporter document versioning alignment (ALIGN-01)."""

import csv
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rag.batch_importer import BatchImporter
from rag.retriever import Retriever
from rag.user_db import UserDB
from rag.vector_store import _get_client
from rag.versioned_retrieval import retrieve_current_then_history


@pytest.fixture(autouse=True)
def offline_embeddings(monkeypatch):
    """Keep versioned import and retrieval on the real embed path without network IO."""
    import rag.embedder
    from config import settings

    class OfflineEmbeddings:
        def create(self, *, model, input):
            if model != settings.bailian_embed_model:
                raise AssertionError(f"unexpected embedding model: {model}")
            vector = [0.1] + [0.0] * (settings.embed_dimension - 1)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=vector.copy()) for _ in input]
            )

    with rag.embedder._cache_lock:
        previous_cache = OrderedDict(rag.embedder._cache)
    rag.embedder.clear_cache()
    monkeypatch.setattr(rag.embedder, "client", SimpleNamespace(embeddings=OfflineEmbeddings()))

    def reject_client_construction(**kwargs):
        raise AssertionError("versioning tests must not construct a real embedding client")

    monkeypatch.setattr(rag.embedder, "OpenAI", reject_client_construction)
    yield

    with rag.embedder._cache_lock:
        rag.embedder._cache.clear()
        rag.embedder._cache.update(previous_cache)


def test_process_csv_creates_initial_version_and_payload(tmp_path, sandbox_api_storage):
    db: UserDB = sandbox_api_storage
    user_id = db.create_user("alice", "Password-123!")
    user = db.get_user_by_id(user_id)

    csv_path = tmp_path / "faq.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["什么是 mTLS？", "mTLS 是双向 TLS 认证"])
        writer.writerow(["什么是 Raft？", "Raft 是一致性协议"])

    importer = BatchImporter()
    result = importer.process_csv(
        str(csv_path),
        mode="qa_pair",
        kb_id="rag_docs",
        user=user,
        user_db=db,
        config={"question_col": "问题", "answer_col": "答案"},
    )

    assert result["status"] == "imported"
    assert result["chunks"] == 2
    assert result["document_id"] > 0
    assert result["version_id"] > 0
    assert result["version_no"] == 1

    # Verify version tree in UserDB
    perm = db.get_document_permission_by_id(result["document_id"])
    assert perm is not None
    assert perm["current_version_id"] == result["version_id"]

    version = db.get_document_version_by_id(result["version_id"])
    assert version is not None
    assert version["version_no"] == 1
    assert version["status"] == "current"
    assert version["validity_status"] == "valid"
    assert version["uploaded_by"] == user_id

    # Verify vector payload in Qdrant
    client = _get_client()
    points, _ = client.scroll(
        collection_name="rag_docs",
        with_payload=True,
        limit=10,
    )
    assert len(points) == 2
    for pt in points:
        assert pt.payload.get("version_id") == result["version_id"]
        assert pt.payload.get("document_version_id") == result["version_id"]
        assert pt.payload.get("document_id") == result["document_id"]
        assert pt.payload.get("is_current") is True


def test_import_files_multiple_csv_version_trees(tmp_path, sandbox_api_storage):
    db: UserDB = sandbox_api_storage
    user_id = db.create_user("bob", "Password-123!")
    user = db.get_user_by_id(user_id)

    csv1 = tmp_path / "net_faq.csv"
    with open(csv1, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["什么是 BGP？", "BGP 是边界网关协议"])

    csv2 = tmp_path / "os_faq.csv"
    with open(csv2, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["什么是 POSIX？", "POSIX 是可移植操作系统接口"])

    importer = BatchImporter()
    results = importer.import_files(
        [str(csv1), str(csv2)],
        mode="qa_pair",
        kb_id="rag_docs",
        user=user,
        user_db=db,
    )

    assert len(results) == 2
    doc_ids = [r["document_id"] for r in results]
    assert len(set(doc_ids)) == 2  # distinct documents

    for res in results:
        perm = db.get_document_permission_by_id(res["document_id"])
        assert perm["current_version_id"] == res["version_id"]
        v = db.get_document_version_by_id(res["version_id"])
        assert v["version_no"] == 1
        assert v["status"] == "current"


def test_batch_imported_data_retrievable_in_version_isolated_mode(tmp_path, sandbox_api_storage):
    db: UserDB = sandbox_api_storage
    user_id = db.create_user("charlie", "Password-123!")
    user = db.get_user_by_id(user_id)

    csv_path = tmp_path / "isolated_faq.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["什么是 Paxos？", "Paxos 是一种经典的分布式共识算法"])

    importer = BatchImporter()
    result = importer.process_csv(
        str(csv_path),
        mode="qa_pair",
        kb_id="rag_docs",
        user=user,
        user_db=db,
    )

    # Now perform versioned retrieval
    retriever = Retriever(collection_name="rag_docs")
    retrieved = retrieve_current_then_history(
        retriever=retriever,
        query="Paxos 分布式共识",
        top_k=3,
        user=user,
        user_db=db,
        kb_id="rag_docs",
    )

    assert len(retrieved) > 0
    assert any("Paxos" in c.text for c in retrieved)
    assert all(c.document_version_id == result["version_id"] for c in retrieved if "Paxos" in c.text)


def test_batch_importer_rollback_on_failure(tmp_path, sandbox_api_storage):
    db: UserDB = sandbox_api_storage
    user_id = db.create_user("david", "Password-123!")
    user = db.get_user_by_id(user_id)

    csv_path = tmp_path / "broken.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["问题", "答案"])
        writer.writerow(["测试回滚", "回滚内容"])

    importer = BatchImporter()

    with (
        patch("rag.vector_store.add_to_collection", side_effect=RuntimeError("Vector indexing failure")),
        pytest.raises(RuntimeError),
    ):
        importer.process_csv(
            str(csv_path),
            mode="qa_pair",
            kb_id="rag_docs",
            user=user,
            user_db=db,
            doc_name="broken.csv",
        )

    # Ensure permission and version records were rolled back
    perm = db.get_document_permission("broken.csv", "rag_docs")
    assert perm is None
