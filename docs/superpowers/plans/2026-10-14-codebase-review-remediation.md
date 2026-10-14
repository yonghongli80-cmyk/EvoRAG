# Codebase Review Remediation Plan

**Goal:** Close all 4 actionable findings from the 2026-10-14 Superpowers Deep Code Review, hardening authorization, dual-substrate index synchronization, and SQLite concurrency.

**Spec:** `docs/superpowers/specs/2026-10-14-comprehensive-codebase-review.md`

## Execution Rules
- Strict red-green-refactor TDD for every behavior change.
- Focused tests first; full regression only after all focused suites pass.
- Never bypass authorization policies or introduce regression in existing permissions.
- Maintain complete orthogonality between Track A (code repair) and Track B (wiki knowledge).

## Tasks

1. **[SEC-01] Wiki REST API fine-grained authorization (TDD)** [x]
   - Add failing test proving unprivileged users cannot view, edit, rollback, or delete Wiki pages in restricted knowledge bases. [x]
   - Inject `Request` and `user_db` into `rag/wiki/api.py`, adding `require_kb_view` and `require_kb_edit` guards to all page endpoints. [x]
   - Verify all Wiki tests pass (Green). [x]

2. **[DAT-01] Wiki rename stale Qdrant vector cleanup (TDD)** [x]
   - Add failing test verifying that renaming a Wiki page purges old points matching the previous title in Qdrant. [x]
   - Update `WikiStore.update_page` and `WikiIndexer.sync_page` to accept/handle `old_title` and call `delete_doc(kb_id, old_title)`. [x]
   - Verify test turns green. [x]

3. **[CONC-01] SQLite WAL mode and busy timeout consistency** [x]
   - Update `rag/wiki/store.py` and `rag/evolution/store.py` to configure `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=30000`. [x]
   - Add test or assertions ensuring WAL mode is enabled upon store initialization. [x]

4. **[EVO-01] Immutable immunity boundary for `rag/wiki/**`** [x]
   - Add `"rag/wiki/**"` to `FORBIDDEN_GLOBS` in `rag/evolution/code_patches.py`. [x]
   - Update or verify layer-1 gate tests in `tests/test_code_patches.py`. [x]

5. **Full Verification & Regression** [x]
   - Run `ruff check .` to guarantee 0 lint/format violations. [x]
   - Run focused suites: `tests/test_wiki_*.py`, `tests/test_gap_remediation.py`, `tests/test_code_patches.py`. [x]
   - Verify health and clean git status. [x]

## Completion Record

Completed on 2026-10-14.
- All 4 actionable review findings closed via strict TDD:
  - `SEC-01`: Fine-grained KB authorization added to all Wiki endpoints; verified unprivileged access denied with 403/404.
  - `DAT-01`: Wiki rename purges old Qdrant vectors under the previous title; verified 0 ghost points remain.
  - `CONC-01`: Configured `PRAGMA journal_mode = WAL` and `PRAGMA busy_timeout = 30000` on `WikiStore` and `FailureTraceStore`.
  - `EVO-01`: Added `"rag/wiki/**"` to `FORBIDDEN_GLOBS`; verified layer-1 gate refusal across 102 tests.
- All 30 focused Wiki/Evolution tests passed 100%.
- All 102 code patch immunity gate tests passed 100%.
- End-to-end sandbox simulation script passed 100%.
- `ruff check .` passed with 0 warnings.

