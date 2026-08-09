"""Redis 缓存模块。

替换内存缓存，支持持久化和多进程共享。
Redis 不可用时自动降级到内存缓存。
"""

import hashlib
import json
import logging
import threading
import time
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


class RedisCache:
    """Redis 缓存，带降级到内存缓存的能力。"""

    def __init__(self, ttl_seconds: int = 300, max_size: int = 1000):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._redis = None
        self._fallback: dict[str, tuple[str, float]] = {}  # 内存降级缓存
        self._lock = threading.Lock()
        self._connected = False
        self._redis_retry_after = 0.0
        self._redis_cooldown_seconds = 30.0

    def _get_redis(self):
        """延迟初始化 Redis 连接。"""
        if self._redis is None:
            if time.monotonic() < self._redis_retry_after:
                return None
            try:
                import redis
                self._redis = redis.from_url(settings.redis_url, decode_responses=True)
                # 测试连接
                self._redis.ping()
                self._connected = True
                self._redis_retry_after = 0.0
                logger.info("Redis 连接成功: %s", settings.redis_url)
            except Exception as e:
                logger.warning("Redis 连接失败，降级到内存缓存: %s", e)
                self._redis = None
                self._connected = False
                self._redis_retry_after = time.monotonic() + self._redis_cooldown_seconds
        return self._redis

    def _key(self, question: str, kb_id: str = "rag_docs", user_id: str = "__anon__") -> str:
        """生成缓存 key，纳入知识库与用户维度，避免跨用户/跨库串答案。"""
        return f"rag:query:{kb_id}:{user_id}:{hashlib.md5(question.encode()).hexdigest()}"

    def get(self, question: str, kb_id: str = "rag_docs", user_id: str = "__anon__") -> dict | None:
        """获取缓存。未命中返回 None；命中返回 {"answer": ..., "sources": [...]}。"""
        key = self._key(question, kb_id, user_id)
        redis = self._get_redis()

        if redis:
            try:
                data = redis.get(key)
                if data:
                    return json.loads(data)
                return self._fallback_get(key)
            except Exception as e:
                logger.warning("Redis get 失败，降级到内存: %s", e)
                return self._fallback_get(key)
        else:
            return self._fallback_get(key)

    def set(self, question: str, answer: str, kb_id: str = "rag_docs", user_id: str = "__anon__", sources: list | None = None) -> None:
        """设置缓存，带 TTL。sources 一并存储，供缓存命中时做权限过滤。"""
        key = self._key(question, kb_id, user_id)
        data = json.dumps({"answer": answer, "sources": sources or []}, ensure_ascii=False)

        redis = self._get_redis()
        if redis:
            try:
                redis.set(key, data, ex=self.ttl)
                return
            except Exception as e:
                logger.warning("Redis set 失败，降级到内存: %s", e)

        self._fallback_set(key, data)

    def delete(self, question: str, kb_id: str = "rag_docs", user_id: str = "__anon__") -> None:
        """删除缓存。"""
        key = self._key(question, kb_id, user_id)
        redis = self._get_redis()
        if redis:
            try:
                redis.delete(key)
                return
            except Exception:
                pass
        self._fallback_delete(key)

    def clear(self) -> None:
        """清空 RAG 缓存（只删除 rag:query:* 前缀的 key，不影响其他数据）。"""
        redis = self._get_redis()
        if redis:
            try:
                # 只删除 rag:query:* 前缀的 key，不用 flushdb
                cursor = 0
                while True:
                    cursor, keys = redis.scan(cursor=cursor, match="rag:query:*", count=100)
                    if keys:
                        redis.delete(*keys)
                    if cursor == 0:
                        break
                return
            except Exception:
                pass
        with self._lock:
            self._fallback.clear()

    def _fallback_get(self, key: str) -> dict | None:
        """内存降级获取。"""
        with self._lock:
            if key in self._fallback:
                data, expire_time = self._fallback[key]
                if time.time() < expire_time:
                    return json.loads(data)
                else:
                    del self._fallback[key]
        return None

    def _fallback_set(self, key: str, data: str) -> None:
        """内存降级设置。"""
        with self._lock:
            # 容量限制
            if len(self._fallback) >= self.max_size:
                # 删除最旧的
                oldest_key = min(self._fallback, key=lambda k: self._fallback[k][1])
                del self._fallback[oldest_key]
            self._fallback[key] = (data, time.time() + self.ttl)

    def _fallback_delete(self, key: str) -> None:
        """内存降级删除。"""
        with self._lock:
            self._fallback.pop(key, None)

    @property
    def is_redis_connected(self) -> bool:
        """是否连接到 Redis。"""
        self._get_redis()
        return self._connected
