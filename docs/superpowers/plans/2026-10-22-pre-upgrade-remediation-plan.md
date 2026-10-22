# 架构升级前整改计划（2026-10-22）

> 依据：`docs/superpowers/plans/2026-10-22-full-codebase-review-findings.md`
> 审查 spec：`docs/superpowers/specs/2026-10-22-full-codebase-review.md`
> 基线：`HEAD=67da261`，2366 passed，ruff 全绿，服务 healthy。
> **原则：P0 未清零不进入 Qdrant/PostgreSQL 升级；每项先写失败用例再改代码。**

---

## 0. 门禁

| 门禁 | 条件 |
| --- | --- |
| G0 | 干净环境 `pip install -r requirements.lock` + `python -c "import rag.api"` 成功；CI 有对应冒烟 |
| G1 | 跨用户记忆/会话用例通过（A 用户无法读 B 的 session/conv 历史） |
| G2 | `materials/upload → worker 激活 → create_backup` 端到端通过，`referenced_file_missing` 不再出现 |
| G3 | 全量 `pytest`、`ruff`、benchmark 不退；`/health` healthy |
| G4 | 每个 P1 修复带至少一条负向/回归用例 |

---

## 1. P0（升级前必须清零）

### P0-01 F-01：锁文件补齐与安装冒烟
- **文件**：`requirements.lock`、`requirements-dev.lock`（重新生成）、`.github/workflows/ci.yml`、
  `tests/test_dependency_lock.py`（增强）。
- **步骤**：
  1. 用项目约定的 `pip-compile` 从 `pyproject.toml` 重生成两份锁（含 `typesafe-sdk`、
     `httpx2`/`truststore` 等传递依赖）。
  2. CI 增加：`pip install --no-index --find-links=<wheelhouse>` 或干净 venv 安装后
     `python -c "import rag.api"`；`pip check`。
  3. 加 lock↔pyproject 一致性测试（解析 `pyproject` 依赖名，断言 lock 中存在）。
- **验收**：干净 venv/容器内 `import rag.api` 成功；`pytest --collect-only` 不再因缺包失败；
  Docker build 成功。
- **回滚**：锁文件单独提交，可 revert。
- **风险**：重生成可能升级传递依赖；固定为当前可用的最小版本集。

### P0-02 B-01：对话记忆租户隔离
- **文件**：`rag/pipeline.py`、`rag/memory.py`、`rag/routers/query.py`、`tests/test_memory*.py`、
  `tests/test_api*.py`。
- **设计**：
  1. 记忆键加 principal：`session_key = f"{user_id}:{session_id}"`，或 `DialogueMemory` 增加
     `user_id` 列/参数；所有读写都带用户。
  2. `/query` 与 `/query/stream` 在使用 `conversation_id` 前先 `get_conversation(cid, user_id)`；
     不存在/不属于当前用户 → 404/403，**不得进入生成**。
  3. 缺省 `session_id=None` 不再落 `"default"`；生成 `user_id` 作用域的临时会话，或要求显式
     `session_id`。
  4. 旧数据迁移：已有 `memory.db` 的 session 行补 `user_id`；无法归属的旧会话只读或清空。
- **验收**：A 用户用 B 的 conv/session 提问 → 404/403，B 的历史不进入 prompt；
  默认会话不再跨用户命中；多轮正常。
- **回滚**：迁移前备份 `memory.db`；功能开关 `RAG_MEMORY_REQUIRE_PRINCIPAL`（默认 true）。
- **风险**：旧会话失忆；迁移需一次性脚本 + 回滚快照。

### P0-03 D-01：素材上传版本 promote / 备份收录
- **文件**：`rag/materials/api.py`、`rag/materials/worker.py`、`rag/document_versions.py`、
  `rag/backups/service.py`、`tests/test_material_api.py`、`tests/test_backup_service.py`。
- **设计（二选一，推荐 A）**：
  - A. 上传在入队前把文件从 `staging/` promote 到 `final_version_path`（复用
    `document_versions` 的 promote），DB `file_path` 指向 final；staging 仅作临时区。
  - B. 备份不再按 `staging` 目录名排除，而是按 `document_versions.file_path` 白名单收录
    已提交版本；仍排除真正的临时/隔离目录。
- **验收**：`POST /materials/upload` → worker 激活 → `create_backup` 成功；
  备份清单包含该版本文件；`create-if-due --trigger deploy` 不因 `referenced_file_missing` 失败。
- **回滚**：备份目录独立，不改现有已备份数据；promote 失败保持 staging 且不入队。
- **风险**：已有走旧路径的版本仍需一次迁移（把 staging 文件 promote）。

---

## 2. P1（升级前应完成）

| 项 | 修复方向 | 文件 | 验收 |
| --- | --- | --- | --- |
| A-01/A-02 熔断降级 | 熔断返回可识别失败（异常/`degraded` 结果）；不改写缓存、不写 memory、不附 reliability、发 degraded/error 事件；补 pipeline 级用例 | `generator.py`、`llm_pool/provider.py`、`pipeline.py`、测试 | 熔断期间：无缓存写入、无 memory 写入、轨迹/事件标记失败 |
| C-01 沙箱边界 | 低权用户/容器 + 无出网 + 只读挂载 + 独立 HOME；或文档明确非安全边界；加 max_rss/CPU | `evolution/sandbox.py`、docs | 子进程读不到 `.env`、连不上网（可用测试环境模拟） |
| C-02/C-03 回放证据 | staleness 对 patched worktree 计算；语料指纹传真值；`seams` 缺失 fail-closed；seam 补丁直接拒回放 | `sandbox.py`、`replay_fixture.py`、测试 | seam 补丁不再得到 `passed`；缺 fixture 字段判 stale |
| D-02 墓碑 | `require_doc_view/edit` org 分支补 `deleted_at`；行数据端点按版本状态过滤 | `api_security.py`、`spreadsheets/api.py`、测试 | org KB 删除后成员读 → 404 |
| D-03 并发编辑 | reserve 事务加唯一 pending session/CAS；第二个请求 409 | `spreadsheet_repo.py`、测试 | 两请求同 base，仅一个 202，另一个 409 |
| D-04 回收清理 | worker 增加 purge job + 30 天扫描；补齐 dataset/fingerprint 清理 | `materials/worker.py`、`recycle.py`、测试 | 到期文档文件/向量/FTS 被清，审计留痕 |
| D-05 Wiki 索引 | 入队 `document_index_jobs` + 重试；delete+add 补偿；content 加 max_length | `wiki/api.py`、`wiki/indexer.py`、测试 | 索引失败可重试；页面最终可检索 |
| E-01 素材 Bearer | 删除 `materials.ts` 的显式 header，统一 cookie；补测试 | `frontend/src/api/materials.ts`、测试 | 归档/确认导入不再 401 |
| E-02 登出清理 | logout/401 集中 reset chat/files store | `MainLayout.vue`、`stores/*`、测试 | A→B 切换看不到 A 数据 |
| E-03 SSE error | 处理 `type:"error"`；失败态/重试；兼容 `data.error` | `stores/chat.ts`、`utils/api.ts`、测试 | 流异常显示错误，不空白 |
| F-02 构建上下文 | 补齐 `.dockerignore`（real-case-handoff/artifacts/scratch/tmp_test_dir）或白名单 COPY；CI 镜像断言 | `.dockerignore`、`Dockerfile`、CI、测试 | 构建上下文不含公司数据；镜像内无这些路径 |
| F-03 首次管理员 | 一次性 bootstrap（创建 admin + 强制改密）；文档写清 seed 与 `ALLOW_INITIAL_PASSWORDS_REMOTE` 冲突 | `start.py`/脚本、`docs/deployment.md`、测试 | 空库按文档可登录；seed 后远程启动不循环 |
| B-02 legacy KB | 启动时无条件迁移；无 policy 的 KB fail-closed 到 admin-only；自检计数 | `org_auth/migration.py`、`api.py`、`api_security.py`、测试 | public 旧 KB 对普通用户不可见 |

---

## 3. P2/P3（随升级或之后）

- **检索/降级**：A-03 背景块角色、A-04 BM25 chunk_kind/tags、A-05 注入词与资料隔离、
  A-06 regenerate 路径、A-07 路由开销/agent 话术、A-08 锁饥饿、A-09 wiki 扫描、A-10 归因。
- **API/权限**：B-03~B-08（is_admin 审计/默认值、认证入口统一、错误脱敏、KB 列表语义、
  DB source 路径白名单、BatchImporter 必填主体）。
- **演化**：C-04~C-11（快照只读、canary 守卫/上限、预算/租约、终态重生、mode-only 补丁、
  ACL 红线、apply 锁）。
- **数据**：D-06~D-12（7z 流式、合并区上限、结构编辑不变式、审计遮蔽、整理撤销、
  FTS 转义、CSV 解析/公式前缀）。
- **前端**：E-04~E-10（管理员路由、超时/取消、竞态、CAS 版本、切库清理、下载 base、XSS 测试网）。
- **运维**：F-04~F-10（SIGTERM、配置生效、健康启动期、测试隔离、CI 真实构建、render、
  脚本供应链）。
- **架构债**：`api.py` 791 行、`pipeline.py` 1547 行、延迟导入 198 处；
  与 UPGRADE Phase 1/2 一起处理，不阻塞 P0/P1。

---

## 4. 实施顺序（建议）

1. **F-01**（1 个提交，先让干净环境能跑）→ 立即验证 `import rag.api` + 全量测试。
2. **B-01**（记忆隔离，P0 安全）→ 先加负向用例，再改 memory/路由，迁移脚本。
3. **D-01**（备份链，P0 数据保护）→ 端到端上传→备份用例。
4. **A-01/A-02**（熔断降级）→ 小改动、高收益。
5. **D-02/D-03/D-04/D-05**（数据一致性）→ 每项独立提交。
6. **E-01/E-02/E-03**（前端可用性/隐私）→ 可并行。
7. **F-02/F-03 + B-02**（构建/首启/迁移）→ 部署前最后一道。
8. C-01/C-02 如升级前要用沙箱作为证据门禁，必须先修；否则只能记录为“非安全边界”。

---

## 5. 总体验收

```powershell
# 干净环境
python -m venv .venv-clean
.\.venv-clean\Scripts\python.exe -m pip install -r requirements.lock
.\.venv-clean\Scripts\python.exe -c "import rag.api"

# 回归
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m ruff check .

# 数据/备份
python scripts\backup.py create --trigger manual
python scripts\backup.py verify <backup-id>

# 前端
cd frontend
npm ci
npm run test
npm run build
```

**放行条件**：P0 全部关闭，P1 至少 A-01/A-02、C-01/C-02、D-02~D-05、E-01~E-03、
F-02/F-03、B-02 关闭；全量测试、ruff、benchmark、locust 0 失败；备份/恢复演练通过。
