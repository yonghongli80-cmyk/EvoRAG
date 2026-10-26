# Unit of Work contract for database migration

**Status: proposed application contract only.** This document defines requirements for a future database port. No Unit of Work API, PostgreSQL adapter, migration engine, or shared transaction layer is implemented by documenting the contract.

## Current boundary

The application currently uses `UserDB` as a SQLite facade over one `ConnectionManager`. The manager owns a serialized write connection and per-thread read connections. Some repository methods use `cm.transaction(immediate=True)`; other paths acquire `write_connection()` and commit directly. Nested transaction contexts reuse the current connection rather than establishing a savepoint. SQLite runtime migrations advance `user_version`, and material full-text search uses FTS5. The external PostgreSQL data-source connector is separate from the application database. Source evidence and dialect details are recorded in the [PostgreSQL data-access ADR](data-access-postgresql.md) and the [SQL portability inventory](sql-portability-inventory.md).

These facts mean that swapping a connection URL or introducing a thin driver wrapper is insufficient. Transaction ownership and cross-repository atomicity must be defined before an application adapter is built.

## Required contract

1. **One owner per command.** A request or background command creates one explicit read or write Unit of Work. The Unit of Work owns the connection/session and is the only component that commits or rolls back it. Repositories receive that Unit of Work and must not commit independently.
2. **Shared transaction across repositories.** Repositories used by one logical operation share the same Unit of Work. ACL changes, document-version pointer updates, lifecycle state, and their database-side audit/outbox records commit together when the domain operation requires atomicity.
3. **Explicit nesting.** Nested units either use a defined savepoint contract or are rejected. They must not silently reuse a transaction while implying independent rollback.
4. **Conditional writes.** State transitions use expected-state/version predicates and verify that exactly one row changed. A stale version, uniqueness conflict, or authorization denial is a domain result, not a transient retry.
5. **Bounded whole-command retries.** Only recognized transient serialization/deadlock outcomes may retry, and the complete database command is replayed outside the failed transaction with bounded attempts and deadline. Individual statements are not retried in place.
6. **No repeated external side effects.** A retried command cannot send a model request, mutate a file, or update Qdrant twice. Such work must be represented by a durable outbox/idempotency record or another explicitly idempotent handoff. If commit outcome is uncertain, the command reconciles by its idempotency key instead of blindly retrying.
7. **Stable domain behavior.** Adapters preserve IDs, NULL/default behavior, ordering, time/boolean/JSON conversion, ACL decisions, current-version semantics, lifecycle results, and error categories. FTS behavior requires its own Chinese-language equivalence decision and test corpus.

## Intended boundary (not an implementation stub)

When implementation is authorized, the intended calling shape is conceptually:

```text
with database.unit_of_work(mode="write") as uow:
    documents = DocumentRepository(uow)
    materials = MaterialRepository(uow)
    perform_one_domain_command(documents, materials)
```

This is illustrative pseudocode, not an available Python API. Do not add a no-op or SQLite-only facade named `UnitOfWork` and treat it as migration progress. The eventual interface must be designed against all writer paths and the selected PostgreSQL driver, then tested against both adapters.

## Required evidence before an adapter is considered ready

- Complete writer and SQL inventory, including unresolved dynamic SQL and transaction ownership; no unreviewed path may be inferred portable.
- Golden repository behavior over deterministic synthetic fixtures, including ACL/admin/legacy cases, NULLs, uniqueness and CAS conflicts, ordering, FTS cases, and error classification.
- Transaction contract tests for commit, rollback, shared multi-repository work, nesting/savepoint behavior, and whole-command retry without duplicate external effects.
- Isolated PostgreSQL schema migrations and repeatability checks, plus a separately approved sanitized representative fixture before claiming business-shaped equivalence.
- Full writer admission/fence, drain and consistent snapshot protocol before any migration or dual-database comparison involving mutable state. Sequence/row IDs are not commit watermarks.
- Reconciliation and restore evidence for SQL, files, FTS/vector projections, and outbox state before any cutover or rollback claim.

Until those gates are met, SQLite remains the supported application path. A local inventory or synthetic-only contract test does not imply PostgreSQL support, multi-instance readiness, or permission for live shadow reads. See the [shared-state, storage, and worker ADR](shared-state-storage-workers.md) for cross-store fencing and deployment gates.
