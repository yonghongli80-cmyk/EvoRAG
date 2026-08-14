# Material Indexing Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Newly uploaded files are indexed by the running API and can be added to a knowledge base before indexing finishes.

**Architecture:** Keep `document_permissions.current_version_id` authoritative. The API starts one bounded material worker and stops it before closing Qdrant. The download endpoint chooses a confined current version, otherwise the newest staging version.

**Tech Stack:** FastAPI, SQLite, Qdrant local client, pytest.

## Global Constraints

- Preserve ACL checks and version-root confinement.
- Use red-green-refactor TDD; tests never use production data or Qdrant paths.
- Do not make `staging` a retrieval current version.

---

### Task 1: Serve a permitted staging version for knowledge-base transfer

**Files:**
- Modify: `rag/api.py`
- Test: `tests/test_material_api.py`

- [ ] Write a failing API test that uploads a material, then downloads it while its only version is `staging`.
- [ ] Run the test and confirm the current-only download path returns 404.
- [ ] Select the newest `staging` version only when no current version exists, resolve it through `resolve_version_path`, and retain existing ACL checks.
- [ ] Re-run the focused material API tests.

### Task 2: Complete jobs and run the bounded worker with the API

**Files:**
- Modify: `rag/user_db.py`, `rag/materials/worker.py`, `rag/api.py`, `tests/conftest.py`
- Test: `tests/test_material_jobs.py`, `tests/test_pipeline_lifecycle.py`

- [ ] Write failing tests for a claimed job becoming `done`, a processing exception becoming `retry` or `failed`, and API startup invoking the worker while tests can disable it.
- [ ] Run the tests and confirm the statuses/lifecycle behavior is absent.
- [ ] Add atomic job-result persistence, a bounded daemon loop using `rag.embedder.embed`, and orderly shutdown that releases leases before Qdrant closes.
- [ ] Re-run focused tests, then backend and frontend full suites plus `pip check`.
