# 金典知识库权限前置检索与统一资源鉴权实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在“金典知识库”中把动作级 ACL 前置到 Qdrant、BM25、材料 FTS 和旧 KB 查询，并让所有预览、引用、版本、比较和下载入口统一实时鉴权。

**Architecture:** `OrgAclService` 先返回 `AuthorizedDocumentScope`；各召回器只用其中的 document IDs 在候选生成前过滤，审计记录名单与文档两类 ACL 版本；版本化检索成为 pipeline、agent 和旧 KB query 的唯一检索路径；所有资源入口按具体 action 二次鉴权。最终答案缓存继续关闭。

**Tech Stack:** Python 3.12、Qdrant client、rank-bm25、SQLite FTS5、FastAPI、pytest、Vue 3/Vitest

## Global Constraints

- 工作目录固定为 `C:\Users\Administrator\Desktop\金典知识库`。
- 空 ACL 和过滤失败均 fail closed；无权文本不能进入候选、模型、标题、摘要、引用或异常信息。
- Qdrant 批次先全局合并 dense score，再做 RRF。
- download 独立检查；`downloadable=false` 全局拒绝普通下载。
- final-answer cache read/write 保持禁用；本计划不实现 ACL 缓存。
- 解析、分块、embedding、OCR、表格、版本存储、材料任务、引用基础结构和重试机制直接复用 V3；本计划仅定点修改检索与资源访问边界。
- `roster_acl_version` 不写入 Qdrant point，名单更新不得触发全量向量刷新。

---

### Task 1: 索引稳定文档与逐文档 ACL 版本元数据

**Files:**
- Modify: `rag/vector_store.py`
- Modify: `rag/materials/worker.py`
- Test: `tests/test_vector_acl_payload.py`

**Interfaces:**
- Produces: Qdrant payload `document_id/owner_org_code/sensitivity/document_acl_version`，并保留现有内容/逻辑版本字段；`roster_acl_version` 仅随 `AuthorizedDocumentScope` 进入查询审计。

- [ ] **Step 1: 写失败测试**

捕获 point payload；验证存在逐文档 `document_acl_version` 且不存在 `roster_acl_version`；名单版本变化不触发向量重建；未归属/未激活敏感文档拒绝索引；worker 不从路径或名称猜 ACL。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_vector_acl_payload.py -q`  
Expected: FAIL，payload 不完整。

- [ ] **Step 3: 显式传递 ACL 快照**

worker 在 UserDB 事务外读取已提交的文档 ACL 快照，并作为参数传到 vector_store；索引 payload 不含 NAS 路径或全局名单版本。

- [ ] **Step 4: 运行索引回归**

Run: `pytest tests/test_vector_acl_payload.py tests/test_incremental_material_index.py tests/test_vector_store_idempotent.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/vector_store.py rag/materials/worker.py tests/test_vector_acl_payload.py
git commit -m "feat: index governed document acl metadata"
```

### Task 2: Qdrant ACL 分批与全局 dense 合并

**Files:**
- Modify: `rag/vector_store.py`
- Test: `tests/test_vector_acl_filter.py`

**Interfaces:**
- Consumes: `AuthorizedDocumentScope.document_ids: frozenset[int]`；调用边界不得把整个 scope 或版本字段交给 Qdrant。
- Produces: `search_collection(..., allowed_document_ids: AbstractSet[int] | None)`；空集合返回空，None 仅供标记的系统维护调用。

- [ ] **Step 1: 写失败测试**

验证 MatchAny、空集不查、异常不无过滤重试；超过批量阈值时每批同向量/同条件/同候选深度，按 point ID 去重最高 raw score，全局 score 降序截断；不得拼接批内名次。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_vector_acl_filter.py -q`  
Expected: FAIL，参数/合并不存在。

- [ ] **Step 3: 实现过滤与全局合并**

仅同 collection 和 distance 的 dense score 直接比较；合并后的 dense 列表再交给 Retriever 与 BM25 做 RRF。

- [ ] **Step 4: 运行向量回归**

Run: `pytest tests/test_vector_acl_filter.py tests/test_vector_store.py tests/test_versioned_retrieval.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/vector_store.py tests/test_vector_acl_filter.py
git commit -m "feat: merge acl-filtered vector batches globally"
```

### Task 3: BM25 评分前 ACL

**Files:**
- Modify: `rag/retriever.py`
- Test: `tests/test_bm25_acl_filter.py`

**Interfaces:**
- Produces: `Retriever.retrieve(..., allowed_document_ids)`；dense 与 BM25 全局候选完成后统一 RRF。

- [ ] **Step 1: 写召回质量失败测试**

构造 20 个高匹配无权文档和 1 个较弱有权文档；top_k=5 必须返回有权文档。缺 document_id 的旧 chunk 在用户查询中拒绝。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_bm25_acl_filter.py -q`  
Expected: FAIL，有权结果被挤出。

- [ ] **Step 3: 实现评分前候选掩码**

先筛 chunk indices，再计算/选择 BM25 分数；空集立即返回；禁止先全库 top_k 后过滤。

- [ ] **Step 4: 运行检索回归**

Run: `pytest tests/test_bm25_acl_filter.py tests/test_retriever.py tests/test_rrf_dedup.py tests/test_retriever_bm25_persistence.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/retriever.py tests/test_bm25_acl_filter.py
git commit -m "feat: rank bm25 only within authorized documents"
```

### Task 4: 材料 FTS 与 /materials/locate 前置 ACL

**Files:**
- Modify: `rag/user_db.py`
- Modify: `rag/materials/locator.py`
- Modify: `rag/materials/api.py`
- Modify: `rag/materials/archives.py`
- Test: `tests/test_material_fts_acl.py`
- Test: `tests/test_material_locator.py`

**Interfaces:**
- Produces: `search_material_search_fts(text, allowed_document_ids, limit=50)`；空集合返回空；材料定位 FTS/semantic 共用允许文档集合。

- [ ] **Step 1: 写失败测试**

验证 FTS SQL 在 LIMIT 前限制 document IDs；大量无权高匹配记录不能挤掉有权记录；`/materials/locate`、archive search 不泄露无权文件名、数量、路径或下载 URL；ACL 异常返回空/受控错误。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_material_fts_acl.py tests/test_material_locator.py -q`  
Expected: FAIL，FTS 仍全库查询。

- [ ] **Step 3: 实现 FTS SQL 过滤**

用临时值表/参数化 CTE 或安全分批方式在 FTS MATCH 与 LIMIT 同一查询层过滤；不得字符串拼接 ID。locator 在检索前调用 `allowed_document_ids(view)`，不再先全库 FTS 再丢弃。

- [ ] **Step 4: 运行材料回归**

Run: `pytest tests/test_material_fts_acl.py tests/test_material_locator.py tests/test_material_api.py tests/test_archive_import.py tests/test_incremental_material_index.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/user_db.py rag/materials/locator.py rag/materials/api.py rag/materials/archives.py tests/test_material_fts_acl.py tests/test_material_locator.py
git commit -m "feat: filter material fts before limiting results"
```

### Task 5: Pipeline、Agent 与旧 KB 查询统一版本化检索

**Files:**
- Modify: `rag/pipeline.py`
- Modify: `rag/versioned_retrieval.py`
- Modify: `rag/agent.py`
- Modify: `rag/tools.py`
- Modify: `rag/api.py`
- Test: `tests/test_pipeline_org_acl.py`
- Test: `tests/test_agent_org_acl.py`
- Test: `tests/test_legacy_kb_query_acl.py`

**Interfaces:**
- Produces: 用户查询在任何 retrieve 前调用 `allowed_document_ids(view)`；`POST /knowledge-bases/{kb_id}/query` 调同一版本化 ACL 服务，不再调用 `KnowledgeBaseManager.search` 后过滤。

- [ ] **Step 1: 写失败测试**

覆盖 pipeline/stream/agent/比较工具；ACL 空集不 embed；异常 fail closed；历史版本仍按逻辑文档；模型不能伪造 allowed IDs；并发不串 ACL；旧 KB query 无权结果不占 top_k。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_pipeline_org_acl.py tests/test_agent_org_acl.py tests/test_legacy_kb_query_acl.py -q`  
Expected: FAIL，仍有后过滤路径。

- [ ] **Step 3: 收口检索入口**

服务端闭包绑定 ACL，不暴露给 LLM schema；用户路径禁止 `allowed_document_ids=None`。移除或停用旧 KB 旁路，保留响应兼容层。

- [ ] **Step 4: 运行检索回归**

Run: `pytest tests/test_pipeline_org_acl.py tests/test_agent_org_acl.py tests/test_legacy_kb_query_acl.py tests/test_pipeline.py tests/test_agent_permission_filter.py tests/test_versioned_retrieval.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/pipeline.py rag/versioned_retrieval.py rag/agent.py rag/tools.py rag/api.py tests/test_pipeline_org_acl.py tests/test_agent_org_acl.py tests/test_legacy_kb_query_acl.py
git commit -m "feat: unify permission-aware knowledge retrieval"
```

### Task 6: 版本、下载、表格与结构化数据统一动作鉴权

**Files:**
- Modify: `rag/operations_api.py`
- Modify: `rag/spreadsheets/api.py`
- Modify: `rag/materials/api.py`
- Modify: `rag/api_security.py`
- Test: `tests/test_document_access_unified.py`
- Test: `tests/test_spreadsheet_acl.py`

**Interfaces:**
- Produces: `require_document_action(request, document_id, user, action)`；版本下载用 download；行读取/结构化查询用 view；导出用 download；写操作用 edit/delete/manage 对应动作。

- [ ] **Step 1: 写失败测试**

验证 view 可预览但不可下载；edit 不含 download；manage 不含 view；`downloadable=false` 拒绝 owner/admin 普通下载；历史版本下载实时授权；表格行/结构化查询/导出分别使用正确动作；响应不含物理路径。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_document_access_unified.py tests/test_spreadsheet_acl.py -q`  
Expected: FAIL，operations download 仍只校验 view。

- [ ] **Step 3: 实现统一 action dependency**

替换 `operations_api.py` 版本下载的 `require_doc_view`；审计 action 与两个 ACL 版本。materials 的删除、恢复、组织和归档操作映射到明确动作。

- [ ] **Step 4: 运行资源回归**

Run: `pytest tests/test_document_access_unified.py tests/test_spreadsheet_acl.py tests/test_document_versions.py tests/test_spreadsheet_api.py tests/test_structured_query.py tests/test_material_api.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/operations_api.py rag/spreadsheets/api.py rag/materials/api.py rag/api_security.py tests/test_document_access_unified.py tests/test_spreadsheet_acl.py
git commit -m "feat: authorize every document action independently"
```

### Task 7: 引用、历史重开、比较与敏感不泄露

**Files:**
- Modify: `rag/citations.py`
- Modify: `rag/reliability/api.py`
- Modify: `rag/reliability/service.py`
- Modify: `rag/reliability/comparison.py`
- Test: `tests/test_citation_acl_reopen.py`
- Test: `tests/test_comparison_acl_reopen.py`
- Test: `tests/test_sensitive_metadata_masking.py`

**Interfaces:**
- Produces: 引用序列化和比较创建/重开时逐文档 `view` 实时检查；无权表现为不存在且不返回标题、摘要、数量、download URL。

- [ ] **Step 1: 写失败测试**

创建引用/比较后撤权，再重开必须 404/省略；敏感 deny 不泄露 doc_name、section、preview、命中数、路径或“存在但无权”原因；比较工具每次读取证据都复检。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_citation_acl_reopen.py tests/test_comparison_acl_reopen.py tests/test_sensitive_metadata_masking.py -q`  
Expected: FAIL，旧入口使用创建时权限或序列化链接。

- [ ] **Step 3: 实现实时复检和统一不存在语义**

引用响应仅在当前 view 允许时生成元数据；下载 URL 的出现不代表 download 已授权，点击仍检查 download。异常日志只记录内部 ID/reason code。

- [ ] **Step 4: 运行可靠性回归**

Run: `pytest tests/test_citation_acl_reopen.py tests/test_comparison_acl_reopen.py tests/test_sensitive_metadata_masking.py tests/test_comparison_persistence.py tests/test_answer_citations.py tests/test_reliability_api.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/citations.py rag/reliability tests/test_citation_acl_reopen.py tests/test_comparison_acl_reopen.py tests/test_sensitive_metadata_masking.py
git commit -m "fix: reauthorize citations and comparisons on access"
```

### Task 8: 保持答案缓存关闭并执行全链路验收

**Files:**
- Modify: `tests/test_pipeline_cache_security.py`
- Create: `tests/test_org_nas_kb_e2e.py`
- Modify: `docs/permission-design.md`
- Modify: `README.md`

**Interfaces:**
- Produces: 缓存禁用回归；本地模拟 NAS 的三模块闭环。

- [ ] **Step 1: 固化缓存禁用测试**

验证同步/流式 pipeline 不读取或写入最终答案缓存，即使 cache 对象存在；从本计划删除 fingerprint/恢复缓存的任何期望。

- [ ] **Step 2: 写端到端测试**

初始化名单、绑定正式/test userid、扫描 public/两个同名部门链/三类敏感目录，完成索引；验证普通成员、负责人、动作 deny、跨部门 allow、移动、撤权、FTS、旧 KB query、引用、版本下载、表格和比较。

- [ ] **Step 3: 运行完整测试**

Run: `pytest -q`  
Run: `cd frontend; npm test -- --run; npm run build`  
Expected: 全部 PASS。

- [ ] **Step 4: 更新文档并检查敏感信息**

记录动作矩阵、根规则、名单激活/回滚、本地模拟 NAS 和未来真实 NAS 接入；确认无凭据、生产绝对路径或测试 userid 被标为正式身份。

- [ ] **Step 5: 提交**

```powershell
git add tests/test_pipeline_cache_security.py tests/test_org_nas_kb_e2e.py docs/permission-design.md README.md
git commit -m "test: verify governed knowledge base end to end"
```
