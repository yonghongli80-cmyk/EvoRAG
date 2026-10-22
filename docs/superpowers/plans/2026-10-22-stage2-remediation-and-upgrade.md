# 阶段 2：剩余 P2 修复与升级 Phase 0/1（2026-10-22）

> 前置：`2026-10-22-pre-upgrade-remediation-plan.md` 的 P0/P1 已关闭（HEAD `92bd406`）。
> 本阶段目标：把审查中可安全落地的 P2 一并修掉，同时推进 Phase 0（`/ready`、`/metrics`、
> 压测档案）与 Phase 1 配置面（`RAG_QDRANT_URL`、Compose qdrant）。
> **Docker daemon 当前不可用，Phase 1 只做代码/配置/测试，不做运行时迁移。**

## 工作流

| 流 | 范围 | 文件边界 |
| --- | --- | --- |
| S1 API/安全 | B-03~B-08 + A-06 | `routers/auth.py`、`routers/documents.py`、`routers/query.py`、`routers/knowledge.py`、`auth.py`、`repositories/user_repo.py`、`repositories/analysis_repo.py`、`data_sources/db_source.py`、`batch_importer.py` |
| S2 检索/降级 | A-03~A-05、A-07~A-10 | `pipeline.py`、`retriever.py`、`bm25_store.py`、`guard.py`、`memory.py`、`agent.py`、`tools.py`、`concurrency.py`、`context.py`、`versioned_retrieval.py`、`reliability/`、`jev/` |
| S3 自演化 | C-04~C-08、C-10、C-11 | `evolution/`（sandbox/api/worker/rollout/candidate_store/code_patches/service_control） |
| S4 数据 | D-06~D-12 + D 观察 | `materials/archives.py`、`materials/organizer.py`、`spreadsheets/`、`repositories/material_repo.py` |
| S5 前端 | E-04~E-10 | `frontend/src/`，重建 `static/` |
| S6 运维 | F-04、F-06~F-08、F-09/F-10（F-05 配置膨胀留架构） | `start.py`、`deploy.sh`、`render.yaml`、`.github/workflows/ci.yml`、`tests/conftest.py`、`scripts/` |
| S7 升级 | Phase 0 + Phase 1 配置面 | `rag/api.py`、`rag/routers/system.py`、`config.py`、`rag/vector_store.py`、`docker-compose.yml`、`.env.example`、docs、tests |

## 明确不做/留架构

- 真实容器/低权沙箱隔离（C-01 已用 fail-closed 契约收口；容器化随部署环境）。
- PostgreSQL、Redis 多副本、对象存储（Phase 2+，需新环境）。
- F-05 配置字段大清理（触及 120 个字段，随 Phase 2 做）。
- 纯样式/文档 P3，除非顺手。

## 验收

- 每条 P2 带至少一条回归用例；全量 pytest、ruff、前端 Vitest/build 通过。
- `/ready` 能区分依赖未就绪与存活；`/metrics` 只读、无鉴权泄露。
- `RAG_QDRANT_URL` 为空时本地路径行为不变；设置时走 HTTP；有单测。
- Compose 有 qdrant 服务与持久卷；Docker 不可用的事实写进报告。
- 分工作流提交，不混提交。

## 状态

- [x] S1 API/安全 — `d4e67a1`
- [x] S2 检索/降级 — `a8160d2`
- [x] S3 自演化 — `217fa6a`
- [x] S4 数据 — `e1c9974`
- [x] S5 前端 — `1961ed5`
- [x] S6 运维 — `77b036e`（F-05 留架构；`docker-compose.yml` 归 S7）
- [x] S7 升级 Phase 0/1 — `e5a78a1`
- [x] 集成回归与提交

## 实施记录（2026-10-22）

| 项 | 结果 |
| --- | --- |
| 全量 pytest | **2786 passed / 1 skipped，19:53** |
| 前端 Vitest | **298 passed / 39 files** |
| 前端 build | 成功，`static/` 已重建 |
| ruff（全树） | All checks passed |
| `/health` | 200 |
| `/ready` | **200**（SQLite/Qdrant ok；Redis 未运行但 `RAG_REDIS_REQUIRED=false`，组件 `degraded` 不阻塞） |
| `/metrics` | admin 200；匿名 401 |
| admin 登录 / KB / 查询 | 200 / 4 个 KB / 200 |
| 备份 | `status=verified`（前一轮） |

### 行为变化/残留

1. **Redis 可选**：单机默认 `RAG_REDIS_REQUIRED=false`；多副本/共享锁部署必须置 true，
   否则 Redis 故障只降级不阻塞 `/ready`。
2. **E-09 partial**：KB 关联仍走下载再上传；后端没有 document_id 引用端点，留阶段 3。
3. **C-05**：canary 现在要求层 2 `benchmark=passed` + 层 3 `shadow` 至少 1 个样本；
   shadow gate 目前没有生产写入者，真实部署会 409，属有意的 fail-closed。
4. **D-10**：整理批次新增列通过运行时幂等 `ALTER TABLE` 补齐，未改声明式 schema。
5. **Docker 阻塞**：daemon 不可用，Phase 1 只完成配置面/Compose/单测；未 pull 镜像、
   未起 Qdrant server、未做迁移/双进程验收。`docker compose config` 静态校验通过。
6. **前端 E-07** 后端已补 `expected_version` / `metadata_version`；CAS 完整可用。

