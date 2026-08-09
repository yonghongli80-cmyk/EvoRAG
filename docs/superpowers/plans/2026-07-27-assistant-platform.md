# Assistant Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

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

- New backend: `rag/assistants/{models,profiles,scope,runtime,workflow_dsl,prompts,onboarding,policy,api}.py`, `rag/assistants/tools/__init__.py`, `rag/assistants/tools/registry.py`.
- New tests: `tests/test_assistant_*.py`, `tests/test_onboarding_assistant.py`, `tests/test_policy_assistant.py`.
- Modify: `rag/user_db.py`, `rag/pipeline.py`, `rag/api.py`, reliability/citation/history/audit services.
- New frontend: `frontend/src/api/assistants.ts`, `frontend/src/components/AssistantSelector.vue`, `AssistantCard.vue`, `frontend/src/views/AssistantAdminView.vue`, tests.
- Modify: home/chat/layout routes and stores.

---

### Task 1: Tool Registry, Workflow DSL, and Prompt Templates

**Files:** Create `rag/assistants/models.py`, `rag/assistants/tools/__init__.py`, `rag/assistants/tools/registry.py`, `rag/assistants/workflow_dsl.py`, `rag/assistants/prompts.py`, `tests/test_assistant_tools.py`, `tests/test_workflow_dsl.py`; modify `rag/user_db.py`.

**Interfaces:** `ToolSpec`; `ToolRegistry.register`; `WorkflowDefinition`; `validate_workflow`; `publish_prompt_template`.

- [ ] **Step 1: Write failing immutable-version test**

```python
def test_workflow_rejects_unknown_tool_and_cycle(tool_registry):
    definition = workflow(
        steps=[
            step("a", tool="missing", on_success="b"),
            step("b", tool="retrieve", on_success="a"),
        ]
    )
    with pytest.raises(WorkflowValidationError, match="unknown tool|cycle"):
        validate_workflow(definition, tool_registry)


def test_workflow_rejects_unreachable_step_and_forward_output_reference(tool_registry):
    definition = workflow(
        entrypoint="a",
        steps=[
            step("a", tool="retrieve", input_bindings={"query": "steps.b.result"}, on_success="stop"),
            step("b", tool="retrieve", on_success="stop"),
        ],
    )
    with pytest.raises(WorkflowValidationError, match="unreachable|not an ancestor"):
        validate_workflow(definition, tool_registry)


def test_workflow_rejects_more_than_twenty_steps(tool_registry):
    with pytest.raises(WorkflowValidationError, match="20"):
        validate_workflow(workflow_with_linear_steps(21), tool_registry)


def test_prompt_publication_creates_immutable_versions(prompt_store):
    first = prompt_store.publish("policy-answer", "v1")
    second = prompt_store.publish("policy-answer", "v2")
    assert first.version == 1 and second.version == 2
    assert prompt_store.get(first.id).template == "v1"
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_tools.py tests\test_workflow_dsl.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement registry and declarative versioned DSL**

The DSL is a bounded DAG of at most 20 steps with exactly one declared entrypoint. A step contains only `id`, `tool_key`, `input_bindings`, `output_key`, `condition`, `on_success`, and `on_error`. Bindings may reference `request.<declared-field>`, `context.<allowlisted-field>`, or `steps.<ancestor-id>.<declared-output-field>`. Conditions allow only `eq | ne | exists | not_exists | in` against a literal JSON value. No Python expressions, imports, arbitrary attribute traversal, loops, dynamic tool names, or runtime templates.

Validation rejects duplicate IDs/output keys, missing or unreachable steps, cycles, missing transitions, non-terminating paths, references to non-ancestors, unknown tools, bindings that do not exactly satisfy the registered tool's Pydantic input model, and output bindings absent from its output model. Canonical workflow JSON plus registry schema hashes are persisted as an immutable workflow-definition version. Prompt templates are immutable version rows with `(template_key, version)`, content hash, bounded variables, and an exact allowlist of variable names; publication never updates an existing row.

```python
class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    required_capabilities: frozenset[str] = frozenset()
    read_only: bool
    timeout_seconds: Annotated[int, Field(ge=1, le=120)]
    max_result_bytes: Annotated[int, Field(ge=1, le=1_000_000)]


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    tool_key: str
    input_bindings: dict[str, str]
    output_key: str
    condition: StepCondition | None = None
    on_success: str | Literal["stop"]
    on_error: str | Literal["stop"]


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")]
    entrypoint: str
    steps: Annotated[tuple[WorkflowStep, ...], Field(min_length=1, max_length=20)]
```

- [ ] **Step 4: Run profile tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_tools.py tests\test_workflow_dsl.py tests\test_user_db.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/models.py rag/assistants/tools rag/assistants/workflow_dsl.py rag/assistants/prompts.py rag/user_db.py tests/test_assistant_tools.py tests/test_workflow_dsl.py
git commit -m "feat: define assistant workflow contracts"
```

---

### Task 2: Versioned Assistant Profiles and Immutable Execution Scope

**Files:** Create `rag/assistants/profiles.py`, `rag/assistants/scope.py`, `tests/test_assistant_profiles.py`, `tests/test_assistant_scope.py`; modify `rag/user_db.py`.

**Interfaces:** `create_assistant_draft`, `validate_assistant_draft`, `publish_assistant_profile`, `get_published_profile`; `build_execution_scope`; `scope.narrow`.

- [ ] **Step 1: Write failing non-expansion test**

```python
def test_profile_publish_resolves_registered_tools_and_workflow(profile_fixture):
    published = profile_fixture.publish(tool_keys=["retrieve"], workflow_key="policy-v1")
    assert published.version == 1
    assert published.workflow_definition_version_id is not None


def test_child_scope_cannot_add_document(parent_scope):
    with pytest.raises(ScopeExpansionError):
        parent_scope.narrow(document_ids=parent_scope.document_ids | {999})
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_profiles.py tests\test_assistant_scope.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement profile validation and frozen scope**

Publishing resolves every tool, prompt-template version, workflow-definition version, output schema, KB scope, and capability. Profiles store immutable resolved version IDs, not mutable names.

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
& .\.venv\Scripts\python.exe -m pytest tests\test_assistant_profiles.py tests\test_assistant_scope.py tests\test_document_security_boundaries.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/profiles.py rag/assistants/scope.py rag/user_db.py tests/test_assistant_profiles.py tests/test_assistant_scope.py
git commit -m "feat: publish bounded assistant profiles"
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
