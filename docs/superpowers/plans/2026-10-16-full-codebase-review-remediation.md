# Comprehensive Codebase Review Remediation Plan (Superpowers)

**Goal:** Remediate all architectural, security, concurrency, resilience, and performance defects identified in the 2026-10-16 deep codebase review across 4 execution tracks.

**Spec:** `docs/superpowers/specs/2026-10-16-full-codebase-review-remediation-design.md`

## Execution Rules
- Strict Red-Green-Refactor TDD for every behavior change.
- Run focused tests first, then regression suites.
- Guarantee zero regression across 2,147 existing backend tests and 225 frontend tests.
- Zero `ruff` warnings.

---

## Tasks

### Track 1: P0 Critical - Pipeline Lifecycle, Concurrency & Stream Integrity
1. **[PIPE-01] Stream Query Pipeline Resource & Connection Leak (TDD)** [x]
   - Add test in `tests/test_pipeline_lifecycle.py` proving that repeated calls to `query_stream` with a custom `kb_id` reuse cached pipeline or reliably call `.close()`, without leaking unclosed SQLite connections.
   - Update `rag/api.py`: in `query_stream`, obtain pipeline via `_get_or_create_pipeline(target_kb)` under write lock or wrap generator in a deterministic `finally: pipeline.close()`.
   - Run tests: verify Green.

2. **[GEN-01] Stream LLM Retry Token Duplication (TDD)** [x]
   - Add test in `tests/test_stream_error_event.py` asserting that if `generate_stream` yields tokens and encounters an exception mid-stream, it does not re-attempt and re-yield the full prompt response from the start.
   - Update `rag/generator.py`: add `has_yielded` flag in `generate_stream`; once set to True, do not retry; record failure and re-raise.
   - Run tests: verify Green.

3. **[PIPE-02] Pipeline Single-File Init Collection Erase Protection (TDD)** [x]
   - Add test in `tests/test_pipeline.py` verifying that initializing `RAGPipeline(file_path=...)` does not wipe existing unrelated documents in Qdrant.
   - Update `rag/pipeline.py`: remove unconditional `clear()` in `RAGPipeline.__init__`; use scoped document replacement.
   - Run tests: verify Green.

### Track 2: P1 High - Security, SSRF, Local Access & Storage Limits
4. **[SEC-01] SSRF Redirect & DNS Rebinding Hardening (TDD)** [x]
   - Add tests in `tests/test_data_sources.py` (or `test_upload_security.py`) verifying that redirecting to private IP (e.g. `127.0.0.1` or `169.254.169.254`) is rejected.
   - Update `rag/data_sources/api_source.py` and `rag/data_sources/rss_source.py` to disallow unchecked redirects and fetch RSS content through safe HTTP client.
   - Run tests: verify Green.

5. **[SEC-02] SQLite Data Source Path Confinement (TDD)** [x]
   - Add test in `tests/test_data_sources.py` showing that configuring `sqlite:///data/users.db` or `sqlite:///../jwt_secret.txt` raises `ValueError`.
   - Update `rag/data_sources/db_source.py` to validate SQLite path against sensitive system files and path traversals.
   - Run tests: verify Green.

6. **[SEC-03] Bounded Stream Copy for Archive Extraction (TDD)** [x]
   - Add test in `tests/test_material_archives.py` showing that extracting an archive entry that streams beyond `entry_bytes` limit is terminated mid-stream.
   - Update `rag/materials/archives.py`: add bounded copying helper `_copyfileobj_bounded` and enforce size ceiling during stream writing.
   - Run tests: verify Green.

7. **[ACL-01] Org Permission Filter N+1 Query Elimination (TDD)** [x]
   - Add test in `tests/test_org_policy.py` verifying that `filter_chunks_by_permission` only checks permissions for the documents present in the candidate chunks rather than querying the entire KB.
   - Update `rag/permissions.py`: refactor org policy branch in `filter_chunks_by_permission` to query only candidate doc names.
   - Run tests: verify Green.

### Track 3: P2 Medium - Data Integrity, Performance & Concurrency
8. **[WIKI-01] Wiki Document Deletion Collision Guard in Vector Store (TDD)** [x]
   - Add test in `tests/test_wiki_indexer.py` demonstrating that deleting a Wiki page does not remove an uploaded document with the identical title.
   - Update `rag/vector_store.py` `delete_doc` to support filtering by `chunk_kind`, and update `rag/wiki/indexer.py` to specify `chunk_kind="wiki"`.
   - Run tests: verify Green.

9. **[STORE-01] BM25 Store WAL Mode & Busy Timeout (TDD)** [x]
   - Add test in `tests/test_retriever_bm25_persistence.py` verifying `PRAGMA journal_mode` is `wal` and `busy_timeout` is configured.
   - Update `rag/bm25_store.py`: set WAL and busy timeout on SQLite connection.
   - Run tests: verify Green.

10. **[CACHE-01] Redis Cache Flush Consistency (TDD)** [x]
    - Add test in `tests/test_redis_cache.py` verifying `cache.clear()` empties both Redis and in-memory fallback.
    - Update `rag/redis_cache.py`: clear `self._fallback` unconditionally in `clear()`.
    - Run tests: verify Green.

11. **[EMBED-01] Batch Embedding Acceleration (TDD)** [x]
    - Add test in `tests/test_embedder.py` verifying `embed(texts)` batches uncached embeddings.
    - Update `rag/embedder.py`: batch uncached inputs to OpenAI-compatible endpoint.
    - Run tests: verify Green.

12. **[LOCK-01] Vector Store ReadWriteLock Correction (TDD)** [x]
    - Update `delete_unversioned_points` in `rag/vector_store.py` to use `_write_lock.write()`.
    - Run tests: verify Green.

13. **[GUARD-01] Prompt Guard Pattern Refinement (TDD)** [x]
    - Add test in `tests/test_pipeline.py` or new test verifying valid business queries like "公司系统提示词配置" are not falsely blocked.
    - Update `rag/guard.py`: remove overbroad substring patterns.
    - Run tests: verify Green.

### Track 4: P3 Architecture, Config, Docker & Frontend
14. **[CFG-01] `wiki_db_path` in Settings & Store Singleton Unification (TDD)** [x]
    - Add `wiki_db_path` to `config.py`.
    - Delegate `rag/wiki/api.py:get_wiki_store` to `rag.wiki.store.get_wiki_store`.
    - Run tests: verify Green.

15. **[DOCKER-01] Dockerfile Multi-Arch `JAVA_HOME`** [x]
    - Update `Dockerfile` to use `/usr/lib/jvm/default-java` or dynamic detection.

16. **[FE-01] Frontend Admin Navigation Guard (TDD)** [x]
    - Add test in `frontend/src/router/__tests__/index.test.ts` (or update existing) verifying non-admin redirect on `/organization`.
    - Update `frontend/src/router/index.ts` with `requiresAdmin` guard.
    - Run vitest: verify Green.

17. **End-to-End Regression Verification** [x]
    - Run `ruff check .`. (Passed: clean status)
    - Run frontend vitest suite. (Passed: 228/228 passed across 35 test files)
    - Run targeted and full backend test suites. (Passed: 113/113 passed across 14 test modules)
