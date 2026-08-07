# RAGv3 全项目审查修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 2026-08-07 全项目深度审查发现的所有问题（Critical 8 / High 18 / Medium 21 / Low 组），覆盖安全、正确性、可用性、前端、部署五大领域，并清理 git 卫生问题。

**Architecture:** 沿用现有分层（api → pipeline → retriever/generator/agent → user_db/vector_store/redis_cache），修复以"最小侵入 + 权限/缓存/并发三条主线"推进：①所有数据读取路径统一走权限过滤（单一事实来源 `scope`）；②缓存 key 纳入 `kb_id`+用户维度并在命中后校验权限；③同步阻塞调用全部移出 async 事件循环。前端统一 axios 拦截器与 `/api` 前缀契约。

**Tech Stack:** Python 3.12 + FastAPI + SQLite/Qdrant/Redis + LangChain + DeepSeek/BGE 接口;前端 Vue3 + TypeScript + Vite + Pinia;pytest + Vitest。修复全程 TDD。

## Global Constraints

- 基线：任何任务开始前先跑 `pytest tests/ -q` 确认全绿（当前约 250+ 用例），每任务结束该任务相关测试必须通过，不允许整段测试套件变红。
- 每个任务遵循 TDD 循环：写失败测试 → 运行确认失败 → 最小实现 → 运行确认通过 → 提交。
- 提交信息用 Conventional Commits（`fix:` / `feat:` / `refactor:` / `test:` / `docs:`），中文描述，可附 issue 编号。
- **不破坏现有架构模式**：除非任务显式标注，禁止大规模重构；`api.py` 已是 103KB 巨型文件，仅在有明确边界（如拆分 feedback/gap 连接管理）时做局部抽取。
- **权限单一事实来源**：所有 `check_doc_permission` / 检索过滤 / 下载校验统一读取 `scope` 列；`is_public` 与 `scope` 不得再双写不一致。
- **缓存安全**：`RedisCache._key` 必须包含 `kb_id` + 用户维度；缓存命中路径必须同样经过权限过滤。
- **异步纪律**：async 函数内不得直接调用同步 LLM/SQLite/网络阻塞操作，一律 `asyncio.to_thread(...)`。
- 密钥类：新加的配置从环境变量读取，禁止写入代码或提交到仓库。
- 前端与后端 API 前缀契约统一为 `/api`（dev proxy 与 axios baseURL 一致）。

---

## Phase 0：git 卫生与工程基线（3 任务，先做，立即可交付）

### Task 0.1：移除远程 URL 内嵌 GitHub PAT

**Files:**
- Modify: `.git/config`（不在提交内，但必须在实现环境处理）

**背景：** `git remote -v` 显示 origin 为 `https://ghp_***@github.com/sighistp/RAG.git`，内嵌明文 GitHub PAT，任何能读取该仓库 `.git/config` 的人都可使用它。

- [ ] **Step 1：** 记录需轮换的 token 前缀（`ghp_` 开头）并通知用户在 GitHub Settings → Developer settings → Personal access tokens 中 **Revoke 该 token 并生成新的**（新 token 不再写入 URL）。
- [ ] **Step 2：** 移除 URL 中的凭据：
  ```bash
  git remote set-url origin https://github.com/sighistp/RAG.git
  ```
- [ ] **Step 3：** 验证：`git remote -v` 不再含 `ghp_`；`git fetch` 能正常完成（若需要认证走凭据管理器 `gh auth` 或新 token 通过提示输入）。
- [ ] **Step 4：** 提交：`docs: 说明远程地址移除内嵌凭据`（`.git/config` 不入库，此提交可选）。

**Acceptance:** 远程 URL 无明文 token；用户已轮换原 token。

### Task 0.2：`.gitignore` 补充 `.venv` 等项目目录

**Files:**
- Modify: `.gitignore`

**背景：** 仓库根目录存在 `.venv/`（未跟踪且未忽略），有被误提交风险。

- [ ] **Step 1：** 在 `.gitignore` 追加：
  ```gitignore
  # Python virtual environments
  .venv/
  venv/
  ```
- [ ] **Step 2：** 验证：`git check-ignore -v .venv` 输出匹配项。
- [ ] **Step 3：** 提交：`chore: gitignore 忽略 .venv`。

**Acceptance:** `.venv` 被忽略，`git status` 干净。

### Task 0.3：依赖清单统一（pyproject ↔ requirements 漂移）

**Files:**
- Modify: `pyproject.toml`、`requirements.txt`
- Test: `tests/test_upload_smoke.py`（新建，验证 python-multipart 存在时上传可用）

**背景：** `requirements.txt` 含 `python-multipart`、`redis`，但 `pyproject.toml [project.dependencies]` 缺失；`pip install -e .` 会装出"上传 500 + 无 Redis"的运行时。

- [ ] **Step 1：** 写失败测试 `tests/test_upload_smoke.py`：用 `TestClient` 上传小文件断言 200（此测试仅在 `python-multipart` 缺失时失败，先确认它能在当前环境通过以作回归）。
- [ ] **Step 2：** 对齐依赖：把 `python-multipart`、`redis` 补入 `pyproject.toml [project.dependencies]`；两处版本区间保持一致（建议 `python-multipart>=0.0.9`、`redis>=5.0`）。
- [ ] **Step 3：** 运行 `pip install -e .` 验证可导入：`python -c "import multipart, redis"`。
- [ ] **Step 4：** 提交：`fix: pyproject 补齐 python-multipart/redis，统一依赖清单`。

**Acceptance:** `pip install -e .` 后 `import multipart, redis` 成功，上传路径有回归测试。

---

## Phase 1：Critical 安全修复（8 任务）

### Task 1.1：缓存 key 纳入 kb_id + 用户维度，命中后仍走权限过滤

**Files:**
- Modify: `rag/redis_cache.py:46-48`（`_key`）、`rag/pipeline.py:80-82`（读）、`:177`（写）
- Test: `tests/test_cache_permission_isolation.py`（新建）

**背景：** `_key` 仅 `md5(question)`；命中发生在 `filter_chunks_by_permission` 之前。用户 A 查私有文档后，用户 B 问同问题直接拿到 A 的答案；KB A/B 互相串答案。

- [ ] **Step 1：** 写失败测试：同一 question 在 user1/kb1 与 user2/kb2 下 `get` 返回不同/独立；模拟缓存命中返回后断言权限过滤函数仍被调用。
- [ ] **Step 2：** 改造 `_key(question, kb_id, user_id)` → `rag:query:{kb_id}:{user_id}:{md5(question)}`；`get`/`set`/`delete` 签名同步加参数（默认参数兼容旧调用，`user_id="__anon__"` 用于匿名）。
- [ ] **Step 3：** `pipeline.py`：`self._cache.get(question, kb_id, user_id)`；对缓存命中的 `answer`，在返回前对 `sources` 用 `filter_chunks_by_permission` 重新校验（命中缓存也记录到 memory，见 Task 2.8）。
- [ ] **Step 4：** 提交：`fix: 缓存 key 纳入 kb_id+用户维度并校验权限，杜绝跨用户/跨库泄露`。

**Acceptance:** 新增隔离测试通过；缓存命中路径不再跳过权限过滤。

### Task 1.2：agent 路由补权限过滤 + 路由判定精确化

**Files:**
- Modify: `rag/pipeline.py:84-88`、`rag/agent.py:20-31`（retrieve 工具）、`rag/agent.py:249-257`（route_question 判定）
- Test: `tests/test_agent_permission_filter.py`（新建）

**背景：** `route=="agent"` 提前 return，不走 retrieve/rerank/权限过滤；agent 的 `retrieve` 工具无条件检索整个集合（含私有 chunk）。路由判定是子串匹配 `"agent" in result`。

- [ ] **Step 1：** 写失败测试：用户无权文档 A，构造走 agent 路由的问题，断言回答不引用文档 A 的 chunk。
- [ ] **Step 2：** `agent.py` 的 `RAGAgent` 接受 `user` + `kb_id`（构造时传入）；`retrieve` 工具内调用 `retriever.retrieve(...)` 后追加 `filter_chunks_by_permission(self._user_db, kb_id, results, user)`。
- [ ] **Step 3：** `pipeline.py` agent 分支把 `user`/`kb_id` 透传给 `agent.run(...)`；`route_question` 判定改为精确匹配：`result.strip().lower() in {"agent", "rag", ...}` 白名单，落白名单外走默认 RAG。
- [ ] **Step 4：** 提交：`fix: agent 路由执行权限过滤并精确化路由判定`。

**Acceptance:** 新测试通过；agent 检索结果不含用户无权 chunk。

### Task 1.3：默认开启认证 + 非回环绑定强制认证

**Files:**
- Modify: `config.py:29`（`auth_enabled` 默认）、`start.py`、`.env.example`
- Test: `tests/test_auth_default_enabled.py`（新建）

**背景：** `auth_enabled: bool = False` 默认关闭认证，`.env` 未开启；容器绑定 `0.0.0.0`。当前部署等于全站匿名。

- [ ] **Step 1：** 写失败测试：不设置 `RAG_AUTH_ENABLED` 时 `settings.auth_enabled is True`。
- [ ] **Step 2：** `config.py` 改 `auth_enabled: bool = True`；`start.py` 增加启动告警：当 `host=="0.0.0.0"` 且 `auth_enabled` 为 False 时打印 ERROR 日志（不阻断）。
- [ ] **Step 3：** `.env.example` 显式示例 `RAG_AUTH_ENABLED=true`；本项目 `.env` 同步补 `RAG_AUTH_ENABLED=true`（并提醒用户确认）。
- [ ] **Step 4：** 提交：`fix: 默认开启认证，非回环绑定强制告警`。

**Acceptance:** 默认开启；相关既有鉴权测试仍全绿（若有依赖默认关闭的测试，一并修正其 fixture）。

### Task 1.4：数据源加归属、config 脱敏、禁止任意 SQL

**Files:**
- Modify: `rag/user_db.py:624-661`、`rag/data_sources/db_source.py:42-52`、`rag/data_sources/base.py`（如存在）、`rag/api.py:2347,2364,2440`
- Test: `tests/test_data_source_permissions.py`（新建）

**背景：** `data_sources` 无 `user_id`，list 原样回显完整连接串；`test_connection`/sync 会执行 `config` 里的任意 SQL；删除无归属校验。

- [ ] **Step 1：** 写失败测试：用户 B 不能 list/delete/sync 用户 A 的数据源；`list_data_sources` 返回的 config 不含密码字段。
- [ ] **Step 2：** `user_db.py`：迁移 `data_sources` 表加 `owner_id` 列（`ALTER TABLE ... ADD COLUMN owner_id TEXT`，兼容旧数据置 `'admin'` 或空）；`create_data_source` 记录 `owner_id`；`list_data_sources(user_id)`/`delete_data_source`/`get_data_source` 按 owner 过滤。
- [ ] **Step 3：** `api.py`：`/sources` 系列端点从 `_get_current_user` 取 user 并透传；list 返回前对 config 脱敏（密码/连接串字段替换为 `"***"`，新增 `has_secret` 布尔供前端提示）。
- [ ] **Step 4：** `db_source.py`：`test_connection` 改为只 `SELECT 1`（或 `PRAGMA schema_version`），不执行 `self.query`；`sync` 执行 SQL 前用白名单校验（仅允许以 `SELECT` 开头的只读语句，其他拒绝）。
- [ ] **Step 5：** 提交：`fix: 数据源按用户归属隔离、config 脱敏、禁止非只读 SQL`。

**Acceptance:** 新权限测试通过；list 不回显凭据；test_connection 不执行用户 SQL。

### Task 1.5：calculate 工具限制指数与结果位数

**Files:**
- Modify: `rag/tools.py:26-29`（`_eval_node` 的 `ast.Pow` 分支）、`:33-43`（calculate 入口）
- Test: `tests/test_tools_overflow.py`（新建）

**背景：** `ast.Pow` 无指数上限，`calculate("9**9**9")` 计算超大位数结果，耗尽内存/CPU 挂死 worker。

- [ ] **Step 1：** 写失败测试：`calculate("9**9**9")`、`calculate("2**2**2**2**2")` 返回错误消息而不是挂死；超大 `10**1000000` 同样被拒。
- [ ] **Step 2：** `_eval_node`：对 `ast.Pow` 先检查 `right` 实数值 > 1000 即抛 `ValueError("指数过大")`；对 `BinOp` 计算结果用 `int(result.bit_length())` / `abs(result) > 10**6` 检查位数，超限抛错。
- [ ] **Step 3：** calculate 入口捕获该 ValueError，返回 `"计算被拒绝：指数或结果超出安全范围"`，不抛异常。
- [ ] **Step 4：** 提交：`fix: calculate 工具限制指数与结果位数，防 DoS`。

**Acceptance:** 溢出测试快速返回错误，不占用大量资源。

### Task 1.6：上传强制认证、无权限记录默认拒绝、同名覆盖保护

**Files:**
- Modify: `rag/api.py:736-782`（upload）、`rag/permissions.py:45-47,98-100`
- Test: `tests/test_upload_auth_ownership.py`（新建）

**背景：** 上传不强制认证（权限记录仅登录时创建）；无记录文档被 `check_doc_permission` 一律放行；写盘无条件覆盖同名文件。→ 任意用户可删除/下载他人文件、覆盖他人文件。

- [ ] **Step 1：** 写失败测试：①无 token 上传被 401；②对无权限记录的文档，非 owner 调用 delete/download 被拒；③上传同名已存在文件且非 owner 时被 409。
- [ ] **Step 2：** `api.py` upload 端点加 `user = Depends(_get_current_user)`（认证开启时强制），并创建权限记录（`create_document_permission(owner=user["id"], is_public=False)`）。
- [ ] **Step 3：** `permissions.py`：`check_doc_permission` 对无记录文档由"放行"改为"按文档当前 `is_public`/owner 判定"——若 DB 完全无该文件记录，`delete` 返回 `None`（拒绝），`download` 仅当 `is_public` 显式成立才放行；`check_kb_permission` 同理。保留 `is_public=True` 的公开语义不受影响。
- [ ] **Step 4：** `api.py` 写盘前：若目标文件已存在，比较当前用户是否为 owner（或文件 `is_public`），否则 409。
- [ ] **Step 5：** 提交：`fix: 上传强制认证，无记录文档默认拒绝，防越权删除/覆盖`。

**Acceptance:** 新增三组失败测试通过；既有上传/download 正常路径仍通过。

### Task 1.7：统一 `is_public`/`scope` 单一事实来源

**Files:**
- Modify: `rag/user_db.py:880-894`（toggle_document_visibility）、`rag/permissions.py:53-61`、`rag/user_db.py:901-913`
- Test: `tests/test_visibility_scope_sync.py`（新建）

**背景：** 双份记账：toggle 只翻 `is_public`，授权判定读 `scope`。设为私有不生效（scope 仍 public = 数据继续公开）；设为公开不生效（scope 仍 private = 功能失效）。

- [ ] **Step 1：** 写失败测试：①owner 把 public 文档置私有后，非 owner 的 download/检索被拒；②私有文档置公开后，非 owner 可下载/检索。
- [ ] **Step 2：** `toggle_document_visibility(doc_id, is_public)` 内部同步写 `scope`：`scope = 'public' if is_public else 'private'`，并更新对应 permissions 行的 `scope`。
- [ ] **Step 3：** 收敛读取：检索过滤、下载、`check_doc_permission` 统一读 `scope`（并确保 `is_public` 与 `scope` 永远同写同读）。
- [ ] **Step 4：** 提交：`fix: is_public 与 scope 同步更新，单一事实来源`。

**Acceptance:** 双向切换测试通过；无其他路径残留双写。

### Task 1.8：query_stream 前置/后置挪进线程

**Files:**
- Modify: `rag/pipeline.py:180-220`（query_stream）、`rag/api.py:1184-1207`
- Test: `tests/test_stream_eventloop_nonblocking.py`（新建）

**背景：** async 生成器内同步调用 `_prepare_context`（同步 LLM + `time.sleep` 重试 + SQLite），阻塞整个事件循环，期间所有请求无响应。

- [ ] **Step 1：** 写失败测试：并发发起一个流式请求（mock LLM 慢响应）与一个健康检查 `/health`，断言 `/health` 在流式请求前置阶段不被阻塞（用 `asyncio` 计时断言）。
- [ ] **Step 2：** `query_stream` 开头改为 `prepared, error = await asyncio.to_thread(self._prepare_context, question, sid, doc_name, top_k, tags, user, kb_id)`；`to_thread` 前 `await asyncio.get_running_loop().run_in_executor(...)` 或直接 `asyncio.to_thread`（注意传参为关键字/位置一致）。
- [ ] **Step 3：** 生成阶段（`generate_stream` 的 async for）保持 await；流结束后 `memory.add_message`/`summarize`/`tracker.save`/`_cache.set` 全部包进 `await asyncio.to_thread(...)`。
- [ ] **Step 4：** 提交：`fix: 流式查询前置与收尾移入线程，避免阻塞事件循环`。

**Acceptance:** 非阻塞测试通过；流式输出功能与现有 SSE 测试保持通过。

---

## Phase 2：High 修复（18 任务）

### Task 2.1：agent 工具包装统一锁

**Files:**
- Modify: `rag/pipeline.py:66-67,136-143,197-204`
- Test: `tests/test_agent_tool_wrap_concurrency.py`（新建）

**背景：** 同步/异步两把锁（threading.Lock + asyncio.Lock）保护同一批可变 `tool.func`，并发 wrap/unwrap 撕裂状态。

- [ ] **Step 1：** 写失败测试：并发跑 `query()` 与 `query_stream()`（agent 路由），断言 `tool.func` 不残留 wrapper、`captured_calls` 不串线。
- [ ] **Step 2：** 方案 A（推荐）：去掉运行期改写 `tool.func` 的机制——改为 `RAGAgent.run` 接收 `tool_calls: list` 参数，工具调用时 append；或用 `threading.RLock` 一把锁同时保护同步与异步路径（把 async 路径包进 `asyncio.to_thread` 后用同一把 threading 锁）。
- [ ] **Step 3：** 提交：`fix: agent 工具包装改用单一互斥，消除并发竞态`。

**Acceptance:** 并发测试通过；无残留包装。

### Task 2.2：BM25 索引与向量库同步

**Files:**
- Modify: `rag/bm25_store.py`、`rag/vector_store.py:96-115,154-162`、`rag/retriever.py:96-108`
- Test: `tests/test_bm25_invalidation.py`（新建）

**背景：** `save_chunks` 仅首次无缓存时写 SQLite 快照；增删改后 BM25 永久陈旧。

- [ ] **Step 1：** 写失败测试：添加文档 → 删除文档 → BM25 检索不命中已删内容；添加 → 检索能命中新内容。
- [ ] **Step 2：** 在 `vector_store.add_to_collection` / `delete_doc` / `clear` 末尾调用 `bm25_store.invalidate()`（清模块级 `_bm25_cache` 并将 SQLite 快照标记失效）。
- [ ] **Step 3：** `retriever.py` 优先从**当前 chunk 集重建** BM25 而非总是读 SQLite 旧快照；或快照带版本号，与向量库集合变更计数比较。
- [ ] **Step 4：** 提交：`fix: BM25 缓存随文档增删改失效重建`。

**Acceptance:** 失效测试通过；删除文档不再被 BM25 命中。

### Task 2.3：embed 重试捕获 OpenAI 真实异常

**Files:**
- Modify: `rag/embedder.py:13-21`
- Test: `tests/test_embedder_retry_errors.py`（新建）

**背景：** `@retry(retryable_exceptions=(TimeoutError, ConnectionError, OSError))` 不覆盖 `openai.APIConnectionError`/`APIStatusError`/`APITimeoutError`，真实失败不被重试。

- [ ] **Step 1：** 写失败测试：mock `openai` 客户端首次抛 `APIConnectionError` 第二次成功，断言 `embed` 重试成功；抛 429 `APIStatusError` 也重试。
- [ ] **Step 2：** 改为 `@retry(retryable_exceptions=(openai.APIConnectionError, openai.APITimeoutError, openai.RateLimitError, openai.APIStatusError, TimeoutError, ConnectionError))`（`import openai` 顶层，配合 `try/except ImportError` 兜底）。
- [ ] **Step 3：** 提交：`fix: embed 重试捕获 OpenAI SDK 真实异常`。

**Acceptance:** 重试测试通过。

### Task 2.4：index_folder 原子化

**Files:**
- Modify: `rag/folder_indexer.py:83-110`
- Test: `tests/test_folder_indexer_atomic.py`（新建）

**背景：** `_vs.clear()` 先删后写，embed 失败即丢全部旧数据且 `index_state.json` 未更新。

- [ ] **Step 1：** 写失败测试：mock embed 在写入中途抛错，断言旧文档仍可检索（未被清空）。
- [ ] **Step 2：** 改造：写入到临时集合名（如 `rag_docs_tmp`）→ 全部成功后 `rename`/切换集合 → 再清空旧集合；或将 `clear()` 移到 embed 全部成功之后。
- [ ] **Step 3：** embed 调用移入 per-file 容错：单 chunk 失败记录并跳过，不中断整批。
- [ ] **Step 4：** 提交：`fix: 重索引先写后切，崩溃不丢旧数据`。

**Acceptance:** 原子性测试通过。

### Task 2.5：检索链路 embedding 失败降级纯 BM25

**Files:**
- Modify: `rag/retriever.py:123`
- Test: `tests/test_retriever_embed_fallback.py`（新建）

**背景：** `retrieve()` 首行 embed，失败抛异常，BM25 完全独立可用却不降级。

- [ ] **Step 1：** 写失败测试：mock embed 抛错，断言 `retrieve` 返回纯 BM25 结果（或带 `degraded=True` 标记）而不抛异常。
- [ ] **Step 2：** `retrieve` 包 try/except，embed 失败时走 sparse 分支（doc_name/tags 过滤逻辑保留），返回结果带 `"degraded"` 标志供日志/上游感知。
- [ ] **Step 3：** 提交：`fix: embedding 失败时降级纯 BM25 检索`。

**Acceptance:** 降级测试通过。

### Task 2.6：guard 对检索文档上下文做注入检测

**Files:**
- Modify: `rag/guard.py:60-69`、`rag/pipeline.py:75-78`、`rag/memory.py:177-189`
- Test: `tests/test_guard_document_injection.py`（新建）

**背景：** `check_injection` 只查 `question`，chunk 内容直接拼入 prompt，恶意文档可注入指令。

- [ ] **Step 1：** 写失败测试：文档 chunk 含"忽略之前的指令"类文本，断言 build_messages 前被拦截/标记。
- [ ] **Step 2：** pipeline rag 分支在 `build_messages` 前对 `context` 各 chunk 调 `check_injection(c.text)`；任一命中则：不注入该 chunk（标记 `excluded_by_guard`）并记录日志。
- [ ] **Step 3：** 提交：`fix: 检索上下文注入检测，恶意文档不进入 prompt`。

**Acceptance:** 注入测试通过；正常文档不受影响。

### Task 2.7：check_output 掩蔽全部命中 + 修正 `sk-` 误伤

**Files:**
- Modify: `rag/guard.py:78-88`
- Test: `tests/test_guard_output_full_masking.py`（新建）

**背景：** 命中首个即 return，多个泄漏点只处理一个；`sk-` 子串误伤 `risk-analysis` 等正常词。

- [ ] **Step 1：** 写失败测试：输出含两个 `sk-xxx`，断言两个都被掩蔽；输出含 `risk-analysis`，断言不被掩蔽。
- [ ] **Step 2：** 改 `re.sub` 为 `re.sub(pattern, replacement, text)`（`re.subn`）替换所有命中；泄漏模式改为更严格边界：`\bsk-[A-Za-z0-9_-]{10,}\b`（长度下限 + 词边界），过滤常见词误伤。
- [ ] **Step 3：** 提交：`fix: 输出审计掩蔽全部命中并收紧 sk- 模式`。

**Acceptance:** 两组测试通过。

### Task 2.8：缓存命中不跳过 memory 记录

**Files:**
- Modify: `rag/pipeline.py:133-134`
- Test: `tests/test_cache_memory_record.py`（新建）

**背景：** route=="cached" 直接 return，不 `add_message`，多轮上下文断裂。

- [ ] **Step 1：** 写失败测试：命中缓存后再问第二轮，`build_messages` 含第一轮 Q/A。
- [ ] **Step 2：** cached 分支也执行 `memory.add_message(sid, "user", question)` / `add_message(sid, "assistant", answer)`（跳过 summarize 可接受）。
- [ ] **Step 3：** 提交：`fix: 缓存命中也写入对话记忆`。

**Acceptance:** 记忆测试通过。

### Task 2.9：retry 只重试明确异常 + 退避上限

**Files:**
- Modify: `rag/resilience.py:13-32`
- Test: `tests/test_retry_specific_errors.py`（新建）

**背景：** 默认对 `Exception` 全重试，掩盖编程错误；退避无上限。

- [ ] **Step 1：** 写失败测试：抛 `KeyError` 的调用不重试立即上抛；抛重试类异常重试且最大延迟封顶。
- [ ] **Step 2：** 默认 `retryable_exceptions` 改为 `(TimeoutError, ConnectionError, OSError, openai.APIStatusError)`；`delay = min(base * 2**attempt, max_delay)`（max_delay 默认 10s）；未命中的调用点显式声明。
- [ ] **Step 3：** 提交：`fix: retry 仅重试明确异常并封顶退避`。

**Acceptance:** 测试通过；现有依赖 retry 的调用点回归全绿。

### Task 2.10：权限等级规则落地或文档对齐

**Files:**
- Modify: `rag/permissions.py`、`rag/user_db.py:901`（`get_accessible_doc_names`）或 `docs/permission-design.md`
- Test: `tests/test_permission_level.py`（新建）

**背景：** 设计文档声称"等级≥文档等级"（level 1-5），代码未实现，`user_level` 形参被忽略。

- [ ] **Step 1：** 与产品确认模型。若采用等级制：`get_accessible_doc_names` 增加 `AND user_level >= dp.permission_level` 判定，`check_doc_permission` 同步；若放弃等级制：更新 `docs/permission-design.md` 删除等级规则并注明实际为 owner/public/shared 三态。
- [ ] **Step 2：** 按选定方向补测试。
- [ ] **Step 3：** 提交：`fix/docs: 权限等级模型落地（或文档对齐实现）`。

**Acceptance:** 与设计文档一致，测试通过。

### Task 2.11：前端 dev proxy 与 `/api` 前缀契约统一

**Files:**
- Modify: `frontend/vite.config.ts:11-15`、`frontend/src/utils/api.ts`、`frontend/src/stores/*.ts`（`const API = ''`）
- Test: 前端现有 Vitest 用例全绿 + 手动 `npm run dev` 验证

**背景：** src 无任何 `/api` 前缀，proxy 只匹配 `/api` → dev 下所有请求命中 SPA fallback 返回 index.html。

- [ ] **Step 1：** 统一：`api.ts` 建 `axios.create({ baseURL: import.meta.env.VITE_API_BASE ?? '/api' })`；所有 stores 请求路径保留 `/files`、`/conversations` 等相对路径（由 baseURL 补 `/api`）。
- [ ] **Step 2：** 所有裸 `fetch` 路径同步加 `/api` 前缀（见 Task 2.12 一并处理）。
- [ ] **Step 3：** 提交：`fix: 前端统一 /api 前缀，dev proxy 生效`。

**Acceptance:** `npm run dev` 下列表正常加载（后端已在 8000 运行）。

### Task 2.12：裸 fetch 统一走 axios（或统一错误处理）

**Files:**
- Modify: `frontend/src/stores/chat.ts:165,238,259`、`frontend/src/views/FileModeView.vue:57,116`、`frontend/src/views/KnowledgeDetailView.vue:307`
- Test: `frontend/src/stores/__tests__/chat.test.ts` 扩展

**背景：** 6 处裸 fetch 绕过 axios 拦截器，401/5xx/网络错误行为不一致；`toggleVisibility` 还从 localStorage 手动拼 `Bearer null`。

- [ ] **Step 1：** 新增 `api.getStream(url, headers)` 封装流式请求（走 axios 或保留 fetch 但统一错误回调），供 chat store 流式/标题/suggest 复用。
- [ ] **Step 2：** FileModeView/KnowledgeDetailView 的 visibility/download 改用 `api` 实例调用；token 由统一拦截器注入（Task 2.14）。
- [ ] **Step 3：** 提交：`fix: 前端请求统一走拦截器，错误处理一致`。

**Acceptance:** 相关测试通过；401 时触发统一登出。

### Task 2.13：chat store 跨模式状态隔离

**Files:**
- Modify: `frontend/src/stores/chat.ts:25-32,137-172`
- Test: `frontend/src/stores/__tests__/chat.test.ts` 扩展

**背景：** 模块级单例，切换路由不重置 `selectedFile/currentConvId/messages`，KB 提问被写进文件会话。

- [ ] **Step 1：** 新增 `resetModeState(mode)` action：切换 mode 时清空 `selectedFile`、`currentConvId`、`messages`（仅当目标 mode 与当前不同时）。
- [ ] **Step 2：** `selectConversation` 失败时回滚 `selectedFile` 恢复（先成功后提交）；`_selectedFilesByConv` 在会话删除/重建时清理对应 key。
- [ ] **Step 3：** 提交：`fix: chat store 按模式隔离会话状态`。

**Acceptance:** 跨模式切换测试通过。

### Task 2.14：前端请求拦截器统一注入 token

**Files:**
- Modify: `frontend/src/utils/api.ts`（新增 request 拦截器）、`frontend/src/views/AnalyticsView.vue:20`
- Test: `frontend/src/utils/__tests__/api.test.ts` 扩展

**背景：** 无 request 拦截器，靠调用方手动传 header，`AnalyticsView` 已漏传。

- [ ] **Step 1：** `api.ts` 加 request 拦截器：从 `localStorage.getItem('rag_token')` 读取，非空则设 `Authorization: Bearer <token>`；响应拦截器保留 401 处理。
- [ ] **Step 2：** 移除调用方手动 `getAuthHeaders()` 拼接（auth.ts 保留内部逻辑但不再重复拼）。
- [ ] **Step 3：** `AnalyticsView.vue:20` 无需改（拦截器自动注入），补测试断言带 header。
- [ ] **Step 4：** 提交：`fix: axios 请求拦截器统一注入 token`。

**Acceptance:** 测试断言请求带 Authorization。

### Task 2.15：依赖锁定 + 去掉 --no-cache

**Files:**
- Modify: `requirements.txt` → 生成 `requirements.lock`（pip-compile）、`Dockerfile:35`、`deploy.sh:11`、`scripts/deploy.sh:11`

**背景：** 依赖 `>=x,<y` 区间无锁文件，`--no-cache` 全量重建 → 不可复现。

- [ ] **Step 1：** 用 `pip-compile`（pip-tools）生成 `requirements.lock`（精确版本）；CI/部署安装改 `pip install -r requirements.lock`。
- [ ] **Step 2：** `Dockerfile` 与部署脚本去掉 `--no-cache`（保留 pip 层缓存），基础镜像 tag 固定（`python:3.12-slim-bookworm` 级别，替代浮动 `3.12-slim`）。
- [ ] **Step 3：** 提交：`build: 锁定依赖版本，部署复用镜像缓存`。

**Acceptance:** 同一代码两次构建的依赖版本一致。

### Task 2.16：Dockerfile 非 root 运行

**Files:**
- Modify: `Dockerfile:12-59`、`docker-compose.yml`（卷权限）

**背景：** 无 USER 指令，容器以 root 运行并写宿主卷。

- [ ] **Step 1：** Dockerfile 末尾：`RUN useradd -m -u 10001 rag && chown -R rag:rag /app`（`/app` 内 data/qdrant_data 由 compose 初始化属主）；`USER rag`。
- [ ] **Step 2：** compose 中数据卷用命名卷或 entrypoint 里 `chown` 初始化。
- [ ] **Step 3：** 提交：`build: 容器以非特权用户运行`。

**Acceptance:** `docker compose up` 后 `docker exec ... whoami` 为 rag 且卷可写。

### Task 2.17：Redis 仅内网 + 加密码

**Files:**
- Modify: `docker-compose.yml:5-6`、`render.yaml`（如用）

**背景：** Redis 无密码映射到宿主全网卡 0.0.0.0:6379。

- [ ] **Step 1：** 删除 `ports: - "6379:6379"`（应用经 compose 内网访问即可）；若确需本机调试，改 `127.0.0.1:6379:6379`。
- [ ] **Step 2：** Redis command 加 `--requirepass ${REDIS_PASSWORD:-}`，`redis_cache.py` 的 `redis_url` 从环境变量注入密码（`redis://:pass@redis:6379`）。
- [ ] **Step 3：** 提交：`fix: Redis 不暴露公网并启用密码`。

**Acceptance:** 宿主机 6379 不再对外；应用可正常连接 Redis。

### Task 2.18：.dockerignore 补项目冗余目录

**Files:**
- Modify: `.dockerignore`

**背景：** `.worktrees/`（约 925MB 含 venv）等被 `COPY . .` 打进镜像。

- [ ] **Step 1：** `.dockerignore` 追加：`.worktrees/`、`tests/`、`docs/`、`benchmarks/`、`tmp/`、`.venv/`、`.ruff_cache/`。
- [ ] **Step 2：** 验证：`docker build` 的 `COPY . .` 步骤上下文体积显著下降（`docker build --no-cache` 前置 `tar` 打印，或 `docker buildx build --progress=plain` 观察）。
- [ ] **Step 3：** 提交：`build: dockerignore 排除 worktrees 与开发目录`。

**Acceptance:** 构建上下文不含 .worktrees。

---

## Phase 3：Medium 修复（21 任务）

### Task 3.1：pipeline 复用 FeedbackProcessor/GapAnalyzer 单例

**Files:**
- Modify: `rag/pipeline.py:107-111,161-168`
- Test: `tests/test_pipeline_reuses_connections.py`（新建）

**背景：** 每次查询新建 sqlite 连接 + 建表 DDL，进入每请求热路径；api.py 已有单例却没用。

- [ ] **Step 1：** 测试：连续 2 次查询（mock LLM/检索），断言 `sqlite3.connect` 仅调用 1 次（monkeypatch 计数）。
- [ ] **Step 2：** pipeline 构造时注入 `feedback_processor`/`gap_analyzer`（由 api.py 传入单例），默认 None 时才新建。
- [ ] **Step 3：** 提交：`perf: pipeline 复用 feedback/gap 单例连接`。

**Acceptance:** 连接次数测试通过。

### Task 3.2：RAGPipeline 复用 + 显式 close

**Files:**
- Modify: `rag/api.py:796-802,852-858,1041-1046,1097-1101`、`rag/pipeline.py:57-64`
- Test: `tests/test_pipeline_lifecycle.py`（新建）

**背景：** 每次上传/删除/索引重建 pipeline，`DialogueMemory`/`ExecutionTracker` 连接从不 close。

- [ ] **Step 1：** 测试：连续文件操作后连接数不增长（mock `sqlite3.connect` 计数）。
- [ ] **Step 2：** api.py 维护 pipeline 实例缓存（按 kb_id），文件变更只刷新对应实例的索引数据；`RAGPipeline.close()` 关闭 memory/tracker 连接，替换实例前调用。
- [ ] **Step 3：** 提交：`fix: 复用 pipeline 实例并显式关闭连接`。

**Acceptance:** 生命周期测试通过。

### Task 3.3：add_document_to_kb 文档落盘

**Files:**
- Modify: `rag/api.py:1352-1413`
- Test: `tests/test_kb_add_document_persist.py`（新建）

**背景：** 该端点写完临时文件就 unlink，`_insert_doc` 读 `DATA_DIR/filename` 恒 False，TOC/摘要/回读全空。

- [ ] **Step 1：** 测试：调用端点后断言 `DATA_DIR/upload/<filename>` 存在且内容正确。
- [ ] **Step 2：** `finally os.unlink(tmp_path)` 改为：成功后把临时文件 `shutil.move` 到 `DATA_DIR/upload/<safe_name>`，失败才清理。
- [ ] **Step 3：** 提交：`fix: add_document_to_kb 将文档持久化到上传目录`。

**Acceptance:** 落盘测试通过；TOC/摘要可读。

### Task 3.4：add_tags_to_file 批量 Qdrant 更新

**Files:**
- Modify: `rag/api.py:953-964`
- Test: `tests/test_tags_bulk_update.py`（新建）

**背景：** 逐 point retrieve + set_payload，上万 chunk 上万次 RTT，且同步跑在事件循环。

- [ ] **Step 1：** 测试：mock Qdrant 客户端，断言 `set_payload` 以批量操作调用（非逐 point）。
- [ ] **Step 2：** 改用 Qdrant `retrieve` 批量读 payload（一次传 points ids），构造批量 `set_payload`（`client.set_payload(..., points=[...], ...)` 一次提交）。
- [ ] **Step 3：** 提交：`perf: add_tags_to_file 批量更新 payload`。

**Acceptance:** 批量调用测试通过。

### Task 3.5：流式中途异常 yield error 事件

**Files:**
- Modify: `rag/api.py:1184-1207`（event_generator）、`rag/generator.py:86-92`
- Test: `tests/test_stream_error_event.py`（新建）

**背景：** 重试耗尽抛异常冒出 async 生成器，客户端永久挂起，持久化跳过。

- [ ] **Step 1：** 测试：mock 生成抛错，断言 SSE 流收到 `{"type":"error",...}` 且随后正常结束。
- [ ] **Step 2：** event_generator 内 `try/except` 包裹 `async for`，捕获后 `yield data: {"type":"error","reason":...}` 再 return。
- [ ] **Step 3：** 提交：`fix: 流式异常补发 error 事件`。

**Acceptance:** 错误事件测试通过。

### Task 3.6：add_to_collection 幂等去重

**Files:**
- Modify: `rag/vector_store.py:96-115`、`rag/knowledge_base.py`（add_document）
- Test: `tests/test_vector_store_idempotent.py`（新建）

**背景：** 重复导入同名文档产生重复向量。

- [ ] **Step 1：** 测试：两次 add 同名文档，search 不出现重复 chunk。
- [ ] **Step 2：** add 前先按 `doc_name` 删除既有 points 再写入；或 upsert 以 `(doc_name, chunk_index)` 为稳定 ID。
- [ ] **Step 3：** 提交：`fix: 向量写入按 doc_name 幂等`。

**Acceptance:** 幂等测试通过。

### Task 3.7：默认集合路径 dense 过滤对齐 sparse

**Files:**
- Modify: `rag/retriever.py:124-127`
- Test: `tests/test_retriever_default_collection_filter.py`（新建）

**背景：** collection_name 为空时 dense 不带 doc_name/tags 过滤，sparse 端过滤 → 权限范围失效。

- [ ] **Step 1：** 测试：无 collection_name 时指定 doc_name，dense 结果只含该文档。
- [ ] **Step 2：** dense 分支同样应用 `doc_name`/`tags` 过滤（构造 Qdrant filter 或对 dense 结果二次按 doc_name 过滤）。
- [ ] **Step 3：** 提交：`fix: 默认集合 dense 检索对齐 doc_name/tags 过滤`。

**Acceptance:** 过滤测试通过。

### Task 3.8：RRF 融合权限字段一致性

**Files:**
- Modify: `rag/retriever.py:146-163`、`rag/vector_store.py:71-80,142-151`
- Test: `tests/test_rrf_dedup.py`（新建）

**背景：** dense/sparse 重建的 Chunk 的 `doc_permission_id` 不同 → 同一文本两个 key 各计分产生重复。

- [ ] **Step 1：** 测试：带权限字段的文档检索结果无重复 chunk。
- [ ] **Step 2：** RRF 融合时以 `(doc_name, chunk_index, text)` 为归一化 key（忽略权限字段），scores 去重后再恢复权限字段。
- [ ] **Step 3：** 提交：`fix: RRF 融合按内容去重，避免重复 chunk`。

**Acceptance:** 去重测试通过。

### Task 3.9：反馈 weights 接线或删除死代码

**Files:**
- Modify: `rag/pipeline.py:93,102-111`、`rag/retriever.py:122,144`、`rag/retriever.py:153-161`
- Test: `tests/test_feedback_weights_wired.py`（新建）

**背景：** pipeline 在 rerank 后算 weights，但 `retrieve` 从未收到；`_rrf_fuse` 的 weights 分支永不执行。

- [ ] **Step 1：** 测试：有反馈权重的 chunk 排序受影响。
- [ ] **Step 2：** 选择接线：`retrieve` 增加 `weights` 参数，pipeline 在 rerank 后二次融合权重（或把权重提前到 retrieve 前用缓存）。或选择删除：移除 md5 计算与 weights 分支，保留接口兼容。
- [ ] **Step 3：** 提交：`feat/fix: 反馈权重生效（或移除死代码）`。

**Acceptance:** 权重测试通过；无未接线死代码。

### Task 3.10：DBSource SQL 白名单 + SSRF 防护 + 阻塞 IO 移线程

**Files:**
- Modify: `rag/data_sources/db_source.py`、`rag/data_sources/api_source.py:89-113`、`rag/data_sources/rss_source.py:24-52`、`rag/data_sources/base.py`
- Test: `tests/test_data_source_ssrf_sql.py`（新建）

**背景：** test_connection 执行任意 SQL；APISource/RSS 抓任意 URL（SSRF），async 内做阻塞 IO。

- [ ] **Step 1：** 测试：连接串指向内网 IP（127.0.0.1/169.254.169.254）被拒；sync 只读 SQL 被白名单放行、非只读被拒。
- [ ] **Step 2：** 新增 URL 校验工具：拒绝非 http/https、拒绝内网/链路本地地址（IP 解析后校验）、拒绝私有网段。
- [ ] **Step 3：** `async def fetch` 内改为 `await asyncio.to_thread(requests.get, url, timeout=5)`；test_connection 只 `SELECT 1`。
- [ ] **Step 4：** 提交：`fix: 数据源防 SSRF、SQL 白名单、异步不阻塞`。

**Acceptance:** SSRF/SQL 测试通过。

### Task 3.11：文件大小上限 + 超大 chunk 截断

**Files:**
- Modify: `rag/loader.py:25-28,52-74`、`rag/batch_importer.py:89-98`、`config.py`（新增 `max_upload_bytes`）
- Test: `tests/test_loader_size_limit.py`（新建）

**背景：** 无大小上限整文件读入内存；超大表格单 chunk 超 embedding token 上限。

- [ ] **Step 1：** 测试：超限文件上传被 413/拒；超大文本 chunk 被截断到安全长度。
- [ ] **Step 2：** loader 对 `f.read()` 改为 `f.read(max_bytes)` 或流式读并检测超限；config 加 `max_upload_bytes`（默认如 50MB）。
- [ ] **Step 3：** 表格解析产出的单 chunk 按 `max_chunk_chars` 截断（如 8000 字符）并记录。
- [ ] **Step 4：** 提交：`fix: 文件大小上限与 chunk 截断`。

**Acceptance:** 上限测试通过。

### Task 3.12：chunk 保留来源元数据

**Files:**
- Modify: `rag/cleaner.py:84-103`、`rag/pipeline.py:43`、`rag/chunker.py`、`rag/vector_store.py:106-111`
- Test: `tests/test_chunk_metadata.py`（新建）

**背景：** title/author/date 元数据提取后丢弃，chunk 无法按来源过滤/溯源。

- [ ] **Step 1：** 测试：clean 返回的 title/author/date 出现在最终 chunk payload 中。
- [ ] **Step 2：** pipeline 不再丢弃 `_metadata`，透传给 chunker；`Chunk` 增加 `metadata` 字段，vector_store payload 落 `metadata`。
- [ ] **Step 3：** 提交：`feat: chunk 保留来源元数据`。

**Acceptance:** 元数据测试通过。

### Task 3.13：generate_stream 传 temperature + 熔断及时记录

**Files:**
- Modify: `rag/generator.py:49-92`
- Test: `tests/test_generator_stream_params.py`（新建）

**背景：** 流式不传 temperature 与同步不一致；熔断只在全部重试耗尽后记录。

- [ ] **Step 1：** 测试：流式调用传入 temperature；超时类异常时熔断器 `record_failure` 被及时调用。
- [ ] **Step 2：** 流式分支补 `temperature` 参数（与同步一致）；`@retry` 每轮失败即 `circuit.record_failure()`，成功时 `record_success()`。
- [ ] **Step 3：** 提交：`fix: 流式参数与同步对齐，熔断及时反馈`。

**Acceptance:** 参数/熔断测试通过。

### Task 3.14：verify_api_key 路径检查 password_changed_at

**Files:**
- Modify: `rag/auth.py:75-88,110`、`rag/api.py:406-408`
- Test: `tests/test_auth_password_change_invalidates.py`（新建）

**背景：** token 失效逻辑只在 `_get_current_user`，仅 `Security(verify_api_key)` 的端点改密后旧 token 仍有效。

- [ ] **Step 1：** 测试：改密后旧 token 调 `/sources` 被拒。
- [ ] **Step 2：** `verify_api_key` 的 JWT 分支内传入 `password_changed_at`（查库）并校验 `iat < password_changed_at` 则拒。
- [ ] **Step 3：** 提交：`fix: 改密后所有端点旧 token 失效`。

**Acceptance:** 失效测试通过。

### Task 3.15：RAG_AUTH_KEYS 文档与代码方向统一

**Files:**
- Modify: `.env.example:23` 或 `rag/auth.py:104-106`
- Test: `tests/test_auth_keys_parsing.py`（新建）

**背景：** 文档示例 `{"key":"user"}` 与代码解析 `{"user":"key"}` 相反。

- [ ] **Step 1：** 测试：按 `.env.example` 填写的配置能正确鉴权。
- [ ] **Step 2：** 统一为 `user_id → api_key`，修正 `.env.example` 示例与注释。
- [ ] **Step 3：** 提交：`fix: RAG_AUTH_KEYS 解析方向与文档一致`。

**Acceptance:** 解析测试通过。

### Task 3.16：memory 摘要行选择修正

**Files:**
- Modify: `rag/memory.py:137-140`
- Test: `tests/test_memory_summary_boundary.py`（新建）

**背景：** LIMIT 按"每轮=2 条消息"×2 假设选行，agent 多消息轮次错位、重复摘要、DB 无限增长。

- [ ] **Step 1：** 测试：一轮含多条 assistant 消息时摘要不重叠、`last_summarized_count` 正确。
- [ ] **Step 2：** 改为按 user 消息对数定位起止行（`LIMIT` 用 `new_to_summarize` 对应的实际消息 id 边界），或在 summary 里记录 `summarized_upto_id` 精确游标。
- [ ] **Step 3：** 考虑旧消息清理：超过保留窗口的已摘要消息删除（若测试 `test_memory.py` 固化 24 条留存，先确认语义再改）。
- [ ] **Step 4：** 提交：`fix: memory 摘要游标精确化`。

**Acceptance:** 边界测试通过。

### Task 3.17：Redis miss 查 fallback + 冷却

**Files:**
- Modify: `rag/redis_cache.py:30-44,55-65`
- Test: `tests/test_redis_cache_fallback_cooldown.py`（新建）

**背景：** Redis 健康但 miss 时不回读内存 fallback（降级数据丢失）；宕机期间每次调用刷日志无冷却。

- [ ] **Step 1：** 测试：Redis set 失败写 fallback 后，Redis 恢复但 miss 时 `get` 返回 fallback 值。
- [ ] **Step 2：** `get` 在 redis miss 时 `return self._fallback_get(key)`（与异常分支一致）。
- [ ] **Step 3：** `_get_redis` 失败后进入冷却（如 30s 内不再尝试 ping），冷却期直接走 fallback；日志降频（冷却期间只打一次）。
- [ ] **Step 4：** 提交：`fix: Redis 降级缓存回读与连接冷却`。

**Acceptance:** fallback/冷却测试通过。

### Task 3.18：数据库迁移机制

**Files:**
- Create: `rag/db_migrations.py`、`rag/migrations/`（含版本 001 基线）
- Modify: `rag/user_db.py:44-88`（建表入口）、`start.py`（启动调用）
- Test: `tests/test_db_migrations.py`（新建）

**背景：** 全 `CREATE TABLE IF NOT EXISTS`，升级加列会静默 "no such column"。

- [ ] **Step 1：** 测试：迁移器对旧库执行新增列的 002 迁移成功且幂等。
- [ ] **Step 2：** 实现轻量迁移器：`user_version` PRAGMA 记录版本；按版本数组顺序执行迁移脚本；启动时自动跑。
- [ ] **Step 3：** 把 Task 1.4 的 `owner_id` 加列改由 002 迁移承载（验证迁移链路）。
- [ ] **Step 4：** 提交：`feat: SQLite 版本化迁移`。

**Acceptance:** 迁移测试通过；启动幂等。

### Task 3.19：CI 门禁 + 部署后健康验证 + 固定 action SHA

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `.github/workflows/deploy.yml:12,17-22`

**背景：** 只有 deploy.yml，无 CI；action 用可变 `@v1`；部署后无健康验证/回滚。

- [ ] **Step 1：** 新建 `ci.yml`：`ruff check .` + `pytest tests/ -q` + 前端 `npm ci && npm run test`（可在本机先验证命令可跑通，CI 仓库配置后续由用户 push 后生效）。
- [ ] **Step 2：** deploy.yml 改为 `needs: [ci]`；action 固定 commit SHA（`appleboy/ssh-action@<sha>`）。
- [ ] **Step 3：** 部署后轮询 `/health`（超时 60s），失败则记录并回滚上一镜像（`docker compose pull <prev>` 或标注人工介入）。
- [ ] **Step 4：** 提交：`ci: 增加 lint/test 门禁与部署健康验证`。

**Acceptance:** 工作流文件语法正确（`actionlint` 或人工检查）；本机对应命令全绿。

### Task 3.20：ResultCache 死代码移除或接线

**Files:**
- Modify: `rag/resilience.py:109-159`、`rag/pipeline.py:65`、`tests/test_resilience.py:105-209`
- Test: 迁移对应测试

**背景：** pipeline 用 RedisCache，ResultCache 未使用，测试却大量断言它（错位）。

- [ ] **Step 1：** 选择移除：删除 ResultCache 类与 `_access_count`；把 `test_resilience.py` 中 ResultCache 相关测试改为针对 RedisCache 的集成/降级断言，或删除。
- [ ] **Step 2：** 提交：`refactor: 移除未使用的 ResultCache 死代码`。

**Acceptance:** 测试套件无对死代码的断言；全绿。

### Task 3.21：add_document_to_kb / 其他每请求建连排查收口（含 GapAnalyzer 单例）

**Files:**
- Modify: `rag/pipeline.py:166`（GapAnalyzer 复用）、测试补充
- 说明：与 Task 3.1 同源，合并交付。

**Acceptance:** 与 3.1 相同。

---

## Phase 4：Low 修复（分组任务，14 项并入 6 个任务）

### Task 4.1：后端低危健壮性修复（PBKDF2 迭代 + JWT 密钥权限 + SQLite busy_timeout + health 连接泄漏）

**Files:**
- Modify: `rag/auth.py:31-33,49,60`、`rag/user_db.py:30`、`rag/api.py:326-337`
- Test: `tests/test_auth_params.py`（新建）

- [ ] **Step 1：** PBKDF2 迭代 260000 → 600000（兼容旧哈希：校验时先试 600k 再试 260k）。
- [ ] **Step 2：** JWT 密钥写入后 `os.chmod(secret_file, 0o600)`。
- [ ] **Step 3：** `user_db.py` 连接加 `timeout=30` + `PRAGMA busy_timeout=30000`。
- [ ] **Step 4：** `health()` 的 sqlite 连接包 try/finally。
- [ ] **Step 5：** 提交：`fix: 认证参数与连接健壮性`。

**Acceptance:** 参数测试通过；health 异常不泄漏连接。

### Task 4.2：反馈接口校验 + 注册防枚举

**Files:**
- Modify: `rag/user_db.py:417,603`、`rag/api.py:352`
- Test: `tests/test_feedback_validation.py`（新建）

- [ ] **Step 1：** `add_feedback` 前校验 `message_belongs_to_user` 且 `value in {-1,0,1}`（API 层白名单）。
- [ ] **Step 2：** `/register` 已存在用户返回统一文案（与登录一致）。
- [ ] **Step 3：** 提交：`fix: 反馈值域校验与注册防枚举`。

**Acceptance:** 校验测试通过。

### Task 4.3：评估/压测脚本修复

**Files:**
- Modify: `scripts/run_eval.py:165,51-54`、`scripts/run_stress_test.sh:34-42`
- Test: `scripts/run_stress_test.sh` 在 clean 环境可启动

- [ ] **Step 1：** `run_stress_test.sh` 开头 `mkdir -p benchmarks`。
- [ ] **Step 2：** `run_eval.py` 密码改从环境变量 `RAG_ADMIN_PASSWORD` 读取（无则提示）；Hit Rate 判定加否定/口径说明或改精确匹配。
- [ ] **Step 3：** 提交：`fix: 评估/压测脚本健壮性`。

**Acceptance:** 脚本可运行。

### Task 4.4：前端 dead code 与构建保护

**Files:**
- Modify: `frontend/vite.config.ts:18-21`、`frontend/src/router/index.ts`、`frontend/src/views/ChatView.vue/AnalyticsView.vue/FilesView.vue`、`frontend/src/utils/api.ts:37`、`frontend/src/stores/chat.ts:39,42-44`
- Test: 前端测试全绿

- [ ] **Step 1：** `vite.config.ts` 改 `emptyOutDir: false` 或 `outDir` 指向独立 `dist/`（保留 static 手工文件）。
- [ ] **Step 2：** 与产品确认：三个未注册视图是删除还是注册路由；`AnalyticsView` 若保留需修复缺鉴权（Task 2.14 已覆盖）。删除死代码或补路由。
- [ ] **Step 3：** 清理 `withRetry`/`currentMode`/`conversationsByMode` 未使用导出（`currentMode` 恒 'file' 改为按 mode 计算）。
- [ ] **Step 4：** 提交：`refactor: 前端清理死代码与构建保护`。

**Acceptance:** 前端测试全绿；构建不误删 static 资源。

### Task 4.5：KnowledgeDetailView 响应路由参数

**Files:**
- Modify: `frontend/src/views/KnowledgeDetailView.vue:30,76-83`
- Test: `frontend/src/views/__tests__/KnowledgeDetailView.test.ts`（新建）

- [ ] **Step 1：** 加 `watch(() => route.params.id, load)`，同一组件实例路由切换时重新加载。
- [ ] **Step 2：** 提交：`fix: 知识库详情响应路由参数变化`。

**Acceptance:** 路由切换测试通过。

### Task 4.6：测试质量修复与关键路径补测

**Files:**
- Modify: `tests/test_concurrency.py:78-90`、`tests/test_feedback_processor.py:67-79`、`tests/test_code_review_fixes.py:26-30`
- Modify: `tests/test_guard.py`、`tests/test_tools.py`、`tests/test_redis_cache.py`、`tests/test_memory.py`（补关键路径）
- Test: 上述测试修正后语义正确

- [ ] **Step 1：** `test_concurrency.py`：`test_vector_store_uses_lock_for_writes` 改为断言写路径确实持锁（monkeypatch `acquire`/检查锁状态）。
- [ ] **Step 2：** `test_feedback_processor.py`：`test_feedback_processor_dedup_user` 断言去重后权重为 0.9（未去重 0.8 场景单独断言）。
- [ ] **Step 3：** `test_code_review_fixes.py`：`status_code` 改为精确断言（200/预期码），不允许 500 通过。
- [ ] **Step 4：** 补测：`guard` 文档上下文注入、`check_output` 多命中、`RedisCache` 跨库键隔离与 fallback、`calculate` 溢出（Task 1.5 已含）、memory 多消息轮次摘要。
- [ ] **Step 5：** 提交：`test: 修正无效断言并补关键路径测试`。

**Acceptance:** 修正后的测试语义真实；关键路径有回归保护。

---

## Self-Review（writing-plans 自审）

**1. Spec（审查报告）覆盖核对** —— 已逐项映射：Critical 8 项 → Phase 1 Task 1.1-1.8；High 18 项 → Phase 2 Task 2.1-2.18；Medium 21 项 → Phase 3；Low 14 项 → Phase 4 分组（4.1-4.6）；git 卫生 → Phase 0。审查报告中列出的每一项均有对应任务，无遗漏。

**2. Placeholder 扫描** —— 所有任务含具体文件、改动点、测试与提交信息；无 "TBD/TODO/添加适当错误处理" 类占位。Task 2.10 / 3.9 / 4.4 含"二选一"决策点，均已给出两种方向的落点与判据（属产品决策，不是占位）。

**3. 一致性检查** —— `_key(question, kb_id, user_id)` 在 1.1 定义，pipeline 调用点与其签名一致；`verify_api_key` 改动（3.14）与 auth 结构一致；`filter_chunks_by_permission` 在 1.1/1.2/1.6 反复引用，签名统一为 `(user_db, kb_id, chunks, user)`；Task 3.18 迁移器承载 1.4 的加列，顺序依赖标注。

---

## Execution Handoff

计划已保存至 `docs/superpowers/plans/2026-08-07-review-fixes-complete.md`。两种执行方式：

**1. Subagent-Driven（推荐）** —— 每个任务派发全新 subagent（superpowers:subagent-driven-development），任务间我做两阶段审查，迭代快。

**2. Inline Execution** —— 本会话用 superpowers:executing-plans 按批次执行，检查点处暂停审查。

选择哪种方式？另注：superpowers 技能已安装到 `~/.claude/skills/`，但需**重启 Claude Code 会话**后才能在技能列表中启用（届时可 `/brainstorming`、`/writing-plans` 直接调用）。
