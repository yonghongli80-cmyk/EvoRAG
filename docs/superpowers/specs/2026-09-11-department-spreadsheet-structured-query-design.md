# Department Spreadsheet Structured Query Design

Date: 2026-09-11
Status: Approved design, pending implementation plan

## Purpose

Add a structure-first query path for spreadsheet data in departmental knowledge bases. Numeric answers must come from typed, read-only database operations rather than vector similarity or model arithmetic. Semantic retrieval remains available for explanatory and qualitative questions.

## Scope

The first release supports:

- exact lookup and typed filters;
- count and distinct count;
- sum, average, minimum, and maximum;
- Top N ordering, with N limited to 100;
- queries across all current spreadsheet versions the user may access in the active departmental knowledge base;
- union-style aggregation across multiple tables with compatible schemas;
- source citations down to document, worksheet, and row or cell range.

The first release does not support arbitrary SQL, joins between unrelated schemas, formulas that mutate workbooks, or queries spanning different knowledge bases.

## Design Principles

1. Numeric truth comes from structured data operations.
2. The model may select and explain a validated plan but may not execute or supply arbitrary SQL.
3. Authorization and current-version filtering happen before schema discovery or data access.
4. Ambiguous fields, incompatible types, or inconsistent units produce a clarification response, never a guessed number.
5. Results remain auditable through source ranges, selected datasets, and scanned-row counts.

## Architecture

The chat pipeline gains a spreadsheet query coordinator beside the existing semantic retrieval path:

1. Classify the question as structured, semantic, or hybrid.
2. Establish the active departmental knowledge-base scope and authorized current document versions.
3. Search a lightweight schema catalog using document name, worksheet name, column display name, normalized aliases, inferred type, row count, representative values, and update time.
4. Map the question to candidate datasets and columns.
5. Validate compatibility across candidates and either request clarification or create an allowlisted query plan.
6. Execute the plan against read-only SQLite datasets.
7. Return canonical values, source ranges, selected datasets, and rows scanned.
8. For hybrid questions, give the structured result and relevant semantic evidence to the answer generator for explanation.

The semantic path remains unchanged for questions that do not require exact table computation.

## Components

### Query classifier

Classifies user intent without inspecting unauthorized schemas. It recognizes lookup, filter, aggregate, ranking, and qualitative intents. Deterministic numeric cues are evaluated before any model-based classification.

### Authorized schema catalog

Provides metadata only for current spreadsheet versions in the active knowledge base that the user can view. Each catalog entry contains stable dataset and sheet identifiers, document and sheet names, columns, inferred types, row counts, small representative value samples, unit hints, and version timestamps.

The catalog is updated when a spreadsheet dataset becomes current and invalidated when a new version is activated, recycled, restored, or loses authorization.

### Candidate selector

Ranks sheets and columns by exact normalized matches, aliases, type compatibility, representative values, and semantic schema similarity. It does not scan full worksheet contents during selection.

If one interpretation is not clearly dominant, it returns candidate choices and the conflicting fields to the user.

### Safe query planner

Produces a fixed JSON plan. The plan may contain only:

- dataset and sheet identifiers selected from the authorized catalog;
- allowlisted column keys;
- typed filters using `eq`, `ne`, `gt`, `gte`, `lt`, `lte`, `contains`, `between`, or `in`;
- aggregate functions `count`, `distinct_count`, `sum`, `average`, `min`, or `max`;
- ordering and Top N up to 100;
- a result-row limit up to 100.

Validation rejects unknown identifiers, unauthorized datasets, cross-schema joins, incompatible types, missing filter operands, and unsupported operations.

### Multi-table compatibility checker

Tables may be unioned only when the columns used by the plan map unambiguously and have compatible inferred types and units. Compatibility uses normalized names and explicit aliases but retains each source column key in the execution plan.

Different document versions of the same logical document are never combined; only the current version is eligible. Duplicate dataset identifiers and duplicate source rows introduced by repeated selection are removed. Business-level duplicate rows are not silently removed because identical rows can represent legitimate transactions.

If units, date semantics, or column meanings conflict, execution stops with a clarification response listing the affected tables and fields.

### Structured executor

Extends the existing typed spreadsheet query gateway. It opens dataset files in SQLite read-only mode, applies typed predicates, executes aggregation or ranking inside SQLite, and combines compatible per-dataset partial results deterministically.

Numeric calculations use decimal-safe canonical values. Date comparisons use normalized stored date values. String collation behavior is explicit and consistent across datasets.

### Answer composer

Exact values are passed to the model as immutable computed facts. The model may summarize or explain them but must not recalculate or replace them. The response distinguishes computed results from qualitative interpretation.

Every result includes document name, worksheet, row or cell range, current version, and the number of rows scanned. Multi-table results list every contributing source.

## Query Behavior

Examples of supported questions include:

- “佳能各品类的销量合计是多少？”
- “2025 年之后金额最高的 10 条记录。”
- “每个部门有多少个不同客户？”
- “筛选状态为已完成且金额大于 10 万的记录。”

For a qualitative question such as “哪些品类表现异常，原因可能是什么？”, the coordinator first computes relevant totals or trends, then combines those facts with semantic evidence for explanation.

## Ambiguity and Error Handling

The system asks a focused clarification question when:

- multiple columns plausibly match the requested business term;
- candidate tables use incompatible types or units;
- the requested period, aggregation dimension, or knowledge-base scope is missing and materially changes the answer;
- the relevant spreadsheet has not finished structured indexing;
- a requested join or calculation is outside the allowlist.

It reports unsupported operations, query limits, and processing failures in user-facing language. It must not silently fall back to vector-only numeric answers.

## Performance

- Schema selection reads only the catalog, not full cell data.
- SQLite indexes are created for frequently filtered typed columns, prioritizing identifiers, categories, dates, and common status fields.
- Aggregation occurs inside SQLite; raw tables are not sent to the model.
- Normalized question classification, schema selection, and final structured results may be cached.
- Cache keys include knowledge-base identity, user authorization scope, active dataset versions, and normalized plan. Dataset activation and permission changes invalidate affected entries.
- Ordinary local filters and aggregates target approximately one second of structured execution time, excluding model interpretation latency.

## Security

- The active knowledge base and document ACLs constrain the catalog before planning.
- Dataset and column identifiers come from server-side authorization results, never directly from model text.
- SQLite connections are read-only.
- Generated SQL is assembled only from validated identifiers and parameterized values.
- Audit records capture classification, selected datasets, validated plan, execution status, rows scanned, and clarification or rejection reason without exposing physical file paths.

## API and Response Shape

The existing chat request remains the user-facing entry point. Internally, the coordinator returns one of:

- `structured_result`: canonical values, preview rows, citations, selected datasets, and rows scanned;
- `hybrid_result`: structured facts plus authorized semantic evidence;
- `clarification_required`: a concise question and structured candidate options;
- `unsupported`: an explainable reason code and supported alternatives.

This preserves the current chat UI while allowing it to render exact-result and clarification states explicitly.

## Testing

Automated tests cover:

- intent classification for exact and qualitative questions;
- single-table lookup, typed filtering, every supported aggregate, and Top N;
- compatible multi-table aggregation and deterministic partial-result merging;
- rejection of unit, type, and field ambiguities;
- current-version-only behavior and duplicate dataset suppression;
- knowledge-base and document permission isolation;
- query-plan validation and resistance to arbitrary SQL or unknown identifiers;
- citation document, sheet, and range accuracy;
- cache invalidation after version and authorization changes;
- hybrid answers preserving computed values;
- no vector-only fallback for unresolved numeric questions;
- representative department-scale performance benchmarks.

End-to-end tests upload multiple compatible and incompatible spreadsheets, wait for structured indexing, issue natural-language questions through chat, and verify exact results, clarification behavior, citations, and authorization boundaries.

## Delivery Sequence

1. Extend structured query models and execution for lookup, typed ordering, Top N, and compatible multi-dataset aggregation.
2. Add authorized schema catalog access and compatibility checks.
3. Add deterministic intent cues and the validated natural-language planning boundary.
4. Integrate the coordinator into the chat pipeline with structured, hybrid, clarification, and unsupported outcomes.
5. Render structured citations and clarification choices in the existing chat UI.
6. Add cache invalidation, auditing, benchmarks, and end-to-end verification.

## Success Criteria

- Supported numeric questions return database-computed values rather than model-computed values.
- Queries can aggregate compatible current spreadsheets across one departmental knowledge base.
- Unauthorized and historical versions never influence selection or results.
- Ambiguous or incompatible data produces clarification instead of a guessed answer.
- Every computed answer identifies all contributing sources and ranges.
- Representative ordinary local queries meet the structured execution target or expose measured bottlenecks without weakening correctness.
