"""Small process-local login throttle for the single-process desktop service."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class LoginThrottle:
    def __init__(self, limit: int, window_seconds: int, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_keys = max(1, int(max_keys))
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune_all(self, now: float) -> None:
        cutoff = now - self.window_seconds
        expired = [
            key for key, attempts in self._attempts.items()
            if not attempts or attempts[-1] <= cutoff
        ]
        for key in expired:
            self._attempts.pop(key, None)
        while len(self._attempts) > self.max_keys:
            self._attempts.pop(next(iter(self._attempts)))

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._attempts[key]
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._attempts.pop(key, None)
            return deque()
        return attempts

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return len(self._prune(key, now)) >= self.limit

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._attempts) >= self.max_keys:
                self._prune_all(now)
            attempts = self._prune(key, now)
            if key not in self._attempts:
                self._attempts[key] = attempts
            attempts.append(now)
            while len(self._attempts) > self.max_keys:
                self._attempts.pop(next(iter(self._attempts)))

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
