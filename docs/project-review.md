# RAGv3 项目审阅报告

> **归档说明（2026-08-19）：** 本文是 2026-06-21 的历史快照，其中“当前状态与待办”已被后续实现取代。Docker、CI/CD、Vue 前端、文件工作流、版本与表格能力均应以当前代码和 `docs/README.md` 为准，不得把本文的旧清单重新当作现行计划。
>
> 审阅日期：2026-06-21

## 一、项目定位

**RAGv3** 是一个**基于检索增强生成的智能知识库系统**，面向中文场景。项目定位为个人工程项目，目标达到企业级质量水准。

核心能力：上传文档 → 自动向量化索引 → 混合检索+重排序 → LLM生成回答，同时支持 Agent 工具调用、多轮对话、反馈优化、知识差距分析等进阶功能。

---

## 二、技术架构全景

### 后端（Python 3.12 + FastAPI）

| 模块 | 职责 | 核心文件 |
|------|------|----------|
| **文档处理** | 加载(txt/md/pdf/docx/xlsx) → 清洗(编码检测/去重) → 分块(500 tokens, 80 overlap) | `loader.py`, `cleaner.py`, `chunker.py` |
| **向量化** | BGE-M3 embedding (1024维) + Qdrant 本地存储 | `embedder.py`, `vector_store.py` |
| **混合检索** | Dense(Qdrant cosine) + Sparse(BM25Okapi+jieba) + RRF融合(k=60) | `retriever.py`, `bm25_store.py` |
| **重排序** | Bailian gte-rerank-v2 API，失败降级为原始顺序 | `reranker.py` |
| **生成** | DeepSeek v4 flash，支持同步+SSE流式 | `generator.py` |
| **对话记忆** | SQLite持久化，10轮自动摘要 | `memory.py` |
| **Agent** | LangChain ReAct循环，4个工具(retrieve/calculate/sql/plot)，两层反思 | `agent.py`, `tools.py` |
| **路由** | LLM判断问题走 RAG 路径还是 Agent 路径 | `pipeline.py` 中的 `route_question()` |
| **安全** | 28种注入模式检测，输入消毒，输出审计，SQL注入防护 | `guard.py` |
| **容错** | 指数退避重试 + 三态熔断器 + BloomFilter防穿透 + TTL抖动防雪崩 | `resilience.py` |
| **认证** | JWT + PBKDF2-HMAC-SHA256 (260k迭代) | `auth.py`, `user_db.py` |
| **反馈优化** | 正反馈+0.1/负反馈-0.1调整chunk权重，影响RRF融合排序 | `feedback_processor.py` |
| **知识差距** | 检索分数<0.3或回答含"未找到"时记录gap | `gap_analyzer.py` |
| **数据源** | 抽象基类 + RSS/DB/API三种数据源适配器 | `data_sources/` |
| **评估** | JSONL数据集 → 命中率 + P95延迟 | `eval.py` |

### 前端（Vue 3 + TypeScript + Vite）

| 模块 | 说明 |
|------|------|
| **框架** | Vue 3 Composition API + Pinia + Vue Router |
| **UI库** | Element Plus + 自定义CSS Design Tokens (8px网格) |
| **状态管理** | 3个Store: auth(token+用户)、chat(会话+消息+流式)、files(文件列表) |
| **路由** | 4个主页面: 文件模式/知识库模式/分析模式/登录 |
| **流式对话** | 原生fetch + ReadableStream消费SSE，CJK字符截断处理 |
| **安全** | DOMPurify过滤消息HTML，Axios拦截器统一处理401/403/5xx |
| **测试** | Vitest + @vue/test-utils，9个测试文件 |

### 数据存储

| 存储 | 用途 | 文件 |
|------|------|------|
| **Qdrant** (本地文件模式) | 向量数据库，cosine相似度搜索 | `qdrant_data/` |
| **SQLite** × 4 | 用户/会话(`users.db`)、对话记忆(`memory.db`)、BM25索引(`bm25_index.db`)、分析(`analysis.db`) | `data/` |

---

## 三、完整数据流

```
用户提问
  ↓
API (api.py) → Pipeline (pipeline.py)
  ↓
sanitize_input → injection_guard → cache_check
  ↓
LLM路由 → "rag" / "agent"
  ↓                          ↓
RAG路径                     Agent路径
  ↓                          ↓
query_rewrite              LangChain ReAct + 4工具
  ↓                          ↓
hybrid_retrieve             self-reflection (最多2轮)
(dense + sparse + RRF)
  ↓
rerank (Bailian API)
  ↓
feedback_weight_adjust
  ↓
build_messages (memory + context)
  ↓
LLM generate (DeepSeek)
  ↓
output_filter → persist_memory → cache_update → gap_analysis
  ↓
返回 answer + sources
```

---

## 四、测试覆盖

- **后端**: 50+ 测试文件，248个测试用例，严格 TDD，所有外部API mock，SQLite用 `:memory:`
- **前端**: 9个测试文件，覆盖stores/components/views/composables/utils
- **覆盖模块**: 从基础的loader/chunker到高级的pipeline/concurrent/feedback/eval全覆盖

---

## 五、设计文档体系

项目有**极其完整**的设计文档体系（这在个人项目中非常少见）：

- **specs/**: 12份设计规格文档，涵盖工业级容错、缓存加固、数据清洗、Prompt管理、Agent反思、UI重设计等
- **plans/**: 10份实施计划，含详细的TDD步骤和任务分解
- **dev-log.md**: 开发日志

---

## 六、当前状态与待办

### 已完成

- ✅ 核心RAG流水线（检索+生成+对话记忆）
- ✅ 混合检索 + 重排序 + 反馈优化
- ✅ Agent系统（4工具 + 反思机制）
- ✅ 多用户认证 + JWT
- ✅ 多知识库管理
- ✅ SSE流式输出
- ✅ 安全防护（注入检测/输入消毒/输出审计）
- ✅ 容错体系（重试/熔断/缓存加固）
- ✅ Vue 3前端重写
- ✅ 增量索引（启动时只索引新增/修改文件）

### 未完成（根据设计文档）

- ❌ Docker部署（Dockerfile/docker-compose未创建）
- ❌ CI/CD流水线
- ❌ 反馈驱动的检索权重持久化生效（`feedback_processor`记录权重但retriever读取可能不完整）
- ❌ 前端 `FilesView` 中的批量导入功能（组件存在但路由未挂载）
- ❌ 后端全异步改造（当前用 `asyncio.to_thread()` 包装同步代码）
- ❌ 评估数据集扩充（当前eval框架完整但数据集较小）

---

## 七、工程质量评价

| 维度 | 评分 | 说明 |
|------|------|------|
| **架构设计** | ⭐⭐⭐⭐⭐ | 模块职责清晰，pipeline编排合理，容错/安全/缓存层层防护 |
| **代码质量** | ⭐⭐⭐⭐ | 类型注解完整，日志规范，pre-commit+ruff规范，但pipeline.py略显臃肿 |
| **测试覆盖** | ⭐⭐⭐⭐⭐ | 248个测试用例，TDD实践，mock策略合理 |
| **文档完整性** | ⭐⭐⭐⭐⭐ | 设计规格+实施计划+开发日志，远超一般个人项目 |
| **前端质量** | ⭐⭐⭐⭐ | Composition API + Pinia + Design Tokens，架构规范，但部分组件可进一步拆分 |
| **可部署性** | ⭐⭐⭐ | 缺Docker/CI/CD，启动依赖Java（PDF解析），环境配置较复杂 |

---

**总结**：这是一个完成度很高、工程质量远超一般个人项目的RAG系统。架构合理、测试充分、文档齐全。主要短板在部署工程化（Docker/CI/CD）和部分功能的收尾工作。
