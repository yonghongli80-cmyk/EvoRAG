# 历史暂存受控恢复设计

## 目的和授权

用户要求按顺序处理历史暂存恢复写流程，root统筹、Luna并行、保护现有服务和业务数据。前一专项已关闭于380813f。本设计选择是root依据代码和两份只读勘查作出的工程裁决；不表示用户提供了业务事实或同意运行真实恢复。

## 方案

保留audit CLI永久只读，另提供维护用Python preview/apply接口。自动认领审计候选会缺少内容与身份凭据；直接修改现有confirm会扩大日常请求的行为。本方案使用显式输入、数据库可信SHA256、重新授权、KB permit与SQLite写事务，代价是需要停业务写者的维护步骤，且本轮只覆盖下列两种确定情况。

1. `version_pointer`：目标版本必须为staging、声明路径在root内且缺失；操作者显式提供root/<document_id>/<version_no>/内直接regular文件，与version.content_hash严格匹配。只更新version.file_path。
2. `archive_rehome`：非rag_docs KB的pending_confirmation manifest，container document/version对应、版本为staging，源为root/staging下直接regular文件，hash与container version一致。复制到root/staging/kb/<KB SHA256>/recovery-<request SHA256>.bin，更新manifest.source_path；若version.file_path也是原source则同时改为新copy，否则保持。原source始终保留，manifest状态/expiry/entries保持。

current/historical、已确认/过期归档（过期仅指expires_at已到）、无hash/不匹配、多重引用、缺身份、rag_docs archive rehome、在途任务、被删除/受保护文档和生命周期删除态均拒绝。归档内容不会被解包或重新确认。没有HTTP路由、可写CLI、自动retry/索引/激活/删原件。

## 接口

`rag/materials/recovery.py`提供：

- frozen dataclass `RecoveryRequest(operation_key: str, kind: str, actor_user_id: int, document_id: int, version_id: int, candidate_path: str = '', manifest_id: int | None = None)`。
- frozen dataclass `RecoveryPlan(request: RecoveryRequest, versions_root: str, source_path: str, destination_path: str, expected_version_path: str, expected_manifest_source: str | None, content_hash: str, byte_size: int, metadata_fingerprint: str, request_hash: str)`。
- `preview_staging_recovery(db, versions_root: Path, request: RecoveryRequest) -> RecoveryPlan`：不改业务metadata/文件/回执，不自行猜文件名或挑候选。
- `apply_staging_recovery(db, plan: RecoveryPlan, *, maintenance_confirmed: bool = False) -> dict`：必须显式True；重新授权、重算/校验plan所有字段，返回持久JSON回执。
- `RecoveryError(code: str)`具有稳定code；生命周期错误保留原KBLifecycleError。

operation_key为1..128字符ASCII字母数字/下划线/横线，request种类/正整数（排除bool）严格校验，hash为64位小写十六进制。版本root必须绝对、无链接且已有目录。维护确认是可信调用者的前提声明，不是锁或安全凭证；操作者还须排除所有外部文件写者，不可把此接口开放给未受信网络请求。

## 前置校验和原子性

preview/apply均由actor ID重新加载账号，拒绝缺失/停用组织person；沿用require_doc_version_edit，以及非rag_docs的require_kb_edit，不发明角色或admin规则。文档的deleted/protected保护保持。所有metadata读取用于写决策时来自同一writer conn；apply先在同一同步上下文持kb_mutation，再BEGIN IMMEDIATE。拒绝调用时已处在writer事务中，避免ConnectionManager嵌套join提前回执。

metadata fingerprint包含目标权限/版本/manifest完整行及相关全局引用/在途状态，以规范JSON SHA256比较。全库document_versions.file_path、document_permissions.current_version_id、kb_documents.document_version_id/file_path、material_archive_manifests.container_version_id/source_path，和active jobs均在事务内核对。除了目标version、目标manifest相互的必要引用，其他版本/manifest、所有current pointers和KB document对目标version/source/destination的引用都拒绝；jobs任意scope的queued/retry/running目标version拒绝。路径按词法及解析路径比较；不能证明完整检查时fail closed。目标document/version换owner、状态/权限/路径变更或新共享引用均使旧plan失效。

文件检查拒绝root/祖先/文件symlink/junction、逃逸/..、错误类型、hardlink（nlink != 1）；hash使用有界分块并在读前后比较文件身份/大小/mtime和路径身份。archive copy使用exclusive create和fsync，不覆盖；已有确定目标仅在同hash、无外部引用时允许作为上次崩溃保留copy继续。文件检查在copy前及metadata CAS前重做。维护前提仍必要，不能声称Python路径检查抵御有权限的外部恶意文件系统竞争。

app迁移v6安装material_recovery_receipts：operation_key全局PRIMARY KEY、request_hash、actor_user_id、document_id、version_id、receipt_json、created_at。无cascade外键，保留删除后审计凭据。回执与metadata CAS同事务提交，repo helper不commit。相同key/hash且当前ACL/identity/最终路径字节仍有效时返回相同回执；不同请求冲突，不返回其他主体回执。失败/commit失败保留源文件、候选、已复制字节和原metadata/身份；没有成功回执。崩溃留下的copy可同请求重试，不当成自动可删除垃圾。

## 验收

只用Temp合成DB/目录：preview无变更，指针修复与archive copy-rehome、hash/链接/共享/错owner/状态/权限撤销拒绝，同key重放/异参冲突、在途job、KB tombstone、事务提交失败/崩溃遗留copy重试。独立task/whole复审，相关既有审计/归档/生命周期/迁移回归，Ruff与生产锁烟测。D main及服务不改，C无.env；offline guard、BelowNormal、BLAS1，最多2workers。实际运行恢复、部署和生产迁移均保留维护门禁。
