# 交接后定向整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复已确认的初始化去重、运营摘要与 purge 失败结算问题，更新交接并核实 Qdrant 运行验收条件。
**Architecture:** 复用现有清洗、授权、仓储和任务重试机制；三个代码任务顺序实施与审查，环境检查可并行只读进行。
**Tech Stack:** Python 3.12 / FastAPI / SQLite / Qdrant / Vue 3 / pytest / Vitest。
**Spec:** docs/superpowers/specs/2026-10-23-post-handoff-remediation-design.md

## Global Constraints
- 使用 Python 3.12 及当前锁定依赖，不新增依赖。
- 不读取/输出密钥或上传资料正文，不改用户现有数据，不提交 AGENTS.md 与记录器数据。
- 保持外部 API 响应键不变；仅收紧全局摘要授权。
- 不抬高 tests/test_architecture_budget.py 的预算。
- 实现先有失败回归测试；任务独立提交，统筹方统一跑一次后端全量、前端测试/构建及 Ruff。
- 代码在 codex/post-handoff-remediation 隔离分支完成；不自动推送或合并共享主分支。

## Review Focus
- 完全重复文本对应不同索引/元数据时仍保留首个 Chunk；近似重复及空输入维持既有清洗语义（Task 1）。
- 同一库多份陈旧文档只计一次；只要有近期当前文档就不陈旧，墓碑或无当前版本不参与（Task 2）。
- 普通用户不能通过直接 API 访问前端隐藏的全局运营摘要（Task 2）。
- purge 第一次逻辑删除已提交后遇到向量故障，下一次仍能从保留的版本记录清理成功（Task 3）。
- 清理持续故障耗尽后状态为 failed，不能静默 done；新文档与恢复中的文档保持现有保护（Task 3）。

### Task 1: 保留首个原始分块的真正去重
**Files:** Modify rag/pipeline.py；Test tests/test_pipeline_clean.py（或聚焦的新测试文件）。
**Interfaces:** Consumes deduplicate_chunks(list[str]) -> list[str]；Produces 同接口 RAGPipeline，self.chunks 与 embed/add 的输入不含已移除重复块。
**Requirements:** 保留首个 Chunk 实例及原始 chunk_index、metadata，输出顺序沿用去重器；完全重复两块只留一块，近似重复仍按既有规则清理，空列表不报错；pipeline 行数不超冻结预算。
- [x] 先添加针对实际 RAGPipeline 初始化行为的回归测试：相同文本、不同对象索引元数据，断言输出仅保留首块；补近似重复与空分块边界。只替换外部嵌入/向量与必要持久化副作用，不复制去重算法。
- [x] 在修复前运行新增测试，保存预期断言失败的 RED 证据。
- [x] 做最小修复，不调整全局去重策略或接口。
- [x] 跑新增测试、tests/test_pipeline_clean.py、tests/test_cleaner.py、tests/test_architecture_budget.py 和改动文件 Ruff，记录 GREEN。
- [x] 仅提交自己的生产代码和测试，写任务报告；全量集成测试由统筹方执行一次。

### Task 2: 运营摘要权限与陈旧知识库指标
**Files:** Modify rag/operations_api.py、rag/repositories/chat_repo.py；Test tests/test_operations_api.py，必要时 tests/test_knowledge_tasks.py。
**Interfaces:** GET /knowledge-operations/summary 保留响应键；UserDB.task_summary()/ChatRepository.task_summary() 签名不变。
**Requirements:** 摘要只允许系统管理员；匿名 401、普通用户 403、管理员 200。统计仅使用 deleted_at IS NULL 且 current_version_id IS NOT NULL 的文档；按 kb_id 分组后 MAX(updated_at) 早于截止点才计入。两份老文档同库=1；老新混合库=0；仅墓碑库=0；无当前版本=0；不同的陈旧库各计一次。其余任务统计不变。
- [x] 先添加真实 API 授权与数据库统计回归测试；把旧的普通用户成功摘要用例改为管理员成功，并新增成员/匿名拒绝用例。
- [x] 修复前跑新增测试，记录权限与计数的 RED 断言失败；时间使用固定值或受控旧/新时间，不依赖墙钟临界点。
- [x] 复用现有管理员依赖，修正聚合查询，避免 N+1 或加载全库文档到 Python。
- [x] 跑 tests/test_operations_api.py、相关仓储测试及改动文件 Ruff；记录 GREEN。
- [x] 独立提交代码与测试，写任务报告；全量集成测试由统筹方执行一次。

### Task 3: purge 向量失败进入持久任务重试
**Files:** Modify rag/materials/worker.py；Test tests/test_material_recycle.py，必要时 tests/test_material_jobs.py。
**Interfaces:** 继续使用 JobResult、MaterialWorker.run、settle_material_job 与既有 attempt/backoff，不新增任务种类。
**Requirements:** 向量删除失败由现有 runner 结算 retry，达到 max_attempts 后 failed；不能 done。逻辑 purge 已完成仍保留版本 ID，后续重试最终清理向量并 done；多个版本清理幂等，恢复窗口保护不变。
- [x] 先添加真实持久任务执行测试：第一次向量服务故障 -> retry，第二次恢复 -> done；断言失败后未虚假完成、第二次确实清除了原版本向量；另测持续失败耗尽。
- [x] 在修复前跑测试，保存 RED；使用小型故障注入向量适配器而非对 purge 主逻辑整段打桩。
- [x] 最小调整错误传播或结算，复用 runner 的重试预算，保留原异常日志与上下文。
- [x] 跑 tests/test_material_recycle.py、tests/test_material_jobs.py 和改动文件 Ruff，记录 GREEN。
- [x] 独立提交自己的代码与测试，写任务报告；全量集成测试由统筹方执行一次。

### Task 4: 更新交接并核实 Phase 1 运行前置
**Files:** Modify docs/handoff-2026-10-23.md、docs/README.md、docs/deployment.md；同步 .github/workflows/deploy.yml 的说明注释；必要时追加 docs/phase0-observability-and-baseline.md；保持历史计划原记录。
**Interfaces:** 消费 Tasks 1–3 的已审查提交和真实环境检查证据，不把计划或 mock 结果当运行时验收。
- [x] 核对双远端快照、最新交接入口、GitHub Actions 的触发条件，去掉当前无 GitHub remote 的陈旧断言。
- [x] 把本轮问题与修复提交/测试纳入交接，历史 2801 测试标为当时基线，当前结果单列。
- [x] 记录现有 Docker Desktop/WSL/daemon 预检证据及唯一启动尝试结果。用户已选择“先完成代码整改，保留运行验收步骤”；本轮不再尝试容器运行验收、不安装系统组件、不改生产配置或迁移用户数据。
- [x] 文档按事实更新，不为人类说明文字编写文本匹配测试；独立提交。

### Task 5: 集成验证与整分支审查
**Files:** 本任务不增加生产行为；必要时只修复本轮引入的回归。
- [ ] 统一运行后端全量 pytest、全树 Ruff、pip check、前端全量测试与临时目录构建；保存完整输出。
- [ ] 生成整分支 diff 包，Luna 只读审查规格符合性、质量、数据边界与测试证据。
- [ ] 如有重要问题，集中一轮修复与定向复审；记录保留问题及环境限制。
- [ ] 保留可审阅分支及提交，交付改动、验证结果和运行时剩余事项；按项目要求实际提交工作记录并检查返回。

### Task 6: 清洁工作区测试凭据隔离（Task 5 重跑前置）
**Files:** tests/conftest.py、tests/test_test_isolation.py；不改变生产凭据默认值。
首次全量暴露测试依赖原目录 .env 中模型密钥：客户端构造在模拟执行前即失败。补固定假测试凭据及真实消费者回归，保持路径隔离、单例重置和禁用外部子系统的约束；不复制真实密钥或新增依赖。先RED、后GREEN及相关测试，再独立提交与审查，之后重跑Task5全量。

### Task 7: 批量导入版本测试的离线嵌入边界
**Files:** tests/test_batch_import_versioning.py；不改生产导入或检索代码。
全量重跑发现该文件四项用例直接请求嵌入服务并因假凭据401失败。复用已观察RED，以文件级外部嵌入替身输出有效确定向量；保留真实SQLite/Qdrant、版本可见域与回滚断言，恢复嵌入单例/缓存并禁止真实客户端回落。相关回归GREEN、独立提交与审查后执行最终完整套件。

实施状态：Tasks 1–4 已独立审查通过；Task 6 提交414264c/518fd44、Task 7 提交eb14c85，均已独立审查通过。Task 5 等待修复后稳定提交的完整后端验收及整分支审查。
