# RAGv3 架构防腐与模块化解耦实施计划 (Superpowers Execution Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 全面解耦 `rag/api.py`（3,353行）与 `rag/user_db.py`（4,051行）两大上帝文件，统一鉴权依赖注入，实现 SQLite WAL 读写分离高并发连接池，对齐批量导入与版本树，根除项目架构“屎山化”风险。

**规范文件：** `docs/superpowers/specs/2026-10-17-codebase-antidegradation-and-modularization-design.md`

## 执行准则与门禁 (Execution Rules & Gates)
- **严格遵循 TDD**：所有重构必须先编写测试或确保对应用例覆盖，重构前后全量断言通过。
- **全量无回归保障**：重构全程保持现有 2,100+ 后端用例及 230+ 前端用例全绿通过。
- **静态检查零警告**：每次改动后必须执行 `ruff check .`，保持 0 warnings / 0 errors。
- **零破坏性改动**：保持 `UserDB` 门面及所有已有 HTTP API 签名、参数及响应模型 100% 向后兼容。

---

## 任务拆解与推进轨道 (Tasks)

### Track 1: API 层解耦与模块化路由 (`rag/routers/`)
- [x] 1. **[ROUTE-00] 编写路由完整性镜像对齐测试 (TDD)**
  - 创建 `tests/test_router_parity.py`。
  - 在重构前通过 FastAPI 运行时内省提取当前 `rag/api.py` 中所有注册的 `(path, methods)` 路由集合。
  - 断言拆分完成后的应用路由集合完全包含并严格对齐原集合，确保无任何端点遗漏或路径偏差。（已验证 100% 通过）

- [x] 2. **[ROUTE-01] 拆分认证与用户路由 `rag/routers/auth.py`**
  - 创建 `rag/routers/auth.py`，注册 `auth_router = APIRouter(prefix="/auth", tags=["auth"])`。
  - 迁入：`/login`, `/logout`, `/refresh`, `/me`, `/users`, `/reset-password`, `/api-keys` 及其处理函数。
  - 在 `rag/api.py` 中挂载 `app.include_router(auth_router)`。
  - 运行 `pytest tests/test_auth_session_security.py` 及路由镜像测试，确保 Green。

- [x] 3. **[ROUTE-02] 拆分文档管理与上传路由 `rag/routers/documents.py`**
  - 创建 `rag/routers/documents.py`，注册 `documents_router = APIRouter(prefix="", tags=["documents"])`。
  - 迁入：`/upload`, `/documents`, `/documents/{id}`, `/documents/versions`, `/documents/diff`, `/batch-import`。
  - 在 `rag/api.py` 中挂载 `app.include_router(documents_router)`。
  - 运行 `pytest tests/test_batch_import.py tests/test_documents.py`，确保 Green。

- [x] 4. **[ROUTE-03] 拆分问答与推理流式路由 `rag/routers/query.py`**
  - 创建 `rag/routers/query.py`，注册 `query_router = APIRouter(prefix="", tags=["query"])`。
  - 迁入：`/query`, `/query/stream`, `/query/compare`, `/query/batch`。
  - 运行 `pytest tests/test_stream_error_event.py tests/test_pipeline_lifecycle.py`，确保 Green。

- [x] 5. **[ROUTE-04] 拆分协同电子表格路由与分析卡片路由 `rag/routers/`**
  - 创建 `rag/routers/spreadsheets.py` 与 `rag/routers/analysis.py`。
  - 迁入表格在线读取、协同锁、单元格编辑、分析卡片 CRUD、导出与 LLM 摘要端点。
  - 运行 `pytest tests/test_analysis.py tests/test_spreadsheet_workbook_editor.py`，确保 Green。（63/63 与 14/14 全过）

- [x] 6. **[ROUTE-05] 拆分系统管理路由并精简 `rag/api.py`**
  - 创建 `rag/routers/system.py`，迁入 `/health`, `/metrics`, `/config` 等监控端点。
  - 在 `rag/api.py` 中保留应用工厂、CORS 中间件、Session 中间件和 Lifespan，代码行数精简。
  - 运行 `pytest tests/test_router_parity.py`，验证 100% 路由镜像对齐通过。

---

### Track 2: 数据访问层解耦与仓储模式 (`rag/repositories/`)
- [x] 7. **[REPO-00] 编写 UserDB 门面对等性基准测试 (TDD)**
  - 创建 `tests/test_user_db_facade.py`，覆盖用户增删改查、任务调度、文档权限查询等核心入口，确保解耦后对外暴露的方法签名与返回值结构零差异。（已验证 100% 通过）

- [x] 8. **[REPO-01] 抽象数据库连接管理器 `rag/db/connection.py`**
  - 实现 `ConnectionManager` 类，提供读连接池与带互斥锁的单一写连接。
  - 读连接自动执行 `PRAGMA query_only = ON; PRAGMA busy_timeout = 5000;`。
  - 编写 `tests/test_db_connection.py`，验证读操作可在写锁持有期间并发读取，且写锁互斥安全。（已验证 100% 通过）

- [x] 9. **[REPO-02] 提取独立 SQL Schema 迁移管理器 `rag/db/migrator.py`**
  - 将 `UserDB._init_tables` 中的 DDL 语句拆解为迁移文件：
    - `rag/db/migrations/001_initial_schema.sql`
    - `rag/db/migrations/002_org_permissions.sql`
    - `rag/db/migrations/003_spreadsheets.sql`
    - `rag/db/migrations/004_wiki_and_analysis.sql`
  - 实现基于 `schema_migrations` 元数据表的轻量级迁移调度器，测试其幂等性。（已验证 100% 通过）

- [ ] 10. **[REPO-03] 拆分子仓储类并接入 `UserDB` 门面**
  - 建立 `rag/repositories/`：
    - `user_repo.py`: 处理用户账户、凭证与安全版本；
    - `org_repo.py`: 处理组织架构树与部门角色；
    - `document_repo.py`: 处理文档权限与可见性授权；
    - `task_repo.py`: 处理后台任务状态与进度持久化；
    - `spreadsheet_repo.py`: 处理表格元数据与协同锁；
    - `history_repo.py`: 处理问答会话、历史流与点赞反馈。
  - 在 `rag/user_db.py` 中将原有的单体方法改为委托给对应的子仓储，行数从 4,051 行瘦身至 400 行以内的门面层。
  - 运行 `pytest tests/test_user_db_facade.py`，验证全功能正常。

---

### Track 3: 鉴权与安全链路标准化 (Deprecate `_require_auth`)
- [ ] 11. **[AUTH-01] 扫描并迁移剩余遗留端点至 `Depends(require_user)` (TDD)**
  - 编写 `tests/test_auth_dependency_coverage.py`，利用 FastAPI 路由依赖内省，断言不存在使用 raw `Header("")` 且缺少 `require_user` 保护的受限接口。
  - 逐步更新原遗留接口，将函数签名中手写的 `authorization: str = Header("")` 替换为 `user: User = Depends(require_user)`。
  - 删除 `_require_auth` 辅助函数。
  - 运行 `pytest tests/test_auth_session_security.py`，验证 Token 失效、API-Key 与 Session Cookie 均能无差别正常工作。

---

### Track 4: 消除子系统数据孤岛 (Subsystem Alignment)
- [x] 12. **[ALIGN-01] 批量导入对齐文档版本树 (TDD)**
  - 编写测试 `tests/test_batch_import_versioning.py`，断言通过 `/batch-import` 导入的文件会在 `document_versions` 表中产生有效的 `version_id`，且 Qdrant 向量 payload 中携带该 `version_id`。
  - 在 `rag/batch_importer.py` 中引入 `DocumentVersioning` 接口并在入库时创建初始版本记录。
  - 运行测试，验证版本隔离模式检索能正确匹配批量导入的片段。

- [x] 13. **[ALIGN-02] 重排序引擎配置中心化 (TDD)**
  - 编写测试 `tests/test_reranker_configuration.py`，验证更换 `Settings.reranker_provider`（SiliconFlow / Bailian / Local）时正确路由到对应的请求协议。
  - 在 `rag/reranker.py` 中消除写死的模型名称与 URL，统一从 `Settings` 读取。

---

### Track 5: 验证门禁与全量回归 (Verification Gate)
- [ ] 14. **[VERIFY-01] 运行全量 Python 后端测试套件**
  - 执行 `.\.venv\Scripts\pytest.exe -q`。
  - 确保 2,100+ 测试全绿（100% Passed）。

- [ ] 15. **[VERIFY-02] 运行全量前端 Vitest 套件并打包验证**
  - 在 `frontend/` 目录执行 `npx vitest run`。
  - 执行 `npm run build`，断言产物正常生成且无类型错误。

- [ ] 16. **[VERIFY-03] 运行代码风格与 Lint 检查**
  - 执行 `.\.venv\Scripts\ruff.exe check .`。
  - 确保 0 errors / 0 warnings。
