# Pipeline 拆分与观测补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 pipeline 拆成兼容且有界的职责模块，完成直接阶段计时和并发隔离日志，并保留 Docker 运行验收。
**Architecture:** rag.pipeline 保留兼容 facade，准备/Jev、查询执行、诊断按职责拆分，经只读实时 bindings 注入旧替换点；请求内计时与日志上下文独立于业务策略。
**Tech Stack:** Python 3.12 / FastAPI / SQLite / pytest / 当前锁定依赖。
**Spec:** docs/superpowers/specs/2026-10-23-pipeline-observability-design.md

## Global Constraints
- Python 3.12 和当前锁定依赖，不新增依赖。
- 生产配置/数据/服务生命周期不变；不读取真实密钥或客户资料。
- RAGPipeline 构造/query/query_stream、QueryResult、现有 SSE 和授权/降级/记忆契约不变。
- facade 模块替换和实例方法替换继续作用于真实执行；不得批量改 patch 路径或删弱原业务断言来让重构过关。
- 不抬高架构预算：api.py<=830；第一阶段 pipeline.py<=1150，最终<=700；局部 imports<=155，最终 ratchet<=150；新模块按设计上限冻结。
- 在 codex/pipeline-observability 隔离分支完成，独立提交与审查；不自动合并/推送共享 main。
- 仅用合成夹具和离线模型替身；由统筹统一跑一次最终后端全量及全树 Ruff，未改依赖/前端时不重复安装或重建产物。
- Docker/Qdrant 运行、认证、持久化、迁移和双进程验收保留待办，不据本轮测试标记已验收。

## Review Focus
- 已构造实例之后才 patch rag.pipeline.generate / rewrite_query，或覆盖实例 _prepare_context，真实执行必须仍走替身（Tasks1–2）。
- _prepare_context_legacy 与 _prepare_context_standard 的 alias 身份、可选资源所有权和异常帧新文件定位不得因委托改变（Tasks1–2）。
- 双 rerank、Jev 拒答、fixed/structured/cache、同步及三种流式生成均仅记录真实调用，不从总时长推生成（Task3）。
- 消费方暂停、部分流失败、done 后关闭、准备线程尚在工作时取消：无重复终态/伪 done，取消快照封存且不等待线程（Task3）。
- 异步流 yield 期间消费方日志、同任务后续请求、to_thread 和并发请求保持上下文独立；标识中的控制字符不能污染日志（Task4）。

### Task 1: 拆分准备与 Jev，建立兼容依赖边界
**Files:** Create rag/pipeline_bindings.py、rag/pipeline_prepare.py、rag/pipeline_jev.py；Modify rag/pipeline.py、tests/test_architecture_budget.py；Create tests/test_pipeline_split_compat.py。
**Interfaces:** PipelineBindings(namespace: Mapping[str, Any]) 提供只读实时名称读取；facade 传 MappingProxyType(globals())，不能冻结 callables 或反向 import facade。迁移函数沿用原方法参数，在内部函数增加 owner/bindings；原 facade 方法签名逐一保留。
**Ownership:** prepare 接管历史路由/用户 session key/改写缓存、rerank、prepare_context/standard、候选检索/ACL/注入/可靠性/structured helpers 与来源过滤；Jev 接管 _jev_active、_structured_field_names、_submit_shadow_case、_prepare_context_with_jev、_prepare_rag_with_jev、_attach_jev_stage。纯 helper 可兼容导出。保留 _prepare_context_legacy = _prepare_context_standard。
- [x] 新建兼容特征测试：保留 RAGPipeline 构造与所有既有方法签名（对照 BASE8294753 AST/inspect，不运行第二副生产实现）、QueryResult 五字段与原路径、alias 身份；实际调用验证构造后 patch rewrite_query/instance prepare 仍生效，不只测 import。
- [x] 将 pipeline 上限先收紧为1150并添加新模块上限（bindings80、prepare750、jev550），在旧代码上记录结构要求 RED。既有行为特征测试应先通过，不故意损坏生产行为来制造 RED。
- [x] 迁移职责并保留薄委托、实例 override 和实时 facade patch 面；不变更路由/ACL/cache/清洗/生命周期逻辑，不新增循环/局部 import 绕行。
- [x] 运行 tests/test_pipeline_split_compat.py、test_pipeline.py、test_pipeline_jev.py、test_pipeline_dual_rerank.py、test_pipeline_multiturn_routing.py、test_pipeline_rewrite_cache.py、test_pipeline_clean.py、test_pipeline_lifecycle.py、test_pipeline_spreadsheet_query.py、test_agent_permission_filter.py、test_architecture_budget.py 及改动文件 Ruff。GREEN 后独立提交与任务报告。

### Task 2: 拆分查询执行与诊断，收紧最终架构预算
**Files:** Create rag/pipeline_query.py、rag/pipeline_diagnostics.py；Modify rag/pipeline.py、tests/test_pipeline_split_compat.py、tests/test_architecture_budget.py。
**Interfaces:** 沿用 Task1 PipelineBindings。query、query_stream、_stream_prepared 的参数/生成器形态不变；内部函数通过 owner 调用 prepare、failure、memory/gap 等原实例接缝。QueryResult 仍定义于 rag.pipeline，内部由 bindings 取得。诊断包括原 failure factory/分类/提交与异常帧；原辅助符号在 facade 保留。
- [ ] 扩展兼容回归：构造后 patch generate / generate_stream、覆盖 _prepare_context，实际 query 与 SSE 走替身；失败轨迹使用真实新模块异常帧且不包含异常正文；不削弱现有 SSE/error/reliability/资源归属断言。
- [ ] 将 facade 上限改700，新 query650、diagnostics350，局部 imports ratchet150；记录旧 Task1 布局 RED。
- [ ] 移出 query/stream 与诊断，保持同步/异步调用顺序和失败降级；异步薄委托显式关闭内层 iterator，兼容导出保持；可移动本轮涉及的安全纯 import 到模块级达到预算，不抬高预算、不动无关生产逻辑。
- [ ] 跑兼容/预算、test_pipeline.py、test_pipeline_stream.py、test_pipeline_lifecycle.py、test_pipeline_arena.py、test_stream_eventloop_nonblocking.py、test_stream_error_event.py、test_stream_done_reliability.py、test_stream_kb_target.py、test_breaker_degradation.py、test_structured_outcome_observability.py、test_refusal_reasons.py、test_memory_principal_isolation.py、test_redacted_price_evidence.py 及改动文件 Ruff。GREEN 后独立提交与报告。

### Task 3: 直接阶段计时、失败/取消轨迹与指标闭环
**Files:** Create rag/stage_timing.py、tests/test_pipeline_stage_timing.py；Modify rag/pipeline.py、rag/pipeline_prepare.py、rag/pipeline_jev.py、rag/pipeline_query.py、rag/pipeline_diagnostics.py、rag/tracker.py、rag/routers/system.py、tests/test_tracker.py、tests/test_system_ready_metrics.py、tests/test_architecture_budget.py。
**Interfaces:** stage_timing.StageTimings 的 start(stage)->opaque token、finish(token)->None、snapshot()->dict[str,float]、seal()->dict[str,float]；snapshot 返回已完成区间，seal 立即把当前活动区间截至封存点计入并冻结，后续更新忽略。use_stage_timings(timings) / current_stage_timings() / ensure_stage_timings() 用 ContextVar 绑定/恢复；measure_call(stage, callable, *args, **kwargs) 与 measure_stream(stage, AsyncIterator) 执行真实调用并保证 finally 收尾。同步全段和异步每次推进/关闭作用域绑定，不能把请求 collector 泄给消费方。
**Contract:** 保存 rerank_ms / generation_ms；新执行结果字段 execution_outcome 仅success/failed/degraded/cancelled。保留 preparation_ms 旧含重排口径，不相加。最终答案 generation 不覆盖 Agent/路由/改写/Jev 内部调用；arena 计合并事件流等待，不求并行模型之和。
- [ ] 写 RED 行为回归：单/双 rerank 精确累计、无候选不造样本；普通与Jev prepared 都带真实重排时长；同步生成和 fixed/structured/blocked/cache 的有/无样本；可控 perf_counter 和离线 provider，不 sleep 拼墙钟。
- [ ] 写 RED 流式回归：默认/balanced/arena 的逐次 anext 时间累计；模拟消费暂停不计 generation；异常/熔断/部分 token 后失败保留实测，未成功不得写答案记忆或伪 done。
- [ ] 写 RED 取消回归：首次 yield 后 aclose、provider await 中取消、准备线程被事件阻塞时取消、done 后关闭、终态 to_thread 保存被事件阻塞时取消。取消重新传播，上游关闭；未终态保存一条 cancelled 部分trace、不进入FailureObserver；封存含活动区间，取消不等后台准备线程，释放线程后无二次落盘/改动封存值/污染另一请求。终态写入已经发起时由在先终态拥有最终记录权，不追加取消终态；非取消上下文的保存异常仍传播，既有Agent非终态观测保持。
- [ ] 在 tracker 允许新 stage 和有界 outcome；非法键、bool、负值、NaN/inf/非数值不入计时；SQLite表结构与旧列/JSON键不变。/metrics 输出两个新增 stage、沿用旧 series 与标签，不新增 request_id/kb_id 标签。端到端读真实临时SQLite，核对旧preparation/Jev与新stage样本。
- [ ] 实现请求计时/实际调用钩子与封存，保持同一失败只经既有适当路径记录；直接 private prepare 也有collector；不以差值/整段Agent充当生成耗时。stage_timing上限220。
- [ ] 跑新 timing、tracker、system metrics、兼容/架构、pipeline/stream/dual_rerank/Jev/arena、breaker、eventloop、done reliability 与 structured observability；改动文件 Ruff。GREEN 后独立提交与报告。

### Task 4: 请求日志字段与并发/流式上下文隔离
**Files:** Modify rag/logging_config.py、rag/api.py、rag/pipeline.py、rag/pipeline_prepare.py、rag/pipeline_jev.py、rag/pipeline_query.py；Create tests/test_logging_context.py；必要时扩 tests/test_pipeline_stage_timing.py。
**Interfaces:** 保留 get_request_id()/set_request_id()；新增 get_kb_id()/get_route() 和 scoped logging_context(*, request_id=None, kb_id=None, route=None)。None表示保留当前值，显式"-"表示未知；内部 token finally reset。日志 route 是逻辑执行路由，与HTTP metrics route模板不同。Task3 的每次iterator推进/关闭作用域叠加日志绑定。
- [ ] 写 RED：JSON/文本格式都有 request_id/kb_id/route；未知默认"-"；合法请求ID保留，空/控制字符/超过128字符的ID替换随机ID并回响应头；KB日志控制字符安全处理，不新增正文/身份/凭据字段。
- [ ] 写 RED：两个并发请求在 async 与 to_thread 中各有正确字段；同步/异常后恢复；同一task交替推进两条流、yield期间消费方日志、aclose/取消/done后恢复；嵌套上下文回到原值，不只恢复默认值。用事件/barrier控制，不用sleep。
- [ ] 实现中间件请求上下文和 pipeline有效KB/已知逻辑route上下文；rerank/生成/诊断日志能看到本次标识；准备未判路由时"-"，未知路由other；非pipeline日志缺失字段用"-"。不修改SSE、请求授权或现有有效ID响应契约。
- [ ] 跑新日志、stage timing、pipeline/stream、system metrics、eventloop、multi-model API/请求ID相关回归和架构预算；api<=830、局部imports<=150，改动文件 Ruff。GREEN 后独立提交与报告。

### Task 5: 集成验收、文档与整分支审查（统筹）
**Files:** docs/handoff-2026-10-23.md、docs/README.md、docs/phase0-observability-and-baseline.md、UPGRADE.md、本计划；不增加生产行为。
- [ ] 所有分项独立审查通过后，统一跑完整后端 pytest（保存日志/JUnit/退出码）及全树Ruff；按最终结果记录跳过和既有警告。仅当新变更/失败/疑点存在时重跑相关测试。
- [ ] 实测架构行数/局部imports，更新文档代码完成项、模块职责、直接计时/嵌套preparation/流式背压/取消口径和日志字段。Docker运行、其它架构阶段与自动shadow仍为待办；不捏造P99压测或部署结果。
- [ ] 打包8294753..最终HEAD整分支差异，fresh Luna审查规格/质量/安全与真实验收。若有问题按一轮集中修复、一次定向复审处理；保存Ruling与范围取舍。
- [ ] 保留可审阅分支；核对原main/生产配置未改；交付结果与剩余事项，按公司规则实际submit本任务工作记录并检查返回。
