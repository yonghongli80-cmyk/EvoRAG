import random
import threading

from openai import AsyncOpenAI, OpenAI

from config import settings
from rag.resilience import CircuitBreaker, retry

client = None
_client_lock = threading.Lock()

_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)


def deepseek_default_headers() -> dict[str, str] | None:
    """Authentication headers for providers that do not accept Bearer tokens.

    Returns ``None`` for the standard OpenAI-compatible scheme so the SDK's own
    ``Authorization: Bearer`` stays the only credential carrier.
    """
    header = settings.deepseek_auth_header.strip()
    if not header:
        return None
    return {header: settings.deepseek_api_key}


@retry(max_attempts=3, backoff_base=1.0, retryable_exceptions=(TimeoutError, ConnectionError, OSError))
def generate(messages: list[dict], temperature: float | None = None) -> str:
    global client
    if temperature is None:
        temperature = settings.generation_temperature
    if not _breaker.allow_request():
        return "系统繁忙，请稍后重试。"
    if client is None:
        with _client_lock:
            if client is None:
                client = OpenAI(
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_base_url,
                    timeout=30.0,
                    default_headers=deepseek_default_headers(),
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


class StructuredOutputError(Exception):
    """Raised when a model response is not a single valid JSON object."""

    def __init__(self, detail: str = "structured_output_invalid"):
        self.detail = detail
        super().__init__(detail)


def _extract_json_object(text: str) -> dict:
    import json

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json object in response")
    return json.loads(text[start : end + 1])


def generate_structured(messages: list[dict], response_model: type, temperature: float = 0.0):
    """Ask the model for exactly one JSON object and validate it strictly.

    The response is never evaluated as Python or repaired with ``eval``; any
    parse/validation failure raises :class:`StructuredOutputError`.
    """
    import json

    from pydantic import ValidationError

    instruction = {
        "role": "system",
        "content": (
            "只输出一个合法的 JSON 对象，不要输出任何其他文字、注释或 Markdown 代码块。"
            "不要使用 eval 或执行代码。"
        ),
    }
    text = generate([instruction, *messages], temperature=temperature)
    try:
        obj = _extract_json_object(text)
    except (ValueError, json.JSONDecodeError):
        raise StructuredOutputError() from None
    try:
        return response_model.model_validate(obj)
    except ValidationError:
        raise StructuredOutputError() from None


_async_client = None
_async_client_lock = threading.Lock()


async def generate_stream(messages: list[dict], temperature: float | None = None, max_attempts: int = 3):
    """流式生成，逐个 yield token。连接失败时自动重试。"""
    global _async_client
    if temperature is None:
        temperature = settings.generation_temperature
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
                    default_headers=deepseek_default_headers(),
                )
    clean_msgs = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in messages]

    last_exc = None
    has_yielded = False
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
                    has_yielded = True
                    yield chunk.choices[0].delta.content
            _breaker.record_success()
            return  # 成功，退出重试循环
        except (TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            _breaker.record_failure()
            if has_yielded:
                # 已经向客户端发送了部分 token，重试会导致内容从头重复，必须直接抛出
                raise
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

