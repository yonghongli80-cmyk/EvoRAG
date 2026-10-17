# Multi-Model Concurrency, Load Balancing & Parallel Arena Design (Superpowers)

**Date:** 2026-10-16  
**Status:** Approved  
**Target:** RAGv3 Multi-LLM Concurrency, Redis-Backed Gate, High-Throughput Load Balancing & Side-by-Side Arena Answering  

---

## 1. Context & Objectives

In production multi-tenant deployments, RAG systems encounter severe concurrency bottlenecks when relying on a single LLM provider and single API key:
1. **Concurrency & Rate Limit Chokepoints (RPM/TPM)**: Single API accounts face 429 rate limits and vendor-side queueing, causing user queries to block sequentially ("挨个挨个问").
2. **Global Failure Cascades**: A single shared circuit breaker can trip for the whole system when one provider sputters.
3. **No Multi-Model Comparative Answering**: Users cannot trigger multiple models simultaneously to compare answers or achieve ensemble consensus in real time.

This design introduces a high-performance, Redis-backed multi-model concurrency engine supporting two complementary operational modes:
- **Mode A (High-Concurrency Load Balancing & Failover)**: Transparently distributes multi-tenant user traffic across a pool of configured LLM providers (e.g. DeepSeek, Qwen, Kimi, OpenAI, Ollama), with Redis-backed distributed semaphores, per-provider circuit breakers, and zero-latency failover when 429 / timeouts occur.
- **Mode B (Parallel Multi-Model Answering & Arena)**: Allows a single user query to concurrently dispatch to multiple models (e.g. DeepSeek + Qwen in parallel), multiplexed over a single SSE stream with model tags, rendered side-by-side in the frontend with optional ensemble synthesis.

All designs adhere to the project's non-negotiable invariants:
- **Redis First with Transparent In-Memory Fallback**: If Redis is offline or not installed, the concurrency gate transparently degrades to local `asyncio.Semaphore` and in-memory rate-limiting with zero downtime or exceptions.
- **Strict Backward Compatibility**: Default configuration gracefully derives from existing `settings.deepseek_*` settings so existing deployments, CLI tools, and tests run unchanged.
- **Zero Regressions**: 100% test pass rate across the 2,158-test backend baseline and 228-test frontend suite.

---

## 2. Architecture & Component Decomposition

```
rag/llm_pool/
├── __init__.py           # Public exports (LLMPoolRouter, get_llm_pool_router, LLMProvider)
├── models.py             # ProviderConfig, ProviderStats, ModelResponseChunk
├── concurrency.py        # DistributedConcurrencyGate (Redis sliding-window + asyncio.Semaphore fallback)
├── provider.py           # LLMProvider (AsyncOpenAI client wrapper + independent CircuitBreaker)
└── router.py             # LLMPoolRouter (Load balancer, failover orchestrator, parallel multiplexer)
```

### 2.1 Configuration (`config.py` & `rag/llm_pool/models.py`)
- `LLMProviderConfig`:
  - `provider_id: str`: Unique key, e.g. `"deepseek"`, `"qwen"`, `"openai"`, `"local-ollama"`.
  - `display_name: str`: Human-readable label for UI, e.g. `"DeepSeek V3"`, `"通义千问 2.5"`.
  - `model: str`: Target model identifier for the API, e.g. `"deepseek-chat"`, `"qwen-plus"`.
  - `base_url: str`: Endpoint URL.
  - `api_key: str`: Secret token.
  - `auth_header: str`: Custom header (e.g. `"x-api-key"`) or empty for standard `Bearer`.
  - `weight: int = 1`: Load balancing distribution weight.
  - `max_concurrency: int = 10`: Inflight request limit per provider.
  - `rpm_limit: int = 60`: Requests-per-minute throttle.
  - `timeout_seconds: float = 30.0`: Individual provider call timeout.
  - `is_active: bool = True`: Admin toggle.
- In `config.py`:
  - `llm_providers_json: str = "[]"`: JSON string defining additional providers.
  - When empty, the system automatically synthesizes a default provider from existing `deepseek_api_key`, `deepseek_base_url`, `deepseek_model`, and `deepseek_auth_header`.

### 2.2 Concurrency & Rate Limiting Gate (`rag/llm_pool/concurrency.py`)
- **Redis Mode**:
  - Inflight tracking: Redis key `rag:llm:inflight:{provider_id}`.
  - Acquire: Redis `INCR`. If value > `max_concurrency`, `DECR` and return `False`. Set TTL lease (60s) to prevent leaks on worker crash.
  - Release: Redis `DECR`.
  - Rate limiting (RPM): Redis sorted set sliding window `rag:llm:rpm:{provider_id}`.
- **Fallback In-Memory Mode**:
  - Automatically activated if Redis connection fails or raises `redis.ConnectionError`.
  - Uses `asyncio.Semaphore(max_concurrency)` per provider.
  - Uses local sliding window timestamp deque for RPM.

### 2.3 Independent Provider & Resilience (`rag/llm_pool/provider.py`)
- Each `LLMProvider` encapsulates:
  - Its own `AsyncOpenAI` instance with configured `timeout` and `default_headers`.
  - Its own isolated `CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)`.
  - Failure in Provider A does **not** impact Provider B.
  - On 429 or network errors, `record_failure()` is scoped strictly to that provider.

### 2.4 Router & Load Balancer (`rag/llm_pool/router.py`)
- **Mode A: Balanced Generation (`generate_stream_balanced`)**:
  1. Filters providers by `is_active` and `circuit_breaker.allow_request()`.
  2. Selects candidate using Weighted Least-Inflight strategy:
     $$\text{score} = \frac{\text{inflight} + 1}{\text{weight}}$$
  3. Acquires concurrency slot via `DistributedConcurrencyGate`.
  4. Attempts stream generation.
  5. If an unrecoverable 429 or provider error occurs before tokens yield, releases slot, marks temporary penalty cooldown, and immediately retries with the next best provider (Failover).
- **Mode B: Parallel Arena Generation (`generate_stream_parallel`)**:
  1. Accepts `provider_ids: list[str]`.
  2. Spawns concurrent asynchronous generator tasks using an `asyncio.Queue` multiplexer.
  3. Yields merged events tagged with `model: str`:
     ```json
     {"type": "token", "model": "deepseek", "content": "..."}
     {"type": "token", "model": "qwen", "content": "..."}
     {"type": "model_done", "model": "deepseek"}
     {"type": "model_done", "model": "qwen"}
     ```
  4. Emits final `{"type": "done"}` when all requested models finish.

---

## 3. API & Protocol Contracts

### 3.1 Model Catalog Discovery: `GET /models`
- **Response**:
```json
{
  "default_model": "deepseek",
  "models": [
    {"id": "deepseek", "name": "DeepSeek V3", "model": "deepseek-chat", "healthy": true},
    {"id": "qwen", "name": "通义千问 Plus", "model": "qwen-plus", "healthy": true}
  ]
}
```

### 3.2 SSE Stream Protocol: `POST /query/stream`
- **Request Parameters Extension (`StreamQueryRequest`)**:
  - `model: str | None = None` (Specify target model for Mode A single query).
  - `models: list[str] | None = None` (Specify multiple models for Mode B Arena query).
- **SSE Event Payloads**:
  - For single model: maintains 100% backward compatibility:
    `data: {"type": "token", "content": "hello"}`
  - For multi-model arena:
    `data: {"type": "token", "model": "deepseek", "content": "hello"}`
    `data: {"type": "token", "model": "qwen", "content": "hi"}`
    `data: {"type": "model_done", "model": "deepseek"}`
    `data: {"type": "sources", "sources": [...]}`
    `data: {"type": "done"}`

---

## 4. Frontend Integration (`frontend/`)

### 4.1 Chat Store (`frontend/src/stores/chat.ts`)
- State additions:
  - `availableModels: Ref<Array<{id: string, name: string}>>`
  - `selectedModel: Ref<string>`
  - `arenaEnabled: Ref<boolean>`
  - `arenaModels: Ref<string[]>`
- Multiplexed Stream Parsing:
  - In `sendMessage()`, when `arenaEnabled` is true, payloads send `models: arenaModels.value`.
  - Assistant message contains `arenaResponses: Record<string, string>`.
  - As tokens arrive with `data.model`, updates `assistantMsg.arenaResponses[data.model] += data.content`.

### 4.2 UI Components
- Model Selector & Arena Switch:
  - Added to chat input header (Select single model or toggle "双模型对比").
- Arena Message View:
  - When message contains `arenaResponses`, renders a responsive 2-column comparison card side-by-side with independent token typing animations and copy/feedback buttons.

---

## 5. Testing & Verification Strategy

- **TDD Unit Tests**:
  - `tests/test_llm_pool_concurrency.py`: verifies distributed semaphore acquire/release with Redis and mock fallback.
  - `tests/test_llm_pool_router.py`: tests Mode A least-inflight balancing, 429 failover, Mode B multiplexed parallel token streaming.
  - `tests/test_api_multi_model.py`: tests `GET /models`, `POST /query/stream` with single and multiple model parameters.
  - `frontend/src/stores/__tests__/chat.test.ts`: tests parsing multi-model SSE events.
- **Regression Suite**:
  - Complete backend `pytest` (2,158 tests).
  - Complete frontend `vitest` (228 tests).
  - Clean `ruff check .`.
