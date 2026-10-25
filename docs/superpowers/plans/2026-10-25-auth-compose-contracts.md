# Auth and Compose Contract Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Disjoint frontend and topology tasks run concurrently after private Node preparation; root owns Git and validation.

**Goal:** 固定401/403和部署拓扑契约，修复普通401残留本地身份，纠正文档误称只读卷。
**Architecture:** Task1仅auth store/回归及会话文档，Task2仅Compose注释/部署文档，Task3准备隔离Node镜像与验证驱动。root整合、独立复查并继续历史恢复及容量架构专项。
**Tech Stack:** 已锁定Vue/Pinia/Axios/Vitest、C私有Node22/Python3.12、Compose静态config；不改依赖。
**Spec:** `docs/superpowers/specs/2026-10-25-auth-compose-contracts-design.md`。

## Global Constraints

- 基线02c9192，C分支codex/auth-compose-contracts-2026-10-25；D main仍1d413ed，本专项不合并/双推/部署，不调用真实API、不修改业务会话/数据/容器。
- 当前会话401同步清身份和共享数据；403重置chat/files但保留AuthUser；generation判断必须先于任何重置，保持旧会话错误/成功和取消保护。
- Compose所有解析后的语义不变，只有注释和文档修正；不把现有RW卷叫只读备份，不凭设置URL声称多副本可用。
- 不直接使用frontend/node_modules（指向D）；私有C依赖package-lock必须同哈希，Node22、单worker、BelowNormal、offline guard阻断非loopback和8080，构建只在artifact私有../static。
- 所有日志在artifacts/auth-compose-contracts-2026-10-25；子代理不Git/子代理/timer/submit，root唯一timerT-D4CB7672EA41。不读取真实.env、不npm安装/升级，不删除旧环境/工作树/SDD。

## Review Focus

- /login或导航失败时普通401仍留身份：Task1真实Axios当前401 RED，不依赖真正导航；stream共用链也覆盖。
- 旧账号的401/403到达新登录后：Task1普通/stream矩阵，保持新用户/新store及无多余提示/导航。
- 403误当注销或共享store仍保留旧数据：Task1同时断言AuthUser保留及chat/files归零，/me行为保持。
- Compose启动依赖与运行就绪混淆、RW卷被当不可变备份：Task2矩阵及解析前后完全相等，沿用既有部署/ready测试。
- 私有验证意外碰D node_modules/static/8080或真实.env：Task3镜像路径与lock校验、Node网络/priority guard、私有构建路径及合成config输入，root检查。

## Environment

所有路径相对本repo，调用工具时显式使用本C工作目录。Python=`artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe`，不是系统默认Python；PYTHONPATH为repo、`artifacts/kb-lifecycle-concurrency-2026-10-24/tools`及`artifacts/staging-recovery-2026-10-25/verification/tools`，pytest先加载`-p offline_network_guard -p staging_test_priority`。Node22=`artifacts/review-backlog-2026-10-24/env/node22/node-v22.23.4-win-x64/node.exe`；C私有依赖=`artifacts/review-backlog-2026-10-24/env/frontend-copy/node_modules`（普通目录、非D链接），相邻package-lock与当前同hash。所有日志新建于本专项artifact目录，不使用D依赖或真实.env。
### Task 1: Session contract and current-401 identity reset

**Files:** Modify `frontend/src/stores/auth.ts`、`frontend/src/stores/__tests__/auth.interceptor.test.ts`、`frontend/src/utils/__tests__/api.test.ts`（局部location替身及跳转断言，不改生产API）；Create `docs/auth-session-contract.md`。
**Interfaces:** 复用registerSessionResetHandler与authSessionGeneration，不改utils/api.ts生产接口、后端cookie或router；401只在通过现有generation检查后清user并sessionChecked=true。

- [x] 等Task3私有Node镜像/runner就绪；写真实Axios普通401 RED（窗口/login避免导航即证明user仍残留）及stream401语义守卫，覆盖当前403数据清空/身份保留与旧普通/stream403不干扰新账号。保留原401/迟到成功/取消回归，避免mock掉实际handler。
- [x] 最小auth改动，使当前401同步清身份、原错误传播与跳转保持；没有403注销、旧响应副作用、额外/me或/logout请求，不增加token存储。
- [x] 同步仅自己修改的源文件到私有镜像，guarded Node22单worker定向auth.interceptor/auth/utils api测试GREEN；报告实际RED/GREEN及矩阵，root提交/fresh复审。不在原frontend目录运行测试或build。

### Task 2: Runtime topology contract and accurate rollback comments

**Files:** Modify `docker-compose.yml`与`.env.example`注释、`docs/deployment.md`；Create `docs/runtime-topology-contract.md`。
**Interfaces:** 所有YAML解析值、env示例赋值、volume及依赖保持；文档明确host/Compose-local/server/未来multi-instance与health/ready区别。

- [x] 读取既有配置/部署/readiness测试，写策略矩阵及启动/运行故障含义；只纠正RW旧卷不能称只读备份的注释，不移除服务、不新增profiles或改默认。
- [x] 用基线Git字节/YAML字典及env非注释赋值对照证明语义不变，使用现有`test_qdrant_deployment.py test_redis_deployment_security.py test_system_ready_metrics.py test_deployment_health.py`定向守卫；不为注释写镜像实现的冗余永久测试。
- [x] 如CLI支持，显式合成.env、清理继承敏感env的隔离子进程渲染Compose local/server config，保存JSON/exit；只config不访问引擎或up/down，不输出真实设置。报告后root提交/fresh复审。

### Task 3: Private Node verification preparation

**Files:** Create ignored `artifacts/auth-compose-contracts-2026-10-25/frontend/`源码镜像、`tools/node-offline-guard.cjs`及`verification/run-frontend.ps1`，本计划task-3-report.md。
**Interfaces:** 输出确切Node22路径、workspace/dependencies路径、同步源文件方法及受限Vitest/typecheck/build命令给Task1和root；镜像原源码配置语义不改，原包/锁不修改。

- [x] 查当前repo的`artifacts/review-backlog-2026-10-24/env/node22/`嵌套node.exe及`artifacts/review-backlog-2026-10-24/env/frontend-copy/node_modules`，拒绝D路径或二次link指向D，比较当前及私有copy package-lock哈希；不安装依赖。
- [x] 复制Git跟踪frontend文件到新镜像（排除node_modules/static/.env*），在镜像建立指向C依赖的junction，不重用原frontend/node_modules；记录镜像清单和路径边界。
- [x] preload使用Node标准os.setPriority与net guard，允许本机随机测试端口/IPC，拒绝非loopback和8080；runner不打印env，Node22单worker，日志文件不可覆盖。静态构建的../static绝对路径必须在此artifact根内，调用前校验。
- [x] 只运行node --version/help及guard自检/compile级检查，不运行产品测试/构建；写报告并通知root/Task1 READY，之后由Task1/root执行各自验证。不得派生代理。

### Task 4: Root integrated verification and onward work

**Files:** Update本计划、总计划、docs/README.md及handoff；Create `docs/reviews/2026-10-25-auth-compose-contracts.md`。

- [x] Task1/2独立规格/质量复审，整分支复查；最终无发现，冻结源/测试e868649。
- [x] 私有Node22单worker完整frontend 41文件330项、vue-tsc、私有生产构建及相关Python六文件42项/Ruff通过。静态构建只artifact，不改C/D原static。
- [ ] 只读核对原服务identity/ready/env hash，确认源码/镜像对应，交接/裁决/局限齐全；唯一实际submit记录并保留证据环境。
- [ ] 更新总计划契约项，继续第三阶段历史暂存恢复写流程，再推进容量/SLO及长期架构，不在本专项结束整个顺序请求。
