# RAGv3 全库代码审查 Spec（架构升级前，2026-10-22）

> 状态：**审查进行中**。本文件定义范围、方法、证据标准和产出，不是整改实现。
> 基线：`HEAD=67da261`，工作树干净；`pytest -q` **2366 passed / 0 failed**（31:39）；
> `ruff check .` 全绿；服务 `127.0.0.1:8080` healthy；benchmark PASS（2026-10-22）。
> 上游流程：`docs/superpowers/specs/` + `plans/`（本仓库既有的 superpowers 产物约定）。

---

## 1. 目标

在 Qdrant server / PostgreSQL / Redis / 对象存储 / 多副本升级**之前**，对当前单机代码做一次
证据驱动的全库审查，回答三个问题：

1. 现有代码还有哪些 **P0/P1 真实缺陷**会阻塞或污染架构升级？
2. 历史审查（2026-10-14 / 10-16 / 10-17）的结论哪些已修、哪些仍开、哪些已失效？
3. 升级前必须先修的清单是什么，哪些可以随升级一起做，哪些只是记录？

**本阶段不改产品代码、不写数据库、不停服务。**

---

## 2. 范围

| 域 | 代码 | 审查重点 |
| --- | --- | --- |
| A 检索与生成 | `rag/pipeline.py`、`retriever.py`、`reranker.py`、`embedder.py`、`generator.py`、`guard.py`、`agent.py`、`tools.py`、`reliability/`、`jev/`、`query_rewriter.py`、`versioned_retrieval.py`、`vector_store.py`、`bm25_store.py` | 路由/证据门、改写与双路重排、资源生命周期、流式错误、注入、版本过滤、并发 |
| B API 与权限 | `rag/api.py`、`routers/`、`api_security.py`、`auth.py`、`permissions.py`、`login_throttle.py`、`org_auth/`、`user_db.py`、`repositories/user_repo.py`、`knowledge_base.py`、`kb_metadata.py`、`batch_importer.py`、`document_sources/`、`data_sources/` | 认证/越权/租户边界、IDOR、SSRF、上传、JWT/Cookie/API Key、管理员绕过、SQL 注入 |
| C 自演化 | `rag/evolution/` | 五层门禁、沙箱逃逸、命令/路径注入、状态机、预算与并发、审计、回放 |
| D 数据子系统 | `rag/materials/`、`spreadsheets/`、`wiki/`、`repositories/`、`db/`、`document_versions.py`、`document_version_store.py`、`backups/` | 压缩包/归档、Zip Slip、路径穿越、FTS/SQL、迁移、数据完整性、并发、限额 |
| E 前端 | `frontend/src/`（48 Vue + api/stores/router） | XSS/DOMPurify、token 处理、路由守卫、错误态、竞态、注入、缓存 |
| F 运维与测试 | `config.py`、`start.py`、`Dockerfile`、`docker-compose.yml`、`deploy.sh`、`render.yaml`、`.github/`、`scripts/`、`tests/`、`pyproject.toml`、锁文件 | 密钥、权限、构建上下文、健康检查、依赖风险、测试隔离/抖动、CI 门禁、配置校验 |

**不在本阶段**：实际修复、数据库清理、Qdrant/Postgres 迁移、前端重设计、性能调优实施。

---

## 3. 方法

### 3.1 基线（已完成）
- Git/工作树/分支；
- 全量测试、ruff、benchmark、locust（已有记录）；
- 服务健康。

### 3.2 历史差量
对照以下文档，把每条历史发现标记为 `fixed / open / stale / superseded`：
- `docs/superpowers/specs/2026-10-14-comprehensive-codebase-review.md`
- `docs/superpowers/specs/2026-10-14-second-deep-codebase-review.md`
- `docs/superpowers/specs/2026-10-16-full-codebase-review-remediation-design.md`
- `docs/superpowers/plans/2026-10-17-codebase-deep-audit-and-remediation-master-plan.md`
- `docs/superpowers/specs/2026-10-17-codebase-antidegradation-and-modularization-design.md`
- `docs/superpowers/specs/2026-10-17-frontend-redesign-and-modernization-design.md`
- `docs/code-review-2026-06-26.md`、`docs/project-review.md`

### 3.3 域级并行审查
6 个只读子代理按 A–F 域独立审查，输出统一格式；文件不重叠、不写任何文件。

### 3.4 跨域静态扫描（主代理）
- `except Exception` / 裸 `except` 的静默吞错；
- `# noqa`、`type: ignore`、`suppress(Exception)`；
- `eval` / `exec` / `subprocess` / `shell=True` / `pickle` / `yaml.load`；
- 字符串拼 SQL、`f"SELECT ... {`、`text(`；
- `threading.Lock` / `asyncio.Lock` / `create_task` / 线程池生命周期；
- `Path(...) / user_input`、`resolve()`、`is_relative_to`；
- 认证相关：`is_admin`、`Depends(require_user)`、`Header("")`、`X-API-Key`；
- `TODO/FIXME/HACK/XXX`；
- 配置项数量与未使用开关；
- Docker/CI/依赖/测试隔离。

### 3.5 证据复核
主代理对每条 P0/P1 读代码复核；子代理结论不能直接进最终清单。
每条发现必须有 `file:line`、触发条件、影响、建议修复方向、测试缺口。

---

## 4. 严重级别

| 级别 | 定义 | 例子 |
| --- | --- | --- |
| **P0** | 安全边界突破、数据丢失/污染、生产不可用 | 未授权访问、路径穿越写文件、删除生产数据、启动即崩 |
| **P1** | 高概率错误/越权/资源泄漏/回归风险 | 权限绕过边缘、连接泄漏、流式重复/丢事件、迁移不可逆 |
| **P2** | 中等缺陷、可观测性/并发/性能问题 | 静默吞错、缓存不一致、锁粒度过大、N+1 |
| **P3** | 清理/一致性/文档/测试缺口 | 死代码、命名、注释过期、缺边界用例 |

---

## 5. 输出格式（每条发现）

```text
ID: <域>-<序号>
Severity: P0/P1/P2/P3
Title: <一句话>
Evidence: <file:line> + <关键代码/行为>
Trigger: <什么输入/状态下发生>
Impact: <安全/数据/可用性/维护性>
Suggested fix: <方向，不实现>
Test gap: <缺什么用例/如何验证>
Confidence: high/medium/low
Prior status: fixed/open/stale/new（附历史文档）
```

---

## 6. 验收

1. A–F 六域均有审查结果，且每域至少覆盖“认证/边界/错误处理/并发/测试缺口”中的适用项。
2. P0/P1 全部由主代理复核过代码行；无法复现的标 `confidence: low` 并说明。
3. 历史审查的每条 open 项有现状结论（fixed/open/stale）。
4. 产出 `findings` 报告 + `remediation plan`，明确“升级前必修 / 随升级做 / 只记录”。
5. 审查阶段 `git status` 只允许新增文档；不改产品代码、不写数据库。
