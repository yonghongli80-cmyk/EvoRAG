# 运行拓扑契约

本文说明当前支持的单实例运行方式，以及启动、进程存活与依赖就绪之间的边界。修改 `RAG_QDRANT_URL` 只会选择 Qdrant 存储模式，不会启用多实例部署。

## 拓扑矩阵

| 拓扑 | Qdrant 存储 | Redis 行为 | 启动与就绪 |
| --- | --- | --- | --- |
| 宿主机进程 | `RAG_QDRANT_URL` 为空时使用配置的本地 Qdrant 路径；该目录应由单个进程独占。 | `RAG_REDIS_REQUIRED=false` 时，Redis 对就绪状态是可选项；若已配置但不可用，`/ready` 可报告降级但仍返回 200。若 Redis 是运行必需依赖，应设为 `true`。 | 不经过 Compose 启动门禁。`/health` 报告进程健康；`/ready` 检查已配置的运行时依赖。 |
| Compose-local（默认） | `RAG_QDRANT_URL` 默认为空，应用使用挂载到 `/app/qdrant_data` 的本地路径。`qdrant_data` 命名卷挂载期间可写；切换模式后保留该卷，不会使其变成只读或不可变备份。 | 应用使用 Compose Redis 服务。`RAG_REDIS_REQUIRED` 仍控制 Redis 故障是否阻塞运行时就绪。 | Compose 会等 Redis 和 Qdrant 都报告 healthy 后才启动 `rag-app`，即使空 URL 表示应用没有选择 Qdrant server。 |
| Compose-server | 将 `RAG_QDRANT_URL` 设为 `http://qdrant:6333`，应用便使用 Qdrant server 及其持久化卷 `qdrant_server_data`。 | Redis 运行时策略与 Compose-local 相同。 | Compose 仍会等 Redis 和 Qdrant 健康后再启动 `rag-app`。 |
| 未来多实例 | 必须使用共享 Qdrant server，不能使用进程本地的嵌入式目录。 | 多副本、共享缓存或共享锁部署必须设 `RAG_REDIS_REQUIRED=true`，让 Redis 故障阻塞 `/ready`。 | 当前尚未支持多实例。仅设置 Qdrant URL 不足以启用多实例；还必须设计并验证共享数据库、存储和锁方案。 |

## 启动依赖与运行时就绪

Compose 的 `depends_on` 健康条件是**启动门禁**：Redis 和 Qdrant 都健康后，Compose 才启动 `rag-app`。它不会设置 `RAG_REDIS_REQUIRED`，也不决定应用启动后的就绪策略。若依赖在启动时不可用，Compose 会等待应用启动条件满足；若依赖在应用启动后故障，运行时 `/ready` 按已配置的依赖策略处理，其中 Redis 是否阻塞就绪由 `RAG_REDIS_REQUIRED` 决定。

`/health` 是当前 Compose 健康检查使用的进程存活探针。`/health` 成功不代表所有运行依赖都已就绪。`/ready` 才是依赖就绪探针。`deploy.sh` 优先探测 `/ready` 并要求 HTTP 200；只有 `/ready` 不存在（返回 404）时才回退到 `/health`。因此，若 `/ready` 存在但返回 503，部署脚本会继续等待并最终判定门禁失败。

## Qdrant 回滚边界

Compose-local 模式中的 `qdrant_data` 是当前本地路径使用的可写数据卷；Compose-server 模式使用独立的 `qdrant_server_data` 卷。切换 URL 不会让旧本地卷变成不可变存储，也不会生成备份。回滚前应使用经过验证的备份和相应维护流程；不能把保留的可写卷当作只读恢复副本。
