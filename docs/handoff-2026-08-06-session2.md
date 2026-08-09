# RAGv3 交接文档 — 第二次会话（2026-08-06 晚）

> 本文是**下一次会话的第一入口**（在 `docs/handoff-2026-08-06.md` 之后，反映本会话收尾状态）。
> 阅读顺序：本文 → `docs/handoff-2026-08-06.md` → 企业平台总执行索引 → 当前计划。

---

## 1. 本次会话做了什么（2026-08-06 晚）

接手时计划 2（增量资料生命周期）已完成 T1–T4（提交至 `46f99d9`）。本会话按 TDD 完成 **T5–T11**，并做了全项目安全审查。

| 提交 | 任务 | 内容 |
| --- | --- | --- |
| `9d13a8b` | T5 | 安全压缩包清单/确认/子版本原子差异（`archives.py`）：拒绝路径逃逸/超比/超层/加密/符号链接，`BEGIN IMMEDIATE` 内原子切换容器+子版本指针，幂等结果表 |
| `92702a3` | T6 | 增量单元指纹（`incremental.py`）+ 有界嵌入缓存（`embedding_cache.py`）+ 原子 worker（`worker.py`：stale 取消、SQLite 失败仅删新点、FTS+指纹同事务激活、post-commit 修 `is_current`） |
| `1cd295b` | T7 | 墓碑删除/30 天恢复/幂等清除（`recycle.py` + `material_audit_log`）：所有读路径同步拒绝已删文档，引用审计保留无预览 |
| `21de5ab` | T8 | ACL 优先定位 + 授权下载（`locator.py`）：可见性先行、不可见文件名永不进模型输入、下载 URL 后端生成且每次重查 ACL |
| `9985975` | T9 | 类型化整理预览/确认/回滚（`organizer.py`）+ `generate_structured`（JSON-only、单对象解析、绝不用 eval） |
| `2bf9b82` | T10 | 材料 API 路由（`api.py`：upload 202 / archives / recycle / locate / organization）+ 4 个前端组件 + store/视图接线 |
| `441a0fd` | T11 | 迁移 `migrate_material_metadata`（只回填 FTS 元数据、绝不重嵌入）+ `MaterialWorker` 启停 + 全量验证 + 文档 |
| `…` | 收尾 | `chore: drop unused import in legacy office converter`（ruff 清理） |

**验证基线**：
- 后端全量（`--ignore=tests/test_pipeline.py`）：**708 通过，0 失败**（接管基线 683）。
- 前端 Vitest：**132 通过 / 4 既有失败**（chat 2 + files 2，均为既有 store 断言不一致）；`npm run build` 成功。
- Ruff：新增文件仅剩与既有风格一致的 `B008`；`tests/test_folder_indexer.py` 因统一注册表将 csv 视为可索引格式而更新断言（T1 遗留的不一致）。
- `git diff --check` 干净；`data/` 与 `qdrant_data/` 无测试残留。

---

## 2. ⚠️ 待办：安全审查发现的 3 个授权缺口（未修）

本次审查确认写入路径全部正确，但材料 API 的 3 个**读取**端点缺归属校验，建议下一会话优先修复（各 3–5 行）：

1. **MEDIUM — `GET /materials/recycle`**（`api.py:349` → `user_db.list_recycled_documents`）：返回**全表**已删文档，任何登录用户可见所有人的回收站（文件名、删除者、恢复期限）。修法：加 `user_id` 过滤（owner 或 deleter，admin 全量）。
2. **MEDIUM — `GET /materials/archives/{manifest_id}`**（`api.py:257`）：可读任意用户的压缩包文件清单。修法：读取前复用 `confirm` 的容器所有权校验，失败返回 404。
3. **LOW — `GET /materials/jobs/{job_id}`**（`api.py:241`）：可枚举任意任务状态（document/version 存在性泄露）。修法：响应前校验请求者对 `document_id` 具备 `require_doc_view`。

---

## 3. 仓库当前状态

- 分支：`codex/security-boundary-hardening`；HEAD：`441a0fd`（+1 chore 提交）。
- 工作区：**干净**。
- 相对 `main`：领先 40+ 提交（计划 1 十 10 提交、计划 2 11 提交、文档若干）。

## 4. 后续剩余计划（执行索引 9 步）

| # | 计划 | 状态 |
| --- | --- | --- |
| 1 | 可信知识闭环 | ✅ 10/10 |
| 2 | 增量资料生命周期 | ✅ 11/11 |
| 3 | **回答可靠性**（`2026-07-27-answer-reliability.md`） | 🔜 **下一步**：证据不足拒答/澄清、置信度（仅 high/medium/low/unavailable）、冲突检测、LlamaIndex 窄适配器；新依赖 `llama-index-core>=0.14.23,<0.15`；新建 `rag/reliability/` |
| 4 | 表格知识（spreadsheet-knowledge，8 任务） | 未开始；依赖计划 1 + 计划 3 T1–3 |
| 5 | 客户语音知识治理（8 任务） | 未开始；⚠️ 声明前置企业治理 T1–4，与索引顺序矛盾（见 handoff 第 3 节决定） |
| 6 | 企业治理（7 任务） | 未开始；能力分离/敏感分级/审计链/加密备份/离职交接 |
| 7 | 员工体验（6 任务） | 未开始；搜索建议/热门/收藏/订阅通知/个人历史 |
| 8 | 助手平台（6 任务） | 未开始；配置驱动助手运行时/Workflow DSL/预算 |
| 9 | 高级助手（5 任务） | 未开始；销售/项目/数据分析/安全图表 |

- **C 组独立计划**（未纳入执行索引）：`engineering-infra-plan.md`（待实施）、`tech-debt-plan.md`（3/4，前端 Vue3 重写进行中）。
- **明确推迟**：NAS/SMB/WebDAV/钉钉连接器、组织权限继承、离职交接、外部通知、场景助手、Git 仓库/语音/多模态图像理解、真实工资考勤启用。

---

## 5. 参考的话（不可违反的架构红线，所有计划共用）

1. **单平台**：一套 Vue + 一套 FastAPI + 一套身份/权限/消息/审计；助手只是配置。
2. SQLite `document_permissions.current_version_id` 是**唯一当前版本权威**；Qdrant `is_current` 只是可修复元数据。
3. **最终答案缓存保持禁用**（曾有跨用户泄漏，勿重开）。
4. 助手/模型只能**收窄**权限；固定话术路径严禁调用 LLM。
5. 置信度只给 `high/medium/low/unavailable`，**不给百分比**。
6. 真实工资/考勤数据必须主管批准才启用；管理员默认无明文查看权。
7. 物理路径、凭据、向量 ID 永不进 API 响应；`#footer` 具名插槽在无 Element Plus 的测试渲染下不可见。
8. TDD 纪律：先失败测试 → 实现 → 跑通 → 提交；测试用临时 SQLite/文件 + fake 后端，**绝不写生产 `data/` 与 `qdrant_data/`**。

**本会话学到的关键教训（写代码时注意）**：
- `UserDB._lock` 是**非重入** `threading.Lock()`：在 `with db._lock, db.transaction(immediate=True):` 内**绝不能**调用会再次加锁的 `db.xxx()` 方法，只能直接用 `db._conn.execute(...)`（`_in_tx` 模式）。本会话在 T5/T6/T11 因此踩了 3 次死锁。
- 事务所有权：`create_document_version`、`set_version_file_path` 等方法自带 `BEGIN IMMEDIATE`，不要在已开事务内调用。
- 用 `BEGIN IMMEDIATE` + `WHERE metadata_version=?` 做乐观并发 CAS；幂等用 `UNIQUE(proposal_id, idempotency_key)` 一类键。
- Python `zipfile` 写文件**不支持加密**（加密标志恒 0）；测试加密条目需手工置 `flag_bits` 位。

## 6. 测试与自检命令

```powershell
$project = 'C:\Users\Administrator\Desktop\RAGv3\RAGv3\.worktrees\security-boundary-hardening'
Set-Location $project
git log --oneline -4        # 应见 441a0fd 结尾
git status --short          # 应干净
& .\.venv\Scripts\python.exe -m pytest tests\test_material_schema.py tests\test_format_registry.py tests\test_streamed_staging.py tests\test_material_jobs.py tests\test_legacy_office.py tests\test_archive_import.py tests\test_incremental_material_index.py tests\test_material_recycle.py tests\test_material_locator.py tests\test_material_organizer.py tests\test_material_api.py tests\test_material_migration.py -q -p no:cacheprovider
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run   # 4 既有失败视为正常
& npm.cmd run build
Set-Location ..
```

若以上绿，环境就绪，直接读计划 3（回答可靠性）Task 1 开始。
