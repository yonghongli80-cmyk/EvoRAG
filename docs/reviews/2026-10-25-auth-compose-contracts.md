# 401/403与运行拓扑契约（2026-10-25）

状态：独立任务复审和整分支复审均APPROVE/PASS，冻结`e868649`的根验收通过。基线为本地第二阶段`02c9192`，分支`codex/auth-compose-contracts-2026-10-25`；D main及两个远端仍为`1d413ed`，本专项未合并、双推或部署。

## 最终行为

[设计](../superpowers/specs/2026-10-25-auth-compose-contracts-design.md)、[计划](../superpowers/plans/2026-10-25-auth-compose-contracts.md)、[会话契约](../auth-session-contract.md)和[运行拓扑契约](../runtime-topology-contract.md)为有效入口。

当前会话普通Axios或stream 401在原代际判断之后同步清除AuthUser并标记sessionChecked，再沿用chat/files重置和跳转；不依赖浏览器实际跳转来清身份。当前403仍清chat/files、提示拒绝并保留身份/路由；旧会话401/403或迟到成功不能污染新账号，取消/网络错误不注销。后端cookie、权限、请求、重试、API返回及生产utils/api.ts保持。

Compose解析值及env示例非注释赋值完全保持。契约区分host、Compose-local、Compose-server和未来多实例；明确启动depends_on健康门禁与运行RedisRequired/readiness不同、/health不替代/ready，以及qdrant_data在local模式可写、保留旧卷不等于不可变备份。只纠正注释与文档，不改服务/端口/挂载/依赖/默认URL或healthcheck。

## 分项验证

- Auth提交`8db245f`，真实Axios/store的RED为11项中9通过、2个新增当前普通/stream401失败（身份仍为Alice）。生产改动5行，最终auth.interceptor/auth/api三个文件47项通过、日志无导航警告。原警告被单独定位到未改生产API的两个旧单元用例，使用局部location替身和实际href断言并在afterEach恢复；不是全局吞日志。独立复查APPROVE，无发现。
- Topology提交`4faa4ce`，四个既有部署/readiness文件34项通过；local/server显式合成.env的Compose CLI config渲染均退出0，无引擎/容器动作。YAML映射与env非注释行对基线相等，文案修正后重新计算hash。独立复查规格/质量PASS，无发现。默认Python缺jwt及一次错误工具路径/CLI插件发现失败只作为环境驱动日志保留，未伪造产品RED。
- Node私有镜像122个跟踪文件，依赖C普通目录，锁hash`A8DF9E18D4616147EEDF34C46D200D7E8923D2096BB2BE2BBF56A88E97D44E83`一致。Node22.23.4、单worker、BelowNormal与网络guard：非loopback及8080拒绝，测试IPC/随机loopback允许。初版准备的cwd遗漏及normalized tuple假阳性在任何产品测试前被root发现并修正；真实Node归一化参数现在按指定保护错误码检查，priority失败即终止。
- 最终runner在私有frontend cwd执行，写开始时PID/start/hash及运行元数据，独立stdout/stderr和结束真实exit/hash保留。source hash只读镜像的Git跟踪源码，不递归依赖junction。vue-tsc第一次root执行退出0，前后frontend hash均`DC92BFDEF9940AC7B41ECC4D92CB2C089A465175C4D3B50B2C6F602466B5D8CE`。

根验收：私有Node22单worker完整前端 **41文件330项通过**，234.18秒；vue-tsc及Vite生产构建均实际退出0，构建7.95秒、产物仅写本专项artifact/static。相关Python部署/readiness/Auth六文件 **42通过**，30.28秒、JUnit无失败错误；全树Ruff通过。定向结果有重叠，不相加。全部Node根验收的跟踪镜像源码前后hash相同；原frontend/node_modules指向D，所有测试/构建使用artifact私有镜像和C依赖，原C/D static不重建。

18:10:07只读检查：原服务PID48512及启动时间保持，/ready 200，SQLite/Qdrant local OK、可选Redis degraded；D.env哈希保持，D main仍1d413ed且无跟踪改动，C树无.env。这是原服务保护核对，不表示新代码已部署。原始元数据、stdout/stderr、JUnit及汇总保留于`artifacts/auth-compose-contracts-2026-10-25/`。

## 根裁决及代价

1. 当前401同步清本地身份，再执行原重置/跳转；代价是任何当前401（含登录失败）均保守重置客户端，服务端cookie权威不变，403不注销。
2. 403范围仍是chat/files共享store，旧会话错误忽略；代价是一次权限拒绝可清其它共享缓存，组件局部状态仍由自身负责。
3. 不改Compose解析语义，只修正文档；代价是默认local模式仍等Qdrant服务健康，启动依赖与运行可选Redis需分别理解。
4. 使用私有Node22镜像/C依赖，拒绝D junction；代价是root需要同步文件和核对私有构建路径。
5. 注释/文档采用已有守卫与一次性语义/渲染对照；代价是契约声明逐项映射已有检查，不新增镜像配置字面的冗余测试。
6. 容量目标异步向用户核实，用户表示不清楚并请求建议；后续按单机试点、合成1000/5000/10000文档和并发1/2/4作工程假设，代价是不能冒充业务事实或正式SLO，多实例/迁移仍需独立门禁。

## 交付与接续

本专项未合并/双推/部署，唯一timer`T-D4CB7672EA41`实际submit成功，工作记录`R-20261025-EE897C45`（pending_review），验收文档提交`395577a`。继续总顺序中的历史暂存恢复写流程，再做容量/SLO与长期数据架构；本专项不是整个用户请求终点。
