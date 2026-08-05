# Enterprise Knowledge Platform Plan Index

This index fixes the execution order for the single RAGv3 platform. Each linked plan produces independently testable software and must be executed with `superpowers:executing-plans`.

## Required Order

1. `docs/superpowers/plans/2026-07-27-trusted-knowledge-loop.md`
2. `docs/superpowers/plans/2026-07-28-incremental-material-lifecycle.md`
3. `docs/superpowers/plans/2026-07-27-answer-reliability.md`
4. `docs/superpowers/plans/2026-07-27-spreadsheet-knowledge.md`
5. `docs/superpowers/plans/2026-08-05-customer-speech-knowledge-governance.md`
6. `docs/superpowers/plans/2026-07-27-enterprise-governance.md`
7. `docs/superpowers/plans/2026-07-27-employee-experience.md`
8. `docs/superpowers/plans/2026-07-27-assistant-platform.md`
9. `docs/superpowers/plans/2026-07-27-advanced-assistants.md`

## Checkpoints

- Do not start step 2 until the trusted knowledge loop migration and full regression pass.
- Do not start answer reliability until the incremental material lifecycle migration, tombstone read-path audit, and full verification pass.
- Answer reliability Tasks 1–3 are required before spreadsheet query answers are exposed.
- Do not import the customer-service/sales/streamer materials as published scripts until steps 1–5 are implemented; until then, they remain source files and review candidates only.
- Spreadsheet storage/query and assistant runtime are required before the data-analysis assistant.
- Governance is required before company-wide rollout; ordinary sales/project/non-sensitive spreadsheet development may proceed with synthetic data while supervisor approval for payroll/attendance is pending.
- Employee experience must reuse current ACL and permission-version invalidation.
- Assistant profiles are modes in the same Vue/FastAPI deployment, not separate applications.

## Workbench Start

```powershell
$project = 'C:\Users\Administrator\Desktop\RAGv3\RAGv3\.worktrees\security-boundary-hardening'
Set-Location $project
git status --short
git log -6 --oneline
```

Read the current plan completely, invoke `superpowers:executing-plans`, execute one task at a time, and stop at each plan's full-verification checkpoint before advancing.
