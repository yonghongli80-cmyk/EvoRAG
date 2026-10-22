"""并发层 — 读写锁、连接池包装。"""

import threading
from contextlib import contextmanager


class ReadWriteLock:
    """读写锁：多个读可并发，写独占。

    写者优先（writer-pending）：一旦有写者在等待，新读者就让路。没有这条规则时，连续的
    读者可以一直插队——写者只能碰运气等到"某一瞬间没有读者"，高并发读下会长期拿不到锁
    （2026-10-22 审查 A-08：写者饥饿）。代价是写者排队期间读延迟升高，这是刻意的取舍：
    写操作（重建索引、发布管线）等得越久，读到旧状态的窗口就越长。
    """

    def __init__(self):
        self._read_ready = threading.Condition(threading.Lock())
        self._readers = 0
        self._writing = False
        self._writers_waiting = 0

    @contextmanager
    def read(self):
        with self._read_ready:
            while self._writing or self._writers_waiting:
                self._read_ready.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._read_ready:
                self._readers -= 1
                if self._readers == 0:
                    self._read_ready.notify_all()

    @contextmanager
    def write(self):
        with self._read_ready:
            self._writers_waiting += 1
            try:
                while self._readers > 0 or self._writing:
                    self._read_ready.wait()
                self._writing = True
            finally:
                self._writers_waiting -= 1
        try:
            yield
        finally:
            with self._read_ready:
                self._writing = False
                self._read_ready.notify_all()
