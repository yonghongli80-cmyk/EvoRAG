# 上游 HTTP 响应与内部请求关联观测

## 目标与依据

上一轮真实模型基线的844条上游记录没有采得供应商request-ID摘要。本轮延续用户授权：主代理确定方案，Luna并行实现和独立复查，保护原服务及业务数据。基线为`9f55db6`，既有3207通过/4跳过的完整后端验收仍保留。

三路只读调查位于`artifacts/next-remediation-2026-10-25/audit/`。403 store reset及Compose依赖是现有策略；暂存审计的jobs关联存在Θ(V·J)重复扫描。这些作为后续独立事项，本轮聚焦上游诊断字段。

## 方案取舍

1. 仅在SDK返回后取ID：改动少，但看不到SDK内部重试的中间响应。
2. 为所有SDK替换HTTP客户端：可收集每次响应，但可能改变LangChain缓存、SSL、socket选项和资源生命周期。
3. 在已经创建的原HTTP客户端上追加响应hook：本轮由主代理选择。保留传输和所有现有hook，记录响应头到达时的安全元数据。代价是需要针对锁定SDK的内部HTTP客户端属性进行适配和回归。

OpenAI公开文档确认`x-request-id`及响应/异常ID接口：[API overview](https://developers.openai.com/api/reference/overview)。实际生产适配以本机生产锁环境OpenAI2.53.0、TypeSafe0.7.5、LangChain OpenAI1.4.2、HTTPX2 2.16.0源码为准；既有开发环境另有OpenAI2.54.0、LangChain OpenAI1.5.2、HTTPX2 2.12.0，须独立核对并保留生产锁烟测，不能用开发测试代替生产版本证据。不升级依赖或模型。

## 事件契约

- 独立纯模块`rag/upstream_observability.py`不导入config、UserDB或应用，不创建客户端。
- logger固定为`rag.upstream_observability`。INFO日志message为紧凑JSON；沿用现有日志外层request_id/kb_id/route，不改现有formatter、trace/DB/HTTP/SSE或metrics协议。
- 响应事件固定字段：`schema_version=1`、`event=upstream_http_response`、`operation`、`status_code`、`provider_request_id_state`、`provider_request_id_header`、`provider_request_id_sha256`。
- operation仅`generation/embedding/jev/rerank/other`。HTTPX/HTTPX2的path以`/chat/completions`或`/responses`结束时为generation，以`/embeddings`结束时为embedding，以`/systemone`结束时为jev（锁定TypeSafe SDK的实际端点为`/v1/systemone`），其余other；不读取query或输出URL。requests observer固定rerank。
- ID只允许`x-request-id`和`x-typesafe-request-id`；Jev优先后者，其余优先前者。取第一个非空允许值，保留原始字符串计算UTF-8 SHA256；不把completion ID、内部request_id或文件名当供应商ID。
- ID状态为`present/missing/invalid`。非字符串、超过4096字符、含Unicode Cc控制字符或不能严格UTF-8编码的孤立surrogate为invalid；缺失/空白为missing。先按优先级选择首个非空值，再校验：选中值invalid时保留header名，不再落到次优头；空白值可落到次优头。present输出64位小写hex，其余hash为null。header为实际选中的允许名称，未选中为null。状态码仅接受非bool整数100..599，其余null。
- 不输出原ID、任意headers、正文、问题、向量、URL、认证字段、异常文本或任意provider/model配置名。ID和hash不进入metrics标签或客户端响应。
- 普通观测/日志异常不得改变上游返回、重试、breaker、token或关闭行为；不吞BaseException取消/终止信号。观测失败的降级日志也只用固定字段。
- 不支持的挂接后端返回False，WARNING message为紧凑JSON且仅含`schema_version=1`、`event=upstream_observer_unavailable`、`backend=openai/typesafe/requests`；该告警不伪装为HTTP响应，不打印对象/异常表示。

## 挂接与覆盖

- `attach_openai_observer(client: object, *, asynchronous: bool=False) -> bool`：对锁定SDK的`client._client`追加同步/异步响应hook。
- `attach_typesafe_observer(client: object) -> bool`：对锁定SDK的`client._http_client`追加HTTPX2同步响应hook。
- `attach_requests_observer(session: object) -> bool`：对现有Session的response hooks追加rerank observer。
- 挂接前验证sync/async模式与原客户端匹配，错误模式返回False并安全告警，不能等到SDK请求时因await同步hook而破坏调用。用全局注册锁和回调身份`is`比较保证共享客户端重复/并发挂接幂等，不调用外部hook的自定义相等运算。同一响应只由本观察器记录一次。注册读取、追加及降级告警的普通异常均fail-open；保留原hook顺序和原异常传播，不全局monkeypatch SDK类。
- 不替换client/transport，不改headers、timeout、limits、proxy、SSL、socket、重试或ownership；不调用read/iter/aiter正文，不增加网络调用。SDK内部每个返回HTTP响应的重试均可被hook看到。无HTTP响应的连接失败没有此事件，不补造ID。
- 主生成、embedding、LLM pool、三个evolution客户端在现有构造点接入；Agent在ChatOpenAI构造后对既有root sync/async SDK客户端接入；Jev及reranker在既有构造点接入。
- HTTPX支持的共享hook以固定回调身份去重，不能为每个SDK包装器添加一次重复记录。假客户端或不支持的后端返回False并发出安全的`upstream_observer_unavailable`告警，不影响业务调用。
- ID摘要支持内部关联及对供应商导出ID作同算法比对；不能凭摘要直接让供应商查单。本轮不保存raw ID或声称补回历史844条记录，也不声称已经验证真实供应商会返回这些头。

## 验证与边界

全部测试使用合成凭据、MockTransport/合成response、系统Temp与既有离线guard。证明hook发生在正文消费前、HTTP错误及SDK重试可见、流式取消/部分失败后无额外调用、共享及并发上下文不串线、现有hook和客户端选项/关闭行为保持；输出白名单及泄漏哨兵必须覆盖。

只在C隔离工作树和新`codex/upstream-observability-2026-10-25`分支写入；不重启/切换原8080，不读取真实.env或业务存储，不调用模型/Docker，不修改ACL、Redis可选、canary/shadow或沙箱fail-closed。只允许原服务ready/PID/启动时间/.env哈希的只读前后核对。保留所有已有工作树、环境及证据。

架构预算保持rag/api.py<=830、pipeline.py<=1000、局部导入<=150；新增imports放在模块顶部且不扩大惰性SDK初始化。root统一Git、公司计时T-D911F87810FD及最终记录。稳定源码后以2 worker/BelowNormal/BLAS1完成一次完整后端、全树Ruff及生产锁离线烟测，无前端改动不重复前端套件。
