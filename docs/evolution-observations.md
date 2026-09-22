# 自进化观测中心（第 2 阶段）运维说明

本文件覆盖人工主导的知识库自进化系统的**第 2 阶段（观测基础）**：失败观测的落库、聚类、
后台 worker 与管理端只读页面。第 2 阶段**不生成任何候选修改、不调用诊断模型、不触碰发布
门禁**；诊断辅助（第 3 阶段）、低风险候选与隔离代码进化（第 4/5 阶段）见路线图，
实现计划见 `docs/superpowers/plans/2026-09-22-assisted-self-evolution.md`。

设计约束来源：`docs/superpowers/specs/2026-09-21-assisted-self-evolution-design.md`。

## 1. 能力与边界

- 在线问答链路在 tracker 落库后，把**权限过滤后的可诊断轨迹**异步提交给观测记录器；
  `submit` 有界非阻塞，队列满即丢弃并计数，绝不阻塞或改写在线响应。
- 被安全策略拦截（`route="blocked"`）、权限不足（`permission_limited`）的请求**不产生观测**，
  只进入跳过计数；一次偶发的外部故障同理。
- 观测库只存信号：知识库、失败阶段、根因特征、状态、优先级、块哈希、分数、版本、
  枚举、计数与时间戳。**不存文档块文本，不透出问题文本**（问题文本留在 `execution_logs`，
  管理员在同一处读完整轨迹）。
- 聚类按 `sha256(kb_id | 失败阶段 | 根因特征)` 确定性聚合；同类根因累计达到阈值（默认 3）
  自动进入候选池（`awaiting_diagnosis_review`，待第 3 阶段诊断审核）。
  `undetermined`（无法确定）的聚类永不自动晋升。
- 第 2 阶段所有管理端端点**只读**：没有任何端点能改变状态机；管理员显式标记
  （rejected / needs_more_evidence）随第 3 阶段开放。

## 2. 开关与配置（均为 `RAG_` 前缀环境变量，可写在被忽略的 `.env`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RAG_EVOLUTION_ENABLED` | `false` | 总开关。关闭时 worker 不启动、观测不落库、读取返回空页，**不创建库文件** |
| `RAG_EVOLUTION_DB_PATH` | `data/evolution.db` | 观测库路径（已被 `.gitignore` 覆盖的 data 目录） |
| `RAG_EVOLUTION_QUEUE_LIMIT` | `256` | 记录器有界队列长度 |
| `RAG_EVOLUTION_CLUSTER_THRESHOLD` | `3` | 同类根因累计多少次进入候选池 |
| `RAG_EVOLUTION_CLUSTER_WINDOW_DAYS` | `30` | 聚类统计窗口 |
| `RAG_EVOLUTION_OBSERVATION_RETENTION_DAYS` | `90` | 观测保留天数，worker 每轮清理 |
| `RAG_EVOLUTION_WORKER_ENABLED` | `true` | 与总开关串联的 worker 开关 |
| `RAG_EVOLUTION_WORKER_INTERVAL_SECONDS` | `900` | 每轮间隔 |
| `RAG_EVOLUTION_WORKER_RUN_SECONDS` | `120` | 单轮墙上时钟预算 |
| `RAG_EVOLUTION_WORKER_MAX_BATCH` | `200` | 单批处理观测数 |
| `RAG_EVOLUTION_WORKER_BUSY_LOAD_FACTOR` | `2.0` | 繁忙判定：load1 > factor × CPU 数时暂停当轮（Windows 无 `getloadavg`，需显式 provider 或管理员 `pause`） |

开启示例（本机 `.env`）：

```dotenv
RAG_EVOLUTION_ENABLED=true
```

备份：`data/evolution.db` 纳入既有备份策略（随 `RAG_BACKUP_ROOT`，默认保留 8 份）。

## 3. 管理端页面（进化中心）

- 入口：左侧导航「🧬 进化中心」（仅管理员可见），路由 `/operations/evolution`，
  支持深链与硬刷新（SPA fallback 已覆盖 `/operations/` 前缀）。
- 三个页签：**问题观察**（筛选/分页，无问题文本列）、**聚类与状态机**
  （候选池/观察队列分组、成员详情、转移表图例）、**指标看板**。
- 后端端点（全部 GET、全部 `require_admin`，非管理员 403、未认证 401）：

| 端点 | 说明 |
| --- | --- |
| `GET /knowledge-operations/evolution/observations` | 观测列表，筛选 `status`/`pool`/`kb_id`，分页（默认 20，上限 100） |
| `GET /knowledge-operations/evolution/clusters` | 聚类列表，筛选 `pool`/`status`/`kb_id` |
| `GET /knowledge-operations/evolution/clusters/{cluster_id}` | 聚类详情 + 成员观测；不存在 404 |
| `GET /knowledge-operations/evolution/state-machine` | 状态/池/转移表与只读端点清单 |
| `GET /knowledge-operations/evolution/metrics` | 观测指标与口径定义 |

## 4. 指标口径（第 2 阶段最小集）

响应形如 `{"window_days", "counts", "rates", "definitions"}`；分母为 0 时比率返回 `null`。

| 指标 | 口径 |
| --- | --- |
| `problem_solve_rate` 问题解决率 | 窗口内进入候选池的聚类数 ÷ 窗口内聚类总数（第 2 阶段即"累计达标待诊断"） |
| `generalization_rate` 同类问题泛化率 | 窗口内已聚类观测中，所属聚类窗口内成员 ≥2 的占比 |
| `wrong_answer_rate` 错误答案率 | 已回答观测中带负向反馈（not_useful/outdated）或冲突记录的占比 |
| `refusal_accuracy_rate` 拒答准确率 | 带反馈的拒答观测中获 useful 反馈的占比 |
| `avg_latency_ms` 平均延迟 | 窗口内观测 `total_ms` 均值 |

`definitions` 中每项带 `available_from_phase`：引用正确率、结构化计算准确率（第 3 阶段）、
旧能力回归数、请求与候选成本、人工核验通过率（第 4 阶段）、灰度回退率（第 5 阶段）
在第 2 阶段**显式缺席**，不返回占位值。

## 5. 运行方式

- 服务内：worker 随 lifespan 启动（`RAG_EVOLUTION_ENABLED=true` 时），停机时 join 线程并关闭自有库连接。
- 独立进程：`python -m rag.evolution.worker`（关闭态为不触库 no-op；注意因包内扁平导入，
  该入口会打印一行 runpy RuntimeWarning，行为正常，可忽略）。
- worker 单轮异常只记日志不退出循环；预算（并发/批大小/运行时长/模型调用）命中只停当轮；
  繁忙跳过当轮且不动已入队观测；第 2 阶段模型调用上限恒为 0。

## 6. 回退

1. `RAG_EVOLUTION_ENABLED=false` 并重启（或停掉独立 worker 进程）——观测停止、页面显示空态，
   在线链路逐字节不受影响。
2. 需要抹掉历史：停机后删除 `data/evolution.db`（可从备份恢复）。
3. 完全移除：回退到本阶段之前的提交即可；无索引依赖，无需重建。

## 7. 已知限制

- 第 2 阶段纯只读：候选池只能靠"同类根因累计三次"自动准入；管理员手动标记待第 3 阶段。
- `store` 三条连接（recorder / worker / API）已设 30s busy timeout，极端并发下仍以观测丢失
  （计数）优先于阻塞在线请求。
- 根因特征是确定性子特征（6 项），语义诊断由第 3 阶段的 `EvolutionDiagnoser` 生成草案。
- Windows 上无 `os.getloadavg`，繁忙判定需显式 provider 或管理员 `pause()`。

## 8. 验证基线（本阶段完成时）

- 聚焦测试：evolution 五个测试文件 69 passed；API 集成（api/spa/lifecycle/startup/backup/operations）67 passed。
- 前端：vitest 182 passed；`vue-tsc --noEmit` 干净。
- 全量后端回归与 ruff 以合并前最后一次全绿运行为准（见提交记录）。
- 关闭态实测：`/metrics` 401（未认证）、管理员访问返回空指标、`data/evolution.db` 不创建、worker 不启动。
