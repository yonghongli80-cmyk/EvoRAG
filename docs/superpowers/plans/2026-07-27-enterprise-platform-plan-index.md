# Enterprise Knowledge Platform Plan Index

This index fixes the execution order for the single RAGv3 platform. Each linked plan produces independently testable software and must be executed with `superpowers:executing-plans`.

> **Current-session entry:** read `docs/superpowers/plans/2026-08-09-post-lifecycle-plan-amendment.md` before this index. It records the post-lifecycle baseline, non-negotiable security constraints, and overrides the historical ordering below where they conflict.

## Required Order

1. `docs/superpowers/plans/2026-07-27-trusted-knowledge-loop.md` (complete)
2. `docs/superpowers/plans/2026-07-28-incremental-material-lifecycle.md` (complete)
3. `docs/superpowers/plans/2026-07-27-answer-reliability.md`
4. Enterprise governance Tasks 1–4 from `docs/superpowers/plans/2026-07-27-enterprise-governance.md`
5. `docs/superpowers/plans/2026-07-27-spreadsheet-knowledge.md`
6. `docs/superpowers/plans/2026-08-05-customer-speech-knowledge-governance.md`
7. `docs/superpowers/plans/2026-07-27-employee-experience.md`
8. `docs/superpowers/plans/2026-07-27-assistant-platform.md`
9. `docs/superpowers/plans/2026-07-27-advanced-assistants.md`
10. Remaining enterprise governance tasks and engineering infrastructure as production-release gates.

## Checkpoints

- Steps 1 and 2 are complete in baseline `74aaa94`; do not re-execute them.
- Do not start answer reliability until the incremental material lifecycle migration and tombstone read-path audit are confirmed in the current checkout. Record any test coverage limited by the execution environment.
- Answer reliability Tasks 1–3 are required before spreadsheet query answers are exposed.
- Do not import the customer-service/sales/streamer materials as published scripts until steps 1–5 are implemented; until then, they remain source files and review candidates only.
- Spreadsheet storage/query and assistant runtime are required before the data-analysis assistant.
- Governance is required before company-wide rollout; ordinary sales/project/non-sensitive spreadsheet development may proceed with synthetic data while supervisor approval for payroll/attendance is pending.
- Employee experience must reuse current ACL and permission-version invalidation.
- Assistant profiles are modes in the same Vue/FastAPI deployment, not separate applications.

## Workbench Start

```powershell
$project = 'C:\Users\Administrator\Desktop\RAGv3\RAGv3'
Set-Location $project
git status --short
git log -6 --oneline
```

Read the amendment and current plan completely, invoke `superpowers:executing-plans`, execute one task at a time, and stop at each plan's verification checkpoint before advancing.
