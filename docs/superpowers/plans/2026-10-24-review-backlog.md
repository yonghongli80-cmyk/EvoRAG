# Review Backlog Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 闭环 10 月 24 日复查确认的候选竞态、环境与证据差异、文档和双远端待办，并逐项执行可隔离的运行验收。

**Architecture:** SQLite 事务内原子准入；开发与生产锁各有私有验收环境；Docker 与进程恢复只操作合成数据及本轮专属资源。长期生产架构迁移另列路线。

**Tech Stack:** Python 3.12 / SQLite / pytest / Node 22 / Vitest / Docker Compose / 已有锁文件。

**Spec:** docs/superpowers/specs/2026-10-24-review-backlog-design.md

## Global Constraints

- 基线 c353ebe；在 codex/review-backlog-2026-10-24 隔离分支工作，原目录 main 不提前改变。
- 原 8080 服务、真实 .env、业务数据库、Qdrant 本地目录和真实候选不用于测试、迁移或重启演练。
- 不新增产品依赖；依赖锁不为迁就本机而升级或降级；独立环境按现有锁安装。
- api.py≤830、pipeline.py≤1000、局部 imports≤150；不放宽候选 ACL、状态机、门禁或沙箱规则。
- 工作记录由主代理对本次用户事项统一提交；子代理不重复提交。
- 分项测试只跑与修改或疑点相关的范围；代码稳定后主代理统一全后端、全前端及全树 Ruff，保存日志/JUnit/退出码。
- 运行条件缺失要保留真实失败及下一步，不能用静态配置或替身冒充运行成功。
- 最终用户已授权按交接同步两个远端；顺序推送 main 和 tags，无强推，核对真实远端。

## Review Focus

- 同一聚类两连接/API 与 worker 竞争，只允许一个未决补丁写入；不同聚类不互相阻塞。
- 被拒绝的插入不留下 gate/audit 半条记录，既有普通持久化与问题集行为保持。
- 生产锁环境不依赖本机残余 NLTK/core 或开发依赖；前端独立副本不通过旧 node_modules 补包。
- 所有 Docker/进程操作的 PID、端口、卷和 Git 根目录必须属于本轮；失败后保留可核对证据与清理方案。
- 旧报告统计、新锁验收、合成运行和真实业务部署范围在文档中明确区分。

### Task 1: 独立锁环境与基线证据（Luna，可与 Task 2/3 并行）

**Files:** 本轮忽略目录 artifacts/review-backlog-2026-10-24/env/；不修改锁、源码或原环境。
**Interfaces:** 输出 environment-report.json，含 developer_python、runtime_python、node22、frontend_copy 的绝对路径和安装/核对状态；日志不含凭据。

- [x] 用本机 Python 3.12 创建私有 dev/runtime 环境，分别安装 requirements-dev.lock / requirements.lock，保存原始安装输出和退出码；不得改锁解决下载失败。
- [x] 核对适用平台所有版本匹配、pip check、NLTK/core 不在新环境；只装生产锁的环境用测试前置的假凭据和临时存储执行核心导入与比较工作流。
- [x] 独立安装 Node 22，将 frontend 复制到忽略目录并排除 node_modules；用 npm ci 安装新锁，记录版本匹配。现有工作树 frontend/node_modules 是 junction，不对其执行 npm ci 或删除。
- [x] 报告环境准备结果；最终全量测试由主代理统一执行，不在子项重复全量。环境不可获取时保存精确缺失项，继续可用项目。

### Task 2: 事务内代码候选生成去重（Luna，与环境准备并行）

**Files:** Modify rag/evolution/candidate_store.py、rag/evolution/api.py、rag/evolution/worker.py；Create tests/test_code_generation_concurrency.py；必要时调整相关 fixture 但不削弱断言。
**Interfaces:** 新增 OpenCodeCandidateConflict；CandidateStore.add_generated_code_candidate(observation_id, cluster_id, candidate, *, prompt_version='', model='')->int；继承原 add_candidate 的参数和审计输出，仅接受 code_patch。

- [x] 先写实际 RED：临时 SQLite 两个独立连接在 barrier 后竞争写入，期待一条成功一条明确冲突、同簇仅一条 open candidate 和一份生成审计；API 并发请求一成功一409；API/worker 同聚类竞争仍仅一条。
- [x] 对不同聚类、已终态候选重新生成、非代码制品及审计失败回滚设置有效控制；观察旧代码下缺少事务准入的预期失败，不以 import 错误作为 RED。
- [x] 将既有 candidate/gate/audit 实际插入提取为共用内部函数；新的准入方法在同一个 BEGIN IMMEDIATE 事务内查询 OPEN_CANDIDATE_STATUSES 并插入；不持事务等待模型，不为既有数据库增加强制唯一迁移。
- [x] API 调用新接口并将冲突转为409；worker 调用相同接口，将已被占用的聚类跳过，继续其它项；已实际发生的生成调用仍按原预算计数。
- [x] 跑新回归、test_review_evolution_storage_concurrency.py、test_evolution_api.py、test_code_candidate_api.py、worker 生成相关回归和架构预算/Ruff；保存 RED/GREEN 和自审报告，只提交本任务文件。

### Task 3: Docker 当前条件与独立运行验收（Luna，与 Task 1/2 并行）

**Files:** artifacts/review-backlog-2026-10-24/docker/ 的专用 compose、合成数据和证据；产品配置需修复时先回报主代理分配文件。
**Interfaces:** docker-report.json 明确 engine/build/auth/persistence/migration/top5/two_process 的 passed/failed/not_run 与原因和专属资源清单。

- [x] 读取 docker info、context、版本和现有资源摘要；engine 未连接时可隐藏启动已安装 Docker Desktop 一次，有限等待后记录当前错误，不沿用旧探测。
- [x] engine 可用时构建当前 Dockerfile 镜像；新 Compose 去掉固定生产 container_name，用本轮唯一项目/卷、回环或容器内部端口、随机测试凭据，不读取真实 .env，不使用生产 Compose 卷。
- [x] 在合成集合验证 Qdrant 无/错 key 拒绝、正确 key 访问；容器重建后数据存在；独立本地集合迁移前后逐集合点数和固定向量 top-5 一致。
- [x] 两个独立应用进程连接同一 server，验证各自就绪与共享读写；所有失败保留日志，不能用两个 client 对象代替两个进程。
- [x] 清理仅限本轮拥有的测试容器/网络；保留证据和必要持久卷用于复核，绝不操作已有业务资源。不可运行的步骤附可复执行命令，交给主代理记录阻塞。

### Task 4: 真实进程恢复与尚未证实边界（主代理统筹，空闲 Luna 可执行）

**Files:** 忽略目录 lifecycle/；可复用 tests/test_review_evolution_lifecycle.py 的临时 Git 设置。
**Interfaces:** 生命周期证据记录真实 PID、专用端口、helper 返回结果、Git/候选状态、pending marker 和最终清理；不得启动原应用业务数据。

- [x] 在临时 Git + SQLite + 只使用标准库的小型 HTTP 服务上运行真实 helper，覆盖正常停止/启动/健康以及失败后的撤销与再次启动，验证进程与 Git/候选状态对齐。
- [x] 复核停服失败分支保留 pending marker、阻止下一操作；只终止本轮创建且身份已核对的 PID，日志与命令隐藏测试凭据。
- [x] 用临时 Wiki/Qdrant 注入“向量执行成功后返回错误”，确认重试最终一致性；发现真实缺陷按 RED/GREEN 修复，未发现则记录边界依据。
- [x] 无引用/多知识库重新生成的策略只登记为待产品确认，保持当前行为；缺真实脱敏夹具和不适用平台的跳过按事实记录，不造业务证据。

### Task 5: 当前交接、文档和运行结果（主代理）

**Files:** Create docs/handoff-2026-10-24.md；Modify docs/README.md、UPGRADE.md、docs/phase0-observability-and-baseline.md、当前计划。
**Interfaces:** 新交接作为当前有效入口；历史 reviews 报告保留原始统计和时间。

- [x] 汇总各分项证据、实际环境路径、已修复竞态与生成成本边界；更新索引和 Docker 当前探测/验收状态。
- [x] 记录新的隔离运行与负载口径：可用时对专属实例采集足量合成 HTTP 样本和 P50/P95/P99，明确模型替身/合成数据范围，不当作真实模型负载 SLO。
- [x] 列清真实业务迁移、真实模型 P99、适用平台/夹具和长期架构项目的后续顺序与外部前提；任何未运行项均不得勾为完成。

### Task 6: 统一验收、审查与双远端交付（主代理）

**Files:** 本计划、交接、artifacts/review-backlog-2026-10-24/。

- [x] 分项完成后用 Task 1 的 dev 环境统一运行一次全后端、独立 Node22 环境全前端与私有构建、全树 Ruff；保留全部失败/跳过，真实失败按根因定向修复与必要重验。
- [x] 只装生产锁环境执行 pip check 与隔离导入/工作流冒烟；架构预算保持，确认无真实密钥/业务文件纳入提交。
- [ ] 生成分项 diff 包交给 fresh Luna 复审，主代理核对；最后整分支复审并集中处理确认问题，不重复已通过的大范围测试。
- [ ] 验证 main 与双远端无新分叉，fast-forward 合并后按交接四条命令顺序推送两个 main 和所有 tags，核对真实远端最终 SHA；禁止强推。
- [ ] 复制/保留最终证据，更新计划真实状态并实际 submit 本次公司工作记录；完成与外部条件未满足项分别交付。

### Task 7: 验收中复现的认证测试冷初始化超时（Luna，新增发现）

**Files:** frontend/src/stores/__tests__/auth.interceptor.test.ts；只改测试的无关 UI 依赖隔离，产品认证/API/store 实现保持。
**Interfaces:** 继续测试实际 Axios response interceptor 与实际 auth/files/chat 状态；不得 mock 掉被测拦截器、旧会话判断或状态清理。

- [x] 保存独立 Node22 新锁全前端运行中 beforeEach 超时的 RED 证据，测量冷初始化的实际导入边界，区分未被测试消费的 UI 库与被测模块；不能只凭超时猜根因。
- [x] 若确认为无关 UI 组件图导入，测试仅 stub 通知 UI 边界，继续使用真实 Axios、API 拦截器和状态；不提高 hookTimeout，不删弱用例或断言。
- [x] 新锁副本定向复验7项及API/store相关用例，再由主代理重新执行一次全前端和私有构建；保留首次失败与最终结果，独立审查测试仍有真实失效能力。

### Task 8: 构建上下文中的本地工作记录隔离（主代理，运行中发现）

**Files:** .dockerignore；实际过滤验证仅在本轮忽略目录的哨兵context中执行。

- [x] 用真实Docker scratch/COPY构建和本地导出确认RED：仅含假sentinel的 .codex-recorder-data 与AGENTS.md被旧规则带入，原应用哨兵/引导夹具均保留；不使用真实公司记录。
- [x] 添加 .codex-recorder-data/、AGENTS.md 两条排除；同一真实过滤验证拒绝两个哨兵并保留应用源码与四个仓库引导文件；未写反映字符串实现的新单元测试。
- [x] 现有Docker上下文与备份部署门禁9项通过；此前应用镜像构建已取消且不得交付；修复后context重建由Task3跟踪，不删除系统builder缓存或已有镜像/卷。

### Task 9: 实际镜像构建暴露的Windows清单行尾（Luna，新增发现）

**Files:** Dockerfile；shell行为证明在本轮docker/bootstrap-crlf目录。

- [x] 保存真实构建RUN失败与 .repo_files 的CRLF证据；用实际RUN片段及同名无敏感fixture对照，旧实现LF成功/CRLF失败。
- [x] 在read后只规范化CR字符，再沿用当前3个引导fixture存在检查与复制；不扩大COPY/文件列表，不改用户/权限/启动契约或真实manifest内容。
- [x] 用真实Linux shell复验LF/CRLF均成功并备份相同fixture；现有镜像相关门禁通过；提交Dockerfile并复审，再完成Task3完整重建和双进程验收。
