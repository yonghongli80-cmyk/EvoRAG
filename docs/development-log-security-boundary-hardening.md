# Security Boundary Hardening — 开发日志

工作分支：`codex/security-boundary-hardening`  
工作目录：`RAGv3/.worktrees/security-boundary-hardening`

## 本次会话记录（2026-08-06 晚，第二次会话）

### 前辈会话交接的内容（接手时已知）

接手时通过 `docs/handoff-2026-08-06.md` 获知：

- **计划 1 可信知识闭环已 10/10 完成**（提交 `c833c9e`–`5cc213e`）：文档版本 Schema、源中立 `SourceObject`、统一版本化摄取、版本感知向量、引用持久化、结构化反馈、知识任务、前端交互/运营页、存量迁移。
- **计划 2 已完成 T1–T4**（至 `46f99d9`）：统一格式注册表 + 材料 Schema；流式暂存 + 持久去抖任务；统一提取 + 本地 PDF OCR + PPTX；隔离旧 Office 转换。
- 环境限制：本机无模型 API 凭据（`test_pipeline.py` 因 `Missing credentials` 失败）；`.pytest_cache` 跨账户权限问题需 `-p no:cacheprovider`；前端 4 个既有 Vitest 失败；Ruff 仅保留 `B008`；测试用 `mock_embedding_indexing` 夹具。
- 8 条架构红线（单平台 / `current_version_id` 权威 / 答案缓存禁用 / 权限只收窄 / 置信度不给百分比 / 工资考勤禁启 / 路径凭据不进响应 / TDD 纪律）。

### 本次会话所做（T5–T11 + 全项目安全审查）

逐任务 TDD（先失败测试 → 实现 → 全绿 → 提交），提交与验证见上方「增量资料生命周期」表格：

- **T5** 安全压缩包清单/确认/子版本原子差异（`archives.py`，15 测试 + 47 回归通过）。
- **T6** 增量单元指纹 + 有界嵌入缓存 + 原子 worker（5 测试 + 54 回归通过）。
- **T7** 墓碑删除/30 天恢复/幂等清除（5 测试 + 19 回归通过）。
- **T8** ACL 优先定位 + 授权下载（4 测试 + 16 回归通过）。
- **T9** 类型化整理 + `generate_structured`（5 测试 + 40 回归通过）。
- **T10** 材料 API 路由 + 4 个前端组件 + store/视图接线（5 后端测试 + 4 前端组件测试，构建通过）。
- **T11** 元数据迁移（启动接入，绝不重嵌入）+ `MaterialWorker` 启停 + 全量验证：**后端 708 通过 0 失败**、前端 132 通过 / 4 既有失败、构建通过。
- 更新 `docs/handoff-2026-08-06.md`（计划 2 → 11/11）并新建 `docs/handoff-2026-08-06-session2.md` 作为下一会话入口。
- **安全审查**：确认写入路径全部正确（压缩包路径/注入/命令/ eval / XSS 均防护到位）；发现 3 个**未修**的读取端点授权缺口（`GET /materials/recycle`、`GET /materials/archives/{id}`、`GET /materials/jobs/{id}`），详见交接文档第 2 节——**下一会话优先修复**。

### 关键踩坑记录（供后续参考）

- `UserDB._lock` 非重入：事务块内调用 `db.xxx()` 会死锁，只能用 `db._conn.execute()`；本会话因此踩了 3 次。
- `zipfile` 写加密 zip 不支持（标志恒 0）；`BEGIN IMMEDIATE` + `metadata_version` CAS 用于乐观并发。
- `tests/test_folder_indexer.py` 断言随统一注册表更新（csv 已支持索引）。

## 增量资料生命周期（计划 2）— 已完成 11/11

2026-08-06 完成剩余 T5–T11（接 T1–T4），逐任务 TDD（先写失败测试 → 实现 → 全绿 → 提交）：

| 提交 | 任务 | 内容 | 验证 |
| --- | --- | --- | --- |
| `9d13a8b` | T5 | 安全压缩包清单/确认/子版本原子差异：`archives.py`，拒绝路径逃逸/超比/超层/加密，`BEGIN IMMEDIATE` 内切换容器+子版本指针，幂等结果 | 15 项 `test_archive_import` + 47 项回归通过 |
| `92702a3` | T6 | 增量单元指纹 + 有界嵌入缓存（`embedding_cache.sqlite3`）+ 原子 worker（stale 取消、SQLite 失败仅删新点、FTS+指纹同事务激活） | 5 项 `test_incremental_material_index` + 54 项回归通过 |
| `1cd295b` | T7 | 墓碑删除/30 天恢复/幂等清除：`recycle.py`，所有读路径同步拒绝已删文档，引用审计保留无预览 | 5 项 `test_material_recycle` + 19 项回归通过 |
| `21de5ab` | T8 | ACL 优先定位 + 授权下载：`locator.py`，不可见文件名永不进模型输入，下载 URL 由后端生成且每次重查 ACL | 4 项 `test_material_locator` + 16 项回归通过 |
| `9985975` | T9 | 类型化整理预览/确认/回滚：`organizer.py` + `generate_structured`（JSON-only、单对象解析、绝不用 eval），CAS + 幂等 + 回滚不覆盖手动改动 | 5 项 `test_material_organizer` + 40 项回归通过 |
| `2bf9b82` | T10 | 材料 API 路由（upload 202 / archives / recycle / locate / organization）+ 4 个前端组件 + store/视图接线 | 5 项 `test_material_api` + 4 个前端组件测试通过，生产构建通过 |

**验证基线（T11 Step 4）**：
- 后端全量（`--ignore=tests/test_pipeline.py`）：**708 通过，0 失败**（接管基线 683 + 新增 ~25）。
- 前端 Vitest：**132 通过 / 4 失败**（4 项均为既有 store 断言不一致，未新增）；`npm run build` 成功。
- Ruff：新增文件仅剩与既有风格一致的 `B008`（FastAPI `Depends`）；`tests/test_folder_indexer.py` 因统一注册表将 csv 视为可索引格式而更新断言。
- `git diff --check` 干净；`data/` 与 `qdrant_data/` 无测试残留。
- 迁移 `migrate_material_metadata` 已接入启动流程（只回填 FTS 元数据，绝不重嵌入）。

## 已完成

| 提交 | 内容 | 验证 |
| --- | --- | --- |
| `512be22` | 统一解析 JWT / API Key 身份；API Key 必须映射到真实用户 | 37 项认证与用户库测试通过 |
| `5918a37` | 数据源默认关闭、仅管理员可管理、配置响应脱敏 | 8 项数据源与认证测试通过 |
| `c4cfcee` | 上传/导入认证、同名文件保护、失败补偿、移除 `/data/upload` 静态暴露 | 19 项后端测试、2 项前端测试、前端构建通过 |
| `8cab15c` | 知识库 ACL：列表、详情、查询、删除遵循所有者/管理员/明确共享规则 | 74 项知识库相关测试通过 |
| `68a0edf` | 文档 ACL：历史无权限记录默认拒绝、保护文件不可改、共享 edit 权限持久化 | 34 项文档/权限/上传/知识库测试通过 |
| `3c0609a` | 移除按“问题文本”缓存最终答案的逻辑，避免跨用户泄漏 | 专门缓存安全测试通过 |
| `89cd0b2` | 知识库源文件隔离路径、暂存写入与原子落盘基础模块 | 3 项路径安全测试通过 |
| `c863551` | 上传、单文件删除和整库删除接入持久化源文件、状态记录与失败补偿 | 15 项生命周期/知识库/文档 ACL 测试通过 |
| `0f8a907` | 流式聊天绑定显式 `kb_id` 并在后端先做 KB ACL 验证 | 2 项后端测试、1 项前端 store 测试、前端构建通过 |
| `cd124a6` | KB 详情响应移除绝对 `file_path` 和内部错误字段 | 14 项生命周期/流式/ACL 测试通过 |

## 2026-07-27：缺陷修复轮次

- Agent 改为在首次 `run()` 时创建模型客户端；未配置模型密钥时，服务启动、离线维护和测试不会被提前阻断。
- 默认 `/query` 统一使用严格身份解析；配置中的 API Key 必须映射到真实数据库用户，权限过滤不再在 API Key 路径中退化为匿名。
- 知识库概述、文档目录和摘要写入收口到 `UserDB` 的加锁连接，避免多连接 SQLite 写入导致 `database is locked`；文档级编辑同时遵循受保护文档规则。
- 修正 GBK 短文本被低置信度探测为泰文的乱码问题；RSS 数据源补充声明并安装 `feedparser` 依赖。
- Redis 不可用时增加连接重试间隔，内存降级缓存不会因重复探测耗尽 TTL。
- 删除默认库文件后会重新构建内存流水线；新增回归测试覆盖此前被吞掉的 `NameError`。
- 回归验证以两段执行：461 项 + 149 项，共 610 项 Python 测试通过。FastAPI/Starlette 的弃用警告仍存在，但没有测试失败。

## 可信知识闭环（2026-07-27 计划，实施于 2026-08-06）

| 提交 | 内容 | 验证 |
| --- | --- | --- |
| `c833c9e` | 文档版本 Schema：`document_versions`、`answer_citations` 表与原子激活 | Schema/用户库/生命周期测试通过 |
| `234f19c` | 源中立 `SourceObject` 与版本文件存储（stage/promote/resolve） | 6 项存储测试通过 |
| `9c86cee` | 统一版本化摄取服务与重复契约（reject/overwrite/keep + unchanged） | 重复/覆盖/补偿测试通过 |
| `1a42653` | 版本感知向量载荷与当前优先检索、历史回退（权重 0.35） | 检索/向量回归通过 |
| `14feddc` | 助手消息引用持久化与 `done.assistant_message_id`、版本下载 | 引用/会话/流式测试通过 |
| `d27c454` | 结构化反馈（useful/not_useful/outdated）与确定性聚合键 | 反馈/聚合测试通过 |
| `69222cb` | 知识任务规则（outdated 立即、not_useful 需 3 用户/30 天）、状态机、运营 API | 任务/运营/可见性测试通过 |
| `4176d90` | 前端重复/引用/反馈弹窗、done 事件消息 ID | Vitest + 构建通过 |
| `6c398d9` | 知识运营页与第三个导航入口 `/operations` | Vitest + 构建通过 |

全量验证（排除需模型凭证的 `test_pipeline.py`）：**639 项 Python 测试通过，0 失败**。
前端 Vitest：**122 通过 / 4 失败**（4 项均为既有 store 响应字段断言不一致，与本次改动无关），生产构建通过。
Ruff：新增文件仅剩与既有 `rag/api.py` 一致的 `B008`（FastAPI `Depends` 默认参数）风格告警。
`git diff --check` 干净，`data/` 与 `qdrant_data/` 无测试残留。

明确推迟：NAS/SMB/WebDAV 连接器、组织权限继承、离职交接、外部通知、场景助手。最终答案缓存保持禁用。

## 正在进行

- 下一计划：增量资料生命周期（2026-07-28-incremental-material-lifecycle）。

## 回家后可执行的验证

在 PowerShell 中执行：

```powershell
$project = 'C:\Users\Administrator\Desktop\RAGv3\RAGv3\.worktrees\security-boundary-hardening'
Set-Location $project

& .\.venv\Scripts\python.exe -m pytest `
  tests\test_api_security.py `
  tests\test_data_sources_security.py `
  tests\test_upload_security.py `
  tests\test_kb_security_boundaries.py `
  tests\test_document_security_boundaries.py `
  tests\test_kb_file_lifecycle.py `
  tests\test_stream_kb_target.py `
  tests\test_pipeline_cache_security.py -v

Set-Location "$project\frontend"
& .\node_modules\.bin\vitest.cmd run src/api/__tests__/files.test.ts
& npm.cmd run build
```

## 已知环境限制

- 完整 `tests/test_pipeline.py` 会因本机没有配置模型 API 凭据而失败，错误为 `Missing credentials`。这不是上述安全修复造成的失败。
- FastAPI 的 `@app.on_event("startup")` 与 Starlette TestClient 会显示弃用警告；目前不影响测试结果。
- 全量 Python 测试在 60 秒内未完成，期间出现多项既有失败；全量 Ruff 还会扫描 `.claude/` 的外部技能文件，且现有项目已有大量格式/规则问题。前端完整 Vitest 为 110 通过、4 失败，其中 4 项均是既有测试期望与当前 store 响应字段/认证参数不一致；本次新增的 KB ID 测试通过。
