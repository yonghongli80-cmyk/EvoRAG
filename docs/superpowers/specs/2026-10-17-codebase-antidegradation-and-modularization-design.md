# RAGv3 架构防腐与模块化解耦规范 (Superpowers Design Spec)

**日期：** 2026-10-17  
**状态：** 评审通过 (Approved)  
**目标系统：** RAGv3 后端 API 层、数据访问层与子系统边界防腐治理  
**核心红线：**
1. 坚决不猜接口，全量向后兼容现有 HTTP 路由、参数、响应体与状态码。
2. 坚决不盲改，`UserDB` 对外 API 签名 100% 保持不变（采用门面模式 Facade）。
3. 严格保障全量回归测试 100% 通过（2,100+ 后端用例，230+ 前端用例），`ruff` 零报错。

---

## 1. 背景与治理目标

在经过多轮业务迭代后，RAGv3 系统积累了丰富的企业级特性（RAG、自愈演化、协同电子表格、知识库版本控制、Wiki 双向链接等）。然而代码审查揭示系统正快速滑向“大泥球 (Big Ball of Mud)”：
- `rag/api.py` 膨胀至 3,353 行，承载了全部路由、中间件与背景调度；
- `rag/user_db.py` 膨胀至 4,051 行，承载了 7 个业务领域的 SQL 与上千行内嵌 DDL 补丁；
- 鉴权逻辑存在 `Depends(require_user)` 与手写 `_require_auth` 混用的“双轨制”；
- 单一 Python `threading.Lock()` 强制串行化了 SQLite WAL 模式，锁死读并发；
- 批量导入遗漏了文档版本元数据，导致版本隔离检索时静默漏检。

本设计提出六大治理轨道（Tracks），采用外科手术式（Surgical）的渐进解耦策略，彻底消除系统“屎山化”风险。

---

## 2. 治理轨道与架构设计 (Tracks)

### Track 1: API 层解耦与模块化路由 (`rag/routers/`)
- **目标**：将 `rag/api.py` 职责收敛为纯粹的“装配工厂 (Application Assembler)”，代码行数从 3,353 行降至 300 行以内。
- **目标架构**：
  ```
  rag/
  ├── api.py                   # FastAPI app 实例化、Lifespan、全局中间件、路由挂载
  └── routers/
      ├── __init__.py
      ├── auth.py              # 登录、Token 刷新、登出、API-Key、用户信息
      ├── documents.py         # 文件上传、解析、版本树、Diff、批量导入
      ├── query.py             # 同步问答、流式问答、模型竞技场、多路对比
      ├── knowledge.py         # 知识库 CRUD、组织归属、统计、审计、回收站
      ├── spreadsheets.py      # 表格在线查看、协作者锁、结构变更、版本回滚
      ├── wiki.py              # 挂载已有 rag/wiki/api.py 路由
      ├── evolution.py         # 挂载已有 rag/evolution/ 诊断与候选中心路由
      └── system.py            # /health, /metrics, /system/info, 静态配置
  ```
- **兼容性保障**：
  - 各 Router 声明统一的 `prefix` 与 `tags`，保持既有 URL 完全一致（例如 `/auth/login`、`/documents/upload`）。
  - `rag/api.py` 依然导出 `app = create_app()`，Uvicorn 启动命令与 Dockerfile 入口无需任何修改。

---

### Track 2: 数据访问层解耦与仓储模式 (`rag/repositories/`)
- **目标**：彻底拆分 `rag/user_db.py`，使每个业务子领域拥有独立的 Repository；将嵌入式的 Schema 迁移逻辑外置为标准 SQL 脚本。
- **目标架构**：
  ```
  rag/
  ├── db/
  │   ├── connection.py        # 数据库连接管理器（读写分离）
  │   ├── migrator.py          # 启动时执行增量 SQL 迁移
  │   └── migrations/
  │       ├── 001_initial_schema.sql
  │       ├── 002_org_permissions.sql
  │       ├── 003_spreadsheets.sql
  │       └── 004_wiki_and_analysis.sql
  ├── repositories/
  │   ├── __init__.py
  │   ├── base.py              # 基础仓储接口与事务上下文
  │   ├── user_repo.py         # 用户、凭证、密保、安全版本
  │   ├── org_repo.py          # 部门、组织角色、权限策略
  │   ├── document_repo.py     # 文档权限、版本元数据、分享链接
  │   ├── history_repo.py      # 对话记录、消息流、用户反馈评分
  │   ├── task_repo.py         # 异步任务、状态回执
  │   └── spreadsheet_repo.py  # 表格元数据、协作者在线锁、修订版本
  └── user_db.py               # 门面类 (Facade)，对外部调用保持 100% 接口与行为兼容
  ```
- **门面模式实现原则**：
  - `UserDB` 构造函数接收 `db_path`，内部初始化各个 `Repository`。
  - 原有公有方法（如 `db.get_user(username)`、`db.add_task(task_id)`）直接委托给对应 Repository 执行：
    ```python
    class UserDB:
        def __init__(self, db_path: str = "data/users.db"):
            self._conn_mgr = ConnectionManager(db_path)
            self.users = UserRepository(self._conn_mgr)
            self.docs = DocumentRepository(self._conn_mgr)
            ...
        def get_user(self, username: str) -> Optional[dict]:
            return self.users.get_user(username)
    ```

---

### Track 3: 鉴权与安全链路标准化 (Deprecate `_require_auth`)
- **现状缺陷**：
  - 存在 20 多个端点仍然通过 `authorization: str = Header("")` 并在端点内手动调用 `_require_auth(authorization)`。这绕过了全局 API Key 支持，并且在 `auth_enabled=false` 时行为分歧。
- **整改方案**：
  - 统一迁移至 FastAPI 依赖注入：`current_user: User = Depends(require_user)` 或 `Depends(require_admin)`。
  - 完全废除并删除内联的 `_require_auth` 辅助函数。
  - 依赖统一注入确保：
    1. 自动提取 Cookie Session / Bearer Token / `X-API-Key`；
    2. 校验 `security_version` 与 `password_changed_at`（支持密码修改后立即注销历史 Token）；
    3. `auth_enabled=false` 时统一返回预设的匿名管理员身份。

---

### Track 4: SQLite WAL 读写分离与高并发连接池 (`ConnectionManager`)
- **现状缺陷**：
  - 虽然打开了 `PRAGMA journal_mode=WAL`，但全系统仅持有单一 `self._conn`，外部包裹着单一 `self._lock = threading.Lock()`。所有线程的 SELECT 查询均被排队，无法并发。
- **整改方案**：
  - 构建 `rag/db/connection.py:ConnectionManager`：
    - **写通道**：保留单一写连接 `_write_conn` 并由写锁（或内部队列）保护，执行 INSERT/UPDATE/DELETE/DDL。
    - **读通道**：维护读连接池（基于线程本地存储 `threading.local`），读连接开启 `PRAGMA query_only = ON` 与 `PRAGMA busy_timeout = 5000`。
    - **并发释放**：所有只读 Repository 方法通过 `with self._conn_mgr.read_cursor() as cur:` 获取连接，无需争抢写锁，充分释放 WAL 模式的多读并发潜力。

---

### Track 5: 消除子系统数据孤岛 (Subsystem Alignment)
1. **批量导入版本树对齐**：
   - 在 [`rag/batch_importer.py`](file:///d:/Desktop/RAGv3/rag/batch_importer.py) 中，在完成文件入库与 Qdrant 向量化时，显式调用 [`rag/document_versioning.py`](file:///d:/Desktop/RAGv3/rag/document_versioning.py) 的 `create_initial_version`。
   - 将生成的 `version_id` 同步打在 Qdrant payload 的 `version_id` 字段中。
   - 彻底修复“版本隔离模式”下批量导入文档全部失踪的隐形缺陷。
2. **重排序引擎配置统一**：
   - 重构 [`rag/reranker.py`](file:///d:/Desktop/RAGv3/rag/reranker.py)，移除写死的第三方服务商地址，改为从 `config.Settings` 动态读取 `reranker_provider` 与 `rerank_model`。

---

### Track 6: 前后端类型契约自动化 (Contract Drift Guard)
- **整改方案**：
  - 编写脚本 `scripts/export_openapi_schema.py`，无缝导出后端 FastAPI 最新 OpenAPI JSON。
  - 在 `frontend/package.json` 中增加 `npm run typecheck:api`，使用工具比对前后端 DTO 字段缺失，防止前端因后端字段重构出现静默崩溃。

---

## 3. 风险评估与防御矩阵

| 风险点 | 严重级 | 防御与兜底机制 |
| :--- | :--- | :--- |
| **路由重定向导致客户端 404** | High | 单元测试全覆盖：编写 `tests/test_router_parity.py`，遍历提取拆分前后所有路由列表，断言路径与请求方法完全一致。 |
| **UserDB 拆分导致已有测试 Broken** | High | 保持门面模式 `UserDB` 类与方法签名 100% 不动，底层实现逐个委托，确保现有 2,100+ 测试无需修改任何调用方。 |
| **SQLite 读写分离产生死锁** | Critical | 读连接严格设置 `PRAGMA query_only=ON`，写连接严格配置 `busy_timeout=5000` 并使用单一写锁排队，避免多写锁冲突。 |
| **SQL 迁移幂等性失效** | Medium | 使用 `schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMP)` 记录迁移状态，所有 DDL 采用事务包装。 |
