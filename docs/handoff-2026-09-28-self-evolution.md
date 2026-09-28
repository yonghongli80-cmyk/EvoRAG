# 交接：自进化五阶段落地（2026-09-28）

面向**新会话**的交接文档。上一个会话从「按 Jev 检索优化交接开工」一路做到
**辅助自进化设计的五个阶段全部落地**。本文只写新会话需要知道的：现状、证据、如何验证、
未完成项与下一步。设计正文在 `docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`，
各阶段计划在 `docs/superpowers/plans/`，各阶段运维说明在 `docs/evolution-*.md`。

## 1. 一句话现状

仓库 `D:\Desktop\RAGv3`（远程 `git@gitee.com:li-yonghong111/ragv3.git`，分支 `main`），
工作树干净，HEAD = `70a2f7c`；1102 服务（`rag.api:app`）在本机健康运行。
后端全量 **1799 passed / 1 skipped**，前端 vitest **204 passed**，`vue-tsc` 与 ruff 干净。
**设计五阶段全部实现、验证、推送。** 未完成项见第 6 节。

## 2. 本机环境（新会话必读）

- Python 一律用 `D:\Desktop\RAGv3\.venv\Scripts\python.exe`；前端在 `frontend/` 用 `npx vitest run`、
  `npx vue-tsc --noEmit -p tsconfig.app.json`。
- `.env`（git-ignored）当前关键值：
  - DeepSeek 通道 = **Command Code**：`RAG_DEEPSEEK_BASE_URL=https://api.commandcode.ai/provider/v1`、
    `RAG_DEEPSEEK_MODEL=deepseek/deepseek-v4.1-flash`、`RAG_DEEPSEEK_AUTH_HEADER=x-api-key`（该平台不用 Bearer）。
  - `RAG_JEV_MODE=active`、`RAG_JEV_KB_ALLOWLIST=rag_docs`（另有三个回放测试库见下）。
  - `RAG_EVOLUTION_ENABLED=true`（观测/诊断/候选基建的开关）。
  - `RAG_EVOLUTION_CANDIDATE_ENABLED=false`、`..._CANARY_PERCENT=0`——**安全默认**（问题集灰度关闭）。
- 服务端口 **1102**（启动：`uvicorn rag.api:app --host 127.0.0.1 --port 1102`，日志 `artifacts/1102.{out,err}.log`）。
  本机管理账号 `bench-admin`（id 1）；会话里用一次性随机密码重置登录（模式见第 5 节脚本）。
- **Qdrant 本地模式互斥**：任何进程内使用（基准、回放、沙箱）前必须停掉 1102，否则 "already accessed"。
- Windows 坑：`git apply` 必须显式 `encoding="utf-8"`（zh-CN locale 会把非 ASCII 上下文写坏，
  导致合法补丁被判 "does not apply"）；PowerShell 里跑 `curl` 传 JSON 要用文件体（`-d "@file"`）。

## 3. 五个阶段交付了什么（含提交范围）

| 阶段 | 提交（部分） | 关键文件 | 验收证据 |
| --- | --- | --- | --- |
| 1 Jev 快链路 | 早期提交 | `rag/jev/{client,policy,service,questions,shadow}.py` | `docs/jev-benchmark.md`：P50 −64%、P95 −30.7%、改写 −100%、路线 1.000、Top-5 1.000 |
| 2 观测闭环 | `b9e3962` 之前 | `rag/evolution/{models,store,recorder,clusterer,worker,metrics,api}.py`、前端进化中心 | `docs/evolution-observations.md`；9 观测→3 聚类→2 候选池 |
| 3 诊断辅助 | `b9e3962`…`638d933` | `rag/evolution/{evidence,diagnoser}.py` | `docs/evolution-diagnosis.md`：开发集 **24/28 = 86%**（≥80% 线） |
| 4 问题集候选 + 五层门禁 | `c5b2d4f`…`e39a339` | `rag/evolution/{candidates,candidate_store,candidate_generator,candidate_gates,rollout}.py`、`rag/jev/candidate_shadow.py`、`scripts/benchmark_candidate_questions.py` | `docs/evolution-candidates.md`：生成→层2 零回归→影子→canary 5%→回滚跨重启生效 |
| 5 代码补丁进化 | `c867541`…`70a2f7c` | `rag/evolution/{code_patches,patch_generator,sandbox}.py` + 审核端点 | `docs/evolution-code-patches.md`：坏补丁被测试门禁拒、好补丁批准导出**未应用** |

**五阶段共同的红线**（改任何一块前先读）：
- 系统永不自动应用/合并任何东西；管理员是最终权威（批准/拒绝/确认根因层级都带审计）。
- 永久禁止被进化修改：权限策略、`rag/api_security.py`、`rag/org_auth/**`、`rag/auth.py`、`rag/user_db.py`、
  **`rag/evolution/**`（系统不得改自己的门禁）**、`tests/**`、`scripts/**`、`config.py`、`frontend/**`、密钥。
- 阈值不得为通过而放宽：路线准确率不降（0pp）、Top-5 降幅 ≤2pp、开发集一致率 ≥80%。

## 4. 回放测试语料（都在 git-ignored 的本地目录）

`real-case-handoff/`（**未入库**，含公司脱敏资料，勿提交）里有：
- `documents/{hr,accessories,after-sales}/`：13 份脱敏真实文档，已灌入本地测试库
  `kb_hr` / `kb_accessories` / `kb_after_sales`（文档级 public）。
- `cases/real-cases-v1-draft.json`：26 条真实问题；`observed-outcomes.json`：旧系统真实表现。
- `label-verification.json`：**全库核查证据**（证明“发货地/盘点范围”内容不存在、
  “副厂配件表”内容存在）——第 3 阶段标签修正的依据。
- 可复用脚本：`setup_kbs.py`（建库灌数）、`replay.py` / `replay_extended.py`（回放）、
  `make_report.py`、`verify_labels.py`、`run_devset_per_observation.py`（逐条诊断开发集）、
  `e2e_candidate.py`（第 4 阶段 e2e）、`e2e_code_patch.py <obs_id>`（第 5 阶段 e2e，参数为观测 id）。
  这些脚本引用 `data/evolution.db` 的既有观测；重跑前注意它们会清库/清候选（脚本内有注释）。
- 仓库内（已入库）：`benchmarks/real-cases-v1-draft.json`、`benchmarks/jev_retrieval_cases.json`（20 用例）、
  `docs/real-cases-v1-replay-report.md`（回放对分报告，含 B 类缺陷证据）。

## 5. 常用操作速查

```powershell
# 全量测试（约 15 分钟）
D:\Desktop\RAGv3\.venv\Scripts\python.exe -m pytest -q
# 进化相关专项
... -m pytest tests/test_evolution_*.py tests/test_code_*.py tests/test_candidate_*.py tests/test_patch_*.py -q
# 前端
cd frontend; npx vitest run; npx vue-tsc --noEmit -p tsconfig.app.json

# 登录管理端（一次性密码模式）
$pw = [guid]::NewGuid().ToString('N').Substring(0,16)
D:\Desktop\RAGv3\.venv\Scripts\python.exe -c "import sys;from rag.user_db import UserDB;from rag.auth import hash_password;db=UserDB('data/users.db');
with db._lock: db._conn.execute('UPDATE users SET password=? WHERE id=1',(hash_password(sys.argv[1]),)); db._conn.commit()
db.close()" $pw
# 之后 POST /login {"username":"bench-admin","password":$pw} 取 token

# 管理端关键端点（全部 require_admin）
GET  /knowledge-operations/evolution/{observations,clusters,state-machine,metrics}
GET  /knowledge-operations/evolution/clusters/{id}/diagnosis
POST /knowledge-operations/evolution/clusters/{id}/decision     # approve_for_candidate|reject|needs_more_evidence|confirm_layer
GET  /knowledge-operations/evolution/candidates                  # 问题集候选
POST /knowledge-operations/evolution/candidates/{id}/canary      # body {"percent":5}
POST /knowledge-operations/evolution/candidates/{id}/rollback
GET  /knowledge-operations/evolution/code-candidates             # 代码补丁候选
POST /knowledge-operations/evolution/code-candidates/{id}/decision  # approve_for_apply|reject|needs_revision
# 前端页面：/operations/evolution →「问题观察 / 聚类与状态机 / 指标看板 / 候选灰度 / 代码补丁」
```

## 6. 未完成项（新会话的待办清单）

**需要管理员（人类）出手**
1. **确认 5 条事实性标签**（相机配件域：real-023/016/003/006/020 的期望答案/来源/路由）。
   没有它，诊断草案停在“假设级”、一致率门无法复算。指南：`docs/real-case-collection-guide.md`。

**真实工程缺口（不依赖人类，可按性价比做）**
2. **沙箱基准回放未生效**：worktree 内无知识库数据（`data/qdrant_data` 不入版本控制），
   回放必然 `exit_1`；当前策略“回放不可用不判死”，所以代码补丁回归判定只靠目标测试。
   修法：把只读 KB 快照挂进 worktree（或指一个隔离 Qdrant 实例）——`rag/evolution/sandbox.py` 第 5 步。
3. **passages 候选无响应级版本归属**：`PassageJudgment` 没有版本字段，
   问题集灰度在 passages 目标上无法从 `decision_metadata` 归因（preflight 已有）。
   修法：`rag/jev/models.py` 加字段 + `client.py` 写回。
4. **代码补丁生成未接进 worker 自动轮次**：现由管理员/集成脚本触发（有意的保守选择，
   但与设计第 3 节“后台链路自动跑”有偏差）。要接就把 `patch_generator` 排进 `worker.py` 轮次，
   沿用 `evolution_candidate_model_calls` 预算与失败隔离模式。
5. **诊断器对“埋没型缺陷”判据偏弱**：开发集里 real-020 家族（副厂配件：内容存在却未被采用）
   被判成 `undetermined`，根因是证据包只给重检索前 24 块的预览、深层块不可见。
   修法方向：证据包加入“问题关键词在召回集合中的覆盖度”确定性信号 + 提示词升版复跑开发集。

**设计第 15 节的后续增强（明确未做）**
6. 多分支探索（DGM 式）、知识库级灰度、快照回退自动化。

## 7. 建议的接续顺序

1. 先做第 6.2 项（沙箱挂 KB 快照）——它让第 5 阶段的门禁真正完整，约 1 小时；
2. 再做第 6.3（passages 版本字段，约 30 分钟）；
3. 请管理员给第 6.1 的 5 条标签，然后复算一致率 + 做第 6.5 的证据增强；
4. 第 6.4（自动生成）需要管理员拍板是否扩大自动化范围；
5. 第 6.6 视需求另立计划。

## 8. 纪律提醒（本项目 CLAUDE.md 之外的经验）

- **先写用例再改代码**；每个新能力都要有失败→通过的 TDD 证据。
- **改共享契约（`models.py`/`store.py`/`candidate_store.py`）要自己做**，不要让并行 lane 各自改。
- **不猜接口**：本会话所有接口都从既有代码/文档读出来的，新会话请保持。
- **不夸大**：本会话纠正过两处自己的误判（real-023 其实答对、标签 ranking_error 其实是内容缺口），
  交接与报告里都保留了纠正记录——请继续这样做。
- 提交风格：`feat:`/`fix:`/`docs:` + 正文说明**为什么**；提交前跑相关专项测试 + ruff；
  全量回归约 15 分钟，改核心路径时必跑。
