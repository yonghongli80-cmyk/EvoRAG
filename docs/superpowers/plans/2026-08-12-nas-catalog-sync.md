# 金典知识库 NAS 目录归属与增量同步实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在“金典知识库”中用本地只读目录模拟 NAS，按管理员根规则和最长组织路径链安全归属文件，并幂等同步到现有文档版本生命周期。

**Architecture:** `NasSource` 只负责发现/读取和可选原生文件 ID；`RootRuleResolver` 选择 public/org/sensitive 规则；`OrgPathResolver` 在父组织上下文内匹配最长链；同步器复用 material versions/jobs。无稳定文件 ID 时只在内容哈希全源一对一唯一时推断移动。

**Tech Stack:** Python 3.12、pathlib、SQLite、现有 material lifecycle、pytest

## Global Constraints

- 工作目录固定为 `C:\Users\Administrator\Desktop\金典知识库`。
- 当前仅使用 pytest 临时目录；NAS 只读；绝对路径/凭据不进响应、JWT、向量或日志。
- 未命中根规则、组织路径歧义或不安全路径默认拒绝且不索引。
- `source_key` 不等于稳定文件身份；移动识别遵守 native ID/唯一哈希规则。
- NAS 离线不能生成删除；权限变化先于异步索引刷新生效。
- 直接复用 V3 的文档版本存储、材料任务队列、解析/分块/索引生命周期；本计划只新增 `rag/nas_catalog/` 和必要的适配调用，不复制或重写这些成熟模块。
- public 根固定归属公司根 `org_code`；敏感根无组织后缀时也归属公司根，有后缀时归属最长路径解析出的组织。

---

### Task 1: NAS 源、原生文件 ID 与目录表

**Files:**
- Create: `rag/nas_catalog/models.py`
- Create: `rag/nas_catalog/source.py`
- Create: `rag/nas_catalog/repository.py`
- Modify: `rag/db_migrations.py`
- Test: `tests/test_nas_source.py`
- Test: `tests/test_nas_catalog_repository.py`

**Interfaces:**
- Produces: `DiscoveredFile(source_key, relative_path, native_file_id, size_bytes, mtime_ns)`；`NasSource.iter_files/open_binary`；`LocalPathNasSource`。
- Produces: `nas_sources`、`nas_root_rules`、`nas_catalog_entries`，其中 `(source_id,native_file_id)` 仅对非空值唯一，`(source_id,source_key)` 唯一。

- [ ] **Step 1: 写失败测试**

覆盖中文嵌套、symlink/`..`/根逃逸、规范 `/`；本地适配器尽力返回 NTFS file ID，无法取得时为 null；离线扫描不标 missing；仓储所有写入遵守 UserDB 锁和事务。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_nas_source.py tests/test_nas_catalog_repository.py -q`  
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现源、表和状态机**

`source_key=sha256(normalized_relative_path)` 只代表当前位置；状态限定为 discovered/resolved/unresolved/index_pending/active/missing/deleted/error；连续两次成功完整扫描才确认 missing。

- [ ] **Step 4: 运行安全回归**

Run: `pytest tests/test_nas_source.py tests/test_nas_catalog_repository.py tests/test_db_migrations.py tests/test_upload_security.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/nas_catalog rag/db_migrations.py tests/test_nas_source.py tests/test_nas_catalog_repository.py
git commit -m "feat: add stable-aware read-only nas catalog"
```

### Task 2: 管理员根规则与最长组织路径链

**Files:**
- Create: `rag/nas_catalog/path_resolver.py`
- Test: `tests/test_org_path_resolver.py`

**Interfaces:**
- Produces: `RootRuleResolver.resolve(relative_path) -> RootResolution`；`OrgPathResolver.resolve(segments, start_org_code=None) -> OwnershipResolution`。

- [ ] **Step 1: 写失败测试**

覆盖 `/公司公共资料`→public；`/部门资料/品牌品类事业部/价格维护组` 与 `/部门资料/平台回收事业部/价格维护组` 得到不同 org_code；敏感三根；最长前缀；同父别名；跨父同名；链歧义、未知、NFKC 和非法路径拒绝。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_org_path_resolver.py -q`  
Expected: FAIL，解析器不存在。

- [ ] **Step 3: 实现确定性链匹配**

先唯一选择最长 root rule，再沿父子边消费最多目录段；名称/别名查询始终包含 parent org context。项目/年份等剩余段继承已匹配的最深组织。禁止模型猜测。

- [ ] **Step 4: 运行解析测试**

Run: `pytest tests/test_org_path_resolver.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/nas_catalog/path_resolver.py tests/test_org_path_resolver.py
git commit -m "feat: resolve controlled nas ownership paths"
```

### Task 3: 文件移动身份与扫描差异

**Files:**
- Create: `rag/nas_catalog/diff.py`
- Test: `tests/test_nas_move_detection.py`

**Interfaces:**
- Produces: `plan_catalog_diff(previous, current) -> CatalogDiff`，分类 added/changed/moved/missing/unchanged/ambiguous。

- [ ] **Step 1: 写失败测试**

验证相同 native_file_id 改路径为 moved；无 native ID 但内容哈希在全源旧/新各唯一时为 inferred moved；重复哈希为删除+新增；跨 source 永不推断；路径变化不能靠 source_key 维持身份。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_nas_move_detection.py -q`  
Expected: FAIL，diff 不存在。

- [ ] **Step 3: 实现保守差异算法**

优先 native ID；其次严格一对一 hash；其余不猜。结果携带 identity_reason 供审计。

- [ ] **Step 4: 运行测试**

Run: `pytest tests/test_nas_move_detection.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/nas_catalog/diff.py tests/test_nas_move_detection.py
git commit -m "feat: detect nas moves without path identity assumptions"
```

### Task 4: 增量同步到文档版本与动作 ACL

**Files:**
- Create: `rag/nas_catalog/sync.py`
- Test: `tests/test_nas_catalog_sync.py`

**Interfaces:**
- Produces: `NasCatalogSync.scan(source_id, actor_id) -> SyncResult`；写 `document_org_acl` 并递增 `document_acl_version`；复用现有 `create_document_version/enqueue_latest_index_job`。

- [ ] **Step 1: 写端到端失败测试**

验证 public/org/sensitive 文件；未解析不建 FTS/向量任务；修改建新版本；移动到另一组织时事务内先失效旧 ACL；唯一 hash 保留 document ID；重复 hash 删除+新增；离线不删；确认缺失进入墓碑。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_nas_catalog_sync.py -q`  
Expected: FAIL，同步器不存在。

- [ ] **Step 3: 实现同步编排**

受控复制到版本存储后再解析；不让 worker 持久占用 NAS。权限事务提交后才排索引任务；敏感和未解析文档的日志不写文件名/路径。

- [ ] **Step 4: 运行生命周期回归**

Run: `pytest tests/test_nas_catalog_sync.py tests/test_material_jobs.py tests/test_document_versions.py tests/test_material_recycle.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/nas_catalog/sync.py tests/test_nas_catalog_sync.py
git commit -m "feat: synchronize governed nas documents"
```

### Task 5: NAS 管理 API 与阶段验收

**Files:**
- Modify: `config.py`
- Modify: `rag/api.py`
- Test: `tests/test_nas_catalog_api.py`

**Interfaces:**
- Produces: source/root-rule CRUD、手动 scan、unresolved/ambiguous 列表；配置默认关闭。

- [ ] **Step 1: 写失败测试**

验证管理员限制、允许根目录、规则冲突、响应路径脱敏、并发扫描 409、离线可重试、默认 disabled；普通用户无法获知未解析文件名或数量。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_nas_catalog_api.py -q`  
Expected: FAIL，端点不存在。

- [ ] **Step 3: 实现配置和薄 API**

增加 `nas_enabled=False`、allowed roots、文件数/大小上限；API 调用服务，不接收任意读取路径。

- [ ] **Step 4: 阶段验收**

Run: `pytest tests/test_nas_source.py tests/test_nas_catalog_repository.py tests/test_org_path_resolver.py tests/test_nas_move_detection.py tests/test_nas_catalog_sync.py tests/test_nas_catalog_api.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add config.py rag/api.py tests/test_nas_catalog_api.py
git commit -m "feat: administer governed nas synchronization"
```
