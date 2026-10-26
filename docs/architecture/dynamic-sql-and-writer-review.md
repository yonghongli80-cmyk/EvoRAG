# Dynamic SQL and writer ownership review

**Status: local source-review preparation.** The tools in this document inventory syntax and bind review records to source snapshots. They do not execute application code or SQL, prove a dynamic expression safe, establish complete writer coverage, implement a Unit of Work, or claim PostgreSQL compatibility.

Latest local snapshot: `cdf039a` uses 98 identity/source/evidence-hash unchanged reviews and 5 explicit source rereviews after SQLite quoting changed. Final dispositions are 90 `understood_shape`, 10 `needs_followup`, and 3 `external_source`; 401 canonical references bind 32 Rag files. Inventory coverage remains incomplete and portability remains `not_assessed`. See the [local-contracts acceptance](../reviews/2026-10-26-local-contracts.md) and [command transaction boundaries](command-transaction-boundaries.md). The round-one seed and counts below are historical; changed sources must be rereviewed rather than silently rebound to current hashes.

## Round-one scope and historical evidence

The frozen round-one SQL inventory scanned 208 files and records 103 unresolved SQL sink expressions: 65 `dynamic_fstring`, 36 `dynamic_expression`, and 2 `unsupported_expression`. Its coverage is incomplete by definition. It also records 349 transaction markers (136 `commit`, 85 `read_connection`, 58 `transaction`, 55 `write_connection`, 8 `rollback`, 7 `begin`) and 50 `lastrowid` code markers. Those counts are lexical observations; they are not distinct command counts, call reachability, transaction ownership, or writer completeness.

`artifacts/local-followups-2026-10-26/dynamic-sql-review-seed.json` contains the first manual source review of those 103 findings, based on the round-one inventory and current per-file hashes. It has 88 `understood_shape`, 12 `needs_followup`, and 3 `external_source` dispositions. The follow-ups include the caller-overridable migration iterable, file-loaded legacy migration SQL, SQL copied from SQLite schema metadata, metadata-derived spreadsheet identifiers and dynamic parsed-sheet identifiers at write sinks, snapshot-schema-derived identifiers, and the runtime SELECT tool. The three external-source findings are `DBSource.query` executions against configured SQLite, MySQL, and PostgreSQL data sources. The seed records one exact source reference and source-file hash per finding; its group rationale is review evidence, not a machine-verified proof. It is a source-review seed, not a ready validator manifest: final manifest evidence hashes must be rebuilt and rebound after root freezes the final source tree.

## Dynamic SQL review manifest

`scripts.review_dynamic_sql` provides a standard-library-only API:

```python
manifest = build_manifest(inventory_path, source_root)
write_manifest(manifest, output_path)  # exclusive create
result = validate_manifest(manifest, inventory_path, source_root)
```

The CLI has `build` and `check` subcommands. A new manifest always starts with every finding `unreviewed`. Each finding has a deterministic ID derived from exact `path`, `line`, `owner`, `sink`, and scanner classification; it also binds the referenced source file hash and a `path#Lline` evidence reference. The inventory hash binds the entire input JSON snapshot. `source_inventory_complete` and `source_inventory_unresolved_count` preserve that snapshot's coverage separately from review coverage. Missing, extra, duplicate, stale, mismatched, or path-escaping findings are rejected.

Allowed dispositions are `unreviewed`, `understood_shape`, `needs_followup`, and `external_source`. Any reviewed disposition requires a rationale, evidence, and a `sql_shape_class`; `needs_followup` also requires a next step. Every evidence item must be a normalized local `path#Lpositive_line` beneath the `rag` source root, resolve to an existing file and in-range line, and include the finding's own `source_ref`. Each finding carries `evidence_sha256`, a map from every distinct referenced relative file to its raw-byte SHA-256. The map must exactly cover the referenced files and match current hashes; the validator never fills missing hashes. Even `external_source` findings cite the local connector code; this schema does not accept external URLs or untyped external evidence. An unreviewed finding starts with empty evidence and an empty hash map and cannot carry review claims.

The validator checks evidence provenance structure and file freshness, but it cannot prove a rationale is semantically correct or that the evidence actually supports it.

`coverage.complete` means every finding has a source-bound disposition and evidence fields. It may be complete while some items remain `needs_followup` or `external_source`. `portability_readiness` is fixed to `not_assessed`; this validator cannot promote it. A reviewer must distinguish:

- fixed source literals, closed identifier/declaration sets, and generated SQL fragments;
- runtime values passed separately as driver parameters;
- placeholder counts derived from collection lengths, including empty and batch behavior;
- runtime SQL text or identifiers derived from call arguments, files, or SQLite metadata;
- source shapes whose dialect features still require a separate portability decision.

In particular, knowing that an f-string inserts only a finite list of column names does not make the DDL PostgreSQL-compatible. A bound parameter does not bind an SQL identifier. An `IN` placeholder count derived from a list does not bind the list itself. Opaque query text must remain opaque until its source boundary is reviewed.

## Writer candidate inventory

`scripts.audit_transaction_ownership.scan_tree(rag_root, scripts_root)` parses `.py` files under explicit `rag` and `scripts` roots and returns deterministic candidates. Each candidate captures path, line/column, AST owner, marker, lexical `with` scopes, connection hint, boundary, and a conservative transaction-ownership label. It recognizes common connection/transaction context names, direct `execute`/`commit`/`rollback` calls, imported SQLite/psycopg connection constructors, raw `_conn` access, aliases the local assignment/import visitor can resolve, and parse errors.

The report explicitly keeps the two roots separate. Files under `scripts/` are `maintenance_or_unknown_script` unless they are under the explicit `scripts/fixtures/` or `scripts/test_fixtures/` paths; the tool does not assume every script is a diagnostic fixture. Those directory labels separate inventory scope only; they do not certify a script as safe, unreachable, or non-production. The fixture paths receive `diagnostic_fixture` boundary labels. Imported connection aliases are discarded when a function parameter/local store or later assignment rebinds the name; unresolved receivers remain `unknown`. A Python syntax error or read failure is visible in `errors`; successful parsing only means parse coverage. `writer_completeness` is always `not_proven`. A candidate hit is not proof a path executes, and an absent hit is not proof there is no writer.

The CLI requires `--rag-root`, `--scripts-root`, and a new `--output` path. Both report writers refuse to overwrite evidence and use stable JSON serialization. Candidate reports should be generated only after the root has frozen the source tree; no current-tree report is claimed here.

## Current transaction boundary from source

- `rag/db/connection.py`: `ConnectionManager.transaction()` acquires the write `RLock`. If no transaction is active, it starts `BEGIN` or `BEGIN IMMEDIATE`, commits on success, and rolls back on exception. If the shared connection is already in a transaction, the nested context yields that connection and returns without a savepoint or an independent commit/rollback.
- `write_connection()` holds the lock but does not start a transaction and does not commit. Callers can commit directly. `get_write_connection()` exposes the raw writer. The thread-local `read_connection()` is query-only, but its context is not a read transaction contract.
- `rag/user_db.py`: `transaction()` and `locked_transaction()` both forward to `cm.transaction()`. Startup initializes schema, runs migrations through the raw writer, then installs organization schema inside the same manager context. Repositories share the manager but commonly own their method-level commit decisions.
- `rag/repositories/chat_repo.py` and `material_repo.py` show both styles: some methods use `cm.transaction(immediate=True)`; other methods use `write_connection()` followed by direct `conn.commit()`. `_in_tx` helpers rely on the caller to provide an already-open transaction. A caller chain must be traced before assigning one logical command a single owner.
- Wiki, BM25, feedback, memory, tracker, and evolution components also have private SQLite connections and direct commits. They are separate ownership boundaries unless a specific construction path proves otherwise. The external `DBSource` connector is not the application UserDB.
- Database transactions do not cover file promotion, Qdrant, FTS consumers, or model/network side effects. Their ordering and idempotency need a separate command/outbox/recovery review.

These are source-level observations, not a complete call graph. The candidate scanner intentionally records where code contains relevant syntax without inferring command reachability or cross-store atomicity.

## Remaining Unit of Work gaps

1. Map request, background, startup, migration, cleanup, recovery, and maintenance entry points to every repository/helper call in one logical write.
2. Identify which current methods commit directly, which depend on an outer `ConnectionManager.transaction()`, and which access a raw/private connection. Move toward one command owner only after each call chain and side effect is understood.
3. Decide nesting behavior explicitly: savepoint semantics or rejection. The shared manager currently reuses a transaction silently.
4. Define conditional-write conflict outcomes and whole-command retry rules, including idempotency/outbox protection for external side effects.
5. Keep private stores and external data-source connectors separate from the UserDB UoW unless a deliberate adapter boundary is designed for them.

## Verification plan and proof limits

The pure fixture tests cover exact manifest finding sets, source and inventory hashes, disposition/evidence requirements, stable exclusive output, AST aliases and lexical nesting, direct commits, private/external/unknown boundaries, assignment RHS constructor markers, `AnnAssign`, shadowed and rebound imports, evidence path/line validation, exact per-file evidence hash maps, cross-file staleness, parse errors, and non-execution of fixture source. The first RED run was authorized and completed before the tools existed: both target modules failed collection with the expected missing-module ImportError (native exit 2; see `artifacts/local-followups-2026-10-26/task-3-red.log`). The initial GREEN2 run tested the two new fixture modules plus the unchanged SQL inventory fixture module: child PID 27768, native exit 0, 30 passed in 20.50s. The later evidence-provenance RED/GREEN used the same three modules: RED child PID 40316/native exit 1 exposed the accepted forged refs; after the fix GREEN child PID 45952/native exit 0, 39 passed in 23.28s. Logs are under `artifacts/local-followups-2026-10-26/task-3-evidence-red.log` and `task-3-evidence-green.log` with stdout/stderr companions.

After the root's final source freeze, regenerate the SQL inventory, build an all-unreviewed manifest from that exact inventory, reapply manually reviewed rationale only to exact matching finding identities and source hashes, and run the manifest checker. An inventory with no unresolved SQL, a complete review manifest, or a complete parse report still does not establish PostgreSQL support. Adapter design, isolated PostgreSQL tests, transaction contract tests, migration/recovery evidence, and all ADR gates remain separate work.
