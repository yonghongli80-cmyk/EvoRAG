"""容错层 — 重试、熔断、降级、超时、缓存。"""

from __future__ import annotations

import functools
import hashlib
import math
import random
import threading
import time

import openai


def retry(
    max_attempts: int = 3,
    backoff_base: float = 1.0,
    retryable_exceptions: tuple = (TimeoutError, ConnectionError, OSError, openai.APIStatusError),
    max_delay: float = 10.0,
):
    """重试装饰器，使用有上限的指数退避。"""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        delay = min(backoff_base * (2**attempt), max_delay)
                        time.sleep(delay)
            raise last_exc

        return wrapper

    return decorator


class CircuitBreaker:
    """熔断器：连续失败 N 次→熔断，一段时间后半开探测。"""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = "closed"  # closed → open → half_open → closed
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._probe_admitted = False
        self._lock = threading.Lock()

    def record_failure(self):
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._probe_admitted = False
            if self._failure_count >= self.failure_threshold:
                self.state = "open"

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._probe_admitted = False
            self.state = "closed"

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self.state = "half_open"
                    self._probe_admitted = True
                    return True
                return False
            # half_open: only allow one probe
            if not self._probe_admitted:
                self._probe_admitted = True
                return True
            return False


class BloomFilter:
    """简单布隆过滤器，防缓存穿透。"""

    def __init__(self, capacity: int = 10000, error_rate: float = 0.01):
        self.capacity = capacity
        self.error_rate = error_rate
        # Optimal bit array size: m = -capacity * ln(error_rate) / (ln2)^2
        self.size = max(64, int(-capacity * math.log(error_rate) / (math.log(2) ** 2)))
        self.hash_count = max(1, int(self.size / capacity * 0.693))
        self._bits = bytearray((self.size + 7) // 8)
        self._lock = threading.Lock()

    def _hashes(self, key: str):
        h1 = int(hashlib.md5(key.encode()).hexdigest(), 16)
        h2 = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        for i in range(self.hash_count):
            yield (h1 + i * h2) % self.size

    def add(self, key: str):
        with self._lock:
            for pos in self._hashes(key):
                self._bits[pos // 8] |= 1 << (pos % 8)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return all(self._bits[pos // 8] & (1 << (pos % 8)) for pos in self._hashes(key))


class ResultCache:
    """缓存：防穿透（布隆）+ 防雪崩（TTL 抖动）+ 防热点（热点永不过期）。"""

    def __init__(self, ttl_seconds: float = 300.0, max_size: int = 1000, hot_threshold: int = 10):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self.hot_threshold = hot_threshold
        self._store: dict[str, tuple[str, float]] = {}  # key -> (answer, expire_time)
        self._access_count: dict[str, int] = {}
        self._stale_keys: set[str] = set()
        self._bloom = BloomFilter(capacity=max_size * 2)
        self._lock = threading.Lock()

    def _key(self, question: str) -> str:
        return hashlib.md5(question.encode()).hexdigest()

    def get(self, question: str):
        key = self._key(question)
        if key not in self._bloom:
            return None
        with self._lock:
            self._access_count[key] = self._access_count.get(key, 0) + 1
            if key in self._store:
                answer, expire_time = self._store[key]
                if time.time() < expire_time:
                    return answer
                # Expired — check if hot key
                if self._access_count[key] >= self.hot_threshold:
                    self._stale_keys.add(key)
                    return answer  # Return stale value for hot keys
                del self._store[key]
        return None

    def set(self, question: str, answer: str):
        key = self._key(question)
        self._bloom.add(key)
        with self._lock:
            if len(self._store) >= self.max_size:
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest_key]
            jitter = random.uniform(-0.1, 0.1) * self.ttl
            self._store[key] = (answer, time.time() + self.ttl + jitter)

    def get_stale_keys(self) -> set[str]:
        """返回需要刷新的热点 key 集合。

        热点 key 在过期后仍返回旧值（serve-stale），防止缓存击穿。
        调用方可根据返回的 key 集合决定是否触发异步刷新。
        注意：当前 pipeline 未集成异步刷新，热点 key 持续返回旧值直到被 set() 覆盖。
        """
        with self._lock:
            stale = self._stale_keys.copy()
            self._stale_keys.clear()
        return stale
