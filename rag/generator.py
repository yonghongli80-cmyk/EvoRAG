import random
import threading

from openai import AsyncOpenAI, OpenAI

from config import settings
from rag.resilience import CircuitBreaker, retry

client = None
_client_lock = threading.Lock()

_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)


@retry(max_attempts=3, backoff_base=1.0, retryable_exceptions=(TimeoutError, ConnectionError, OSError))
def generate(messages: list[dict], temperature: float = 0.3) -> str:
    global client
    if not _breaker.allow_request():
        return "系统繁忙，请稍后重试。"
    if client is None:
        with _client_lock:
            if client is None:
                client = OpenAI(
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_base_url,
                    timeout=30.0,
                )
    # Strip reasoning_content (DeepSeek thinking mode) to avoid API rejection
    clean_msgs = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]
    try:
        response = client.chat.completions.create(
            model=settings.deepseek_model,
            messages=clean_msgs,
            temperature=temperature,
            extra_body={"thinking": {"type": "disabled"}},
        )
        result = response.choices[0].message.content
        _breaker.record_success()
        return result or "（模型未返回内容）"
    except Exception:
        _breaker.record_failure()
        raise


_async_client = None
_async_client_lock = threading.Lock()


async def generate_stream(messages: list[dict], temperature: float = 0.3, max_attempts: int = 3):
    """流式生成，逐个 yield token。连接失败时自动重试。"""
    global _async_client
    if not _breaker.allow_request():
        yield "系统繁忙，请稍后重试。"
        return
    if _async_client is None:
        with _async_client_lock:
            if _async_client is None:
                _async_client = AsyncOpenAI(
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_base_url,
                    timeout=30.0,
                )
    clean_msgs = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]

    last_exc = None
    for attempt in range(max_attempts):
        try:
            stream = await _async_client.chat.completions.create(
                model=settings.deepseek_model,
                messages=clean_msgs,
                temperature=temperature,
                extra_body={"thinking": {"type": "disabled"}},
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
            _breaker.record_success()
            return  # 成功，退出重试循环
        except (TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            _breaker.record_failure()
            if attempt < max_attempts - 1:
                import asyncio
                delay = 1.0 * (2 ** attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)
            continue
        except Exception:
            _breaker.record_failure()
            raise

    # 所有重试都失败
    raise last_exc
