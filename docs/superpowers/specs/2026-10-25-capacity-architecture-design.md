# 单机容量基线与长期架构门禁设计

## 意图与选择

用户不知道业务规模并请求建议，授权root统筹和Luna并行，保持现有服务/数据。root建议先单机1000文档、并发2试点，再以1000/5000/10000及并发1/2/4做工程实验。文档数、版本数、分块数/向量维度、原文大小应分别记录；这些均不是业务事实或当前已验收的全链路容量。

沿用现有SQLite/单进程，先测真实材料定位组件，再制定数据库/共享状态/存储/worker的实施与迁移门禁。直接加多副本或换连接串不能验证事务与跨存储一致性；无真实问题分布时重复大批模型调用也不能补足业务SLO。本轮不改生产代码/依赖/默认值、不部署或迁移；交付诊断证据、候选验收口径和可执行的后续方案，不能将设计称为PostgreSQL/对象存储已实现。

## 容量诊断

真实UserDB、MaterialLocator、FTS和现有ACL，semantic_retriever=None。N=1000/5000/10000，每文档历史+当前两版本及对应两FTS行；两普通legacy用户、交替private归属，每10文档一tombstone。查询capacity/top_k10，返回必须当前/可见/未删除，model_input不得包含不可见hidden名称。无正文文件、embedding、Qdrant、HTTP或LLM调用，不能据此评价这些阶段或整条/query吞吐。

单BelowNormal Python进程、BLAS1，所有既有回归结束后执行。每thread独立Locator，同一DB线程本地读连接；并发1/2/4闭环，每组合3重复、每worker40次测量/5预热。记录每样本、组墙钟/seed时间、实际样本数、nearest-rank P50/P95/P99、进程峰值WorkingSet和SQLite大小；不平均分位数。profile前后逻辑dump hash和writer total_changes不变，不把SHM读者状态变化当业务写。

19:04宿主约1.4GiB可用，资源门禁优先于完成矩阵：每次请求前后检查可用物理内存>=1GiB、测试进程WorkingSet<=256MiB；所有worker共享stop event，异常/越界即停止新请求，等已在途读结束，保存部分证据并非零退出。按规模/并发递增，门禁失败的档位标记deferred_resource，不降低门槛或停止用户进程。root执行前后只读检查原PID/start/ready/env hash。

helper仅在ignored artifact内，创建自己唯一短Temp，绝不接受已有DB路径；不读真实.env/业务数据，禁止非loopback与8080连接。SQLite连接前audit hook限制到自建fixture根，环境文件open拒绝。fixtures/日志保留，备份连接显式close；若保存durable DB副本只能从本次合成DB生成。无永久benchmark生产API或空抽象、无为一次性脚本写镜像实现测试；自检先做小规模并核对真实结果，正式矩阵由root执行一次。

## 候选SLO与正式验收

历史200真实模型串行基线保留其旧freeze/热缓存/合成分布，P99 5.016s不用于本轮通过线。root暂建议试点候选P95<=10s、P99<=20s、HTTP错误率<=1%，权限泄露/错owner为零容忍；它们未被正式接受或验收，不是本轮组件profile通过线。正式SLO需真实问题分布、cold/warm分组、至少3独立时段/足量尾部样本、nearest-rank定义与失败超时口径，流式首token/完成延迟另计；以事实/来源/ACL正确性共同验收，超过预算先查分段再做受控对照优化。

## 架构交付边界

1. DAL/PostgreSQL：盘点实际UserDB facade与SQLite耦合，提出UnitOfWork/SQL方言/ID/事务重试/CAS/FTS语义契约，完整schema与行为shadow等价、备份/停写/切换/回滚门禁。保持admin、legacy/org ACL、canary shadow与沙箱fail-closed。切换后新写入不能简单回指旧SQLite，需变更日志/reconcile或继续停写。无新driver/server时只验收方案和离线盘点。
2. Redis：共享场景必须required/fail-closed，不能分布式锁降级为进程内互斥。答案缓存保持disabled；未来cache必须绑定KB/user/ACL/data epoch和模型/prompt版本并重新鉴权。lease需持久fencing及各写目标验证，TTL不等于进程寿命。
3. 对象存储：不可变key绑定KB/document/version/hash，条件写、独立checksum、引用身份与配额；ETag不能泛称内容SHA256，versioning不代替跨存储应用备份。SQL/blob/Qdrant用outbox/reconcile，不弱化tombstone、共享引用和失败补偿。
4. 多副本/独立worker：先共享权威库/存储/持久准入，再拆角色与worker；现有API lifespan仍启worker，未来flag须标proposal，不能提供当前不存在的运行命令。隔离验证双API、旧lease/进程崩溃/重复投递/撤权/删除并发后才准入部署。Qdrant server既有合成验收不等于整个应用多副本。

技术建议依据2026-10-25读取的官方SQLite WAL、PostgreSQL transaction/locks、Redis distributed locks、S3 conditional writes/versioning、Qdrant distributed deployment和Prometheus histograms文档；正式文件逐项链接。外部环境/业务问题集/维护窗口等欠缺条件列清，不伪造运行验收。
