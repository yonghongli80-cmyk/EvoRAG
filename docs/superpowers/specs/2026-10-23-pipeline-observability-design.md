# Pipeline 拆分与观测补齐设计（2026-10-23）

用户明确要求继续实施交接中的 pipeline 拆分和观测待办，由 Luna 子代理工作、主代理统筹；Docker 运行验收继续保留。本设计沿用 UPGRADE.md Phase 0 的行为目标，内部模块与计时口径由统筹选择。

用户后续明确调整：pipeline 主文件 1000 行以内可接受，稳定性优先。700 行不再作为硬目标，不为压数字追加迁移、脆弱兼容层或改变业务行为；按自然职责拆分即可。

## 目标与范围
- 将当前 1758 行的 rag/pipeline.py 分为有界职责模块，保留 RAGPipeline / QueryResult 及已使用的模块导出、方法签名和替换接缝。
- 完成最终答案 generation、实际 rerank 的直接计时，从请求到 ExecutionTrace、SQLite details 再到 /metrics 可核对。
- JSON/文本日志统一 request_id、kb_id、route，支持并发、线程池和流式请求，无上下文串漏。
- 查询结果、ACL/版本隔离、结构化/Jev/传统路由、缓存、失败降级、记忆与 SSE 契约不变。新观测仅增加内部 trace/日志/指标数据。
- 不实施 PostgreSQL/共享 Redis/对象存储/多副本、自动 shadow、Docker 运行或迁移，不新增依赖，不调整生产配置和数据。

## 方案与统筹选择
1. 兼容 facade + 职责模块（采用）：入口负责装配/生命周期/兼容委托，准备、Jev、查询执行和诊断分离。显式只读依赖视图在执行时读取 facade 命名空间，已有 patch("rag.pipeline.generate") 等仍作用于真实路径；新模块不反向 import facade。
2. 多重 mixin：机械移动更快，但方法依赖隐含在 MRO 中，仍需解决替换点和循环依赖，难以独立约束职责，未采用。

模块允许继续通过 owner._prepare_context 等原有实例方法协作，保持实例 monkeypatch 和继承覆盖。依赖视图仅作为旧 facade 的兼容桥，不成为任意服务注册表。保留 QueryResult 的原定义路径和 NamedTuple 字段顺序；纯辅助函数可以在 facade 兼容导出。

拆分迁移必须同时继承自动补丁的 ACL 证据门禁：rag/evolution/code_patches.py 的 ACL_CALL_SITE_TESTS 原只列 rag/pipeline.py，新增的 pipeline_bindings/prepare/jev/query/diagnostics.py 也归同一逻辑组件，沿用该文件的权限测试要求。候选路径白名单、禁止路径、配额及沙箱固定权限回归网不放宽。架构性回归确保未来新增 pipeline*.py 不会静默漏出门禁，并验证新模块候选不带权限用例被拒、带用例可正常验证，生成提示/测试选择也继承权限证据。

## 文件与依赖方向
- rag/pipeline.py：公开入口、装配、资源所有权/关闭、CLI、兼容导出和必要薄委托。最终 <=1000 行，稳定性优先，不追求最低行数。
- rag/pipeline_bindings.py：只读、按名称实时读取的依赖视图；显式传入 globals() 的只读视图，不经 sys.modules 或反向 import 查 facade。<=80 行。
- rag/pipeline_prepare.py：输入保护、历史/改写缓存、传统准备、检索/ACL、安全、可靠性、结构化协作。<=750 行。
- rag/pipeline_jev.py：Jev 路由/证据/阶段、shadow 与字段提示。<=550 行。
- rag/pipeline_query.py：query / query_stream / _stream_prepared 的执行与 SSE、trace/记忆编排。<=650 行（含观测）。
- rag/pipeline_diagnostics.py：失败分类、脱敏异常帧、failure observer 构建与提交。<=350 行。
- rag/stage_timing.py：请求内直接计时、线程安全累计、流式 provider 等待计时和 scoped collector。<=220 行。
- rag/logging_config.py：日志上下文与统一格式；rag/api.py 中间件仅管理 HTTP 请求 ID 生命周期。
- rag/tracker.py / rag/routers/system.py：复用既有 JSON schema、stage 白名单与 metrics 展示，不迁移表。

依赖方向：facade -> 职责模块/计时；职责模块 -> 现有低层服务/共享类型。准备与 Jev 彼此协作经 owner 的兼容方法，不形成模块环。所有已支持的 facade 替换点实时取值，不能仅重新导出名字但让真实实现绕过它。保持 _prepare_context_legacy = _prepare_context_standard 的函数身份关系。现有懒加载可选组件和资源归属不变。

## 计时契约
- 使用 time.perf_counter()，毫秒数只来自实际被包围的区间，不以 total_ms - preparation_ms 推导。
- rerank_ms：实际每次 self.reranker.rerank() 的区间累计，含调用内部重试/降级；双路调用计两次，无候选/未调用不造样本。
- generation_ms：RAG 最终答案生成；同步包围 generate() 完整调用（含内部重试），流式对默认生成器、balanced 和 arena 的每次 await anext() 计时累计。包含上游重试等待，不含 yield 暂停时的下游消费等待。arena 是提供事件的合并流等待，不是并行模型时长之和。
- fixed/structured/blocked/cache 等未调用生成器的分支不产生 generation 样本。路由/改写/Jev 及 Agent 内部工具/LLM 调用不计为最终答案 generation；它们继续保留已有 total/tool/Jev 指标，不把 Agent.run 整段伪装成 generation。
- 保持旧 preparation_ms 语义（Jev 准备全段，包含 rerank）。rerank_ms 是嵌套区间，两者不可相加解释总耗时；不重定义历史同名字段。
- collector 每请求独立，可通过 asyncio.to_thread 的复制上下文共享本请求累计器；并行请求不可共用可变状态。准备中抛错仍能取得已发生的计时。直接 _prepare_context 调用也保留实际 rerank 数据。
- 成功/生成异常/熔断/部分 token 后失败都封存真实已发生时长，不制造成功回答、done 或记忆。
- CancelledError 和显式 aclose 必须传播，及时关闭上游 iterator。取消若未落终态则保存一条 route=error、decision_metadata.execution_outcome=cancelled 的部分轨迹，不进入故障演化队列；成功 done/已处理终态之后关闭不得再落取消轨迹。不新增 SSE 事件。
- 准备通过 to_thread 执行时，取消不等待无法中断的底层线程收尾。立即封存 collector：在取消边界计入正在执行区间到此刻的实际耗时，之后迟到的 finish/update 忽略，不修改已保存轨迹。该记录不是后台线程最终成本；测试用事件协调验证取消先返回、线程后完成且无二次落盘/串漏。
- 终态写入先后顺序有界：成功/失败终态已发起持久化（包括 to_thread 正在写库）时，该在先终态拥有本次请求的最终记录权，取消不再追加第二个终态。写库不能被假装成功，非取消上下文中的保存异常照旧传播；Agent 子路径已有的非终态故障观测不被这条规则删掉。增加阻塞保存事件回归覆盖“提交成功 trace 途中取消”竞态。
- 新 execution_outcome 仅允许 success/failed/degraded/cancelled；非法值不落盘。stage 只接受已知键、非 bool、有限且非负数。
- /metrics 新增 stage=rerank/generation，沿用 stage/route/quantile 标签和既有指标名，旧 series 不变。无调用无 duration 样本；无 request_id/kb_id 等高基数标签。既有未知 trace route 仍归 other，保留旧 total_ms 语义。

## 日志契约
- request_id / kb_id / route 为三个独立 ContextVar，缺省 "-"。set_request_id 旧入口保留；新增 scoped helper 用 token 在 finally 恢复。
- 本轮 route 表示有界逻辑执行路由（rag/structured/agent/fixed/arena/blocked/error 等），不是 HTTP 原始路径或任意用户文字；未知归 other，准备尚未决策为 "-"。
- KB 仅来自本次 pipeline 的有效 kb_id（含实例默认库），不从问题/文件/日志正文推断。有效值仅作为日志字段，不进入 Prometheus 标签。
- HTTP 中间件为请求建立/恢复上下文，合法已有 X-Request-ID 保留；空值、控制字符或超长值用新随机 ID，响应继续返回 X-Request-ID。
- 同步查询覆盖整个执行；异步流仅在每次推进/关闭内部 iterator 的上下文中绑定，yield 给消费方前已恢复，不能污染消费方或同任务后续日志。线程池内上下文可用，结束/异常/取消均恢复。
- Jev shadow 自有线程池也应保留本次三个日志字段；只捕获/恢复日志标识，不把整个 Context 一并复制，以免改变业务 rollout/question-set 选择语义。submit/排队/丢弃/关闭和比较结果不变，回调结束后 worker 上下文恢复。
- 日志统一字段只放有界标识与路由，不新增问题、文档名、用户信息、提示词、token、凭据或 provider URL。文本输出对标识中的控制字符安全处理，JSON 正常转义。

## 全局约束
- Python 3.12 和当前锁定依赖，不新增依赖。
- 生产配置/数据/服务生命周期不变；不读取真实密钥或客户资料。
- RAGPipeline 构造/query/query_stream、QueryResult、现有 SSE 和授权/降级/记忆契约不变。
- facade 模块替换和实例方法替换继续作用于真实执行；不得批量改 patch 路径或删弱原业务断言来让重构过关。
- 不抬高既有主干架构预算：api.py<=830；已完成第一阶段 pipeline.py<=1150，后续<=1000（用户明确接受）；局部 imports<=155，最终 ratchet<=150；新模块按上述上限冻结，职责与行为稳定优先于追求更低行数。
- 在 codex/pipeline-observability 隔离分支完成，独立提交与审查；不自动合并/推送共享 main。
- 仅用合成夹具和离线模型替身；由统筹统一跑一次最终后端全量及全树 Ruff，未改依赖/前端时不重复安装或重建产物。
- Docker/Qdrant 运行、认证、持久化、迁移和双进程验收保留待办，不据本轮测试标记已验收。

## 验收
1. 兼容入口/签名/alias 身份和真实 patch 调用证据；原查询/SSE、ACL、Jev、结构化、缓存、失败与生命周期测试保持。
2. 单/双 rerank、同步生成、三条流式生成路径、失败与取消的直接时间可控验证；下游暂停不增加 generation，未调用不造样本。
3. trace JSON 与 /metrics 端到端证明新 stage 可见，旧记录可读、旧指标不退化、标签不含 KB/请求/正文。
4. sync/async/to_thread 并发日志隔离，流式逐次推进、关闭与错误之后恢复；HTTP 响应 request ID 契约保留。
5. 收紧架构预算、后端完整套件、Ruff 和一次最终 Luna 整分支审查；交接/升级文档列出真实结果和仍未验收项。
