# 全项目完整代码深度审查报告（Superpowers Deep Code Review）

日期：2026-10-14  
审查级别：严格 / 全量架构审查（Superpowers Strict Standard）  
状态：审查完成（Findings Identified & Remediation Proposed）

---

## 1. 审查概述与质量基线

本次代码审查依据 Superpowers 工程规范与高可用 RAG 系统架构设计准则，对项目整体代码库进行了无死角的穿透式严格审查。审查范围涵盖：
1. **认证、鉴权与安全边界**（RBAC / 组织多租户 / 细粒度资源控制 / 注入与穿越防御）；
2. **双模态知识底座体系**（关系/表格知识子系统 + 超文本 Wiki 知识子系统）；
3. **混合检索与向量索引一致性**（Qdrant 稠密向量 + SQLite BM25 词频倒排 + 文档版本生命周期）；
4. **自主演进与免疫系统隔离性**（Track A 代码自修复与 Track B 知识缺失治理的双轨独立性、不可篡改核心区防护）；
5. **并发、异步调度与数据持久化安全**（FastAPI 线程调度、SQLite 锁机制与事务完整性）。

### 基线质量扫描指标
- **静态代码检查**：`ruff check .` 全局扫描 **0 告警 / 0 错误**，格式与类型规范保持最高水准。
- **专项测试矩阵**：
  - 超文本 Wiki 子系统测试网（`tests/test_wiki_*.py`）：**28/28 100% 通过**；
  - 自演进 Track B 闭环测试网（`tests/test_gap_remediation.py`）：**7/7 100% 通过**；
  - 自演进免疫与安全测试网（`tests/test_evolution_*.py`）：**80/80 100% 通过**；
  - 全量回归测试套件（243 个测试文件，2132 个测试用例）：**2132/2132 100% 全量通过**（0 失败，0 错误）。

---

## 2. 审查维度分析与发现清单

本次审查将代码缺陷与潜在隐患严格按严重度分为 **Critical（致命）**、**High（高危）**、**Medium（中危）**、**Low（低危/纵深防御）** 四级。

### [CRITICAL] 致命缺陷
**未检出致命安全漏洞**（无未认证远程代码执行、无明文 SQL 拼接注入、无未受控的任意文件读写与越权提权）。

---

### [HIGH] 高危漏洞

#### 发现 SEC-01：Wiki REST API 接口缺失知识库维度的细粒度权限校验（BFLAC / IDOR 风险）
- **涉及文件**：[`rag/wiki/api.py`](file:///d:/Desktop/RAGv3/rag/wiki/api.py#L59-L198)
- **代码定位**：
  - `create_page` (L73)
  - `get_page` (L100)
  - `update_page` (L122)
  - `delete_page` (L150)
  - `get_page_history` (L167)
  - `rollback_page` (L177)
- **问题机理**：
  上述端点当前仅声明了依赖 `user: dict = Depends(require_user)`，即仅验证了用户是否持有合法的登录凭据。而在业务逻辑中，直接使用路径参数 `page_id` 或请求体 `kb_id` 对 `WikiStore` 进行增删改查及版本回滚，未通过 `require_kb_view(user_db, kb_id, user)` 或 `require_kb_edit(user_db, kb_id, user)` 进行组织知识库访问控制校验。
- **潜在危害**：
  跨部门权限越权。任意已认证的普通员工若获知或枚举其他受限知识库（如财务部、高管决策库）的 `page_id`，可以直接查看敏感 SOP 页面、修改内容、恶意删除甚至秒级回滚页面，绕过了系统的组织权限策略模型。
- **修复方案**：
  1. 在 `create_page` 中，调用 `require_kb_edit(request.app.state.user_db, body.kb_id, user)`；
  2. 在 `get_page` 与 `get_page_history` 中，先查询页面所在 `kb_id`，调用 `require_kb_view(request.app.state.user_db, page.kb_id, user)`；
  3. 在 `update_page`、`delete_page`、`rollback_page` 中，调用 `require_kb_edit(request.app.state.user_db, page.kb_id, user)`；
  4. 在 `list_pages` 中，若指定 `kb_id` 则校验 view 权限；若未指定 `kb_id` 且非管理员，则仅列出用户有权查看的知识库页面。

---

### [MEDIUM] 中危隐患

#### 发现 DAT-01：Wiki 页面重命名时 Qdrant 稠密向量索引存在残留幽灵点（Ghost Points）
- **涉及文件**：[`rag/wiki/indexer.py`](file:///d:/Desktop/RAGv3/rag/wiki/indexer.py#L42-L72)、[`rag/vector_store.py`](file:///d:/Desktop/RAGv3/rag/vector_store.py#L278-L295)
- **代码定位**：`WikiIndexer.sync_page` -> `add_to_collection`
- **问题机理**：
  `vector_store.py` 的 `add_to_collection` 在清理陈旧向量点时，通过 `scroll` 匹配 `doc_name == chunk.doc_name` 获取已有 ID 并做差集清理。在 Wiki 场景中，`chunk.doc_name` 映射为页面的 `title`。当用户将 Wiki 页面标题从 `原标题` 改为 `新标题` 时，新生成的分块其 `doc_name` 均为 `新标题`。因此，`existing_ids` 只能找到当前以 `新标题` 存储的点，而之前以 `原标题` 写入 Qdrant 的旧向量点不会被匹配或删除。
- **潜在危害**：
  Qdrant 向量库中永久残留孤儿向量（Ghost Points）。当用户执行混合语义检索时，旧标题的历史块仍可能被高分召回，引发过时知识幻觉。
- **修复方案**：
  在 `WikiStore.update_page` 中若检测到 `old_title != new_title`，或者在 `WikiIndexer.sync_page(page_id, old_title=None)` 中，先显式执行 `delete_doc(kb_id, old_title)`，彻底清除旧标题对应的一切向量点。

#### 发现 CONC-01：WikiStore 与 FailureTraceStore 未启用 SQLite WAL 模式与并发超时设置
- **涉及文件**：[`rag/wiki/store.py`](file:///d:/Desktop/RAGv3/rag/wiki/store.py#L28-L40)、[`rag/evolution/store.py`](file:///d:/Desktop/RAGv3/rag/evolution/store.py#L205-L215)
- **问题机理**：
  核心数据库 `rag/user_db.py` 严格配置了：
  ```python
  self._conn.execute("PRAGMA journal_mode=WAL")
  self._conn.execute("PRAGMA busy_timeout=30000")
  ```
  而在 `rag/wiki/store.py` 与 `rag/evolution/store.py` 中，初始化仅设置了 `foreign_keys = ON`（或仅设置了 Python 层连接 timeout），未执行 SQLite 原生 `journal_mode=WAL`。
- **潜在危害**：
  在默认的 `DELETE/ROLLBACK` 日志模式下，写事务会获取排他锁并阻塞一切并发读操作。在多进程 Uvicorn 或后台任务并发（例如后台演进 Worker 在写 Trace，前端用户在查 Wiki）的高负载场景下，容易触发 `sqlite3.OperationalError: database is locked`。
- **修复方案**：
  在 `WikiStore._init_schema` 与 `FailureTraceStore.__init__` 中统一补充 `PRAGMA journal_mode=WAL;` 与 `PRAGMA busy_timeout=30000;`。

---

### [LOW / 纵深防御] 架构防御性增强

#### 发现 EVO-01：自演进 Track A 补丁禁止修改黑名单未显式包含 `rag/wiki/**`
- **涉及文件**：[`rag/evolution/code_patches.py`](file:///d:/Desktop/RAGv3/rag/evolution/code_patches.py#L64-L79)
- **代码定位**：`FORBIDDEN_GLOBS`
- **问题机理**：
  自演进系统分层设计中，Layer 1 的 `FORBIDDEN_GLOBS` 保护了安全区、权限区、测试和核心规则。当前五层自演进中的 `code` 兜底层白名单为 `("rag/**",)`。虽然前置诊断器与测试网会拦截荒谬补丁，但在确定性第一道静态门禁中尚未将 `rag/wiki/**` 显式列入绝对不可变黑名单。
- **架构意义**：
  系统宪章明确定义：**代码自修复（Track A）与知识采编闭环（Track B）物理正交隔离**。知识缺失严禁通过修改代码逻辑来特判硬编码。将 `rag/wiki/**` 列入 `FORBIDDEN_GLOBS` 可在第一道静态门禁彻底杜绝大模型代码补丁试图侵入 Wiki 领域的可能性。
- **修复方案**：
  在 `FORBIDDEN_GLOBS` 中追加 `"rag/wiki/**"`。

---

## 3. Superpowers 审查整改行动计划（Remediation Plan）

遵循 Superpowers 的 **严格 TDD（Red-Green-Refactor）** 与 **全量回归保障机制**，整改步骤规划如下：

| 任务编号 | 任务分类 | 治理目标 | 预期成果 |
| :--- | :--- | :--- | :--- |
| **Task 1** | **[HIGH] 安全加固** | 为 `rag/wiki/api.py` 注入完整组织权限校验 | 编写越权攻击失败测试（Red），实现鉴权拦截后转绿（Green） |
| **Task 2** | **[MEDIUM] 数据一致性** | 修复 Wiki 页面改名导致的 Qdrant 幽灵点残留 | 编写改名再检索断言用例，实现旧标题向量原子清除 |
| **Task 3** | **[MEDIUM] 并发加固** | 为 `WikiStore` 与 `FailureTraceStore` 配置 WAL 模式与 busy_timeout | 统一全系统 SQLite 并发事务规范，消除读写锁阻塞 |
| **Task 4** | **[LOW] 免疫边界** | 在 `code_patches.py` 的 `FORBIDDEN_GLOBS` 中封锁 `rag/wiki/**` | 阻断任何代码自演进对 Wiki 子系统的修改企图 |
| **Task 5** | **全量验证与审计** | 运行全量回归测试网与前端冒烟测试，提交合规 Git 记录 | 确保 0 违规、0 回退、全绿交付 |

---

## 4. 结论与建议

经过对全项目完整代码的严格穿透审查，该项目的整体工程质量、架构解耦度、安全性与测试覆盖率均处于极高水准：
- **核心逻辑极为稳健**：SQL 全部使用参数化绑定防注入；路径解析严格走安全白名单与安全校验；自演进核心保护区具有坚实免疫测试防护。
- **本次发现的 4 项治理点**（1 项高危权限缺口、2 项中危一致性/并发隐患、1 项纵深防御增强）逻辑清晰、边界明确，建议立即采用 Superpowers 敏捷修复流程逐项闭环收口。
