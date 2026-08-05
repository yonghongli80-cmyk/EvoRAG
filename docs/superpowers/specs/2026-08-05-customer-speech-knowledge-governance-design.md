# 客服、销售与主播话术知识治理设计

## 1. 目标

在同一个 RAGv3 平台中，将现有 Excel/CSV、Word/PDF 培训资料和后续上传资料转化为可追溯、可审核、可快速命中的话术知识。系统优先返回已经发布的固定话术；只有固定话术未命中或问题需要产品解释时，才进入已有的混合检索与生成链路。

本设计不创建第二个知识库、第二套权限或第二个前端。它扩展可信知识闭环、增量资料生命周期、回答可靠性和表格知识方案。

## 2. 范围与非目标

### 本期范围

- `.xlsx`/`.csv` 的行级问答导入、清洗、复核、发布和快速查询。
- `.docx`/`.pdf` 培训资料保留为普通版本化资料，用于产品知识兜底检索。
- 三个使用模式：`customer_service`、`sales`、`streamer`；它们共享身份、权限、资料和后端。
- 完全重复、同题异答、风险承诺、缺失字段和疑似上下文不一致的待审核队列。
- 已发布固定话术的精确命中、版本化缓存、复制卡片和来源引用。

### 非目标

- 不自动判断哪条售后、质保、价格或运费政策“业务上正确”。
- 不覆盖、删除、移动或改写原始文件。
- 不让 LLM 直接发布、删除、合并或解决冲突。
- 不把所有原始资料合并成一个 Markdown 文件。
- 不做外部平台自动回复；首版只提供内部可复制答案。

## 3. 已有方案的复用边界

| 已有方案 | 直接复用 | 本设计新增 |
| --- | --- | --- |
| 可信知识闭环 | 逻辑文档、版本、ACL、负责人、引用、反馈 | 话术记录的发布状态与来源优先级 |
| 增量资料生命周期 | 不可变原件、作业、嵌入缓存、版本切换 | 话术记录指纹和发布纪元失效 |
| 回答可靠性 | 证据包、冲突表、拒答、置信度 | 问题/回答行级冲突和固定话术拦截 |
| 表格知识 | 版本 SQLite、`qa_pair`、表格引用、行级查询 | 显式问答列映射、清洗候选与审核工作台 |
| 现有 RAGv3 | 登录、权限、Qdrant、BM25、流式回答、反馈 | 精确话术优先路由与安全缓存键 |

## 4. 数据模型

原始表格仍通过 `document_id -> document_version_id -> structured_dataset` 保存。话术层只保存对原始单元格的引用，绝不复制后覆盖原件。

```text
原始文件版本
  -> structured_dataset / sheet / row
      -> speech_import_batch
          -> speech_knowledge_record (candidate/published/superseded/disabled)
              -> duplicate cluster / conflict / risk finding
              -> speech_publication_epoch
```

### 4.1 `speech_import_batches`

- `id`、`dataset_id UNIQUE`、`document_version_id`、`profile_id`、`status`。
- `status`: `staging | scanned | review_required | published | failed`。
- `source_class`: `authoritative_candidate | reference | historical | personal_style | training`。
- `source_priority`: 仅由管理员在导入配置中设定，范围 0-100；它不是自动发布许可。
- `content_hash`、`scanner_version`、`created_at`、`completed_at`、`last_error_code`。

### 4.2 `speech_knowledge_records`

- `id`、`batch_id`、`dataset_id`、`document_version_id`、`sheet_key`、`row_number`。
- `question_raw`、`question_normalized`、`answer_raw`、`answer_normalized`。
- `category`、`keywords_json`、`audiences_json`、`channels_json`、`persona`。
- `cannot_promise`、`suggested_follow_up`、`handoff_hint`。
- `status`: `candidate | published | superseded | disabled`。
- `record_fingerprint`、`scope_fingerprint`、`published_revision`、`published_at`、`published_by`。前两个字段建立普通索引，**不得全局唯一**：相同内容可能来自多个原始文件，必须保留全部来源链路。

`record_fingerprint` = 规范化问题、规范化回答、结构化作用域和来源行内容的 SHA-256。`scope_fingerprint` 不包含回答，用于找同题异答。

### 4.3 审核派生表

- `speech_duplicate_clusters`：完全相同问题、回答、作用域的可合并记录；不会物理删除成员。
- `speech_conflicts`：同一作用域内同题异答、或风险字段与回答不一致；关联已有 `knowledge_conflicts`，保留所有行引用。
- `speech_risk_findings`：`time_sensitive | absolute_claim | price_claim | after_sales_claim | missing_scope | missing_answer | malformed_row`。
- `speech_risk_decisions`：风险项的保留、改写、禁用或误报结论、说明、处理人和记录修订号；重新导入或改写后，旧结论不得自动复用。
- `speech_publication_epochs`：每个知识库一行单调递增整数；任何发布、停用、解决冲突、变更作用域都递增，用于缓存失效。

## 5. 导入与清洗规则

### 5.1 导入配置必须显式确认

每个候选工作表先在预览中确认：表头行、数据起始行、问题列、回答列，以及可选的分类、关键词、不能承诺、建议追问、转人工、角色、渠道、优先级、状态和版本列。只有确认 `question_column` 与 `answer_column` 的表才使用 `qa_pair`。

不具备稳定问答列的表继续作为 `row_groups` 或普通文档，不伪装成问答库。

### 5.2 安全的自动清洗

自动清洗只生成候选字段：Unicode NFKC、全角/半角空格统一、首尾空白去除、连续空白折叠、常见中文标点统一、空单元格转 `NULL`、关键词按中英文逗号/顿号切分后去重。原始字段始终保留。

不得自动删除：任何原始行、任何问题、任何回答、任何来源文件。不得将不同问题的语义相似度当作重复事实。

### 5.3 聚类与冲突

1. 完全重复：`question_normalized + answer_normalized + scope_fingerprint` 相同，进入重复簇。
2. 同题异答：`question_normalized + scope_fingerprint` 相同、`answer_normalized` 不同，创建待审核冲突。
3. 作用域不同：角色、渠道、品类、品牌、型号、场景或人设任何一项不同，不自动判为冲突，只创建“相似但作用域不同”提示。
4. 语义相似：只作为人工复核建议，不能自动合并、停用或发布。

### 5.4 风险规则

风险扫描仅标记，不自动改写。初始规则包括：

- 时间敏感：门店数量、发货时效、价格周期、质保/退货天数。
- 绝对承诺：`100%`、`绝对`、`保证`、`完美`、`全平台最低`、`零风险`。
- 售后/物流承诺：`七天无理由`、`包来回运费`、`承担运费`、`一年质保`、`365天`。
- 缺失作用域：问题或回答涉及型号、渠道、价格、门店、活动，但没有相应作用域字段。

风险不是禁用结论。审核人可发布、改为受限角色、要求改写、标记过期或停用，并留下原因。

## 6. 发布规则与权威性

只有 `published` 记录可被固定话术快速通道返回。`candidate`、`superseded`、`disabled` 不能作为固定答案；其中 `superseded` 可在管理端查看，`disabled` 仅保留审计。

发布要求：

1. 问题、回答和来源行有效。
2. 当前用户对来源文档有编辑权限或是指定负责人/管理员。
3. 关联的活跃冲突必须已解决，或记录明确声明不同作用域。
4. 高风险发现必须有审核决定。
5. 发布操作带 `expected_published_revision`，使用乐观并发控制和幂等键。

文件名包含“审核版”只能作为导入时的推荐来源类别，不自动赋予权威或发布状态。

## 7. 查询路径

```text
用户问题 + 模式/渠道/人设
  -> 规范化与 ACL/当前版本过滤
  -> 已发布话术精确查找
       -> 单一匹配：直接返回 AnswerCard
       -> 多个作用域匹配：要求选择型号/渠道/场景
       -> 活跃冲突：不生成固定答案，返回待澄清
  -> 已发布话术 FTS/BM25 候选
       -> 高置信且无冲突：直接返回 AnswerCard
  -> 现有可靠 RAG 检索、重排、生成与引用
       -> 无充分证据：拒答并记录知识缺口
```

`AnswerCard` 包含：可复制回答、不能承诺、建议追问、转人工提示、角色/渠道标签、来源工作表和行范围、版本更新时间、置信度和 `is_fixed_script=true`。

固定话术路径不调用问题改写、嵌入、外部 reranker 或 LLM；它只使用受 ACL 约束的 SQLite/FTS 查询，因此比通用 RAG 更快且不会改写已批准文案。

## 8. 缓存与失效

只缓存已发布固定话术的最终 `AnswerCard`。缓存键必须包含：

`kb_id + permission_version + audience + channel + persona + publication_epoch + normalized_question`。

不得使用只有 question 文本的缓存键。任何发布、停用、作用域调整、冲突解决、权限版本变更都会使旧键不可命中；读取缓存时仍重新验证 ACL。

## 9. 用户界面与运营

- 员工端：模式切换、提问、答案卡、一键复制、查看来源、反馈“有用/无用/过期”。
- 审核端：导入预览、列映射、重复簇、冲突、风险、发布/停用、变更原因和来源定位。
- 不同模式只影响查询作用域和回答显示，不创建独立应用或独立账号体系。

## 10. 验收标准

- 导入原始文件后，可以从任一话术记录回到原始工作表和行。
- 同题同答只产生一个可发布逻辑答案；原始来源仍完整可追溯。
- 同题异答不会被自动静默选择；未解决时固定话术路径要求澄清。
- 发布/停用后旧缓存不再返回。
- 固定话术查询不调用外部 reranker 或 LLM。
- 任一答案卡能显示来源、版本、风险审核状态和角色/渠道作用域。
- 产品知识等非固定问答仍能通过已有 RAG 路径回答并给出引用。
