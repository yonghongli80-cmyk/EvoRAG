# V3 Excel 在线编辑、版本回退与审计设计

日期：2026-08-14  
状态：已完成交互确认，待实施计划

## 1. 目标

在 RAGv3 现有表格预览、不可变文档版本、结构化数据和增量索引能力上，增加 Excel 单元格在线编辑。用户修改后不覆盖历史文件，而是生成新的不可变 XLSX 版本；所有具有 `edit` 权限的用户都可以回退，回退同样生成新版本。公共表格发生任何修改或回退后，管理者后台立即出现完整审计记录。

本功能只修改 RAGv3：

`C:\Users\Administrator\Desktop\RAGv3\RAGv3`

不涉及 NAS、组织通讯录或“金典知识库”专项项目。

## 2. 首版范围

首版支持 `.xlsx`：

- 修改文本、数字、日期和布尔值；
- 清空已有单元格；
- 一次修改一个或多个工作表中的多个单元格；
- 每次保存最多 500 个单元格；
- 保留工作表结构、样式、未修改公式和合并区域；
- 保存原因可选，但进入审计记录；
- 保存为新版本、查看历史版本和从历史版本回退；
- 公共表格修改进入管理后台表格变更页。

首版不支持：

- 创建或修改公式；
- 增删行列或工作表；
- 新建、删除或调整合并单元格；
- 修改受保护单元格；
- `.xls`、`.xlsm`、外部数据连接或宏编辑；
- Word/PDF 在线编辑；
- 多人实时协同光标或自动合并冲突。

## 3. 用户体验

`SpreadsheetViewer` 在用户拥有 `edit` 权限时显示编辑模式。用户可在本地修改多个单元格，页面维护未保存变更集合，并提供“保存为新版本”和“放弃修改”。

保存过程显示：

1. 正在保存；
2. 正在验证工作簿；
3. 正在建立索引；
4. 已生效，当前版本为 vN；或
5. 保存失败，原版本未受影响；或
6. 文件已被其他人更新，请刷新后重新应用修改。

版本历史增加“恢复此版本”。用户选择历史版本后，系统展示恢复目标、当前版本和不可逆覆盖风险说明；确认后创建一个新版本，不倒拨 current 指针。

## 4. API 合约

### 4.1 保存单元格修改

`POST /documents/{document_id}/spreadsheet-edits`

```json
{
  "base_version_id": 7,
  "changes": [
    {
      "sheet_key": "sheet-1",
      "cell": "B12",
      "value_type": "number",
      "value": 6500
    },
    {
      "sheet_key": "sheet-1",
      "cell": "D18",
      "value_type": "blank",
      "value": null
    }
  ],
  "reason": "更新本周价格和库存"
}
```

约束：

- 请求必须使用 `document_id` 和服务端已知的 `sheet_key`，不接受物理路径；
- `base_version_id` 必须仍是当前版本，否则返回 HTTP 409 和 `version_conflict`；
- `cell` 必须是单个 A1 地址，不接受范围、跨表引用或命名表达式；
- 重复地址在请求校验阶段返回 422，不按顺序覆盖；
- `value_type` 固定为 `text/number/date/boolean/blank`；
- 文本长度、日期范围和数值可序列化性使用明确上限；
- 超过 500 个变更返回 422；
- 只有 `.xlsx` 当前版本可进入此接口。

成功响应返回：

```json
{
  "edit_session_id": 31,
  "document_id": 12,
  "version_id": 8,
  "version_no": 8,
  "status": "pending_index",
  "change_count": 2
}
```

### 4.2 回退历史版本

`POST /documents/{document_id}/versions/{source_version_id}/restore`

```json
{
  "base_version_id": 8,
  "reason": "价格误改，恢复到审核版本"
}
```

恢复不是指针倒拨。若当前为 v8、选择 v4，则创建 v9，内容复制自 v4，并记录 `restored_from_version_id=4`。v5-v8 继续保留，未来仍可恢复。

### 4.3 审计查询

- `GET /admin/spreadsheet-changes`
- `GET /admin/spreadsheet-changes/{session_id}`

查询支持 actor、document、knowledge base、时间范围、operation、资料范围和异常标记过滤。详情接口按管理者的文档与敏感域权限决定是否返回工作表、单元格和前后值。

## 5. 权限

- `edit`：修改、清空、保存新版本和从历史版本回退；
- `view`：查看当前表格及允许访问的历史版本；
- `download`：下载 XLSX；`edit` 不自动包含下载；
- `manage`：查看完整审计、分享和修改 ACL；
- 只有 `view/download` 的用户不能编辑或回退；
- 文件 owner、管理员和共享编辑者只要具有有效 `edit` 都可以回退；
- 每次保存与回退都在开始和激活前再次检查 `edit`，撤权后不能靠已打开页面继续保存；
- 敏感表格审计值只能对同时拥有对应敏感域权限的管理者展示。

在 V3 动作级 ACL 改造完成前，本功能通过现有 `require_doc_edit` 接入；接口内部集中使用一个 spreadsheet edit authorization adapter，后续可替换为动作级 `authorize_document(..., "edit")`，避免散落权限判断。

## 6. 工作簿变更规则

服务端从受控版本存储读取 `base_version_id` 的 XLSX，复制到暂存路径，再使用 `openpyxl` 应用补丁：

1. 以 `data_only=False` 打开，保留公式表达式；
2. 根据数据库结构化数据中的 `sheet_key` 解析真实工作表，不接受客户端 sheet name 作为身份；
3. 拒绝公式单元格、受保护单元格、合并区域中的非左上角单元格；
4. 将 JSON 值转换为确定的 Excel 值类型；`blank` 写入 `None`；
5. 保存到暂存 XLSX；
6. 重新打开暂存文件并验证：工作表集合、合并区域和全部公式与基准版本一致，目标单元格值与请求一致；
7. 计算新文件 SHA-256；若结果与当前版本完全相同，返回 `unchanged`，不创建版本和成功变更审计；
8. 验证通过后才创建 pending 版本。

单元格旧值必须从基准工作簿读取，不能相信客户端提交。服务端记录标准化前后值和显示快照，避免日期、浮点数和空值在审计中歧义。

## 7. 不可变版本与并发

保存使用乐观并发控制：

- 请求携带 `base_version_id`；
- 创建 pending 版本前确认它等于 `document_permissions.current_version_id`；
- 激活前再次确认 current 未改变且新版本仍是该文档最新期望版本；
- 任一检查失败都将本次版本标为 cancelled/failed，清理暂存产物并返回 409；
- 不自动合并两位用户的修改，也不静默覆盖。

版本状态统一沿用/扩展为：

- `staging`
- `pending_index`
- `current`
- `historical`
- `failed`
- `deleted`

修改、审计 session、版本创建、期望版本登记在一个持有 `UserDB._lock` 的 `BEGIN IMMEDIATE` 事务中完成。文件写入和模型调用不持有数据库锁。激活使用现有 material worker 的事务边界。

## 8. 原子处理与失败回滚

处理顺序：

1. 鉴权、格式和 base version 检查；
2. 暂存复制、应用修改、重开验证；
3. 数据库事务创建 pending 版本、edit session 和 cell changes；
4. 移动暂存文件到受控不可变版本路径；
5. 排队结构化解析与索引任务；
6. worker 构建新结构化 dataset、FTS 和向量；
7. 单事务激活新版本和审计状态；
8. 提交后修复 Qdrant advisory current flags。

任何激活前失败：

- 旧版本继续是 current；
- 删除目标版本的临时向量和结构化产物；
- 新版本和 edit session 标记 failed/cancelled；
- 保留不含敏感单元格值的失败 reason code；
- 前端提示原版本未受影响。

回退复用同一服务，只是输入工作簿来自 `source_version_id`，operation 为 `rollback`，并写 `restored_from_version_id`。

## 9. 结构化数据、FTS 与向量

新版本激活前必须拥有完整检索视图：

- 结构化数据：为新版本生成独立 dataset/sheets/rows；
- FTS：使用新版本完整文本替换当前 FTS，删除/清空内容不再命中；
- 向量：按稳定 unit key 和 digest 与前一版本比较。

向量复用的正确语义：

- 变化或新增单元调用嵌入模型；
- 未变化单元复用 embedding cache/前版本向量值，不调用模型；
- 但新版本必须为所有当前单元建立完整的新版本 point 集合；
- 删除单元不进入新版本 point 集合；
- 激活后 current-version 检索只能看到新版本完整集合。

现有 material worker 当前只写 changed units；实施时必须补齐“未变化单元向量值复制/重物化到新版本”的逻辑和回归测试，不能让新版本丢失未修改行的语义检索能力。

## 10. 审计模型与管理页面

### 10.1 数据表

`spreadsheet_edit_sessions`：

- `id`
- `document_id`
- `base_version_id`
- `new_version_id`
- `actor_user_id`
- `actor_display_name_snapshot`
- `operation`：`edit/rollback`
- `status`：`pending/active/failed/cancelled`
- `change_count`
- `reason`
- `restored_from_version_id`
- `created_at`
- `activated_at`
- `request_ip`
- `user_agent`
- `risk_flags_json`

`spreadsheet_cell_changes`：

- `id`
- `session_id`
- `sheet_key`
- `sheet_name_snapshot`
- `cell_address`
- `old_value_type`
- `old_value_json`
- `new_value_type`
- `new_value_json`
- `sensitive`

审计记录不可由普通编辑者删除。用户离职或账号删除后保留内部 ID 和显示名快照。

### 10.2 管理页面

新增“表格变更”页面，显示时间、操作人、文件、操作、变更数量、基础/新版本、原因、状态和风险标记。支持按人、文件、知识库、时间、操作类型、资料范围和异常过滤。

详情页展示按工作表分组的差异：

```text
Sheet1!B12    6800        → 6500
Sheet1!D18    12          → 空
Sheet2!F21    2026-08-01  → 2026-08-15
```

管理者可从记录打开版本历史、查看差异或恢复任一历史版本。

### 10.3 风险标记

首版使用确定性规则：

- 修改公共表格；
- 单次修改超过 100 个单元格；
- 清空超过 20 个非空单元格；
- 回退到非上一版本；
- 同一用户同一文件短时间连续保存超过 5 次；
- 尝试修改公式或受保护单元格（拒绝事件）。

敏感数据权限不足的管理者只能看到文件发生修改、操作人、时间、数量、状态和风险类型，不能看到 sheet、cell、文件标题摘要或前后值中可能泄露的内容。

## 11. 测试与验收

必须覆盖：

- 文本、数字、日期、布尔值和 blank；
- 多工作表批量编辑；
- 样式、公式、工作表和合并区域保持不变；
- 公式、受保护单元格、非法地址、重复地址和超过 500 项被拒绝；
- 相同结果返回 unchanged；
- 两位用户基于同一版本保存时第二个请求 409；
- 保存期间撤权导致激活拒绝；
- 任一步失败时旧版本继续可查可下载；
- 修改内容进入新结构化数据、FTS 和向量；
- 删除内容不再被当前版本检索；
- 未变化单元不调用嵌入模型，但仍存在于新版本完整向量集合；
- 回退生成新版本并记录来源；
- 回退后仍可恢复到回退前版本；
- edit 用户可回退，view/download 用户不可回退；
- 公共表格变更立即进入管理页面；
- 管理筛选、详情与风险标记；
- 敏感审计值遮蔽；
- 失败/拒绝日志不泄露单元格敏感内容；
- 现有版本下载、表格预览、结构化查询、材料 worker 和权限测试无回归。

## 12. 实施阶段

1. 后端单元格 patch、工作簿验证、不可变版本和冲突控制；
2. 新版本完整结构化数据、FTS 和增量向量重物化；
3. 可编辑 SpreadsheetViewer、保存状态、版本历史和恢复操作；
4. 管理后台表格变更列表、详情、筛选和敏感遮蔽；
5. 全链路回归、性能与并发验收。

每阶段独立测试、提交和审查后再继续下一阶段。
