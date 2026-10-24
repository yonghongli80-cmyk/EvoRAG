# Deep Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以最小、可回归的代码改动修复本次深度审查的新缺陷，并保留未解决的架构与运行验收边界。

**Architecture:** 在既有 SQLite repositories、Vue/Pinia、OpenAI async stream、备份文件锁和部署 Bash 中收敛副作用。用户已指定计划后并行 Luna，文件互斥的任务并发实施，Git 与集成由主代理串行处理。

**Tech Stack:** Python3.12/FastAPI/SQLite/pytest，Vue3/Pinia/Axios/Vitest/Node22，Bash，portalocker。

**Spec:** `docs/superpowers/specs/2026-10-24-deep-audit-remediation-design.md`

## Global Constraints

- 不重启、停止、部署或压测现有 `127.0.0.1:8080` / PID48512；不读写业务数据库、业务向量目录或真实候选，不复制或输出 `.env`。
- 单元/API 测试必须加载 `tests/conftest.py`，所有临时数据库/文件位于系统 Temp；不把 `--basetemp` 指到仓库。离线供应商替身不得发送真实网络请求。
- 使用既有隔离 Python 锁环境；前端仅使用独立 Node22/frontend-copy，不修改指向原工作区的 node_modules junction。
- 保持现有 ACL、admin/legacy KB 行为、状态机、canary shadow、沙箱 fail-closed；api.py 不超过830行，pipeline.py 不超过1000行，不放宽架构门禁。
- 不升级依赖、不改模型默认、不重复真实模型基线或 Docker 运行验收。已完成的历史事项不重新计入成果。
- 子代理只修改任务白名单文件，不执行 Git 写操作或开启下一层子代理；主代理统一提交。可并行的前提是文件与测试资源互不冲突。

## Review Focus

- CAS 冲突/数据库事件写失败：失败转换不能改变 validity、task 或事件。
- 路由乱序、404 迟到及确认框期间切库：旧 KB 响应和操作不能作用于新 KB。
- 流在第一 token 后关闭、等待上游时取消、首 token 前重试：每次响应都应释放，已输出 token 不重试。
- KB 删除含多版本/坏路径/文件失败、重复上传被拒绝：不能先失去重试身份或删掉既有目录记录。
- 两个到期调用跨连接/进程交错：同一周期至多一份已验证备份；手工备份锁仍 fail-closed。

## Environment and evidence

工作树：`C:/Users/Syf15/.codex/worktrees/post-handoff-remediation/RAGv3`。Python 命令 `PY` 指 `artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`。Node 目录为 `artifacts/review-backlog-2026-10-24/env/node22/node-v22.23.4-win-x64`；前端运行目录为 `artifacts/review-backlog-2026-10-24/env/frontend-copy`，只同步本任务源文件，禁止操作原 junction。

根证据目录 `artifacts/deep-audit-2026-10-24/`。任务日志各用独立 task-N 目录。基线 c60745d；审查报告在 audit/{backend,frontend,runtime}-report.md，主代理实际 SDK 复现在 audit/stream-probe-retry.log。首次基线因主代理把 basetemp 置于仓库触发隔离测试，失败保留，已改用系统 Temp 重跑，不能把这个工具参数错误归为产品 bug。

### Task 1: Atomic task expiry transition

**Files:** Modify `rag/knowledge_tasks.py`, `rag/repositories/chat_repo.py`; Test `tests/test_knowledge_tasks.py`（必要时 `tests/test_knowledge_tasks_api.py`，不得修改公共conftest）。

**Interfaces:** 保持 `TaskService.transition(task_id, actor, new_status, note, expected_updated_at) -> dict`、`UserDB.transition_knowledge_task(...)` 的调用契约。Repository 在现有任务事务中处理 `confirmed_expired` 的关联版本更新；service 不先独立提交 validity。

- [ ] Step 1: 新增陈旧 expected_updated_at 回归：抛 VersionConflict；task=open、validity=valid、事件数均不变。新增成功转换与事件插入故障回滚的检查，真实临时SQLite。
- [ ] Step 2: `PY -m pytest tests/test_knowledge_tasks.py -q`，保留预期 RED。
- [ ] Step 3: 将关联版本更新放入同一事务，CAS 未成功不得修改；保留 actor/state 校验。
- [ ] Step 4: 重跑该文件与既有知识任务/API/版本有效性相关测试，保留 GREEN。
- [ ] Step 5: 主代理审核任务 diff 后仅提交白名单文件。

### Task 2: Knowledge detail request ownership and cross-KB linking

**Files:** Modify `frontend/src/views/KnowledgeDetailView.vue`; Test `frontend/src/views/__tests__/KnowledgeDetailView.test.ts`。

**Interfaces:** 不改后端 `/files` 和 link endpoint。现有 `loadDetail()`/`enterKnowledgeBase()` 用捕获的KB id和请求代次判断状态归属；确认框与其他有 await 的管理操作捕获KB id，切路由后不对新KB执行旧操作。添加候选按当前 documents 排除，不用全局 in_kb 排除其他KB的源文件。

- [ ] Step 1: 增加 deferred A/B 响应乱序、A迟到404、B失败时不保留A内容/权限、确认框等待时切库的行为回归；in_kb=true但当前库未关联的文件可选，当前库已有仍不可选。
- [ ] Step 2: 只同步本任务文件到独立frontend-copy，`npm exec vitest run src/views/__tests__/KnowledgeDetailView.test.ts`，保留 RED。
- [ ] Step 3: 最小实现请求/操作归属及切库清空；保留角色能力和真实linkDocumentToKb调用。
- [ ] Step 4: 同步并重跑该文件及既有KB/add-link相关前端用例，保留 GREEN。
- [ ] Step 5: 主代理审核任务 diff 后仅提交白名单文件。

### Task 3: Close owned upstream generation streams

**Files:** Modify `rag/generator.py`, `rag/llm_pool/provider.py`, `rag/llm_pool/router.py`; Test create `tests/test_generation_stream_lifecycle.py`，必要时 extend `tests/test_llm_pool_router.py`。

**Interfaces:** 保持 `generate_stream(...)`、`LLMProvider.generate_stream(...)` 与 router SSE事件契约。每个成功创建的上游流由所属生成器在finally或异步上下文中关闭，router显式关闭其消费的provider迭代器；兼容现有测试使用的async generator与SDK的async close()接口。

- [ ] Step 1: 用真实OpenAI SDK/httpx.MockTransport和合成AsyncByteStream写回归：legacy/pool在first-token后aclose立即关闭响应；等待token取消也关闭；正常完成/中途异常/首token前失败与重试不漏响应。通过router消费后关闭也传到底层并释放gate。
- [ ] Step 2: `PY -m pytest tests/test_generation_stream_lifecycle.py -q`，保留 RED，不得只断言mock.close调用。
- [ ] Step 3: 最小实现所属流释放及迭代器关闭；不要改变模型、温度、并发数、重试次数或熔断判定。
- [ ] Step 4: 运行新增文件及 generator_stream、generator_stream_params、llm_pool_provider/router、pipeline_cancellation_observability 既有测试，保留 GREEN。
- [ ] Step 5: 主代理审核任务 diff 后仅提交白名单文件。

### Task 4: Connect regeneration cancellation signal

**Files:** Modify `frontend/src/stores/chat.ts`; Test `frontend/src/stores/__tests__/chat.test.ts` 或现有regenerate专用测试（先定位，报告列出准确路径）。

**Interfaces:** `regenerate(messageIndex)` 的 Axios POST 第三参数传入本次controller.signal；保留 requestId归属校验和stop/reset现有契约。

- [ ] Step 1: 用延迟适配器覆盖 stop、切会话、reset 后请求signal.aborted为true，迟到结果不改当前消息；正常成功仍更新答案/来源/reliability。
- [ ] Step 2: 同步任务文件到独立frontend-copy，运行定向Vitest，保留 RED。
- [ ] Step 3: 只连接已有controller，不新增后端取消/撤销协议。
- [ ] Step 4: 定向聊天/取消/regenerate回归 GREEN；报告明确“浏览器取消不能保证撤销已提交服务器写入”。
- [ ] Step 5: 主代理审核任务 diff 后仅提交白名单文件。

### Task 5: Preserve KB metadata on failures and reclaim all deleted versions

**Files:** Modify `rag/routers/knowledge.py`；必要时 create `rag/kb_document_cleanup.py` 以封装单一清理责任；Test `tests/test_kb_file_lifecycle.py`, `tests/test_kb_link_document.py`，可create `tests/test_kb_failed_mutations.py`。

**Interfaces:** upload/link 记录 pending 行是否由本调用创建，仅回滚自己创建的行。删除逻辑在删除权限/版本数据库身份前，列出全部版本路径并通过 `resolve_version_path(versions_root, stored_path)` 校验并删除；legacy的kb_files路径继续通过已有resolve_stored_path。磁盘清理失败时不能先删除身份；保留已有路由响应契约和来源权限。

- [ ] Step 1: RED真实临时SQLite+合成文件：重复reject/overwrite失败后既有kb_documents仍保留；upload/link两个入口都覆盖；删除文档和整KB时v1/v2路径全消失，另一个文档/外部路径不变。
- [ ] Step 2: 故障注入unlink失败与越界stored path，断言metadata/version身份仍可查询并可重试；保留失败输出，不触及真实文件。
- [ ] Step 3: 最小实现pending所有权与完整版本清理，不加入进程内锁冒充完整跨进程生命周期协调。
- [ ] Step 4: 运行新增/修改文件及KB上传、版本、ACL相关既有回归，保留 GREEN。
- [ ] Step 5: 主代理审核任务 diff 后仅提交白名单文件。KB删除与上传协调另列架构待办，不能登记本任务已解决该竞态。

### Task 6: Remove Redis credentials from connection logs

**Files:** Modify `rag/redis_cache.py`; Test `tests/test_redis_cache.py` 或 create `tests/test_redis_cache_logging.py`。

**Interfaces:** Redis URL与连接/get/set/clear契约不变，成功日志只保留状态。异常分支若仅凭类型无法排除URL泄漏，使用稳定错误类型/阶段，不输出原始异常字符串。

- [ ] Step 1: 合成带password/userinfo/query secret的URL，stub Redis外部边界，caplog证明成功/失败日志不包含凭据，成功与降级功能仍可观察；先保存 RED。
- [ ] Step 2: 去除敏感日志参数，保留故障类别。
- [ ] Step 3: 运行缓存/就绪/日志相关定向回归，保留 GREEN；不连接真实Redis。
- [ ] Step 4: 主代理审核任务 diff 后仅提交白名单文件。

### Task 7: Check updated deployment binding and propagate failures

**Files:** Modify `deploy.sh`; Test `tests/test_deploy_script_health.py`。

**Interfaces:** 保持main成功部署的require/env/harden/build/backup/up/health顺序与告警策略，在maybe_pull成功后执行check_port_binding。各前置阶段须显式传播非零结果，不能依赖在`if ! main`调用上下文中被抑制的set -e；start_stack内up失败也必须在ps之前返回。

- [ ] Step 1: 在临时目录source脚本并stub部署外部命令；maybe_pull将合成Compose从loopback更新为全网卡，断言实际本轮告警反映更新后内容。以真实入口`if ! main`语境覆盖require、pull、build、backup、up失败，验证失败返回非零且后续部署/成功提示不发生；up失败后ps不能掩盖错误。先保存 RED。主代理离线probe已证明pull/backup失败仍up且exit0（audit/deploy-*-failure-probe.log）。
- [ ] Step 2: 移动检查顺序并增加阶段失败显式传播，保留端口告警而非擅自阻断的契约。
- [ ] Step 3: `PY -m pytest tests/test_deploy_script_health.py -q` GREEN；脚本静态语法检查通过，不真实运行deploy/git pull/Docker。
- [ ] Step 4: 主代理审核任务 diff 后仅提交白名单文件。

### Task 8: Recheck backup due under the creation lock

**Files:** Modify `rag/backups/service.py`; Test `tests/test_backup_schedule.py`，必要时 `tests/test_backup_service.py`。

**Interfaces:** 保持 `create_backup(context, trigger, now=None)->BackupRecord` 与 `create_if_due(context, trigger, now=None)->BackupRecord|None`。抽取已有持锁创建体为内部helper，due检查和创建使用同一跨进程文件锁；不得重复获取不可重入锁。并发create/due遇锁占用继续报告backup_in_progress，保留部署前备份门禁的fail-closed，不将“已有创建者正在运行”冒充“已有验证成功的备份”，不删除他人锁/未完成目录。

- [ ] Step 1: 临时目录/合成SQLite、两个due调用在创建准入前交错，验证一个完成后另一个在锁内重查返回None且只有一份formal backup；锁仍占用时create/due保持backup_in_progress且CLI非零，绝不能绕过部署备份门禁；先保存重复决策的 RED。
- [ ] Step 2: 实现受锁due检查，保持格式/验证/保留策略/时钟回退行为。
- [ ] Step 3: 运行backup_schedule/service/CLI/deployment相关回归 GREEN，保留所有失败和成功输出。
- [ ] Step 4: 主代理审核任务 diff 后仅提交白名单文件。

### Task 9: Integrated regression, fresh review, and handoff

**Files:** Create `docs/reviews/2026-10-24-deep-audit.md`; update `docs/handoff-2026-10-24.md`, `docs/README.md` 与本计划checkbox。

**Interfaces:** 只消费前八任务交付及真实日志，不增加未计划行为。主代理协调；每个任务先fresh Luna规格/质量审查，最终整分支审查。

- [ ] Step 1: 审阅并提交已GREEN任务，白名单无交叉；失败/false-positive逐项裁决记录到本计划专属SDD ledger。
- [ ] Step 2: 源码稳定后完整pytest（既有隔离xdist工具、最多4worker、文件分组）、完整Vitest、vue-tsc/生产构建、Ruff和架构门禁；只同步独立frontend-copy，无业务环境安装或运行。
- [ ] Step 3: fresh Luna整分支审查与必要的一轮修复；保留准确通过/失败/跳过数字，不把缺失夹具称为通过。
- [ ] Step 4: 更新报告/交接/索引，列出Compose拓扑、403契约、跨存储KB生命周期、部署/容量/SLO和长期架构待办。只读复核原PID/ready/.env哈希。
- [ ] Step 5: 按已有交付授权快进main、执行交接四条双远端推送并核对真实远端refs；任何外部新改动先读差异，禁止强推。保留隔离工作树及证据。
