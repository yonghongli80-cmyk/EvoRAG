# 交接文档：自演化系统（2026-10-11 会话）

> 给**零上下文**的新会话用。读完这一份就能接手：现状、红线、运维命令、踩过的坑、未决事项。
> 详细叙事在 `docs/development-log.md`（第 1–14 节按时间记录，含每次测量的证据与失败）。

## 0. 一句话现状

五阶段自演化回路（观测 → 聚类 → 诊断 → 候选 → 五层门禁）已**端到端跑通并第一次真实改动了生产代码**：
`#20` 已提交生效，`#26`、`#27` 已应用（未提交），服务的**一键应用**与**一键回滚**都在真实系统上走通。
当前卡点不在代码，而在**三件非代码的事**：真实流量（观察窗零样本）、评测分辨率（20 例基准测不出提升）、
以及需要人的领域判断（4 个聚类、4 张内容缺口工单）。

| 项 | 值 |
| --- | --- |
| 代码 | `HEAD = 729b5f8`，与 gitee 远端一致，工作树干净（除三位其他 agent 未提交的文档） |
| 已应用未提交 | `rag/agent.py`（#27）、`rag/prompt_manager.py`（#26） |
| 后端测试 | 全量 **2044 passed**（最近一次 10 分 33 秒） |
| 服务 | `http://127.0.0.1:8080`，pid 5444 起于 22:37，`/health` 正常（qdrant + sqlite） |
| 前端 | `frontend/` 构建产物写入 `static/`，由服务在 `/static` 提供（改前端必须重新 build） |
| 队列 | 候选：4 条已批准（#10 #20 #26 #27）、2 条待验证（#15 #16 问题集）；聚类：2 已批准、4 停靠待判断、3 无草稿；开放工单 4 张 |

## 1. 30 秒读懂系统

**五阶段**：线上失败记成**观测**（只存信号，绝不存用户文本）→ 同类聚成**聚类** → 模型给**诊断**（根因 + 建议层）
→ 管理员审阅后产出三类制品之一：**问题集候选**、**代码补丁候选**、**知识缺口工单**（工单不是候选，不进任何门禁）。

**五层门禁**：产物边界（scope）→ 基准（sandbox：应用 + 目标测试 + 固定回归网）→ 影子（宿主 A/B）→
灰度/上线（canary）→ 回滚（rollback）。

**红线（不要碰）**：
1. **系统永不自动合入**：应用/回滚只发生在管理员点击之后；
2. **门禁阈值不放宽**：route 降幅 ≤ 0pp、Top-5 降幅 ≤ 2pp、失败调用不增加；
3. 演化补丁**不得改** `tests/**`、`config.py`、`rag/evolution/**`、`scripts/**`、`frontend/**`
   （**人**可以改 —— 本会话里我作为人修过测试夹具与 `rag/evolution/sandbox.py`）；
4. 观测与轨迹**只存结构化信号**，用户内容（问题原文可以，评论文字不行）不得入库；
5. 无管理员标签时，诊断若断言"库里没有"必须写明**需管理员标签**（`diagnosis-v10` 硬校验）。

## 2. 这一会话做完并且**已验证**的事

每一条都有数字或真实运行记录，细节见 `development-log.md` 第 1–14 节。

1. **诊断质量**：管理员标签 4 → 7 条；开发集（无标签 40 行）**33/39 = 84.6%**；`diagnosis-v10` 无证据护栏生效。
2. **补丁生成成功率 58% → 100%**：基线 7/12（58.3%）→ 发现自伤式超时（生成器默认 90s 截断慢调用）
   → 判断症结是"模型在核对行数算术" → 换成**结构化 edits**（模型只给精确替换块，diff 由 `difflib` 合成）
   → 严格同样本 **12/12 = 100%**（首试 100%、零重试、耗时减半）。**默认已是 edits**，
   回到旧行为只需 `RAG_EVOLUTION_PATCH_FORMAT=diff`。
3. **一键应用**（`POST /code-candidates/{id}/apply`）：三道前置检查（能否应用 / `import rag.api` 冒烟 /
   补丁自带目标测试）→ **全量套件** → 停旧进程 → 等端口 → 拉起 → 轮询 `/health`；任一失败就**撤销并且不重启**。
   实测跑通多次，并在一次真实失败里正确"回滚且不重启"。
4. **一键回滚**（`/code-candidates/{id}/rollback`）：与 apply 对称（必须是已应用；反向之后必须还能导入、
   目标测试必须通过；失败就**把补丁放回**）；已提交的补丁会被拒绝并指名用 `git revert`。
   实测：回滚 #26 把它从运行代码里干净摘掉、#27 原地保留、套件仍全绿、服务健康。
5. **堆放治理**（防"屎山"）：准入改为**按聚类**（一个聚类只留一条未决候选，轮内也挡）；
   审查台出**近重复提示**（按"文件:触及函数"分组）；应用前跑**全量套件**。池子从 19 条候选收敛到 6 条。
6. **用户点踩进回路**：`POST /feedback` 的负向反馈现在会落成观测（`route="feedback"`、带该消息的可答性/置信度、
   只记"有无留言"），已端到端验证（obs#160/#161 已进聚类 #36）。
7. **宿主 A/B 独占锁**：并发争用会被记成候选的失败（实测一侧 26 次调用失败，单独跑两次都是 0），
   现在第二条会被拒绝启动。
8. **沙箱如实化**：环境类拒跑不再写状态（此前会"吃掉"候选）；未跟踪文件不再算主树脏；
   二层对照在沙箱里如实记为"未在此测量"（沙箱无凭据，无 fixture 不跑回放）。

## 3. **未验证 / 未决**（接手第一件事就是别把它们当成已完成）

1. **观察窗零样本**：三个补丁上线后**新增观测 0 条**（最近一条观测 13:56，在上线之前）⇒
   "稳定"或"回滚"都没有依据。窗口要靠真实使用来填（当时的判据：200 次调用或 24 小时）。
2. **#26/#27 未提交 git**：已应用、服务在跑，但只存在于工作树。要不要像 #20 那样提交，等人决定。
3. **#26/#27 的效果不可测**：宿主 A/B 两轮结论翻转（第一轮 legacy 侧 -2.5pp、第二轮 0.0pp）⇒
   那 2.5pp 是噪声（40 样本里的 1 个用例）。诚实说法是"**当前分辨率下既测不出回归也测不出改善**"。
4. **#15/#16**（问题集候选）从未沙箱过，仍在待验证。
5. **回滚后无法通过接口重新应用**：`rolled_back` 是终态，`canary → approved_for_canary` 不合法。
   演练用了一次**审计式人工修复**绕过。诚实修法：① 从同一聚类重新生成候选；② 给回滚加"可恢复"语义（需改状态机，属设计变更）。
6. **4 个聚类的判断**（#1 #2 #5 #15）缺的是**人的领域知识**（那份资料到底在不在库里、能否复现），
   系统已把它们停靠为"需更多证据"；**4 张开放工单**要人补资料。
7. **评测分辨率**：20 例 `rag_docs` 基准只能证明"没变差"，很难证明"变好"。扩基准（含多轮）是唯一的结构性修法。
8. 三份**其他 agent 写的**未提交文档（`docs/rag-system-audit-and-upgrade-roadmap.md`、
   `cybernetic-organism-rag-evolution-manifesto.md`、`architecture-hybrid-knowledge-and-evolution.md`）；
   审计那份我已逐条核验过（点踩那条见 development-log 第 7 节，多轮改写与切块见第 8.3 节；切块那条我最终撤回了“属实”，同时记下三个不成立的指标）。
9. 小卫生问题：接口用例会往 `artifacts/service-control.log` 写行，与真实运维记录混在一起。

## 4. 运维手册（都已实测）

### 4.1 服务

```powershell
# 起服务（端口被占时 start.py 会拒绝启动 —— 这是"假重启"陷阱的来源）
python start.py
# 正确的重启：杀掉旧进程 → 等端口真正释放 → 再启动
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*uvicorn rag.api:app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# 判定"服务是否带上了新代码"：比较进程启动时间与源文件 mtime（我手工重启时被这个骗过两次）
```

**登录**：`POST /login {"username": "evolab-admin", "password": "..."}`（管理员）。
密码见你在会话里收到的那条，**不要写进仓库**；忘了可用 `UserDB.create_user` 重设，或直接用 UI 改。
其他账号：`bench-admin` 等（历史会话创建）。

### 4.2 队列操作（都走接口，留审计）

```powershell
# 批准（应用的前置；apply 只接受 approved_for_canary）
POST /knowledge-operations/evolution/code-candidates/26/decision {"action":"approve_for_apply","note":"..."}
# 一键应用（可选重启）
POST /knowledge-operations/evolution/code-candidates/26/apply {"restart":true,"note":"..."}
# 一键回滚
POST /knowledge-operations/evolution/code-candidates/26/rollback {"restart":true,"note":"..."}
# 聚类诊断决策（approve_for_candidate / reject / needs_more_evidence / confirm_layer）
POST /knowledge-operations/evolution/clusters/1/decision {"action":"needs_more_evidence","note":"..."}
```

### 4.3 测量

```powershell
# 生成成功率（固定样本 + 重复，必须用 --observation-ids 保证可比）
python scripts/measure_patch_generation.py --observation-ids 131,151,153,154,155,156 --repeats 2 --label X

# 宿主 A/B（**独占**，会自动拒绝并发；--kb-id 用 rag_docs，用例绑在那个库）
python scripts/measure_patch_regression_ab.py --candidate-id 26 `
  --snapshot D:\Desktop\RAGv3\real-case-handoff\evolab\ab-snap-18c `
  --kb-id rag_docs --user-id 1 --runs 2 --warmups 0

# 沙箱（应用 + 目标测试 + 固定回归网；带 --fixture 才会跑二层回放）
python scripts/run_patch_sandbox.py --candidate-id 26 --actor-id evolab-admin --note "..."
```

### 4.4 语料快照的配方（**踩过大坑**）

宿主 A/B 与回放需要一个**只读副本**，它必须包含：

```
<snap>/qdrant_data/collection/<5 个集合>/storage.sqlite     # 少了它就报 Collection xxx not found
<snap>/users.db  users.db-wal  users.db-shm                 # 少了 WAL，最近创建的账号读不到
<snap>/memory.db memory.db-wal memory.db-shm                # 少了它，worktree 里退回相对路径 data/memory.db
（不要拷 .lock —— 被占用，且只读副本不该带锁）
```

现成可用的一份：`real-case-handoff/evolab/ab-snap-18c`（已验证：5 个集合 + 三个库连 WAL）。

### 4.5 前端

```powershell
cd frontend; npx vue-tsc -b; npx vitest run; npm run build   # 构建写入 ../static，刷新页面即生效
```

## 5. 坑清单（现象 → 原因 → 处置）

| 现象 | 原因 | 处置 |
| --- | --- | --- |
| `benchmark replay did not complete: exit_1`（过去零信息） | 快照为空/缺集合 | 按 4.4 重建快照；现在报错会带类别（`corpus_missing` 等） |
| 沙箱二层总是"无回归" | 沙箱剥掉 `RAG_*` ⇒ **无凭据**，生成全坏但被兜底，两侧一样坏 | 沙箱不再跑无凭据回放；二层对照去宿主 A/B 做 |
| 补丁在 worktree 里套不上（`patch failed: …:82`） | 我写的文件是 LF，而工作树是 CRLF | 写文件跟随仓库行尾；`--3way` 对无 `index` 行的导出补丁无用 |
| 手工重启后代码没变 | 端口未释放时 `start.py` 拒绝启动，旧进程继续服务 | kill → 轮询端口真正释放 → start；用"进程启动时间 vs 文件 mtime"核对 |
| 进程"零输出、退出码 1" | `os.kill(pid, 0)` 在 **Windows 会 TerminateProcess**（检查谁就杀谁） | 已修：Windows 走 `tasklist`（见 `rag/evolution/host_lock.py`） |
| 一侧出现大量 failed calls | 并发争用（两条 A/B、或 A/B 与全量测试重叠） | 宿主 A/B 现在有独占锁 |
| 沙箱每次都说"主树脏" | `git status --porcelain` 含未跟踪文件 | 已修：只按已跟踪改动判定 |
| 候选被"什么都没应用"的运行写成 rejected | 环境类拒跑被当成候选问题 | 已修：环境类不写状态 |
| 审查台显示"没有数字"，但详情里有结论 | metrics 把 verdict 当原始载荷又比了一次 | 已修：以 `passed` 键识别结论 |
| 本地 `origin/main` 显示 ahead 很多 | 一直用 **URL** 推送，git 不更新跟踪引用 | 用 `git ls-remote <url> main` 核实；或 `git update-ref` 修引用 |

## 6. 建议的下一步（按价值排序）

**我能继续做的**
1. **补"回滚后可重新应用"**：回滚后从同一聚类重新生成候选（无需改状态机），把 14.3 的缺口堵上；
2. **扩评测集**（含多轮）—— 这是"能证明变好"的唯一结构性前提；
3. **修测试污染运维日志**的小卫生问题；
4. **#15/#16 跑沙箱**，让队列里最后两条变成可决策；
5. 读那三份其他 agent 的文档并逐条对照实测（审计那份已核验过一半）。

**只有人能做的**
6. 观察窗：让真实用户用起来（或明确放弃观察窗、直接按"无回归"接受）；
7. `#26/#27` 是否提交 git；
8. 4 个聚类（#1 #2 #5 #15）的判断：那份资料在不在库里？失败当时能否复现？
9. 4 张工单要补的资料（`missing_terms` 就是"该写什么"）。

## 7. 文档导航

| 文档 | 看它做什么 |
| --- | --- |
| `docs/development-log.md` | **本项目的过程与证据**（第 1–14 节），含失败与返工，适合讲给别人听 |
| `docs/plan-2026-10-09-evolution-operations.md` | 运维记录（队列、基线、纠正） |
| `docs/evolution-code-patches.md` | 补丁层设计与 §5.3–5.7（沙箱、上下文预算、edits 格式、一键应用/回滚） |
| `docs/evolution-diagnosis.md` / `evolution-observations.md` / `evolution-candidates.md` / `evolution-gap-tickets.md` | 各阶段设计 |
| `docs/jev-benchmark.md` | 20 例基准的构成与阈值 |
| `docs/deployment.md` / `permission-design.md` / `organization-authorization-operations.md` | 部署、权限模型 |
| `docs/handoff-2026-10-02-self-evolution.md` | 上一份交接（本文件是它的延续） |

## 8. 接手的第一小时建议

1. `git log --oneline -12` 看最近提交；读本文件第 2、3 节（做完的与没做完的）；
2. `python -m pytest -q` 跑一次全量（约 10 分钟，基线 2044 passed）；
3. `curl http://127.0.0.1:8080/health`；用管理员账号登录取 token，`GET /knowledge-operations/evolution/code-candidates`
   看队列；
4. 想动补丁就先读 `docs/evolution-code-patches.md` §5 与 `rag/evolution/service_control.py`（应用/回滚的守卫都在那里）；
5. 要测量就先用 4.4 的配方**核对快照**，否则会重复踩"空快照"那个坑。
