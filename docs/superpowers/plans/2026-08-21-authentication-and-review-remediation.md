# Authentication and Review Remediation Plan

**Goal:** Close every actionable finding from the 2026-08-21 project review while preserving the File permission domain and protecting live data.

**Spec:** `docs/superpowers/specs/2026-08-21-authentication-and-review-remediation-design.md`

## Execution rules

- Strict red-green-refactor TDD for every behavior change.
- Focused tests first; full regression only after all focused suites pass.
- Never point tests at repository `data/` or the live Qdrant directory.
- Back up and verify the real database before any cleanup; ambiguous rows are retained.

## Tasks

1. Add failing tests proving disabled or security-version-revoked users cannot pass legacy protected routes; route JWT verification through the unified principal resolver.
2. Add failing tests for anonymous registration and account enumeration; close organization-mode registration, replace the public account directory, add login throttling, cookie login/logout and unsafe-request origin checks.
3. Add failing tests proving non-organization users cannot create personal knowledge bases, tag results exclude inaccessible files, and knowledge-gap analytics require administration; implement the minimum authorization changes.
4. Add failing transaction and partial-PATCH tests; make duplicate metadata writes transaction-safe and preserve omitted secondary memberships.
5. Add frontend red tests for cookie sessions, removed registration/account enumeration, and `manage` versus `administer`; update the auth store and affected views.
6. Add a regression guard that fails if tests use production data paths or live Qdrant; migrate legacy API tests to isolated fixtures.
7. Update CI and development dependencies so frontend build, npm audit and Python dependency audit are mandatory.
8. Implement a dry-run/apply cleanup tool, test its classification and rollback behavior, create and verify an authoritative backup, then remove only unequivocal test artifacts.
9. Remove the duplicate assignment and resolve the full Ruff report using safe automatic fixes followed by focused manual fixes.
10. Run full backend and frontend suites, production build, dependency audits, lint, diff checks, live health smoke tests, and a final self-review.

## Completion record

Completed on 2026-08-21.

- Authoritative verified backup: `20260821T023838Z-89035a47064e`.
- Removed seven proven test users, nine proven SQLite knowledge-base artifacts, and the two matching vector collections that still existed.
- Replaced the vulnerable `python-jose`/`ecdsa` dependency chain with PyJWT.
- Final backend regression: 1159 passed, 1 conditionally skipped.
- Final frontend regression: 29 files and 165 tests passed; production build succeeded.
- Ruff, pip check, npm audit, pip-audit, cleanup dry-run, Git diff check, and live role-based login smoke tests passed.
