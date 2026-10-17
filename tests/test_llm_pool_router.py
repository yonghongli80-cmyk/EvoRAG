"""TDD unit tests for LLMPoolRouter (Load balancing, 429 failover, and parallel arena streaming)."""

import asyncio

import pytest

from rag.llm_pool.concurrency import DistributedConcurrencyGate
from rag.llm_pool.models import LLMProviderConfig
from rag.llm_pool.provider import LLMProvider
from rag.llm_pool.router import LLMPoolRouter


def _make_provider(provider_id: str, name: str, weight: int = 1, max_concurrency: int = 5):
    cfg = LLMProviderConfig(
        provider_id=provider_id,
        name=name,
        model=f"{provider_id}-model",
        base_url="https://api.example.com",
        api_key="key",
        weight=weight,
        max_concurrency=max_concurrency,
    )
    return LLMProvider(cfg)


@pytest.mark.asyncio
async def test_router_least_inflight_load_balancing():
    """Router must choose the provider with the lowest inflight / weight ratio."""
    gate = DistributedConcurrencyGate(redis_client=None)
    p_deepseek = _make_provider("deepseek", "DeepSeek", weight=1)
    p_qwen = _make_provider("qwen", "Qwen", weight=1)

    router = LLMPoolRouter(providers=[p_deepseek, p_qwen], gate=gate)

    # 初始状态两者均无在途请求，按顺序或权重选第一个
    chosen = router.select_balanced_provider()
    assert chosen.config.provider_id in {"deepseek", "qwen"}

    # 人工给 deepseek 占用 2 个在途槽位
    await gate.acquire("deepseek", max_concurrency=5)
    await gate.acquire("deepseek", max_concurrency=5)

    # 此时 qwen 在途为 0，deepseek 在途为 2，router 必须调度给 qwen！
    next_chosen = router.select_balanced_provider()
    assert next_chosen.config.provider_id == "qwen"

    await gate.release("deepseek")
    await gate.release("deepseek")


@pytest.mark.asyncio
async def test_router_failover_when_primary_rate_limited():
    """If primary provider raises 429 / error before yielding tokens, router must failover to backup provider."""
    gate = DistributedConcurrencyGate(redis_client=None)
    p_primary = _make_provider("primary", "Primary Model")
    p_backup = _make_provider("backup", "Backup Model")

    # primary 模拟 429 限流
    async def failing_stream(*args, **kwargs):
        raise ConnectionError("429 Too Many Requests")
        yield "never"

    # backup 正常输出
    async def working_stream(*args, **kwargs):
        yield "Hello from backup!"

    p_primary.generate_stream = failing_stream
    p_backup.generate_stream = working_stream

    router = LLMPoolRouter(providers=[p_primary, p_backup], gate=gate)

    events = []
    async for event in router.generate_stream_balanced([{"role": "user", "content": "hi"}], preferred_provider_id="primary"):
        events.append(event)

    # 验证成功发生故障转移，且从 backup 产出了 token
    tokens = [e["content"] for e in events if e.get("type") == "token"]
    assert "".join(tokens) == "Hello from backup!"
    models = {e["model"] for e in events if "model" in e}
    assert "backup" in models


@pytest.mark.asyncio
async def test_router_parallel_arena_streaming():
    """Mode B: Router must stream from multiple providers concurrently with model tags."""
    gate = DistributedConcurrencyGate(redis_client=None)
    p1 = _make_provider("deepseek", "DeepSeek")
    p2 = _make_provider("qwen", "Qwen")

    async def stream_p1(*args, **kwargs):
        yield "D1"
        await asyncio.sleep(0.01)
        yield "D2"

    async def stream_p2(*args, **kwargs):
        yield "Q1"
        await asyncio.sleep(0.01)
        yield "Q2"

    p1.generate_stream = stream_p1
    p2.generate_stream = stream_p2

    router = LLMPoolRouter(providers=[p1, p2], gate=gate)

    events = []
    async for ev in router.generate_stream_parallel([{"role": "user", "content": "compare"}], provider_ids=["deepseek", "qwen"]):
        events.append(ev)

    deepseek_tokens = [e["content"] for e in events if e.get("model") == "deepseek" and e.get("type") == "token"]
    qwen_tokens = [e["content"] for e in events if e.get("model") == "qwen" and e.get("type") == "token"]

    assert "".join(deepseek_tokens) == "D1D2"
    assert "".join(qwen_tokens) == "Q1Q2"

    done_models = [e["model"] for e in events if e.get("type") == "model_done"]
    assert "deepseek" in done_models
    assert "qwen" in done_models

    # 最后一个事件必须是 overall done
    assert events[-1] == {"type": "done"}
