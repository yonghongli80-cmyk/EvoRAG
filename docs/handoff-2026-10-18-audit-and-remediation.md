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

> 2026-10-19 更新：22.9 报出的问题已逐条修复（结构化链路、Top-5 回归、验收渲染、基准用例标签），
> 过程与数字见 `docs/development-log.md` 22.10。下面只列**仍然开着**的。

1. **企业语料的 route 线（0.806~0.875 vs 脚本要求 0.95）——需要产品先定策略，不是 bug**：
   逐例核查后是三个原因叠加：① `real-012/017/018/020` 现在**拒答且带 5–8 个来源**（检索到了、可靠性层
   判"证据不足"），旧路径对同样问题会作答，于是拒答被记成路由错——要"修"只能放宽 `evidence_threshold`，
   属红线；② 判定器逐轮抖动（同一问题 `intent_confidence` 0.19~0.61），±3 样本 ≈ ±4pp 是噪声；
   ③ `real-008`/`real-022` 的标签本身有争议（**旧路径也是 0/3**）。
   **建议**：先确认"证据不足时该拒答还是该作答"，再决定这条线按 0.95 要求还是改口径。
2. **聚合/结构化链路的已知边界**（都不影响安全性，但会影响答案或体验）：
   - 分组结果仍被 `plan.limit` 截断（截的是分组行，无"已丢弃行"标记）；
   - decimal 列的 `min/max` 仍是**字典序**（`'9.9' > '10.1'`），对"最高/最低"类问题是潜在错值；
   - `_execute_multi_lookup` 对每个数据集都用 `selected[0].sheet_key`，非首个数据集的引用可能指错 sheet；
   - `structured_datasets` 的 `sheet_count/total_rows/total_columns` 对既有 10 行仍是 0
     （回填只在镜像为空时补；今后新写入是正确的）；
   - catalog 构建（`has_datasets`）不在 coordinator 的异常兜底范围内。
3. **#30 已登记为"已批准、0% 不进流量"**：状态 `canary`、`canary_percent=0`。独立复测 3/3 passed
   （不回归稳），但**声称的改善未复现**（`route_delta` 三次全 0.0）——可推进，别声称"可测量的改善"。
4. **4 个 `alice_*` 测试账号已中性化**（口令轮换，旧口令登录 401）；要**删除**仍需产品决定。
5. **并发/性能**：只做过只读并发冒烟；**服务端 P99 仍未测**（并发延迟被 20 线程 Python 客户端主导）。
6. 观察窗仍零样本（真实流量决定），4 个聚类与 4 张工单仍需人的领域判断。

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
