# Hypertext Wiki Substrate & Knowledge-Loop Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 基于架构白皮书（`docs/architecture-hybrid-knowledge-and-evolution.md`）落地“双模态知识底座”之中的第二模态——**网状超文本 Wiki 知识子系统（Hypertext Wiki Substrate）**，并打通自演进系统“轨道 B：知识缺失治理闭环（Track B: Knowledge Gap Remediation）”，使线上 78% 的内容缺口工单（`KnowledgeGapTicket`）能够直接通过原子化 Wiki 录入/补齐，并由后台 Worker 自动复测转为 `content_added` 终态。

**Architecture:** 
1. **数据层（`rag/wiki/`）**：在现有二维表格（`rag/spreadsheets/`）之外，建立原子化网状超文本存储层。采用 SQLite 持久化 Wiki 页面与其版本快照历史（`wiki_pages` 与 `wiki_revisions`），支持双向引用关联解析（`[[PageTitle]]`）与原子化回滚。
2. **解析切块层（`rag/wiki/chunker.py`）**：构建基于 Markdown AST 的结构感知分块器（AST & Structure-Aware Chunker），严格遵循章节标题与表格块边界切块，禁止跨标题断裂；自动将元数据（页面标题、层级大纲面包屑路径、关联标签）作为前缀隐式挂载在 Chunk 文本中，确保向量化后保留完整上下文。
3. **混合索引层（`rag/wiki/indexer.py`）**：无缝对接现有 Qdrant（`bge-m3` 1024 维）与 SQLite BM25 倒排索引，挂载 `source_type="wiki"`，严格继承 `kb_id` 与企业组织 ACL 过滤。
4. **闭环治理轨（`rag/evolution/gap_remediation.py`）**：为现有 `KnowledgeGapTicket` 提供一键知识补录（Wiki/Excel）挂钩端点，打通“工单识别 $\to$ 录入 Wiki 词条 $\to$ materials 增量编译 $\to$ Worker 定期重检探测 $\to$ 自动转 `content_added`”的数据驱动治理闭环。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLite、Qdrant、markdown-it-py (AST 解析)、pytest、Vue 3 + vitest；沿用既有架构与密码学/鉴权模式，不引入臃肿外部框架。

**Spec:** `docs/architecture-hybrid-knowledge-and-evolution.md`（第 1 节双模态知识底座、第 2.2 节网状超文本 Wiki、第 4.3 节轨道 B 知识缺失治理、第 5 节系统安全红线）；前置：第 3 阶段自演进工单（`docs/evolution-gap-tickets.md`）。

---

## Global Constraints

- **双模态正交隔离**：Wiki 模块独立驻留于 `rag/wiki/`，与二维表格 `rag/spreadsheets/` 彼此正交，不可相互污染。公共抽象经由 `rag/pipeline.py` 与 `rag/indexer.py` 汇聚。
- **结构感知切块禁跨界**：分块必须基于语法树（AST）切分，切块不得跨越二级及以上标题（`##`、`###`），不得在表格中途硬截断；每个切块必须带有上下文面包屑前缀（`[Wiki: 页面名 > 章节名]`）。
- **知识缺口不触代码**：轨道 B（知识缺失工单）严格遵循白皮书第 5.4 条“数据驱动真实性”红线——**绝对禁止**通过在代码中硬编码 `if-else` 特判来修复知识缺口。没有知识即判定为工单，补录 Wiki 词条闭环，严禁动用代码补丁轨。
- **工单与自演进不可篡改保护区**：工单补录只修改 Wiki 页面与知识库向量，严禁触碰 `rag/evolution/**`（自演进核心）、`tests/**`、`rag/auth.py`、`rag/permissions.py` 或 `config.py`。
- **全链路留痕与秒级回滚**：Wiki 页面任何变动均记录在 `wiki_revisions`（含作者、变更前后差量、sha256 快照哈希）；支持通过版本号秒级回滚，回滚时自动触发增量重索引。
- **在线查询零额外开销**：Wiki 词条编译入库后走统一的 Qdrant + BM25 + RRF 融合管道，在线生成路径强制执行“无证据绝不回答（No Evidence, No Answer）”原则。

---

## Review Focus

- **AST 切块完备性**：Markdown AST 解析器能否正确提取多级标题层级（Breadcrumbs），表格是否完整保留，是否严格拒绝跨大纲硬截断；覆盖于 Task 2 测试。
- **双向链接与引用解析**：`[[目标页面]]` 双向引用的提取与反向引用追踪是否正确无遗漏；页面重命名时引用完整性是否可审计；覆盖于 Task 1 测试。
- **工单补录到复测的闭环状态机**：针对 `undetermined` 内容缺口工单，Wiki 词条录入并发布后，Worker 复测探测能否自动识别关键词命中并正确推进状态到 `content_added`；覆盖于 Task 4 测试。
- **权限与隔离性**：Wiki 页面的增删改查是否完整继承现有组织架构权限与知识库隔离（`kb_id`、`require_admin` / 编目权限）；覆盖于 Task 5 测试。

---

## File Map

- `rag/wiki/models.py`（新建）：`WikiPage`、`WikiRevision`、`WikiLink` 领域数据模型与 Pydantic 契约。
- `rag/wiki/store.py`（新建）：`WikiStore`（SQLite 底层：`wiki_pages`、`wiki_revisions`、`wiki_links` DDL、事务增删改查、双向链接提取与版本回滚）。
- `rag/wiki/chunker.py`（新建）：`MarkdownASTChunker`（基于 Markdown AST 的层级感知切块，注入父级标题面包屑上下文与实体标签）。
- `rag/wiki/indexer.py`（新建）：`WikiIndexer`（编译 Wiki 切块为 DocumentChunk，协同 Qdrant 与 BM25 增量同步与物理删除清理）。
- `rag/evolution/gap_remediation.py`（新建）：`GapRemediationService`（连接 `KnowledgeGapTicket` 与 Wiki/表格录入，补录后自动触发复测探测）。
- `rag/evolution/worker.py`（修改）：`make_evolution_gap_ticket_step` 增加工单转 `content_added` 自动化状态扭转判定逻辑。
- `rag/evolution/api.py`（修改）：增加工单关联录入路由（`POST /gap-tickets/{id}/remediation`）。
- `rag/wiki/api.py`（新建）：Wiki REST API 端点（页面 CRUD、版本历史对比、双向引用查询、草稿发布）。
- `rag/api.py`（修改）：挂载 `rag/wiki/api.py` 路由。
- `tests/test_wiki_store.py`、`tests/test_wiki_chunker.py`、`tests/test_wiki_indexer.py`、`tests/test_gap_remediation.py`、`tests/test_wiki_api.py`（新建测试套件）。

---

### Task 1: Hypertext Wiki Data Model, Storage & Versioning

**Files:**
- Create: `rag/wiki/models.py`
- Create: `rag/wiki/store.py`
- Create: `tests/test_wiki_store.py`

**Interfaces:**
- Consumes: `rag/user_db.py`（用户信息与审计主体）、SQLite 数据库连接机制。
- Produces:
  - `WikiPage`（id, kb_id, title, slug, content, tags, current_revision_id, created_at, updated_at）
  - `WikiRevision`（id, page_id, revision_num, content, delta, author_id, comment, sha256_hash, created_at）
  - `WikiStore`：`create_page()`, `update_page()`, `get_page()`, `list_pages()`, `get_history()`, `rollback_revision()`, `extract_links()`

- [x] **Step 1: Write failing tests**（页面创建与唯一 slug 校验；更新自动生成递增 revision；回滚到历史版本；双向链接 `[[页面名]]` 正向与反向引用提取；跨知识库隔离性测试）
- [x] **Step 2: Run and confirm failure**
- [x] **Step 3: Implement**（实现 SQLite 表结构与外键级联；Markdown 内部链接提取正则与图谱映射；事务性版本创建与回滚）
- [x] **Step 4: Run focused tests**（`pytest tests/test_wiki_store.py` 验证全绿）
- [x] **Step 5: Commit**：`feat(wiki): implement hypertext wiki storage and revision model`.

---

### Task 2: Markdown AST Structure-Aware Chunker

**Files:**
- Create: `rag/wiki/chunker.py`
- Create: `tests/test_wiki_chunker.py`

**Interfaces:**
- Consumes: `rag/wiki/models.py` 的 Wiki 内容与标题、白皮书第 2.2 节结构感知规则。
- Produces:
  - `MarkdownASTChunker(max_chunk_chars=800, min_chunk_chars=100)`
  - `chunk_wiki_page(page: WikiPage) -> list[DocumentChunk]`
  - 每个 Chunk 包含：`text`（以 `[Wiki: {page.title} > {section_path}]` 为前缀）、`metadata`（`heading_level`, `section_path`, `links`, `kb_id`, `chunk_index`）

- [x] **Step 1: Write failing tests**（标题层级嵌套解析验证；表格块禁止中途截断；过长正文在同段落边界智能切分；上下文面包屑自动挂载；空页面与畸形 Markdown 鲁棒性）
- [x] **Step 2: Run and confirm failure**
- [x] **Step 3: Implement**（集成 `markdown-it-py` 解析 AST Token 流；构建 Heading Stack 维护路径面包屑；表格与代码块原子化保护逻辑）
- [x] **Step 4: Run focused tests**（`pytest tests/test_wiki_chunker.py` 验证通过）
- [x] **Step 5: Commit**：`feat(wiki): build structure-aware markdown AST chunker`.

---

### Task 3: Wiki Ingestion & Hybrid Indexing Pipeline

**Files:**
- Create: `rag/wiki/indexer.py`
- Create: `tests/test_wiki_indexer.py`
- Modify: `rag/pipeline.py`（若需接入特定过滤）

**Interfaces:**
- Consumes: Task 1 `WikiStore`、Task 2 `MarkdownASTChunker`、既有 Qdrant 客户端与 SQLite BM25 倒排索引。
- Produces:
  - `WikiIndexer(wiki_store, vector_store, sparse_store)`
  - `sync_page(page_id: int) -> int`（生成切块、更新向量库与 BM25，返回同步块数）
  - `delete_page(page_id: int) -> None`（清理对应向量与关键词记录）
  - `reindex_all(kb_id: str) -> dict`（批量全量同步）

- [x] **Step 1: Write failing tests**（页面创建同步至双路索引；页面修改只更新受影响块，清理陈旧块；页面物理删除后两路索引彻底清除；ACL 与 kb_id 隔离校验）
- [x] **Step 2: Run and confirm failure**
- [x] **Step 3: Implement**（将 AST 切块注入已有的 `DocumentChunk` 流程；执行向量嵌入并写入 Qdrant；更新 SQLite BM25 词频倒排）
- [x] **Step 4: Run focused tests**（`pytest tests/test_wiki_indexer.py` 验证全绿）
- [x] **Step 5: Commit**：`feat(wiki): integrate hybrid vector and BM25 indexing for wiki`.

---

### Task 4: Knowledge Gap Ticket to Wiki Remediation Loop (Track B)

**Files:**
- Create: `rag/evolution/gap_remediation.py`
- Modify: `rag/evolution/worker.py`
- Modify: `rag/evolution/gaps.py`
- Create: `tests/test_gap_remediation.py`

**Interfaces:**
- Consumes: 既有 `knowledge_gap_tickets`、Task 1 `WikiStore`、Task 3 `WikiIndexer`。
- Produces:
  - `GapRemediationService`：`link_ticket_to_wiki(ticket_id, page_id)`
  - Worker 5b 步骤扩充：探测已关联录入 Wiki 或 Excel 的 open 工单，若缺失词已达到召回覆盖阈值，自动扭转状态为 `content_added` 并落库留痕。

- [x] **Step 1: Write failing tests**（工单与 Wiki 页面关联绑定；知识补录入库后 Worker 重检返回 `covered`；工单状态自动由 `open` 推进为 `content_added`；多成员缺失词覆盖度数学判定测试）
- [x] **Step 2: Run and confirm failure**
- [x] **Step 3: Implement**（在工单记录中持久化关联资产引用；Worker 定期重检逻辑增强，增加对增量 Wiki 内容的召回命中判定；闭环后触发审计日志）
- [x] **Step 4: Run focused tests**（`pytest tests/test_gap_remediation.py tests/test_evolution_gaps.py` 零回归）
- [x] **Step 5: Commit**：`feat(evolution): close Track B loop with wiki gap remediation`.

---

### Task 5: Wiki API Endpoints & Administrative Integration

**Files:**
- Create: `rag/wiki/api.py`
- Modify: `rag/api.py`
- Modify: `rag/evolution/api.py`
- Create: `tests/test_wiki_api.py`

**Interfaces:**
- Consumes: Task 1–4 全部能力、`rag/api_security.py` 的鉴权与管理员检查。
- Produces:
  - `GET /wiki/pages`（按 kb_id 列出页面，支持标签与关键词过滤）
  - `POST /wiki/pages`（创建新页面并触发索引）
  - `GET /wiki/pages/{id}`（详情与正向/反向链接）
  - `PUT /wiki/pages/{id}`（提交编辑并自动生成修订版）
  - `POST /wiki/pages/{id}/rollback`（一键回滚指定修订版）
  - `POST /evolution/gap-tickets/{id}/remediate/wiki`（工单一键跳转补录 API）

- [x] **Step 1: Write failing tests**（API 身份验证与权限守卫；CRUD 全生命周期测试；版本回滚端点测试；工单补录关联端点响应结构测试）
- [x] **Step 2: Run and confirm failure**
- [x] **Step 3: Implement**（FastAPI 路由组装；依赖注入与异常处理；跨模块端点挂载）
- [x] **Step 4: Run focused tests**（`pytest tests/test_wiki_api.py` 全量通过）
- [x] **Step 5: Commit**：`feat(api): expose wiki knowledge and gap remediation endpoints`.

---

## Verification & Execution Playbook

完成以上 5 个任务后，执行以下端到端验证命令确保系统闭环：

```powershell
# 1. 运行 Wiki 与工单治理新增测试套件
.\.venv\Scripts\pytest.exe tests/test_wiki_*.py tests/test_gap_remediation.py -v

# 2. 运行自演化与核心链路全量回归网
.\.venv\Scripts\pytest.exe tests/test_evolution*.py tests/test_benchmark*.py tests/test_pipeline.py

# 3. 运行代码质量与风格检查
.\.venv\Scripts\ruff.exe check rag/wiki rag/evolution

# 4. 真实演练：起草工单 -> 录入 Wiki 词条 -> 触发重检 -> 状态转 content_added
.\.venv\Scripts\python.exe scripts/verify_wiki_knowledge_loop.py
```
