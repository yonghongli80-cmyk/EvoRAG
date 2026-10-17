# Comprehensive Codebase Review Remediation Design (Superpowers)

**Date:** 2026-10-16
**Status:** Approved
**Target:** RAGv3 Architecture, Security, Concurrency, and Resilience Hardening

---

## 1. Context & Objectives

Following a deep, full-scale architectural and security audit of RAGv3, this design specifies surgical remediations across 4 core tracks:
1. **Track 1: P0 Critical - Pipeline Lifecycle, Concurrency & Stream Integrity**
2. **Track 2: P1 High - Security, SSRF, Local Access & Storage Limits**
3. **Track 3: P2 Medium - Data Integrity, Performance & Concurrency**
4. **Track 4: P3 Architecture, Config, Docker & Frontend**

All changes adhere to the CLAUDE and Superpower invariants:
- Zero regressions across existing test baseline (2,147 backend tests, 225 frontend tests).
- Strict Red-Green-Refactor TDD for every defect remediation.
- Fail-closed security boundaries.
- Deterministic resource teardown.

---

## 2. Track 1: P0 Critical - Pipeline Lifecycle & Stream Integrity

### 2.1 [PIPE-01] Stream Query Pipeline Resource & Connection Leak
- **Problem**: In `POST /query/stream` (`rag/api.py`), for non-default knowledge bases (`target_kb != "rag_docs"`), `current_pipeline = await asyncio.to_thread(_make_pipeline, kb_id=target_kb)` instantiates a brand new `RAGPipeline` on every request. Each pipeline opens SQLite connections (`memory.db` and `tracker`). When the stream finishes or errors, `current_pipeline.close()` is never called, leaking database file descriptors and memory.
- **Remediation**:
  - In `rag/api.py`, use the cached pipeline via `_get_or_create_pipeline(target_kb)` under `_pipeline_lock`.
  - Alternatively, if ephemeral pipelines are ever constructed, ensure a `try ... finally` block wraps `event_generator()` to guarantee `current_pipeline.close()`.

### 2.2 [PIPE-02] Single-File Pipeline Collection Erasure Protection
- **Problem**: When `RAGPipeline` is initialized with `file_path` but no `kb_id` (`rag/pipeline.py`), it executes `clear()`, which wipes the entire global Qdrant collection `rag_docs` (`COLLECTION_NAME`).
- **Remediation**:
  - Deprecate destructive global `clear()` in `RAGPipeline.__init__`.
  - For single-file / ad-hoc analysis, use an isolated collection or delete only chunks belonging to `doc_name`, preserving the rest of the collection.

### 2.3 [GEN-01] Stream LLM Retry Token Duplication
- **Problem**: In `rag/generator.py` `generate_stream`, if a network/timeout exception occurs after one or more tokens have already been yielded to the consumer, the `for attempt in range(max_attempts)` loop restarts from token 0 and re-yields tokens. This duplicates content on the user's screen.
- **Remediation**:
  - Introduce `has_yielded: bool = False`.
  - If an exception occurs after `has_yielded is True`, do not retry; record failure on circuit breaker and re-raise or break so the error is reported without duplicating partial text.

---

## 3. Track 2: P1 High - Security Boundaries & Resource Limits

### 3.1 [SEC-01] SSRF Redirect Interception & DNS Rebinding Hardening
- **Problem**: `APISource` and `RSSSource` validate URL via `validate_external_url(url)`, which resolves host IP. However, subsequent `requests.get` follows HTTP redirects by default (`allow_redirects=True`) and re-resolves DNS. If the endpoint redirects to `127.0.0.1` or `169.254.169.254`, `requests` follows it without re-validation. Furthermore, `feedparser.parse(url)` invokes `urllib` without SSRF checks on redirects.
- **Remediation**:
  - In `APISource`, disable automatic redirects (`allow_redirects=False`) or enforce a custom requests session/adapter that validates every hop's resolved IP against `validate_external_host`.
  - In `RSSSource`, fetch feed text via safe `APISource`/requests fetcher first (under timeout and IP validation), then feed bytes/text into `feedparser.parse(content)`.

### 3.2 [SEC-02] SQLite Data Source Path Confinement
- **Problem**: `DBSource` (`rag/data_sources/db_source.py`) allows arbitrary connection strings `sqlite:///path`. A user could configure `sqlite:///data/users.db` or `sqlite:///data/jwt_secret.txt` and execute `SELECT` queries to dump password hashes and system tokens.
- **Remediation**:
  - In `DBSource._validate_sqlite_path(path)`, resolve the path and verify it is strictly within an allowed subfolder (e.g. `data/upload` or `data/structured_datasets`), and explicitly disallow any system database (`users.db`, `memory.db`, `analysis.db`, `jwt_secret.txt`, `evolution.db`, `wiki.db`).

### 3.3 [SEC-03] Bounded Stream Copy for Archive Extraction
- **Problem**: In `rag/materials/archives.py`, `_extract_entry` uses `shutil.copyfileobj(src, out, _CHUNK)` to write to disk completely before checking `st_size > limits.entry_bytes`. A malicious zip bomb with a manipulated uncompressed header could exhaust disk space before the check runs.
- **Remediation**:
  - Implement a bounded copy helper `_copyfileobj_bounded(src, dst, max_bytes)` that counts bytes and raises `ArchiveRejected("entry_size_exceeded")` as soon as `bytes_written > max_bytes`.

### 3.4 [ACL-01] Org Permission Filter N+1 Query Elimination
- **Problem**: In `filter_chunks_by_permission` (`rag/permissions.py`), when organizational policy is active, the function calls `allowed_document_ids` and then loops over all returned IDs with `db.get_document_permission_by_id(document_id)` to collect document names. For a KB with 10k documents, this executes 10k SQLite queries on every question.
- **Remediation**:
  - Instead of looking up all documents in the entire KB, extract only candidate `doc_names` from the input `chunks`. Query permission records only for those candidates (matching the optimization already used in the non-org branch).

---

## 4. Track 3: P2 Medium - Data Integrity & Concurrency

### 4.1 [WIKI-01] Wiki Document Deletion Collision Guard
- **Problem**: `WikiIndexer.sync_page` calls `delete_doc(page.kb_id, page.title)`, and `delete_doc` in `rag/vector_store.py` deletes all points matching `doc_name == page.title`. If a user uploads `README.md` and also has a Wiki page titled `README.md`, updating the Wiki page wipes the uploaded document's vectors.
- **Remediation**:
  - Add `delete_doc(..., chunk_kind: str | None = None)`. In `WikiIndexer`, pass `chunk_kind="wiki"`.

### 4.2 [STORE-01] BM25 Store Concurrency Pragma
- **Problem**: `BM25Store` in `rag/bm25_store.py` connects to SQLite without `PRAGMA journal_mode=WAL` or `PRAGMA busy_timeout=30000`, leading to `database is locked` under concurrent search/indexing.
- **Remediation**:
  - Set `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=30000` in `BM25Store.__init__`.

### 4.3 [CACHE-01] Redis Cache Flush Consistency
- **Problem**: `RedisCache.clear()` deletes Redis keys and returns early, failing to clear `self._fallback`.
- **Remediation**:
  - Clear `self._fallback` unconditionally within `clear()`.

### 4.4 [EMBED-01] Batch Embedding Acceleration
- **Problem**: `embed(texts)` in `rag/embedder.py` executes sequential single-item requests for every chunk.
- **Remediation**:
  - Filter out cached texts, batch uncached texts in groups of 32, perform single OpenAI-compatible embedding calls for the batch, populate cache, and return results in original order.

### 4.5 [LOCK-01] ReadWriteLock Scope Correction
- **Problem**: `delete_unversioned_points` holds `_write_lock.read()` while executing deletions.
- **Remediation**:
  - Change to `_write_lock.write()`.

### 4.6 [GUARD-01] Prompt Injection Pattern Refinement
- **Problem**: `INJECTION_PATTERNS` matches plain business substrings like `"系统提示"` and `"没有任何限制"`.
- **Remediation**:
  - Refine patterns to target command override phrasing rather than common isolated nouns.

---

## 5. Track 4: P3 Architecture, Config, Docker & Frontend

### 5.1 [CFG-01] `wiki_db_path` in Settings & Store Singleton Unification
- **Problem**: Missing `wiki_db_path` in `config.py` causes relative path `"data/wiki.db"` drift. Multiple `_wiki_store` singletons exist.
- **Remediation**:
  - Add `wiki_db_path: str = str(Path(__file__).resolve().parent / "data" / "wiki.db")` to `Settings`.
  - In `rag/wiki/api.py`, delegate `get_wiki_store()` directly to `rag.wiki.store.get_wiki_store()`.

### 5.2 [DOCKER-01] Multi-Arch `JAVA_HOME` in Dockerfile
- **Problem**: Hardcoded `openjdk-amd64` in `Dockerfile`.
- **Remediation**:
  - Use `/usr/lib/jvm/default-java` or auto-detect architecture.

### 5.3 [FE-01] Frontend Admin Navigation Guard
- **Problem**: Vue Router lacks `requiresAdmin` guard.
- **Remediation**:
  - Add `meta: { requiresAuth: true, requiresAdmin: true }` to `/organization`, `/operations/evolution`, and check `authStore.user?.is_admin` in `router.beforeEach`.
