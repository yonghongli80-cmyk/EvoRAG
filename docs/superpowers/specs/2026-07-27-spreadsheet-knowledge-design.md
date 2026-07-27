# RAGv3 Excel/CSV 知识与精确问询设计

## 1. 目标

Excel/CSV 在 RAGv3 中是一等知识来源，不再只作为“转成一大段文本的附件”。

本阶段实现：

- 原文件不可变保存和版本追溯。
- 网页内只读打开、分页、筛选、排序和单元格范围选择。
- 工作表、字段、行号、类型和公式元数据保留。
- 面向语义检索的独立切片和 Qdrant 写入。
- 面向精确筛选、统计和比较的结构化副本。
- LLM 能通过受控工具直接询问表格，并引用工作表和单元格范围。
- 文件覆盖后，原文件、结构化副本和向量索引同步切换版本。

“直接被 LLM 使用”不等于把整张工作簿塞进提示词。系统先定位数据集和范围，再把最小必要证据或确定性计算结果交给模型。

## 2. 范围

支持：

- `.xlsx`
- `.csv`

第一版不支持：

- `.xls`
- `.xlsm` 宏执行
- 在线编辑和保存回原文件
- 任意 Python、Pandas 表达式或原始 SQL
- 重新计算 Excel 公式
- 跨文件公式链接刷新
- 数据透视表编辑
- 隐藏表的默认展示

隐藏工作表可以由有权限用户在查看器中显式选择，但默认不参与普通搜索建议；是否写入向量索引由上传时策略决定。

## 3. 三层存储

一个表格文档版本对应三类数据：

```text
逻辑文档 document_id
  └─ 文档版本 document_version_id
      ├─ 原文件层
      │   └─ immutable .xlsx/.csv
      ├─ 结构化层
      │   └─ version-scoped SQLite dataset
      └─ 向量层
          └─ Qdrant spreadsheet chunks
```

三层都使用同一个 `document_version_id`。任何一层失败都不能把新版本标记为可查询的当前版本。

### 3.1 原文件层

沿用可信知识闭环：

`data/document_versions/{document_id}/{version_no}/{safe_filename}`

原文件用于：

- 原样下载。
- 重新提取。
- 格式和公式审查。
- 恢复和迁移验证。

覆盖上传永不改写原文件。旧版本继续按逻辑文档 ACL 只读访问。

### 3.2 结构化层

每个文档版本使用独立 SQLite 文件：

`data/structured_datasets/{document_id}/{document_version_id}/dataset.sqlite3`

采用“每版本独立库”而不是把所有员工表格塞入用户数据库，原因：

- 版本可以独立构建、验证和删除。
- 解析失败不会污染账号、权限和聊天数据。
- 文件级备份、恢复和哈希校验更直接。
- 查询连接天然绑定一个已授权数据集。
- 不会让模型在一个全局 SQL 库里枚举其他文件表名。

跨数据集比较由查询服务分别读取已授权版本后合并结果，不使用 SQLite `ATTACH` 暴露其他库。

### 3.3 向量层

使用现有 Qdrant collection，但每个点必须带完整表格 payload。当前版本切换仍遵守可信知识闭环的 `is_current` 和 `validity_status`。

向量库负责“找到相关工作表、字段和行组”，不作为工资合计、出勤次数等精确数值的最终计算来源。

## 4. 元数据模型

主业务 SQLite 增加：

### 4.1 `structured_datasets`

- `id`
- `document_id`
- `document_version_id UNIQUE`
- `format`
- `status`：`staging | ready | failed | historical`
- `storage_key`
- `content_hash`
- `sheet_count`
- `total_rows`
- `total_columns`
- `semantic_index_mode`
- `extraction_version`
- `created_at`
- `ready_at`
- `last_error_code`

`storage_key` 是版本根目录内的相对标识，API 不返回物理路径。

### 4.2 `structured_sheets`

- `id`
- `dataset_id`
- `sheet_key`
- `name`
- `ordinal`
- `visibility`：`visible | hidden | very_hidden`
- `header_row`
- `first_data_row`
- `row_count`
- `column_count`
- `merged_range_count`
- `has_formulas`
- `schema_hash`

CSV 创建一个名为 `CSV` 的虚拟工作表。

### 4.3 `structured_columns`

- `id`
- `sheet_id`
- `column_key`
- `ordinal`
- `display_name`
- `normalized_name`
- `inferred_type`
- `number_format`
- `unit`
- `nullable`
- `sensitivity_label`
- `sample_values_json`
- `inference_confidence`

`column_key` 是稳定内部标识；列重名时通过序号区分，不把清洗后的名字直接当 SQL 标识。

### 4.4 `structured_query_runs`

- `id`
- `user_id`
- `assistant_message_id`
- `dataset_version_ids`
- `plan_json`
- `used_columns_json`
- `filter_summary_json`
- `rows_scanned`
- `rows_returned`
- `result_hash`
- `status`
- `error_code`
- `created_at`
- `duration_ms`

不在该表保存工资明细或完整结果。

## 5. 版本 SQLite 布局

每个 `dataset.sqlite3` 包含：

- `dataset_manifest`
- `sheet_manifest`
- `column_manifest`
- 每个工作表一张内部数据表
- `formula_cells`
- `merged_ranges`

工作表数据表使用内部名称，例如 `sheet_0001`，不使用用户表名。

每行包含：

- `__row_number`：原始 Excel/CSV 行号。
- `__row_hash`：规范化行内容哈希。
- 每个字段的规范值。

结构化值规则：

- 空单元格保存 NULL，数字 0 和布尔 false 不得当作空值。
- 整数保存 SQLite INTEGER。
- 日期和日期时间保存 ISO 8601 文本，同时保留原 number format。
- 布尔值保存 0/1 并标记类型。
- 普通文本保存 UTF-8。
- 金额和高精度小数保存规范 Decimal 字符串；聚合由受控 Decimal 计算器完成，避免二进制浮点误差。
- 公式单元格保存公式文本和文件内缓存值；系统不执行公式。
- 错误单元格保存错误码。

查看器显示原格式值；查询服务使用规范值。

## 6. 解析

### 6.1 XLSX

使用 `openpyxl` 的只读模式，分别读取公式和缓存值所需视图。解析：

- 所有工作表及可见状态。
- 单元格值和 number format。
- 公式文本与缓存值。
- 合并区域。
- 工作表维度。
- 冻结窗格信息。

不加载外部链接，不执行宏，不求值公式。损坏或加密工作簿返回稳定错误。

### 6.2 CSV

按顺序尝试：

- UTF-8 BOM。
- UTF-8。
- GB18030。

检测常用分隔符：

- 逗号。
- 制表符。
- 分号。
- 竖线。

编码或分隔符无法可靠判断时，上传进入 `needs_mapping`，让用户在预览中确认，不静默产生错列数据。

### 6.3 表头

默认使用第一条非空行作为候选表头。检测到多行合并表头或重复列名时，上传预览要求确认：

- 表头行。
- 数据起始行。
- 是否组合多行表头。

确认后才完成结构化和向量索引。简单规范表格可以自动完成。

### 6.4 限制

默认可配置上限：

- 50MB 原文件。
- 50 个工作表。
- 单文件 200,000 数据行。
- 单表 500 列。
- 单元格文本 32,000 字符。

超过限制返回可解释错误或要求拆分，不把超大工作簿一次放入模型上下文。

## 7. 提取与激活流程

```text
上传原文件到 staging
→ 创建 staging 文档版本
→ 解析工作簿/CSV
→ 写入临时 dataset.sqlite3
→ 验证行列计数、哈希和 SQLite integrity
→ 生成表格 chunks
→ 写入带 version payload 的 Qdrant staging points
→ 原子移动原文件和结构化库
→ SQLite 事务切换当前文档/数据集版本
→ 新向量设为 current，旧向量设为 historical
```

失败补偿：

- 删除本次临时结构化库。
- 删除本次新向量。
- 新版本标记 failed。
- 旧版本继续 current。
- 不删除旧原文件和旧结构化库。

相同内容哈希覆盖返回 `unchanged`，不重复解析和嵌入。

## 8. 向量切片

### 8.1 Chunk 类型

`workbook_summary`

- 文件名、工作表列表、表规模和可安全显示的说明。
- 不把所有数据放入摘要。

`sheet_schema`

- 工作表名。
- 表头、字段类型、单位。
- 行数和日期范围。
- 可安全使用的少量示例。

`row_group`

- 重复表头。
- 连续数据行。
- 原始起止行号。
- 涉及列集合。

`qa_pair`

- 当用户明确选择问答对模式时，一问一答一个 Chunk。

### 8.2 行组策略

行组不是固定“一行一个”或“整表一个”：

- 目标不超过 1,200 tokens。
- 默认最多 50 行。
- 超长文本行单独成组。
- 空行和重复表头作为边界。
- 日期分区或明显小节可以作为边界。
- 每个行组重复必要表头，不能脱离 schema 后无法理解。

金额等数值可以进入行组文本以支持语义定位，但精确答案必须回查结构化层。

### 8.3 Qdrant payload

每个点至少包含：

- `doc_permission_id`
- `document_id`
- `document_version_id`
- `dataset_id`
- `sheet_id`
- `sheet_key`
- `sheet_name`
- `chunk_kind`
- `chunk_index`
- `row_start`
- `row_end`
- `column_keys`
- `content_hash`
- `is_current`
- `validity_status`
- `updated_at`

检索先执行当前用户 ACL、当前版本和有效状态过滤，再返回行组。

### 8.4 语义索引模式

- `schema_only`：只写工作簿摘要和工作表 schema。
- `row_groups`：同时写数据行组。

普通表格默认 `row_groups`。工资、考勤真实数据在主管确认前生产功能关闭；开启后使用何种模式由敏感数据策略决定。

## 9. 网页只读查看器

### 9.1 打开方式

文件详情页点击“打开表格”进入同一 RAGv3 页面。URL 使用逻辑文档和版本 ID，不暴露存储路径。

### 9.2 功能

- 工作表切换。
- 当前／历史版本切换。
- 冻结表头。
- 虚拟滚动或基于行号的分页。
- 列宽调整。
- 排序。
- 单列和组合筛选。
- 关键字搜索。
- 显示字段类型、单位和敏感标签。
- 选择单元格范围。
- 复制当前授权范围。
- 针对选中范围提问。
- 打开回答引用时高亮对应行列。
- 下载原文件。

第一版没有编辑、公式修改、批注或保存按钮。

### 9.3 API

- `GET /documents/{document_id}/versions/{version_id}/spreadsheet`
- `GET /documents/{document_id}/versions/{version_id}/sheets`
- `GET /documents/{document_id}/versions/{version_id}/sheets/{sheet_key}/rows`
- `POST /documents/{document_id}/versions/{version_id}/sheets/{sheet_key}/filter`
- `POST /documents/{document_id}/versions/{version_id}/spreadsheet-selection`

行查询使用 `after_row` 和 `limit`，默认 100、最大 500。所有请求重新执行逻辑文档 view 权限。

筛选和排序只接受字段 key、枚举运算符和参数值，不接受 SQL 字符串。

## 10. 问询路由

### 10.1 类型

`semantic`

示例：“这个工作簿主要记录什么？”使用 summary/schema/row-group 检索。

`lookup`

示例：“张三 6 月迟到了几次？”使用精确字段和过滤。

`aggregate`

示例：“销售一部平均工资是多少？”使用受控聚合。

`compare`

示例：“比较 5 月和 6 月考勤变化。”分别执行查询后比较。

`hybrid`

示例：“哪个部门出勤下降最明显，相关备注说明了什么？”先确定性计算，再检索对应备注行组。

### 10.2 LLM 使用

LLM 可以：

- 识别用户意图。
- 选择数据集和工作表候选。
- 生成 `StructuredQueryPlan`。
- 根据受控结果和引用组织自然语言。

LLM 不可以：

- 生成并直接执行 SQL。
- 自己计算工资总额或平均值。
- 猜测列映射和单位。
- 获取未通过 ACL 的数据集。
- 把向量文本中的近似数字当精确统计。

查询计划必须经后端 schema 验证和能力检查。精确数值由 SQLite 过滤与 Python Decimal 聚合产生。

### 10.3 选区提问

查看器把选区转换为：

- 文档版本。
- 工作表 key。
- 起止行。
- 起止列。

后端校验范围和权限后加载数据。浏览器不能直接提交任意单元格内容冒充服务器证据。

### 10.4 引用

表格引用新增：

- 数据集版本。
- 工作表。
- 行范围。
- 列范围。
- 单元格范围文本，如 `考勤!B12:F20`。
- 查询计划摘要。
- 参与行数。
- 下载和打开查看器 URL。

点击引用打开相同版本并高亮范围。历史版本引用不会静默跳转到当前版。

## 11. 多文件和多工作表

跨表比较必须先建立字段映射：

- 规范列名。
- 类型。
- 单位。
- 日期粒度。
- 去重键。

自动映射只产生建议。列语义、单位或日期范围不一致时要求用户确认。

多工作表查询可以并行执行，但每个子结果保留独立引用。系统不使用 `ATTACH DATABASE` 让通用 SQL 跨库自由查询。

LlamaIndex Workflow 可以拆解“比较三个月考勤”这类任务，但每个工具调用仍使用受限的结构化查询服务。

## 12. 权限和真实薪资数据

功能开发使用虚构数据。

生产启用工资、考勤前需要主管确认：

- 哪些文件属于严格受限数据。
- 哪些用户拥有 `hr_data_analyst`。
- 原始行组是否允许发送至嵌入服务。
- 查询结果是否允许发送给外部 LLM。
- 是否允许下载、复制、图表和聚合。

系统管理员不自动获得明文权限。当前没有内网模型时，策略可以选择：

- 外部模型只生成查询计划，结果由后端模板输出。
- 仅允许脱敏聚合结果进入外部模型。
- 经明确批准后允许限定明细进入指定模型。

这些策略不改变三层存储和问询接口，因此主管决策不会要求重写功能。

## 13. 迁移

已有 Excel/CSV：

1. 查找已迁移为文档版本 1 的逻辑文档。
2. 为该版本创建 `structured_dataset` staging 记录。
3. 从不可变原文件提取结构化库。
4. 删除旧的“整表单 Chunk”向量并写入新 Chunk。
5. 校验后激活数据集。

迁移幂等，以 `document_version_id + extraction_version` 唯一。原文件缺失或解析失败时保留普通文档状态，并在知识运营显示“表格结构化失败”，不能伪称可精确查询。

## 14. 备份与删除

备份清单包含：

- 原文件。
- dataset.sqlite3。
- 数据集和工作表元数据。
- Qdrant 点数量和内容哈希。

逻辑文档删除时删除全部版本的原文件、结构化库和向量。删除单个历史版本只删除对应 version ID 的三层数据。

文件删除采用待删除状态和补偿任务；部分存储删除失败时保留可重试记录。

## 15. 失败处理

- 工作簿加密：提示无法解析，可保留原文件但不索引。
- 工作簿损坏：新版本 failed，旧版保持 current。
- 公式无缓存值：查看公式，精确查询返回“无可用计算结果”。
- CSV 编码不确定：要求预览确认。
- 表头不确定：要求映射确认。
- Qdrant 写入失败：不激活结构化新版本。
- 结构化库校验失败：删除 staging 数据。
- 查询超时：要求缩小范围。
- 结果过大：只返回汇总和分页预览。
- LLM 不可用：查看器和确定性计算仍可使用。
- 向量检索失败：允许用户从查看器手动选表和范围后查询。

## 16. 性能

- 查看器不把整张表发送给浏览器。
- 行数据按行号分页，响应只包含请求列。
- 提取和嵌入异步执行，上传返回可查询状态。
- 工作表 schema 和安全摘要可缓存。
- 精确查询连接只读 SQLite，设置超时和进度中断。
- 同一文档版本的重复查询可以缓存结果哈希，但读取时重新检查权限。

## 17. 测试

### 17.1 解析与存储

- 多工作表、隐藏表和空表。
- 数字 0、布尔 false 和 NULL 区分。
- 日期、时间、百分比、金额和高精度小数。
- 公式文本、缓存值和错误值。
- 合并表头、重复列名和空列名。
- UTF-8 BOM、UTF-8、GB18030 和不同分隔符 CSV。
- 结构化库 integrity 和内容哈希。

### 17.2 版本

- 覆盖成功后三层同时切换 current。
- 任一层失败后旧版本继续可查询。
- 相同哈希不重复解析。
- 历史引用打开历史表格版本。
- 迁移重复执行不创建重复数据集。

### 17.3 切片与检索

- workbook、schema、row-group 和 qa-pair Chunk。
- 行组遵守 token 和行数上限。
- 每个 Chunk 有正确工作表和行范围。
- 无权限用户不能通过向量检索发现表名、列名或行。
- 精确数值不从向量文本直接计算。

### 17.4 查看器

- 工作表切换、分页、排序、筛选和搜索。
- 选区生成正确服务器范围。
- 引用打开并高亮准确单元格。
- 无编辑入口。
- 权限撤销后已打开页面的下一次请求被拒绝。

### 17.5 问询

- semantic、lookup、aggregate、compare、hybrid 路由。
- 原始 SQL 和 Python 被拒绝。
- 未知列、类型错误、单位冲突和超限被拒绝。
- Decimal 聚合无浮点误差。
- 结果包含数据集版本和单元格引用。
- LLM 不可用时模板结果可用。

### 17.6 工资和考勤闸门

- 虚构数据可以完成全流程测试。
- 生产开关默认关闭。
- 系统管理员没有 HR 能力时不能查看或查询。
- 策略禁止时原始行组不进入外部模型路径。

## 18. 完成标准

- 用户可以在 RAGv3 内只读打开 Excel/CSV，不依赖本地 Excel。
- 表格覆盖后，查看器、结构化查询和向量检索使用同一当前版本。
- 每个 LLM 答案能定位到工作表和行列范围。
- 工资、出勤等精确数值由确定性计算获得。
- 普通语义问题可以使用独立行组向量。
- 解析或索引失败不会破坏旧版本。
- 真实薪资数据的主管决策通过策略开关完成，不需要重写存储和查询架构。
