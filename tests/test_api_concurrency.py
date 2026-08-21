"""api.py 并发测试 — 验证读写锁允许并发查询。"""

import threading
import time


def test_pipeline_lock_is_read_write_lock():
    """api.py 的 _pipeline_lock 应该是 ReadWriteLock 而非 threading.Lock。"""
    import rag.api as api_module
    from rag.concurrency import ReadWriteLock

    assert isinstance(api_module._pipeline_lock, ReadWriteLock), (
        f"_pipeline_lock 是 {type(api_module._pipeline_lock).__name__}，应该是 ReadWriteLock"
    )


def test_concurrent_queries_allowed():
    """多个查询可以并发执行（read lock 不互斥）。"""
    from rag.concurrency import ReadWriteLock

    lock = ReadWriteLock()
    results = []
    barrier = threading.Barrier(3, timeout=5)

    def reader(name):
        with lock.read():
            barrier.wait()  # 3 个 reader 同时到达
            results.append(f"{name}_entered")

    threads = [threading.Thread(target=reader, args=(f"t{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 3, f"3 个 reader 应该都能并发进入，实际只有 {len(results)} 个"


def test_write_lock_blocks_readers():
    """写锁期间读操作被阻塞。"""
    from rag.concurrency import ReadWriteLock

    lock = ReadWriteLock()
    order = []

    def writer():
        with lock.write():
            order.append("write_start")
            time.sleep(0.2)
            order.append("write_end")

    def reader():
        time.sleep(0.05)  # 让 writer 先拿到锁
        with lock.read():
            order.append("read_entered")

    t_write = threading.Thread(target=writer)
    t_read = threading.Thread(target=reader)
    t_write.start()
    t_read.start()
    t_write.join(timeout=5)
    t_read.join(timeout=5)

    # reader 应该在 writer 结束后才能进入
    assert order == ["write_start", "write_end", "read_entered"], f"顺序不对: {order}"
