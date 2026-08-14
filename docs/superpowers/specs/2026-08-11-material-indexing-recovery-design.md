# Material Indexing Recovery Design

## Goal

Make a newly uploaded material usable: it must either become the current indexed version, or surface a retryable failure without preventing a permitted user from adding the staged source to a knowledge base.

## Design

The API owns one daemon `MaterialWorker` loop for its lifetime. It claims a bounded batch, records each result as `done`, `cancelled`, `retry`, or `failed`, and releases outstanding leases during shutdown before Qdrant is closed. Tests disable that loop explicitly through `app.state.disable_material_worker`.

The existing download endpoint remains ACL-first. When a visible document has no current version yet, it may serve its newest `staging` version from the confined document-version root. This supports the existing browser download-and-reupload flow for adding a just-uploaded file to another knowledge base. It never serves failed or historical versions.

## Error handling and verification

Worker exceptions are retried until `material_job_max_attempts`; the final job becomes `failed` with a bounded error code. A material download with no current or staging file remains a 404. Tests use isolated SQLite, version roots, fake workers, and fake embedding/vector boundaries only.
