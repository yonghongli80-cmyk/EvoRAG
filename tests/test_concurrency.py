import threading
import time
from unittest.mock import MagicMock, patch

from rag.concurrency import ReadWriteLock


def test_read_write_lock_allows_concurrent_reads():
    lock = ReadWriteLock()
    results = []

    def reader():
        with lock.read():
            time.sleep(0.05)
            results.append("read")

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 3


def test_read_write_lock_enforces_write_mutual_exclusion():
    lock = ReadWriteLock()
    interleaved = []

    def writer_a():
        with lock.write():
            interleaved.append("a_start")
            time.sleep(0.1)
            interleaved.append("a_end")

    def writer_b():
        time.sleep(0.1)
        with lock.write():
            interleaved.append("b_start")
            time.sleep(0.1)
            interleaved.append("b_end")

    t1 = threading.Thread(target=writer_a)
    t2 = threading.Thread(target=writer_b)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # a_end must come before b_start (no interleaving)
    assert interleaved.index("a_end") < interleaved.index("b_start")


def test_read_write_lock_blocks_write_during_read():
    lock = ReadWriteLock()
    order = []

    def reader():
        with lock.read():
            order.append("read_start")
            time.sleep(0.1)
            order.append("read_end")

    def writer():
        time.sleep(0.02)
        with lock.write():
            order.append("write")

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # write should happen after read_end
    assert order.index("write") > order.index("read_end")


def test_vector_store_uses_lock_for_writes():
    """vector_store.add() should acquire write lock."""
    from rag.models import Chunk
    from rag.vector_store import add

    with patch("rag.vector_store._get_client") as mock_get_client, \
         patch("rag.vector_store._write_lock") as mock_write_lock, \
         patch("rag.vector_store._invalidate_bm25"):
        mock_client = MagicMock()
        mock_client.collection_exists.return_value = True
        mock_get_client.return_value = mock_client

        chunks = [Chunk(text="test", doc_name="t.txt", chunk_index=0)]
        add(chunks, [[0.1] * 1024])
        mock_write_lock.write.assert_called_once_with()
        mock_client.upsert.assert_called_once()
