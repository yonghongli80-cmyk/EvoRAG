# Advanced Assistants Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add sales, project, and data-analysis profiles to the same assistant platform with cited drafts, confirmation gates, and deterministic spreadsheet tools.

**Architecture:** Implement focused domain workflows and tool adapters on the existing assistant runtime. Sales/project outputs remain drafts until explicit confirmation; data analysis delegates to the spreadsheet query gateway and never exposes arbitrary code execution.

**Tech Stack:** Python 3.12, FastAPI, existing assistant runtime, spreadsheet gateway, LlamaIndex comparison Workflow, pytest, Vue 3, TypeScript, Vitest

## Global Constraints

- Do not create new apps, logins, message tables, knowledge stores, or deployments.
- Sales promises for price/function/delivery require current citations and remain internal drafts.
- Customer/project ACL is enforced before every tool call.
- Extracted project actions are suggestions until user confirmation.
- Archived projects are read-only.
- Data assistant accepts `StructuredQueryPlan`, never SQL/Python/Pandas.
- Multi-dataset mapping requires confirmed type/unit/date/key compatibility.
- Charts accept enum styles and validated series, never file paths/code.
- Payroll/attendance flag defaults false; system admin does not imply HR access.
- Ordinary/non-sensitive data features can ship while payroll approval is pending.
- Use `superpowers:executing-plans`; no subagents unless requested.

---

## File Structure

- New backend: `rag/assistants/{sales,projects,data_analysis}.py`, `rag/assistants/tools/{sales,projects,structured,charts}.py`.
- New tests: `tests/test_sales_assistant.py`, `tests/test_project_assistant.py`, `tests/test_data_analysis_assistant.py`, `tests/test_assistant_confirmation.py`.
- Modify: assistant registry/profiles/runtime, `rag/user_db.py`, governance audit.
- New frontend: assistant start forms and result components/tests.
- Modify: home/chat assistant UI.

---

### Task 1: Sales Assistant and Promise Verifier

**Files:** Create sales workflow/tool modules and `tests/test_sales_assistant.py`; modify assistant profile seed.

**Interfaces:** `build_sales_draft(request, scope) -> SalesDraft`; `verify_sales_claims(draft) -> ClaimVerification`.

- [ ] **Step 1: Write failing unsupported-promise test**

```python
async def test_sales_draft_marks_uncited_delivery_promise(sales_fixture):
    result = await sales_fixture.draft("承诺三天交付")
    claim = next(item for item in result.claims if "三天" in item.text)
    assert claim.status == "requires_human_confirmation"
    assert claim.citation_ids == ()
    assert result.label == "内部草稿"
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_sales_assistant.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement retrieve/draft/verify workflow**

Classify price, discount, function, delivery, and contract claims. Supported claims require current citation; unsupported claims remain visibly marked. No tool sends or writes externally.

```python
for claim in draft.claims:
    claim.status = "supported" if claim.current_citation_ids else "requires_human_confirmation"
```

- [ ] **Step 4: Run sales and ACL tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_sales_assistant.py tests\test_document_security_boundaries.py -v
```

Expected: PASS including customer-specific KB isolation.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/sales.py rag/assistants/tools/sales.py tests/test_sales_assistant.py
git commit -m "feat: add cited sales drafts"
```

---

### Task 2: Project Spaces, Reports, and Confirmation

**Files:** Create project workflow/tool modules, `tests/test_project_assistant.py`, `tests/test_assistant_confirmation.py`; modify `rag/user_db.py`.

**Interfaces:** `summarize_project_changes`; `extract_action_candidates`; `confirm_action_candidate`; `archive_project`.

- [ ] **Step 1: Write failing no-auto-assignment test**

```python
async def test_extracted_action_is_not_task_until_confirmed(project_fixture):
    result = await project_fixture.extract_actions("张三周五完成测试")
    assert result.candidates[0].status == "suggested"
    assert project_fixture.task_count() == 0
    project_fixture.confirm(result.candidates[0].id)
    assert project_fixture.task_count() == 1
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_project_assistant.py tests\test_assistant_confirmation.py -v
```

Expected: FAIL.

- [ ] **Step 3: Add project metadata, candidates, confirmation, and archive gate**

Project status is `active | archived`. Archived profile scope is read-only. Candidate confirmation records original draft, edited result, actor, citations, and profile version.

```python
def confirm_candidate(candidate, actor, edited_text):
    require_project_active(candidate.project_id)
    return create_project_task(text=edited_text, confirmed_by=actor["id"], source_candidate_id=candidate.id)
```

- [ ] **Step 4: Run project tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_project_assistant.py tests\test_assistant_confirmation.py tests\test_permissions.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/projects.py rag/assistants/tools/projects.py rag/user_db.py tests/test_project_assistant.py tests/test_assistant_confirmation.py
git commit -m "feat: add project assistant"
```

---

### Task 3: Data Analysis Router and Multi-Dataset Mapping

**Files:** Create data-analysis/structured tool modules and `tests/test_data_analysis_assistant.py`.

**Interfaces:** `plan_data_question(question, visible_datasets) -> DataAnalysisPlan`; `run_data_analysis(plan, scope) -> DataAnalysisResult`.

- [ ] **Step 1: Write failing mapping/payroll tests**

```python
def test_incompatible_units_require_mapping_confirmation(data_fixture):
    plan = data_fixture.plan(datasets=[dataset(unit="元"), dataset(unit="万元")])
    assert plan.status == "mapping_required"
    assert data_fixture.query_gateway.calls == []


def test_payroll_disabled_even_for_platform_admin(data_fixture):
    with pytest.raises(StructuredDataDisabled):
        data_fixture.run_payroll(user=platform_admin_without_hr())
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_data_analysis_assistant.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement semantic/lookup/aggregate/compare/hybrid routing**

Resolve dataset candidates via schema chunks, require mapping confirmation for type/unit/date/key mismatches, then call only `execute_structured_query`. Persist dataset versions, plan, row counts, and spreadsheet citations.

```python
if any(mapping.requires_confirmation for mapping in plan.mappings):
    return DataAnalysisResult(status="mapping_required", mappings=plan.mappings)
return execute_structured_query(plan.structured_query, scope)
```

- [ ] **Step 4: Run data/spreadsheet tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_data_analysis_assistant.py tests\test_structured_query.py tests\test_spreadsheet_api.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/data_analysis.py rag/assistants/tools/structured.py tests/test_data_analysis_assistant.py
git commit -m "feat: add spreadsheet analysis assistant"
```

---

### Task 4: Safe Chart Tool

**Files:** Create `rag/assistants/tools/charts.py`, `tests/test_safe_charts.py`; modify chart API/storage.

**Interfaces:** `ChartRequest(type, title, series, x_axis)`; `render_chart(request, scope) -> ChartArtifact`.

- [ ] **Step 1: Write failing path/code test**

```python
def test_chart_request_rejects_path_and_code():
    with pytest.raises(ValidationError):
        ChartRequest.model_validate(
            {"type": "bar", "title": "工资", "series": [], "output_path": "C:/secret.png", "python": "open('x')"}
        )
```

- [ ] **Step 2: Run RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_safe_charts.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implement enum-only rendering**

Allow `line | bar | stacked_bar | pie | scatter`, validated numeric series, unpredictable artifact IDs, expiry, and authorization on every download. Renderer chooses internal path.

```python
class ChartRequest(BaseModel):
    type: Literal["line", "bar", "stacked_bar", "pie", "scatter"]
    title: str
    series: tuple[ChartSeries, ...]
```

- [ ] **Step 4: Run chart/security tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_safe_charts.py tests\test_upload_security.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add rag/assistants/tools/charts.py tests/test_safe_charts.py
git commit -m "feat: render safe analysis charts"
```

---

### Task 5: Unified Start Forms, API Integration, and Verification

**Files:** Create frontend sales/project/data start forms and result components/tests; modify assistant API/registry, chat/home UI, development log.

**Interfaces:** Reuses `/assistants/{key}/query`; adds typed form payloads and confirmation routes.

- [ ] **Step 1: Write failing unified-platform UI test**

```typescript
it.each(['sales', 'project', 'data-analysis'])('opens %s inside existing chat', async key => {
  const wrapper = mountHomeWithAssistants()
  await wrapper.get(`[data-assistant="${key}"]`).trigger('click')
  expect(mockRouterPush).toHaveBeenCalledWith(expect.objectContaining({ name: 'chat' }))
  expect(mockWindowOpen).not.toHaveBeenCalled()
})
```

- [ ] **Step 2: Run RED**

```powershell
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/components/assistants/AdvancedAssistantForms.test.ts
Set-Location ..
```

Expected: missing components.

- [ ] **Step 3: Implement forms and confirmation UI**

Sales collects scenario/product/output type; project collects project/range/report type; data collects datasets/question/chart flag. Results use shared message, citation, reliability, favorite, feedback, and audit components.

```typescript
const submit = (assistantKey: string, payload: AssistantFormPayload) =>
  assistantsApi.query(assistantKey, payload)
```

- [ ] **Step 4: Run full verification**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/assistants tests/test_sales_assistant.py tests/test_project_assistant.py tests/test_assistant_confirmation.py tests/test_data_analysis_assistant.py tests/test_safe_charts.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
```

Expected: PASS.

- [ ] **Step 5: Document and commit**

```powershell
git add rag/assistants tests frontend/src docs/development-log-security-boundary-hardening.md
git commit -m "docs: verify advanced assistants"
```
