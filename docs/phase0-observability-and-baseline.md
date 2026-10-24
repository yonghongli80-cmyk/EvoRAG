# Phase 0 观测面与压测基线档案（2026-10-22）

> 当前宿主条件、10月24日复查待办与隔离验收见 [`handoff-2026-10-24.md`](handoff-2026-10-24.md)。本文旧环境探测和压测数字保留其日期与范围。

本文件是 `UPGRADE.md` Phase 0/1 的实施说明与可复现压测档案。范围：

* Phase 0：只读 `/ready`、只读 `/metrics`、直接重排/生成分段、可复现的 P50/P95/P99 采集命令与基线记录。
* Phase 1（配置面）：`RAG_QDRANT_URL` / `RAG_QDRANT_API_KEY`、`_get_client()` 的本地/远程分支、
  Compose 的 `qdrant` 服务、`.env.example` 说明。
* **早期Docker探测不可用**：当时Phase 1只完成代码/配置/静态测试；当前修复与运行结果以10月24日交接为准，用户业务数据迁移未据此完成。历史证据见第5节。

原则：指标与基线只写**观测到的事实**；没有样本的指标宁可不出现，也不补数字。

---

## 1. `/ready`：依赖就绪

| 项 | 契约 |
| --- | --- |
| 方法/路径 | `GET /ready` |
| 鉴权 | 无（负载均衡/编排探针可直接调用；响应只含组件状态，不含异常文本） |
| 200 | `{"status": "ready", "components": {...}}`，全部已配置依赖可用 |
| 503 | `{"status": "not_ready", "components": {...}}`，任一已配置依赖失败 |
| 缓存 | `Cache-Control: no-store` |

组件与判定（探针均为只读）：

| 组件 | 检查 | 失败细节 |
| --- | --- | --- |
| `sqlite` | 数据库文件已存在时执行 `SELECT 1`（不建表、不写业务数据；文件缺失直接判失败，不顺手建库） | `sqlite_unavailable` |
| `qdrant` | 共享客户端可列举集合（本地模式会暴露目录锁失败；远程模式暴露 server/RPC 失败） | `qdrant_unavailable` |
| `redis` | 仅当 `RAG_REDIS_URL` 非空时 `PING`（连接/读超时 1s） | 默认可选：失败返回 `{"status": "degraded", "detail": "redis_unavailable"}`，**不阻塞** `/ready`；`RAG_REDIS_REQUIRED=true` 时返回 `error` 并 503；未配置为 `{"status": "skipped", "detail": "not_configured"}` |

示例（`mode` 来自 `RAG_QDRANT_URL`，不暴露 URL 本身）：

```json
{
  "status": "ready",
  "components": {
    "sqlite": {"status": "ok"},
    "qdrant": {"status": "ok", "mode": "local"},
    "redis": {"status": "skipped", "detail": "not_configured"}
  }
}
```

`/health` **保持不变**：它是容器 healthcheck 与既有部署脚本依赖的契约（进程 + 依赖快照），
不检查 Redis；未就绪的严格判定归 `/ready`。容器仍用 `/health` 做健康检查，LB/编排可以把
`/ready` 作为摘流依据。

**本机事实（2026-10-22）**：`RAG_REDIS_URL` 已配置但 6379 无监听（探针返回
`degraded / redis_unavailable`，1s 内超时），SQLite 探针为 `ok`。默认 `RAG_REDIS_REQUIRED=false`
下 `/ready` 仍为 **200**（Redis 降级不阻塞）；多副本/共享锁部署应设
`RAG_REDIS_REQUIRED=true`，让 Redis 故障返回 503。单机完全不使用 Redis 时可设
`RAG_REDIS_URL=`（空）让组件为 `skipped`。

---

## 2. `/metrics`：只读运行指标

| 项 | 契约 |
| --- | --- |
| 方法/路径 | `GET /metrics` |
| 鉴权 | **管理员**（`require_admin`；Bearer JWT 或 `X-API-Key` 映射到管理员）。401=未认证，403=非管理员 |
| 格式 | Prometheus 文本格式，`text/plain; version=0.0.4; charset=utf-8` |
| 数据 | 进程内计数器/样本 + 只读依赖探针 + 只读执行轨迹窗口；不写任何库 |
| 隐私 | 只有 method/路由模板/状态码、provider id、组件名、stage 名；**不含**问题文本、文档名、原始路径、URL、密钥 |

指标清单：

| 指标 | 类型 | 含义 |
| --- | --- | --- |
| `rag_up` | gauge | 恒为 1（进程在服务该请求） |
| `rag_http_requests_total{method,route,status}` | counter | 按路由模板聚合的请求量；`route="unmatched"` 表示未匹配到路由（不落原始路径） |
| `rag_http_request_duration_seconds{method,route,quantile}` + `_count`/`_sum` | summary | 最近 512 个请求样本的 P50/P95/P99（nearest-rank）；流式接口只计到响应头返回 |
| `rag_vector_searches_total{result="hit"|"empty"}` | counter | 向量检索次数（`rag/vector_store.py` 的 `search` / `search_collection`） |
| `rag_vector_search_duration_seconds{quantile}` + `_count`/`_sum` | summary | 检索阶段耗时（最近 512 次） |
| `rag_query_traces` | gauge | 从 `memory.db` 最近窗口读到的执行轨迹条数（上限 500） |
| `rag_query_duration_seconds{route,quantile}` + `_count`/`_sum` | summary | 端到端查询耗时（含生成），**按 route 拆分**（`rag`/`structured`/`tool`/`clarification`/`legacy`/`fixed`/`arena`/`blocked`/`error`，未知归 `other`）；缺少 `total_ms` 的历史行不计入 |
| `rag_query_stage_samples{stage,route}` | gauge | 带有该分段耗时的轨迹条数；0/缺失表示"本窗口没测到"，不是"耗时为 0" |
| `rag_query_stage_duration_seconds{stage,route,quantile}` + `_count`/`_sum` | summary | 已落盘的分段耗时：`preparation` / `jev_preflight` / `jev_passages` / `rerank` / `generation`，按 route 拆分 |
| `rag_dependency_healthy{component}` | gauge | 与 `/ready` 同一组探针；1=ok、0=error；`skipped` 的组件不出现 |
| `rag_llm_provider_healthy{provider}` | gauge | LLM 池中该 provider 是否未被熔断 |
| `rag_llm_inflight_requests{provider}` | gauge | LLM 并发门内的在途请求数 |

### 2.1 分段口径与限制

2026-10-23 在 `codex/pipeline-observability` 隔离分支补齐，交付 `6dcca5c` 已并入 main；集成及最终测试结果见
[`handoff-2026-10-23.md`](handoff-2026-10-23.md)。原服务没有因本轮离线验证而重启。

* `rerank_ms` 由 `rag/pipeline_prepare.py` 包围每次真实 reranker 调用，以
  `time.perf_counter()` 直接测量；双次重排累计两次，Jev 回退走同一入口，无调用不产生样本。
* `generation_ms` 由 `rag/pipeline_query.py` 测量最终 RAG 答案生成。同步覆盖完整生成调用；
  默认、balanced、arena 流式累计每次 `await anext()` 的等待，排除 yield 后消费方暂停。
  arena 测量合并事件流等待，不把并行供应商耗时相加。重试/退避在调用内部时计入。
* Agent、改写、路由及 Jev 内部模型调用不计作最终答案 generation；fixed/structured/blocked/cache
  未调用生成器时也没有 generation 样本。沿用缓存路径原有的轨迹行为，不为指标另造缓存轨迹。
* `preparation_ms` 保留 Jev 准备全段、含重排的旧口径；`rerank_ms` 是嵌套区间，二者不可相加。
  `total_ms` 沿用原查询口径，不用 `total_ms - preparation_ms` 推算 generation。
* `rag/stage_timing.py` 提供请求独立、线程安全的累计器；SQLite 仍用原 `details.stage_timings`
  JSON，没有迁移表。计时只接受已知键、有限非负且非 bool 的数值；历史非法分段也不进入指标。
  新 `decision_metadata.execution_outcome` 仅允许 success/failed/degraded/cancelled。
* 取消在没有终态写入时保存一条内部 `route=error`、`execution_outcome=cancelled` 轨迹，
  不写答案记忆、不向失败演化队列提交取消，也不新增 SSE 完成事件。准备线程无法安全中断：
  取消立即封存活动区间截至取消点的时长，不等线程；迟到更新忽略，所以该快照不代表后台最终成本。
  若终态持久化已经发起，沿用在先终态，不因取消再追加一条。已有 Agent 非终态故障观测保留。
  取消轨迹保存失败时记录固定失败阶段与异常类型，不输出异常正文/traceback，不冒充保存成功，
  原取消/关闭行为继续传播。
* `preparation` / `jev_preflight` / `jev_passages` 三个 stage 只有在 Jev 路径
  （`RAGPipeline._attach_jev_stage`）才落盘；`rag_query_stage_samples` 缺失或为 0 时就该按
  "未采集"读，而不是"很快"。
* HTTP 延迟样本是滚动窗口（每路由 512 条），不是全生命周期直方图；跨抓取的聚合交给
  Prometheus 侧，`/metrics` 不自带仓储。
* 执行轨迹的百分位按 `route` 分组后才可比：同一窗口里 `rag` 可能包含慢的准备/生成，
  `fixed`/`error` 往往是毫秒级，混在一起算分位数会得到无意义的"下降"。窗口是最近 500 条，
  不是进程生命周期；`_count` 就是该 route 的样本数。

日志 JSON 与文本均含 `request_id`、`kb_id`、`route`，缺省为 `-`。route 是有界逻辑执行路由，
不是 HTTP 路径；尚未决策时为 `-`，未知值归 `other`。KB 来自有效请求/实例 KB，标识中的控制字符
在文本格式中转义。合法 `X-Request-ID` 保留并回传；空值、控制字符和超过 128 字符的值替换为 UUID。
同步查询覆盖全段，流式仅在每次推进/关闭时绑定，yield 前恢复消费方原上下文。
独立调用 `_prepare_context()` 也建立同步作用域，准备内可更新逻辑路由，返回或异常后恢复父上下文。
`asyncio.to_thread` 与 Jev shadow 线程也带日志字段；shadow 只捕获三个字段，不复制业务 Context。
这些字段仅用于日志，指标标签和原有 HTTP 路由模板统计保持不变。

### 2.2 抓取

```powershell
# 管理员 token（生产不要用默认密码；本机请用你自己的账号）
$token = (Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/login `
  -ContentType application/json `
  -Body (@{ username = "admin"; password = "<password>" } | ConvertTo-Json)).token

Invoke-WebRequest -Uri http://127.0.0.1:8080/metrics `
  -Headers @{ Authorization = "Bearer $token" } |
  Select-Object -ExpandProperty Content |
  Set-Content -Encoding utf8 artifacts/metrics-before.prom
```

```bash
curl -sS -H "Authorization: Bearer $RAG_METRICS_TOKEN" \
  http://127.0.0.1:8080/metrics -o artifacts/metrics-before.prom
```

`artifacts/` 已被 Git 忽略；抓取结果与压测报告都不得提交。

---

## 3. 压测档案：可复现的 P50/P95/P99

### 3.1 运行前提

1. **隔离实例**：压测会创建/删除知识库与素材（`scripts/locustfile.py` 的既有行为），
   不要指向生产库。使用独立的数据目录/端口（例如 `--host http://127.0.0.1:1102`），
   或使用一次性的 Compose 环境。
2. **专用账号**：自注册默认关闭，必须显式提供既有账号：
   `$env:RAG_LOAD_USERNAME` / `$env:RAG_LOAD_PASSWORD`。
3. **Qdrant 锁**：本地 path 模式下，基准/沙箱与运行中的服务争用同一目录锁；要么停服务，
   要么给隔离实例单独的 `RAG_QDRANT_PATH` 副本。Phase 1 的 server 模式（本机尚未验证）
   才是"不停服务跑基准"的方案。
4. **成本**：`/query` 是真实生成调用；用户数 × 时长的组合直接等于模型调用费用，先小后大。
5. **Windows 编码**：本机默认 GBK 时 `python -m locust` 会因解析 `pyproject.toml` 报
   `Couldn't parse TOML file: 'gbk' codec can't decode ...`（2026-10-22 实测）。
   设 `$env:PYTHONUTF8 = "1"` 后再运行。

工具：`locust 2.46.7`（本机 `.venv` 已安装，未写入 `requirements*.lock`；压测工具不随
服务发布，需要时在一次性环境安装同版本即可）、`scripts/benchmark_jev_retrieval.py`。

### 3.2 HTTP 端到端（locust，含生成）

```powershell
$env:PYTHONUTF8 = "1"
$env:RAG_LOAD_USERNAME = "<loadtest-user>"
$env:RAG_LOAD_PASSWORD = "<password>"

.\.venv\Scripts\python.exe -m locust -f scripts/locustfile.py `
  --host http://127.0.0.1:1102 `
  --headless -u 8 -r 2 -t 60s `
  --csv artifacts/locust-baseline --csv-full-history --only-summary
```

`--csv <前缀>` 会产出 `artifacts/locust-baseline_stats.csv`、`_stats_history.csv`、
`_failures.csv`。从 `_stats.csv` 读取 P50/P95/P99（单位 ms；`50%`/`95%`/`99%` 列）：

```powershell
Import-Csv artifacts/locust-baseline_stats.csv |
  Select-Object Type, Name, "Request Count", "Failure Count", "50%", "95%", "99%" |
  Format-Table -AutoSize
```

看结果时必须同时给出：命令、用户数/生成速率/时长、目标接口、`Request Count`、`Failure Count`、
`Requests/s`。单看 P99 没有意义（请求太少时一个样本就能定义 P99）。

### 3.3 生成前准备（benchmark，P50/P95）

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_jev_retrieval.py `
  --cases benchmarks\jev_retrieval_cases.json `
  --runs 5 --warmups 1 `
  --json-out artifacts\jev-benchmark.json `
  --markdown-out artifacts\jev-benchmark.md
```

多轮用例集（指代补全）：

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_jev_retrieval.py `
  --cases benchmarks\jev_multi_turn_cases.json --kb-id rag_docs --user-id 1 `
  --runs 2 --warmups 0 --json-out artifacts\jev-multi-turn.json
```

口径（详见 `docs/jev-benchmark.md`）：准备阶段 P50/P95 为 nearest-rank；脚本**不输出 P99**，
本次也不从 P50/P95 反推 P99。要看准备的 P99 需要给脚本加原始样本输出（未在本次改动内），
在那之前用 locust 的端到端 P99（`/query`）作为尾延迟口径，并明确它包含生成。

### 3.4 本次记录方式

每次压测在下面模板里留一条记录（提交到 `docs/` 时只写事实，不要补算）：

```text
- 日期/提交:
- 机器/容器规格:
- 模式: qdrant=local|server; JEV mode=off|shadow|active
- 目标: /query | /query/stream | 混合; KB/用户:
- locust: users=/spawn-rate=/run-time=; CSV:
- P50/P95/P99（按接口，ms）:
- 错误率 / QPS / 请求数:
- benchmark: cases/runs/warmups; prep P50/P95（old/new）:
- /metrics 摘录: dependency_healthy、llm inflight、query_stage_samples
- 结论/异常:
```

### 3.5 已记录的基线（复用既有数据，未在本次重跑）

| 日期 | 场景 | 工具/来源 | n | 并发 | P50 | P95 | P99 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-21 | `/query`（含生成） | 既有单机基线，`UPGRADE.md` Phase 0 | 20 | 5 | 10.8s | 17.0s | 17.9s | 原始样本/CSV 未留档 |
| 2026-10-21 | `/health` | 同上 | 200 | 20 | 未记录 | 未记录 | 928ms | 轻接口尾延迟 |
| 2026-10-21 | `/knowledge-bases` | 同上 | 200 | 20 | 未记录 | 未记录 | 318ms | 轻接口尾延迟 |
| 2026-10-22 | 混合套件 | `scripts/locustfile.py`，`UPGRADE.md` Phase 0 | 73 | 8 用户 / 60s | 530ms | 13s | 19s | 0 失败，QPS 1.7；生成主导 |

**这些数字不能横向比较**：场景、样本量、账号与数据都不同。它们的作用是给后续回归一个
"同命令、同参数"的对照点。本次 S7 未新跑压测（服务在运行、不改服务生命周期），因此没有
2026-10-22 的新数字；要得到新的 P99，按 3.2/3.3 在隔离实例上重跑并补记录。

---

## 4. Phase 1 配置面

### 4.1 环境变量

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `RAG_QDRANT_URL` | 空 | 空 = 本地嵌入式目录（`RAG_QDRANT_PATH`，进程级目录锁，保持旧行为）；`http://qdrant:6333` = 连 Qdrant server |
| `RAG_QDRANT_API_KEY` | 空 | server 的 API key；空 = 不发 `api-key` 头 |
| `QDRANT_API_KEY` | 空 | Compose 插值变量：应用侧 `RAG_QDRANT_API_KEY` 取自它；启用 server 认证时再取消 `qdrant` 服务里 `QDRANT__SERVICE__API_KEY` 的注释（不要设成空字符串） |
| `RAG_QDRANT_PATH` | `<repo>/qdrant_data` | 本地模式的目录；server 模式下不生效 |

`rag/vector_store.py::_get_client()` 的选择逻辑：

```text
RAG_QDRANT_URL 非空 → QdrantClient(url=..., api_key=<非空或 None>)
RAG_QDRANT_URL 为空 → QdrantClient(path=RAG_QDRANT_PATH)
```

所有 collection 名称不变（`rag_docs`、`kb_*`），因此索引数据不需要重写。

### 4.2 Compose 变更

* `qdrant` 服务：`qdrant/qdrant:v1.18.3`、持久卷 `qdrant_server_data:/qdrant/storage`、
  健康检查、`expose: 6333`（**不发布宿主端口**，只有 Compose 网络内可达）。
* 认证默认关闭（仅内网可达）。启用方式：在 `.env` 设置非空 `QDRANT_API_KEY`，再取消
  Compose 里 `QDRANT__SERVICE__API_KEY` 那一行的注释；**不要**把它设成空字符串——
  Qdrant 会把空字符串当成"已配置的空 key"（[qdrant#8856](https://github.com/qdrant/qdrant/issues/8856)），
  所以本次默认完全不注入该变量。
* 健康检查不用 `curl/wget`：官方镜像出于安全考虑不包含它们（
  [qdrant#3491](https://github.com/qdrant/qdrant/issues/3491)、
  [qdrant#4250](https://github.com/qdrant/qdrant/issues/4250)），因此用 bash 的
  `/dev/tcp/127.0.0.1/6333` 做 TCP 探活。真正的应用级就绪由 `/ready` 的 `get_collections` 验证。
* `rag-app`：`RAG_QDRANT_URL=${RAG_QDRANT_URL:-}`（默认空，维持本地模式）、
  `RAG_QDRANT_API_KEY=${QDRANT_API_KEY:-}`；`depends_on.qdrant.condition=service_healthy`。
* 旧的 `qdrant_data:/app/qdrant_data` 卷保留，用于回滚。

### 4.3 用户业务存量的切换与迁移步骤

10月24日已在独立合成环境验证server认证、持久化、点数/top-5及双应用就绪，见当前交接。
以下仍是用户实际业务数据的切换流程，需要备份与停写安排，不能据合成验收自动勾成业务迁移完成。

1. 在 `.env` 设置强随机 `QDRANT_API_KEY`（URL-safe，非空），并取消 Compose 中
   `QDRANT__SERVICE__API_KEY` 注释；`RAG_QDRANT_URL` 暂留空。
2. `docker compose up -d qdrant`，确认容器 healthy；从 `rag-app` 容器内访问
   `http://qdrant:6333/readyz`（宿主没有发布端口）。注意：本机没有 daemon 时
   `docker compose config` 也会因既有的 `${REDIS_PASSWORD:?}` 缺少值而失败，
   静态校验需要给占位环境变量；这不等于运行时验证。
3. 按集合迁移：优先 Qdrant 官方 snapshot/restore；或 scroll 导出 + upsert。迁移后必须比对
   **每个集合的点数**与**抽样 top-5**（同一问题、迁移前后一致）。
4. 设置 `RAG_QDRANT_URL=http://qdrant:6333` 并重启 `rag-app`；`GET /ready` 应为
   `qdrant.mode=remote`，`GET /metrics` 的 `rag_dependency_healthy{component="qdrant"}=1`。
5. 验收：两个 API 进程可同时检索/写入（不再有目录锁）；`scripts/verify_structured_schema_mirror.py`
   与 benchmark 可在服务不停的情况下运行。
6. **回滚**：清空 `RAG_QDRANT_URL` 并重启；旧 `qdrant_data` 卷保持只读副本一个发布周期。

---

## 5. 本次验证

| 项 | 结果 |
| --- | --- |
| 新增 Phase 0/1 用例 | 33 个：`tests/test_system_ready_metrics.py`（`/ready` 就绪/未就绪/Redis 分支/管理员鉴权/格式/隐私/分段/只读）、`tests/test_vector_store_remote.py`（本地/远程分支）、`tests/test_qdrant_deployment.py`（Compose/env 模板） |
| 相关回归 | 80 个：`test_api.py`、`test_deployment_health.py`、`test_redis_deployment_security.py`、`test_backup_deployment.py`、`test_router_parity.py`（已同步新增 `/ready`、`/metrics`）、`test_vector_store*.py`、`test_stream_eventloop_nonblocking.py`、`test_deploy_script_health.py`、依赖锁用例。合计 113 passed |
| ruff | 本次改动文件全绿；全树当时仍有其它并行工作流正在修改的文件报错（不属于本工作流） |
| Docker | **不可用**：未拉取 `qdrant/qdrant:v1.18.3`、未启动容器、未验证健康检查/API key/持久卷、未执行迁移、未做双进程验收。Compose 已通过 YAML 解析、静态断言与 `docker compose config`（用占位 `REDIS_PASSWORD`/`QDRANT_API_KEY` 做插值校验；`config` 不接触 daemon） |

---

## 6. 后续待办

当前状态（2026-10-24）：Docker engine已恢复，独立合成集合迁移/持久化、双应用默认启动与remote就绪已通过。
下段10月23日的探测保留为历史，当前结果、软件备份和验收证据以 `handoff-2026-10-24.md` 为准。

2026-10-23 环境复核：Docker 29.8.0、Compose v5.5.1 客户端可用，但
`desktop-linux` 的 `//./pipe/dockerDesktopLinuxEngine` 不存在。
已有 Docker Desktop 仅做一次隐藏启动，约 40 秒后进程退出，仍未建立引擎；启动失败根因未定位。
WSL 2.7.14/WSLService 已安装运行，WSL 未列出 Linux 发行版；Hypervisor 与固件虚拟化显示开启。
在临时示例副本上用占位变量执行 `docker compose config --quiet` 通过，未读取生产环境变量，
当时也未拉取/运行容器，用户选择先完成代码整改；10月24日的隔离运行验收已另行完成，原8080业务服务未被用于演练。

1. 合成运行验收已完成；实际业务迁移与上线前仍按4.3执行备份、停写、逐集合校验与回滚准备。
2. main 已补直接 `rerank` / `generation` 分段与 `/metrics` 展示；部署后采集真实负载，
   核对样本覆盖及 P99。离线回归不能代替部署验收或压测，不从旧分位数补造新结果。
3. 若需要准备的 P99，给 `scripts/benchmark_jev_retrieval.py` 增加原始样本输出（或分位数参数）。
4. 生产抓取：给 Prometheus 配管理员 Bearer token；`/ready` 用于摘流，`/health` 继续做容器健康检查。


## 7. 2026-10-23 pipeline 与日志集成验证

在隔离分支源码 `af32f43` 完成全后端：**2879 passed、3 skipped、1 既有警告**，
1695.10秒，退出码0；JUnit 2882项、0 failures、0 errors；全树Ruff通过。
两项跳过因真实脱敏夹具缺失，另一项为Windows不适用的SIGTERM忽略测试。
首次全量中的4项日志断言失败指向独立准备入口的真实route残留，修复后保留断言重新完整执行，
不将分项结果拼成全量。首次失败和最终通过日志均保留在
`artifacts/pipeline-observability-2026-10-23/`；整分支审查和两项收尾修复复审通过。
交付 `6dcca5c` 已由 `8294753` 快进合并到 main 并推送两个远端；合并后 104 项定向回归
及全树 Ruff 通过，源码与完整验收时一致。152 份原始证据已复制到原目录并校验 SHA256，
集成记录见 `artifacts/pipeline-merge-dual-push-2026-10-23/`。
本轮没有新增 P99 压测结果或运行迁移结果；main 文件已更新，现有 8080 服务进程未重启，
只读 `/ready` 为 200，最终部署与运行验收另行执行。


## 8. 2026-10-24 真实模型合成资料基线

源码 `a448d25`，新生产锁环境、普通 lifespan/鉴权/真实 RAG，独立资料与所有存储。200 条串行同步 `/query`：P50 **3.109s**、P95 **4.422s**、P99 **5.016s**、最大 **22.609s**；HTTP/事实/来源检查均 0 失败。200 条生成/准备分段与真实生成外呼、600 条 Jev 外呼均对齐，管理员 metrics 窗口为含预热/试跑的 208 条。传统 rerank 本轮未触发。

采样包含两次发请求前的身份探测安全暂停，完整重核后保留原数据继续；原 8080 PID 与 ready 保持，测试实例已清理。QPS 包含监护与暂停，不代表容量；原始样本、逐请求诊断和局限见 [专项报告](reviews/2026-10-24-real-model-p99.md)。本轮未改产品逻辑，不以旧混合负载或离线烟测数字对比提升。真实业务分布与稳定SLO仍待后续验收。
