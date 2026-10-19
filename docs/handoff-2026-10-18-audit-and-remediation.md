# 交接文档：RAGv3 自演化系统（2026-10-18 审计与收口）

> 上一份是 `docs/handoff-2026-10-13-self-evolution.md`。本轮**不加新功能**：项目被交给另一位 agent
> 推进了 17 个提交后，我做的是**审计 + 收口**——把它的成果落进 git、修掉审计发现的真缺陷、
> 纠正夸大文档。过程与证据在 `docs/development-log.md` **第 22 节**。

## 0. 一句话现状

重构（单体 → 模块化）**首次真正提交**，全量套件 **2,223 passed / 0 failed**，服务在重构后的代码上
健康运行，白皮书数字已改为实测值。审计发现的 4 类问题（未提交、套件红、隔离缺失、脱敏退化）全部收口。

| 项 | 值 |
| --- | --- |
| 代码 | `HEAD = 97a0bee`（三个提交：代码 / 前端 / 文档），本轮结束时应已推送到 gitee `main` |
| 后端测试 | 全量 **2,223 passed / 0 failed，23:02**（审计前：2,200 / 9 failed，35:58） |
| 前端 | `vue-tsc` 通过、Vitest **231 passed**（**仍有 1 个偶发**，3 次运行 1 次失败）、build 成功 |
| 服务 | `http://127.0.0.1:8080`，跑**模块化后**代码，`/health` 200；`/openapi.json` = **131 path / 141 ops** |
| 队列 | 候选 29 条：`stable` 2（#20 #29）、`awaiting_verification` 1（**#30**，见下）、`rolled_back` 6、`rejected` 20 |
| 知识库 | `kb_metadata` 4 个真实库（accessories / after_sales / evolab / hr）+ 系统 `rag_docs`；E2E 测试库已清 |
| Wiki | `data/wiki.db` **0 页**（E2E 残留的 3 页已删） |
| 残留 | 4 个 `alice_*` 测试账号：产品**没有删除用户的入口**，无 `org_people` 行可停用 ⇒ 需产品决定 |

## 1. 本轮做了什么（3 个提交）

1. **`fb68237` 把重构落进 git，并修掉它带出的缺陷**。上一手的 `rag/context.py`、`rag/routers/`、
   `rag/repositories/`、`rag/db/` 全部是**未跟踪文件**（工作树 92 路径、+1,444/−10,636），而白皮书
   把这份未提交状态写成"Git main"。提交前修掉：
   * **模块属性 setter 单向失效（真 bug）**：`_ApiModule` 的 setter 只把 `user_db`/`DATA_DIR`/`pipeline`
     转发到 `rag.context`，**不写回本模块 `__dict__`**，而 `rag/api.py` 自己的函数读裸全局 ⇒
     `monkeypatch.setattr(rag.api, ...)` 全部失效。实测后果：启动恢复/素材迁移用例 patch 了临时库，
     `_run_material_metadata_migration()` 却在**真实仓库库**建表回填。加 `_bind_local()` 后 3 条测试
     不改编就转绿。
   * **两套脱敏、弱的那个在生效**：`GET /sources` 走的是 `rag/repositories/` 的副本，其密钥键集合里
     没有 `connection_string`/`dsn` ⇒ DSN 被渲染成 `postgresql://user:***@db/name`。收敛到
     `api_security.mask_source_config` 一处（密钥键整字段 `[REDACTED]`，非密钥 URL 仍掩 userinfo/query）。
   * **Arena 绕过守卫**：竞技场分支提前 `return`，不做 `check_output`、不写 trace、不提交失败观测 ⇒
     竞技场回答**游离在自演化回路与输出审核之外**。现已与单模型分支共用后处理，`route` 保留 `"arena"`。
   * **一条靠真实网络通过的用例**：`test_api_multi_model` 只 patch `rag.llm_pool.get_llm_pool_router`，
     而 pipeline 在 import 时按值绑定 ⇒ 实际发真实 DeepSeek 请求。已补 patch `rag.pipeline.*`。
   * **Redis 阻塞事件循环**：并发门在 async 里用同步客户端，改 `redis.asyncio` + 全 `await`。
   * **死代码**：`wiki/api.py` 的 `_wiki_store` 全局从未赋值（看着像缓存），删除并改测试接缝。
2. **`a6a6950` 前端 + 产物**：`EvolutionCenterView` / `KnowledgeDetailView` / `WikiModeView` 拆成
   `views/evolution|knowledge|wiki` 子组件，`static/` 重新构建。
3. **`97a0bee` 文档**：白皮书逐条改为实测值；`development-log` 第 22 节记录审计；把上一手遗留的
   plans/specs 一并入库。

## 2. 审计结论：上一手交付的质量

**内容：好。** Wiki 子系统（6 个端点**每个都做了授权**、SQL 全参数化）、`rag/db/connection.py` 的
WAL 读写分离池、真实仓储类、路由镜像门禁、9 步 E2E（我在隔离快照上独立复现全绿）都是实打实的。
它还做对了几件关键事：把 `stable` 从"未决"里摘掉（解开我上一份交接点名的死锁）、把 `repeats`
真正接进单候选回放、用多数票复测 #15/#16（各 3 轮全败 ⇒ 维持 `rejected` 公正）、并生成 **#30**：
44 例 3 轮全过，**route +2.3pp、Top-5 +5.0pp、外部调用 −6 次**，现处 `awaiting_verification`。

**交付纪律：不及格。** 大重构未提交、套件 9 红、测试写生产库、E2E 只清一半、白皮书把未提交状态
写成 main 且数字普遍夸大（158 端点→131/141；14 routers→8 模块；"拔除 300+ 延迟导入"→194→196；
4 条 PRAGMA 里 2 条编造；"P99<100ms"被仓库自身压测记录反证）。

**我自己的两处判断也在审计中被纠正**（详见 dev-log 22.2）：① 压测工件是**存在**的
（`scripts/locustfile.py` 已入库、`docs/plans/dev-log.md` 记 P99≈9.7s/36s）；② 我说的"5 个孤儿集合"
里 **4 个在 `kb_metadata` 有注册**——是 E2E 没删干净的测试库，会出现在 UI 列表里。

## 3. 未决 / 需人决定

1. **结构化问答当前不可交付（本轮实测发现，优先级最高）**：数据本体是好的
   （`data/structured_datasets/*/dataset.sqlite3` 里 `sheet_0001..3` 各 50–190 行、`column_manifest` 6–57 列），
   但 ① 路由进不去——5/5 结构化用例被判 `structured_low_confidence`，置信度 0.29–0.71 全部低于阈值
   `jev_route_confidence=0.75`；② 历史 29 次结构化执行**全部** `clarification_required`/`unknown_field`。
   而这条链路对应的前端（表格查看/编辑）是存在且在测试里的。建议：补 `structured_query` 判定样例做置信度
   校准，或改成**显式模式**（用户主动进入表格问答）而不是靠意图分类硬猜。详见 development-log 22.9。
2. **企业语料 Top-5 回归**：旧路径 1.000 → 新 JEV 路径 0.923（允许上限 2pp），`real-008`/`real-012`
   三轮全错。JEV 换来速度（P50 −87%）与特殊类提升（0.231→0.615），但召回有净损失
   ⇒ **不要无条件用新路径替换旧路径**；`settings.jev_mode` 是现成的回退开关。
3. **验收报告渲染缺陷**：多轮档案里 `[PASS] p50 (advisory): 8875.3ms <= 5415.8ms` —— 数字是"更慢"，
   却被渲染成 PASS。advisory 项应明确标"未参与判定"。
4. **#30 已登记为"已批准、0% 不进流量"**（2026-10-19 凌晨）：状态 `canary`、`canary_percent=0`。
   依据：独立复测 3/3 passed（不回归稳），但**声称的改善未复现**（route_delta 三次全 0.0）。
   可推进，但不要对外声称"可测量的改善"。
5. **4 个 `alice_*` 测试账号已中性化**（`UserDB.reset_password` 轮换，旧口令登录 401）；要**删除**仍需产品决定。
6. **并发/性能**：只做只读并发冒烟（20 并发 × 200 请求，两轮全 200、无 `database is locked`）；
   **服务端 P99 仍未测**（并发延迟被客户端主导：顺序 `/health` 4.7ms vs 并发 p50 61–65ms）。
7. 观察窗仍零样本（真实流量决定），4 个聚类与 4 张工单仍需人的领域判断。

## 4. 新增的坑（旧坑清单仍然有效）

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `monkeypatch.setattr(rag.api, "user_db", x)` 后内部仍用旧对象 | 元类 property setter 只转发到 `rag.context`，不写回模块 `__dict__` | setter 里同时 `_bind_local()`；判据：`rag.api.__dict__[name] is x` |
| 测试通过，但实际发了真实模型请求 | 目标在 import 时**按值绑定**（`from x import f`），只 patch 源模块到不了使用方 | 同时 patch 使用方的名字（如 `rag.pipeline.get_llm_pool_router`） |
| 套件慢、且 `data/bm25_index.db` 被写 | conftest 未隔离 `bm25_db_path`/`wiki_db_path` | 已在 `sandbox_api_storage` 里重定向；套件 35:58 → 23:02 |
| Qdrant 集合删不掉、`rmtree(ignore_errors=True)` 假成功 | 本地模式客户端进程内持有目录句柄 | 用 `client.delete_collection()`；仍残留则交给分离子进程延时删 |
| `DELETE /wiki/pages/{id}` 返回 409 | 先删了知识库，授权目标随之消失 | 先删 wiki 页再删 KB；已失去所属库的页直接用 store 清 |

## 5. 下一步建议

1. **处理 #30**（批准 → `run_patch_sandbox` → 灰度）：这是当前唯一有正面实测指标的候选。
2. **修前端 flaky**：定位那 1 个偶发用例，避免以后把"偶发红"当噪声习惯化。
3. **补用户删除能力**（若产品认可）：否则 E2E/演练账号只能越积越多。
4. 复跑 `scripts/locustfile.py` 给重构后的并发结论，替换白皮书里"未测"的表述。

## 6. 接手验证（15 分钟内可完成）

```powershell
git log --oneline -5                       # 期望：97a0bee docs / a6a6950 web / fb68237 code
git status --porcelain                     # 期望：空
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider   # 期望：2223 passed（约 23 分钟，非 15）
curl.exe -s http://127.0.0.1:8080/health   # 期望：{"status":"healthy",...}
.\.venv\Scripts\python.exe -c "import json,urllib.request;s=json.load(urllib.request.urlopen('http://127.0.0.1:8080/openapi.json'));print(len(s['paths']))"  # 期望 131
cd frontend; npx vue-tsc -b; npx vitest run  # 期望：0 错误；231 passed（注意偶发）
.\.venv\Scripts\python.exe -m ruff check rag tests scripts scratch  # 期望：All checks passed
```

## 7. 文档导航

| 文档 | 看它做什么 |
| --- | --- |
| `docs/development-log.md` **第 22 节** | 本轮审计与收口的完整证据（含我自己的更正） |
| `docs/PROJECT_INTRODUCTION.md` | 已按实测修正的白皮书（端点/路由器/PRAGMA/P99/运行前提/完成度） |
| `docs/handoff-2026-10-13-self-evolution.md` | 上一份交接（自演化回路的红线、运维命令、坑清单仍有效） |
| `docs/evolution-code-patches.md` | 补丁层与五层门禁设计 |
| `docs/jev-benchmark.md` | 单轮基准 + 多轮用例集与验收档案 |
