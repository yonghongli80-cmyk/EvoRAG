# 401/403会话与Compose拓扑契约

用户总顺序第三阶段第一专项。基线为本地第二阶段闭环`02c9192cd35b9e6263b56f5449bc98a325e35996`，已发布main仍为`1d413ed`；保持隔离，不部署、不变更真实会话或容器。

## 会话契约与已确认缺口

当前API与auth已带authSessionGeneration，旧账号的成功/401/403响应不会复活或清空新账号。403是现有策略：清空chat/files两个共享账号store、提示权限不足，保留AuthUser与登录状态，不跳转login；组件局部状态仍由其自身处理，不声称清理所有浏览器内存。

当前普通请求或stream的401仅重置chat/files并尝试跳转，AuthUser只在/me探针catch或logout才清空。已经处于/login或导航受限时，普通401会留下isAuthenticated=true；本轮用真实拦截器RED确认，再使当前会话401同步清空AuthUser并标记sessionChecked=true，然后沿用原数据重置/跳转。旧会话401/403仍忽略，403不清身份；不改后端cookie、权限、错误payload、重试或请求参数。

新测试须验证当前普通/stream401、/me401、当前403清共享store但保留身份、旧401/403普通/stream不影响新用户或新store、迟到成功不能覆盖、取消和网络错误不触发注销。保留现有generation逻辑和通知错误隔离，不新增token本地存储。

## Compose契约与文档修正

维持三服务单机Compose：redis与qdrant必须healthy后启动rag-app；RAG_QDRANT_URL为空仍使用本地卷，非空使用server；RAG_REDIS_REQUIRED决定运行时/ready的Redis门禁，与Compose启动依赖是两个生命周期环节。/health是存活，/ready才是依赖就绪；现有Compose健康检查仍用/health，deploy.sh已有/ready优先门禁。

本轮不改服务、端口、volumes挂载标志、依赖、健康检查或环境值。只纠正“qdrant_data卷为只读副本”的不实注释：当前挂载可写，local模式使用它；server切换后保留旧卷不能冒充不可变备份，实际回滚须按已验证备份/维护流程。host开发、Compose-local、Compose-server及未来多实例矩阵写入专门契约及deployment文档；多实例不因改一个URL而自动受支持，仍需共享DB/缓存/存储设计。

## 验证与隔离

frontend/node_modules实际是指向D的junction，禁止直接在该依赖树测试或构建。复用C私有env/frontend-copy/node_modules，package-lock哈希须一致；使用已安装Node22和新的artifact源码镜像，构建默认../static落到该artifact内。不得npm install/改锁/重建原static。

Node preload guard拒绝所有非loopback连接及端口8080，设置BelowNormal；假Axios/fetch及Vite测试自己的随机loopback端口可用。Vitest单worker，源码原子patch后同步私有镜像再运行。Compose只作YAML语义等价和使用显式合成.env的config渲染，不触碰Docker引擎或真实.env。保留全部失败/成功日志，root统一复审、验收和实际工作记录。
