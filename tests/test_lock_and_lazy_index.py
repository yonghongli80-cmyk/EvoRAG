"""A-08：ReadWriteLock 不能饿死写者；BM25 索引构建不能卡在 KB 写锁里。

2026-10-22 审查：

* ``ReadWriteLock`` 让源源不断的读者一直插队：写者只能等"某一刻没有读者"，高并发读下
  永远轮不到它。修法是 writer-pending：有写者在等时，新读者排队。
* ``_get_or_create_pipeline`` 由调用方持写锁调用，而 ``RAGPipeline`` 构造会立刻从 Qdrant
  全量 scroll 并分词建 BM25——冷启动时整段时间都锁住所有知识库的读写。修法是把 BM25
  构建推迟到第一次检索，并用双检（double-checked）保证只建一次。
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from rag.concurrency import ReadWriteLock
from rag.retriever import Retriever

# ── writer-pending ──────────────────────────────────────────────────────────


def test_a_waiting_writer_blocks_new_readers():
    """读者退出后写者必须拿到锁——晚到的读者不得插队（防写者饥饿）。"""
    lock = ReadWriteLock()
    order: list[str] = []
    reader_holding = threading.Event()
    release_reader = threading.Event()

    def holder():
        with lock.read():
            order.append("reader_start")
            reader_holding.set()
            release_reader.wait(5)
        order.append("reader_end")

    def writer():
        reader_holding.wait(5)
        time.sleep(0.05)  # 先让写者进入等待
        with lock.write():
            order.append("writer")

    def late_reader():
        reader_holding.wait(5)
        time.sleep(0.15)  # 写者已经在等之后再来一个新读者
        with lock.read():
            order.append("late_reader")

    threads = [
        threading.Thread(target=holder),
        threading.Thread(target=writer),
        threading.Thread(target=late_reader),
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.25)
    release_reader.set()
    for thread in threads:
        thread.join(5)

    assert order.index("writer") < order.index("late_reader"), order


def test_reads_stay_concurrent_when_no_writer_is_waiting():
    """没有写者等待时，读者仍然并发（不能为了公平把读锁退化成互斥）。"""
    lock = ReadWriteLock()
    active = 0
    max_active = 0
    gate = threading.Event()
    started = []

    def reader():
        nonlocal active, max_active
        with lock.read():
            active += 1
            max_active = max(max_active, active)
            started.append(True)
            gate.wait(5)
            active -= 1

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    deadline = time.time() + 5
    while len(started) < 4 and time.time() < deadline:
        time.sleep(0.01)
    gate.set()
    for thread in threads:
        thread.join(5)

    assert max_active == 4


# ── 懒建 BM25（双检） ──────────────────────────────────────────────────────


@pytest.fixture
def bm25_env(tmp_path, monkeypatch):
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module

    db = str(tmp_path / "bm25.db")
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", db)
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", db)
    retriever_module.invalidate_bm25_cache()
    yield retriever_module
    retriever_module.invalidate_bm25_cache()


def _chunks():
    from rag.models import Chunk

    return [
        Chunk(text="年假制度 alpha", doc_name="a.md", chunk_index=0),
        Chunk(text="报销制度 beta", doc_name="b.md", chunk_index=1),
    ]


def test_retriever_construction_does_not_build_the_bm25_index(bm25_env):
    """构造成本必须与索引大小无关：BM25 推迟到第一次检索才建。"""
    with patch("rag.retriever._load_all_chunks", return_value=_chunks()) as load_all:
        retriever = Retriever([], collection_name="kb_lazy")

        assert retriever.chunks == []
        assert retriever.bm25 is None
        load_all.assert_not_called()

        retriever._ensure_bm25()

    load_all.assert_called_once_with("kb_lazy")
    assert retriever.bm25 is not None
    assert [chunk.doc_name for chunk in retriever.chunks] == ["a.md", "b.md"]


def test_first_use_builds_the_index_once_under_concurrency(bm25_env):
    """双检：并发的首次检索（不同 Retriever 实例）只能触发一次全量重建。"""
    build_calls = []

    def slow_load_all(collection_name):
        build_calls.append(collection_name)
        time.sleep(0.1)
        return _chunks()

    retrievers = [Retriever([], collection_name="kb_shared") for _ in range(4)]

    with patch("rag.retriever._load_all_chunks", side_effect=slow_load_all), \
         patch("rag.retriever.embed", lambda _texts: [[0.1] * 1024]), \
         patch("rag.retriever.search_collection", lambda *_a, **_k: []):
        threads = [
            threading.Thread(target=item.retrieve, args=("年假",), kwargs={"top_k": 2})
            for item in retrievers
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

    assert build_calls == ["kb_shared"]


def test_pipeline_construction_leaves_the_index_lazy(tmp_path, monkeypatch):
    """RAGPipeline(kb_id=...) 不再在 KB 写锁内建索引。"""
    import rag.bm25_store as bm25_store
    import rag.retriever as retriever_module
    from rag.pipeline import RAGPipeline

    db = str(tmp_path / "bm25.db")
    monkeypatch.setattr(bm25_store.settings, "bm25_db_path", db)
    monkeypatch.setattr(retriever_module.settings, "bm25_db_path", db)
    retriever_module.invalidate_bm25_cache()

    with patch("rag.pipeline.Retriever") as retriever_cls, \
         patch("rag.pipeline.Reranker"), \
         patch("rag.retriever._load_all_chunks", return_value=_chunks()) as load_all:
        retriever_cls.side_effect = lambda chunks, collection_name=None: Retriever(
            chunks, collection_name=collection_name
        )
        pipeline = RAGPipeline(
            kb_id="kb_pipeline",
            memory_db_path=":memory:",
            user_db=MagicMock(),
            feedback_processor=MagicMock(),
            gap_analyzer=MagicMock(),
        )
        try:
            load_all.assert_not_called()
            assert pipeline.retriever.bm25 is None
        finally:
            pipeline.close()

    retriever_module.invalidate_bm25_cache()
