# Second Deep Code Review Remediation Plan (Superpowers Round 2)

**Goal:** Close all 5 actionable architectural findings from the 2026-10-14 Superpowers Deep Code Review Round 2, ensuring end-to-end reliability for hypertext wiki and dual-substrate retrieval through Evidence Bundle gates, permission filters, Qdrant version indexing, title collision protection, and revision diff endpoints.

**Spec:** `docs/superpowers/specs/2026-10-14-second-deep-codebase-review.md`

## Execution Rules
- Strict red-green-refactor TDD for every behavior change.
- Focused unit/integration tests first; full regression suite only after focused suites pass.
- Maintain zero regressions across existing file/spreadsheet permission models and reliability gates.
- Zero `ruff` warnings.

## Tasks

1. **[RET-01] Evidence Bundle Version Alignment (TDD)** [x]
   - Add failing test in `tests/test_wiki_retrieval_integration.py` proving Wiki chunks are preserved in `RAGPipeline._finalize_rag_context` and successfully build an `EvidenceBundle` without dropping or throwing `ValueError("versioned evidence required")`. [x]
   - Update `WikiChunk.to_chunk` in `rag/wiki/chunker.py` to accept `document_version_id` and `version_no`. [x]
   - Update `WikiIndexer.sync_page` in `rag/wiki/indexer.py` to pass `page.current_revision_id` to both `to_chunk` and `add_to_collection`, and purge existing points for `page.title` to prevent vector bloat. [x]
   - Run tests to verify they pass (Green). [x]

2. **[ACL-01] Permission-Aware Retrieval Filter for Wiki Chunks (TDD)** [x]
   - Add failing test in `tests/test_wiki_permissions.py` proving a non-admin user with KB read access can retrieve Wiki chunks through `filter_chunks_by_permission` without them being rejected as unrecorded documents. [x]
   - Update `rag/permissions.py`: in `filter_chunks_by_permission`, handle `chunk.chunk_kind == "wiki"` by checking KB-level view authorization (both Org policy and standard KB permissions). [x]
   - Run tests to verify they pass (Green). [x]

3. **[RET-02] Qdrant Version Pushdown Inclusion for Wiki Pages (TDD)** [x]
   - Add failing test in `tests/test_wiki_retrieval_integration.py` showing `_visible_current_version_ids` includes `current_revision_id` for accessible Wiki pages. [x]
   - Update `_visible_current_version_ids` and `_visible_historical_version_ids` in `rag/versioned_retrieval.py` to incorporate Wiki revisions when the user has KB view access. [x]
   - Run tests to verify they pass (Green). [x]

4. **[WIKI-01] Title Collision Protection in WikiStore (TDD)** [x]
   - Add failing test in `tests/test_wiki_store.py` showing that creating or updating a page with a title that already exists in the same KB raises `ValueError`. [x]
   - Update `WikiStore.create_page` and `WikiStore.update_page` in `rag/wiki/store.py` to enforce unique `(kb_id, title)`. [x]
   - Run tests to verify they pass (Green). [x]

5. **[API-01] Revision Diff REST Endpoint (TDD)** [x]
   - Add failing test in `tests/test_wiki_api.py` calling `GET /wiki/pages/{id}/revisions/{num}/diff` and verifying the returned delta diff. [x]
   - Add `get_revision_by_num(page_id, revision_num)` to `WikiStore`. [x]
   - Add `GET /wiki/pages/{page_id}/revisions/{revision_num}/diff` endpoint to `rag/wiki/api.py`. [x]
   - Run tests to verify they pass (Green). [x]

6. **End-to-End Regression & Verification** [x]
   - Run `ruff check .` to guarantee 0 lint/format violations. [x]
   - Run all focused Wiki and retrieval tests: `tests/test_wiki_*.py`, `tests/test_versioned_retrieval.py`, `tests/test_permissions.py`, `tests/test_answerability.py`. [x]
   - Run the full test suite (2,143 tests) to guarantee 0 regressions. [x]

## Completion Record

Completed on 2026-10-14.
- All 5 actionable review findings closed via strict TDD:
  - `RET-01`: `WikiChunk.to_chunk` and `WikiIndexer.sync_page` now inject `document_version_id=page.current_revision_id`; Wiki chunks pass `EvidenceBundle` validation and are never silently discarded.
  - `ACL-01`: `filter_chunks_by_permission` honors KB-level view permissions for `chunk_kind == "wiki"` across both standard and organization policy knowledge bases.
  - `RET-02`: `_visible_current_version_ids` and `_visible_historical_version_ids` incorporate Wiki revisions so Qdrant version pushdown filters include active Wiki articles.
  - `WIKI-01`: `WikiStore.create_page` and `update_page` guard against title collisions in the same knowledge base, preventing vector index overwrite.
  - `API-01`: Exposed `GET /wiki/pages/{page_id}/revisions/{revision_num}/diff` endpoint returning unified diffs.
- Focused suites: 33 Wiki/Integration tests passing (100%).
- Full regression suite: 2,143 tests passing (100%), 0 failures.
- `ruff check .`: 0 errors/warnings.
