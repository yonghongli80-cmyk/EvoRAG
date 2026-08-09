# RAGv3 可信知识闭环设计

## 1. 目标

本阶段把当前 RAGv3 从“可上传并问答”升级为可追溯、可纠错、可持续维护的企业知识初版，同时为未来 NAS 文件同步预留稳定边界。

完成后必须满足：

- 上传人自动成为新文档负责人，管理员可改派。
- 同名文件支持“跳过、覆盖、保留”三种明确行为。
- 覆盖产生新版本，旧版本保留且不再作为默认回答依据。
- 回答引用能定位到具体逻辑文档、具体版本、章节或分块、更新时间和有效状态。
- 员工可提交“有用、无用、内容过期”反馈，并补充说明或正确答案建议。
- “内容过期”立即形成待办；相同“无用”反馈在 30 天内由 3 名不同员工提交后形成待办。
- 文件负责人和管理员可以看到反馈提交人；普通员工只能看到自己的反馈。
- 第三个主导航页面改为“知识运营”，承载个人反馈、负责人待办和管理员运营总览。
- 未来 NAS 只需实现来源适配器，不重写版本、权限、引用、反馈和待办核心。

## 2. 本阶段范围

### 2.1 包含

- 统一逻辑文档身份与版本记录。
- 本地上传来源适配器。
- 重名处理弹窗与后端并发安全。
- 当前版本索引、历史版本降权检索和过期警告。
- 回答引用持久化与授权下载。
- 反馈、聚合规则、待办状态机和负责人改派。
- “知识运营”页面及角色化内容。
- 旧“分析”页面退出主导航，但保留路由、接口和已有数据。
- 对已有文档进行幂等迁移，使其成为版本 1。

### 2.2 不包含

- NAS、SMB、WebDAV、钉钉连接器。
- 部门、岗位、上下级继承、多级审批和离职交接。
- 场景化助手入口。
- 邮件、短信、钉钉机器人等外部通知；本阶段只提供站内待办。
- 对历史版本正文做人工差异比对或自动生成修订说明。
- 最终答案缓存；现有安全决策继续保持。

## 3. 核心边界

### 3.1 逻辑文档

现有 `document_permissions` 继续作为逻辑文档和 ACL 主记录，避免并行维护两套权限身份。新增字段：

- `current_version_id INTEGER NULL`：当前有效版本。
- `source_type TEXT NOT NULL DEFAULT 'upload'`：当前仅允许 `upload`，未来可增加 `nas`。
- `source_key TEXT NOT NULL DEFAULT ''`：来源系统内稳定标识，不使用显示文件名充当外部身份。
- `updated_at REAL NOT NULL`：最后一次当前版本切换时间。

`doc_name + kb_id` 仍是当前 UI 范围内的唯一显示名称；未来 NAS 同步使用 `source_type + source_key` 识别同一来源对象。

新建逻辑文档时：

- `owner_id` 固定为首个上传人。
- 管理员可改派 `owner_id`。
- 共享编辑者可以上传新版本，但不会自动取得所有权。
- `owner_id=0` 的历史数据在运营台显示为“未分配负责人”，只允许管理员改派。

### 3.2 文档版本

新增 `document_versions`：

```sql
CREATE TABLE document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    version_no INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('staging', 'current', 'historical', 'failed')),
    validity_status TEXT NOT NULL CHECK(validity_status IN (
        'valid', 'reported_outdated', 'confirmed_outdated'
    )),
    created_at REAL NOT NULL,
    superseded_at REAL,
    UNIQUE(document_id, version_no),
    FOREIGN KEY(document_id) REFERENCES document_permissions(id) ON DELETE CASCADE,
    FOREIGN KEY(uploaded_by) REFERENCES users(id)
);
```

文件保存在：

`data/document_versions/{document_id}/{version_no}/{safe_filename}`

历史版本继承逻辑文档 ACL，不创建独立共享记录。API 不返回 `file_path`。

### 3.3 当前索引记录

`kb_documents` 继续表示某逻辑文档当前已索引版本，新增：

- `document_id INTEGER`。
- `document_version_id INTEGER`。

原有 `(kb_id, filename)` 唯一性继续保证一个逻辑文档只有一个当前索引记录。历史版本元数据由 `document_versions` 保存。

Qdrant payload 新增：

- `document_id`
- `document_version_id`
- `version_no`
- `is_current`
- `validity_status`
- `section_title`
- `indexed_at`

覆盖成功后，旧版本向量的 `is_current` 更新为 `false`，新版本为 `true`。删除逻辑文档时删除其所有版本向量和物理文件；仅删除历史版本时只删除指定 `document_version_id`。

## 4. 文件来源适配

新增 `rag/document_sources/base.py`：

```python
@dataclass(frozen=True)
class SourceObject:
    source_type: str
    source_key: str
    display_name: str
    content: bytes
    content_hash: str
    observed_at: float


class DocumentSource(Protocol):
    def read(self, source_key: str) -> SourceObject: ...
```

新增 `rag/document_sources/upload.py`，负责把浏览器上传转换为 `SourceObject`。

版本、权限、索引和反馈服务只接收 `SourceObject`，不依赖 NAS 或本地目录语义。未来 NAS 连接器只需要实现相同接口，并把 NAS 文件唯一标识放入 `source_key`。

## 5. 同名上传与版本切换

### 5.1 API 协议

上传接口增加 multipart 字段：

`duplicate_action: "reject" | "overwrite" | "keep"`

第一次上传默认使用 `reject`。发现同名逻辑文档时返回：

```json
{
  "detail": {
    "code": "duplicate_document",
    "document_id": 12,
    "filename": "员工手册.pdf",
    "current_version": 3
  }
}
```

前端显示“跳过、覆盖、保留”：

- 跳过：关闭弹窗，不发送第二次请求。
- 覆盖：以 `duplicate_action=overwrite` 重试。
- 保留：以 `duplicate_action=keep` 重试，服务端生成 `员工手册 (1).pdf`，若占用则递增。

### 5.2 覆盖流程

覆盖只允许逻辑文档所有者、管理员或拥有文档 edit 权限的共享用户执行。

事务顺序：

1. 清理文件名并计算内容哈希。
2. 若哈希等于当前版本，返回 `status="unchanged"`，不创建重复版本。
3. 创建 `staging` 版本记录并写入临时文件。
4. 解析、分块、嵌入并写入带版本 payload 的新向量。
5. 原子移动临时文件到版本目录。
6. 单个 SQLite 事务内把旧版改为 `historical`、新版改为 `current`、更新 `current_version_id` 和 `kb_documents`。
7. SQLite 的 `current_version_id` 是当前版本唯一事实源；检索按它生成 `document_version_id` 过滤。Qdrant 的 `is_current` 仅作可修复元数据，提交后把旧版标记为 `false`，失败则进入幂等修复队列。

任一步失败都删除本次向量、临时文件和 `staging` 记录；旧版保持当前版本。

并发上传依赖 `(document_id, version_no)` 唯一约束。冲突返回 `409 version_conflict`，客户端重新加载当前版本后再决定。

## 6. 检索与引用追溯

### 6.1 检索规则

第一轮先从 SQLite 解析用户可见文档的 `current_version_id`，再按这些 `document_version_id AND validity_status='valid'` 检索。不得只依赖 Qdrant `is_current`。ACL 过滤后结果少于 `min(top_k, 3)` 时，再检索历史版本或已确认过期的当前版本补足。

历史结果：

- 排名分数乘以 `0.35`。
- 不得挤掉已取得的当前版本结果。
- 在引用中标记 `is_historical=true`。
- 回答提示词必须包含“历史资料，仅供参考，内容可能过期”。

### 6.2 章节定位

`Chunk` 新增：

```python
section_title: str | None = None
document_id: int | None = None
document_version_id: int | None = None
version_no: int | None = None
is_current: bool = True
updated_at: float | None = None
```

Markdown 和纯文本使用分块前最近的标题作为 `section_title`；DOCX/PDF 无可靠标题时使用解析器段落标题；仍无法取得时回退为 `第 {chunk_index + 1} 段`。

### 6.3 引用持久化

新增 `answer_citations`：

```sql
CREATE TABLE answer_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assistant_message_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    document_version_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    section_title TEXT NOT NULL,
    text_preview TEXT NOT NULL,
    rank INTEGER NOT NULL,
    is_historical INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(assistant_message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
);
```

assistant message 与其全部 citations 必须由单个 `persist_assistant_answer_with_citations()` 在同一个 SQLite 事务中写入；任何引用失败都回滚消息。请求带唯一 `persistence_key`，重试只有在回答哈希、引用哈希和引用数量一致时才返回原消息，否则报幂等冲突。SSE 只在事务提交后于 `done` 事件返回 `assistant_message_id`，前端据此提交反馈。

对外 citation 响应只包含：

- `citation_id`
- `doc_name`
- `kb_id`
- `section_title`
- `version_no`
- `updated_at`
- `is_historical`
- `text_preview`
- `download_url`

下载 URL 指向现有鉴权 API 的版本扩展，不暴露物理路径。每次点击仍重新执行文档 view 权限检查。

## 7. 反馈与纠错闭环

### 7.1 反馈类型

现有 feedback 表增加：

- `kind TEXT`：`useful | not_useful | outdated`。
- `question_text TEXT`。
- `question_hash TEXT`。
- `citation_id INTEGER NULL`。
- `suggested_answer TEXT NOT NULL DEFAULT ''`。
- `status TEXT NOT NULL DEFAULT 'submitted'`。

保留 `value` 用于兼容旧统计：`useful=1`，其余为 `-1`。

反馈约束：

- 同一用户对同一 assistant message 只保留一条当前反馈。
- `outdated` 在回答有多个引用时必须选择 citation。
- `not_useful` 可选择 citation；未选择且无法推断文档时进入管理员未分配队列。
- 反馈提交人姓名只返回给本人、任务负责人和管理员。

`question_hash` 使用清理空白、统一大小写和中文标点后的问题文本计算 SHA-256。本阶段不调用 LLM 做聚类。

### 7.2 待办

新增 `knowledge_tasks`：

```sql
CREATE TABLE knowledge_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL CHECK(task_type IN (
        'outdated', 'not_useful', 'insufficient_evidence', 'evidence_conflict'
    )),
    cluster_key TEXT NOT NULL,
    active_key TEXT UNIQUE,
    document_id INTEGER,
    document_version_id INTEGER,
    assigned_to INTEGER,
    status TEXT NOT NULL CHECK(status IN (
        'open', 'in_progress', 'resolved', 'dismissed', 'confirmed_expired'
    )),
    report_count INTEGER NOT NULL DEFAULT 1,
    resolution_note TEXT NOT NULL DEFAULT '',
    due_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    resolved_at REAL
);
```

任务规则：

- `outdated`：提交后立即创建；同一文档版本已有 open/in_progress 任务时追加 report_count。
- `not_useful`：按 `question_hash + document_id` 聚合 30 天内不同 `user_id`；达到 3 人时创建任务。
- 有文档时默认 `assigned_to=document.owner_id`。
- 没有引用、负责人为 0 或负责人已不存在时，`assigned_to=NULL`，只出现在管理员队列。
- 默认截止时间为创建后 7 天。
- open/in_progress 任务的 `active_key=cluster_key`；进入终态时把 `active_key` 设为 NULL，因此同一聚合键在任意时刻最多只有一个活动任务。
- 负责人可设为 `in_progress`、`resolved`、`dismissed`、`confirmed_expired`。
- 管理员可执行全部状态变化和改派。
- `confirmed_expired` 不切换 `current_version_id`，但把该版本的 `validity_status` 设为 `confirmed_outdated`；它只可作为带警告的历史参考。真正恢复有效状态必须上传新版本。

所有任务状态变更写入 `knowledge_task_events`，记录 actor、旧状态、新状态、备注和时间。

## 8. 知识运营页面

主导航第三项由“分析”改为“知识运营”，路由为 `/operations`。`/analysis` 保留但不出现在主导航。

### 8.1 我的反馈

所有登录用户可见：

- 反馈类型、原问题、反馈说明。
- 对应文件和版本。
- 提交时间、当前处理状态和处理结果。

不显示其他提交人。

### 8.2 待我处理

只显示 `assigned_to=current_user.id` 的任务：

- 问题、回答摘要、引用文件、版本、报告次数。
- 提交人列表；只有负责人和管理员可见。
- 截止时间和超期状态。
- 开始处理、已修复、无需处理、确认过期操作。

### 8.3 运营总览

仅管理员可见：

- open/in_progress/overdue 数量。
- 30 天高频无效问题。
- 已确认过期且尚无新版的文档。
- `owner_id=0` 或负责人不存在的文档。
- 180 天没有当前版本更新的知识库。
- 任务改派和负责人改派入口。

## 9. API 边界

新增路由统一使用 `require_user`：

- `GET /documents/{document_id}/versions`
- `GET /documents/{document_id}/versions/{version_id}/download`
- `PUT /documents/{document_id}/owner`
- `POST /feedback/knowledge`
- `GET /knowledge-operations/my-feedback`
- `GET /knowledge-operations/my-tasks`
- `GET /knowledge-operations/summary`
- `GET /knowledge-operations/tasks`
- `PUT /knowledge-operations/tasks/{task_id}`
- `PUT /knowledge-operations/tasks/{task_id}/assignee`

授权规则：

- 版本列表和下载要求逻辑文档 view。
- 新版本上传要求逻辑文档 edit。
- 修改负责人只允许管理员。
- 我的反馈只允许本人。
- 待我处理只允许任务负责人或管理员。
- 总览、全局任务列表和改派只允许管理员。

## 10. 错误与一致性

- `400`：非法状态转换、过期反馈未选择引用。
- `401`：未认证。
- `403`：无文档、版本、任务或管理员权限。
- `404`：逻辑文档、版本、反馈或任务不存在。
- `409 duplicate_document`：同名上传尚未选择处理方式。
- `409 version_conflict`：并发版本号或当前版本发生变化。
- `409 task_state_conflict`：基于过期状态提交任务更新。
- `413`：文件超限。

API 不返回本地路径、来源凭据、向量 ID 或内部异常全文。文件、SQLite 和 Qdrant 的跨存储操作采用 staging + 补偿；SQLite 状态切换在单事务中完成。

## 11. 迁移

迁移必须幂等：

1. 为 `document_permissions` 增加版本和来源字段。
2. 为每条现有文档权限记录创建版本 1。
3. 优先使用 `kb_documents.file_path`；默认库使用当前安全上传路径。
4. 文件存在时计算哈希并设为 `current`；文件缺失时创建 `failed` 版本，运营台显示“源文件缺失”。
5. 更新 `current_version_id`、`kb_documents.document_id` 和 `document_version_id`。
6. 旧 Qdrant payload 缺少版本字段时，只允许管理员执行一次重建索引；普通查询不把未知版本当作当前有效资料。

## 12. 测试策略

所有任务使用 TDD，测试使用临时 SQLite、临时版本目录和隔离 Qdrant/mock，不读写仓库 `data/`。

必须覆盖：

- 上传人是新文档负责人，覆盖不改变负责人。
- 跳过不写文件、数据库或向量。
- 同内容覆盖不产生新版本。
- 覆盖成功保留旧文件并切换当前版本。
- 覆盖失败后旧版仍是当前版本。
- 保留自动生成未占用名称。
- 两个并发覆盖只有一个成功提交当前版本。
- 当前版本优先；不足时历史版本以 0.35 权重补足并带警告。
- 引用不含物理路径，且下载重新执行 ACL。
- SSE 返回 assistant message ID，刷新对话后 citations 仍存在。
- 内容过期立即建任务。
- 无用反馈只有 30 天内 3 名不同员工才建任务。
- 同一员工重复反馈不增加聚合人数。
- 负责人和管理员可见提交人，其他员工不可见。
- 非负责人不能修改任务。
- 管理员可以改派文档负责人和任务。
- 第三导航为知识运营，旧分析路由仍可直接访问。
- 运营页三类角色视图及空状态、错误状态均有前端测试。

## 13. 实施分段

1. 数据模型、迁移和来源边界。
2. 版本化上传、重名处理和当前版本切换。
3. 版本感知索引、历史回退和引用持久化。
4. 反馈聚合、任务状态机和负责人机制。
5. 知识运营 API 与第三页面。
6. 全量回归、迁移演练和开发日志。

场景化助手、组织权限和 NAS 连接器必须在本设计完成并稳定后单独编写规格。
