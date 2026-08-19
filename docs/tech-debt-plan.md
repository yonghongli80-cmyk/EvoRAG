# RAGv3 技术债修复设计

> **日期：** 2026-06-12
> **状态：** 已完成并归档（4/4，2026-08-19 复核）
> **范围：** BM25 持久化 + SQLite 统一目录 + JWT secret 持久化 + 前端组件化
>
> 原计划第 4 项已由 Vue 3 前端重写完成，不再实施旧的静态 HTML/JS 拆分方案。本文保留为历史设计记录，不再作为待办入口。

---

## 1. BM25 持久化（SQLite chunk 存储） ✅

### 问题

`retriever.py` 每次查询都调用 `_load_all_chunks()` 从 Qdrant 全量加载 chunks 构建 BM25 索引。数据量大时（>1000 chunks）是性能瓶颈。

### 方案

用 SQLite 存储完整 chunks（非倒排索引），索引文档时持久化，查询时直接加载后在内存构建 BM25Okapi。

### 数据模型

```sql
-- data/bm25_index.db
CREATE TABLE IF NOT EXISTS bm25_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    text TEXT NOT NULL,
    doc_name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bm25_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### 工作流程

```
索引时：
  chunks → 存入 SQLite bm25_chunks 表

查询时：
  1. 检查 SQLite 是否有该 collection 的 chunks
  2. 有 → 直接从 SQLite 加载 chunks，在内存构建 BM25Okapi
  3. 无 → 从 Qdrant 全量 scroll + 构建 BM25Okapi + 存入 SQLite
```

**注意：** 实际实现存储完整 chunks 而非倒排索引。每次查询仍需在内存构建 BM25Okapi，但避免了 Qdrant scroll 的网络开销。倒排索引方案（存储 term/tf）效率更高但复杂度也更高，当前方案是折中。

### 文件变更

| 文件 | 改动 |
|------|------|
| `rag/bm25_store.py` | 新建 — SQLite chunk 存储 |
| `rag/retriever.py` | 改造 — 优先从 SQLite 加载 |
| `config.py` | 新增 `bm25_db_path` 配置 |
| `tests/test_bm25_store.py` | 新建 — 6 个测试 |
| `tests/test_retriever_bm25_persistence.py` | 新建 — 2 个集成测试 |

---

## 2. SQLite 统一目录 ✅

### 问题

当前 3 个 SQLite 文件分散在不同位置：
- `memory.db` — 对话记忆（项目根目录）
- `data/users.db` — 用户系统（data/ 目录）
- `data/analysis.db` — 分析数据（data/ 目录）

### 方案

统一放到 `data/` 目录下（已完成）：

```
data/
├── memory.db          # 对话记忆
├── users.db           # 用户系统
├── analysis.db        # 分析数据
├── bm25_index.db      # BM25 倒排索引（新增）
├── upload/            # 上传的文档
└── qdrant_data/       # Qdrant 向量数据
```

### 文件变更

| 文件 | 改动 |
|------|------|
| `config.py` | 更新默认路径为 `data/xxx.db` |
| `rag/memory.py` | 默认 db_path 改为 `data/memory.db` |
| `rag/tracker.py` | 默认 db_path 改为 `data/memory.db` |
| `rag/pipeline.py` | 传递新路径 |
| `start.py` | 确保 `data/` 目录存在 |

---

## 3. JWT Secret 持久化 ✅

### 问题

JWT secret 未设置时每次重启随机生成，导致：
- 重启后所有用户 token 失效
- 多实例部署时 token 不互通

### 方案

首次启动生成 secret，写入 `data/jwt_secret.txt`，后续读取。（已完成）

### 实现

```python
# rag/auth.py
_JWT_SECRET_FILE = Path(__file__).resolve().parent.parent / "data" / "jwt_secret.txt"

def _load_or_create_secret() -> str:
    env_secret = os.environ.get("RAG_JWT_SECRET")
    if env_secret:
        return env_secret
    if _JWT_SECRET_FILE.exists():
        return _JWT_SECRET_FILE.read_text().strip()
    # 首次生成
    secret = secrets.token_urlsafe(32)
    _JWT_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _JWT_SECRET_FILE.write_text(secret)
    return secret

_JWT_SECRET = _load_or_create_secret()
```

### 文件变更

| 文件 | 改动 |
|------|------|
| `rag/auth.py` | 改用 `_load_or_create_secret()` |
| `data/jwt_secret.txt` | 自动生成（加入 .gitignore） |
| `.gitignore` | 添加 `data/jwt_secret.txt` |

---

## 4. 前端组件化 ✅

### 问题

`static/index.html` 1690 行单文件，难维护。

### 原方案（已由 Vue 3 重写替代）

拆分为多个文件：

```
static/
├── index.html          # HTML 结构（~200 行）
├── css/
│   └── style.css       # 样式（~400 行）
├── js/
│   ├── api.js          # API 调用封装（~100 行）
│   ├── auth.js         # 登录/注册逻辑（~100 行）
│   ├── chat.js         # 聊天逻辑（~200 行）
│   ├── files.js        # 文件管理（~100 行）
│   └── sidebar.js      # 侧边栏逻辑（~100 行）
└── favicon.ico         # 图标
```

**当前结果：** 前端已经迁移为 `frontend/src/` 下的 Vue 3 + TypeScript 工程，并通过 Vite 构建到 `static/`。旧静态文件拆分方案不再执行。

---

## 实现顺序

| 阶段 | 内容 | 依赖 | 状态 |
|------|------|------|------|
| 1 | SQLite 统一目录 | 无 | ✅ 已完成 |
| 2 | JWT Secret 持久化 | 无 | ✅ 已完成 |
| 3 | BM25 持久化 | 阶段 1 | ✅ 已完成 |
| 4 | 前端组件化 | — | ✅ 已由 Vue 3 重写完成 |

---

## 验收标准

1. ✅ `data/` 目录下有 4 个 SQLite 文件（memory、users、analysis、bm25_index）
2. ✅ 重启后 JWT token 不失效（`data/jwt_secret.txt` 持久化）
3. ✅ BM25 chunks 持久化到 SQLite，查询时不从 Qdrant scroll（从 SQLite 加载后在内存构建 BM25Okapi）
4. ✅ 248 个测试全过
