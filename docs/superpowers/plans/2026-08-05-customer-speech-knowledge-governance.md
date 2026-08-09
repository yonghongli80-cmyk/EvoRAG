# 客服、销售与主播话术知识治理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在同一个 RAGv3 平台内，让 Excel/CSV 话术以可追溯、可审核、可版本化缓存的固定答案优先返回，并把产品知识问题安全地回退到已有 RAG。

**Architecture:** 复用表格知识计划的版本 SQLite 与 `qa_pair`，增加一个只保存行引用和派生规范化字段的话术治理层。候选记录经过重复、冲突和风险扫描后由负责人发布；查询先查已发布固定话术，只有未命中或需解释的问题才使用已有可靠 RAG 链路。

**Tech Stack:** Python 3.12、FastAPI、SQLite FTS5、Pydantic v2、Qdrant、Redis（可选）、Vue 3、Element Plus、pytest、Vitest。

## 全局约束

- 本计划必须在可信知识闭环、增量资料生命周期、回答可靠性和表格知识计划完整通过后执行。
- 不新建账号、ACL、文件版本、Qdrant collection 或前端应用；复用已有逻辑文档、版本、权限版本和引用设施。
- 原始文件、版本 SQLite 和原始表格行永远不被本计划覆盖、删除或模型改写。
- 只有 `.xlsx`/`.csv` 且经用户确认问答列映射的工作表创建话术候选；Word/PDF 留在普通 RAG 产品知识路径。
- `candidate`、`superseded`、`disabled` 记录不得进入固定话术答案；只有 `published` 可返回。
- 任何同题异答、风险承诺或作用域不完整的记录必须审核；LLM/DeepSeek 只能给建议，不能发布、停用、合并或裁决政策。
- 固定话术路径不得调用 query rewrite、embedding、外部 reranker 或 LLM。
- 缓存键必须包含 `kb_id`、权限版本、角色、渠道、人设、发布纪元和规范化问题；读取缓存仍重新校验 ACL。
- 所有修改遵循 RED -> 最小 GREEN -> 回归 -> commit；每项任务独立可验收。

---

## 文件结构

### 新建后端

- `rag/speech_knowledge/models.py`：不可变输入、枚举、候选记录、答案卡和审核命令模型。
- `rag/speech_knowledge/normalizer.py`：文本、关键词和作用域的确定性规范化与指纹。
- `rag/speech_knowledge/importer.py`：从已激活 structured dataset 构建候选行。
- `rag/speech_knowledge/scanner.py`：完全重复、同题异答、作用域差异与风险扫描。
- `rag/speech_knowledge/publication.py`：乐观并发发布、停用、冲突决议和纪元递增。
- `rag/speech_knowledge/retrieval.py`：SQLite FTS 精确/关键词固定话术检索。
- `rag/speech_knowledge/cache.py`：带作用域和纪元的固定话术缓存适配器。
- `rag/speech_knowledge/api.py`：导入、审核、发布、查询与运营 API。
- `rag/speech_knowledge/evaluation.py`：固定话术评测集与指标计算。

### 修改后端

- `rag/spreadsheets/models.py`、`rag/spreadsheets/api.py`：显式 `QAPairImportMapping` 和导入预览确认。
- `rag/user_db.py`：话术治理迁移、读写接口、FTS 和审计。
- `rag/pipeline.py`：在通用 RAG 前调用固定话术分发器。
- `rag/citations.py`：AnswerCard 行范围引用序列化。
- `rag/redis_cache.py`：仅提供命名空间键值原语；不再用裸问题构建固定话术键。
- `rag/api.py`：注册话术路由与内部回答模式参数。
- `config.py`、`.env.example`：阈值、缓存 TTL、风险规则版本。

### 新建前端

- `frontend/src/api/speechKnowledge.ts`
- `frontend/src/components/ScriptAnswerCard.vue`
- `frontend/src/components/SpeechImportMappingDialog.vue`
- `frontend/src/components/SpeechReviewQueue.vue`
- `frontend/src/components/SpeechConflictReview.vue`
- `frontend/src/views/SpeechOperationsView.vue`

### 测试

- `tests/test_speech_schema.py`
- `tests/test_speech_normalizer.py`
- `tests/test_speech_importer.py`
- `tests/test_speech_scanner.py`
- `tests/test_speech_publication.py`
- `tests/test_speech_retrieval.py`
- `tests/test_speech_cache.py`
- `tests/test_speech_pipeline.py`
- `tests/test_speech_api.py`
- `tests/test_speech_evaluation.py`
- 前端对应 `*.test.ts`

---

### Task 1：问答映射、话术元数据和数据库迁移

**Files:**
- Create: `rag/speech_knowledge/models.py`, `tests/test_speech_schema.py`
- Modify: `rag/spreadsheets/models.py`, `rag/user_db.py`, `config.py`, `tests/conftest.py`

**Interfaces:**
- Produces `QAPairImportMapping(question_column_key: str, answer_column_key: str, optional_columns: Mapping[str, str])`。
- Produces `SpeechStatus = Literal['candidate', 'published', 'superseded', 'disabled']`。
- Produces `UserDB.create_speech_import_batch(dataset_id: int, profile: SpeechImportProfile) -> int`。
- Produces `UserDB.get_speech_publication_epoch(kb_id: str) -> int`。

- [ ] **Step 1: 写失败的唯一性和映射测试**

```python
def test_qa_mapping_requires_distinct_question_and_answer_columns():
    with pytest.raises(ValidationError):
        QAPairImportMapping(question_column_key="c_question", answer_column_key="c_question")


def test_one_speech_import_batch_per_dataset(speech_db):
    profile = SpeechImportProfile(source_class="reference", source_priority=20)
    speech_db.create_speech_import_batch(dataset_id=9, profile=profile)
    with pytest.raises(sqlite3.IntegrityError):
        speech_db.create_speech_import_batch(dataset_id=9, profile=profile)
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_schema.py -v
```

Expected: `ImportError` 或 `AttributeError`，因为话术模型和迁移尚不存在。

- [ ] **Step 3: 实现枚举、映射和迁移**

在 `rag/spreadsheets/models.py` 增加不可变映射模型；问题列与回答列必须不同，所有 optional key 必须来自已确认工作表列。`rag/user_db.py` 创建 `speech_import_batches`、`speech_knowledge_records`、`speech_duplicate_clusters`、`speech_duplicate_members`、`speech_conflicts`、`speech_risk_findings`、`speech_risk_decisions`、`speech_publication_epochs` 和 FTS5 索引。

```python
class QAPairImportMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    question_column_key: str
    answer_column_key: str
    optional_columns: Mapping[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def distinct_required_columns(self) -> Self:
        if self.question_column_key == self.answer_column_key:
            raise ValueError("question and answer columns must differ")
        return self
```

`speech_knowledge_records` 必须唯一约束 `(batch_id, sheet_key, row_number)`，并保留 `dataset_id`、`document_version_id`、`sheet_key`、`row_number`。`record_fingerprint` 与 `scope_fingerprint` 只建立普通索引，不能建立唯一约束，否则会丢失来自不同文件的相同原始资料。发布纪元初始为 0。

- [ ] **Step 4: 运行 schema 与既有表格测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_schema.py tests\test_spreadsheet_schema.py tests\test_user_db.py -v
```

Expected: PASS。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/models.py rag/spreadsheets/models.py rag/user_db.py config.py tests/conftest.py tests/test_speech_schema.py
git commit -m "feat: add speech knowledge metadata"
```

---

### Task 2：确定性规范化与候选记录导入

**Files:**
- Create: `rag/speech_knowledge/normalizer.py`, `rag/speech_knowledge/importer.py`
- Test: `tests/test_speech_normalizer.py`, `tests/test_speech_importer.py`
- Modify: `rag/spreadsheets/api.py`, `rag/user_db.py`

**Interfaces:**
- Produces `normalize_speech_text(value: str | None) -> str | None`。
- Produces `normalize_keywords(value: str | None) -> tuple[str, ...]`。
- Produces `build_scope_fingerprint(record: SpeechDraft) -> str`。
- Produces `import_speech_candidates(batch_id: int, mapping: QAPairImportMapping, actor: dict) -> ImportSpeechResult`。

- [ ] **Step 1: 写失败的规范化与来源保留测试**

```python
def test_normalizer_preserves_raw_but_unifies_spacing_and_punctuation():
    result = normalize_speech_text("  7 天 无理由 ， 包 邮  ")
    assert result == "7 天 无理由, 包 邮"


def test_import_keeps_dataset_sheet_and_original_row(speech_fixture):
    result = speech_fixture.import_rows([
        {"row_number": 12, "问题Q": "能退吗？", "标准回答A": "按规则处理"}
    ])
    record = result.records[0]
    assert (record.dataset_id, record.sheet_key, record.row_number) == (9, "sheet_0001", 12)
    assert record.status == "candidate"
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_normalizer.py tests\test_speech_importer.py -v
```

Expected: FAIL，因为导入和规范化函数不存在。

- [ ] **Step 3: 实现不改原文的候选导入**

使用 `unicodedata.normalize('NFKC', value)`、空白折叠和有限标点翻译构建规范化字段；原始值从不修改。关键词只按 `、,，;；` 拆分、去空、按第一次出现保序去重。问题或回答为空时创建 `candidate` 并附 `missing_question`/`missing_answer` 风险，不丢行。

```python
def record_fingerprint(draft: SpeechDraft) -> str:
    payload = "\x1f".join((
        draft.question_normalized or "",
        draft.answer_normalized or "",
        draft.scope_fingerprint,
        str(draft.document_version_id),
        draft.sheet_key,
        str(draft.row_number),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

导入 API 必须先重新校验来源文档编辑权限、dataset/version 归属和映射列存在性；使用 `(batch_id, idempotency_key)` 防重复。结果返回候选数、缺失数和可定位的行错误。

- [ ] **Step 4: 运行导入、ACL 和表格 API 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_normalizer.py tests\test_speech_importer.py tests\test_spreadsheet_api.py tests\test_document_security_boundaries.py -v
```

Expected: PASS；原始结构化表没有写入或改动。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/normalizer.py rag/speech_knowledge/importer.py rag/spreadsheets/api.py rag/user_db.py tests/test_speech_normalizer.py tests/test_speech_importer.py
git commit -m "feat: import speech candidates from spreadsheets"
```

---

### Task 3：重复簇、同题异答与风险扫描

**Files:**
- Create: `rag/speech_knowledge/scanner.py`
- Test: `tests/test_speech_scanner.py`
- Modify: `rag/user_db.py`, `config.py`

**Interfaces:**
- Produces `scan_speech_batch(batch_id: int) -> SpeechScanResult`。
- Produces `classify_pair(left: SpeechRecord, right: SpeechRecord) -> PairRelation`。
- Produces `scan_risks(record: SpeechRecord) -> tuple[RiskFinding, ...]`。

- [ ] **Step 1: 写失败的重复、作用域和风险测试**

```python
def test_same_question_answer_and_scope_becomes_duplicate_cluster(scanner_fixture):
    result = scanner_fixture.scan([
        record(question="能退吗？", answer="支持七天", channel="shop"),
        record(question="能退吗", answer="支持七天", channel="shop"),
    ])
    assert result.duplicate_cluster_count == 1
    assert result.conflict_count == 0


def test_same_question_with_different_scope_is_not_auto_conflict(scanner_fixture):
    result = scanner_fixture.scan([
        record(question="保修多久", answer="365 天", category="相机"),
        record(question="保修多久", answer="180 天", category="耳机"),
    ])
    assert result.scope_difference_count == 1
    assert result.conflict_count == 0


def test_absolute_and_time_sensitive_words_are_findings(scanner_fixture):
    findings = scanner_fixture.risks("100%好评，近30天最低价，包来回运费")
    assert {item.kind for item in findings} >= {"absolute_claim", "time_sensitive", "after_sales_claim"}
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_scanner.py -v
```

Expected: FAIL，因为扫描器和风险规则不存在。

- [ ] **Step 3: 实现确定性扫描器**

完全重复键为 `question_normalized + answer_normalized + scope_fingerprint`。冲突键为 `question_normalized + scope_fingerprint`；同一冲突键存在不同非空答案时，创建或更新活跃 `speech_conflicts`，并同步调用回答可靠性方案中的冲突候选存储。作用域字段有差异时只记录 `scope_difference`，不能创建冲突或更改任何记录状态。

风险规则从 `settings.speech_risk_rule_version` 对应的只读词典加载，匹配结果保存命中位置和规则 ID，避免只保存不可解释的布尔值。

```python
def classify_pair(left: SpeechRecord, right: SpeechRecord) -> PairRelation:
    if left.question_normalized != right.question_normalized:
        return "unrelated"
    if left.scope_fingerprint != right.scope_fingerprint:
        return "same_question_different_scope"
    if left.answer_normalized == right.answer_normalized:
        return "exact_duplicate"
    return "same_question_conflicting_answer"
```

- [ ] **Step 4: 运行扫描、可靠性冲突与回归测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_scanner.py tests\test_conflicts.py tests\test_answerability.py -v
```

Expected: PASS；扫描是幂等的，不产生重复活跃冲突。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/scanner.py rag/user_db.py config.py tests/test_speech_scanner.py
git commit -m "feat: scan speech duplicates conflicts and risks"
```

---

### Task 4：审核、发布、停用与发布纪元

**Files:**
- Create: `rag/speech_knowledge/publication.py`
- Test: `tests/test_speech_publication.py`
- Modify: `rag/user_db.py`, `rag/knowledge_tasks.py`

**Interfaces:**
- Produces `publish_speech_record(record_id: int, actor: dict, expected_revision: int, idempotency_key: str) -> SpeechRecord`。
- Produces `resolve_speech_conflict(conflict_id: int, resolution: SpeechConflictResolution, actor: dict, idempotency_key: str) -> dict`。
- Produces `disable_speech_record(record_id: int, reason: str, actor: dict, expected_revision: int) -> SpeechRecord`。

- [ ] **Step 1: 写失败的发布前置条件和并发测试**

```python
def test_publish_rejects_active_conflict_and_does_not_increment_epoch(publication_fixture):
    record = publication_fixture.record_with_active_conflict()
    with pytest.raises(ActiveSpeechConflict):
        publication_fixture.publish(record.id, expected_revision=0)
    assert publication_fixture.epoch() == 0


def test_publish_uses_compare_and_swap_and_increments_epoch_once(publication_fixture):
    record = publication_fixture.clean_candidate()
    published = publication_fixture.publish(record.id, expected_revision=0, key="publish-1")
    replay = publication_fixture.publish(record.id, expected_revision=0, key="publish-1")
    assert published.status == replay.status == "published"
    assert publication_fixture.epoch() == 1
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_publication.py -v
```

Expected: FAIL，因为发布服务尚不存在。

- [ ] **Step 3: 实现单事务发布操作**

一个 `BEGIN IMMEDIATE` 事务中：重新读取记录和来源 ACL、验证 revision、验证无活跃冲突、验证每个风险 finding 已有明确决定、更新状态/审核字段、写审计事件、递增知识库发布纪元、保存幂等结果。`source_priority` 仅用于候选排序，不能绕过冲突或风险审核。

```python
def publish_speech_record(...):
    with db.transaction(immediate=True):
        record = db.require_speech_record_for_update(record_id)
        ensure_expected_revision(record, expected_revision)
        require_source_edit_or_owner(actor, record)
        ensure_no_active_conflict(record)
        ensure_risks_decided(record)
        db.mark_speech_published(record_id, actor["id"])
        epoch = db.bump_speech_publication_epoch(record.kb_id)
        return db.store_idempotent_publication_result(idempotency_key, record_id, epoch)
```

停用、解决冲突、作用域变更也必须递增纪元。任何操作失败都不改变状态或纪元。

- [ ] **Step 4: 运行发布、审计、任务和权限测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_publication.py tests\test_reliability_operations.py tests\test_document_security_boundaries.py -v
```

Expected: PASS；非负责人不能发布，重放请求不产生第二次状态变化。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/publication.py rag/user_db.py rag/knowledge_tasks.py tests/test_speech_publication.py
git commit -m "feat: publish reviewed speech knowledge"
```

---

### Task 5：固定话术检索与作用域安全缓存

**Files:**
- Create: `rag/speech_knowledge/retrieval.py`, `rag/speech_knowledge/cache.py`
- Test: `tests/test_speech_retrieval.py`, `tests/test_speech_cache.py`
- Modify: `rag/redis_cache.py`, `config.py`

**Interfaces:**
- Produces `find_fixed_script(query: SpeechQueryScope) -> SpeechLookupDecision`。
- Produces `SpeechQueryScope(kb_id, user, audience, channel, persona, question)`。
- Produces `SpeechAnswerCard`。
- Produces `SpeechCache.get(scope: SpeechQueryScope, epoch: int) -> SpeechAnswerCard | None`。

- [ ] **Step 1: 写失败的精确命中、歧义与缓存隔离测试**

```python
def test_exact_published_script_returns_without_rag(retrieval_fixture):
    result = retrieval_fixture.lookup(question="能退吗？", audience="customer_service", channel="shop")
    assert result.kind == "fixed_script"
    assert result.card.is_fixed_script is True


def test_two_published_scopes_require_clarification(retrieval_fixture):
    result = retrieval_fixture.lookup(question="保修多久", audience="customer_service", channel="shop")
    assert result.kind == "clarification_required"
    assert "品类" in result.required_fields


def test_cache_key_changes_with_epoch_and_channel(cache_fixture):
    first = cache_fixture.key(question="能退吗", channel="shop", epoch=2)
    second = cache_fixture.key(question="能退吗", channel="live", epoch=2)
    third = cache_fixture.key(question="能退吗", channel="shop", epoch=3)
    assert len({first, second, third}) == 3
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_retrieval.py tests\test_speech_cache.py -v
```

Expected: FAIL，因为查询与缓存适配器不存在。

- [ ] **Step 3: 实现 FTS 优先查询与 AnswerCard**

先用规范化问题做等值查询；没有单一结果时，只在同一 `kb_id`、当前可见 document version、`published` 状态和匹配作用域内查询 FTS5。作用域筛选按 audience/channel/persona 的精确匹配或通配空值进行，品牌、型号、品类、场景必须由用户问题/已选上下文显式满足。多个不同记录仍可能匹配时返回澄清字段，不能用 LLM 猜。

```python
class SpeechAnswerCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    record_id: int
    answer: str
    cannot_promise: str | None
    suggested_follow_up: str | None
    handoff_hint: str | None
    citation: CitationRef
    audience: str
    channel: str | None
    persona: str | None
    is_fixed_script: Literal[True] = True
```

缓存键使用 SHA-256，输入为序列化后的 `kb_id, permission_version, audience, channel, persona, epoch, normalized_question`。缓存命中后重新调用 `require_doc_view` 和 `require_current_version`；任何失败均视为未命中。

- [ ] **Step 4: 运行检索、缓存、ACL 和引用测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_retrieval.py tests\test_speech_cache.py tests\test_answer_citations.py tests\test_document_security_boundaries.py -v
```

Expected: PASS；`candidate`、历史版本或撤销权限的记录绝不命中。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/retrieval.py rag/speech_knowledge/cache.py rag/redis_cache.py config.py tests/test_speech_retrieval.py tests/test_speech_cache.py
git commit -m "feat: retrieve published speech scripts quickly"
```

---

### Task 6：接入 RAG 流水线、引用和反馈闭环

**Files:**
- Modify: `rag/pipeline.py`, `rag/citations.py`, `rag/api.py`, `rag/feedback_processor.py`
- Create: `tests/test_speech_pipeline.py`

**Interfaces:**
- Consumes `find_fixed_script(SpeechQueryScope)`。
- Produces `QueryResult.answer_card: SpeechAnswerCard | None` 与既有 `sources`。
- Produces API 响应字段 `answer_mode: 'fixed_script' | 'rag' | 'clarification_required' | 'refusal'`。

- [ ] **Step 1: 写失败的短路与回退测试**

```python
def test_pipeline_fixed_script_skips_rewrite_embed_rerank_and_generate(monkeypatch, pipeline):
    monkeypatch.setattr(pipeline.speech_retriever, "find", lambda _: fixed_card())
    for name in ("rewrite_query", "embed", "generate"):
        monkeypatch.setattr("rag.pipeline." + name, lambda *_: pytest.fail(name + " called"))
    result = pipeline.query("能退吗？", audience="customer_service", channel="shop")
    assert result.answer_card.is_fixed_script


def test_pipeline_falls_back_to_reliable_rag_when_no_script(pipeline):
    result = pipeline.query("索尼 A5000 续航如何？", audience="sales", channel="shop")
    assert result.answer_mode == "rag"
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_pipeline.py -v
```

Expected: FAIL，因为 RAGPipeline 未接入话术分发器。

- [ ] **Step 3: 在通用 RAG 前加入安全短路**

`RAGPipeline._prepare_context()` 在输入安全检查和用户身份解析后调用固定话术检索。固定答案直接生成 AnswerCard 和行范围引用；澄清结果不调用生成模型；只有 `no_match` 才执行现有 rewrite/retrieve/rerank/generate。流式接口对固定答案一次性发送 `answer_card` 事件和 `done`。

反馈记录必须包含 `record_id`、`publication_epoch` 和 `answer_mode`；“内容过期”反馈为该记录创建负责人任务，但不自动停用。

- [ ] **Step 4: 运行流水线、流式、反馈和可靠性回归**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_pipeline.py tests\test_pipeline.py tests\test_pipeline_stream.py tests\test_feedback_processor.py tests\test_answerability.py -v
```

Expected: PASS；固定话术无外部模型调用，普通问题仍有原始引用。

- [ ] **Step 5: 提交**

```powershell
git add rag/pipeline.py rag/citations.py rag/api.py rag/feedback_processor.py tests/test_speech_pipeline.py
git commit -m "feat: route fixed scripts before rag generation"
```

---

### Task 7：审核 API、导入映射和员工答案卡界面

**Files:**
- Create: `rag/speech_knowledge/api.py`, `tests/test_speech_api.py`
- Create: `frontend/src/api/speechKnowledge.ts`, `frontend/src/components/ScriptAnswerCard.vue`, `frontend/src/components/SpeechImportMappingDialog.vue`, `frontend/src/components/SpeechReviewQueue.vue`, `frontend/src/components/SpeechConflictReview.vue`, `frontend/src/views/SpeechOperationsView.vue`
- Modify: `rag/api.py`, `frontend/src/router/index.ts`, `frontend/src/views/ChatView.vue`
- Test: corresponding frontend tests

**Interfaces:**
- Produces `POST /speech-knowledge/imports/preview`、`POST /speech-knowledge/imports/{id}/scan`。
- Produces `GET /speech-knowledge/review`、`PUT /speech-knowledge/records/{id}/publish`、`PUT /speech-knowledge/records/{id}/disable`、`PUT /speech-knowledge/conflicts/{id}/resolve`。
- Produces client `SpeechKnowledgeApi.publish(recordId, expectedRevision, idempotencyKey)`。

- [ ] **Step 1: 写失败的 API 权限和 UI 行为测试**

```python
def test_only_owner_or_admin_can_publish(speech_api, reviewer_headers, employee_headers):
    assert speech_api.put("/speech-knowledge/records/7/publish", headers=employee_headers).status_code == 403
    assert speech_api.put("/speech-knowledge/records/7/publish", headers=reviewer_headers).status_code == 200
```

```typescript
it('copies fixed script and exposes source without edit controls', async () => {
  const wrapper = mount(ScriptAnswerCard, { props: { card: fixedCard } })
  await wrapper.get('[data-testid="copy-script"]').trigger('click')
  expect(wrapper.text()).toContain('来源')
  expect(wrapper.find('[data-testid="edit-script"]').exists()).toBe(false)
})
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_api.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run src/components/ScriptAnswerCard.test.ts
Set-Location ..
```

Expected: FAIL，因为路由和组件不存在。

- [ ] **Step 3: 实现 API 与只读/审核界面**

预览 API 只返回经 ACL 校验的 schema、样本行和可选列；确认映射才创建 batch。审核队列必须显示来源文件、工作表、行号、原文/规范化预览、作用域、风险和冲突关联。发布/停用/解决动作发送 revision 与幂等键。员工端答案卡提供复制、来源、不能承诺、建议追问和转人工提示，不提供编辑入口。

- [ ] **Step 4: 运行后端、前端与 ACL 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_api.py tests\test_document_security_boundaries.py -v
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
```

Expected: PASS；撤销权限后审核页和来源链接均拒绝访问。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/api.py rag/api.py frontend/src/api/speechKnowledge.ts frontend/src/components frontend/src/views/SpeechOperationsView.vue frontend/src/router/index.ts frontend/src/views/ChatView.vue tests/test_speech_api.py
git commit -m "feat: add speech knowledge review and answer cards"
```

---

### Task 8：清洗批次迁移、评测和运营文档

**Files:**
- Create: `rag/speech_knowledge/evaluation.py`, `tests/test_speech_evaluation.py`
- Modify: `.env.example`, `docs/deployment-guide.md`, `docs/development-log-security-boundary-hardening.md`
- Add: `data/eval/speech_script_eval.jsonl`（只含虚构数据）

**Interfaces:**
- Produces `evaluate_speech_scripts(cases: Iterable[SpeechEvalCase]) -> SpeechEvalReport`。
- Produces metrics `exact_script_hit_rate`、`wrong_scope_rate`、`unresolved_conflict_block_rate`、`fixed_path_external_call_rate`、`p95_fixed_path_ms`。

- [ ] **Step 1: 写失败的评测与冲突拦截测试**

```python
def test_eval_counts_conflict_block_as_success_when_no_script_is_returned():
    report = evaluate_speech_scripts([
        SpeechEvalCase(question="保修多久", expected_mode="clarification_required"),
    ])
    assert report.unresolved_conflict_block_rate == 1.0


def test_fixed_path_requires_zero_external_calls(eval_fixture):
    result = eval_fixture.run_fixed_case("能退吗")
    assert result.external_calls == 0
```

- [ ] **Step 2: 运行 RED**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_speech_evaluation.py -v
```

Expected: FAIL，因为评测模块和虚构样本不存在。

- [ ] **Step 3: 实现评测、批次验收和文档**

评测不得使用真实客户、薪资、订单或聊天数据。部署文档写明清洗指南路径、导入顺序、审查责任、缓存 TTL、风险规则版本、如何回滚一条发布记录，以及固定话术不等于自动回复。清洗批次只有满足以下条件才能建议进入试点：

```text
所有候选有来源行
所有同题异答都有未解决标记或审核决议
所有风险 finding 都有审核状态
固定路径外部调用数为 0
真实问题评测集的错误作用域率为 0
```

- [ ] **Step 4: 完整验证**

```powershell
& .\.venv\Scripts\python.exe -m pytest -q
& .\.venv\Scripts\python.exe -m ruff check rag/speech_knowledge rag/spreadsheets rag tests/test_speech_schema.py tests/test_speech_normalizer.py tests/test_speech_importer.py tests/test_speech_scanner.py tests/test_speech_publication.py tests/test_speech_retrieval.py tests/test_speech_cache.py tests/test_speech_pipeline.py tests/test_speech_api.py tests/test_speech_evaluation.py
Set-Location frontend
& .\node_modules\.bin\vitest.cmd run
& npm.cmd run build
Set-Location ..
git diff --check
git status --short data qdrant_data
```

Expected: 所有测试通过；没有真实业务数据进入仓库、测试夹具或评测集。

- [ ] **Step 5: 提交**

```powershell
git add rag/speech_knowledge/evaluation.py tests/test_speech_evaluation.py data/eval/speech_script_eval.jsonl .env.example docs/deployment-guide.md docs/development-log-security-boundary-hardening.md
git commit -m "docs: add speech knowledge governance operations"
```

## 执行顺序与试点

1. 完成企业计划索引中的步骤 1-4。
2. 执行本计划 Task 1-8。
3. 第一批只建立 `话术库_修订审核版.xlsx` 和统一经营规则的候选 batch；不得自动发布。
4. 负责人解决冲突和风险后，发布少量高频客服话术。
5. 用 20-50 个真实但脱敏的问题做评测，再扩大到销售和主播模式。

不要把 `知识点1.xlsx`、`知识点2.xlsx`、分类页、旧版话术和个人话术一次性都设为已发布来源；它们先进入候选/比对批次。
