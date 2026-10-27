# 本地安全后续设计（2026-10-27）

## 目标与依据
继续最新交接中的本地故障矩阵和 writer 准备工作，先收紧三个有具体源码风险的边界。基线为 8eac1fa；这是新一轮，不复用上一轮 3517/5 作为新结果。用户要求先计划，再并发 6.1 Terra，由 root 统筹，服务器验收保留，业务数据不受影响。

## 方案与取舍
root 选择先完成小范围、能用合成资料证实的安全合同，而不是现在切换 PG/全局 UoW 或重构大路由。直接进入主库/共享存储迁移缺少完整 writer admission 和真实恢复验收；只加文档又不能减少已有路径风险。保留现有数据库格式、事务嵌套复用语义和 API 响应，只调整已证实的失败边界。

## A. 到期 purge 的文件边界
现有 purge 从 document_versions.file_path 直接 unlink，versions_root 只用于最后清理空目录。先建立完整删除计划，再执行任何文件或向量副作用。候选文件必须位于显式配置的 versions_root 或 structured_datasets_root 下，路径各级不得跨符号链接/Windows junction，不得引用其他文档持有的版本或 archive source。现存根内 legacy 非规范文件名不直接判成错误；根内目录存在并不代表文件属于该文档。有现存版本文件需要删除时缺失 versions_root 必须失败闭合；更新仓库内测试调用者传入真实临时根，生产 worker 已传入。错误应使用稳定 RecycleError 原因，并保留 tombstone/路径/可重试现场。
沿用 unlink/vector 成功后才提交终态的协议；不宣称文件回滚、进程崩溃恢复、跨进程 fencing 或无 TOCTOU。SQL 和 dataset 身份校验、跨文档引用及链接检查需要测试。目录裁剪也不得通过链接。整批复审补充：其他文档的 structured_datasets 派生路径同样是持有引用；即使两个配置根相同/嵌套也必须保护。无法提供解析外来数据集所需根时保守失败闭合，不能把缺根当作无引用。

## B. ConnectionManager 关闭竞态
不改变 WAL 读写分离和现有 transaction 嵌套合同。关闭与新连接发布必须有清晰的锁内判定：关闭后不发布新 reader；配置失败/关闭竞争产生的未发布连接必须关闭；等待 writer 锁的请求在获锁后再次检查 closed。正常 reader 初始化不等待业务 writer transaction。已交给调用者的 reader 不新增 lease/drain 保证，close 仍可关闭它。

## C. Writer 清单的保守 AST 证据
工具只解析源码，不执行或导入被扫描文件。修复控制流/词法作用域导致的已知连接错误归因：分支、循环、异常路径、with 内重绑定和类作用域不能让仅某条路径成立的构造器/连接身份泄漏成确定标签。相同确定身份的简单直线别名与已覆盖构造器保持识别；不能解析就 unknown，保留 SQL sink 候选和来源。保持确定性、独占输出、根边界、writer_completeness=not_proven，不增加伪调用图。

## 全局约束
- 只在 C 隔离 worktree 改动，不切换 D 主目录、不改 .env、不连接真实模型/数据库/向量服务。
- 临时数据库和文件只在新 OS 临时目录；离线 pytest、低优先级、BLAS 单线程；pytest 由 root 串行调度。
- 产品不新增依赖，不做数据库迁移，不改变 transaction 嵌套语义，不批量格式化或为行数做无关拆分。
- 三实现代理使用 gpt-6.1-terra/high，互不修改对方文件；不自行创建子代理、提交、合并或推送。
- root 负责测试运行、独立评审和集成；保留失败证据，不把合成测试说成真实业务验收。
- 服务器矩阵/P99/SLO、真实迁移恢复、真实 fixture/供应商证据、PG/多实例/outbox 等继续列为未验。

