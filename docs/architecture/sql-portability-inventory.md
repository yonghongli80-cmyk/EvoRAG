# SQL portability inventory

**Status: local static inventory tooling; no PostgreSQL adapter, migration, or compatibility result is claimed.** The source scanner reads Python and SQL files under an explicitly supplied `rag` root. It does not import or execute application modules, open databases, read `.env` or runtime data, install drivers, or rewrite SQL.

## Running the inventory

Use a new output path so earlier evidence is never replaced:

```powershell
python -m scripts.audit_sql_portability --root rag --output artifacts/local-prep-2026-10-26/sql-portability-inventory.json
```

The output argument is exclusive: an existing file causes a write failure. Without `--output`, the report is printed to stdout. Exit code `0` means the requested source root contains supported source files and all discovered `.py` and `.sql` files were read and parsed without unresolved SQL sinks; exit code `1` means the report is explicit but incomplete (for example, a missing/empty root, a dynamic SQL expression, or a parse error); exit code `2` means argument parsing or report writing failed. A report is still emitted for incomplete scans when the output path is writable.

The official repository-wide counts are intentionally not embedded here. Generate the report from the root's final frozen source tree and record its artifact path and summary in the implementation handoff. A scan of an earlier or dirty tree is only a snapshot and must not be relabelled as the final inventory.

## Report contract

The stable JSON report contains:

- `entries`: statically recovered SQL sinks and SQL-file statements, each with repository-relative `path`, source `line`, enclosing `owner`, sink kind, static classification, SQL text, and recognized `features`.
- `unresolved`: SQL sink calls whose text depends on a variable, f-string, unsupported expression, missing argument, or whose static SQL has an unterminated quoted/comment region that prevents safe transaction-marker splitting. The entry records the path, line, owner, sink, and reason; the scanner does not guess the resulting SQL.
- `transaction_markers`: source positions for `transaction`, `commit`, `rollback`, `read_connection`, and `write_connection` calls, plus explicit transaction statements in each lexically split statement of Python SQL literals and SQL files. This includes multi-statement `executescript` literals while ignoring transaction-like words in comments and quoted values. For Python SQL literals, `line` anchors to the argument expression's starting AST line; `sql_relative_line` and `statement_index` locate the marker within decoded SQL. These fields do not map back to the exact source character or physical token line. SQL-file markers keep the file's actual line. These are evidence locations, not a proof of transaction ownership or atomicity.
- `code_markers`: source positions for non-SQL portability markers detected in Python, currently `lastrowid`.
- `errors`: missing/non-directory roots, unreadable files, Python syntax errors, SQL lexical errors, and skipped symbolic links. An error is never silently counted as a successful parse.
- `coverage`: discovered and scanned file counts, Python/SQL file counts, unresolved/error counts, and a `complete` flag. `complete` is false whenever an error or unresolved sink exists; it does not mean PostgreSQL compatibility when true.

Feature labels include SQLite-specific or dialect-sensitive constructs such as `PRAGMA`, `AUTOINCREMENT`, `INSERT OR IGNORE/REPLACE`, `REPLACE INTO`, `ROWID`, FTS virtual-table/MATCH markers, `strftime`, `json_extract`, `lastrowid`, qmark placeholders, and `WITHOUT ROWID`. Labels identify review candidates; they do not say that every other statement is portable.

## Parsing limits

Python is parsed with the standard-library AST. Literal strings and concatenations of literal strings are inventoried; variable-based SQL and f-strings remain unresolved. The scanner does not infer runtime assignments or follow values across functions.

SQL files are split at semicolons outside common quoted and commented regions. Comments and quoted text are blanked before feature matching so words in comments or string literals are not reported as active SQL. This is a conservative lexical pass, not a full SQL grammar: vendor procedural blocks, unusual quoting, templating, generated SQL, macros, and dialect-specific escaping may be missed or reported as lexical errors. Review unresolved/error coverage before using the inventory to plan migration.

## Existing code context

The PostgreSQL ADR records the current application boundary: `UserDB` builds a SQLite `ConnectionManager`; runtime schema migration uses SQLite `user_version`; repositories still use SQLite transaction and SQL behavior; material search is FTS5; and the optional `psycopg2` data-source connector is not an application UserDB adapter. See [the PostgreSQL data-access ADR](data-access-postgresql.md) for the source-level evidence and migration gates.

This inventory is a preparation artifact. It does not implement or enable a PostgreSQL connection, prove repository behavior equivalence, choose a Chinese full-text strategy, migrate data, or authorize live shadow reads.
