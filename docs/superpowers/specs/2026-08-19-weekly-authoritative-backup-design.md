# RAGv3 每周权威数据备份与隔离验证设计

日期：2026-08-19
状态：已确认，待实施计划

## 1. 目标

为当前单机和 Docker 部署提供一套无需 NAS、无需外部服务的自动备份能力。系统每 7 天最多创建一份新的成功备份，默认保留最近 8 份。备份必须可以离线验证，损坏或不完整的结果不得被发布为可恢复备份。

本阶段优先保护不可替代的权威数据，不备份可以重新生成的检索派生物。恢复只进入隔离目录，不自动覆盖正在运行的生产数据。

## 2. 范围与非目标

### 2.1 备份内容

备份以下配置路径中实际存在的权威数据：

- `users.db`：用户、权限、知识库、文档版本指针、审计和资料生命周期元数据；必需。
- `analysis.db`：分析模式数据；不存在时记录为可选缺失。
- `memory.db`：对话记忆、执行记录、反馈和缺口分析数据；不存在时记录为可选缺失。
- `data/upload/`：兼容旧导入流程的原始文件。
- `data/kb_files/`：知识库文件存储。
- `data/document_versions/`：已提交的不可变文档版本。
- `data/structured_datasets/`：已生成并被当前功能使用的结构化数据文件。

备份文件树时拒绝符号链接和任何解析后越出已配置根目录的路径。`staging`、`quarantine`、`archive_work` 等未提交临时目录不进入正式备份。

### 2.2 明确排除

- `.env`、模型 API Key、Redis 密码和 JWT 密钥。
- Qdrant 本地数据；恢复后由权威文档重新生成。
- `bm25_index.db`、`embedding_cache.sqlite3`、图表缓存和 `index_state.json`。
- Redis 缓存和任何最终答案缓存。
- NAS、云盘、对象存储、远程上传和跨机器复制。
- 在线一键替换生产目录。
- 加密和防恶意篡改签名。本阶段的 SHA-256 用于检测意外损坏，不声称提供密码学身份认证。

## 3. 存储布局

本地默认备份根目录为仓库同级的 `RAGv3-backups/`，避免再次增大 RAGv3 项目目录。可通过 `RAG_BACKUP_ROOT` 覆盖。

Docker 使用独立命名卷 `backup_data:/app/backups`，并设置 `RAG_BACKUP_ROOT=/app/backups`。备份不得写入 `rag_data`，否则源数据卷损坏会同时破坏备份。

正式备份目录格式：

```text
RAGv3-backups/
  20260819T030000Z-<backup-id>/
    manifest.json
    manifest.sha256
    sqlite/
      users.db
      analysis.db
      memory.db
    files/
      upload/
      kb_files/
      document_versions/
      structured_datasets/
```

创建期间使用同一备份根目录下的 `.incomplete-<backup-id>/`。只有所有复制与验证步骤成功后，才原子重命名为正式目录。失败时删除临时负载并记录错误日志，不创建可被扫描为成功备份的目录。

## 4. 清单契约

`manifest.json` 使用稳定的 `schema_version=1`，至少包含：

- `backup_id`、UTC `created_at`、应用版本和 Python 版本；
- 触发来源：`scheduled`、`deploy` 或 `manual`；
- 每个 SQLite 快照的逻辑名称、相对路径、字节数、SHA-256 和 `PRAGMA integrity_check` 结果；
- 每个文件根的逻辑名称和源配置标识；
- 每个备份文件的规范化 POSIX 相对路径、字节数和 SHA-256；
- 文件总数、总字节数和明确的排除项；
- `qdrant_included=false` 与恢复后需要重建检索索引的标记。

清单使用确定性 JSON 序列化。`manifest.sha256` 保存 `manifest.json` 的 SHA-256，用于先验证清单本身，再验证其列出的内容。

清单不得记录源机器的绝对路径、连接凭据或环境变量值。逻辑根与实际路径的映射只在运行时配置中存在。

## 5. 创建流程

备份服务提供统一接口，供后台调度、部署检查和命令行复用：

```python
create_backup(context, trigger) -> BackupRecord
create_if_due(context, trigger, now) -> BackupRecord | None
verify_backup(backup_dir) -> BackupVerification
restore_test(backup_dir, isolation_root) -> RestoreReport
prune_verified_backups(backup_root, keep=8) -> tuple[str, ...]
```

创建步骤：

1. 在备份根目录获取独占锁；已有任务运行时返回 `backup_in_progress`，不并发执行。
2. 创建 `.incomplete-<backup-id>`，所有路径均经过子路径校验。
3. 对每个实际存在的 SQLite 数据库使用 SQLite Backup API 创建独立一致快照，不直接复制活动中的 `-wal` 或 `-shm` 文件。
4. 对快照执行 `PRAGMA integrity_check`，失败立即终止。
5. 复制允许的文件树。不可变版本直接复制并校验；兼容旧目录中的可变文件在复制前后比较尺寸、修改时间与 SHA-256，发生变化时本轮失败。
6. 生成文件清单与统计，写入确定性 `manifest.json` 和 `manifest.sha256`。
7. 对临时目录执行一次完整 `verify_backup`。
8. 验证成功后原子重命名为正式目录。
9. 只有正式目录发布成功后，才清理超出最近 8 份的旧成功备份。

三个 SQLite 数据库之间不存在跨库原子事务，本阶段不声称提供法律级全局时间点。SQLite 快照分别一致；文档版本以不可变文件为基础。若数据库快照引用的文件在复制期间被并发清理，交叉验证必须令本轮失败，后台稍后重试。

## 6. 调度和部署

应用启动后创建一个守护检查器，每小时检查一次备份根目录中最新的已验证备份。距最新成功备份满 `7 * 24` 小时才调用 `create_if_due`。首次运行且不存在成功备份时应立即尝试创建。

调度参数：

- `backup_enabled=true`；
- `backup_interval_days=7`；
- `backup_retention_count=8`；
- `backup_check_interval_seconds=3600`；
- `backup_root=<repo-parent>/RAGv3-backups`，Docker 中覆盖为 `/app/backups`。

时间判断只读取正式备份的已验证清单，不依赖单独的可损坏“最后执行时间”文件。时钟倒退时不创建重复备份；未来时间戳视为异常并记录告警。

Docker 部署顺序调整为：

1. 拉取代码并构建新镜像；
2. 使用新镜像和现有 `rag_data`、`backup_data` 卷运行一次 `create-if-due --trigger deploy`；
3. 备份存在且未过期时不重复创建；
4. 备份创建或验证失败时停止部署，旧容器继续运行；
5. 备份门禁通过后再执行 `docker compose up -d` 和健康检查。

本地部署脚本 `deploy.sh` 与 GitHub Actions 必须调用同一个 Python 命令，不复制备份算法。

## 7. 验证和隔离恢复

`verify_backup` 按以下顺序执行：

1. 验证目录名、清单 schema 和必填字段；
2. 验证 `manifest.sha256`；
3. 拒绝绝对路径、`..`、重复规范化路径和符号链接；
4. 验证所有列出文件的尺寸与 SHA-256；
5. 拒绝清单未列出的数据库快照和关键数据文件；
6. 对 SQLite 快照执行 `PRAGMA integrity_check`；
7. 检查 `users.db` 中已提交文档版本引用的逻辑文件是否存在于备份清单；
8. 返回结构化状态、计数和不包含绝对路径的错误代码。

`restore_test` 只接受调用者指定的隔离根目录。实际目标固定为 `<isolation-root>/<backup-id>/`，该目标必须尚不存在或为空，并且隔离根及目标都不能等于项目目录、`data/`、Qdrant 目录、备份源目录或位于这些目录之下。流程先完整验证备份，再复制到目标，重新运行文件哈希和 SQLite 完整性检查，最后生成 `restore-report.json`。

恢复报告明确标记 `vector_rebuild_required=true`。本阶段不自动启动应用、不调用嵌入模型、不替换生产路径。

## 8. 错误处理

- 磁盘空间不足、权限错误、SQLite 快照失败、源文件变化、文件缺失、哈希不一致和清单损坏都使本轮失败关闭。
- 定时备份失败只记录高可见度日志，不终止正在运行的应用；下一小时重新检查并重试。
- 部署触发的备份失败返回非零退出码并阻止部署。
- 清理只处理通过完整验证的正式备份目录。新备份未成功发布时不执行清理。
- 清理永远保留最近一份成功备份，即使配置被错误设置为小于 1；配置模型本身要求保留数量至少为 1。
- 清理失败不使刚完成的有效备份失效，但必须记录具体备份 ID 和稳定错误码。
- 对外日志和命令输出不得包含原始文档内容、绝对数据路径、密码或密钥。

## 9. 命令行接口

新增 `scripts/backup.py`：

```text
python scripts/backup.py create [--trigger manual]
python scripts/backup.py create-if-due [--trigger deploy]
python scripts/backup.py verify <backup-id>
python scripts/backup.py restore-test <backup-id> <isolation-root>
python scripts/backup.py list
```

成功输出简短 JSON，包含备份 ID、状态、文件数、总字节数和是否需要重建向量。失败输出稳定错误码并返回非零退出码。命令不接受任意源数据路径，源路径只来自经过验证的应用配置。

## 10. TDD 与验收

所有测试只使用 `tmp_path` 下的临时 SQLite、文件根、备份根和隔离目录，不读取、写入或锁定生产 `data/`、`qdrant_data/` 或真实备份目录。每个行为先运行失败测试，确认失败原因后才实现最小代码。

实施测试顺序：

1. SQLite Backup API 在源库继续写入时仍生成可通过 `integrity_check` 的快照。
2. 成功备份生成确定性清单、清单校验和与完整文件哈希。
3. 源文件在复制期间变化、引用文件缺失或任一数据库损坏时不发布正式目录。
4. 损坏备份、额外关键文件、路径越界和符号链接不能通过验证。
5. 隔离恢复拒绝非空目标、生产路径和备份源内部目标；成功恢复后再次通过全部验证。
6. 首次运行立即备份；未满 7 天不创建；恰满 7 天创建；时钟倒退不重复创建。
7. 并发调用只能有一个任务进入创建流程。
8. 第 9 份成功备份发布后只保留最近 8 份；新备份失败时不清理任何旧备份。
9. 部署门禁在近期备份存在时复用、过期时创建、失败时返回非零且不执行 `up -d`。
10. 后台调度失败不终止应用，后续检查可以重试成功。

完成标准：

- 所有新增测试均保留可证明的 RED→GREEN 记录；
- 相关备份测试、后端全量测试、前端全量测试和前端生产构建通过；
- 关键 Ruff 检查和 `git diff --check` 通过；
- 手动命令在完全隔离夹具上完成 create → verify → restore-test；
- Git 工作区中不出现真实备份、生产数据库、Qdrant 数据或密钥。

## 11. 后续能力

若未来确需不停机的法律级全局恢复点，应另立项目，引入覆盖所有 SQLite、文件提升和向量写入的全局写屏障，并为 Qdrant 使用同一备份纪元的快照。该能力不在本设计中隐式预留半成品接口。
