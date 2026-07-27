# Assistant Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one configuration-driven assistant runtime to RAGv3 and deliver onboarding and policy assistants without creating separate applications.

**Architecture:** Version assistant profiles and registered tools in the existing database, create an immutable execution scope per request, and route profiles to bounded RAG/workflow runners. All responses reuse the existing message, citation, reliability, feedback, history, and audit pipeline.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLite, LlamaIndex Workflow adapter, pytest, Vue 3, Pinia, TypeScript, Vitest

## Global Constraints

- One frontend, backend, login, permission engine, message model, and deployment.
- Assistant profile can only narrow ACL; it cannot grant access.
- Configuration never imports arbitrary Python.
- Execution kinds are exactly `rag_chat | deterministic_workflow | controlled_multi_agent`.
- Controlled multi-agent has at most planner/evidence_worker/verifier roles.
- Every run has immutable user/capability/resource scope, step/time/tool budgets, and profile version.
- Disabled assistant history stays readable but cannot execute.
- Onboarding excludes personal HR records.
- Policy assistant defaults to current valid versions and exposes conflicts.
- Use `superpowers:executing-plans`; no subagents unless requested.

---

## File Structure

- New backend: `rag/assistants/{models,profiles,scope,runtime,onboarding,policy,api}.py`, `rag/assistants/tools/__init__.py`, `rag/assistants/tools/registry.py`.
- New tests: `tests/test_assistant_*.py`, `tests/test_onboarding_assistant.py`, `tests/test_policy_assistant.py`.
- Modify: `rag/user_db.py`, `rag/pipeline.py`, `rag/api.py`, reliability/citation/history/audit services.
- New frontend: `frontend/src/api/assistants.ts`, `frontend/src/components/AssistantSelector.vue`, `AssistantCard.vue`, `frontend/src/views/AssistantAdminView.vue`, tests.
- Modify: home/chat/layout routes and stores.

---

### Task 1: Versioned Assistant Profiles

**Files:** Create `rag/assistants/models.py`, `rag/assistants/profiles.py`, `tests/test_assistant_profiles.py`; modify `rag/user_db.py`.

**Interfaces:** `create_assistant_draft`, `validate_assistant_draft`, `publish_assistant_profile`, `get_published_profile(key)`.

- [ ] **Step 1: Write failing immutable-version test**

```python
def test_publishing_profile_does_not_mutate_previous_version(profile_fixture):
    first = profile_fixture.publish(name="制度助手", tools=["retrieve"])
    second = profile_fixture.publish(name="制度助手", tools=["retrieve", "compare"])
    assert first.version == 1
    assert second.version == 2
    assert profile_fixture.get_version(1).tools == ("retrieve",)
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_profiles.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add profile/version tables and schema validation**

Tool keys must exist in registry; execution kind is enum; output schema is valid JSON Schema; knowledge scopes reference existing KBs; prompt length and profile key are bounded.

```python
class AssistantProfileDraft(BaseModel):
    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,47}$")]
    execution_kind: Literal["rag_chat", "deterministic_workflow", "controlled_multi_agent"]
    tool_keys: tuple[str, ...]
```

- [ ] **Step 4: Run profile tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_profiles.py tests\test_user_db.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/models.py rag/assistants/profiles.py rag/user_db.py tests/test_assistant_profiles.py
git commit -m "feat: add versioned assistant profiles"
```

---

### Task 2: Tool Registry and Immutable Execution Scope

**Files:** Create `rag/assistants/tools/__init__.py`, `rag/assistants/tools/registry.py`, `rag/assistants/scope.py`, `tests/test_assistant_tools.py`.

**Interfaces:** `ToolSpec`; `ToolRegistry.register(spec, handler)`; `build_execution_scope(db, user, profile) -> ExecutionScope`; `scope.narrow(...)`.

- [ ] **Step 1: Write failing non-expansion test**

```python
def test_child_scope_cannot_add_document(parent_scope):
    with pytest.raises(ScopeExpansionError):
        parent_scope.narrow(document_ids=parent_scope.document_ids | {999})
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_tools.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement registry declarations and frozen scope**

ToolSpec includes Pydantic input/output, capabilities, assistants, read-only flag, timeout, max result bytes, model visibility, and audit action. Register only explicit callables; no module path strings.

```python
@dataclass(frozen=True)
class ExecutionScope:
    user_id: int
    capability_keys: frozenset[str]
    kb_ids: frozenset[str]
    document_ids: frozenset[int]
```

- [ ] **Step 4: Run tool/scope tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_tools.py tests\test_document_security_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/tools rag/assistants/scope.py tests/test_assistant_tools.py
git commit -m "feat: constrain assistant tools"
```

---

### Task 3: Bounded Runtime and Shared Message Pipeline

**Files:** Create `rag/assistants/runtime.py`, `tests/test_assistant_runtime.py`; modify `rag/pipeline.py`.

**Interfaces:** `AssistantRuntime.run(request, scope) -> AssistantResult`; `AssistantRuntime.stream(request, scope) -> AsyncIterator[AssistantEvent]`.

- [ ] **Step 1: Write failing budget/shared-message tests**

```python
async def test_runtime_stops_at_tool_budget(runtime_fixture):
    runtime_fixture.profile(tool_budget=2)
    result = await runtime_fixture.run(tool_calls=3)
    assert result.status == "budget_exceeded"
    assert runtime_fixture.executed_tools == 2


async def test_runtime_persists_profile_version_and_citations(runtime_fixture):
    result = await runtime_fixture.run(tool_calls=1)
    message = runtime_fixture.db.get_message(result.assistant_message_id)
    assert message["assistant_profile_version_id"] == runtime_fixture.profile_version_id
    assert runtime_fixture.db.get_answer_citations(message["id"])
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_runtime.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement three runners and one persistence path**

`rag_chat` delegates to current pipeline; deterministic workflow executes declared steps; controlled multi-agent accepts only three role keys and bounded queues. Every result passes answerability, citations, message persistence, feedback linkage, activity, and audit exactly once.

```python
_RUNNERS = {
    "rag_chat": run_rag_chat,
    "deterministic_workflow": run_deterministic_workflow,
    "controlled_multi_agent": run_controlled_multi_agent,
}
```

- [ ] **Step 4: Run runtime/pipeline tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_runtime.py tests\test_pipeline.py tests\test_conversations.py tests\test_answer_citations.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/runtime.py rag/pipeline.py tests/test_assistant_runtime.py
git commit -m "feat: run bounded assistants"
```

---

### Task 4: Onboarding Assistant

**Files:** Create `rag/assistants/onboarding.py`, `tests/test_onboarding_assistant.py`; modify `rag/user_db.py`.

**Interfaces:** `get_onboarding_checklist(user_id)`, `set_checklist_item(user_id, item_id, completed)`, `run_onboarding_question`.

- [ ] **Step 1: Write failing HR-exclusion test**

```python
async def test_onboarding_scope_excludes_personal_hr_kb(onboarding_fixture):
    result = await onboarding_fixture.ask("我的工资是多少")
    assert result.decision.kind == "insufficient_evidence"
    assert onboarding_fixture.personal_hr_kb_id not in onboarding_fixture.tool_scopes
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_onboarding_assistant.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add checklist state and onboarding workflow**

Checklist stages are day one/week one/month one. Completion is explicitly `personal_progress`, never HR approval. Links/actions are references only.

```python
class ChecklistItem(BaseModel):
    stage: Literal["day_one", "week_one", "month_one"]
    completion_kind: Literal["personal_progress"] = "personal_progress"
```

- [ ] **Step 4: Run onboarding tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_onboarding_assistant.py tests\test_assistant_runtime.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/onboarding.py rag/user_db.py tests/test_onboarding_assistant.py
git commit -m "feat: add onboarding assistant"
```

---

### Task 5: Policy Assistant

**Files:** Create `rag/assistants/policy.py`, `tests/test_policy_assistant.py`.

**Interfaces:** `run_policy_question`; `compare_policies`; `explain_policy_scope`.

- [ ] **Step 1: Write failing conflict test**

```python
async def test_policy_conflict_blocks_unqualified_conclusion(policy_fixture):
    policy_fixture.seed_current_claims(values=["5天", "7天"])
    result = await policy_fixture.ask("年假是多少")
    assert result.decision.kind == "clarification_required"
    assert result.conflicts
    assert "年假是" not in result.answer
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_policy_assistant.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement current-first policy workflow**

Apply effective date/scope filters, use historical only on explicit history request or fallback, call reliability conflict/comparison services, and never infer department/region when user attributes are absent.

```python
if request.requires_user_scope and not request.selected_scope:
    return PolicyResult.clarification("请选择适用部门或地区")
```

- [ ] **Step 4: Run policy/reliability tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_policy_assistant.py tests\test_answerability.py tests\test_conflicts.py tests\test_document_comparison.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/policy.py tests/test_policy_assistant.py
git commit -m "feat: add policy assistant"
```

---

### Task 6: Assistant API, Unified UI, and Verification

**Files:** Create `rag/assistants/api.py`, API tests, frontend assistant API/selector/card/admin view/tests; modify `rag/api.py`, chat/home/layout/router/stores.

**Interfaces:** Produces `/assistants`, assistant conversation/query/stream, admin draft/publish, and onboarding checklist routes.

- [ ] **Step 1: Write failing one-platform tests**

```python
def test_assistant_query_uses_existing_conversation_table(isolated_api, user_headers):
    response = isolated_api.post("/assistants/policy/conversations", headers=user_headers)
    conversation_id = response.json()["conversation_id"]
    assert isolated_api.app.state.user_db.get_conversation(conversation_id, user_id(user_headers))["mode"] == "assistant"
```

```typescript
it('switches assistant inside the same chat route', async () => {
  const wrapper = mountChat()
  await wrapper.get('[data-assistant="policy"]').trigger('click')
  expect(mockRouterPush).not.toHaveBeenCalledWith(expect.stringContaining('http'))
  expect(wrapper.findComponent(AssistantSelector).props('selected')).toBe('policy')
})
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_api.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/components/AssistantSelector.test.ts
Set-Location ..
```

Expected: missing routes/components.

- [ ] **Step 3: Implement one router and in-chat selector**

Use existing auth, conversations, SSE done events, citations, feedback, favorites, history, and audit. Disabled profile returns 409 for new runs; old messages remain readable.

```typescript
const selectAssistant = (key: string) => {
  chatStore.startConversation({ mode: 'assistant', assistant_key: key })
}
```

- [ ] **Step 4: Run full verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/assistants tests/test_assistant_profiles.py tests/test_assistant_tools.py tests/test_assistant_runtime.py tests/test_onboarding_assistant.py tests/test_policy_assistant.py tests/test_assistant_api.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
```

Expected: PASS.

- [ ] **Step 5: Document and commit**

```powershell
git add rag/assistants rag/api.py tests frontend/src docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify assistant platform"
```
