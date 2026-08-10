# Persisted document comparison design

## Goal

Enable the existing permission-first document comparison endpoint with real,
traceable comparison citations.  A comparison citation is not an answer
citation and must never be represented by a document version ID or an
`answer_citations.id`.

## Scope and invariants

- Every selected document is authorized before retrieval or model/tool input.
- Each retrieval is restricted to the selected document's current version.
- A supported comparison cell has one or more comparison citations.  Missing
  evidence yields `insufficient_evidence`; one document never fills another
  document's cell.
- Comparison data belongs to its requesting user.  Reading it rechecks both
  ownership (or administrator authority) and the current ACL for every linked
  document.
- An inaccessible, deleted, or no-longer-current source is not named or
  otherwise disclosed in a response.
- The existing answer-citation schema and chat history remain untouched.

## Data model

Add a comparison aggregate with three private persistence concepts:

1. `document_comparisons` records the requester, question, requested
   dimensions, selected document/version snapshot, result status, and
   timestamps.
2. `comparison_cells` stores one result cell per comparison, dimension, and
   document.  It records `supported` or `insufficient_evidence` plus supported
   text only.
3. `comparison_citations` belongs to a cell and stores the document ID,
   version ID, chunk index, section title, preview, rank, and historical flag.

The selected document/version snapshot is retained for auditability, but is
never sufficient by itself to authorize a later read.  Foreign keys cascade
from comparison to cells and citations.

## Request and retrieval flow

1. `POST /reliability/compare` resolves all supplied IDs and checks view
   permission for each before it creates a scope or invokes a tool.
2. A configured comparison adapter receives only the immutable authorized
   scope and queries each document separately, filtering retrieval to that
   document's current version.
3. The adapter maps retriever hits into evidence with source locations.  The
   workflow maps valid evidence to supported cells and failures or empty
   evidence to `insufficient_evidence` cells.
4. The service persists the comparison, cells, and independent citations in a
   single transaction.  It then returns the persisted result with a
   `comparison_id` and comparison-citation IDs.

## Read and failure behavior

A read endpoint returns a comparison only to its owner or an administrator,
after rechecking view permission for every referenced document.  Any failed
ownership or ACL check returns a generic authorization/not-found response
without document names, paths, raw IDs, source snippets, or version details.

If the retrieval backend is unavailable, the API returns a generic 503.  A
per-document tool failure is a normal partial comparison: only that cell is
marked `insufficient_evidence`, with no invented citations.

## API compatibility

The comparison response grows a `comparison_id`; each cell retains its
existing status/text fields and exposes only IDs from `comparison_citations`.
The frontend may continue to render the current table and can later link its
citation IDs to an authorized comparison-read endpoint.  Existing answer
reliability and answer-citation APIs are unchanged.

## Verification

Tests are written first and prove:

- the adapter retrieves only the authorized document's current version;
- supported cells persist independent comparison citations;
- insufficient evidence persists no citation;
- a user cannot read another user's comparison;
- loss of ACL after persistence prevents source disclosure;
- the configured production API completes a comparison rather than returning
  503 solely because no comparison tool was wired.

Focused backend tests, the full backend suite, frontend tests/build, Ruff, and
`pip check` are run before completion.  Test fixtures use temporary SQLite,
temporary files, and isolated vector collections only.
