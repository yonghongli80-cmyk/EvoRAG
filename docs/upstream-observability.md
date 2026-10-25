# 上游 HTTP 响应观测

新事件将供应商返回的request-ID摘要与应用已有的request_id、kb_id、route日志上下文关联。它用于诊断HTTP响应和重试，不代表模型完成成功，也不是计费账单。

## 采集范围

实现接入后，主生成、embedding、LLM pool、Agent、Jev、reranker及evolution生成客户端在原客户端上挂接response hooks。观察器不替换连接/transport、不读取响应正文，也不增加模型调用。SDK内部有HTTP响应的重试会分别产生事件；纯连接失败、DNS/连接超时或缓存命中没有HTTP响应事件。

流式事件在响应头到达时记录，可能早于首token。即使状态为200，后续正文仍可能失败或被取消；判断生成结果需要结合原有执行trace。一次用户请求可以有多个embedding batch、Jev投票或生成/回退调用，因此可能对应多条事件。

## JSON日志读取

沿用应用现有JSON日志配置。原有外层字段不变，观察器的`message`另包含紧凑JSON：

```json
{
  "schema_version": 1,
  "event": "upstream_http_response",
  "operation": "generation",
  "status_code": 200,
  "provider_request_id_state": "missing",
  "provider_request_id_header": null,
  "provider_request_id_sha256": null
}
```

仅处理外层`logger == "rag.upstream_observability"`的记录，再解析message：

```python
outer = json.loads(line)
if outer.get("logger") == "rag.upstream_observability":
    event = json.loads(outer["message"])
    # 关联键来自外层request_id/kb_id/route；供应商ID字段来自event。
```

operation固定为`generation/embedding/jev/rerank/other`。HTTPX端点无法识别时为other；不从任意provider配置名构造日志标签。supplier响应头仅识别`x-request-id`和`x-typesafe-request-id`，缺失不等于供应商从未收到请求。

`provider_request_id_state`为present时，摘要是返回头原字符串严格UTF-8编码后的SHA256（64位小写hex）。missing表示允许头缺失或只有合规空白，invalid表示选中的值类型、长度、控制字符或UTF-8编码不合规范；二者摘要均null。Jev优先TypeSafe头，其余优先通用头。选中的非法值不会被次优头悄悄覆盖；tab/newline及超长空白按invalid报告。

不支持当前后端或模式不匹配时返回安全降级告警`upstream_observer_unavailable`；告警不算HTTP响应。新事件不记录原ID、任意headers、正文、URL、认证材料或动态模型/provider名。原有第三方SDK日志仍由其既有日志配置控制，本功能不启用SDK DEBUG采集，也不声称已经清洗第三方的所有输出。

普通观测或日志故障可以产生仅含固定版本和事件名的`upstream_observer_error`告警；故障不会替换模型结果、原异常或stream关闭行为。

## 关联的限制

- 内部request_id与供应商ID各有来源，不互相冒充；响应正文的completion ID也不是HTTP request-ID。
- 摘要可与供应商合法导出的ID按相同算法比对。仅持有摘要无法向供应商直接查单，也不能单独证明供应商身份或计费唯一性；本轮没有raw-ID保管和反查功能。
- 多个并发上游调用可以共享同一个内部请求上下文。事件逐HTTP响应记录，不提供新的逻辑SDK调用树或attempt序号。
- 本地事件不能补回此前基线中未采得的844条ID，也不代表已经在真实供应商上验证了返回头。
- IDs及摘要只进日志，不成为metrics标签，不进入HTTP/SSE返回或业务DB。

## SDK升级与验收

挂接依赖锁定SDK内部HTTP客户端位置（OpenAI的`_client`、TypeSafe的`_http_client`）以及公开response hook列表。升级SDK时需复查同步/异步模式、客户端/transport身份、既有hook保留、共享挂接幂等、重试与关闭行为，并同时执行开发环境及生产锁烟测。

规格及验证进度见[设计](superpowers/specs/2026-10-25-upstream-observability-design.md)与[实施计划](superpowers/plans/2026-10-25-upstream-observability.md)。当前运行实例不会因源码提交自动获得新观察器；部署状态以最新交接为准。
