# Upstream Response Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将供应商HTTP响应ID摘要与内部请求上下文关联，并保持原有模型调用行为。
**Architecture:** 独立安全事件模块向现有HTTPX/HTTPX2/requests客户端追加幂等response hooks；不替换客户端，不改formatter/DB/API或metrics。
**Tech Stack:** 现有Python3.12、锁定OpenAI/TypeSafe/LangChain、标准logging/hashlib/json/threading；不新增依赖。
**Spec:** `docs/superpowers/specs/2026-10-25-upstream-observability-design.md`

## Global Constraints

- 只写C:/Users/Syf15/.codex/worktrees/post-handoff-remediation/RAGv3；branch codex/upstream-observability-2026-10-25，base9f55db6；不改D main或运行实例。
- 不读取真实.env/业务库/向量，不调用真实模型/外部服务/Docker。验证只用合成数据、MockTransport、既有offline_network_guard、系统Temp短新basetemp、低优先级和BLAS1，最终最多2 worker。
- 不替换transport/client，不改timeout/limits/proxy/SSL/socket、重试、breaker、客户端ownership、token/关闭语义；不读响应正文；保留现有hook顺序。
- 日志仅规格固定字段；无raw ID/headers/body/URL/凭据/动态provider名，无任何ID metrics label。无HTTP响应不伪造事件。
- 保持API<=830、pipeline<=1000、局部导入<=150，所有新imports优先顶层纯模块；不放宽预算，不改schema/ACL/Redis/canary/沙箱。
- root串行Git及公司计时T-D911F87810FD/submit，子代理不commit/stage、计时/记录或派代理；保留工作树和证据。

## Review Focus

- SDK重试先429/500后200，每个物理响应一次事件；连接失败无假响应（Task1/2）。
- 流头先于首token被记录，消费者取消/正文失败不增加调用、不读尽正文、原stream仍关闭（Task1/2）。
- LangChain共享HTTP客户端重复/并发挂接不重复记录，不丢默认缓存/SSL/socket/transport或原hook（Task1/3）。
- header异常、超长/控制字符、bool状态码、logger失败均不泄漏原值或改变调用结果（Task1）。
- TypeSafe HTTPX2重试、rerank HTTP错误及缓存命中无外呼、两请求上下文隔离（Task1/2/3）。

## Execution Context

PY=artifacts/review-backlog-2026-10-24/env/developer-python/Scripts/python.exe；runtime=同env/runtime-python/Scripts/python.exe。PYTHONPATH含repo、artifacts/kb-lifecycle-concurrency-2026-10-24/tools及artifacts/staging-recovery-2026-10-25/verification/tools；pytest先加载`-p offline_network_guard -p staging_test_priority`。basetemp为系统Temp不存在的短`u25xxxxxx`。日志集中artifacts/upstream-observability-2026-10-25/。不重复已验收基线全套，先具体RED，源码冻结后统一完整运行。

### Task 1: Safe event schema and existing-client hooks

**Files:** Create `rag/upstream_observability.py`, `tests/test_upstream_observability.py`。
**Interfaces:** 产出规格的三个attach函数，签名和bool语义完全一致。模块纯导入；实际HTTPX/HTTPX2 hook和requests hook共用安全事件构造。logger=`rag.upstream_observability`，所有payload值和ID校验按spec，不改logging_config。

- [ ] 写明确RED：新模块不存在；真实HTTPX/HTTPX2及requests合成响应，JSON schema/ID哈希/缺失/invalid/status验证（含空白primary fallback、invalid primary不fallback、UTF-8 surrogate、bool状态码）；不访问body的poison stream，原hook顺序、重复及并发挂接、sync/async logging_context隔离、错误mode拒绝、带抛错__eq__的已有hook不影响身份去重，注册和logger普通异常不外溢。
- [ ] 用真实锁定OpenAI+MockTransport证明500/429后成功的全部响应事件、最终APIStatusError、纯连接失败无事件、流式在读取前记录且不提前消费。对比挂接前后同一SDK/_client/transport/timeout/limits/follow_redirects/close身份与参数，不改默认重试以迁就测试（可用Retry-After:0避免等待）。
- [ ] 实现最小模块：锁保护注册、固定回调去重、安全字段白名单/哈希、hook普通异常隔离；unsupported后端安全降级，不全局patch。
- [ ] 新测试GREEN，Ruff/compile/diff；root选择性提交，fresh规格/质量复审通过后Task2/3才能接入。

### Task 2: Native OpenAI clients

**Files:** Modify `rag/generator.py`, `rag/embedder.py`, `rag/llm_pool/provider.py`, `rag/evolution/candidate_generator.py`, `rag/evolution/diagnoser.py`, `rag/evolution/patch_generator.py`；Create `tests/test_upstream_native_clients.py`。
**Interfaces:** 消费`attach_openai_observer(client, asynchronous=False)->bool`，在原构造之后、首次请求之前调用；不向SDK构造器传自定义http_client。同步/异步对应值为False/True。

- [ ] 新集成RED：真实SDK+MockTransport经项目入口调用，generator sync/stream、embedding批次/缓存、pool sync/stream、三个evolution构造均能观测；原返回/token/错误对象、breaker计数、重试次数和关闭行为保持。
- [ ] 只添加顶层helper导入及构造点挂接；不复制事件逻辑、不加额外调用、不改现有请求参数或异常策略。
- [ ] 新测试及generator/embedder/pool/evolution现有相关定向GREEN，Ruff/compile/架构；root提交与fresh规格/质量复审。

### Task 3: Agent, Jev and reranker native clients

**Files:** Modify `rag/agent.py`, `rag/jev/client.py`, `rag/reranker.py`；Create `tests/test_upstream_adapter_clients.py`。
**Interfaces:** Agent在ChatOpenAI构造后挂接`root_client`和`root_async_client`（False/True）；Jev在TypeSafeClient原构造后调用`attach_typesafe_observer`；reranker在requests.Session原构造/配置后调用`attach_requests_observer`。所有client仍惰性创建，现有ownership保持。

- [ ] 新集成RED：实际默认LangChain客户端及共享包装器不被替换、sync/async hook均在；项目Agent构造点实际调用挂接。真实HTTPX2+TypeSafe合成transport验证response ID/重试和缺头不影响mapper；requests合成adapter验证rerank错误/成功均记录、输出结果保持。
- [ ] 仅接入原构造点，不启用include_response_headers、不改SDK日志级别、业务错误分类或投票，不改Session认证与关闭逻辑。
- [ ] 新测试及Agent/Jev/reranker/网络资源关闭相关定向GREEN，Ruff/compile/架构；root提交与fresh规格/质量复审。

### Task 4: Final integration, evidence and handoff

**Files:** Create `docs/upstream-observability.md`, `docs/reviews/2026-10-25-upstream-observability.md`；update本计划、docs/README.md及docs/handoff-2026-10-24.md。

- [ ] root核对任务接口/报告和源码范围，整分支独立复审；统一修复实际发现并冻结源码。
- [ ] 同一冻结源码完整后端2 worker/loadfile/BelowNormal/BLAS1及offline guard，保存退出码、stdout/stderr/JUnit；全树Ruff和生产锁离线SDK/stream/TypeSafe/requests烟测，显式核对生产2.53.0/1.4.2/2.16.0的私有属性、两种hook模式及重试，保留任何失败，不拼接数字。
- [ ] 写事件schema与嵌套JSON解析、ID摘要匹配限制、缺头/无响应边界和SDK私有属性升级检查；历史基线不回填。核对原PID/start/ready/.env哈希，记录真实结果。
- [ ] 根选择性提交代码文档，实际submit当前T-D911F87810FD的唯一中文公司记录并核对成功；保留工作树、环境和原始证据。
