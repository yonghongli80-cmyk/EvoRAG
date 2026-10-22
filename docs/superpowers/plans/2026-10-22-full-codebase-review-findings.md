# RAGv3 全库审查发现（2026-10-22）

> 配套：`docs/superpowers/specs/2026-10-22-full-codebase-review.md`、
> `docs/superpowers/plans/2026-10-22-full-codebase-review-plan.md`。
> 基线：`HEAD=67da261`，工作树干净，**2366 passed / 0 failed**，`ruff` 全绿，服务 healthy。
> 审查方式：6 个域级只读子代理 + 主代理跨域扫描 + P0/P1 代码复核。
> **本阶段不改产品代码、不写数据库。**

---

## 0. 结论先行

| 级别 | 数量 | 含义 |
| --- | --- | --- |
| **P0** | **3** | 干净环境部署即崩 / 跨用户数据泄露 / 唯一备份永久失败 |
| **P1** | **14** | 安全边界、测量证据、数据一致性、核心可用性 |
| P2 | ~40 | 降级路径、并发、性能、错误语义、前端状态 |
| P3 | ~12 | 清理、测试网、文档/配置漂移 |

**升级前必须修（P0 + 关键 P1）**：F-01、B-01、D-01、A-01/A-02、C-01/C-02、
D-02/D-03/D-04/D-05、E-01/E-02/E-03、F-02/F-03、B-02。

历史审查的差量结论：2026-10-16 的 12 项中 10 项已修，`GUARD-01`、`SEC-02` 仍开；
2026-10-17 的模块化主体已落地，但 `api.py=791`、`pipeline.py≈1547`、延迟导入仍 198 处，
属于架构债，不是本轮 P0/P1。

---

## 1. P0（3 条）

### P0-01 | F-01 | 锁文件缺 `typesafe-sdk`，干净环境 import 即崩
- **Evidence**：`pyproject.toml:37` 声明 `typesafe-sdk>=0.7,<0.8`；`requirements.lock` 与
  `requirements-dev.lock` 均无该包（实测 grep 只命中 `httpx2/truststore`）；`Dockerfile:35-36`
  只装 `requirements.lock`；`rag/api.py:51` → `rag/evolution/api.py:70` → `rag/jev/questions.py:10`
  顶层 `from typesafe_sdk import ...`；`rag/pipeline.py:29` 同样顶层导入。
- **Trigger**：`docker compose build` 后启动，或任何 `pip install -r requirements.lock` 的干净机器；
  CI 用 dev lock 收集测试时同样失败。
- **Impact**：`ModuleNotFoundError`，容器退出 = 生产不可用；CI 在收集期失败；
  README「完整锁文件」不成立；pip-audit 审不到实际安装的包。
- **Fix direction**：重新生成两份 lock（pip-compile）；CI 加 lock↔pyproject 一致性与
  `python -c "import rag.api"` 冒烟；禁止手改锁文件。
- **Test gap**：`tests/test_dependency_lock.py` 只断言每行含 `==`，不校验依赖齐全。
- **Confidence**：high（静态导入链已复核）。

### P0-02 | B-01 | 对话记忆按 `session_id` 全局共享，跨用户泄露 + 投毒
- **Evidence**：`rag/routers/query.py:208-210`、`:285-287`（`conversation_id` 直接映射为
  `conv_<id>`，**生成前没有归属校验**；`:228-249` 的 `get_conversation(...)` 只在生成后落库时调用）；
  `rag/pipeline.py:1280/1342-1343`、`:1144-1145`（`build_messages(sid)`）；
  `rag/memory.py:169-176`（只按 session_id 读历史，无 user/kb 维度）；缺省 `session_id=None`
  时 pipeline 落到 `"default"`。
- **Trigger**：任何登录用户 `POST /query` 不带 session_id（共享 `"default"`）；或传他人的
  `session_id` / `conversation_id`（顺序整数可猜）。
- **Impact**：他人最近 10 轮问答与摘要进入当前 prompt（模型可复述）；新问题写入同一会话 =
  泄露 + 投毒；会话消息端点的归属校验只保护落库，不保护生成。
- **Fix direction**：记忆键加 principal（或先校验 conv 归属再使用 `conv_` 命名空间）；
  缺省 session_id 不得落 `default`；两个流式/同步端点都在生成前完成归属校验。
- **Test gap**：无「B 用 A 的 conversation_id/session_id」用例。
- **Confidence**：high。

### P0-03 | D-01 | 素材上传把版本文件留在 `staging/`，周备份永久失败
- **Evidence**：`rag/materials/api.py:249-252`（`create_document_version(..., str(staged.path))`）；
  `rag/materials/staging.py:54-56`（落盘 `<versions_root>/staging`）；
  `rag/backups/service.py:35`（`staging/quarantine/archive_work` 被排除）与
  `:546-550`（`required_file_keys` 不满足 → `referenced_file_missing`）；
  正规路径 `rag/document_versions.py:136-138` 会 promote 到 `final_version_path`。
- **Trigger**：`POST /materials/upload` → worker 激活版本 → 任意 `create_backup` /
  `create-if-due --trigger deploy`。
- **Impact**：每周权威备份永不成功；部署门禁按设计拒绝 `compose up`；无可用恢复点；
  只要有一个走该接口的文档被索引，整个备份链就坏。
- **Fix direction**：上传复用 `final_version_path` + `promote_version` 后再入队；
  或备份按 `document_versions.file_path` 白名单收录已提交版本，不按目录名排除。
- **Test gap**：缺「上传 → 激活 → create_backup 成功」端到端用例。
- **Confidence**：high（代码路径已逐行复核）。

---

## 2. P1（14 条）

### P1-01 | A-01 | 熔断文案被写进改写缓存，永久污染该问题
- **Evidence**：`rag/generator.py:32-33` 熔断打开返回「系统繁忙，请稍后重试。」而非异常；
  `rag/query_rewriter.py:34-35` 原样返回；`rag/pipeline.py:550-559` 只排除空/等于原问题，
  该文案被写入 `_rewrite_cache`。
- **Impact**：该问题此后检索词永久变成「系统繁忙…」，召回垃圾 → 固定拒答，直到 LRU/重启；
  同 KB 所有用户命中；与 `_rewrite_cached` 的 docstring 相反。
- **Fix**：失败返回可识别失败（或抛异常），只在成功且改变问题时缓存；补熔断文案不缓存用例。

### P1-02 | A-02 | 熔断文案被当成正式答案：入记忆、入轨迹、仍报 reliability
- **Evidence**：`rag/pipeline.py:1313`（同步直接当答案）、`:1339-1343`（写 memory）、
  `:1477-1479`（流式当 token）、`:1495-1502`（轨迹 `route=rag`）、`:1520-1524`（done 带 reliability）；
  `_submit_failure_observation` 对 answerable 返回 None。
- **Impact**：用户把「系统繁忙」当正式答案（可能附 high/medium 置信度）；污染多轮上下文；
  运维/演化看不到失败；无 error 事件。
- **Fix**：降级改成结构化失败，统一走 `_record_stream_failure` 并下发 degraded 事件；
  不写 memory、不附 reliability；补 pipeline 级用例。

### P1-03 | B-02 | 无 `kb_org_policies` 的 legacy KB 回退个人 ACL，迁移不自动
- **Evidence**：`rag/api_security.py:160-177`（policy None → `check_kb_permission`）、
  `rag/permissions.py:102-127`（`scope='public'` 任意登录用户可读）；`rag/org_auth/migration.py:14-27`
  只迁移有 metadata 的 KB，唯一调用点是手工 `seed.py`，启动不跑。
- **Impact**：两套授权模型并存；`public` 旧 KB 仍可按 ID 被全体账号读；2026-08-20 §7
  「迁移期仅管理员可见」未强制。
- **Fix**：启动无条件迁移；policy 缺失 fail-closed 到 admin-only；加未迁移自检。

### P1-04 | C-01 | 沙箱是 VCS 隔离，不是安全隔离：候选代码以宿主权限执行、可联网
- **Evidence**：`rag/evolution/sandbox.py:515-549`（`sys.executable -m pytest` 跑补丁后的
  worktree `rag/**`）；`sandbox.py:332-353`（`_sandbox_env` 只剥 `RAG_*`/`PYTHON*`，不降权、
  不断网、不限文件系统）；worktree 的 `.git` 指向主仓，主仓 `.env`/`data/users.db` 路径可推出。
- **Impact**：审核沙箱阶段的任意代码执行（宿主身份、可联网、可读生产凭据）；设计承诺的
  CPU/内存/网络隔离只实现了超时。
- **Fix**：低权用户/容器 + 只读挂载 + 无出网 + 独立 HOME；或明确文档化「不是安全边界」；
  补 max_rss/CPU 限制。

### P1-05 | C-02 | 回放 fixture 的 staleness 算在干净树上，seam 补丁仍拿「无回归」
- **Evidence**：`rag/evolution/sandbox.py:966` `fixture_staleness(replay_fixture, baseline_worktree)`
  ——baseline 是未打补丁的树；同一 `stale` 决定两侧是否用 fixture（`:971-989`）；
  `rag/evolution/replay_fixture.py:186-204` 才对传入 worktree 判 seam。
- **Impact**：补丁改的 reranker/embedder/agent 等被 fixture 顶掉，两侧跑同一份旧行为，
  `patch_verdict` 输出 `passed +0.0pp` 并推进 `awaiting_verification` ——「没测补丁」签发「无回归」。
- **Fix**：对 patched worktree 计算 staleness（或两侧都算，任一不过即拒）；diff 触碰 seam
  直接拒绝回放；补集成反例。

### P1-06 | D-02 | 组织 KB 的删除墓碑不被校验，已删文档仍可读
- **Evidence**：`rag/api_security.py:221-227`（org 分支 `require_kb_view` 后直接 `return doc`，
  不查 `deleted_at`）；对照 `rag/permissions.py:50-52` 非 org 路径会拒绝；行数据端点只查
  `dataset.status`。
- **Impact**：org KB 删除后成员仍可读表格行/下载；删除只是 UI 语义。
- **Fix**：`require_doc_view/edit` 的 org 分支补 `deleted_at`；行数据/审计端点按 version.status 过滤。

### P1-07 | D-03 | 同一 base 的并发单元格编辑不 409，先提交被静默丢弃
- **Evidence**：`rag/repositories/spreadsheet_repo.py:299-341` 只校验
  `current_version_id == base_version_id`，不排斥已有 pending session；两个请求各拿
  MAX+1 version，都成功；`rag/materials/worker.py:324-332` 只保留 latest，前者 cancelled。
- **Impact**：两位编辑者同时保存，前者无提示丢失；与 2026-08-14 §7/§11「第二个请求 409」冲突。
- **Fix**：reserve 事务加「唯一 pending session」CAS/唯一约束，第二个返回 VersionConflict。

### P1-08 | D-04 | 30 天回收清理从未运行
- **Evidence**：`rag/materials/recycle.py:160` 定义 `purge_document`；grep 仅
  `__init__.py` 导出与测试引用；worker 无 purge job、无扫描、无调度、无端点。
- **Impact**：删除文档的原文件/FTS/向量/structured_datasets 永不释放；合规保留与磁盘承诺失效。
- **Fix**：worker 增加 `purge_document` job + 周期扫描；补齐 dataset/fingerprint 清理。

### P1-09 | D-05 | Wiki 写入与索引非原子，索引失败页面永久不可检索
- **Evidence**：`rag/wiki/api.py:117` 先 `store.create_page`（已提交）再 `indexer.sync_page`；
  `rag/wiki/indexer.py:64-89` 先 delete 再 embed/add；异常/重启留下「删了没加」；
  `content` 无长度上限。
- **Impact**：页面 UI 存在但 Qdrant/BM25 缺块，RAG 永远检索不到；无重试/审计。
- **Fix**：复用 `document_index_jobs` 入队 + 重试；delete+add 补偿；content 加 max_length。

### P1-10 | E-01 | 素材 API 仍从 localStorage 拼空 Bearer，归档链路 100% 401
- **Evidence**：`frontend/src/api/materials.ts:3-5` 及 10+ 调用点；`utils/api.ts:11`
  启动即删 `rag_token`；后端只在「没有 Authorization 头」时才用 cookie（`rag/api.py:451`）；
- **Impact**：已登录用户打开归档/确认导入恒 401，拦截器整页跳 `/login`，形成「点归档→闪回」；
  若回填 localStorage token 修，则破坏 HttpOnly 会话模型。
- **Fix**：删除 `headers()`，统一走 axios 默认 cookie/withCredentials；补 materials 测试。

### P1-11 | E-02 | 退出登录不清理 Pinia store，换账号可见上一账号数据
- **Evidence**：`frontend/src/views/MainLayout.vue:28-31` 仅 `router.push('/login')`；
  `stores/files.ts:31,44`（`_loaded` 常驻）、`stores/chat.ts:69-76`（同 mode 不清空）。
- **Impact**：共用浏览器 A 退出 → B 登录，B 看到 A 的私有文件名/大小/共享标记/对话标题；
  网络失败时长期停留。
- **Fix**：logout/401 集中 reset chat/files store；补 A→B 切换用例。

### P1-12 | E-03 | SSE `type:"error"` 完全不处理，空白回答且丢消息 id
- **Evidence**：`frontend/src/stores/chat.ts:228-265` 只处理 token/sources/structured_result/done；
  后端 `rag/routers/query.py:337`、`rag/pipeline.py:1381` 会发 error；`utils/api.ts:67` 只读 `detail`。
- **Impact**：生成中途异常时助手气泡空白、无错误提示、无 assistant_message_id，反馈/重生成失效。
- **Fix**：增加 error 分支（reason/失败态/重试）；兼容 `data.error`；补 SSE error 用例。

### P1-13 | F-02 | `COPY . .` 把公司数据/备份/测试产物打进生产镜像
- **Evidence**：`Dockerfile:39` `COPY . .`；`.dockerignore` 无 `real-case-handoff/`、
  `artifacts/`、`scratch/`、`tmp_test_dir/`；实测 `real-case-handoff` **13.26MB**、
  `artifacts` 1.83MB、`scratch` 0.12MB、`tmp_test_dir` 0.39MB；`.gitignore:20,23` 明确排除前两者
  （注释 "company data, never committed"）。
- **Impact**：镜像/registry 携带客户文档、备份目录名、基准日志；`docker save` 即可取出；
  违反 README 安全声明与 UPGRADE 的风险提示。
- **Fix**：补齐 `.dockerignore` 或改白名单 COPY；CI 构建后断言镜像内不存在这些路径。

### P1-14 | F-03 | 干净部署没有可用管理员；seed 的 123456 账号与远程启动护栏互斥
- **Evidence**：`config.py:75-78`（auth=true、自注册 false、`allow_initial_passwords_remote=false`）；
  `rag/routers/auth.py:62-63`（注册 404）；`rag/api.py:332-339`（`INIT_ADMIN_USERNAME` 只提权已存在用户）；
  `rag/org_auth/seed.py:90`（280 个 `demo*` 账号统一 `123456`）；`start.py:33-43` 在
  0.0.0.0 + active 组织账号 123456 时拒绝启动；Docker 强制 0.0.0.0。
- **Impact**：干净 compose 部署无人能登录；按手册 seed 后容器重启循环，除非关闭唯一口令护栏
  （`RAG_ALLOW_INITIAL_PASSWORDS_REMOTE=true`，部署文档未写）。
- **Fix**：提供一次性 bootstrap（创建 admin + 强制改密）；文档写清 seed 与远程绑定冲突；
  开启 override 时打印安全警告并审计。

---

## 3. P2 / P3（按域，压缩）

| ID | 级别 | 主题 | 关键证据 |
| --- | --- | --- | --- |
| A-03 | P2 | Jev 背景相关块进入证据包，可抬置信度/单独满足可答性 | `jev/policy.py:234-240`、`reliability/bundle.py:30-46` |
| A-04 | P2 | BM25 重建丢 `chunk_kind`，wiki 在 BM25 路径被 ACL 误删；tags 半边失效 | `retriever.py:79-93`、`permissions.py:195-213` |
| A-05 | P2 | 注入黑名单可绕过且误伤业务词；语义检测只在 Jev 路径 | `guard.py:15-45`、`memory.py:185-190` |
| A-06 | P2 | `/regenerate` 固定 rag_docs、丢历史警告、跳过输出过滤、不更新 reliability | `routers/query.py:424-443` |
| A-07 | P2 | legacy 每问答多一次路由模型调用；agent 分支固定拒答；SQL 工具绑 `:memory:` | `pipeline.py:682-691`、`agent.py:140` |
| A-08 | P2 | 写锁内建 BM25、ReadWriteLock 写者饥饿、比较端点用写锁读 | `concurrency.py:15-37`、`context.py:186-199` |
| A-09 | P2 | 每题两遍全量 wiki + 修订扫描（含正文），N+1 | `versioned_retrieval.py:64-132` |
| A-10 | P2 | 重排全降级/引用兜底拒答都记成检索缺口，`best_score=None` | `pipeline.py:389`、`reranker.py:159-162` |
| B-03 | P2 | is_admin 授予无审计、可自降；`_is_admin(None)=True` fail-open | `routers/auth.py:222-242`、`user_repo.py:159-166` |
| B-04 | P2 | 认证入口未统一（delete_file 手写 header、/suggest legacy、4 处死代码） | `routers/documents.py:352-364`、`routers/query.py:382` |
| B-05 | P2 | 内部异常文本回显客户端（5 处） | `routers/documents.py:391`、`knowledge.py:443/614/669/699` |
| B-06 | P2 | KB 列表 404 连坐；403/404/409 语义分裂 | `routers/knowledge.py:167-171`、`api_security.py:170-175` |
| B-07 | P2 | DBSource SQLite 仍是文件名黑名单（SEC-02 未闭环） | `data_sources/db_source.py:24-33,89-101` |
| B-08 | P2 | BatchImporter 管理员身份桩 + 直连生产 users.db | `batch_importer.py:143-160` |
| C-03 | P2 | fixture 语料指纹校验是死代码，seams 缺项即跳过 | `sandbox.py:374`、`replay_fixture.py:195-201` |
| C-04 | P2 | 回放快照未强制只读，两侧共享同一份 | `sandbox.py:377-408,969-989` |
| C-05 | P2 | canary 不校验 artifact_type/层 2/3，先写状态后生效 | `evolution/api.py:814-880` |
| C-06 | P2 | 灰度硬上限在 `restore()` 未 clamp，kill switch 失效 | `rollout.py:268-313` |
| C-07 | P2 | worker 预算事后记账、无跨进程租约 | `worker.py:236-272`、`host_lock.py` |
| C-08 | P2 | `rejected`/`needs_more_evidence` 终态使观测永久退出生成队列 | `candidate_store.py:628-631` |
| C-10 | P2 | 权限/审计红线条目靠文件名黑名单，ACL 调用点在可改文件内 | `code_patches.py:64-80`、`pipeline.py:633/713/1079` |
| C-11 | P2 | apply/rollback 无并发保护、不取 host_lock；helper 跑全量测试 | `evolution/api.py:1566-1873`、`service_control.py:343-359` |
| D-06 | P2 | 7z 条目整体解压进内存后才检查上限，可 OOM | `materials/archives.py:378-386` |
| D-07 | P2 | 行数据端点逐格展开合并区域，可被一个工作簿拖垮 | `spreadsheets/api.py:284-293` |
| D-08 | P2 | 结构编辑拒绝面不全，保存后只比对表名 | `structure_editor.py:60-86,201-210` |
| D-09 | P2 | 敏感审计值遮蔽未实现（`sensitive=0`） | `edit_service.py:457`、`audit.py:163-167` |
| D-10 | P2 | 整理撤销只恢复 logical_path；冲突仍返回 applied | `organizer.py:294-339,265-272` |
| D-11 | P2 | FTS 语法错误被吞成空集，查询非法与查不到不可分 | `material_repo.py:276-289` |
| D-12 | P2 | CSV 先 splitlines 再解析致引号内换行错行；导入不中和公式前缀 | `spreadsheets/parser.py:202,246-261` |
| E-04 | P2 | 两个管理员路由缺 `requiresAdmin`（FE-01 只做一半） | `router/index.ts:43-56` |
| E-05 | P2 | 无超时/取消/停止，流卡住永久锁死输入 | `utils/api.ts:13-16`、`stores/chat.ts:202-214` |
| E-06 | P2 | 会话切换无请求序号/取消，快速点击显示错会话 | `stores/chat.ts:122-147` |
| E-07 | P2 | 素材 CAS 版本号硬编码 0；回收站/整理弹窗无入口 | `stores/files.ts:85,102-106`、`materials/api.py:437-447` |
| E-08 | P2 | `/kb/:id` 切换不重置对话状态，跨库串味 | `KnowledgeDetailView.vue:77-88`、`chat.ts:69-76` |
| E-09 | P2 | 文件下载绕过 API base + 下载再上传；dev 把 HTML 当文档 | `api/files.ts:7`、`KnowledgeDetailView.vue:240-248` |
| F-04 | P2 | 容器 PID1 是 start.py，SIGTERM 不转发，lifespan 清理被跳过 | `Dockerfile:64`、`start.py:85-122` |
| F-05 | P2 | 11 个 Settings 字段无人引用；4 个 .env 键走 `os.getenv` 不生效 | `config.py`、`rag/logging_config.py:37-38`、`rag/auth.py:26` |
| F-06 | P2 | 无 start_period；deploy.sh 不做健康验证/回滚；超时仍打印“已就绪” | `docker-compose.yml:36-40`、`deploy.sh:31-33` |
| F-07 | P2 | 测试隔离依赖本机 .env，默认关鉴权，canary/evolution 未中和 | `tests/conftest.py:8-34` |
| F-08 | P2 | CI 与托管/构建脱节：无 lock 检查/import 冒烟/docker build；origin 是 Gitee | `ci.yml:15-19`、`git remote -v` |
| A-11/A-12、B-09~B-12、C-09/C-12、E-10、F-09/F-10 | P3 | Jev 平票、客户端生命周期、登录加固、组织写入、mode-only 补丁、canary 数据、XSS 测试网、render/脚本供应链 | 见各域审查记录 |

---

## 4. 历史审查差量

| 历史项 | 现状 | 证据 |
| --- | --- | --- |
| PIPE-01 流式 pipeline 泄漏 | **fixed** | `_get_or_create_pipeline` 缓存 + `context.py` |
| PIPE-02 单文件 `clear()` 清空集合 | **fixed** | `pipeline.py:277` 改为 `delete_doc(doc_name)` |
| GEN-01 流式重试 token 重复 | **fixed** | `generator.py` `has_yielded` |
| SEC-01 SSRF 重定向 | **fixed** | `data_sources/base.py:56-72` 逐跳校验 + `allow_redirects=False` |
| SEC-02 SQLite 路径限定 | **open** | `db_source.py:89-101` 仍是文件名黑名单；B-07 |
| SEC-03 归档有界拷贝 | **fixed（zip）** / **open（7z）** | `archives.py:352-373`；D-06 |
| ACL-01 N+1 | **fixed（dense/普通）** / **open（BM25、locator）** | `permissions.py:194-203`；A-04/D 观察 |
| WIKI-01 删除碰撞 | **fixed** | `delete_doc(..., chunk_kind="wiki")` |
| STORE-01 BM25 WAL | **fixed** | `bm25_store.py:41-42` |
| CACHE-01 Redis fallback | **fixed** | `redis_cache.py:114` |
| EMBED-01 批量嵌入 | **fixed** | `embedder.py:17,104-106` |
| LOCK-01 删除用读锁 | **fixed** | `vector_store.py:59` |
| GUARD-01 注入词收窄 | **open** | `guard.py:15-45` 仍含「系统提示/没有任何限制」；A-05 |
| CFG-01 wiki_db_path | **fixed** | `config.py:72` |
| DOCKER-01 JAVA_HOME | **fixed** | `Dockerfile:25-29` |
| FE-01 管理员路由守卫 | **partial** | `/organization` 有，`/operations/*` 没有；E-04 |
| 10-17 模块化（routers/context/repos/连接池/前端拆分） | **fixed 主体** | 15 router、`user_db` 门面、连接管理、evolution tabs |
| 10-17 “拔除 366 处 lazy import / api.py <250” | **open（架构债）** | 延迟导入 198、`api.py=791`、`pipeline.py≈1547` |

---

## 5. 已验证的“没有问题的点”（避免重复报告）

- 外部注入：`subprocess` 全部 argv 列表、无 `shell=True`；补丁走 stdin/临时文件；
  `test_paths` 限 `tests/**` 真实文件（C 域）。
- 权限：`knowledge.py` 17 个端点逐一调用 `require_*`；Wiki IDOR 已修；`/sources` 默认 404+admin；
  `/register` 默认关闭；cookie→Bearer 同源校验已就位（B 域）。
- 数据：Zip Slip/符号链接/加密条目/压缩比/条目数/有界流式拷贝（zip）、不可变版本、
  参数化 SQL、FTS 不拼接（D 域）。
- 前端 XSS：全仓 3 处 `v-html` 均先转义，当前不可利用；缺 sanitizer 兜底的是
  `ConversationSearch.vue`（E 域）。
- 自演化：状态机不可跳级、`net_zero_churn` 只读、层 2 阈值未弱化、回滚语义完整、
  所有端点管理员鉴权（C 域）。

---

## 6. 审查限制

1. 全部为静态审查 + 定向只读；没有在干净环境复现 F-01，没有构建镜像/跑全量测试。
2. 部分 P2/P3 的利用条件未实测（C-09 mode-only、D-07 大合并区、F-07 环境泄漏）。
3. `docs/superpowers/plans/2026-10-22-full-codebase-review-findings.md` 是汇总；逐域原始
   证据行号以本文件为准，子代理报告未单独入库。

---

## 7. 下一步

按 `docs/superpowers/plans/2026-10-22-pre-upgrade-remediation-plan.md` 的 P0 → P1 顺序实施；
P0 未修完不进入 Qdrant/Postgres 升级。
