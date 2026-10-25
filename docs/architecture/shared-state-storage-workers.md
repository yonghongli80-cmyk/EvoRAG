# ADR：共享状态、对象存储与独立 Worker 准入

**状态：提案，尚未实现、部署或运行验收**  
**日期：2026-10-25**  
**前置依赖：** [数据访问与 PostgreSQL ADR](data-access-postgresql.md)（由 Task 2 提供）；本 ADR 不修改其内容。

## 决策摘要

保持当前 SQLite 与单 API 进程的运行方式。多 API/独立 worker 必须等权威 DAL 与 PostgreSQL 迁移方案先完成实现和验证，再建立跨进程任务准入、写目标 fencing、共享 BlobRef 与跨存储恢复。不能仅把 Redis 标为 required、把本地目录改成对象存储 URL，或增加副本就宣称安全。

在共享拓扑启用前：答案缓存维持关闭；所有涉及共享 ACL、租约或状态的组件在依赖缺失/过期时 fail-closed；API 与 worker 的部署角色、缓存开关和对象存储接口都属于未来设计，当前配置没有可用的独立角色命令。

## 现状盘点（implemented）

- `config.py` 有 Redis URL、`redis_required`（默认 false）、素材 worker lease 时长、文档版本本机根目录和备份本机根目录配置。`/ready` 可对已配置 Redis 执行 ping，Redis 故障在 required=false 时按 degraded 处理。它只是健康门，不会令所有 Redis 使用者 fail-closed。
- LLM pool 的 Redis 并发计数/RPM 失败时会回退到进程内 semaphore/window；配置未设置 Redis 时也是内存路径。因此两份 API 可各自准入请求，不能把该 gate 当分布式写入锁或持久 fencing。
- 问题改写缓存（默认启用）驻留进程内。BM25 数据快照由 `BM25Store` 持久化到本机 SQLite，`Retriever` 另将已加载 chunks/索引放入进程内字典缓存；后者没有跨进程失效协议。全局答案缓存当前没有启用（`pipeline_prepare.py` 中明确令 `cached=None`）。这些性能缓存均没有共享 ACL/data epoch 协议。
- `document_index_jobs` 在 SQLite 中保存素材任务及 owner/expiry；claim 允许过期 lease 被重新领取，worker 会续租，并以 owner 条件 settle。此机制是已实现的任务租约，不是由每个 SQL/blob/vector 写目标共同校验的单调 fencing token。API lifespan 当前启动素材 supervisor、备份 scheduler 和 evolution worker，并在关闭时停止它们。
- 素材任务消费者按 job type/scope 分派 wiki reindex；`claim_material_jobs` 是跨文档与 wiki job 的通用队列入口，不是 wiki 缺失分派的 bug。文档版本文件使用本机路径，已有 content hash、staging/promote/recovery、删除 tombstone/恢复窗口/延迟 purge，以及校验备份 manifest 和文件 checksum 的本机备份流程。
- Qdrant 客户端同时有本地目录与 URL 两种配置路径；URL 路径和 Compose 服务只表明代码/部署配置存在，不代表本轮已运行 server 或证明应用级多副本写一致性。当前 SQLite、文件目录、Qdrant 各自不是一个跨存储原子事务。

逐文件证据及状态见 [shared-state-inventory.json](../../artifacts/capacity-architecture-2026-10-25/shared-state-inventory.json)。

## 设计提案与不变量（proposal）

### 1. 依赖次序与失效行为

1. 先交付 Task 2 的 DAL/PG：事务边界、版本 CAS、可见性/ACL 查询、队列 claim/settle 与审计写都走同一个权威数据库接口。对照行为、迁移、停写、回滚及新写入回放门禁通过后，才可启用第二 API 或 worker。epoch 维护依赖同一 Unit of Work（UoW）/事务能力。
2. `sharedRequired` 是未来拓扑配置概念，不是现存环境变量或启动命令。启用时启动检查必须确认权威 SQL、共享 Blob、Qdrant endpoint、Redis/租约与 ACL/data epoch 依赖均配置且可用；任何必需依赖未就绪则 readiness 失败、共享请求/写入拒绝，不降级至本机 cache、路径、锁或内存 semaphore。关闭时保留明确的单机模式，不把单机 readiness 推广成多副本认证。
3. 权限判定始终重新以权威 SQL ACL 为准。缓存命中前后验证 user 身份/状态与授权，不能用缓存命中绕过撤权；无法取到 ACL/epoch 时拒绝返回证据或生成答案。

### 2. 权威 epoch 与事务原子性（硬性准入门禁）

ACL epoch 与 data/visibility epoch 存在权威 SQL 中，不以 Redis key、进程内计数或异步通知作为真值。每个影响授权或可见性的写入口都必须经过 Task 2 UoW：成员关系、角色、owner/scope/归属及 ACL 策略变化，与对应 ACL epoch 递增在同一 SQL 事务原子提交或一起回滚；文档新增、版本切换、删除/恢复、索引可见性等 data visibility 变化，与对应 KB data epoch 递增在同一事务原子提交或一起回滚。覆盖 API、管理/批处理、worker、迁移和修复入口；遗漏入口即不准启用共享缓存。模型与 prompt/template 版本作为键中显式版本，不冒充数据库 ACL epoch。

同事务 outbox 只负责通知副本清理旧 key、刷新本地观察水位或触发重建；它不是 epoch 推进的替代方案。通知延迟、丢失或 Redis 故障都不能影响权威 epoch 正确性。请求读取以权威 SQL 当前授权及 epoch/版本构造 cache key；提交响应前，在一个明确的授权准入点重新查询本次响应所需的 ACL 与 epoch，并要求读到的提交水位不落后于最新权威值。无法确认当前授权、查询失败或副本/缓存观察水位落后时，拒绝命中并 fail-closed。旧 epoch 下迟到写入的 cache entry 可留待过期/清理，但新请求按新 epoch 构 key，永远不能取到它。

普通非流式响应的授权点是响应提交前最后一次权威 SQL 检查；在该检查之后发生的撤权不能追回已提交/已发送的响应。新请求必须看到提交后的撤权。流式响应以建立流时的权威检查作为准入点，并在每个尚未发送的证据/答案块提交前重新验证授权与所需 epoch；撤权后停止后续块并结束/标记为权限失效，已经发出的块不承诺撤回。网络中已发送但客户端未收到的数据也视为已发送，不能声称可被撤销。

### 3. 缓存键与失效

任何未来共享查询/答案缓存键至少绑定：`kb_id`、`user_id`（或可证明等价的授权主体集合指纹）、ACL epoch、KB data epoch、模型版本、prompt/template 版本、规范化问题及历史摘要 hash。检查 ACL 后生成 epoch 有界的缓存键；命中时仍做权威授权检查。ACL 授权/撤销、文档新增/版本切换/删除/恢复、prompt 或模型变更均推进相应 epoch。Redis 不可用、epoch 读不到或同步失败时关闭命中并按共享请求 fail-closed；不能返回旧答案。现存改写缓存与 BM25 缓存继续作为进程内性能缓存处理，若多副本启用必须明确版本戳/失效协议，不能误当授权缓存。

### 4. 持久租约与 fencing

权威 PG 中每个工作分区/资源持久保存递增 fencing generation、lease owner、expiry、attempt 和状态变更；每次新 claim（包括过期接管）在事务中原子增加 generation。worker 取得 `(resource, owner, generation)` 后，每个副作用写入都携带 generation，并由 SQL 条件更新、blob 提交服务和向量写入协调层拒绝小于当前 generation 的写。仅在 SQL settle 时检查 owner 不够：旧 worker 可能在 lease 过期后仍写文件或 Qdrant。

Redis lease/TTL 只用于降低争用或短期协调，不能替代持久 generation；租约过期可能因暂停、网络分区或故障转移让两个 worker 同时认为自己持有锁。fencing 必须由最终写目标验证，或所有写经能验证 token 的协调器提交。无法令外部写目标拒绝旧 generation 时，该目标在迁移窗口只能单写者/停写，不能声称 fencing 完整。

### 5. 不可变 BlobRef、条件写与补偿

统一不可变对象身份 `BlobRef = (tenant/kb, document_id, version_id, sha256, byte_length, object_key)`；key 由受信 ID/hash 构造且包含版本身份，上传新版本不覆盖旧 key。服务端先对流式上传独立计算 SHA-256/长度，校验配额、MIME/格式与授权，再用“仅当 key 不存在”的条件写；已有对象只有在下载复算 hash、长度吻合时才视为幂等成功，冲突不能覆盖。ETag 不得当通用 SHA-256 证明。PG 只在 blob 校验完成后通过事务更新版本指针/引用并写 outbox；outbox 投影 worker 幂等更新 Qdrant/搜索索引，失败重试并由 reconcile 对账。

SQL 中的版本/引用关系是权威。对象清理由引用计数/全局引用扫描确认无所有者、无未完成事务/outbox、无保留义务后才可执行；共享对象不能因一个文档删除而物理删除。删除先写 tombstone 并拒绝读取、取消/栅栏旧任务、撤销当前版本可见性；满足恢复期后生成 purge outbox，保留 tombstone 与操作身份/补偿记录。删除与恢复并发通过版本 CAS 决定唯一胜者，失败需可重试、可审计；对象删除支持延迟和补偿，不把跨存储操作伪装为原子事务。

### 6. Worker 拆分

将来 API 只处理请求，material/evolution/backup worker 角色需有显式、经代码实现并验证的配置；本 ADR 不给出当前可运行的 role flag 或命令。共享 DB、blob、vector、outbox 和可验证备份全部就绪后才可单独部署。任务至少一次投递，消费者按稳定 job id + generation + 输入版本幂等；重复消息不得重复创建版本或重复删共享对象。lease 到期、进程崩溃后由新 generation 接手，旧 worker 的 SQL、blob 与 vector 副作用必须全被拒绝。周期任务以数据库协调/唯一任务日历键去重，不能依赖每个 API 的本地 scheduler 单例。

## 故障矩阵与上线门禁（proposal，均待实现/运行）

| 场景 | 必须满足的行为 | 可检查的准入证据 |
|---|---|---|
| 两个 API 并发读写同一 KB | 两者读同一 SQL ACL/data epoch 和 BlobRef；撤权后旧 API 不可从自身热缓存继续返回；并发版本提交由 CAS 串行化，唯一当前版本。 | 两 API 独立进程、同主体撤权/改版本竞态；核对所有响应与 SQL 审计、epochs；权限泄露和双当前版本为零。 |
| epoch 与权威变更提交/回滚 | ACL/成员/角色/归属写和 ACL epoch、数据可见性写和 data epoch 均在一个 UoW 中原子提交或共同回滚；所有入口覆盖。outbox 只做可恢复通知/清旧 key，不推进权威 epoch。 | 提交前注入失败并回滚，确认数据与 epoch 均不变；提交后在发通知前崩溃，确认 SQL 中 epoch 已更新且新请求不能命中旧 key；逐类审计 API/admin/batch/worker/migration 写入口。 |
| API/worker lease 到期后旧 worker 继续执行 | 接管事务获得更大 fencing generation；SQL、Blob、Qdrant 对旧 generation 均拒写，旧 worker 只能留下可审计失败。 | 令 worker A 暂停至租约过期，B 接管并写入，再恢复 A；逐一验证 SQL、文件/对象、向量 payload 无 A 的迟到副作用。 |
| worker 在副作用中崩溃 | 状态可从 outbox/任务重放；无半成品被当作已提交版本。只有证明候选 staging 对象身份、全局引用、保留义务均已核清后才能清理；身份/引用/保留状态未知时必须保留候选并进入人工或可审计补偿流程。 | 在 blob 条件写、SQL commit、outbox ack、Qdrant upsert 各阶段强制终止并恢复；验证状态机和 reconcile 收敛。 |
| 至少一次重复投递/重复 claim | 相同 job/input/generation 的副作用幂等，attempt 和审计不丢，多个 consumer 不产生重复对象或重复版本。 | 并行投递同一 job、确认 ack 丢失后重投；检查唯一键、job settle、BlobRef 与向量唯一身份。 |
| 撤权/改版与缓存命中并发 | epoch 在权威 SQL 变更事务中推进；Redis 通知延迟/重放/故障不影响正确性；新请求按权威当前 ACL+epoch 构 key，迟到的旧 epoch cache write 不可被取用。ACL 查询或水位确认故障即拒绝。 | API A 热缓存，API B 撤权/改版本并提交后再延迟、丢弃、重复 outbox 通知并关闭 Redis；新请求必须拒绝或命中新 epoch。覆盖请求准入检查至响应提交之间的竞争，并验证响应提交后的撤权不声称追回已发送内容。 |
| Outbox 延迟、重放或 Redis 故障 | SQL epoch 已原子提交；通知仅用于清旧 key/刷新副本水位。副本若无法确认最新 epoch 或水位落后则拒绝共享缓存命中，不能用旧水位服务。 | 提交前 rollback、提交后崩溃、长时间延迟及重复重放 outbox；Redis 故障；断言事务 epoch 一致、通知可重放、fail-closed，不存在依赖异步最终推进的窗口。 |
| 在途非流式/流式响应遇撤权 | 非流式响应在提交前执行最后授权准入；流式响应开始时检查并在每个待发送块前复核。撤权后不启动新请求/后续块；已经提交或发送的内容不承诺撤回。 | 卡住响应提交前检查并并发撤权，确认无未经授权的新响应提交；流中途撤权后检查停止后续块，保留“已发送块不可追回”的明确记录。 |
| 文档删除与索引/版本 worker 并发 | tombstone/CAS 优先阻断新读写；过期 generation 不得重建向量/文件引用；共享 BlobRef 只在所有权全无后 purge；合法恢复仅恢复文档版本的可见性，不回滚当前 ACL 或既有撤权。 | 同时删除、索引、恢复；验证唯一生命周期状态、搜索不可见、共享引用仍完整，延迟 purge 能安全重跑。 |
| 跨存储备份/恢复 | 备份覆盖同一一致性点的 SQL、所有引用 Blob、Qdrant snapshot 或可验证重建清单、配置/epoch/outbox checkpoint；逐对象 checksum 和引用闭合通过。 | 暂停写或使用可证明的日志水位；隔离环境恢复并核对 BlobRef 闭合、ACL、tombstone、outbox、向量可重建；恢复演练成功后才准入。 |
| 切换后发生新写入，再请求回滚 | 禁止把连接串简单改回旧库。先停写并冻结新入口，导出切换后变更日志/事务水位，对旧库回放或做确定性 reconcile，再验证 ACL、owner、epoch、BlobRef 与向量版本；无法证明无遗漏时保持新库只读/服务停写并前滚修复。 | 演练包含切换后真实形状的新建/改版/撤权/删除，再恢复旧目标；逐笔核对日志水位、引用、向量、owner 和 ACL。没有可验证逆迁移就不准回指旧库。 |

门禁须同时满足：Task 2 DAL/schema/迁移门禁通过；ACL/data epoch 与权威写同 UoW 原子提交、所有写入口覆盖；outbox 延迟/重放/Redis 故障不破坏正确性；响应准入点已定义并通过撤权竞态验收；全写目标能拒绝旧 token；双 API 和独立 worker 故障矩阵通过；跨存储备份恢复可复演；真实负载与业务 SLO 已另行确认。未满足条件前保持单 API/单 worker 所有权，不扩副本。

## 备份、恢复与回滚

当前本机备份实现对 SQLite 与已引用文件生成带 checksum/manifest 的备份并提供 verify/restore 流程；它没有证明 PostgreSQL、对象存储与远程 Qdrant 的同一时点快照可一致恢复。多存储方案需记录 SQL commit LSN/事务水位、outbox 最大已投递/待处理游标、BlobRef 清单与对象版本、Qdrant snapshot 时间/集合 schema 及可重建指针。恢复时先隔离写流量、恢复权威 SQL 与 blob，按同一水位重放/去重 outbox，再恢复或全量重建向量，最后核对 ACL、epoch、tombstone、引用闭合与抽样 hash，方可开放流量。

迁移后的写入需有不可变审计日志/CDC 或完整、可排序的变更日志供逆向回放；epoch/ID 映射也须保存。若无法证明日志连续且外部 Blob/向量副作用已纳入对账，回滚动作是停写与前滚修复，不是 URL 回切。对象版本控制有助于找回对象历史，但不能代替应用级跨存储备份。

## 当前不验收项（not-run）

本轮只做源码与文档盘点：未启动 Redis/PostgreSQL/S3 兼容服务/Qdrant server/容器，未访问真实 `.env` 或业务 DB，未作双 API、独立 worker、epoch 原子提交/回滚、outbox 延迟/重放、Redis 故障、请求/流授权准入、锁故障、对象条件写、跨存储备份/恢复或切换后回滚实验。上述矩阵均为未来设计门禁；当前不能宣称 PostgreSQL、共享 Redis、对象存储、持久 fencing 或多副本 worker 已支持。

## 技术依据与建议边界

以下“官方资料事实”只描述各产品文档说明的行为；“本 ADR 工程建议”是为本项目提出的准入约束，不是官方产品保证。

- **官方资料事实：** Redis 分布式锁文档讨论锁有效期、故障转移下的安全性限制，并介绍 fencing token 思路；Redis 锁本身不能替应用写入目标拒绝陈旧持有者。[Redis distributed locks](https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/)
- **官方资料事实：** S3 条件写可用条件请求避免对象被意外覆盖；S3 versioning 保留对象版本。二者是对象层功能，不提供 SQL/向量/对象的一致事务。[S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)；[S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- **官方资料事实：** Qdrant 的分布式部署文档涉及集群/复制部署配置；本项目客户端能连接 Qdrant server 不自动证明应用 ACL、任务投影或跨存储恢复正确。[Qdrant distributed deployment](https://qdrant.tech/documentation/distributed_deployment/)
- **本 ADR 工程建议：** PG 持久 generation + 每个写目标校验、epoch 绑定缓存、不变 BlobRef、条件创建、outbox/reconcile、保留 tombstone 与恢复身份，以及上表逐项实测。运行门禁尚未执行，须在具备隔离环境后留下结果。

