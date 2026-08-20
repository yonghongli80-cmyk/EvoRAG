# Organizational Knowledge Base Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fixed 280-person fictional organization whose accounts receive organization-scoped knowledge-base access while preserving the File page's existing private/shared/public permissions.

**Architecture:** Introduce a focused `rag/org_auth/` package for schema access, deterministic seed data, policy evaluation, migration, and audit. All knowledge-base HTTP and retrieval paths consume one `OrgAuthorizationService`; existing `rag.permissions.check_doc_permission()` remains authoritative for the File page. The Vue client renders capabilities returned by the server and never makes security decisions by itself.

**Tech Stack:** Python 3.12, FastAPI, SQLite, python-jose, pytest, Vue 3, Pinia, Vue Router, Element Plus, TypeScript 6, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-20-organizational-knowledge-base-authorization-design.md`

## Global Constraints

- Use strict TDD: add a failing behavior test, observe the expected failure, implement the minimum behavior, then rerun the focused suite.
- Organization seed totals are exactly 61 units, 280 accounts, 49 managers, 8 secondary memberships, 2 company administrators, and 65 knowledge bases.
- Account IDs are `demo001` through `demo280`; every initial password is `123456`; no forced password change is added.
- Do not show “演示环境”, “测试账号”, or equivalent labels in the UI.
- Knowledge bases have organization roles `none`, `read`, `manage`, and `administer`; they do not use personal private/shared/public or personal shares.
- Knowledge-base documents inherit the knowledge base role. File-page documents retain the existing private/shared/public model unchanged.
- Unauthorized resource access returns 404 or an empty result without resource metadata. Authorization/filter failures fail closed.
- All compound SQLite writes use the existing `UserDB` lock and a single transaction; do not open ad-hoc SQLite connections.
- All fictional text must pass a denylist scan derived from names and terms in `C:\Users\Administrator\Desktop\金典拍拍知识库`; no source-project identity text may be copied.
- Each task ends with its own Git commit. Do not mix unrelated cleanup into these commits.

## File Structure

- Create `rag/org_auth/models.py`: enums and immutable authorization/profile result types.
- Create `rag/org_auth/schema.py`: idempotent organization tables and indexes.
- Create `rag/org_auth/repository.py`: all locked organization reads and transactional writes.
- Create `rag/org_auth/seed_manifest.py`: deterministic fictional organization, people, memberships, libraries, and Markdown fixtures.
- Create `rag/org_auth/seed.py`: seed validation, persistence, resumable content indexing, and CLI.
- Create `rag/org_auth/policy.py`: pure role resolution with deny precedence and sensitivity checks.
- Create `rag/org_auth/service.py`: repository-backed authorization façade used by APIs and retrieval.
- Create `rag/org_auth/migration.py`: legacy knowledge-base classification as `unassigned`.
- Create `rag/org_auth/audit.py`: redacted audit event writer/query service.
- Create `rag/org_auth/api.py`: directory, organization administration, preview, grant, and audit routes.
- Modify `rag/user_db.py`: safe locked-transaction adapter, security fields, and org schema installation.
- Modify `rag/api_security.py`, `rag/api.py`, `rag/versioned_retrieval.py`, `rag/pipeline.py`, and `rag/agent.py`: identity validation and unified KB enforcement.
- Create `frontend/src/api/organization.ts`: typed organization APIs.
- Create `frontend/src/components/AccountPicker.vue` and `frontend/src/components/OrganizationTree.vue`.
- Create `frontend/src/views/OrganizationAdminView.vue`.
- Modify `frontend/src/views/LoginView.vue`, `KBModeView.vue`, `KnowledgeDetailView.vue`, `MainLayout.vue`, router, and auth store.
- Create focused backend tests under `tests/test_org_*.py` and focused frontend tests next to the affected views/components.

---

### Task 1: Organization Schema and Locked Repository Boundary

**Files:**
- Create: `rag/org_auth/__init__.py`, `rag/org_auth/models.py`, `rag/org_auth/schema.py`, `rag/org_auth/repository.py`
- Modify: `rag/user_db.py`
- Test: `tests/test_org_schema.py`, `tests/test_org_repository.py`, `tests/test_sqlite_unified.py`

**Interfaces:**
- Produces `OrgRole`, `KnowledgeBaseType`, `AuthorizationDecision`, and `PersonProfile`.
- Produces `UserDB.locked_transaction(immediate: bool = False)` and `OrgRepository(db: UserDB)`.

- [ ] **Step 1: Write failing schema and transaction tests**

```python
def test_org_schema_is_idempotent_and_enforces_stable_keys(tmp_path):
    db = UserDB(str(tmp_path / "org.db"))
    OrgRepository(db).install_schema()
    OrgRepository(db).install_schema()
    assert OrgRepository(db).table_names() >= {
        "org_units", "org_people", "org_memberships", "org_manager_scopes",
        "org_sensitive_qualifications", "kb_org_policies", "kb_org_grants",
        "org_audit_events", "org_seed_state",
    }

def test_locked_transaction_rolls_back_all_rows(db):
    repo = OrgRepository(db)
    with pytest.raises(ValueError):
        repo.create_unit_pair_then_fail("OU-001", "OU-001")
    assert repo.get_unit("OU-001") is None
```

- [ ] **Step 2: Run tests and confirm the red state**

Run: `python -m pytest tests/test_org_schema.py tests/test_org_repository.py tests/test_sqlite_unified.py -q`  
Expected: collection/import failure because `rag.org_auth` and `locked_transaction` do not exist.

- [ ] **Step 3: Implement the minimal schema and repository**

Use CHECK/UNIQUE/FK constraints for unit codes, person codes, one primary membership, grant subject/effect/role, KB type, and sensitivity. Add this public lock adapter instead of exposing `_conn` to callers:

```python
@contextmanager
def locked_transaction(self, immediate: bool = False):
    with self._lock:
        with self.transaction(immediate=immediate):
            yield self._conn
```

`OrgRepository.install_schema()` calls `install_org_schema(conn)` inside this adapter. Repository reads acquire `db._lock` only through a new `UserDB.locked_read()` context manager; no org module opens SQLite independently.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_org_schema.py tests/test_org_repository.py tests/test_sqlite_unified.py -q`  
Expected: PASS, including rollback and concurrent-read checks.

- [ ] **Step 5: Commit**

```powershell
git add rag/org_auth rag/user_db.py tests/test_org_schema.py tests/test_org_repository.py tests/test_sqlite_unified.py
git commit -m "feat: add organization authorization schema"
```

### Task 2: Deterministic Fictional Seed and Resumable Content Indexing

**Files:**
- Create: `rag/org_auth/seed_manifest.py`, `rag/org_auth/seed.py`
- Test: `tests/test_org_seed_manifest.py`, `tests/test_org_seed.py`

**Interfaces:**
- Produces `build_seed_manifest() -> SeedManifest` and `seed_organization(db, index_document) -> SeedReport`.
- `SeedReport` contains exact created/skipped/failed counts and a `complete` flag.

- [ ] **Step 1: Write failing deterministic-seed tests**

```python
def test_manifest_has_fixed_counts_and_identity_mapping():
    manifest = build_seed_manifest()
    assert (len(manifest.units), len(manifest.people), len(manifest.managers)) == (61, 280, 49)
    assert len([m for m in manifest.memberships if not m.primary]) == 8
    assert len(manifest.knowledge_bases) == 65
    assert [p.username for p in manifest.people] == [f"demo{i:03d}" for i in range(1, 281)]
    assert len({(p.username, p.display_name) for p in manifest.people}) == 280

def test_seed_is_idempotent_and_does_not_overwrite_edits(db, fake_indexer):
    seed_organization(db, fake_indexer)
    OrgRepository(db).rename_person("P-001", "林知远甲")
    second = seed_organization(db, fake_indexer)
    assert second.created_people == 0
    assert OrgRepository(db).get_person("P-001").display_name == "林知远甲"
```

Also assert all passwords authenticate as `123456`, organization depth is four, two admins exist, every KB has one or two Markdown fixtures, indexing resumes after a simulated failure, and a reference-project denylist scan returns no matches.

- [ ] **Step 2: Run tests and confirm missing manifest/seed failures**

Run: `python -m pytest tests/test_org_seed_manifest.py tests/test_org_seed.py -q`  
Expected: FAIL because seed interfaces are absent.

- [ ] **Step 3: Implement fixed data generation and CLI**

Build 61 stable nodes from one company root, seven top-level groups, 21 departments, and 32 teams. Generate 280 unique fixed names from 20 fictional surnames × 14 fictional given names, assign stable positions and memberships, assign 49 explicit manager scopes and eight secondary memberships, and define one public, 61 organizational, and three sensitive KBs. Use `hash_password("123456")` only when an account is missing.

Expose `python -m rag.org_auth.seed --db-path <path>`; persist each fixture's indexing state so a failed document resumes without duplicating completed documents. Do not perform external embedding work inside application startup.

- [ ] **Step 4: Run seed tests and a temporary-database smoke test**

Run: `python -m pytest tests/test_org_seed_manifest.py tests/test_org_seed.py -q`  
Run: `New-Item -ItemType Directory -Force .tmp | Out-Null; python -m rag.org_auth.seed --db-path .tmp/org-seed-smoke.db --metadata-only`  
Expected: PASS and a report of 61 units, 280 people, 65 KB policies, with a second invocation reporting zero duplicate creations.

- [ ] **Step 5: Commit**

```powershell
git add rag/org_auth/seed_manifest.py rag/org_auth/seed.py tests/test_org_seed_manifest.py tests/test_org_seed.py
git commit -m "feat: seed fixed fictional organization"
```

### Task 3: Active Accounts, Rich Profiles, and Immediate Token Invalidation

**Files:**
- Modify: `rag/user_db.py`, `rag/api_security.py`, `rag/api.py`
- Test: `tests/test_org_identity.py`, `tests/test_auth_jwt_persistence.py`, `tests/test_auth_password_change_invalidates.py`

**Interfaces:**
- `UserDB.get_user_by_id()` additionally returns `active` and `security_version`.
- JWT carries `security_version`; `/me` returns `profile` with person code, name, title, primary and secondary organizations.

- [ ] **Step 1: Write failing identity tests**

```python
def test_disabled_account_and_old_security_version_are_rejected(client, seeded_db):
    token = login(client, "demo010", "123456")
    disable_person(seeded_db, "P-010")
    assert client.get("/me", headers=bearer(token)).status_code == 401

def test_me_returns_fixed_person_profile(client, seeded_db):
    response = client.get("/me", headers=login_headers(client, "demo001", "123456"))
    assert response.json()["profile"]["person_code"] == "P-001"
    assert "display_name" in response.json()["profile"]
```

- [ ] **Step 2: Run tests and observe current tokens remain valid**

Run: `python -m pytest tests/test_org_identity.py tests/test_auth_jwt_persistence.py tests/test_auth_password_change_invalidates.py -q`  
Expected: new assertions FAIL while existing password-change tests remain PASS.

- [ ] **Step 3: Add account state and version checks**

Issue tokens with the current security version. In both `resolve_principal()` and legacy `_get_current_user()`, require an active user and exact version equality; retain `password_changed_at` validation. Enrich `/me` from `OrgRepository.get_profile_for_user(user_id)` without using name matching.

- [ ] **Step 4: Run auth tests**

Run: `python -m pytest tests/test_org_identity.py tests/test_auth_jwt.py tests/test_auth_jwt_persistence.py tests/test_auth_password_change_invalidates.py tests/test_register_login.py -q`  
Expected: PASS; ordinary self-registration still supports File mode but receives no organization profile or KB access.

- [ ] **Step 5: Commit**

```powershell
git add rag/user_db.py rag/api_security.py rag/api.py tests/test_org_identity.py tests/test_auth_jwt_persistence.py tests/test_auth_password_change_invalidates.py
git commit -m "feat: bind authentication to organization identities"
```

### Task 4: Pure Organization Policy Engine

**Files:**
- Create: `rag/org_auth/policy.py`, `rag/org_auth/service.py`
- Test: `tests/test_org_policy.py`

**Interfaces:**
- Produces `resolve_role(context: AuthorizationContext) -> AuthorizationDecision`.
- Produces `OrgAuthorizationService.authorize(user_id, kb_id, required: OrgRole) -> AuthorizationDecision` and `allowed_kb_ids(user_id, required=OrgRole.READ) -> frozenset[str]`.

- [ ] **Step 1: Write the failing permission matrix**

```python
@pytest.mark.parametrize(("case", "role"), [
    ("employee-own-org", OrgRole.READ),
    ("employee-peer-org", OrgRole.NONE),
    ("manager-descendant", OrgRole.MANAGE),
    ("secondary-member", OrgRole.READ),
    ("secondary-member-manager", OrgRole.READ),
    ("sensitive-org-without-qualification", OrgRole.NONE),
    ("sensitive-qualification-without-org", OrgRole.NONE),
    ("sensitive-qualified-member", OrgRole.READ),
    ("explicit-deny-over-allow", OrgRole.NONE),
    ("company-admin", OrgRole.ADMINISTER),
    ("unassigned-non-admin", OrgRole.NONE),
])
def test_role_matrix(case, role, contexts):
    assert resolve_role(contexts[case]).role is role
```

- [ ] **Step 2: Run and verify missing policy failures**

Run: `python -m pytest tests/test_org_policy.py -q`  
Expected: FAIL because policy types/functions do not exist.

- [ ] **Step 3: Implement deterministic precedence**

Evaluate in this order: inactive user/unit/KB → company admin → unassigned → matching deny → sensitivity conjunction → matching special grant → manager ancestor scope → membership → company public → none. `authorize()` compares enum rank and emits stable reason codes such as `account_inactive`, `explicit_deny`, `sensitivity_required`, `outside_org_scope`, and `allowed_manager_scope`.

- [ ] **Step 4: Run policy tests**

Run: `python -m pytest tests/test_org_policy.py -q`  
Expected: PASS for every matrix row and organization-cycle rejection.

- [ ] **Step 5: Commit**

```powershell
git add rag/org_auth/policy.py rag/org_auth/service.py tests/test_org_policy.py
git commit -m "feat: evaluate organization knowledge base roles"
```

### Task 5: Legacy KB Migration and Organization-Owned CRUD

**Files:**
- Create: `rag/org_auth/migration.py`
- Modify: `rag/user_db.py`, `rag/api_security.py`, `rag/api.py`
- Test: `tests/test_org_kb_migration.py`, `tests/test_org_kb_api.py`, `tests/test_kb_permissions.py`

**Interfaces:**
- Produces `migrate_legacy_knowledge_bases(db) -> MigrationReport`.
- Replaces KB request field `scope` with `org_unit_code`; responses expose `kb_type`, `org_unit`, and `effective_role`.

- [ ] **Step 1: Write failing migration and CRUD tests**

```python
def test_legacy_kb_becomes_unassigned_and_admin_only(db, users):
    db.create_kb_metadata("legacy", "旧知识库", users.employee_id, "public")
    migrate_legacy_knowledge_bases(db)
    assert OrgRepository(db).get_kb_policy("legacy").kb_type is KnowledgeBaseType.UNASSIGNED
    assert service_for(db).authorize(users.employee_id, "legacy", OrgRole.READ).allowed is False
    assert service_for(db).authorize(users.admin_id, "legacy", OrgRole.READ).allowed is True

def test_employee_cannot_create_and_manager_can_create_only_in_scope(client, seeded_db):
    assert client.post("/knowledge-bases", json={"name": "A", "org_unit_code": "TEAM-01"}, headers=employee).status_code == 404
    assert client.post("/knowledge-bases", json={"name": "A", "org_unit_code": managed_team}, headers=manager).status_code == 200
    assert client.post("/knowledge-bases", json={"name": "B", "org_unit_code": peer_team}, headers=manager).status_code == 404
```

Also assert `/scope` and `/share` routes no longer mutate KB access, while document permission routes for File mode remain unchanged.

- [ ] **Step 2: Run migration/API tests**

Run: `python -m pytest tests/test_org_kb_migration.py tests/test_org_kb_api.py tests/test_kb_permissions.py -q`  
Expected: FAIL on missing migration and current owner/scope behavior.

- [ ] **Step 3: Implement safe migration and CRUD**

Insert one `unassigned` policy per pre-existing `kb_metadata` row and store legacy owner/scope/share JSON in migration audit data. Change list/create/delete/rename/overview/TOC/document mutation routes to require `OrgAuthorizationService`; remove KB scope mutations and personal share behavior with 404 responses. Delete cleanup must use `UserDB.locked_transaction()`, not a new SQLite connection.

- [ ] **Step 4: Run focused KB and File permission regressions**

Run: `python -m pytest tests/test_org_kb_migration.py tests/test_org_kb_api.py tests/test_kb_permissions.py tests/test_permissions.py tests/test_permissions_api.py -q`  
Expected: PASS; legacy KB is admin-only and File permission tests remain green.

- [ ] **Step 5: Commit**

```powershell
git add rag/org_auth/migration.py rag/user_db.py rag/api_security.py rag/api.py tests/test_org_kb_migration.py tests/test_org_kb_api.py tests/test_kb_permissions.py
git commit -m "feat: migrate knowledge bases to organization ownership"
```

### Task 6: Enforce Every KB Resource Endpoint

**Files:**
- Modify: `rag/api.py`, `rag/operations_api.py`, `rag/materials/api.py`, `rag/spreadsheets/api.py`, `rag/citations.py`, `rag/reliability/api.py`
- Test: `tests/test_org_kb_security_boundaries.py`, `tests/test_operations_api.py`, `tests/test_material_api.py`, `tests/test_spreadsheet_api.py`, `tests/test_reliability_api.py`

**Interfaces:**
- All KB resource handlers consume `OrgAuthorizationService.authorize()`; File handlers keep `require_doc_view/edit()`.

- [ ] **Step 1: Add a route-by-route denial matrix**

Parameterize unauthorized calls covering KB detail, query, upload, delete, rename, overview, TOC generation, document TOC/summary, version list/download, materials locate/archive, spreadsheet read/export, citations, and persisted comparison reopen. Assert 404 and absence of KB/document names in response bodies.

```python
@pytest.mark.parametrize("method,path", PROTECTED_KB_CASES)
def test_kb_resources_do_not_reveal_existence(client, outsider, method, path):
    response = client.request(method, path, headers=outsider.headers)
    assert response.status_code == 404
    assert "机密薪酬" not in response.text
```

- [ ] **Step 2: Run the boundary suite and capture current bypasses**

Run: `python -m pytest tests/test_org_kb_security_boundaries.py -q`  
Expected: FAIL for handlers still using owner/share or document-only checks.

- [ ] **Step 3: Route all handlers through shared guards**

Add `require_org_kb_read/manage/administer()` helpers in `rag/api_security.py`. Resolve document-to-KB relationships before authorization. Normalize denied and missing resources to the same 404 response; retain internal reason codes only for audit.

- [ ] **Step 4: Run affected API suites**

Run: `python -m pytest tests/test_org_kb_security_boundaries.py tests/test_operations_api.py tests/test_material_api.py tests/test_spreadsheet_api.py tests/test_reliability_api.py -q`  
Expected: PASS with no endpoint bypass.

- [ ] **Step 5: Commit**

```powershell
git add rag/api.py rag/api_security.py rag/operations_api.py rag/materials/api.py rag/spreadsheets/api.py rag/citations.py rag/reliability/api.py tests/test_org_kb_security_boundaries.py
git commit -m "fix: enforce organization access across kb resources"
```

### Task 7: Pre-Retrieval Filtering for Search, Agent, and Answers

**Files:**
- Modify: `rag/versioned_retrieval.py`, `rag/pipeline.py`, `rag/agent.py`
- Test: `tests/test_org_retrieval_scope.py`, `tests/test_agent_permission_filter.py`, `tests/test_pipeline_cache_security.py`, `tests/test_versioned_retrieval.py`

**Interfaces:**
- Produces `OrgAuthorizationService.allowed_document_ids(user_id, kb_id) -> frozenset[int]`.
- `retrieve_current_then_history(..., allowed_document_ids: frozenset[int])` never queries without that set for KB mode.

- [ ] **Step 1: Write failing pre-filter and fail-closed tests**

```python
def test_unauthorized_ids_are_filtered_before_dense_and_bm25(fake_retriever, org_service):
    retrieve_for_org(fake_retriever, org_service, user_id=12, kb_id="finance")
    assert fake_retriever.calls == []

def test_acl_failure_never_falls_back_to_unfiltered_retrieval(fake_retriever, broken_service):
    assert retrieve_for_org(fake_retriever, broken_service, 12, "finance") == []
    assert fake_retriever.calls == []
```

Also verify agent tools, reranking, cached sources, answer citations, current versions, and historical versions receive only authorized document IDs.

- [ ] **Step 2: Run retrieval security tests**

Run: `python -m pytest tests/test_org_retrieval_scope.py tests/test_agent_permission_filter.py tests/test_pipeline_cache_security.py tests/test_versioned_retrieval.py -q`  
Expected: FAIL where personal document ACL is still used for KB retrieval.

- [ ] **Step 3: Implement one authorization scope before candidate generation**

For KB mode, replace `get_accessible_doc_names()` with the org service's document-ID set and pass it to dense and BM25 retrieval before scoring. Keep post-rerank filtering as defense in depth. Empty scope and exceptions return no context and a permission-limited answer; they never call unfiltered retrieval.

- [ ] **Step 4: Run retrieval and pipeline suites**

Run: `python -m pytest tests/test_org_retrieval_scope.py tests/test_agent_permission_filter.py tests/test_pipeline_cache_security.py tests/test_versioned_retrieval.py tests/test_pipeline.py -q`  
Expected: PASS and spies prove filtering precedes retrieval/reranking.

- [ ] **Step 5: Commit**

```powershell
git add rag/versioned_retrieval.py rag/pipeline.py rag/agent.py tests/test_org_retrieval_scope.py tests/test_agent_permission_filter.py tests/test_pipeline_cache_security.py tests/test_versioned_retrieval.py
git commit -m "fix: filter organization knowledge before retrieval"
```

### Task 8: Administration APIs and Redacted Audit Trail

**Files:**
- Create: `rag/org_auth/audit.py`, `rag/org_auth/api.py`
- Modify: `rag/api.py`
- Test: `tests/test_org_admin_api.py`, `tests/test_org_audit.py`

**Interfaces:**
- Adds `/organization/tree`, `/organization/units/{code}`, `/organization/people/{person_code}`, `/organization/preview/{user_id}`, `/organization/grants`, and `/organization/audit`.
- Adds public read-only `/login/accounts?q=` returning account, name, title, and primary department only.

- [ ] **Step 1: Write failing admin-boundary and redaction tests**

```python
def test_preview_is_read_only_and_returns_effective_libraries(client, admin, employee_id):
    response = client.get(f"/organization/preview/{employee_id}", headers=admin)
    assert {kb["effective_role"] for kb in response.json()["knowledge_bases"]} <= {"read", "manage", "administer"}
    assert "token" not in response.json()

def test_audit_redacts_secrets(audit_service):
    event = audit_service.record(actor_id=1, action="login_failed", details={"password": "123456", "query": "secret text"})
    assert "123456" not in json.dumps(event)
    assert "secret text" not in json.dumps(event)
```

- [ ] **Step 2: Run admin/audit tests**

Run: `python -m pytest tests/test_org_admin_api.py tests/test_org_audit.py -q`  
Expected: FAIL because routes and audit service are absent.

- [ ] **Step 3: Implement scoped administration and audit**

Only company admins mutate org units, memberships, manager scopes, account state, sensitivities, KB assignment, and grants. Managers may read their descendant tree. Preview computes roles without issuing a token. Record login, identity, KB, content, and denied-access events using internal IDs and allowlisted detail keys.

- [ ] **Step 4: Run API and audit suites**

Run: `python -m pytest tests/test_org_admin_api.py tests/test_org_audit.py tests/test_api_security.py -q`  
Expected: PASS; unauthorized admin mutations return 404.

- [ ] **Step 5: Commit**

```powershell
git add rag/org_auth/audit.py rag/org_auth/api.py rag/api.py tests/test_org_admin_api.py tests/test_org_audit.py
git commit -m "feat: add organization administration and audit"
```

### Task 9: Login Account Picker and Rich Auth Store

**Files:**
- Create: `frontend/src/api/organization.ts`, `frontend/src/components/AccountPicker.vue`
- Create test: `frontend/src/components/__tests__/AccountPicker.test.ts`
- Modify: `frontend/src/views/LoginView.vue`, `frontend/src/stores/auth.ts`
- Modify test: `frontend/src/stores/__tests__/auth.test.ts`
- Create test: `frontend/src/views/__tests__/LoginView.test.ts`

**Interfaces:**
- `searchLoginAccounts(query: string): Promise<LoginAccount[]>`.
- Auth `user.profile` exposes person code, display name, title, primary and secondary organizations.

- [ ] **Step 1: Write failing picker and profile tests**

```ts
it('selects a fixed employee and fills only the username', async () => {
  const wrapper = mount(AccountPicker, { props: { modelValue: '' } })
  await wrapper.get('[data-test="account-search"]').setValue('产品')
  await flushPromises()
  await wrapper.get('[data-test="account-demo001"]').trigger('click')
  expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['demo001'])
  expect(wrapper.text()).not.toContain('测试账号')
  expect(wrapper.text()).not.toContain('演示环境')
})
```

- [ ] **Step 2: Run frontend red tests**

Run: `npm test -- src/components/__tests__/AccountPicker.test.ts src/views/__tests__/LoginView.test.ts src/stores/__tests__/auth.test.ts` from `frontend`  
Expected: FAIL because account picker and rich profile types are missing.

- [ ] **Step 3: Implement the picker without exposing the password**

Debounce search, display account/name/title/department, emit the selected username, and leave the password input untouched. Preserve existing login and registration flows. Render the signed-in profile in the existing layout without environment labels.

- [ ] **Step 4: Run focused frontend tests**

Run: `npm test -- src/components/__tests__/AccountPicker.test.ts src/views/__tests__/LoginView.test.ts src/stores/__tests__/auth.test.ts` from `frontend`  
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/api/organization.ts frontend/src/components/AccountPicker.vue frontend/src/components/__tests__/AccountPicker.test.ts frontend/src/views/LoginView.vue frontend/src/views/__tests__/LoginView.test.ts frontend/src/stores/auth.ts frontend/src/stores/__tests__/auth.test.ts
git commit -m "feat: add employee account selection"
```

### Task 10: Organization-Scoped Knowledge Base UI

**Files:**
- Modify: `frontend/src/views/KBModeView.vue`, `frontend/src/views/KnowledgeDetailView.vue`
- Create test: `frontend/src/views/__tests__/KBModeView.test.ts`
- Modify test: `frontend/src/views/__tests__/KnowledgeDetailView.test.ts`

**Interfaces:**
- Consumes KB response fields `kb_type`, `org_unit`, `effective_role`, and `can_create_in`.

- [ ] **Step 1: Write failing capability-rendering tests**

```ts
it('groups authorized libraries and never renders personal sharing controls', async () => {
  mockKBs([publicKB, departmentKB, sensitiveKB])
  const wrapper = mount(KBModeView)
  await flushPromises()
  expect(wrapper.text()).toContain('公司公共库')
  expect(wrapper.text()).toContain('组织知识库')
  expect(wrapper.text()).toContain('敏感知识库')
  expect(wrapper.text()).not.toContain('私有')
  expect(wrapper.text()).not.toContain('共享给')
  expect(wrapper.find('[data-test="share-kb"]').exists()).toBe(false)
})
```

Add cases proving employees lack create/delete/upload controls, managers see content management in scope, and administrators see assignment controls.

- [ ] **Step 2: Run KB view tests**

Run: `npm test -- src/views/__tests__/KBModeView.test.ts src/views/__tests__/KnowledgeDetailView.test.ts` from `frontend`  
Expected: FAIL because the current UI is owner/scope/share based.

- [ ] **Step 3: Replace personal controls with server capabilities**

Group cards by KB type, show organization and effective role, conditionally render creation/content actions, remove `ShareDialog` and scope fields from KB flows, and submit `org_unit_code` when a manager creates a KB.

- [ ] **Step 4: Run KB frontend tests**

Run: `npm test -- src/views/__tests__/KBModeView.test.ts src/views/__tests__/KnowledgeDetailView.test.ts` from `frontend`  
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/views/KBModeView.vue frontend/src/views/KnowledgeDetailView.vue frontend/src/views/__tests__/KBModeView.test.ts frontend/src/views/__tests__/KnowledgeDetailView.test.ts
git commit -m "feat: render organization knowledge libraries"
```

### Task 11: Organization Administration UI

**Files:**
- Create: `frontend/src/components/OrganizationTree.vue`, `frontend/src/views/OrganizationAdminView.vue`
- Create tests: `frontend/src/components/__tests__/OrganizationTree.test.ts`, `frontend/src/views/__tests__/OrganizationAdminView.test.ts`
- Modify: `frontend/src/router/index.ts`, `frontend/src/views/MainLayout.vue`
- Modify test: `frontend/src/views/__tests__/MainLayout.test.ts`

**Interfaces:**
- Consumes Task 8 organization tree, unit detail, mutation, preview, grant, and audit APIs.

- [ ] **Step 1: Write failing role-aware navigation and admin tests**

```ts
it('shows organization management only to company admins', async () => {
  const admin = mountLayout({ is_admin: true })
  const employee = mountLayout({ is_admin: false })
  expect(admin.text()).toContain('组织权限')
  expect(employee.text()).not.toContain('组织权限')
})

it('preview lists effective libraries but exposes no impersonation action', async () => {
  const wrapper = mount(OrganizationAdminView)
  await wrapper.get('[data-test="preview-person"]').trigger('click')
  await flushPromises()
  expect(wrapper.text()).toContain('可查看')
  expect(wrapper.find('[data-test="login-as-person"]').exists()).toBe(false)
})
```

- [ ] **Step 2: Run administration UI tests**

Run: `npm test -- src/components/__tests__/OrganizationTree.test.ts src/views/__tests__/OrganizationAdminView.test.ts src/views/__tests__/MainLayout.test.ts` from `frontend`  
Expected: FAIL because components and route do not exist.

- [ ] **Step 3: Implement the four-level tree and admin forms**

Render unit people, managers, secondary memberships, sensitivities, KBs, grants, account status, and audit. Gate navigation and mutations by server-provided admin status. The preview panel is read-only and has no token or write controls.

- [ ] **Step 4: Run administration UI tests**

Run: `npm test -- src/components/__tests__/OrganizationTree.test.ts src/views/__tests__/OrganizationAdminView.test.ts src/views/__tests__/MainLayout.test.ts` from `frontend`  
Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/components/OrganizationTree.vue frontend/src/components/__tests__/OrganizationTree.test.ts frontend/src/views/OrganizationAdminView.vue frontend/src/views/__tests__/OrganizationAdminView.test.ts frontend/src/router/index.ts frontend/src/views/MainLayout.vue frontend/src/views/__tests__/MainLayout.test.ts
git commit -m "feat: add organization authorization console"
```

### Task 12: Full Regression, Seed Installation, and Operational Documentation

**Files:**
- Modify: `README.md`
- Create: `docs/organization-authorization-operations.md`
- Modify: no production files are planned in this task; every integration defect found in Step 2 must first be reproduced by an added assertion in `tests/test_org_acceptance.py`, then fixed in its owning file from Tasks 1–11.

**Interfaces:**
- Documents exact seed, rerun, migration, verification, rollback, account-disable, and legacy-KB assignment commands.

- [ ] **Step 1: Add final acceptance tests before final code adjustments**

Create `tests/test_org_acceptance.py` that seeds twice, logs in representative employee/manager/admin/secondary/sensitive accounts, validates all 65 KB visibility sets, validates immediate token revocation, and verifies a File-page private/shared/public matrix in the same database.

```python
def test_org_and_file_permission_domains_remain_independent(seeded_system):
    assert seeded_system.kb_role("demo040", "KB-PEER") == "none"
    assert seeded_system.can_view_shared_file("demo040", "personal-shared.txt") is True
    assert seeded_system.can_view_private_file("demo040", "someone-else-private.txt") is False
```

- [ ] **Step 2: Run the new acceptance test and fix only uncovered integration defects**

Run: `python -m pytest tests/test_org_acceptance.py -q`  
Expected before final wiring: FAIL on any missing startup/router/fixture integration; after minimal corrections: PASS.

- [ ] **Step 3: Run all verification commands**

Run from repository root:

```powershell
python -m pytest -q
python -m ruff check rag tests
Set-Location frontend
npm test
npm run build
Set-Location ..
git diff --check
git status --short
```

Expected: complete backend suite PASS except already-documented platform skips; Ruff PASS; all frontend tests PASS; production build succeeds; no whitespace errors; only intended documentation/test changes remain before the final commit.

- [ ] **Step 4: Run the real idempotent initializer and restart smoke test**

Create and verify an authoritative backup first, then run the initializer:

```powershell
$backup = (python scripts/backup.py create --trigger manual | ConvertFrom-Json)
python scripts/backup.py verify $backup.backup_id
python -m rag.org_auth.seed --db-path data/users.db
python -m rag.org_auth.seed --db-path data/users.db
```

Expected: first run creates or resumes the fixed organization and content; second run reports zero duplicate creations. Start the app with `python start.py`, open `http://localhost:8080`, log in as one employee, one manager, and one administrator, and verify their KB lists differ while File mode remains available.

- [ ] **Step 5: Document operations and commit**

Document the actual backup artifact, seed report, migration behavior, rollback steps, and representative account checks. Then commit:

```powershell
git add README.md docs/organization-authorization-operations.md tests/test_org_acceptance.py
git commit -m "docs: finalize organization authorization rollout"
```

Do not claim completion until the verification output from Step 3 and smoke evidence from Step 4 have been reviewed.
