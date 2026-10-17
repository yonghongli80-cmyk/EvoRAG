"""LLMProvider implementation with dedicated CircuitBreaker and AsyncOpenAI client."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncGenerator

from openai import AsyncOpenAI, OpenAI

from rag.llm_pool.models import LLMProviderConfig
from rag.resilience import CircuitBreaker

logger = logging.getLogger(__name__)


class LLMProvider:
    """Encapsulates a single LLM Provider (endpoint, model, credentials, and circuit breaker)."""

    def __init__(self, config: LLMProviderConfig) -> None:
        self.config = config
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
        self._async_client: AsyncOpenAI | None = None
        self._sync_client: OpenAI | None = None
        self._client_lock = threading.Lock()

    def default_headers(self) -> dict[str, str] | None:
        """Return custom authentication headers if configured, otherwise None."""
        header = self.config.auth_header.strip()
        if not header:
            return None
        return {header: self.config.api_key}

    def _get_async_client(self) -> AsyncOpenAI:
        if self._async_client is None:
            with self._client_lock:
                if self._async_client is None:
                    self._async_client = AsyncOpenAI(
                        api_key=self.config.api_key or "sk-placeholder",
                        base_url=self.config.base_url,
                        timeout=self.config.timeout_seconds,
                        default_headers=self.default_headers(),
                    )
        return self._async_client

    def _get_sync_client(self) -> OpenAI:
        if self._sync_client is None:
            with self._client_lock:
                if self._sync_client is None:
                    self._sync_client = OpenAI(
                        api_key=self.config.api_key or "sk-placeholder",
                        base_url=self.config.base_url,
                        timeout=self.config.timeout_seconds,
                        default_headers=self.default_headers(),
                    )
        return self._sync_client

    def is_available(self) -> bool:
        """Check if provider is administratively enabled and healthy."""
        return self.config.is_active and self.circuit_breaker.allow_request()

    async def generate_stream(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_attempts: int = 1,
    ) -> AsyncGenerator[str, None]:
        """Stream generated tokens asynchronously with isolated circuit breaker protection."""
        if not self.circuit_breaker.allow_request():
            yield "系统繁忙，请稍后重试。"
            return

        client = self._get_async_client()
        clean_msgs = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]

        extra_body = None
        # DeepSeek 系列模型默认关闭 thinking 标签以防止格式异常
        if "deepseek" in self.config.model.lower() or "deepseek" in self.config.provider_id.lower():
            extra_body = {"thinking": {"type": "disabled"}}

        create_kwargs: dict = {
            "model": self.config.model,
            "messages": clean_msgs,
            "temperature": temperature,
            "stream": True,
        }
        if extra_body:
            create_kwargs["extra_body"] = extra_body

        has_yielded = False
        try:
            stream = await client.chat.completions.create(**create_kwargs)
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    has_yielded = True
                    yield chunk.choices[0].delta.content
            self.circuit_breaker.record_success()
        except Exception:
            self.circuit_breaker.record_failure()
            if has_yielded:
                # 已经推流部分内容，禁止重试避免内容重复
                raise
            raise

    def generate(self, messages: list[dict], temperature: float = 0.3) -> str:
        """Synchronously generate response."""
        if not self.circuit_breaker.allow_request():
            return "系统繁忙，请稍后重试。"

        client = self._get_sync_client()
        clean_msgs = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]

        extra_body = None
        if "deepseek" in self.config.model.lower() or "deepseek" in self.config.provider_id.lower():
            extra_body = {"thinking": {"type": "disabled"}}

        create_kwargs: dict = {
            "model": self.config.model,
            "messages": clean_msgs,
            "temperature": temperature,
        }
        if extra_body:
            create_kwargs["extra_body"] = extra_body

        try:
            response = client.chat.completions.create(**create_kwargs)
            result = response.choices[0].message.content
            self.circuit_breaker.record_success()
            return result or "（模型未返回内容）"
        except Exception:
            self.circuit_breaker.record_failure()
            raise
