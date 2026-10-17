# Multi-Model Concurrency, Load Balancing & Arena Execution Plan (Superpowers)

**Goal:** Implement a Redis-backed multi-LLM concurrency, load balancing, failover, and parallel arena comparison engine for RAGv3.

**Spec:** `docs/superpowers/specs/2026-10-16-multi-model-concurrency-and-arena-design.md`

## Execution Rules
- Strict Red-Green-Refactor TDD for every functional change.
- Run focused tests first, then full regression suites.
- Guarantee zero regression across 2,158 existing backend tests and 228 frontend tests.
- Zero `ruff` warnings.
- Redis-first with 100% reliable in-memory fallback.

---

## Tasks

### Phase 1: Configuration & Models
1. **[CFG-POOL] Multi-Provider Configuration & Data Structures** [x]
   - Update `config.py` with `llm_providers_json` (defaults to auto-synthesizing DeepSeek from existing settings).
   - Create `rag/llm_pool/models.py` with `LLMProviderConfig`, `ProviderHealthState`, and `ModelResponseChunk`.
   - Run existing tests to verify zero regressions.

### Phase 2: Redis-Backed Concurrency & Rate Limiting Gate (TDD)
2. **[GATE-TDD] Distributed Concurrency Gate Tests** [x]
   - Create `tests/test_llm_pool_concurrency.py`.
   - Test Redis-based inflight counter (`INCR`/`DECR`) and max concurrency cap.
   - Test transparent in-memory fallback to `asyncio.Semaphore` when Redis connection is down or not configured.
   - Test sliding-window RPM rate limiter.
3. **[GATE-IMPL] Implement DistributedConcurrencyGate** [x]
   - Create `rag/llm_pool/concurrency.py` implementing `DistributedConcurrencyGate`.
   - Run `tests/test_llm_pool_concurrency.py`: verify Green.

### Phase 3: Provider Abstraction & Isolated Resilience (TDD)
4. **[PROV-TDD] LLMProvider Unit Tests** [x]
   - Create `tests/test_llm_pool_provider.py`.
   - Test `AsyncOpenAI` client instantiation with custom headers and timeout.
   - Test provider-level isolated `CircuitBreaker` (failure on Provider A does not trip Provider B).
   - Test token streaming and error classification.
5. **[PROV-IMPL] Implement LLMProvider** [x]
   - Create `rag/llm_pool/provider.py`.
   - Run `tests/test_llm_pool_provider.py`: verify Green.

### Phase 4: Router - Load Balancing & Parallel Arena (TDD)
6. **[ROUTER-TDD] Router Balancing, Failover & Parallel Arena Tests** [x]
   - Create `tests/test_llm_pool_router.py`.
   - Test Mode A: Weighted least-inflight load balancing across providers.
   - Test Mode A: Automatic zero-latency failover when a provider returns 429 or timeout.
   - Test Mode B: Concurrent multi-provider parallel streaming using async queue multiplexer, tagged with `model`.
7. **[ROUTER-IMPL] Implement LLMPoolRouter** [x]
   - Create `rag/llm_pool/router.py` and `rag/llm_pool/__init__.py`.
   - Wire `rag/generator.py` and `rag/pipeline.py` to route through `get_llm_pool_router()`.
   - Run `tests/test_llm_pool_router.py` and `tests/test_generator_stream.py`: verify Green.

### Phase 5: API Layer & Endpoints (TDD)
8. **[API-TDD] Multi-Model API Tests** [x]
   - Create `tests/test_api_multi_model.py`.
   - Test `GET /models` endpoint returns active provider catalog.
   - Test `POST /query/stream` accepts `model` and `models: ["deepseek", "qwen"]` and yields model-tagged SSE events.
9. **[API-IMPL] Update API Endpoints** [x]
   - Update `rag/api.py`: add `GET /models` and extend `StreamQueryRequest` in `POST /query/stream`.
   - Run `tests/test_api_multi_model.py`: verify Green.

### Phase 6: Frontend Integration & Arena UI (TDD)
10. **[FE-STORE-TDD] Chat Store Multi-Model & Arena Streaming Tests** [x]
    - Update `frontend/src/stores/__tests__/chat.test.ts` to test parsing multiplexed SSE events with `model` field.
11. **[FE-STORE-IMPL] Chat Store & UI Component Update** [x]
    - Update `frontend/src/stores/chat.ts` to support `selectedModel`, `arenaEnabled`, and `arenaResponses`.
    - Update chat header with model selector dropdown and arena comparison switch.
    - Update chat bubble view to render dual-column side-by-side comparison when arena mode is active.
    - Run `npm test -- --run` and `npm run build`: verify Green.

### Phase 7: Full Regression Quality Gates
12. **[GATE-VERIFY] Complete Project Regression Verification** [x]
    - Run `ruff check .`: verify 0 errors, 0 warnings.
    - Run full backend `pytest`: verify all 2,171 tests pass (100% Green).
    - Run full frontend `vitest`: verify all 230 tests pass (100% Green).
    - Validate production static bundle build (`npm run build` 100% clean).
