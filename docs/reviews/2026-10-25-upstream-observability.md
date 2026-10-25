# 上游供应商响应关联观测整改（2026-10-25）

状态：实现、三项独立规格/质量复审、整分支复查、唯一修复波次增量复查及root统一代码验收通过；未合并main或部署。产品代码冻结在`73c74d9e8a3f1c029ee67f09813258c25aeb3dae`，最终测试冻结为`f80784c5306e991973186cf78438f8ec6688c43f`；差异仅为测试缓存隔离修复，不影响生产代码或已执行烟测。之后仅修改文档和本机验收辅助文件。

## 范围与选择

用户要求继续由主代理思考统筹、Luna并行推进，并保持现有服务与业务数据。新分支`codex/upstream-observability-2026-10-25`基于上一轮已验收的`9f55db6`，复用C隔离工作树；此前暂存安全修复仍在本地分支链中，未被丢弃或部署到运行实例。

三路只读调查确认：普通403会清空账号级前端数据store但不会注销身份，这是现有实现和测试编码的策略；Compose对Redis/Qdrant的依赖也与权威部署说明一致，需后续单独明确部署策略。暂存审计jobs关联为Θ(V·J)，100/500/1000版本的合成测量约0.41/1.78/3.41秒，属于待优化成本，不能据此承诺生产SLO。本轮选择已确定的供应商request-ID观测缺口，未把这些独立事项混作本轮已修复缺陷。

[设计规格](../superpowers/specs/2026-10-25-upstream-observability-design.md)、[实施计划](../superpowers/plans/2026-10-25-upstream-observability.md)、[操作指南](../upstream-observability.md)为本轮有效入口。SDK标准request-ID来源同时参考[OpenAI API官方说明](https://developers.openai.com/api/reference/overview)，具体适配以已安装且匹配各自lock的SDK源码为准。

## 最终行为

- 新增纯标准库模块`rag/upstream_observability.py`，在原HTTPX/HTTPX2/requests客户端的response hook列表追加固定回调。原客户端、transport、SSL/socket/proxy、超时、limits、重试和资源所有权保持。同步/异步模式错误时安全拒绝挂接；注册以身份比较和锁保证幂等。
- 主生成sync/stream、embedding、LLM pool两种模式、三个evolution闭包、Agent两种现有root SDK、Jev和reranker在原惰性构造点接入。生产构造参数及请求参数未增加`http_client`或响应header暴露选项。
- 新logger的message为固定JSON，HTTP响应事件仅有7个规定字段；供应商ID只来自两种允许响应头并保存SHA256，复用原日志外层request_id/kb_id/route。无正文/原ID/任意headers/URL或动态provider/model名称，不进入DB、metrics、HTTP或SSE。
- SDK内部返回HTTP响应的重试分别产生事件；无响应的连接失败和缓存命中不伪造响应事件。流头先于正文被观察，HTTP200不能被解释为后续流式生成一定成功。
- 普通观测、注册及logger故障不改变模型返回、原异常、breaker或stream关闭；BaseException取消/终止信号保持传播。ID缺失或非法只改变诊断字段，不改变模型结果。

摘要支持内部关联及与供应商合法导出ID作同算法比对，不能凭摘要直接查单，也不能单独证明供应商身份/计费唯一性。没有raw-ID保管或反查功能，没有补回先前844条基线记录，未调用真实供应商确认当前返回头。原第三方SDK日志仍由既有配置控制；本轮只保证新增事件的字段边界。

## 实施与分项证据

| 任务 | 实际结果与独立复审 |
| --- | --- |
| 公共观察器 | 提交`163a6df`。新接口预期RED；共享HTTPX2客户端的两个SDK包装器曾产生2条重复事件，经真实RED后统一同步回调身份，最终28项原生离线测试通过。Ruff/format/compile通过，fresh规格/质量PASS。 |
| 原生SDK入口 | 提交`4979497`。8个构造入口RED均在业务返回成立后缺观测事件；接入后9项新集成及相关/架构组合211通过，Ruff/compile通过，fresh规格/质量PASS。后加的partial-stream用例属于GREEN兼容性守卫，不宣称它有初始独立RED。 |
| Agent/Jev/reranker | 提交`73c74d9`。4项有效RED；新4项、相关223项及架构2项通过，Ruff/compile通过，fresh规格/质量PASS。Agent保留真实缓存client/transport，仅在测试里patch合成handler；Jev验证真实SDK两次请求及缺头mapper，rerank验证三次错误重试与原序fallback。 |

上述定向组合重叠，不可相加为全量数字。根代理统一Git，按文件范围提交；两个消费任务在公共helper批准后并行，未混入彼此施工文件。原始报告和review保留在本计划SDD目录。

整分支复查范围`9f55db6..a6d7b6a`无Critical/Important，提出一项P3：Agent测试关闭了LangChain进程级缓存中的client而未清理该测试条目。唯一修复波次`f80784c`先用原global cache_info变化取得RED，再用真实默认builder的测试局部LRU隔离，保留两个Agent共享和全部close/transport断言，确认原全局函数/缓存统计不变；嵌套finally保证清理异常也恢复原引用。最终Agent目标1通过、adapter文件4通过，Ruff/format/compile通过；唯一增量复查判ADDRESSED、无新问题。未清除其它用例的全局缓存，未改生产代码。

## 根统一验证

- 全树`ruff check .`退出0；新源和测试与冻结提交一致。
- 生产锁离线烟测实际执行9项，4.237秒、退出0。版本为OpenAI2.53.0、LangChain OpenAI1.4.2、HTTPX2 2.16.0、TypeSafe0.7.5，与生产lock一致；开发定向测试另用其对应lock的2.54.0/1.5.2/2.12.0。
- 烟测覆盖真实SDK同步/异步、SSE头先于正文、重试/错误、无响应不造事件、TypeSafe实际`/v1/systemone`、requests及原hook/对象/关闭行为；额外构造生产LangChain默认共享sync/async roots，确认挂接不替换cache/client/transport/SSL/socket状态且无外呼。
- 完整后端第1次执行的会话中断，stdout停在约86%，没有最终退出码或JUnit，不能判为通过；原生进程查询确认测试进程已不存在，原始日志及独立中断核对记录保留，不推断中断原因。
- 第2次在同一冻结`f80784c`完整执行，结果为**3248通过、4跳过**，pytest计时939.77秒、退出0；JUnit共3252项，0失败、0错误，suite计时939.453秒。开始14:24:17、结束14:40:08（Asia/Shanghai）。正常conftest、2 worker/loadfile/BelowNormal/BLAS1、离线guard及全新短系统Temp目录，数字只来自这一次完整运行。
- 四项跳过分别为Windows无法忽略terminate、平台不提供fork，以及两项缺失真实脱敏数据夹具；不把这些用例计为通过。统一汇总确认产品/测试文件及依赖锁相对冻结提交无漂移，生产烟测后产品源未变；生产烟测及两份Ruff日志与原记录SHA256一致。

证据根目录`artifacts/upstream-observability-2026-10-25/`；生产烟测及Ruff位于`verification/`。`preflight-results.json`记录早期烟测门禁，原有`full_backend: pending`是当时状态，不覆盖后续`verification-summary.json`的最终结果；后者包含实际退出码、JUnit统计、版本、冻结等价及日志SHA256。关键原始证据如下，均相对于`verification/`：

| 证据 | SHA256 |
| --- | --- |
| `backend-final-attempt-2.stdout.log` | `a1b7cf1caaf5cb89a725ed4e8b13094e7db35ea23db9ba11655b964b0af8fbcb` |
| `backend-final-attempt-2.junit.xml` | `6c887672037d43a42f650025d3939c567bd54d0d2aff926de14862e2a9c6c655` |
| `backend-final-attempt-2.json` | `988c29fb4f264b3af5ad18efe375003c95b8b4ad8460e62429a496de7c06c007` |
| `runtime-smoke-attempt-1.log` | `2d659a9ae3d3adc7f561b8a23e0bbd124b8f4c8d7952c22ea2d462878a299100` |

原始文件被Git忽略，跨机器交接须另外携带，不能把源码内报告误作原始日志已随Git推送。

## 环境与过程边界

- 受管沙箱进程视图曾将原PID显示为不存在；原生限定查询确认原PID48512和原启动时间仍在，不能把沙箱不可见解释为停服。最终14:40:19再次只读核对，PID48512、启动时间`2026-10-23T14:09:17.6870660+08:00`保持，`/ready`200、SQLite/Qdrant local OK、可选Redis degraded。`.env` SHA256保持`6128C4BD1F1BC0E43AB5189BAD25FA160CB8567B555B1B0CC5921E8771B0E1BD`，隔离树无`.env`；D main为`3f524904e16b19119fe76009c41763b89bbdae8d`且无跟踪文件改动，结果见`verification/service-after.json`。未重启或部署，测试未连接业务存储，也未直接读取或写入业务库/向量文件、调用真实模型。
- managed sandbox的asyncio内部loopback self-pipe会挂住。原生同guard/低优先级零任务探针退出0；新测试保留真实asyncio、SDK dispatch及close覆盖，改用经自动审核批准的精确原生离线命令，没有手工推进协程或改变loop policy伪造通过。
- Task1纯模块使用`--noconftest`，此前autouse fixture的setup错误只涉及合成Temp测试库，未访问业务库。Task2/3及root最终全量均使用正常conftest隔离。
- 目标新代码均已lint。旧`agent.py`/`jev/client.py`无关区块的全文件format差异保持原样；未为通过本轮扩大格式化范围或放宽架构预算。
- runtime prep误起子timer已关闭，没有另提交子任务工作记录；本事项最终由root使用T-D911F87810FD统一实际submit，前一阶段的记录不复用。

## 主代理裁决及代价

1. 本轮聚焦已确定的上游诊断缺口；代价是403/Compose策略和元数据扫描优化仍作为后续独立事项。
2. 在既有client上挂hook，保留默认传输行为并看到重试响应；代价是私有SDK client属性需要在升级时复核。
3. 只存ID SHA256及固定字段；代价是不能凭摘要直接向供应商查单，需受控外部导出比对或另行设计反查。
4. SDD文件操作使用PowerShell/Python等价实现，避开MSYS沙箱named-object失败；代价是root需核对路径所有权和包生成等价。
5. 从已验收本地9f55db6分支继续；代价是未来集成要包含前一轮仍在本地的暂存安全修复。
6. 非空ID先做长度/Cc/UTF-8校验再判断纯空白fallback；代价是tab/newline或超长空白计为invalid而非missing。
7. 在唯一最终修复波次关闭非阻断测试缓存隔离问题，再跑完整套件；代价是增加测试级收尾工作，生产源和SDK传输行为保持。

## 交付和剩余事项

本轮源码仍在隔离分支，没有合并/推送main或部署。完整验收和服务核对已完成，验收文档提交为`824c70365744703fdc30bc433dad09c3c39b4fdc`，后续仅补记录回执。root已使用T-D911F87810FD实际submit本事项唯一公司工作记录，返回`R-20261025-61811CC2`、`pending_review`，本地记录已创建、等待人工复核；不是部署回执。记录包含当前事项的动作、实际验证和限制，未复用前阶段记录或创建子任务重复记录。原工作树、SDK环境、SDD和所有成功/失败日志继续保留。

后续继续处理元数据扫描索引优化、明确403前端数据reset及Compose可选组件/存储拓扑、历史暂存恢复写流程、业务规模/SLO及长期数据访问架构；本轮没有把它们标为完成，也不据这些合成响应验证宣称整个项目已上线验收。

后续按以下顺序拆成独立任务，执行前分别冻结规格和文件范围；本表是待办，不是本轮完成项：

| 顺序 | 范围 | 约束和验收重点 |
| --- | --- | --- |
| 1 | 暂存审计jobs按version预索引 | 将重复关联扫描从Θ(V·J)改成一次建索引后查找；保留行校验、可选表兼容、报告值和顺序。用确定性行访问次数及完整报告对照验收，耗时仅作诊断。 |
| 2 | KB清理目标快照与目录查找 | 先消除同一document的重复版本查询，再单独评估目录成员索引；跨owner引用、归档身份、链接安全检查和错误优先级必须保持，不能只为提速省略全局安全校验。 |
| 3 | 403及Compose策略 | 先写清现有行为与目标契约；区分账号数据清空和注销、应用可选组件与容器启动依赖、local与server存储，不把策略选择直接当作缺陷修改。 |
| 4 | 历史暂存恢复写流程 | 只读audit候选不能直接作为写入授权。先设计明确owner、ACL复核、幂等、回执与失败保留，再考虑任何rehome或cleanup；不自动操作现有业务数据。 |
| 5 | 运行与长期架构 | 真实供应商返回头、业务分布及容量/SLO另做受控验收；数据访问/PostgreSQL、共享Redis、对象存储、多副本及独立worker分阶段设计。既有Docker运行证据保留，不据本轮离线SDK烟测推断新的部署状态。 |
