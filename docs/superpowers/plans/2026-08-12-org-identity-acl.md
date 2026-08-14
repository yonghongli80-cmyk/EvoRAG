# 金典知识库初始化、身份与动作级 ACL 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 初始化唯一最终项目“金典知识库”，并建立稳定名单人员、正式 userid 绑定、组织全路径和逐动作 allow/deny ACL。

**Architecture:** RAGv3 是代码底座，“金典拍拍知识库”是名单与规则来源；初始化后只修改 `C:\Users\Administrator\Desktop\金典知识库`。新 `rag/org_acl/` 通过 `UserDB` 的锁和事务访问 SQLite，名单以快照预检、确认和回滚方式激活。

**Tech Stack:** Python 3.12（`pyproject.toml requires-python >=3.12`，Docker 3.12）、FastAPI、Pydantic、SQLite、pytest、Vue 3/Vitest

## Global Constraints

- 所有命令和文件路径在 Task 0 后均相对 `C:\Users\Administrator\Desktop\金典知识库`。
- RAGv3 与“金典拍拍知识库”在初始化后只读；禁止向它们写业务改动。
- `roster_person_id` 是名单人员主键；姓名不鉴权；正式 userid 可为空。
- 权限按 `view/download/edit/delete/manage` 每动作一行；deny 优先；download 独立。
- 最终答案缓存保持关闭。
- V3 复用采用“受控当前工作树快照”：复制 tracked 文件和四份设计/计划白名单，记录 HEAD、tracked diff/hash、额外文件 hash 与排除项；禁止复制整个 dirty 目录。

---

### Task 0: 初始化唯一最终项目

**Files:**
- Source: `C:\Users\Administrator\Desktop\RAGv3\RAGv3`
- Source: `C:\Users\Administrator\Desktop\金典拍拍知识库`
- Create: `C:\Users\Administrator\Desktop\金典知识库`
- Create: `docs/source-baseline.md`

**Interfaces:**
- Produces: 独立 Git 仓库“金典知识库”；可重复的来源清单；通过的基线测试。

- [ ] **Step 1: 只读校验三个绝对路径**

确认目标目录存在且为空；记录两个来源的 Git HEAD、dirty 状态、Python/Node 版本。列出 RAGv3 tracked 文件、tracked 修改和未跟踪文件；未跟踪文件默认排除，仅四份已审核设计/计划进入额外白名单。若目标非空则停止，不覆盖。

- [ ] **Step 2: 复制代码底座**

按 `git ls-files` 清单复制 RAGv3 当前工作树内容，再复制四份白名单设计/计划；不得对源目录做无差别递归复制。排除 `.git`、`.venv`、`node_modules`、`data` 运行内容、`.pytest_cache`、`static/assets` 历史构建、其他未跟踪文件和密钥。复制前扫描候选清单中的 `.env`、凭据和私钥特征；命中即停止。复制完成后解析目标绝对路径，确认所有写入均位于“金典知识库”。

- [ ] **Step 3: 创建独立仓库与基线文档**

`docs/source-baseline.md` 写入两个来源路径、RAGv3 HEAD、tracked 修改文件及 SHA-256、四份白名单文件及 SHA-256、排除项、迁入的 Demo 数据/规则文件清单和“后续只改本目录”。执行 `git init`；首次提交包含完整、可运行且已做密钥扫描的代码底座与基线文档，不包含运行产物。

- [ ] **Step 4: 安装/复用依赖并跑基线**

Run: `python --version`，Expected: `Python 3.12.x`。  
Run: `pytest -q`。  
Run: `cd frontend; npm ci; npm test -- --run; npm run build`。  
同时显式创建被排除但运行所需的空目录，并使用隔离临时数据库完成一次后端启动/健康检查；前端构建产物由命令重建且保持不提交。Expected: 全部 PASS；若来源本身有失败，记录精确测试名并在继续前处理。

- [ ] **Step 5: 提交**

```powershell
git add -A
git status --short
git commit -m "chore: initialize jindian knowledge base"
```

### Task 1: 稳定人员、组织与规则数据库

**Files:**
- Create: `rag/org_acl/models.py`
- Modify: `rag/db_migrations.py`
- Test: `tests/test_org_acl_schema.py`

**Interfaces:**
- Produces: `roster_people`、`external_identities`、`org_units`、`org_unit_aliases`、`roster_snapshots`、`roster_snapshot_items`、`org_memberships`、`org_managers`、`sensitive_grants`、`document_org_acl`、`document_acl_rules`、`acl_audit_logs`。
- Produces: `AclAction`, `AclEffect`, `AuthorizationDecision`, `AccessContext`。

- [ ] **Step 1: 写失败测试**

验证迁移幂等；`roster_person_id` 唯一；`external_userid` 可空且 `(provider, external_userid)` 对非空值唯一；组织 `(parent_org_code, normalized_name)` 唯一；相同“价格维护组”可存在于不同父节点；规则唯一键包含 document/subject/action/effect。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_org_acl_schema.py -q`  
Expected: FAIL，表不存在。

- [ ] **Step 3: 实现迁移和值对象**

`document_acl_rules` 包含 `subject_type/subject_id/action/effect/expires_at/granted_by/active`；`document_org_acl` 包含独立 `document_acl_version`；系统状态表包含 `roster_acl_version`。Python 端使用 Literal/Enum 限制五种动作和 allow/deny。

- [ ] **Step 4: 运行迁移回归**

Run: `pytest tests/test_org_acl_schema.py tests/test_db_migrations.py tests/test_sqlite_unified.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/org_acl/models.py rag/db_migrations.py tests/test_org_acl_schema.py
git commit -m "feat: add action-level organizational acl schema"
```

### Task 2: 遵守 UserDB 锁与事务的组织仓储

**Files:**
- Create: `rag/org_acl/repository.py`
- Modify: `rag/user_db.py`
- Test: `tests/test_org_acl_repository.py`

**Interfaces:**
- Produces: 由 `UserDB.org_acl_repository()` 创建的仓储；仓储不能接收裸 connection。
- Produces: `access_context(user_id)`, `active_rules(document_id, action, now)`, `bump_roster_acl_version()`, `bump_document_acl_version(document_id)`。

- [ ] **Step 1: 写并发与事务失败测试**

验证仓储写入使用同一 `_lock`；异常时多表回滚；并发身份绑定只有一个成功；不得出现嵌套 BEGIN 或绕锁 connection 访问。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_org_acl_repository.py -q`  
Expected: FAIL，仓储不存在。

- [ ] **Step 3: 实现 UserDB 托管仓储**

公共仓储方法内部 `with db._lock: with db.transaction(immediate=True):`；批量只读也持锁。仓储私有化 `_conn` 使用，业务服务不能直接拿 connection。

- [ ] **Step 4: 运行并发回归**

Run: `pytest tests/test_org_acl_repository.py tests/test_concurrency.py tests/test_api_concurrency.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/org_acl/repository.py rag/user_db.py tests/test_org_acl_repository.py
git commit -m "feat: persist org acl through userdb transactions"
```

### Task 3: 固定名单预检、确认、回滚

**Files:**
- Create: `rag/org_acl/roster.py`
- Test: `tests/test_org_roster_import.py`
- Create: `data/bootstrap/roster.snapshot.example.json`

**Interfaces:**
- Produces: `preview_roster(snapshot, actor_id) -> RosterPreview`；`confirm_roster(preview_id, actor_id) -> RosterActivation`；`rollback_roster(snapshot_id, actor_id)`。

- [ ] **Step 1: 写失败测试**

覆盖稳定 person ID、nullable 正式 userid、明确 test identity、兼任、重复 userid、孤儿/环、同父同名冲突、跨父同名合法；预检不改有效权限；确认原子递增 `roster_acl_version`；错误确认回滚；上一快照可恢复。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_org_roster_import.py -q`  
Expected: FAIL，接口不存在。

- [ ] **Step 3: 实现两阶段快照服务**

预检输出人员/组织/负责人/敏感授权差异和预计失权人数；确认必须引用未过期 preview hash；单事务切换 active snapshot。把旧 Demo 名单转换为示例快照时保留来源痕迹，不导入姓名登录代码。

- [ ] **Step 4: 运行名单回归**

Run: `pytest tests/test_org_roster_import.py tests/test_org_acl_repository.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/org_acl/roster.py data/bootstrap/roster.snapshot.example.json tests/test_org_roster_import.py
git commit -m "feat: activate reviewed roster snapshots"
```

### Task 4: 动作级授权服务

**Files:**
- Create: `rag/org_acl/service.py`
- Create: `rag/org_acl/__init__.py`
- Test: `tests/test_org_acl_service.py`

**Interfaces:**
- Produces: `authorize_document(user_id, document_id, action) -> AuthorizationDecision`；`allowed_document_ids(user_id, kb_id, action='view') -> AuthorizedDocumentScope`，其中 `document_ids: frozenset[int]`、`roster_acl_version: int`、`document_acl_versions: dict[int, int]`。

- [ ] **Step 1: 写权限矩阵失败测试**

覆盖本部门 view、负责人下级、平级拒绝、跨部门规则、个人 deny 覆盖部门 allow、规则到期、五动作互不隐含、edit 不自动 download、manage 不自动 view、HR 敏感资格、public、未归属和 `downloadable=false` 全局拒绝。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_org_acl_service.py -q`  
Expected: FAIL，服务不存在。

- [ ] **Step 3: 实现批量授权计算**

按总体设计第 6 节顺序计算；API 预设在写规则时展开成动作行；`allowed_document_ids()` 使用批量 SQL，禁止 N+1，并返回 `AuthorizedDocumentScope`。召回层只能读取 `scope.document_ids`，审计层记录 `scope.roster_acl_version` 和命中文档对应的 `scope.document_acl_versions`。

- [ ] **Step 4: 运行现有权限回归**

Run: `pytest tests/test_org_acl_service.py tests/test_permissions.py tests/test_kb_permissions.py tests/test_document_security_boundaries.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/org_acl tests/test_org_acl_service.py
git commit -m "feat: resolve per-action document authorization"
```

### Task 5: 管理 API 与正式身份绑定

**Files:**
- Modify: `rag/auth.py`
- Modify: `rag/api.py`
- Test: `tests/test_org_admin_api.py`
- Test: `tests/test_external_identity_auth.py`

**Interfaces:**
- Produces: `POST /admin/org/previews`、`POST /admin/org/previews/{id}/confirm`、`POST /admin/org/snapshots/{id}/rollback`、`POST /admin/identities/bind`、逐动作 ACL rule API。

- [ ] **Step 1: 写失败测试**

验证仅管理员；正式/test identity 分类；重复/未知绑定；客户端 userid 伪造；规则 action/effect/expiry 校验；preview 未确认前权限不变；管理响应不泄露敏感身份材料。

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_org_admin_api.py tests/test_external_identity_auth.py -q`  
Expected: FAIL，端点不存在。

- [ ] **Step 3: 实现薄 API**

API 只做 principal、Pydantic 校验和服务调用。后续钉钉扫码只需把服务端换得的 userid 交给同一 identity service；本任务不访问外网。

- [ ] **Step 4: 阶段验收**

Run: `pytest tests/test_org_acl_schema.py tests/test_org_acl_repository.py tests/test_org_roster_import.py tests/test_org_acl_service.py tests/test_org_admin_api.py tests/test_external_identity_auth.py tests/test_auth_jwt.py -q`  
Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/auth.py rag/api.py tests/test_org_admin_api.py tests/test_external_identity_auth.py
git commit -m "feat: administer roster identities and acl rules"
```
