# Enterprise Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add separated capabilities, sensitivity classification, tamper-evident audit, batch permissions, verified backup/restore, and idempotent departure handover.

**Architecture:** Extend the existing user database with capability and governance metadata while keeping content access behind existing document ACLs. High-risk operations use focused services with preview/idempotency, append-only audit events, and state machines.

**Tech Stack:** Python 3.12, FastAPI, SQLite WAL, Qdrant snapshots, pytest, Vue 3, TypeScript, Vitest

## Global Constraints

- `platform_admin` does not imply `hr_data_analyst`.
- Capabilities are composable and checked by backend routes.
- Sensitivity levels are exactly `internal | confidential | restricted`.
- Failed classification remains closed as `classification_pending`.
- Audit rows never contain tokens, passwords, physical paths, full questions, or payroll details.
- Batch changes require preview and preserve per-item prior state.
- Expired access is denied synchronously, independent of cleanup jobs.
- Restore always targets isolation first; failed restore never changes production.
- Handover preserves historical actor/uploader IDs.
- Real payroll/attendance stays disabled until supervisor approval.
- Use `superpowers:executing-plans`; no subagents unless requested.

---

## File Structure

- New: `rag/governance/{models,capabilities,classification,audit,permission_batches,backups,handovers,api}.py`.
- New tests: `tests/test_governance_*.py`.
- Modify: `rag/user_db.py`, `rag/auth.py`, `rag/api_security.py`, `rag/api.py`, `config.py`, `tests/conftest.py`.
- New frontend: `frontend/src/api/governance.ts`, `frontend/src/views/GovernanceView.vue`, focused governance components/tests.
- Modify: `frontend/src/views/KnowledgeOperationsView.vue`, `frontend/src/router/index.ts`.

---

### Task 1: Capability Model and Compatibility Migration

**Files:** Create `rag/governance/models.py`, `rag/governance/capabilities.py`, `tests/test_governance_capabilities.py`; modify `rag/user_db.py`, `rag/api_security.py`.

**Interfaces:** `has_capability(db, user, capability) -> bool`; `require_capability(capability)` FastAPI dependency; `set_user_capabilities(actor, user_id, values, expected_version)`.

- [ ] **Step 1: Write failing separation test**

```python
def test_platform_admin_does_not_inherit_hr_access(db, admin):
    db.set_user_capabilities(admin["id"], {"platform_admin", "content_admin"})
    assert has_capability(db, admin, "platform_admin")
    assert not has_capability(db, admin, "hr_data_analyst")
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_capabilities.py -v
```

Expected: FAIL because capability tables are absent.

- [ ] **Step 3: Add `capabilities`, `user_capabilities`, and versioned mutation**

Seed exact capability keys from the spec. Existing admins receive only `platform_admin` and `content_admin`. Mutation requires `platform_admin` and optimistic `capability_version`.

```python
def has_capability(db: UserDB, user: dict, key: str) -> bool:
    return key in db.list_user_capabilities(user["id"])
```

- [ ] **Step 4: Run capability/auth tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_capabilities.py tests\test_auth.py tests\test_api_security.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/governance/models.py rag/governance/capabilities.py rag/user_db.py rag/api_security.py tests/test_governance_capabilities.py
git commit -m "feat: separate governance capabilities"
```

---

### Task 2: Sensitivity Classification

**Files:** Create `rag/governance/classification.py`, `tests/test_governance_classification.py`; modify `rag/user_db.py`, `rag/document_versions.py`.

**Interfaces:** `scan_classification(source: SourceObject) -> ClassificationSuggestion`; `confirm_classification(document_id, actor, level, expected_updated_at)`.

- [ ] **Step 1: Write failing closed-state test**

```python
def test_scanner_failure_keeps_document_pending(classification_fixture):
    classification_fixture.scanner.fail()
    result = classification_fixture.upload("工资表.xlsx")
    assert result.classification_status == "classification_pending"
    assert classification_fixture.viewer("outsider").status_code == 403
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_classification.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add classification fields, scanner rules, and confirmation**

Scan filenames, headers, and configured patterns asynchronously. Raising sensitivity is owner/content-admin; lowering requires both owner confirmation and `content_admin`. Scanner output is advisory and never grants access.

```python
class ClassificationSuggestion(BaseModel):
    level: Literal["internal", "confidential", "restricted"]
    reasons: tuple[str, ...]
    requires_confirmation: bool = True
```

- [ ] **Step 4: Run classification/version tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_classification.py tests\test_document_versions.py tests\test_upload_security.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/governance/classification.py rag/user_db.py rag/document_versions.py tests/test_governance_classification.py
git commit -m "feat: classify sensitive knowledge"
```

---

### Task 3: Append-Only Audit Chain

**Files:** Create `rag/governance/audit.py`, `tests/test_governance_audit.py`; modify `rag/user_db.py`, `rag/api.py`.

**Interfaces:** `AuditEventInput`; `append_audit_event(db, event) -> int`; `verify_audit_chain(db, start_id, end_id) -> AuditVerification`.

- [ ] **Step 1: Write failing tamper test**

```python
def test_modified_audit_row_breaks_chain(audit_db):
    first = append_audit_event(audit_db, event(action="download"))
    second = append_audit_event(audit_db, event(action="permission.change"))
    audit_db.unsafe_test_update(second, action="login")
    assert verify_audit_chain(audit_db, first, second).valid is False
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_audit.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add canonical JSON hashing and redaction**

Hash `previous_hash + canonical_event_json`. Serializer accepts resource IDs and safe summaries only; reject keys matching `token|password|file_path|salary_rows|question_text`.

```python
current_hash = hashlib.sha256(
    bytes.fromhex(previous_hash) + canonical_json(event).encode("utf-8")
).hexdigest()
```

- [ ] **Step 4: Run audit and API regression**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_audit.py tests\test_upload_security.py tests\test_feedback.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/governance/audit.py rag/user_db.py rag/api.py tests/test_governance_audit.py
git commit -m "feat: add tamper-evident audit"
```

---

### Task 4: Batch and Expiring Permissions

**Files:** Create `rag/governance/permission_batches.py`, `tests/test_permission_batches.py`; modify `rag/user_db.py`, `rag/permissions.py`.

**Interfaces:** `preview_permission_batch(request, actor) -> BatchPreview`; `execute_permission_batch(preview_id, idempotency_key) -> BatchResult`; `rollback_permission_batch(batch_id, actor) -> BatchResult`.

- [ ] **Step 1: Write failing non-clobbering rollback test**

```python
def test_rollback_does_not_overwrite_later_manual_change(batch_fixture):
    batch = batch_fixture.grant("alice", document_ids=[1])
    batch_fixture.manual_change("alice", 1, "edit")
    result = batch_fixture.rollback(batch.id)
    assert result.conflicts == [1]
    assert batch_fixture.permission("alice", 1) == "edit"
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_permission_batches.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add preview snapshot, item rows, expiry, and compare-and-set rollback**

Restricted resources require the dedicated capability and cannot enter ordinary batches. Permission reads add `valid_from <= now < expires_at`; expired rows are denied even before cleanup.

```sql
UPDATE document_shares SET permission=?, valid_from=?, expires_at=?, version=version+1
WHERE id=? AND version=?;
```

- [ ] **Step 4: Run permission/security tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_permission_batches.py tests\test_permissions.py tests\test_document_security_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/governance/permission_batches.py rag/user_db.py rag/permissions.py tests/test_permission_batches.py
git commit -m "feat: add reversible permission batches"
```

---

### Task 5: Verified Backup and Isolated Restore

**Files:** Create `rag/governance/backups.py`, `tests/test_governance_backups.py`; modify `config.py`.

**Interfaces:** `create_backup(context) -> BackupRecord`; `verify_backup(backup_id) -> BackupVerification`; `restore_test(backup_id, isolation_root) -> RestoreReport`.

- [ ] **Step 1: Write failing corruption test**

```python
def test_corrupt_backup_cannot_be_restored(backup_fixture):
    backup = backup_fixture.create()
    backup_fixture.corrupt(backup, "users.db")
    verification = backup_fixture.verify(backup)
    assert verification.status == "verification_failed"
    with pytest.raises(BackupNotRestorable):
        backup_fixture.restore_test(backup)
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_backups.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement manifests and isolation-only restore**

Manifest covers SQLite snapshot, immutable versions, structured datasets, Qdrant snapshot/rebuild manifest, schema version, sizes, and SHA-256. Secrets are excluded. Restore uses a caller-provided isolated root and never replaces production paths.

```python
def restore_test(record: BackupRecord, isolation_root: Path) -> RestoreReport:
    target = ensure_child_path(isolation_root, record.id)
    verify_manifest(record)
    return restore_into(target, record)
```

- [ ] **Step 4: Run backup tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_backups.py tests\test_existing_document_version_migration.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add config.py rag/governance/backups.py tests/test_governance_backups.py
git commit -m "feat: verify backup and restore"
```

---

### Task 6: Departure Handover State Machine

**Files:** Create `rag/governance/handovers.py`, `tests/test_governance_handovers.py`; modify `rag/user_db.py`, `rag/auth.py`.

**Interfaces:** `preview_handover(user_id, recipient_id) -> HandoverPreview`; `run_handover(handover_id, idempotency_key) -> HandoverRecord`.

- [ ] **Step 1: Write failing resume test**

```python
def test_handover_resumes_without_rewriting_historical_actor(handover_fixture):
    run = handover_fixture.start(fail_after="sessions_revoked")
    completed = handover_fixture.resume(run.id)
    assert completed.status == "completed"
    assert handover_fixture.document_owner() == handover_fixture.recipient_id
    assert handover_fixture.audit_actor_for_upload() == handover_fixture.departing_id
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_handovers.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement persisted state transitions**

Disable account, revoke sessions, revoke granted access, transfer active ownership/tasks/subscriptions, then verify no active orphan. Keep users, uploaders, feedback authors, messages, and audit actors unchanged.

```python
_HANDOVER_STEPS = (
    "account_disabled", "sessions_revoked", "assets_assigned", "verified", "completed"
)
```

- [ ] **Step 4: Run handover/auth tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_handovers.py tests\test_auth.py tests\test_conversations.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/governance/handovers.py rag/user_db.py rag/auth.py tests/test_governance_handovers.py
git commit -m "feat: add departure handover"
```

---

### Task 7: Governance API, UI, and Verification

**Files:** Create `rag/governance/api.py`, `tests/test_governance_api.py`, `frontend/src/api/governance.ts`, `frontend/src/views/GovernanceView.vue` and component tests; modify `rag/api.py`, `frontend/src/views/KnowledgeOperationsView.vue`.

**Interfaces:** Produces the capability, classification, batch, audit, backup, and handover routes from the spec.

- [ ] **Step 1: Write failing role UI/API tests**

```python
def test_backup_operator_cannot_download_document(isolated_api, backup_headers, document_id):
    assert isolated_api.post("/backups", headers=backup_headers).status_code == 200
    assert isolated_api.get(f"/documents/{document_id}/download", headers=backup_headers).status_code == 403
```

```typescript
it('shows governance modules only for matching capabilities', async () => {
  mockUser({ capabilities: ['auditor'] })
  const wrapper = mountGovernance()
  await flushPromises()
  expect(wrapper.text()).toContain('审计')
  expect(wrapper.text()).not.toContain('离职交接')
})
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_governance_api.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/views/GovernanceView.test.ts
Set-Location ..
```

Expected: missing routes/components.

- [ ] **Step 3: Implement router and capability-gated modules**

Mount one router. Frontend consumes `/me` capabilities; backend independently enforces every action. Pagination and stable error codes are mandatory.

```python
router = APIRouter(prefix="/governance", dependencies=[Depends(require_user)])
router.add_api_route("/audit-events", list_audit_events, dependencies=[Depends(require_capability("auditor"))])
```

- [ ] **Step 4: Run full verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/governance tests/test_governance_capabilities.py tests/test_governance_classification.py tests/test_governance_audit.py tests/test_permission_batches.py tests/test_governance_backups.py tests/test_governance_handovers.py tests/test_governance_api.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
```

Expected: PASS.

- [ ] **Step 5: Document and commit**

```powershell
git add rag/governance rag/api.py tests frontend/src docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify enterprise governance"
```
